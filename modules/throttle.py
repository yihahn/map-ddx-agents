import threading
import time

import httpx

# Input: a request timeout and the minimum spacing a host needs between requests. Output: an
# httpx.Client that spaces every request it sends by at least that many seconds, shared by every
# thread in the process (llm_http_client) — one client per spacing, so hosts with different limits
# do not queue behind each other. Algorithm: one lock-protected "earliest next call time" per
# client that each request claims before it is sent, which is the same shape as the NCBI limiter in
# module2_deterministic/pubmed.py and for the same reason — Send fans branches out as threads, so a
# per-call sleep would let them collectively blow past the cap. The infer:11239 host answers 429
# with no Retry-After and no rate headers, measured at a burst of 5 and then roughly one request
# per 6 seconds sustained, so the only way to stay inside it is to not ask too often: retries
# recover a collision, they do not prevent one, and a branch that exhausts them raises and takes
# the whole graph down — 30 of PT09's 37 diagnoses went unverified that way. The spacing itself is
# each backend's min_interval in modules/backends.py; a host with no limit gets a client with no
# hook at all.

_lock = threading.Lock()
_clients: dict[float, httpx.Client] = {}


def _spacer(min_interval: float):
    """A request hook that holds each request until min_interval has passed since the last one."""
    state = {"next_at": 0.0}
    lock = threading.Lock()

    def claim_slot(_request: httpx.Request) -> None:
        with lock:
            now = time.monotonic()
            wait = max(0.0, state["next_at"] - now)
            state["next_at"] = max(now, state["next_at"]) + min_interval
        if wait:
            time.sleep(wait)

    return claim_slot


def llm_http_client(timeout: httpx.Timeout, min_interval: float = 0.0) -> httpx.Client:
    """An httpx client that throttles itself, for ChatOpenAI to send its requests through.

    trust_env is off: the inference hosts sit on this machine's own subnet, while the environment
    points at an HTTP proxy that cannot resolve the hospital's internal names and answers
    ERR_DNS_FAIL for them. Ignoring the proxy here means a run no longer depends on NO_PROXY being
    exported by whoever starts it.

    One client per spacing for the process, because get_llm() is called per node rather than once
    per run: a fresh client each time would open a fresh connection pool, and the point of the
    throttle is that everything bound for one host shares one queue. The first caller's timeout is
    the one that applies; all three modules pass the same values.
    """
    with _lock:
        if min_interval not in _clients:
            hooks = {"request": [_spacer(min_interval)]} if min_interval > 0 else {}
            _clients[min_interval] = httpx.Client(timeout=timeout, trust_env=False, event_hooks=hooks)
    return _clients[min_interval]
