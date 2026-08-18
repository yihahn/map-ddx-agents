import os

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langfuse import Langfuse
from langfuse.langchain import CallbackHandler

# Input: none (reads LLM/Langfuse credentials from the repo-root .env). Output: a shared
# ChatOpenAI client bound to the self-hosted Gemma endpoint, and a Langfuse CallbackHandler
# for LangGraph tracing. Algorithm: load .env once at import time, construct a single
# Langfuse() client (which auto-reads LANGFUSE_PUBLIC_KEY/SECRET_KEY/BASE_URL) so the
# CallbackHandler picks it up as its default client, and expose get_llm()/get_langfuse_handler()
# so every node/run script shares the same configured instances.

load_dotenv()

_LLM_BASE_URL = "http://infer.mi2rl.co:8000/v1"
_LLM_MODEL = "google/gemma-4-26B-A4B-it"
_LLM_API_KEY = "infer"

Langfuse()


def get_llm(temperature: float = 0.0) -> ChatOpenAI:
    return ChatOpenAI(
        model=_LLM_MODEL,
        base_url=_LLM_BASE_URL,
        api_key=os.environ.get("LLM_API_KEY", _LLM_API_KEY),
        temperature=temperature,
    )


def get_langfuse_handler() -> CallbackHandler:
    return CallbackHandler()
