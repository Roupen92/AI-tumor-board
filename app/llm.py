"""Thin LLM wrapper used by every model call in the tumor board.

Uses Google's OpenAI-compatible Gemini endpoint so the rest of the codebase keeps
using the familiar OpenAI SDK shape (tool calls, response_format, etc.) — only the
base URL and API key change. Set MEDBOARD_PROVIDER=openai to fall back to OpenAI.
"""
import os
import re
import time
import logging
from typing import Any

from openai import OpenAI, APIConnectionError, RateLimitError, APIStatusError

from app.config import MODEL_NAME

# Sentinel exception so callers (specialist.py / board.py) can render a clean
# user-facing message instead of dumping the raw OpenAI JSON.
class QuotaExceeded(Exception):
    """Raised when the LLM provider returns a rate-limit error we couldn't retry past."""

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


_RETRY_DELAY_RE = re.compile(r"['\"]retryDelay['\"]\s*:\s*['\"](\d+(?:\.\d+)?)s['\"]")


def _parse_retry_delay(err: Exception) -> float | None:
    """Try to extract Google's suggested retryDelay (seconds) from an error message."""
    m = _RETRY_DELAY_RE.search(str(err))
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def chat(
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    response_format: dict | None = None,
    model: str | None = None,
    max_retries: int = 5,
) -> Any:
    """Call the configured LLM with retry on transient errors.

    On rate-limit errors, prefer the provider's suggested retryDelay over
    blind exponential backoff (Google's Gemini API includes this in 429s).
    """
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

    # Gemini-specific tweak: when Gemini's "thinking" mode is on AND we use tools,
    # the model attaches a `thought_signature` to each function call that the
    # OpenAI-compat layer strips when we replay history. Subsequent calls then
    # fail with HTTP 400 'Function call is missing a thought_signature'. Disable
    # thinking entirely so signatures aren't required (also faster for tool loops).
    provider = os.getenv("MEDBOARD_PROVIDER", "gemini").lower()
    if provider != "openai":
        kwargs["extra_body"] = {
            **(kwargs.get("extra_body") or {}),
            "google": {"thinking_config": {"thinking_budget": 0}},
        }

    attempt = 0
    while True:
        try:
            return client.chat.completions.create(**kwargs)
        except RateLimitError as e:
            attempt += 1
            if attempt >= max_retries:
                # Raise a clean sentinel so callers can render a user-facing message
                # instead of the raw JSON blob.
                raise QuotaExceeded(
                    "LLM quota exhausted. If you're on Google Gemini, check that "
                    "billing is enabled on your project at "
                    "https://console.cloud.google.com/billing — free-tier limits "
                    "(5 requests/minute) are too tight for this app."
                ) from e
            suggested = _parse_retry_delay(e)
            backoff = max(suggested or 0, min(2**attempt, 30))
            backoff = min(backoff, 60)   # cap at 60s
            log.warning(
                "LLM rate-limited (attempt %d/%d); waiting %.1fs%s",
                attempt, max_retries, backoff,
                f" (server suggested {suggested}s)" if suggested else "",
            )
            time.sleep(backoff)
        except APIConnectionError as e:
            attempt += 1
            if attempt >= max_retries:
                raise
            backoff = min(2**attempt, 16)
            log.warning("LLM connection error; retrying in %ds", backoff)
            time.sleep(backoff)
        except APIStatusError as e:
            if e.status_code in (500, 502, 503, 504) and attempt < max_retries:
                attempt += 1
                backoff = min(2**attempt, 16)
                log.warning("LLM %d error; retrying in %ds", e.status_code, backoff)
                time.sleep(backoff)
                continue
            raise
