"""Thin LLM wrapper used by every model call in the tumor board.

Uses Google's OpenAI-compatible Gemini endpoint so the rest of the codebase keeps
using the familiar OpenAI SDK shape (tool calls, response_format, etc.) — only the
base URL and API key change. Set MEDBOARD_PROVIDER=openai to fall back to OpenAI.
"""
import os
import time
import logging
from typing import Any

from openai import OpenAI, APIConnectionError, RateLimitError, APIStatusError

from app.config import MODEL_NAME

log = logging.getLogger(__name__)

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

_client: OpenAI | None = None


def get_client() -> OpenAI:
    """Return a singleton client. Defaults to Gemini; set MEDBOARD_PROVIDER=openai to use OpenAI."""
    global _client
    if _client is not None:
        return _client

    provider = os.getenv("MEDBOARD_PROVIDER", "gemini").lower()
    if provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Paste your key into .env and restart "
                "(or unset MEDBOARD_PROVIDER to use Gemini)."
            )
        _client = OpenAI(api_key=api_key)
        log.info("LLM client: OpenAI, model=%s", MODEL_NAME)
    else:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Paste your Google AI Studio key into .env "
                "and restart (or set MEDBOARD_PROVIDER=openai to use OpenAI)."
            )
        _client = OpenAI(api_key=api_key, base_url=GEMINI_BASE_URL)
        log.info("LLM client: Gemini via OpenAI-compat endpoint, model=%s", MODEL_NAME)

    return _client


def chat(
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    response_format: dict | None = None,
    model: str | None = None,
    max_retries: int = 3,
) -> Any:
    """Call GPT-5.1 with retry on transient errors. Returns the raw response object."""
    client = get_client()
    kwargs: dict[str, Any] = {
        "model": model or MODEL_NAME,
        "messages": messages,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    if response_format:
        kwargs["response_format"] = response_format

    attempt = 0
    while True:
        try:
            return client.chat.completions.create(**kwargs)
        except (RateLimitError, APIConnectionError) as e:
            attempt += 1
            if attempt >= max_retries:
                raise
            backoff = min(2**attempt, 16)
            log.warning("LLM transient error (%s); retrying in %ds", type(e).__name__, backoff)
            time.sleep(backoff)
        except APIStatusError as e:
            if e.status_code in (500, 502, 503, 504) and attempt < max_retries:
                attempt += 1
                backoff = min(2**attempt, 16)
                log.warning("LLM %d error; retrying in %ds", e.status_code, backoff)
                time.sleep(backoff)
                continue
            raise
