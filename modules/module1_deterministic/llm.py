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


load_dotenv()

_LLM_BASE_URL = "http://infer.mi2rl.co:8000/v1"
_LLM_MODEL = "google/gemma-4-26B-A4B-it"
_LLM_API_KEY = "infer"
_REQUEST_TIMEOUT = httpx.Timeout(connect=10.0, read=240.0, write=60.0, pool=60.0)
_MAX_RETRIES = 2

Langfuse()


def get_llm(temperature: float = 0.0) -> ChatOpenAI:
    return ChatOpenAI(
        model=_LLM_MODEL,
        base_url=_LLM_BASE_URL,
        api_key=os.environ.get("LLM_API_KEY", _LLM_API_KEY),
        temperature=temperature,
        timeout=_REQUEST_TIMEOUT,
        max_retries=_MAX_RETRIES,
    )


def get_langfuse_handler() -> CallbackHandler:
    return CallbackHandler()
