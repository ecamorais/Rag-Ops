"""Gemini client and the single answer-generation call (structured JSON output).

Error messages are our own text plus the HTTP code and status only. Exception text,
headers and the client object are never logged or shown, because they could hold the key.
"""

import random
import re
import threading
import time

import httpx
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, Field, ValidationError

from app.config import settings

MAX_ATTEMPTS = 4  # total HTTP attempts for one question, retries included
BASE_DELAY = 2.0  # seconds; doubles each retry (2, 4, 8) plus jitter; used for 429 and network errors
BACKOFF_503 = (5.0, 15.0, 45.0)  # 503 (overloaded) clears slowly, so wait longer; one value per retry
MAX_WAIT = 60.0  # never sleep longer than this, even if the server asks for more
QUOTA_USED_UP_AFTER = 120.0  # a 429 retry delay above this means the quota is used up: do not retry
REQUEST_TIMEOUT_MS = 60_000

# Every HTTP attempt sent to Gemini, and how many of them were retries after 429/503.
http_attempts = 0
retries = 0
# Token counts of the last successful response: {"prompt", "output", "total"}, or None.
last_usage = None

# Quota guard. After a "quota used up" 429 (retry delay above QUOTA_USED_UP_AFTER) the reset time is
# remembered per model name, and calls for that model are refused at once, with no HTTP attempt,
# until it passes. Kept in memory only, so a server restart forgets it. _clock is a module name so
# that tests can replace it with a fake clock.
_clock = time.monotonic
_quota_resets = {}  # model name -> _clock() value at which the quota is expected to be back
_quota_lock = threading.Lock()  # protects _quota_resets
_attempt_locks = {}  # model name -> lock held during one HTTP attempt (see generate_answer)


class GeminiAnswer(BaseModel):
    answer: str
    found: bool
    citations: list[str] = Field(default_factory=list)


class LlmError(Exception):
    """Base class. str(error) is always safe to show."""


class MissingApiKeyError(LlmError):
    pass


class InvalidApiKeyError(LlmError):
    pass


class BadRequestError(LlmError):
    pass


class RateLimitError(LlmError):
    """retry_after_seconds: how long the server (or the guard) says to wait, or None when unknown."""

    def __init__(self, message, retry_after_seconds=None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ServerError(LlmError):
    pass


class NetworkError(LlmError):
    pass


class BadResponseError(LlmError):
    pass


_client = None


def get_client():
    global _client
    if not settings.gemini_api_key:
        raise MissingApiKeyError("GEMINI_API_KEY is not set. Add it to .env (see .env.example).")
    if _client is None:
        _client = genai.Client(
            api_key=settings.gemini_api_key,
            # attempts=1 turns off the SDK's own tenacity retry. Retries are done
            # only by generate_answer, so one question is at most MAX_ATTEMPTS HTTP attempts.
            http_options=types.HttpOptions(
                timeout=REQUEST_TIMEOUT_MS,
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
    return _client


def _code_status(error):
    return f"HTTP {error.code} {error.status or ''}".strip()


def _retry_hint(error):
    """Seconds the server asked us to wait (RetryInfo in the body, or Retry-After), else None."""
    try:
        details = error.details
        if isinstance(details, dict):
            for item in details.get("error", details).get("details", []) or []:
                delay = item.get("retryDelay") if isinstance(item, dict) else None
                if delay:
                    return float(str(delay).rstrip("s"))
    except (AttributeError, TypeError, ValueError):
        pass
    try:
        value = error.response.headers.get("retry-after")
        if value:
            return float(value)
    except (AttributeError, TypeError, ValueError):
        pass
    return None


def _zero_quota(error):
    """A 429 whose quota limit is 0 will not recover by waiting a few seconds."""
    return bool(re.search(r"limit:\s*0\b", str(error.message or "")))


def _format_wait(seconds):
    """'about N minutes' under an hour, otherwise 'about N hours'; at least 1, singular for 1."""
    if seconds < 3600:
        n, unit = max(1, round(seconds / 60)), "minute"
    else:
        n, unit = max(1, round(seconds / 3600)), "hour"
    return f"about {n} {unit}{'' if n == 1 else 's'}"


def _quota_message(seconds, where):
    return f"Gemini's quota for this model is used up; it resets in {_format_wait(seconds)} ({where})."


def _quota_seconds_left(model):
    """Seconds until the remembered reset time for this model; 0 when there is none or it has passed."""
    with _quota_lock:
        reset = _quota_resets.get(model)
        if reset is None:
            return 0.0
        left = reset - _clock()
        if left <= 0:
            del _quota_resets[model]
            return 0.0
        return left


def _remember_quota_reset(model, seconds):
    with _quota_lock:
        _quota_resets[model] = _clock() + seconds


def _classify(error):
    """Map a non-retryable (or exhausted) API error to one of our readable errors."""
    code = error.code
    where = _code_status(error)
    if code in (401, 403) or (code == 400 and "api key" in str(error.message or "").lower()):
        return InvalidApiKeyError(f"Gemini rejected the API key ({where}). Check GEMINI_API_KEY in .env.")
    if code == 400:
        return BadRequestError(
            f"Gemini rejected the request ({where}); the model or response schema may not be supported."
        )
    if code == 429:
        if _zero_quota(error):
            return RateLimitError(f"Gemini quota for this model is zero on this key ({where}).")
        delay = _retry_hint(error)
        if delay is not None and delay > QUOTA_USED_UP_AFTER:
            return RateLimitError(_quota_message(delay, where), retry_after_seconds=delay)
        return RateLimitError(f"Gemini is rate limited; try again in a minute ({where}).", retry_after_seconds=delay)
    if code >= 500:
        return ServerError(f"Gemini is having server problems; try again shortly ({where}).")
    return LlmError(f"Gemini returned an error ({where}).")


def _parse(response):
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, GeminiAnswer):
        return parsed
    text = response.text if response.candidates else None
    if not text or not text.strip():
        raise BadResponseError("Gemini returned an empty or blocked response.")
    try:
        return GeminiAnswer.model_validate_json(text)
    except ValidationError:
        raise BadResponseError("Gemini returned a response that did not match the expected format.") from None


def _record_usage(response):
    """Keep only the three token counts from the response; nothing else is read or stored."""
    global last_usage
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        last_usage = None
        return
    last_usage = {
        "prompt": usage.prompt_token_count,
        "output": usage.candidates_token_count,
        "total": usage.total_token_count,
    }


def generate_answer(system, prompt):
    """One question = one logical call, at most MAX_ATTEMPTS HTTP attempts. Returns GeminiAnswer.

    HTTP attempts for one model are made one at a time (the attempt lock), and the quota guard is checked
    inside that lock. So when several requests arrive together and the first one learns that the quota is
    used up, the others find the remembered reset time and are refused without any HTTP attempt.
    """
    global http_attempts, retries, last_usage
    last_usage = None
    client = get_client()
    model = settings.gemini_model
    attempt_lock = _attempt_locks.setdefault(model, threading.Lock())  # setdefault is atomic
    config = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_schema=GeminiAnswer,
        temperature=0.2,
    )
    for attempt in range(1, MAX_ATTEMPTS + 1):
        with attempt_lock:
            left = _quota_seconds_left(model)
            if left > 0:
                raise RateLimitError(_quota_message(left, "HTTP 429 RESOURCE_EXHAUSTED"), retry_after_seconds=left)
            http_attempts += 1
            try:
                response = client.models.generate_content(model=model, contents=prompt, config=config)
                _record_usage(response)
                return _parse(response)
            except errors.APIError as error:
                hint = _retry_hint(error)
                used_up = error.code == 429 and hint is not None and hint > QUOTA_USED_UP_AFTER
                if used_up:
                    _remember_quota_reset(model, hint)
                retryable = error.code in (429, 503) and not used_up and not (error.code == 429 and _zero_quota(error))
                if not retryable or attempt == MAX_ATTEMPTS:
                    raise _classify(error) from None
                wait = hint
                if wait is None:
                    wait = BACKOFF_503[attempt - 1] if error.code == 503 else BASE_DELAY * 2 ** (attempt - 1)
                wait = min(wait, MAX_WAIT) + random.uniform(0, 1)
            except httpx.TransportError as error:
                if attempt == MAX_ATTEMPTS:
                    raise NetworkError(
                        f"Could not reach Gemini ({type(error).__name__}). Check your internet connection."
                    ) from None
                wait = BASE_DELAY * 2 ** (attempt - 1) + random.uniform(0, 1)
        retries += 1  # the lock is released first, so a backoff sleep never blocks other requests
        time.sleep(wait)
    raise LlmError("Gemini call failed.")  # unreachable; keeps the function total
