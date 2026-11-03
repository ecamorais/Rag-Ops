"""Errors: how a failed Gemini call becomes an HTTP response, how app.llm retries and classifies, and that nothing leaks.

Written rules the expected values come from (not from running the code):
  README.md:77             no key: a question that needs the model returns "GEMINI_API_KEY is not set" (HTTP 500, code
                           `server_misconfigured`); a question that retrieves nothing is answered without the model
  README.md:153, CLAUDE.md:25   "The app retries 429 and 503 with backoff and shows a clear message if it still fails"
  llm.py:3-4               "Error messages are our own text plus the HTTP code and status only. Exception text, headers and the
                           client object are never logged or shown, because they could hold the key."
  llm.py:19-24             MAX_ATTEMPTS = 4 "total HTTP attempts for one question, retries included"; BASE_DELAY = 2.0 "doubles each
                           retry (2, 4, 8) plus jitter; used for 429 and network errors"; BACKOFF_503 = (5, 15, 45) "one value per
                           retry"; MAX_WAIT = 60.0 "never sleep longer than this, even if the server asks for more";
                           QUOTA_USED_UP_AFTER = 120.0 "a 429 retry delay above this means the quota is used up: do not retry";
                           REQUEST_TIMEOUT_MS = 60_000 (CLAUDE.md:118 "the 60 s timeout")
  llm.py _retry_hint       "Seconds the server asked us to wait (RetryInfo in the body, or Retry-After)"
  llm.py:49, main.py:163   "str(error) is always safe to show"; the handler shows it as is
  llm.py:26-30             http_attempts, retries and last_usage ({"prompt", "output", "total"}) are counters of the real calls
  main.py:51               LLM_ERROR_STATUS: "First match wins, so subclasses are listed before LlmError itself."
  README.md:148            the server never logs bodies, headers or the key; main._redact: "Server-side log text must never
                           contain the API key."
What is NOT written (the code strings other than server_misconfigured, the status of each error class, which statuses are retried,
the jitter, a hint replacing the backoff, ...) is pinned in tests named "pinned_".
The "real logic" tests call the REAL llm.generate_answer (captured at import, before the stub fixture patches it) with a FakeClient and
always assert that the FakeClient was called, so a stub can never make them pass. No real Gemini client is ever created: the one test
that builds a client does it with a capturing fake in place of genai.Client. Sleeping and jitter are replaced by a recorder.
Covered elsewhere and not repeated: the security headers on 404/422/429/503/500 and the unexpected-500 body (test_security_headers.py),
404/405/422 shapes and codes (test_api_validation.py). The memory of the quota guard, its threshold and the Retry-After arithmetic
belong to test_quota_guard.py.
"""
import dataclasses
import inspect
import json
import logging
import time
import types
from pathlib import Path

import httpx
import pytest
from google.genai import errors as gerrors
from google.genai import types as gtypes

from app import llm, main

ROOT = Path(__file__).resolve().parent.parent
if not (ROOT / "chroma_db").is_dir():
    pytest.skip("chroma_db/ is missing: run python scripts/ingest.py first", allow_module_level=True)

# Captured at import: the stub fixture patches llm.generate_answer only while a test runs.
REAL_GENERATE = llm.generate_answer
REAL_GET_CLIENT = llm.get_client
ORIGINAL_STATE = (llm._quota_resets, llm._attempt_locks)

KEY = "FAKEKEY123"
HOSTILE = "Bad key FAKEKEY123 at C:\\Users\\secret\\app.py line 12, /etc/passwd\nTraceback (most recent call last)"
FORBIDDEN = ["FAKEKEY123", "secret", "app.py", "/etc/passwd", "Traceback", "most recent call", "line 12"]
QUESTION = "What happened in OPS-1001?"  # an exact id: the context is never empty, so the model is needed
NOTHING = "zzzxqv qwrtp blorf"  # retrieves nothing
OK_TEXT = json.dumps({"answer": "Because of OPS-1001.", "found": True, "citations": ["OPS-1001"]})


# --- helpers ----------------------------------------------------------------------------------------------------------------

def api_error(code, message="upstream message", status=None, details=None, response=None):
    body = {"error": {"code": code, "message": message, "status": status}}
    if details is not None:
        body["error"]["details"] = details
    return (gerrors.ServerError if code >= 500 else gerrors.ClientError)(code, body, response)


def reply(text, usage=None):
    return gtypes.GenerateContentResponse(
        candidates=[gtypes.Candidate(content=gtypes.Content(parts=[gtypes.Part(text=text)], role="model"))], usage_metadata=usage)


def retry_in(seconds):
    return [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": f"{seconds}s"}]


class FakeClient:
    """Stands in for genai.Client: client.models.generate_content(...) pops the next scripted result (an exception is raised);
    when the script is used up, `repeat` is used again and again."""

    def __init__(self, script, repeat=None):
        self.script, self.repeat, self.calls = list(script), repeat, []
        self.models = self

    def generate_content(self, model, contents, config):
        self.calls.append((model, contents, config))
        item = self.script.pop(0) if self.script else self.repeat
        assert item is not None, "the script ran out"
        if isinstance(item, BaseException):
            raise item
        return item


class Clockwork:
    """Replaces time.sleep and random.uniform inside app.llm: records the waits and the arguments of uniform."""

    def __init__(self):
        self.sleeps, self.uniform_args, self.jitter = [], [], 0.0

    def sleep(self, seconds):
        self.sleeps.append(seconds)

    def uniform(self, low, high):
        self.uniform_args.append((low, high))
        return self.jitter


@pytest.fixture(scope="module", autouse=True)
def the_guard_state_outside_this_file_is_untouched():
    """After the last test of this file (all patches undone) the module state of app.llm is what it was before: no remembered
    quota reset, no lock, no counter, no client, so a >120 s 429 here cannot refuse questions in any other test file."""
    before = (dict(llm._quota_resets), dict(llm._attempt_locks), llm.http_attempts, llm.retries, llm.last_usage, llm._client,
              llm.settings, main.settings, llm.get_client, llm.generate_answer)
    yield
    assert llm._quota_resets is ORIGINAL_STATE[0] and llm._attempt_locks is ORIGINAL_STATE[1]
    assert llm._quota_resets == {} and llm._attempt_locks == {}, "a test of this file left state in app.llm"
    after = (dict(llm._quota_resets), dict(llm._attempt_locks), llm.http_attempts, llm.retries, llm.last_usage, llm._client,
             llm.settings, main.settings, llm.get_client, llm.generate_answer)
    assert after == before


@pytest.fixture(autouse=True)
def clean_llm_state(monkeypatch):
    """A fresh quota memory, fresh locks and zero counters for every test; the originals come back when the patches are undone."""
    monkeypatch.setattr(llm, "_quota_resets", {})
    monkeypatch.setattr(llm, "_attempt_locks", {})
    monkeypatch.setattr(llm, "http_attempts", 0)
    monkeypatch.setattr(llm, "retries", 0)
    monkeypatch.setattr(llm, "last_usage", None)
    monkeypatch.setattr(llm, "_client", None)
    yield


@pytest.fixture
def waits(monkeypatch):
    clock = Clockwork()
    monkeypatch.setattr(llm, "time", types.SimpleNamespace(sleep=clock.sleep, monotonic=time.monotonic))
    monkeypatch.setattr(llm, "random", types.SimpleNamespace(uniform=clock.uniform))
    return clock


@pytest.fixture
def real(monkeypatch):
    """The real generate_answer, called with a FakeClient (installed through use_client)."""
    assert REAL_GENERATE.__module__ == "app.llm" and inspect.isfunction(REAL_GENERATE)  # not the conftest StubModel
    monkeypatch.setattr(llm, "generate_answer", REAL_GENERATE)


@pytest.fixture
def use_client(monkeypatch, real, waits):
    def install(*script, repeat=None):
        client = FakeClient(script, repeat)
        monkeypatch.setattr(llm, "get_client", lambda: client)
        return client
    return install


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setattr(llm, "settings", dataclasses.replace(llm.settings, gemini_api_key=KEY))
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, gemini_api_key=KEY))


def ask():
    return llm.generate_answer("system text", "prompt text")


def chat(client, question=QUESTION):
    return client.post("/chat", json={"question": question})


def everything(response):
    """The status line, every header and the body of a response, as one text."""
    return f"{response.status_code}\n{dict(response.headers)}\n{response.text}"


# --- preconditions --------------------------------------------------------------------------------------------------------------

def test_the_real_functions_are_what_the_tests_below_call():
    assert REAL_GENERATE.__module__ == "app.llm" and REAL_GENERATE.__name__ == "generate_answer"
    assert inspect.isfunction(REAL_GENERATE) and type(REAL_GENERATE).__name__ != "StubModel"
    assert REAL_GET_CLIENT.__module__ == "app.llm" and REAL_GET_CLIENT.__name__ == "get_client"


def test_the_stub_is_installed_in_a_test_that_does_not_ask_for_the_real_function(stub_model):
    assert llm.generate_answer is stub_model and llm.generate_answer is not REAL_GENERATE


# --- main.py: an LlmError becomes a response (the model is the stub, only the mapping is tested) ----------------------------------------

MAPPING = [
    (llm.RateLimitError, 429, "rate_limited"),
    (llm.ServerError, 503, "upstream_unavailable"),
    (llm.NetworkError, 503, "upstream_unavailable"),
    (llm.BadResponseError, 502, "bad_upstream_response"),
    (llm.BadRequestError, 502, "bad_upstream_response"),
    (llm.MissingApiKeyError, 500, "server_misconfigured"),
    (llm.InvalidApiKeyError, 500, "server_misconfigured"),
    (llm.LlmError, 502, "bad_upstream_response"),
]


@pytest.mark.parametrize("error_class, status, code", MAPPING, ids=[m[0].__name__ for m in MAPPING])
def test_pinned_each_llm_error_class_has_its_status_and_code(api_client, stub_model, error_class, status, code):
    """README:77 writes 500 / server_misconfigured for the missing key and 429 / 503 / 500 as statuses; the rest is pinned.
    main.py:51: subclasses are listed before LlmError, so RateLimitError is a 429 and not the base class's 502."""
    stub_model.fail(error_class("A readable message."))
    response = chat(api_client)
    assert len(stub_model.calls) == 1
    assert response.status_code == status and response.json() == {"error": "A readable message.", "code": code}


def test_the_missing_key_error_is_the_written_500(api_client, stub_model):
    stub_model.fail(llm.MissingApiKeyError("GEMINI_API_KEY is not set. Add it to .env (see .env.example)."))
    response = chat(api_client)
    assert response.status_code == 500
    assert response.json()["code"] == "server_misconfigured" and response.json()["error"].startswith("GEMINI_API_KEY is not set")


def test_a_429_without_a_wait_time_has_no_retry_after(api_client, stub_model):
    stub_model.fail(llm.RateLimitError("Try again in a minute."))
    assert "retry-after" not in chat(api_client).headers


# --- no key (the real get_client; README:77) --------------------------------------------------------------------------------------

def test_without_a_key_a_question_that_needs_the_model_is_the_written_500(api_client, monkeypatch, real):
    monkeypatch.setattr(llm, "get_client", REAL_GET_CLIENT)
    assert llm.settings.gemini_api_key == ""
    response = chat(api_client)
    assert response.status_code == 500
    assert response.json()["code"] == "server_misconfigured" and "GEMINI_API_KEY is not set" in response.json()["error"]
    assert llm.http_attempts == 0 and llm._client is None


def test_without_a_key_a_question_that_retrieves_nothing_is_answered(api_client, monkeypatch, real):
    monkeypatch.setattr(llm, "get_client", REAL_GET_CLIENT)
    response = chat(api_client, NOTHING)
    assert response.status_code == 200 and response.json()["found"] is False and response.json()["sources"] == []
    assert llm.http_attempts == 0


def test_get_client_without_a_key_raises_and_creates_nothing(monkeypatch):
    def refuse(**kwargs):
        raise AssertionError("a client was created without a key")
    monkeypatch.setattr(llm.genai, "Client", refuse)
    with pytest.raises(llm.MissingApiKeyError, match="GEMINI_API_KEY is not set"):
        REAL_GET_CLIENT()


def test_get_client_turns_off_the_sdk_retry_sets_the_60_second_timeout_and_keeps_one_client(monkeypatch, with_key):
    """llm.py:94-99 and 24, CLAUDE.md:118. The one test that builds a client: with a capturing fake in place of genai.Client."""
    made = []

    class CapturingClient:
        def __init__(self, **kwargs):
            made.append(kwargs)

    monkeypatch.setattr(llm.genai, "Client", CapturingClient)
    monkeypatch.setattr(llm, "_client", None)  # restored at teardown, so the session guard's "llm._client is None" still holds
    first = REAL_GET_CLIENT()
    assert isinstance(first, CapturingClient) and REAL_GET_CLIENT() is first
    assert len(made) == 1
    assert made[0]["api_key"] == KEY
    options = made[0]["http_options"]
    assert options.timeout == 60_000 and options.retry_options.attempts == 1


# --- app.llm: the real generate_answer with a fake client ----------------------------------------------------------------------------

def test_a_good_answer_is_returned_after_one_http_attempt(use_client, waits):
    usage = gtypes.GenerateContentResponseUsageMetadata(prompt_token_count=11, candidates_token_count=22, total_token_count=33)
    client = use_client(reply(OK_TEXT, usage))
    answer = ask()
    assert answer == llm.GeminiAnswer(answer="Because of OPS-1001.", found=True, citations=["OPS-1001"])
    assert len(client.calls) == 1 and waits.sleeps == []
    assert (llm.http_attempts, llm.retries) == (1, 0)
    assert llm.last_usage == {"prompt": 11, "output": 22, "total": 33}


def test_the_call_carries_the_model_the_prompt_and_the_json_schema(use_client):
    client = use_client(reply(OK_TEXT))
    ask()
    (model, contents, config), = client.calls
    assert model == llm.settings.gemini_model and contents == "prompt text"
    assert config.system_instruction == "system text"
    assert config.response_mime_type == "application/json" and config.response_schema is llm.GeminiAnswer


def test_citations_default_to_none_when_the_model_leaves_them_out(use_client):
    use_client(reply(json.dumps({"answer": "a", "found": False})))
    assert ask() == llm.GeminiAnswer(answer="a", found=False, citations=[])


def test_the_token_counts_of_a_failed_call_are_not_kept(use_client):
    llm.last_usage = {"prompt": 1, "output": 1, "total": 2}
    client = use_client(api_error(404, status="NOT_FOUND"))
    with pytest.raises(llm.LlmError):
        ask()
    assert len(client.calls) == 1 and llm.last_usage is None


# 429 -----------------------------------------------------------------------------------------------------------------------------

def test_a_429_without_a_hint_is_retried_with_the_doubling_backoff_then_reported(use_client, waits):
    """llm.py:19-20: 4 attempts in all, waits 2, 4, 8."""
    client = use_client(repeat=api_error(429, status="RESOURCE_EXHAUSTED"))
    with pytest.raises(llm.RateLimitError) as caught:
        ask()
    assert len(client.calls) == 4 and waits.sleeps == [2.0, 4.0, 8.0]
    assert (llm.http_attempts, llm.retries) == (4, 3)
    assert caught.value.retry_after_seconds is None
    assert "HTTP 429 RESOURCE_EXHAUSTED" in str(caught.value)


def test_a_429_then_a_good_answer_returns_it(use_client, waits):
    client = use_client(api_error(429, status="RESOURCE_EXHAUSTED"), reply(OK_TEXT))
    assert ask().found is True
    assert len(client.calls) == 2 and waits.sleeps == [2.0] and (llm.http_attempts, llm.retries) == (2, 1)


def test_pinned_a_wait_time_from_the_server_replaces_the_backoff_and_is_reported_in_the_end(use_client, waits):
    """llm.py _retry_hint: the seconds the server asked us to wait. That it replaces the backoff is unwritten."""
    client = use_client(repeat=api_error(429, status="RESOURCE_EXHAUSTED", details=retry_in(30)))
    with pytest.raises(llm.RateLimitError) as caught:
        ask()
    assert len(client.calls) == 4 and waits.sleeps == [30.0, 30.0, 30.0]
    assert caught.value.retry_after_seconds == 30.0


def test_pinned_a_retry_after_header_is_read_like_the_body_hint(use_client, waits):
    response = httpx.Response(429, headers={"retry-after": "25"})
    client = use_client(repeat=api_error(429, status="RESOURCE_EXHAUSTED", response=response))
    with pytest.raises(llm.RateLimitError) as caught:
        ask()
    assert len(client.calls) == 4 and waits.sleeps == [25.0, 25.0, 25.0] and caught.value.retry_after_seconds == 25.0


def test_a_429_whose_wait_is_above_120_seconds_is_not_retried(use_client, waits):
    """llm.py:23: a retry delay above 120 means the quota is used up: do not retry."""
    client = use_client(repeat=api_error(429, status="RESOURCE_EXHAUSTED", details=retry_in(300)))
    with pytest.raises(llm.RateLimitError) as caught:
        ask()
    assert len(client.calls) == 1 and waits.sleeps == [] and (llm.http_attempts, llm.retries) == (1, 0)
    assert caught.value.retry_after_seconds == 300.0
    assert "used up" in str(caught.value)


def test_pinned_a_429_with_a_zero_quota_limit_is_not_retried(use_client, waits):
    client = use_client(repeat=api_error(429, "Quota exceeded for metric, limit: 0, model: x", "RESOURCE_EXHAUSTED"))
    with pytest.raises(llm.RateLimitError, match="zero"):
        ask()
    assert len(client.calls) == 1 and waits.sleeps == []


# 503 and the other server errors -------------------------------------------------------------------------------------------------------

def test_a_503_is_retried_with_the_503_backoff_then_reported(use_client, waits):
    """llm.py:21: BACKOFF_503 = (5, 15, 45), one value per retry."""
    client = use_client(repeat=api_error(503, status="UNAVAILABLE"))
    with pytest.raises(llm.ServerError, match="HTTP 503 UNAVAILABLE"):
        ask()
    assert len(client.calls) == 4 and waits.sleeps == [5.0, 15.0, 45.0] and (llm.http_attempts, llm.retries) == (4, 3)


def test_a_503_then_a_good_answer_returns_it(use_client, waits):
    client = use_client(api_error(503, status="UNAVAILABLE"), api_error(503, status="UNAVAILABLE"), reply(OK_TEXT))
    assert ask().found is True
    assert len(client.calls) == 3 and waits.sleeps == [5.0, 15.0]


@pytest.mark.parametrize("code, status", [(500, "INTERNAL"), (502, "BAD_GATEWAY"), (504, "DEADLINE_EXCEEDED")])
def test_pinned_other_5xx_errors_are_not_retried(use_client, waits, code, status):
    client = use_client(repeat=api_error(code, status=status))
    with pytest.raises(llm.ServerError, match=f"HTTP {code} {status}"):
        ask()
    assert len(client.calls) == 1 and waits.sleeps == []


# the other API errors --------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("code, status, message, error_class", [
    (400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key.", llm.InvalidApiKeyError),
    (401, "UNAUTHENTICATED", "bad credentials", llm.InvalidApiKeyError),
    (403, "PERMISSION_DENIED", "denied", llm.InvalidApiKeyError),
    (400, "INVALID_ARGUMENT", "the response schema is not supported", llm.BadRequestError),
], ids=["400 api key", "401", "403", "400 other"])
def test_pinned_client_errors_are_classified_and_never_retried(use_client, waits, code, status, message, error_class):
    client = use_client(repeat=api_error(code, message, status))
    with pytest.raises(error_class) as caught:
        ask()
    assert type(caught.value) is error_class
    assert len(client.calls) == 1 and waits.sleeps == []
    assert f"HTTP {code} {status}" in str(caught.value) and message not in str(caught.value)


@pytest.mark.parametrize("code, status", [(404, "NOT_FOUND"), (409, "ABORTED")])
def test_pinned_any_other_client_error_is_a_plain_llm_error(use_client, waits, code, status):
    client = use_client(repeat=api_error(code, "upstream text", status))
    with pytest.raises(llm.LlmError) as caught:
        ask()
    assert type(caught.value) is llm.LlmError and str(caught.value) == f"Gemini returned an error (HTTP {code} {status})."
    assert len(client.calls) == 1 and waits.sleeps == []


def test_pinned_an_exception_that_is_not_a_gemini_or_network_error_is_not_wrapped(use_client, waits):
    client = use_client(RuntimeError("something else"))
    with pytest.raises(RuntimeError, match="something else"):
        ask()
    assert len(client.calls) == 1 and waits.sleeps == []


# network errors and timeouts -----------------------------------------------------------------------------------------------------------------

TRANSPORT = [httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError, httpx.WriteError]


@pytest.mark.parametrize("error_class", TRANSPORT, ids=[c.__name__ for c in TRANSPORT])
def test_pinned_a_network_error_is_retried_like_a_429_then_reported_by_class_name(use_client, waits, error_class):
    """llm.py:20: BASE_DELAY "used for 429 and network errors". A timeout (60 s, CLAUDE.md:118) is a transport error."""
    client = use_client(repeat=error_class("boom " + HOSTILE))
    with pytest.raises(llm.NetworkError) as caught:
        ask()
    assert len(client.calls) == 4 and waits.sleeps == [2.0, 4.0, 8.0] and (llm.http_attempts, llm.retries) == (4, 3)
    assert error_class.__name__ in str(caught.value)
    assert not any(part in str(caught.value) for part in FORBIDDEN)


def test_a_network_error_then_a_good_answer_returns_it(use_client, waits):
    client = use_client(httpx.ReadTimeout("slow"), reply(OK_TEXT))
    assert ask().found is True
    assert len(client.calls) == 2 and waits.sleeps == [2.0]


# the response ----------------------------------------------------------------------------------------------------------------------------------

BAD_RESPONSES = [
    (gtypes.GenerateContentResponse(candidates=[]), "empty or blocked"),
    (gtypes.GenerateContentResponse(), "empty or blocked"),
    (reply("   "), "empty or blocked"),
    (reply(""), "empty or blocked"),
    (gtypes.GenerateContentResponse(candidates=[gtypes.Candidate()]), "empty or blocked"),
    (reply("this is not json " + HOSTILE), "did not match"),
    (reply("[]"), "did not match"),
    (reply('{"x": 1}'), "did not match"),
    (reply('{"answer": "a"}'), "did not match"),
    (reply('{"answer": 5, "found": true}'), "did not match"),
    (reply('{"answer": "a", "found": "maybe"}'), "did not match"),
]
BAD_IDS = ["no candidates", "no candidates field", "blank text", "empty text", "candidate without content", "not json", "json list",
           "wrong fields", "missing found", "answer is a number", "found is not a bool"]


@pytest.mark.parametrize("response, words", BAD_RESPONSES, ids=BAD_IDS)
def test_pinned_an_empty_or_malformed_answer_is_a_bad_response_and_is_not_retried(use_client, waits, response, words):
    client = use_client(response)
    with pytest.raises(llm.BadResponseError, match=words) as caught:
        ask()
    assert len(client.calls) == 1 and waits.sleeps == []
    assert not any(part in str(caught.value) for part in FORBIDDEN)


# waits -----------------------------------------------------------------------------------------------------------------------------------------

def test_pinned_the_jitter_is_added_to_every_wait_and_is_drawn_from_0_to_1(use_client, waits):
    """llm.py:20 says "plus jitter" for 429 and network errors; for the 503 table and the hint it is unwritten."""
    waits.jitter = 0.7
    client = use_client(repeat=api_error(429, status="RESOURCE_EXHAUSTED"))
    with pytest.raises(llm.RateLimitError):
        ask()
    assert len(client.calls) == 4
    assert waits.sleeps == pytest.approx([2.7, 4.7, 8.7]) and waits.uniform_args == [(0, 1)] * 3


def test_a_wait_the_server_asks_for_above_60_seconds_is_cut_to_60(use_client, waits):
    """llm.py:22: MAX_WAIT = 60.0 "never sleep longer than this, even if the server asks for more". A hint of 100 s is not above 120,
    so it is retried. Without jitter the cap is exact."""
    waits.jitter = 0.0
    client = use_client(repeat=api_error(429, status="RESOURCE_EXHAUSTED", details=retry_in(100)))
    with pytest.raises(llm.RateLimitError):
        ask()
    assert len(client.calls) == 4 and waits.sleeps == [60.0, 60.0, 60.0]


def test_a_wait_the_server_asks_for_below_60_seconds_is_kept(use_client, waits):
    client = use_client(repeat=api_error(429, status="RESOURCE_EXHAUSTED", details=retry_in(50)))
    with pytest.raises(llm.RateLimitError):
        ask()
    assert len(client.calls) == 4 and waits.sleeps == [50.0, 50.0, 50.0]


def test_a_wait_is_never_longer_than_max_wait_even_with_the_jitter(use_client, waits):
    """llm.py:22: MAX_WAIT "never sleep longer than this, even if the server asks for more". The jitter is part of the sleep."""
    waits.jitter = 1.0
    client = use_client(repeat=api_error(429, status="RESOURCE_EXHAUSTED", details=retry_in(100)))
    with pytest.raises(llm.RateLimitError):
        ask()
    assert len(client.calls) == 4 and len(waits.sleeps) == 3
    assert max(waits.sleeps) <= llm.MAX_WAIT, waits.sleeps


@pytest.mark.parametrize("error_class", [httpx.ConnectError, httpx.ReadTimeout], ids=["ConnectError", "ReadTimeout"])
def test_no_wait_before_a_network_error_is_longer_than_max_wait_even_with_the_jitter(use_client, waits, error_class):
    """The same rule (llm.py:22) for the network path, which does not use the cap itself: it holds because the waits are short."""
    waits.jitter = 1.0
    client = use_client(repeat=error_class("boom"))
    with pytest.raises(llm.NetworkError):
        ask()
    assert len(client.calls) == 4 and len(waits.sleeps) == 3
    assert max(waits.sleeps) <= llm.MAX_WAIT, waits.sleeps


# --- through the API: the real generate_answer, the fake client, the HTTP response ---------------------------------------------------------

def test_pinned_a_429_that_keeps_a_wait_time_carries_a_retry_after_header(api_client, use_client):
    client = use_client(repeat=api_error(429, status="RESOURCE_EXHAUSTED", details=retry_in(30)))
    response = chat(api_client)
    assert len(client.calls) == 4 and response.status_code == 429 and response.json()["code"] == "rate_limited"
    assert "retry-after" in response.headers


def test_pinned_a_429_without_a_wait_time_carries_no_retry_after_header(api_client, use_client):
    client = use_client(repeat=api_error(429, status="RESOURCE_EXHAUSTED"))
    response = chat(api_client)
    assert len(client.calls) == 4 and response.status_code == 429 and "retry-after" not in response.headers


def test_a_good_answer_goes_all_the_way_through(api_client, use_client):
    client = use_client(reply(OK_TEXT))
    response = chat(api_client)
    assert len(client.calls) == 1 and response.status_code == 200
    assert response.json()["answer"] == "Because of OPS-1001." and response.json()["citations"] == ["OPS-1001"]


# --- nothing leaks -------------------------------------------------------------------------------------------------------------------------

def hostile(code, status, message=HOSTILE):
    return api_error(code, message, status, details=[{"note": HOSTILE, "retryDelay": "300s"}] if code == 429 else [{"note": HOSTILE}])


LEAKS = [
    ("400 api key", [hostile(400, "INVALID_ARGUMENT", "API key " + HOSTILE)], None, 500, "server_misconfigured"),
    ("400 other", [hostile(400, "INVALID_ARGUMENT")], None, 502, "bad_upstream_response"),
    ("401", [hostile(401, "UNAUTHENTICATED")], None, 500, "server_misconfigured"),
    ("403", [hostile(403, "PERMISSION_DENIED")], None, 500, "server_misconfigured"),
    ("404", [hostile(404, "NOT_FOUND")], None, 502, "bad_upstream_response"),
    ("429 used up", [hostile(429, "RESOURCE_EXHAUSTED")], None, 429, "rate_limited"),
    ("429 zero limit", [hostile(429, "RESOURCE_EXHAUSTED", "limit: 0 " + HOSTILE)], None, 429, "rate_limited"),
    ("429 exhausted", [], api_error(429, HOSTILE, "RESOURCE_EXHAUSTED", details=[{"note": HOSTILE}]), 429, "rate_limited"),
    ("503 exhausted", [], hostile(503, "UNAVAILABLE"), 503, "upstream_unavailable"),
    ("500", [hostile(500, "INTERNAL")], None, 503, "upstream_unavailable"),
    ("network", [], httpx.ConnectError("https://x.invalid/v1?key=FAKEKEY123 " + HOSTILE), 503, "upstream_unavailable"),
    ("timeout", [], httpx.ReadTimeout(HOSTILE), 503, "upstream_unavailable"),
    ("malformed answer", [reply("{" + HOSTILE)], None, 502, "bad_upstream_response"),
    ("empty answer", [gtypes.GenerateContentResponse(candidates=[])], None, 502, "bad_upstream_response"),
    ("unexpected exception", [RuntimeError(HOSTILE)], None, 500, "internal_error"),
]


@pytest.mark.parametrize("script, repeat, status, code", [l[1:] for l in LEAKS], ids=[l[0] for l in LEAKS])
def test_an_error_response_leaks_neither_the_key_nor_a_path_nor_a_trace_nor_the_upstream_text(api_client, use_client, with_key,
                                                                                                script, repeat, status, code):
    client = use_client(*script, repeat=repeat)
    response = chat(api_client)
    assert len(client.calls) >= 1, "the fake client was not called"
    assert response.status_code == status and set(response.json()) == {"error", "code"} and response.json()["code"] == code
    text = everything(response)
    for part in FORBIDDEN:
        assert part not in text, part
    assert "upstream message" not in text


def test_the_unexpected_500_is_exactly_the_generic_message(api_client, use_client, with_key):
    use_client(RuntimeError(HOSTILE))
    response = chat(api_client)
    assert response.status_code == 500 and response.json() == {"error": "Internal error", "code": "internal_error"}


CAUSES = [("429", api_error(429, HOSTILE, "RESOURCE_EXHAUSTED")), ("503", api_error(503, HOSTILE, "UNAVAILABLE")),
          ("400", api_error(400, HOSTILE, "INVALID_ARGUMENT")), ("403", api_error(403, HOSTILE, "PERMISSION_DENIED")),
          ("500", api_error(500, HOSTILE, "INTERNAL")), ("transport", httpx.ReadTimeout(HOSTILE))]


@pytest.mark.parametrize("cause", [c[1] for c in CAUSES], ids=[c[0] for c in CAUSES])
def test_a_raised_llm_error_carries_no_exception_chain(use_client, waits, cause):
    """`from None`: the upstream exception, which holds the raw message, is not attached as the cause."""
    client = use_client(repeat=cause)
    with pytest.raises(llm.LlmError) as caught:
        ask()
    assert len(client.calls) >= 1
    assert caught.value.__cause__ is None and caught.value.__suppress_context__ is True


def test_a_bad_answer_error_carries_no_exception_chain(use_client):
    client = use_client(reply("{not json"))
    with pytest.raises(llm.BadResponseError) as caught:
        ask()
    assert len(client.calls) == 1 and caught.value.__cause__ is None and caught.value.__suppress_context__ is True


def test_pinned_the_upstream_status_text_is_part_of_the_message_but_the_upstream_message_is_not(use_client):
    """llm.py:3-4 allows "the HTTP code and status". A status is an enum name in real Gemini answers (RESOURCE_EXHAUSTED, ...); if the
    upstream sent a hostile status it would be shown. Recorded as it is today, no code change."""
    client = use_client(api_error(403, "UPSTREAM-MESSAGE", "C:\\secret\\STATUS"))
    with pytest.raises(llm.InvalidApiKeyError) as caught:
        ask()
    assert len(client.calls) == 1
    assert "HTTP 403 C:\\secret\\STATUS" in str(caught.value) and "UPSTREAM-MESSAGE" not in str(caught.value)


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


@pytest.fixture
def log():
    handler, logger = LogCapture(), logging.getLogger("ops_rag")
    logger.addHandler(handler)
    yield handler
    logger.removeHandler(handler)


def test_the_log_of_an_unexpected_error_has_the_trace_but_never_the_key(api_client, use_client, with_key, log):
    use_client(RuntimeError("the key is " + KEY))
    assert chat(api_client).status_code == 500
    text = "\n".join(log.lines)
    assert "Unhandled error on POST /chat" in text and "Traceback" in text and "RuntimeError" in text
    assert KEY not in text and "***" in text


@pytest.mark.parametrize("cause", [api_error(429, HOSTILE, "RESOURCE_EXHAUSTED"), api_error(503, HOSTILE, "UNAVAILABLE"),
                                   httpx.ConnectError(HOSTILE), reply("{" + HOSTILE)], ids=["429", "503", "network", "bad answer"])
def test_the_log_of_a_gemini_failure_has_neither_the_key_nor_the_upstream_text_nor_the_question(api_client, use_client, with_key, log, cause):
    use_client(repeat=cause)
    chat(api_client)
    text = "\n".join(log.lines)
    assert "chat failed llm_call=yes" in text
    for part in FORBIDDEN + [QUESTION]:
        assert part not in text, part
