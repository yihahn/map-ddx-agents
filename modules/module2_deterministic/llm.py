import os

import httpx
import openai
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langfuse import Langfuse
from langfuse.langchain import CallbackHandler

# Input: none (reads LLM/Langfuse credentials from the repo-root .env). Output: a shared
# ChatOpenAI client bound to the self-hosted Qwen3.8 endpoint, and a Langfuse CallbackHandler
# for LangGraph tracing. Algorithm: load .env once at import time, construct a single
# Langfuse() client (which auto-reads LANGFUSE_PUBLIC_KEY/SECRET_KEY/BASE_URL) so the
# CallbackHandler picks it up as its default client, and expose get_llm()/get_langfuse_handler()
# so every node/run script shares the same configured instances.
# Responses are streamed, so the read timeout bounds the gap between tokens rather than the whole
# answer — one Qwen3.8 thinking call can run for many minutes under load.

load_dotenv()

# Which Qwen3.8 host this process talks to (GPU200 | INFER | DGX); its URL, model and key are the
# QWEN38_<server>_URL/_MODEL/_API_KEY entries of the repo-root .env, never in code. A batch splits
# patients across hosts by setting QWEN_SERVER per process (run_batch_full_qwen38.sh).
QWEN_SERVER = os.environ.get("QWEN_SERVER", "GPU200")
# Qwen3.8 is a reasoning model: greedy decoding (temperature 0) makes its thinking ramble ~4x
# longer (measured 198s vs 52s on one call), so use the vendor-recommended sampling instead.
_TEMPERATURE = 0.6
_TOP_P = 0.95
# Explicit timeout/retries: the openai SDK default (600s read, 2 retries) can stall a branch for
# 30 minutes silently, and queued requests on the shared Qwen server need more than 240s.
_REQUEST_TIMEOUT = httpx.Timeout(connect=10.0, read=1200.0, write=60.0, pool=60.0)
_MAX_RETRIES = 5
# Requests each host takes at once: gpu200 decodes ~8 streams (more just queue, measured 2026-10-05);
# the A5000 (INFER) and DGX hosts admit 2 per key and answer a third with 429 (measured 2026-10-06).
# Graphs run at most this many LLM-calling nodes at once; 429s are retried with SDK backoff.
_SERVER_CONCURRENCY = {"GPU200": 8, "INFER": 2, "DGX": 2}
MAX_CONCURRENT_REQUESTS = _SERVER_CONCURRENCY[QWEN_SERVER]
_STRUCTURED_ATTEMPTS = 8
# Output cap (reasoning included) when a caller sets none: the longest healthy call measured ~35k tokens
# (fetch_criteria on a long Merck page), while a runaway thinking loop streamed 50k+ for over an hour on
# the shared host. A capped reply fails to parse and get_structured_llm() retries it.
_DEFAULT_MAX_TOKENS = 40960

Langfuse()


def get_llm(temperature: float = _TEMPERATURE) -> ChatOpenAI:
    return ChatOpenAI(
        model=os.environ[f"QWEN38_{QWEN_SERVER}_MODEL"],
        base_url=os.environ[f"QWEN38_{QWEN_SERVER}_URL"],
        api_key=os.environ[f"QWEN38_{QWEN_SERVER}_API_KEY"],
        temperature=temperature,
        top_p=_TOP_P,
        max_tokens=_DEFAULT_MAX_TOKENS,
        timeout=_REQUEST_TIMEOUT,
        max_retries=_MAX_RETRIES,
        streaming=True,
    )


def get_structured_llm(schema, **kwargs):
    """get_llm(**kwargs).with_structured_output(schema), retried with exponential backoff (10 s up to
    2 min) when the reply cannot be parsed — the Qwen host now and then returns an empty message body,
    which langchain raises as a ValueError, or a runaway reasoning loop hits the output cap
    (LengthFinishReasonError) — or when the host answers 429: the 2-request limit is per
    key and other users share it, so a slot can stay taken for minutes, past the SDK's short retries."""
    return get_llm(**kwargs).with_structured_output(schema).with_retry(
        retry_if_exception_type=(ValueError, openai.RateLimitError, openai.LengthFinishReasonError), stop_after_attempt=_STRUCTURED_ATTEMPTS,
        exponential_jitter_params={"initial": 10, "max": 120})


def get_langfuse_handler() -> CallbackHandler:
    return CallbackHandler()
