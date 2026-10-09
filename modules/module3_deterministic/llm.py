import httpx
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langfuse import Langfuse
from langfuse.langchain import CallbackHandler

from ..backends import ACTIVE, BACKENDS, REASONING_EFFORT, THINKING
from ..throttle import llm_http_client

# Input: none (reads LLM/Langfuse credentials from the repo-root .env). Output: a shared
# ChatOpenAI client bound to the self-hosted LLM endpoint, and a Langfuse CallbackHandler
# for LangGraph tracing. Algorithm: load .env once at import time, construct a single
# Langfuse() client (which auto-reads LANGFUSE_PUBLIC_KEY/SECRET_KEY/BASE_URL) so the
# CallbackHandler picks it up as its default client, and expose get_llm()/get_langfuse_handler()
# so every node/run script shares the same configured instances. get_llm() pins an explicit
# request timeout and retry count: the openai SDK defaults to a 600-second read timeout with 2
# retries, so a single dropped response stalls one branch for up to 30 minutes with no error.
# backend selects which self-hosted model to talk to, so one node can use a different one.


load_dotenv()

# Self-hosted backends, selected per node by name from modules/backends.py. The default is the one
# LLM_BACKEND names for the whole run — qwen3.8-27b, where it used to be google/gemma-4-26B-A4B-it
# at 80k on :8000, a host that no longer answers. The model comparison in
# reports/readme/module3_key_issues.md §6, which kept gemma over qwen3-coder-next on criteria
# coverage and quotation rate, was measured against that gone model and has to be redone before it
# means anything again. "coder" is the separate llama.cpp host the verifier switch exists for.
DEFAULT_BACKEND = ACTIVE
_REQUEST_TIMEOUT = httpx.Timeout(connect=10.0, read=240.0, write=60.0, pool=60.0)
# With reasoning on there is no ceiling on how long a call may take: a single structured-output call
# reasoned for 200-300 seconds in the 2026-10-06 experiment, past the 240 above, and a timeout there
# would throw the reasoning away and start it again. Only the connect step keeps a limit, so an
# unreachable host still fails fast.
_THINKING_TIMEOUT = httpx.Timeout(None, connect=10.0)
# How many times a request is retried. The infer:11239 host sits behind an nginx that refuses
# requests with 429 rather than queueing them, and the openai SDK retries 429 with backoff, so this
# is the second half of the guard whose first half is each backend's spacing and max_concurrency
# (modules/backends.py): those keep fan-out inside the host's ceiling, and these retries absorb the
# collisions that remain. It also covers a dropped connection on any host.
_MAX_RETRIES = 5

# Thinking is turned off. qwen3.8-27b is a reasoning model that fills a separate `reasoning`
# field before it writes any content, and on a specialist-sized prompt it spent 11,946 characters
# there and returned content of length 0, stopping on finish_reason=length rather than on an
# answer. Measured on the same prompt: 54.0s and 4,096 completion tokens with thinking against
# 18.8s and 1,115 with it off. Generation speed was never the problem — the host runs at ~79 tok/s
# — the tokens were going somewhere the pipeline cannot read, and a structured-output call that
# runs out of length that way fails to parse and takes its branch down with it.
_NO_THINKING = {"chat_template_kwargs": {"enable_thinking": False}}
# LLM_THINKING=on turns it back on for a run (modules/backends.py), optionally at a set effort.
_THINKING = {"chat_template_kwargs": {"enable_thinking": True}} | (
    {"reasoning_effort": REASONING_EFFORT} if REASONING_EFFORT else {}
)

Langfuse()


def get_llm(temperature: float = 0.0, backend: str = DEFAULT_BACKEND) -> ChatOpenAI:
    host = BACKENDS[backend]
    return ChatOpenAI(
        model=host.model,
        base_url=host.base_url,
        api_key=host.api_key,
        temperature=temperature,
        http_client=llm_http_client(_REQUEST_TIMEOUT, host.min_interval),
        # Passed again here because the client's own timeout is not what applies: the openai SDK
        # sends a timeout with every request, and ChatOpenAI's default of None means none at all.
        # PT09 sat on one unanswered call for over two hours, twice, with read=240 set above.
        timeout=_THINKING_TIMEOUT if THINKING else _REQUEST_TIMEOUT,
        max_retries=_MAX_RETRIES,
        extra_body=_THINKING if THINKING else _NO_THINKING,
    )


def get_langfuse_handler() -> CallbackHandler:
    return CallbackHandler()
