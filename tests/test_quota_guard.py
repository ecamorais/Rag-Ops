"""The quota guard in app/llm.py: after a Gemini 429 that says the quota is used up, questions that need the model are refused at once.

Written rules the expected values come from (not from running the code):
  CLAUDE.md:109            "after a Gemini 429 whose retry delay is above 120 s ("quota used up"), the reset time is remembered per model
                           name. Until it passes, /chat questions that need the model are refused at once with 429 and a Retry-After
                           header (whole seconds, at most one day), with no Gemini request. Questions that need no model are still
                           answered."
  README.md:40             "After a Gemini 429 whose retry delay is long ("quota used up") the server remembers the reset time and refuses
                           model questions with a 429 and a Retry-After header until then, without sending a request."
  CLAUDE.md:118-119        "Gemini calls are serialised per model (one HTTP attempt at a time) ..."; "The quota guard forgets on restart
                           (it is in memory only)."
  main.py:33               "Retry-After on a 429 is capped at one day."
  llm.py:23, 32-38         QUOTA_USED_UP_AFTER = 120.0 "a 429 retry delay above this means the quota is used up: do not retry"; "_clock is a
                           module name so that tests can replace it with a fake clock."
  llm.py _format_wait      "'about N minutes' under an hour, otherwise 'about N hours'; at least 1, singular for 1."
  llm.py:216-218           HTTP attempts for one model are made one at a time (the attempt lock), and the guard is checked inside that
                           lock: the requests that arrive while the first one learns the quota is used up "are refused without any HTTP
                           attempt".
  llm.py:259               "the lock is released first, so a backoff sleep never blocks other requests"
What no document says (which side of the reset time is "passed", the direction of the rounding, the zero-quota cases, ...) is pinned in tests
named "pinned_". The clock is faked (llm._clock): nothing here sleeps. The few tests with threads use only Events with a 5 s safety timeout, daemon
threads that are always joined in a finally, and fail with a clean assertion, never a hang (their names contain "threads").
Covered in test_errors.py and not repeated: a 300 s hint is not retried and carries retry_after_seconds, the header is present with a hint and
absent without one, no leaks. The tests here start from an armed guard.
"""
import dataclasses
import importlib.util
import inspect
import json
import math
import os
import re
import threading
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
REAL_CLOCK = llm._clock
ORIGINAL_STATE = (llm._quota_resets, llm._attempt_locks)
MODEL = llm.settings.gemini_model
OTHER_MODEL = "some-other-model"
T0 = 1000.0  # the fake clock at the start of a test
QUESTION = "What happened in OPS-1001?"  # an exact id: the context is never empty, so the model is needed
NOTHING = "zzzxqv qwrtp blorf"  # retrieves nothing
OK_TEXT = json.dumps({"answer": "Because of OPS-1001.", "found": True, "citations": ["OPS-1001"]})
SAFETY = 5  # seconds: the longest any thread test waits for an Event


# --- helpers (the same small helpers as test_errors.py) ---------------------------------------------------------------------

def api_error(code, message="upstream message", status=None, details=None, response=None):
    body = {"error": {"code": code, "message": message, "status": status}}
    if details is not None:
        body["error"]["details"] = details
    return (gerrors.ServerError if code >= 500 else gerrors.ClientError)(code, body, response)


def used_up(seconds):
    """A 429 whose body says to retry after `seconds`."""
    return api_error(429, status="RESOURCE_EXHAUSTED", details=[{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": f"{seconds}s"}])


def reply(text):
    return gtypes.GenerateContentResponse(
        candidates=[gtypes.Candidate(content=gtypes.Content(parts=[gtypes.Part(text=text)], role="model"))])


class FakeClient:
    """Stands in for genai.Client: generate_content pops the next scripted result (an exception is raised); when the script is used up,
    `repeat` is used again and again."""

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


class FakeClock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


class Clockwork:
    """Replaces time.sleep and random.uniform inside app.llm: records the waits."""

    def __init__(self):
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)

    def uniform(self, low, high):
        return 0.0


def state():
    return (dict(llm._quota_resets), dict(llm._attempt_locks), llm.http_attempts, llm.retries, llm.last_usage, llm._client, llm._clock,
            llm.settings, main.settings, llm.get_client, llm.generate_answer, llm.time, llm.random)


@pytest.fixture(scope="module", autouse=True)
def the_guard_state_outside_this_file_is_untouched():
    """After the last test of this file (all patches undone) the state of app.llm is what it was before: no remembered reset, no lock, no
    counter, no client, the real clock, so an armed guard here cannot refuse questions in any other test file."""
    before = state()
    yield
    assert llm._quota_resets is ORIGINAL_STATE[0] and llm._attempt_locks is ORIGINAL_STATE[1]
    assert llm._quota_resets == {} and llm._attempt_locks == {}, "a test of this file left state in app.llm"
    assert state() == before


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


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """The module's own clock, faked: it only moves when a test moves it."""
    fake = FakeClock()
    monkeypatch.setattr(llm, "_clock", fake)
    return fake


@pytest.fixture
def waits(monkeypatch):
    recorder = Clockwork()
    monkeypatch.setattr(llm, "time", types.SimpleNamespace(sleep=recorder.sleep, monotonic=time.monotonic))
    monkeypatch.setattr(llm, "random", types.SimpleNamespace(uniform=recorder.uniform))
    return recorder


@pytest.fixture
def real(monkeypatch):
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
def as_model(monkeypatch):
    def switch(name):
        monkeypatch.setattr(llm, "settings", dataclasses.replace(llm.settings, gemini_model=name))
    return switch


def ask():
    return llm.generate_answer("system text", "prompt text")


def arm(use_client, seconds=300):
    """One question whose answer is a 429 with a retry delay of `seconds`: the guard is armed when `seconds` is above 120."""
    client = use_client(used_up(seconds))
    with pytest.raises(llm.RateLimitError):
        ask()
    assert len(client.calls) == 1
    return client


def chat(client, question=QUESTION):
    return client.post("/chat", json={"question": question})


# --- preconditions --------------------------------------------------------------------------------------------------------------

def test_the_real_function_and_the_real_clock_are_what_the_tests_use(stub_model):
    assert REAL_GENERATE.__module__ == "app.llm" and inspect.isfunction(REAL_GENERATE)
    assert REAL_CLOCK is time.monotonic  # llm.py:36
    assert llm.generate_answer is stub_model  # a test that does not ask for the real function gets the stub


def test_the_fake_clock_is_installed_and_starts_at_t0():
    assert llm._clock is not REAL_CLOCK and llm._clock() == T0


# --- the threshold: "above 120 s" ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("seconds", [1, 60, 100, 119.5, 120, 120.0])
def test_a_retry_delay_of_120_seconds_or_less_is_retried_and_does_not_arm_the_guard(use_client, waits, seconds):
    client = use_client(repeat=used_up(seconds))
    with pytest.raises(llm.RateLimitError):
        ask()
    assert len(client.calls) == 4 and waits.sleeps == [min(float(seconds), 60.0)] * 3  # llm.py:19, 22
    assert llm._quota_resets == {}
    follow_up = use_client(reply(OK_TEXT))  # the next question goes to Gemini
    assert ask().found is True and len(follow_up.calls) == 1


@pytest.mark.parametrize("seconds", [120.001, 120.5, 121, 300, 100000])
def test_a_retry_delay_above_120_seconds_arms_the_guard_and_is_not_retried(use_client, waits, seconds):
    client = use_client(repeat=used_up(seconds))
    with pytest.raises(llm.RateLimitError):
        ask()
    assert len(client.calls) == 1 and waits.sleeps == []
    assert llm._quota_resets == {MODEL: pytest.approx(T0 + seconds)}


def test_a_retry_after_header_is_read_like_the_body_delay_for_the_threshold(use_client, waits):
    """llm.py _retry_hint: "RetryInfo in the body, or Retry-After"."""
    low = use_client(repeat=api_error(429, status="RESOURCE_EXHAUSTED", response=httpx.Response(429, headers={"retry-after": "120"})))
    with pytest.raises(llm.RateLimitError):
        ask()
    assert len(low.calls) == 4 and llm._quota_resets == {}
    high = use_client(repeat=api_error(429, status="RESOURCE_EXHAUSTED", response=httpx.Response(429, headers={"retry-after": "121"})))
    with pytest.raises(llm.RateLimitError):
        ask()
    assert len(high.calls) == 1 and llm._quota_resets == {MODEL: pytest.approx(T0 + 121)}


def test_pinned_only_a_429_arms_the_guard(use_client, waits):
    """The text says "a Gemini 429". A 503 with a long delay in its body is retried and arms nothing."""
    body = [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "300s"}]
    client = use_client(repeat=api_error(503, status="UNAVAILABLE", details=body))
    with pytest.raises(llm.ServerError):
        ask()
    assert len(client.calls) == 4 and llm._quota_resets == {}


def test_pinned_a_429_without_any_delay_does_not_arm_the_guard(use_client, waits):
    client = use_client(repeat=api_error(429, status="RESOURCE_EXHAUSTED"))
    with pytest.raises(llm.RateLimitError):
        ask()
    assert len(client.calls) == 4 and llm._quota_resets == {}


def test_pinned_a_short_delay_and_then_a_long_one_arms_the_guard_on_the_second_attempt(use_client, waits):
    client = use_client(used_up(30), used_up(300), reply(OK_TEXT))
    with pytest.raises(llm.RateLimitError):
        ask()
    assert len(client.calls) == 2 and waits.sleeps == [30.0]
    assert llm._quota_resets == {MODEL: pytest.approx(T0 + 300)}


def test_pinned_a_zero_quota_429_with_a_long_delay_arms_the_guard_but_the_first_answer_has_no_retry_after(api_client, use_client):
    """Observation from reading llm.py: used_up is computed before the zero-quota check, and _classify returns the "zero on this key" message without
    retry_after_seconds. CLAUDE.md:109 does not say what that first answer carries."""
    zero = api_error(429, "Quota exceeded for metric, limit: 0, model: x", "RESOURCE_EXHAUSTED",
                     details=[{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "300s"}])
    client = use_client(zero)
    first = chat(api_client)
    assert len(client.calls) == 1 and first.status_code == 429 and "zero" in first.json()["error"]
    assert "retry-after" not in first.headers
    assert llm._quota_resets == {MODEL: pytest.approx(T0 + 300)}  # armed all the same
    second = chat(api_client)
    assert len(client.calls) == 1 and second.status_code == 429 and second.headers["retry-after"] == "300"


def test_pinned_a_zero_quota_429_without_a_delay_does_not_arm_the_guard(use_client):
    zero = api_error(429, "Quota exceeded for metric, limit: 0, model: x", "RESOURCE_EXHAUSTED")
    client = use_client(zero)
    with pytest.raises(llm.RateLimitError, match="zero"):
        ask()
    assert len(client.calls) == 1 and llm._quota_resets == {}
    follow_up = use_client(reply(OK_TEXT))  # the next question calls Gemini again
    assert ask().found is True and len(follow_up.calls) == 1


# --- what is remembered, and the refusal -------------------------------------------------------------------------------------------

def test_the_reset_time_is_remembered_under_the_model_name_as_now_plus_the_delay(use_client):
    arm(use_client, 300)
    assert llm._quota_resets == {MODEL: T0 + 300}


def test_while_the_guard_is_on_a_question_is_refused_with_no_http_attempt(use_client, waits, clock):
    arm(use_client, 300)
    attempts, retries = llm.http_attempts, llm.retries
    clock.now = T0 + 100
    client = use_client(reply(OK_TEXT))
    with pytest.raises(llm.RateLimitError) as caught:
        ask()
    assert client.calls == [], "a request was sent to Gemini while the guard was on"
    assert (llm.http_attempts, llm.retries) == (attempts, retries) and waits.sleeps == []
    assert type(caught.value) is llm.RateLimitError
    assert caught.value.retry_after_seconds == pytest.approx(200)  # what is left of the 300 s
    assert "used up" in str(caught.value)  # pinned wording: llm._quota_message
    assert llm._quota_resets == {MODEL: T0 + 300}  # a refusal does not change the memory


def test_a_refusal_does_not_need_a_working_client(monkeypatch, use_client, real):
    """The guard is checked before any request, but get_client is called first (llm.py:222): with a client that is never used."""
    arm(use_client, 300)
    client = use_client(AssertionError("a request was sent"))
    with pytest.raises(llm.RateLimitError):
        ask()
    assert client.calls == []


# --- the module's clock: expiry ----------------------------------------------------------------------------------------------------------

def test_the_guard_ends_when_the_reset_time_has_passed(use_client):
    arm(use_client, 300)
    resets = dict(llm._quota_resets)
    for moment in (T0 + 1, T0 + 150, T0 + 299.999):
        llm._clock.now = moment
        client = use_client(reply(OK_TEXT))
        with pytest.raises(llm.RateLimitError):
            ask()
        assert client.calls == []
    assert llm._quota_resets == resets
    llm._clock.now = T0 + 300.1  # past the reset time
    client = use_client(reply(OK_TEXT))
    assert ask().found is True and len(client.calls) == 1


def test_pinned_at_exactly_the_reset_time_the_guard_is_over(use_client):
    """"Until it passes" does not say which side the equal case is on; the code treats left <= 0 as over."""
    arm(use_client, 300)
    llm._clock.now = T0 + 300
    client = use_client(reply(OK_TEXT))
    assert ask().found is True and len(client.calls) == 1
    assert llm._quota_resets == {}  # and the entry is gone: nothing is left that says the guard is on


def test_pinned_an_expired_entry_is_removed_when_the_next_question_finds_it(use_client):
    arm(use_client, 300)
    llm._clock.now = T0 + 301
    assert llm._quota_resets == {MODEL: T0 + 300}  # nothing removes it by itself
    client = use_client(reply(OK_TEXT))
    ask()
    assert len(client.calls) == 1 and llm._quota_resets == {}


def test_pinned_a_new_used_up_answer_after_the_expiry_arms_the_guard_again_from_the_new_time(use_client):
    arm(use_client, 300)
    llm._clock.now = T0 + 400
    client = use_client(used_up(500))
    with pytest.raises(llm.RateLimitError):
        ask()
    assert len(client.calls) == 1
    assert llm._quota_resets == {MODEL: T0 + 400 + 500}


# --- per model ---------------------------------------------------------------------------------------------------------------------------

def test_a_second_model_name_is_not_blocked(use_client, as_model):
    arm(use_client, 300)
    as_model(OTHER_MODEL)
    client = use_client(reply(OK_TEXT))
    assert ask().found is True and len(client.calls) == 1
    assert client.calls[0][0] == OTHER_MODEL and llm._quota_resets == {MODEL: T0 + 300}


def test_each_model_has_its_own_reset_time_and_the_first_is_still_refused(use_client, as_model):
    arm(use_client, 300)
    as_model(OTHER_MODEL)
    arm(use_client, 500)
    assert llm._quota_resets == {MODEL: T0 + 300, OTHER_MODEL: T0 + 500}
    llm._clock.now = T0 + 400  # the first model's quota is back, the second one's is not
    for model, refused in ((MODEL, False), (OTHER_MODEL, True)):
        as_model(model)
        client = use_client(reply(OK_TEXT))
        if refused:
            with pytest.raises(llm.RateLimitError):
                ask()
            assert client.calls == []
        else:
            assert ask().found is True and len(client.calls) == 1


# --- through the API: Retry-After ----------------------------------------------------------------------------------------------------------

def test_the_first_answer_and_a_refusal_both_say_429_with_a_retry_after(api_client, use_client):
    client = use_client(used_up(300))
    first = chat(api_client)
    assert first.status_code == 429 and first.json()["code"] == "rate_limited" and first.headers["retry-after"] == "300"
    assert len(client.calls) == 1
    second = chat(api_client)
    assert len(client.calls) == 1, "the second question reached Gemini"
    assert second.status_code == 429 and second.json()["code"] == "rate_limited" and set(second.json()) == {"error", "code"}
    assert second.headers["retry-after"] == "300"
    assert "used up" in second.json()["error"]


@pytest.mark.parametrize("passed", [0.5, 1, 100, 250, 299, 299.5, 299.75])
def test_the_retry_after_is_a_whole_number_of_seconds_not_more_than_one_second_above_what_is_left(api_client, use_client, clock, passed):
    """CLAUDE.md:109: "whole seconds"."""
    arm(use_client, 300)
    clock.now = T0 + passed
    client = use_client(reply(OK_TEXT))
    response = chat(api_client)
    assert client.calls == [] and response.status_code == 429
    value = response.headers["retry-after"]
    assert re.fullmatch(r"[0-9]+", value)
    left = 300 - passed
    assert math.floor(left) <= int(value) <= math.ceil(left)


@pytest.mark.parametrize("passed, expected", [(0.5, "300"), (100, "200"), (299.5, "1"), (299.75, "1")],
                         ids=["299.5 left", "200 left", "0.5 left", "0.25 left"])
def test_pinned_the_retry_after_is_the_remaining_time_rounded_up(api_client, use_client, clock, passed, expected):
    """Only "whole seconds" is written; the code rounds up (math.ceil), so a client never retries too early."""
    arm(use_client, 300)
    clock.now = T0 + passed
    response = chat(api_client)
    assert response.status_code == 429 and response.headers["retry-after"] == expected


def test_pinned_a_fractional_delay_is_rounded_up_in_the_first_answer(api_client, use_client):
    use_client(used_up(300.4))
    assert chat(api_client).headers["retry-after"] == "301"


@pytest.mark.parametrize("seconds, expected", [(86399, "86399"), (86400, "86400"), (86400.5, "86400"), (200000, "86400")])
def test_the_retry_after_is_at_most_one_day_in_the_first_answer(api_client, use_client, seconds, expected):
    """main.py:33 and CLAUDE.md:109: "at most one day"."""
    use_client(used_up(seconds))
    assert chat(api_client).headers["retry-after"] == expected


def test_the_cap_applies_to_the_remaining_time_and_falls_away_below_a_day(api_client, use_client, clock):
    arm(use_client, 200000)
    for passed, expected in ((0, "86400"), (100000, "86400"), (113600, "86400"), (150000, "50000")):
        clock.now = T0 + passed
        response = chat(api_client)
        assert response.status_code == 429 and response.headers["retry-after"] == expected, passed


@pytest.mark.parametrize("seconds, expected", [(0.2, "1"), (-3, "1")], ids=["0.2", "negative"])
def test_pinned_a_retry_after_is_at_least_1(api_client, stub_model, seconds, expected):
    """main.py max(1, ...): for a positive number math.ceil already gives 1, so only a negative number tells the two apart."""
    stub_model.fail(llm.RateLimitError("x", retry_after_seconds=seconds))
    response = chat(api_client)
    assert response.status_code == 429 and response.headers["retry-after"] == expected


def test_pinned_a_zero_wait_time_has_no_retry_after(api_client, stub_model):
    stub_model.fail(llm.RateLimitError("x", retry_after_seconds=0))
    assert "retry-after" not in chat(api_client).headers


# --- through the API: what is still answered ----------------------------------------------------------------------------------------------

def test_questions_that_need_no_model_are_still_answered_while_the_guard_is_on(api_client, use_client):
    client = use_client(used_up(300))
    assert chat(api_client).status_code == 429 and len(client.calls) == 1
    nothing = chat(api_client, NOTHING)
    assert nothing.status_code == 200 and nothing.json()["found"] is False and nothing.json()["sources"] == []
    missing = chat(api_client, "What happened in OPS-9999?")
    assert missing.status_code == 200 and missing.json()["missing_references"] == ["OPS-9999"] and missing.json()["found"] is False
    assert len(client.calls) == 1  # none of them reached Gemini
    assert chat(api_client).status_code == 429  # a question that needs the model is still refused


def test_the_other_routes_do_not_depend_on_the_guard(api_client, use_client):
    use_client(used_up(300))
    assert chat(api_client).status_code == 429
    assert api_client.get("/health").status_code == 200
    assert api_client.get("/filters").status_code == 200
    assert api_client.get("/tickets/OPS-1001").status_code == 200
    search = api_client.post("/search", json={"query": "vpn outage"})
    assert search.status_code == 200 and search.json()["hits"]


def test_the_guard_ends_for_the_api_too(api_client, use_client, clock):
    arm(use_client, 300)
    assert chat(api_client).status_code == 429
    clock.now = T0 + 301
    client = use_client(reply(OK_TEXT))
    response = chat(api_client)
    assert response.status_code == 200 and len(client.calls) == 1


# --- the wording of the wait ("about N minutes / hours") -----------------------------------------------------------------------------------

@pytest.mark.parametrize("seconds, text", [
    (1, "about 1 minute"), (30, "about 1 minute"), (59, "about 1 minute"), (120, "about 2 minutes"), (300, "about 5 minutes"),
    (3000, "about 50 minutes"), (3600, "about 1 hour"), (7200, "about 2 hours"), (10800, "about 3 hours"), (86400, "about 24 hours"),
])
def test_the_wait_is_told_in_minutes_under_an_hour_and_in_hours_from_an_hour(seconds, text):
    """The _format_wait docstring: 'about N minutes' under an hour, otherwise 'about N hours'; at least 1, singular for 1."""
    assert llm._format_wait(seconds) == text


def test_pinned_just_under_an_hour_is_sixty_minutes():
    assert llm._format_wait(3599) == "about 60 minutes"


def test_pinned_the_message_has_the_wait_and_the_code_and_status():
    message = llm._quota_message(300, "HTTP 429 RESOURCE_EXHAUSTED")
    assert "used up" in message and "about 5 minutes" in message and message.endswith("(HTTP 429 RESOURCE_EXHAUSTED).")


# --- the guard forgets on restart (CLAUDE.md:119) -------------------------------------------------------------------------------------------

def snapshot(folder):
    """(relative name, size, mtime) of every file under app/ or data/, without __pycache__."""
    found = {}
    for path in sorted((ROOT / folder).rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            stat = path.stat()
            found[path.relative_to(ROOT).as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return found


def test_the_guard_is_only_in_memory_a_fresh_copy_of_the_module_knows_nothing_of_it(use_client):
    """This proves the in-memory claim INDIRECTLY: a real server restart cannot be done inside pytest. It shows (1) that arming the guard writes no
    file under app/ or data/ and creates no .env or state file, and (2) that a fresh execution of app/llm.py, which is what a restart is, starts with
    an empty memory although the guard is armed in this process."""
    files_before = {name: snapshot(name) for name in ("app", "data")}
    env_before = (ROOT / ".env").exists()  # only whether it exists; the file is never opened
    root_before = sorted(p.name for p in ROOT.iterdir() if re.search(r"quota|state", p.name, re.I))
    arm(use_client, 300)
    assert llm._quota_resets == {MODEL: T0 + 300}
    assert {name: snapshot(name) for name in ("app", "data")} == files_before
    assert (ROOT / ".env").exists() == env_before
    assert sorted(p.name for p in ROOT.iterdir() if re.search(r"quota|state", p.name, re.I)) == root_before
    spec = importlib.util.spec_from_file_location("llm_fresh_copy", llm.__file__)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    assert fresh is not llm and fresh._quota_resets == {} and fresh._quota_seconds_left(MODEL) == 0.0
    assert llm._quota_resets == {MODEL: T0 + 300}  # the running module still has it


# --- serialisation per model (CLAUDE.md:118) and the lock during a backoff sleep (llm.py:259) -----------------------------------------------
# Events only, daemon threads, a 5 s safety timeout on every wait, every thread joined in a finally.

class WatchedLock:
    """The attempt lock of one model, plus an Event that is set when a thread arrives while the lock is held."""

    def __init__(self):
        self._lock = threading.Lock()
        self.someone_waits = threading.Event()

    def __enter__(self):
        if self._lock.locked():
            self.someone_waits.set()
        self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self._lock.release()


class GateClient:
    """The first call for gate_model stops inside generate_content until `release` is set, then answers with `outcome`; every other
    call answers at once."""

    def __init__(self, gate_model, release, inside, outcome):
        self.gate_model, self.release, self.inside, self.outcome = gate_model, release, inside, outcome
        self.calls, self.gated = [], False
        self.models = self

    def generate_content(self, model, contents, config):
        self.calls.append(model)
        if model == self.gate_model and not self.gated:
            self.gated = True
            self.inside.set()
            self.release.wait(SAFETY)
            if isinstance(self.outcome, BaseException):
                raise self.outcome
            return self.outcome
        return reply(OK_TEXT)


def in_thread(box, name, function):
    def run():
        try:
            box[name] = function()
        except BaseException as error:  # the test looks at it
            box[name] = error
    thread = threading.Thread(target=run, name=name, daemon=True)
    thread.start()
    return thread


def test_threads_a_request_that_arrives_while_the_first_one_learns_the_quota_is_used_up_is_refused_without_a_request(monkeypatch, real, waits, as_model):
    lock = WatchedLock()
    llm._attempt_locks[MODEL] = lock
    release, inside, box = threading.Event(), threading.Event(), {}
    client = GateClient(MODEL, release, inside, used_up(300))
    monkeypatch.setattr(llm, "get_client", lambda: client)
    threads = []
    try:
        threads.append(in_thread(box, "A", ask))
        assert inside.wait(SAFETY), "the first request never reached Gemini"
        threads.append(in_thread(box, "B", ask))
        assert lock.someone_waits.wait(SAFETY), "the second request did not wait for the lock"
        assert len(client.calls) == 1 and llm.http_attempts == 1, "the second request was sent while the first held the lock"
    finally:
        release.set()
        for thread in threads:
            thread.join(SAFETY)
    assert not any(thread.is_alive() for thread in threads), "a thread did not finish"
    assert isinstance(box["A"], llm.RateLimitError) and box["A"].retry_after_seconds == 300
    assert isinstance(box["B"], llm.RateLimitError) and box["B"].retry_after_seconds == pytest.approx(300)
    assert len(client.calls) == 1 and llm.http_attempts == 1, "the refused request still sent something"
    assert llm._quota_resets == {MODEL: T0 + 300}


def test_threads_a_request_for_another_model_is_not_held_by_the_lock_of_the_first(monkeypatch, real, waits, as_model):
    release, inside, box = threading.Event(), threading.Event(), {}
    client = GateClient(MODEL, release, inside, reply(OK_TEXT))
    monkeypatch.setattr(llm, "get_client", lambda: client)
    threads = []
    try:
        threads.append(in_thread(box, "A", ask))
        assert inside.wait(SAFETY), "the first request never reached Gemini"
        as_model(OTHER_MODEL)  # the next request is for another model
        other = in_thread(box, "C", ask)
        threads.append(other)
        other.join(SAFETY)
        assert not other.is_alive(), "the request for another model was held by the lock of the first"
        assert isinstance(box["C"], llm.GeminiAnswer)
        assert threads[0].is_alive(), "the first request should still be inside Gemini"
        assert client.calls == [MODEL, OTHER_MODEL]
    finally:
        release.set()
        for thread in threads:
            thread.join(SAFETY)
    assert not any(thread.is_alive() for thread in threads), "a thread did not finish"
    assert isinstance(box["A"], llm.GeminiAnswer)
    assert llm._attempt_locks[MODEL] is not llm._attempt_locks[OTHER_MODEL]


def test_threads_the_lock_is_free_while_a_request_sleeps_before_its_retry(monkeypatch, real):
    asleep, resume, box = threading.Event(), threading.Event(), {}

    def sleep(seconds):  # the backoff sleep of the first request: it stops here until the test lets it go
        asleep.set()
        resume.wait(SAFETY)

    monkeypatch.setattr(llm, "time", types.SimpleNamespace(sleep=sleep, monotonic=time.monotonic))
    monkeypatch.setattr(llm, "random", types.SimpleNamespace(uniform=lambda low, high: 0.0))
    client = FakeClient([api_error(503, status="UNAVAILABLE"), reply(OK_TEXT), reply(OK_TEXT)])  # A fails, B answers, A's retry answers
    monkeypatch.setattr(llm, "get_client", lambda: client)
    threads = []
    try:
        threads.append(in_thread(box, "A", ask))
        assert asleep.wait(SAFETY), "the first request never went to sleep"
        second = in_thread(box, "B", ask)
        threads.append(second)
        second.join(SAFETY)
        assert not second.is_alive(), "the second request was held by the lock while the first one slept"
        assert isinstance(box["B"], llm.GeminiAnswer) and len(client.calls) == 2
        assert threads[0].is_alive(), "the first request should still be asleep"
    finally:
        resume.set()
        for thread in threads:
            thread.join(SAFETY)
    assert not any(thread.is_alive() for thread in threads), "a thread did not finish"
    assert isinstance(box["A"], llm.GeminiAnswer) and len(client.calls) == 3 and llm.retries == 1


# =====================================================================================================================================
# A retry hint that is not a usable number of seconds is no hint at all
# The rule (decided after the inf / nan probe; llm._usable_wait): a wait time from the server counts only when it is finite and zero or more.
# An infinite, not-a-number or negative hint is treated as no hint, so the normal backoff applies: llm.py:20-21 (BASE_DELAY doubles: 2, 4, 8;
# BACKOFF_503 = 5, 15, 45) and MAX_ATTEMPTS = 4 (llm.py:19). Before the fix a 429 with an infinite hint armed the guard with "inf" and escaped as
# an OverflowError, and every later model question was a 500 until a restart; nan, -inf and negative hints made time.sleep raise ValueError.
# Huge FINITE hints (1e12 and up) are NOT covered here: they belong to the separate clamp step.
# =====================================================================================================================================

BAD_HINTS = ["inf", "-inf", "nan", "-5", "1e999"]
FORMS = ["body", "header"]


def hinted(code, form, hint):
    """An API error whose server hint is `hint` (text), in the body (RetryInfo) or in the Retry-After header."""
    if form == "body":
        return api_error(code, status="RESOURCE_EXHAUSTED" if code == 429 else "UNAVAILABLE",
                         details=[{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": f"{hint}s"}])
    return api_error(code, status="RESOURCE_EXHAUSTED" if code == 429 else "UNAVAILABLE", response=httpx.Response(code, headers={"retry-after": hint}))


@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("hint", BAD_HINTS)
def test_a_429_with_an_unusable_hint_is_retried_with_the_normal_backoff_and_does_not_arm_the_guard(use_client, waits, form, hint):
    client = use_client(repeat=hinted(429, form, hint))
    with pytest.raises(llm.RateLimitError) as caught:
        ask()
    assert len(client.calls) == 4 and waits.sleeps == [2.0, 4.0, 8.0]
    assert (llm.http_attempts, llm.retries) == (4, 3)
    assert llm._quota_resets == {}
    assert caught.value.retry_after_seconds is None  # the same error as for a 429 without any hint
    assert "used up" not in str(caught.value) and "rate limited" in str(caught.value)


@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("hint", BAD_HINTS)
def test_a_503_with_an_unusable_hint_is_retried_with_the_503_backoff(use_client, waits, form, hint):
    client = use_client(repeat=hinted(503, form, hint))
    with pytest.raises(llm.ServerError):
        ask()
    assert len(client.calls) == 4 and waits.sleeps == [5.0, 15.0, 45.0]
    assert llm._quota_resets == {}


@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("hint", BAD_HINTS)
def test_through_the_api_a_429_with_an_unusable_hint_is_a_plain_429_without_retry_after(api_client, use_client, form, hint):
    client = use_client(repeat=hinted(429, form, hint))
    response = chat(api_client)
    assert len(client.calls) == 4
    assert response.status_code == 429 and response.json()["code"] == "rate_limited" and set(response.json()) == {"error", "code"}
    assert "retry-after" not in response.headers


@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("hint", BAD_HINTS)
def test_after_a_429_with_an_unusable_hint_the_next_question_is_not_refused(use_client, form, hint):
    """The regression that motivated the fix: with "inf" the guard used to be armed for ever and every later question failed."""
    first = use_client(repeat=hinted(429, form, hint))
    with pytest.raises(llm.RateLimitError):
        ask()
    assert len(first.calls) == 4 and llm._quota_resets == {}
    second = use_client(reply(OK_TEXT))
    assert ask().found is True
    assert len(second.calls) == 1, "the next question never reached Gemini: the guard refused it"


@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("hint", BAD_HINTS)
def test_through_the_api_after_a_429_with_an_unusable_hint_the_next_question_is_answered(api_client, use_client, form, hint):
    first = use_client(repeat=hinted(429, form, hint))
    assert chat(api_client).status_code == 429 and len(first.calls) == 4
    second = use_client(reply(OK_TEXT))
    response = chat(api_client)
    assert response.status_code == 200, response.text  # not a refusal and not a 500
    assert len(second.calls) == 1 and llm._quota_resets == {}


@pytest.mark.parametrize("form, hint", [("body", "0"), ("header", "0")])
def test_pinned_a_hint_of_zero_is_still_a_zero_second_wait_and_does_not_arm_the_guard(api_client, use_client, waits, form, hint):
    """Unchanged by the fix: 0 is finite and not negative, so it is a hint. The wait is the hint (0) plus the jitter, the error carries 0, and
    a 0 has no Retry-After header (it is not a wait time to show)."""
    assert llm._retry_hint(hinted(429, form, hint)) == 0.0
    client = use_client(repeat=hinted(429, form, hint))
    with pytest.raises(llm.RateLimitError) as caught:
        ask()
    assert len(client.calls) == 4 and waits.sleeps == [0.0, 0.0, 0.0]
    assert caught.value.retry_after_seconds == 0.0 and llm._quota_resets == {}
    through_api = use_client(repeat=hinted(429, form, hint))
    response = chat(api_client)
    assert len(through_api.calls) == 4 and response.status_code == 429 and "retry-after" not in response.headers


@pytest.mark.parametrize("bad", ["inf", "-inf", "nan", "-5", "1e999"])
def test_a_bad_body_entry_followed_by_a_good_one_uses_the_good_one(use_client, waits, bad):
    two_entries = [{"retryDelay": f"{bad}s"}, {"retryDelay": "30s"}]
    error = api_error(429, status="RESOURCE_EXHAUSTED", details=two_entries)
    assert llm._retry_hint(error) == 30.0
    client = use_client(repeat=error)
    with pytest.raises(llm.RateLimitError) as caught:
        ask()
    assert len(client.calls) == 4 and waits.sleeps == [30.0, 30.0, 30.0] and caught.value.retry_after_seconds == 30.0


@pytest.mark.parametrize("bad", ["inf", "-inf", "nan", "-5", "1e999"])
def test_a_bad_body_entry_followed_by_a_good_header_uses_the_header(use_client, waits, bad):
    error = api_error(429, status="RESOURCE_EXHAUSTED", details=[{"retryDelay": f"{bad}s"}], response=httpx.Response(429, headers={"retry-after": "45"}))
    assert llm._retry_hint(error) == 45.0
    client = use_client(repeat=error)
    with pytest.raises(llm.RateLimitError) as caught:
        ask()
    assert len(client.calls) == 4 and waits.sleeps == [45.0, 45.0, 45.0] and caught.value.retry_after_seconds == 45.0


def test_a_bad_body_entry_and_a_bad_header_are_no_hint():
    error = api_error(429, status="RESOURCE_EXHAUSTED", details=[{"retryDelay": "nans"}], response=httpx.Response(429, headers={"retry-after": "inf"}))
    assert llm._retry_hint(error) is None


def test_pinned_a_good_body_entry_still_wins_over_the_header():
    error = api_error(429, status="RESOURCE_EXHAUSTED", details=[{"retryDelay": "30s"}], response=httpx.Response(429, headers={"retry-after": "45"}))
    assert llm._retry_hint(error) == 30.0
