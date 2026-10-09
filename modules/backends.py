import os
from typing import NamedTuple

from dotenv import load_dotenv

# Input: environment (and the repo-root .env) — LLM_BACKEND picks the backend every module talks
# to, QWEN38_GPU200_* and LLM_API_KEY carry the endpoints' credentials. Output: BACKENDS, the
# self-hosted model endpoints by name, and ACTIVE / MAX_CONCURRENCY for the one in use.
# Algorithm: one table shared by modules 1-3, so a switch of host is one environment variable
# rather than three edits, and each host carries the two numbers that differ between them — how
# far apart its requests must be and how many may be in flight — because those are properties of
# the host, not of the module calling it.

load_dotenv()


class Backend(NamedTuple):
    base_url: str
    model: str
    api_key: str
    min_interval: float  # seconds between requests the host tolerates; 0 = no spacing needed
    concurrency: int     # requests that may be in flight at once (LangGraph max_concurrency)
    context: int         # the served max_model_len, in tokens


BACKENDS: dict[str, Backend] = {
    # qwen3.8-27b served directly by vLLM on GPU200. Measured 2026-10-07: 24 simultaneous requests
    # all answered 200 (5.6s for the batch, 1.3s for one alone), so it needs no spacing; 12 is the
    # host's stated concurrent capacity.
    "gpu200": Backend(
        base_url=os.environ.get("QWEN38_GPU200_URL", "http://gpu200.mi2rl.co:8002/v1"),
        model=os.environ.get("QWEN38_GPU200_MODEL", "Qwen/Qwen3.8-27B"),
        api_key=os.environ.get("QWEN38_GPU200_API_KEY", "infer"),
        min_interval=0.0,
        concurrency=12,
        context=98304,
    ),
    # The same model behind nginx on infer:11239. It answers 429 past a burst of 6-8 and then about
    # one request per 6 seconds (see throttle.py), so it is usable only spaced and two at a time.
    "infer": Backend(
        base_url="http://infer.mi2rl.co:11239/v1",
        model="qwen3.8-27b",
        api_key=os.environ.get("LLM_API_KEY", "infer"),
        min_interval=float(os.environ.get("LLM_MIN_INTERVAL", "6.0")),
        concurrency=2,
        context=262144,
    ),
    # The llama.cpp host module 3's verifier switch exists for; down since the endpoint moved, so
    # check it before selecting it.
    "coder": Backend(
        base_url="http://infer2.mi2rl.co:8080/v1",
        model="qwen3-coder-next-q8",
        api_key="infer",
        min_interval=0.0,
        concurrency=2,
        context=0,
    ),
}

ACTIVE = os.environ.get("LLM_BACKEND", "gpu200")
MAX_CONCURRENCY = BACKENDS[ACTIVE].concurrency

# Reasoning ("thinking") is off unless LLM_THINKING says otherwise. Off is the default because on
# infer:11239 it filled max_tokens with reasoning and returned empty answers, and because module 3
# makes hundreds of calls per patient. LLM_REASONING_EFFORT is passed through to the server, which
# for Qwen3.8 on GPU200 accepts low, medium and xhigh (its default when the field is absent) and
# rejects anything else with 400.
THINKING = os.environ.get("LLM_THINKING", "off").lower() in ("1", "on", "true", "yes")
REASONING_EFFORT = os.environ.get("LLM_REASONING_EFFORT") or None
