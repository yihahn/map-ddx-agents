import os

import httpx
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langfuse import Langfuse
from langfuse.langchain import CallbackHandler

# Input: none (reads LLM/Langfuse credentials from the repo-root .env). Output: a shared
# ChatOpenAI client bound to the self-hosted Gemma endpoint, and a Langfuse CallbackHandler
# for LangGraph tracing. Algorithm: load .env once at import time, construct a single
# Langfuse() client (which auto-reads LANGFUSE_PUBLIC_KEY/SECRET_KEY/BASE_URL) so the
# CallbackHandler picks it up as its default client, and expose get_llm()/get_langfuse_handler()
# so every node/run script shares the same configured instances. get_llm() pins an explicit
# request timeout and retry count: the openai SDK defaults to a 600-second read timeout with 2
# retries, so a single dropped response stalls one branch for up to 30 minutes with no error.
# backend selects which self-hosted model to talk to, so one node can use a different one.


load_dotenv()

_LLM_API_KEY = "infer"
# Two self-hosted backends. "gemma" reads clinical prose; "qwen" is the coder-tuned model on the
# llama.cpp host, kept available for the verifier because that node is a tool-calling ReAct loop
# rather than a reading task, and it carries a 131k context against gemma's 80k.
BACKENDS = {
    "gemma": ("http://infer.mi2rl.co:8000/v1", "google/gemma-4-26B-A4B-it"),
    "qwen": ("http://infer2.mi2rl.co:8080/v1", "qwen3-coder-next-q8"),
}
DEFAULT_BACKEND = "gemma"
_REQUEST_TIMEOUT = httpx.Timeout(connect=10.0, read=240.0, write=60.0, pool=60.0)
_MAX_RETRIES = 2

Langfuse()


def get_llm(temperature: float = 0.0, backend: str = DEFAULT_BACKEND) -> ChatOpenAI:
    base_url, model = BACKENDS[backend]
    return ChatOpenAI(
        model=model,
        base_url=base_url,
        api_key=os.environ.get("LLM_API_KEY", _LLM_API_KEY),
        temperature=temperature,
        timeout=_REQUEST_TIMEOUT,
        max_retries=_MAX_RETRIES,
    )


def get_langfuse_handler() -> CallbackHandler:
    return CallbackHandler()
