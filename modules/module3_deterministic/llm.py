import os

import httpx
import openai
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langfuse import Langfuse
from langfuse.langchain import CallbackHandler

# Input: none (reads LLM/Langfuse credentials from the repo-root .env). Output: a shared
# ChatOpenAI client bound to a self-hosted Qwen3.8 host, and a Langfuse CallbackHandler
# for LangGraph tracing. Algorithm: load .env once at import time, construct a single
# Langfuse() client (which auto-reads LANGFUSE_PUBLIC_KEY/SECRET_KEY/BASE_URL) so the
# CallbackHandler picks it up as its default client, and expose get_llm()/get_langfuse_handler()
# so every node/run script shares the same configured instances. get_llm() pins an explicit
# request timeout and retry count: the openai SDK defaults to a 600-second read timeout with 2
# retries, so a single dropped response stalls one branch for up to 30 minutes with no error.
# backend selects which Qwen3.8 host to talk to, so one node can use a different one. Responses are
# streamed: the read timeout then bounds the gap between tokens instead of the whole answer, since one
# Qwen3.8 thinking call (e.g. fetch_criteria on a long Merck page) can run past 20 minutes.


load_dotenv()

# Which Qwen3.8 host this process talks to (GPU200 | INFER | DGX); its URL, model and key are the
# QWEN38_<server>_URL/_MODEL/_API_KEY entries of the repo-root .env, never in code. A batch splits
# patients across hosts by setting QWEN_SERVER per process (run_batch_full_qwen38.sh).
QWEN_SERVER = os.environ.get("QWEN_SERVER", "GPU200")
_SERVER_CONCURRENCY = {"GPU200": 8, "INFER": 2, "DGX": 2}
# The hosts as backends qwen38_gpu200 / qwen38_infer / qwen38_dgx: (url, model, env var holding the key).
BACKENDS = {f"qwen38_{s.lower()}": (os.environ.get(f"QWEN38_{s}_URL"), os.environ.get(f"QWEN38_{s}_MODEL"),
                                    f"QWEN38_{s}_API_KEY") for s in _SERVER_CONCURRENCY}
DEFAULT_BACKEND = f"qwen38_{QWEN_SERVER.lower()}"
# Greedy decoding (temperature 0) makes Qwen3.8's reasoning ramble ~4x longer; use vendor sampling.
_TEMPERATURE = 0.6
_TOP_P = 0.95
_REQUEST_TIMEOUT = httpx.Timeout(connect=10.0, read=1200.0, write=60.0, pool=60.0)
_MAX_RETRIES = 5
# Requests each host takes at once: gpu200 decodes ~8 streams (more just queue, measured 2026-10-05);
# the A5000 (INFER) and DGX hosts admit 2 per key and answer a third with 429 (measured 2026-10-06).
# Graphs and collapse run at most this many LLM calls at once; 429s are retried with SDK backoff.
MAX_CONCURRENT_REQUESTS = _SERVER_CONCURRENCY[QWEN_SERVER]
_STRUCTURED_ATTEMPTS = 8
# Output cap (reasoning included) when a caller sets none: the longest healthy call measured ~35k tokens
# (fetch_criteria on a long Merck page), while a runaway thinking loop streamed 50k+ for over an hour on
# the shared host. A capped reply fails to parse and get_structured_llm() retries it.
_DEFAULT_MAX_TOKENS = 40960

Langfuse()


def get_llm(temperature: float = _TEMPERATURE, backend: str = DEFAULT_BACKEND,
            max_tokens: int | None = None) -> ChatOpenAI:
    base_url, model, key_env = BACKENDS[backend]
    return ChatOpenAI(
        model=model,
        base_url=base_url,
        api_key=os.environ[key_env],
        temperature=temperature,
        top_p=_TOP_P,
        max_tokens=max_tokens or _DEFAULT_MAX_TOKENS,
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
