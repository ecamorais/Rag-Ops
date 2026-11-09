"""app/auth/routes.py: POST /auth/login at HTTP level (Phase 3, step D).

The app is the throwaway one of gate_support.py with the REAL router and middleware, the real AuthService on a database under tmp_path and the FakeClock. The
delay of a failed login is applied by routes.apply_delay, which these tests replace with a recorder, so nothing sleeps. Expected values come from the Phase 3
plan and the step D request: 200 {"username","csrf_token"} with two Set-Cookie headers; every failure the same 401 {"error":"Invalid username or password.",
"code":"invalid_credentials"} with no cookie, after apply_delay(delay) (never for success, busy or refusal); busy 429 + Retry-After 2; trusted refusal 429
too_many_attempts + Retry-After; the rightmost X-Forwarded-For entry only when the proxy is trusted; a body that is not exactly the expected object is 422
invalid_request with nothing echoed. Tests named pinned_* record behaviour the plan leaves open.
"""
import json
import logging
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from app.auth import passwords, routes
from app.auth.passwords import Busy, HashGate
from gate_support import PASSWORD, RouteRig, build_routes_app, cookies_of

BAD_LOGIN = {"error": "Invalid username or password.", "code": "invalid_credentials"}
INVALID = {"error": "The request is not valid.", "code": "invalid_request"}
BUSY = {"error": "The server is busy. Try again shortly.", "code": "busy"}
TOO_MANY = {"error": "Too many attempts. Try again later.", "code": "too_many_attempts"}
MARKER = "hunter2-hunter2-hunter2"  # a password no response, log or error may ever show
BACKSLASH = chr(92)
ROOT = Path(__file__).resolve().parents[2]

IMPORT_PROOF = r"""
import faulthandler, json, os, sys, threading
faulthandler.dump_traceback_later(120, exit=True)
sys.path.insert(0, sys.argv[1])
import fastapi, starlette.responses, starlette.requests, starlette.concurrency  # the libraries read what they read before the recorder starts
reads = []

class Recorder(dict):
    def __getitem__(self, k): reads.append(k); return super().__getitem__(k)
    def get(self, k, d=None): reads.append(k); return super().get(k, d)
    def __contains__(self, k): reads.append(k); return super().__contains__(k)
    def __iter__(self): reads.append("<iter>"); return super().__iter__()
    def keys(self): reads.append("<keys>"); return super().keys()
    def items(self): reads.append("<items>"); return super().items()
    def __len__(self): reads.append("<len>"); return super().__len__()

os.environ = Recorder(dict(os.environ))
import app.auth.routes
print(json.dumps({"reads": reads, "threads": threading.active_count(), "app_config": "app.config" in sys.modules, "main": "app.main" in sys.modules,
                  "dotenv": "dotenv" in sys.modules, "cwd": sorted(os.listdir("."))}))
"""


class Delays(list):
    """What routes.apply_delay was called with, and on which thread."""

    def __init__(self):
        super().__init__()
        self.threads = []


@pytest.fixture(autouse=True)
def delays(monkeypatch):
    recorded = Delays()

    async def record(seconds):
        recorded.append(seconds)
        recorded.threads.append(threading.get_ident())

    monkeypatch.setattr(routes, "apply_delay", record)
    return recorded


@pytest.fixture
def make_rig(tmp_path, make_service, clock):
    made = []

    def make(*, base_url="http://testserver", users=("alice",), **overrides):
        values = {"session_idle_seconds": 600, "session_absolute_seconds": 3600}
        values.update(overrides)
        env = make_service(**values)
        for name in users:
            env.add_local_user(name, PASSWORD)
        made.append(env)
        return RouteRig(build_routes_app(tmp_path / f"static{len(made)}"), env, clock, base_url=base_url)

    return make


@pytest.fixture
def rig(make_rig):
    return make_rig()


class FullGate(HashGate):
    """A hashing gate that is always full."""

    def acquire(self):
        raise Busy("queue_full")


def failures_in_db(rig):
    return rig.env.sql("SELECT COUNT(*) FROM login_failures")[0][0]


# --- importing the module -------------------------------------------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def import_proof(tmp_path_factory):
    work = tmp_path_factory.mktemp("routes_import_proof")
    result = subprocess.run([sys.executable, "-B", "-c", IMPORT_PROOF, str(ROOT)], capture_output=True, text=True, timeout=180, cwd=work)
    assert result.returncode == 0, result.stderr[-800:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_importing_routes_reads_no_environment_variable_and_loads_neither_the_app_nor_its_settings(import_proof):
    assert import_proof["reads"] == [] and not import_proof["app_config"] and not import_proof["main"] and not import_proof["dotenv"]


def test_importing_routes_starts_nothing_and_creates_no_file(import_proof):
    assert import_proof["threads"] == 1 and import_proof["cwd"] == []


# --- success ---------------------------------------------------------------------------------------------------------------------------------------

def test_a_good_login_is_200_with_the_username_and_the_csrf_token_and_nothing_else(rig):
    response = rig.login_http("alice", PASSWORD)
    body = response.json()
    assert response.status_code == 200 and set(body) == {"username", "csrf_token"} and body["username"] == "alice" and len(body["csrf_token"]) == 43
    assert response.headers["content-type"] == "application/json" and response.headers["cache-control"] == "no-store"


def test_a_good_login_sets_exactly_two_cookies_as_separate_headers_with_the_approved_flags(rig):
    response = rig.login_http("alice", PASSWORD)
    lines = response.headers.get_list("set-cookie")
    assert len(lines) == 2 and all("," not in line.split(";")[0] for line in lines)
    cookies = cookies_of(response)
    assert set(cookies) == {"ops_session", "ops_csrf"}
    session_value, session = cookies["ops_session"]
    csrf_value, csrf = cookies["ops_csrf"]
    assert session == {"Path": "/", "Max-Age": "3600", "HttpOnly": True, "SameSite": "Lax"}
    assert csrf == {"Path": "/", "Max-Age": "3600", "SameSite": "Lax"}
    assert len(session_value) == 43 and csrf_value == response.json()["csrf_token"] and session_value != csrf_value
    assert session_value not in response.text  # the session token is only in the cookie


def test_the_new_session_works_on_a_protected_route_and_its_csrf_token_works_on_an_unsafe_one(rig):
    cookies = cookies_of(rig.login_http("alice", PASSWORD))
    token, csrf = cookies["ops_session"][0], cookies["ops_csrf"][0]
    assert rig.send("GET", "/filters", token=token).json() == {"user": "alice"}
    assert rig.send("POST", "/chat", token=token, csrf=csrf).status_code == 200
    assert rig.send("POST", "/chat", token=token, csrf="wrong").status_code == 403


def test_the_username_is_normalised_in_the_answer_and_the_login_still_works(rig):
    response = rig.login_http("  ALICE  ", PASSWORD)
    assert response.status_code == 200 and response.json()["username"] == "alice"


def test_a_good_login_is_never_delayed(rig, delays):
    for _ in range(3):
        rig.login_http("alice", "wrong password here")
    count = len(delays)
    assert rig.login_http("alice", PASSWORD).status_code == 200
    assert len(delays) == count


def test_each_login_makes_a_new_session(rig):
    first = cookies_of(rig.login_http("alice", PASSWORD))["ops_session"][0]
    second = cookies_of(rig.login_http("alice", PASSWORD))["ops_session"][0]
    assert first != second
    assert rig.send("GET", "/filters", token=first).status_code == 200 and rig.send("GET", "/filters", token=second).status_code == 200


# --- the old session ---------------------------------------------------------------------------------------------------------------------------------

def test_a_login_ends_the_session_it_was_made_from(rig):
    old = rig.login("alice")
    response = rig.login_http("alice", PASSWORD, cookie=f"ops_session={old.token}")
    assert response.status_code == 200
    assert rig.send("GET", "/filters", old).status_code == 401  # the old one is gone
    new = cookies_of(response)["ops_session"][0]
    assert rig.send("GET", "/filters", token=new).status_code == 200
    assert rig.env.sql("SELECT COUNT(*) FROM sessions") == [(1,)]


def test_pinned_a_login_ends_whatever_live_session_cookie_it_carries_even_another_users(rig):
    rig.env.add_local_user("bob", PASSWORD)
    bobs = rig.login("bob")
    assert rig.login_http("alice", PASSWORD, cookie=f"ops_session={bobs.token}").status_code == 200
    assert rig.send("GET", "/filters", bobs).status_code == 401


@pytest.mark.parametrize("cookie", ["ops_session=" + "B" * 43, "ops_session=garbage", "ops_session=", "other=1", "ops_session=x; ops_session=y"])
def test_a_dead_or_odd_session_cookie_does_not_stop_a_login(rig, cookie):
    assert rig.login_http("alice", PASSWORD, cookie=cookie).status_code == 200


def test_a_failed_login_leaves_the_old_session_alone(rig):
    old = rig.login("alice")
    assert rig.login_http("alice", "the wrong password", cookie=f"ops_session={old.token}").status_code == 401
    assert rig.send("GET", "/filters", old).status_code == 200


# --- failures: the answer ----------------------------------------------------------------------------------------------------------------------------

def test_a_bad_login_is_one_fixed_401_with_no_cookie_and_one_delay_call(rig, delays):
    response = rig.login_http("alice", "the wrong password")
    assert response.status_code == 401 and response.json() == BAD_LOGIN
    assert response.content == b'{"error":"Invalid username or password.","code":"invalid_credentials"}'
    assert response.headers["cache-control"] == "no-store" and response.headers.get_list("set-cookie") == [] and "www-authenticate" not in response.headers
    assert delays == [0]


def test_the_delay_schedule_of_the_default_untrusted_mode_is_0_0_0_2_3_3(rig, delays):
    for _ in range(6):
        assert rig.login_http("alice", "the wrong password").status_code == 401
    assert delays == [0, 0, 0, 2, 3, 3]


def test_the_same_schedule_holds_for_a_name_that_does_not_exist(rig, delays):
    for _ in range(6):
        assert rig.login_http("nobody", "the wrong password").status_code == 401
    assert delays == [0, 0, 0, 2, 3, 3]


def test_untrusted_a_correct_password_is_never_refused_whatever_the_failures(rig, delays):
    for _ in range(8):
        rig.login_http("alice", "the wrong password")
    before = len(delays)
    assert rig.login_http("alice", PASSWORD).status_code == 200
    assert len(delays) == before


def test_untrusted_one_names_failures_never_delay_another_name(rig, delays):
    rig.env.add_local_user("bob", PASSWORD)
    for _ in range(6):
        rig.login_http("alice", "the wrong password")
    delays.clear()
    rig.login_http("bob", "the wrong password")
    assert delays == [0]


def test_the_untrusted_delays_follow_a_configured_cap_and_base(make_rig, delays):
    rig = make_rig(delay_cap_untrusted_seconds=10, delay_base_seconds=2)
    for _ in range(8):
        rig.login_http("alice", "the wrong password")
    assert delays == [0, 0, 0, 2, 4, 8, 10, 10]


def test_the_delay_is_applied_on_the_event_loop_not_in_the_scrypt_thread(rig, delays):
    seen = []
    real = rig.env.svc.log_in
    rig.env.svc.log_in = lambda *args: seen.append(threading.get_ident()) or real(*args)
    rig.login_http("alice", "the wrong password")
    assert len(seen) == 1 and len(delays.threads) == 1 and seen[0] != delays.threads[0]


def test_the_delay_comes_after_the_check_not_before(rig, delays):
    order = []
    real = rig.env.svc.log_in
    rig.env.svc.log_in = lambda *args: order.append("check") or real(*args)

    async def record(seconds):
        order.append("delay")

    routes_apply = routes.apply_delay
    routes.apply_delay = record
    try:
        rig.login_http("alice", "the wrong password")
    finally:
        routes.apply_delay = routes_apply
    assert order == ["check", "delay"]


# --- busy and refusals -------------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["alice", "nobody"])
def test_a_full_hashing_gate_is_429_busy_with_retry_after_2_and_records_nothing(rig, delays, name):
    rig.env.svc._gate = FullGate(0, 1)
    response = rig.login_http(name, PASSWORD)
    assert response.status_code == 429 and response.json() == BUSY and response.headers["retry-after"] == "2"
    assert response.headers["cache-control"] == "no-store" and response.headers.get_list("set-cookie") == []
    assert delays == [] and failures_in_db(rig) == 0
    assert rig.env.sql("SELECT COUNT(*) FROM sessions") == [(0,)]


def test_trusted_four_failures_then_429_with_a_retry_after_that_counts_down(make_rig, delays):
    rig = make_rig(trust_proxy=True)
    for _ in range(4):
        assert rig.login_http("alice", "the wrong password").status_code == 401
    assert delays == [0, 0, 0, 0]  # trusted mode refuses instead of delaying
    response = rig.login_http("alice", "the wrong password")
    assert response.status_code == 429 and response.json() == TOO_MANY and response.headers["retry-after"] == "2"
    assert response.headers["cache-control"] == "no-store" and response.headers.get_list("set-cookie") == []
    rig.clock.advance(1)
    assert rig.login_http("alice", "the wrong password").headers["retry-after"] == "1"
    assert len(delays) == 4  # a refusal is never delayed


def test_trusted_during_a_refusal_even_the_right_password_is_refused_and_after_it_the_login_works(make_rig, delays):
    rig = make_rig(trust_proxy=True)
    for _ in range(4):
        rig.login_http("alice", "the wrong password")
    assert rig.login_http("alice", PASSWORD).status_code == 429
    rig.clock.advance(2)
    assert rig.login_http("alice", PASSWORD).status_code == 200
    assert rig.login_http("alice", PASSWORD).status_code == 200  # the success cleared the failures
    assert len(delays) == 4


def test_trusted_the_refusal_schedule_grows_and_retry_after_is_never_below_1(make_rig):
    rig = make_rig(trust_proxy=True)
    waits = []
    for _ in range(15):
        response = rig.login_http("alice", "the wrong password")
        if response.status_code == 429:
            waits.append(int(response.headers["retry-after"]))
            rig.clock.advance(waits[-1])  # wait it out, then the next attempt is checked again and fails again
    assert waits == [2, 4, 8, 16, 32, 60]  # base * 2 ** (failures - free - 1), capped at 60


def test_trusted_one_names_refusal_does_not_touch_another_name(make_rig):
    rig = make_rig(trust_proxy=True, users=("alice", "bob"))
    for _ in range(5):
        rig.login_http("alice", "the wrong password")
    assert rig.login_http("bob", PASSWORD).status_code == 200


# --- the client address ---------------------------------------------------------------------------------------------------------------------------

def key_exists(rig, name, ip):
    return rig.env.store.get_failure(rig.env.store.failure_key(name, ip)) is not None


def xff(value):
    return [("X-Forwarded-For", value)]


def test_trusted_the_rightmost_x_forwarded_for_entry_is_the_address(make_rig):
    rig = make_rig(trust_proxy=True)
    rig.login_http("alice", "the wrong password", headers=xff("9.9.9.9, 1.2.3.4"))
    assert key_exists(rig, "alice", "1.2.3.4") and not key_exists(rig, "alice", "9.9.9.9") and not key_exists(rig, "alice", "testclient")


def test_trusted_a_rotating_leftmost_entry_does_not_dodge_the_refusal(make_rig):
    rig = make_rig(trust_proxy=True)
    statuses = [rig.login_http("alice", "the wrong password", headers=xff(f"10.0.0.{i}, 1.2.3.4")).status_code for i in range(5)]
    assert statuses == [401, 401, 401, 401, 429]


def test_trusted_another_rightmost_address_has_its_own_count(make_rig):
    rig = make_rig(trust_proxy=True)
    for _ in range(4):
        rig.login_http("alice", "the wrong password", headers=xff("1.2.3.4"))
    assert rig.login_http("alice", "the wrong password", headers=xff("1.2.3.4")).status_code == 429
    assert rig.login_http("alice", PASSWORD, headers=xff("5.6.7.8")).status_code == 200


@pytest.mark.parametrize("value", ["evil", "", "1.2.3.4:80", "[::1]", "9.9.9.9, evil", "1.2.3", "fe80::1%eth0"])
def test_trusted_an_invalid_rightmost_entry_falls_back_to_the_socket_peer(make_rig, value):
    rig = make_rig(trust_proxy=True)
    rig.login_http("alice", "the wrong password", headers=xff(value))
    assert key_exists(rig, "alice", "testclient")


@pytest.mark.parametrize("trusted", [False, True])
def test_the_route_hands_the_trust_setting_and_the_socket_peer_to_client_ip_whatever_the_header_says(make_rig, monkeypatch, trusted):
    rig = make_rig(trust_proxy=trusted)
    seen = []
    real = routes.runtime.client_ip
    monkeypatch.setattr(routes.runtime, "client_ip", lambda peer, headers, trust: seen.append((peer, trust)) or real(peer, headers, trust))
    rig.login_http("alice", "the wrong password", headers=xff("1.2.3.4"))
    assert len(seen) == 1 and seen[0][1] is trusted and seen[0][0][0] == "testclient"


def test_trusted_without_the_header_the_peer_is_used(make_rig):
    rig = make_rig(trust_proxy=True)
    rig.login_http("alice", "the wrong password")
    assert key_exists(rig, "alice", "testclient")


def test_trusted_an_ipv6_address_is_written_canonically(make_rig):
    rig = make_rig(trust_proxy=True)
    rig.login_http("alice", "the wrong password", headers=xff("2001:DB8:0:0:0:0:0:1"))
    assert key_exists(rig, "alice", "2001:db8::1")


def test_untrusted_the_header_has_no_effect_at_all(rig, delays):
    for i in range(4):
        rig.login_http("alice", "the wrong password", headers=xff(f"10.0.0.{i}, 1.2.3.{i}"))
    assert delays == [0, 0, 0, 2]  # one count for the name, whatever the header says
    assert key_exists(rig, "alice", "") and not key_exists(rig, "alice", "1.2.3.0") and not key_exists(rig, "alice", "testclient")


# --- the body --------------------------------------------------------------------------------------------------------------------------------------------

BAD_BODIES = {
    "empty": b"", "not_json": b"not json", "list": b"[]", "string": b'"x"', "null": b"null", "number": b"123", "empty_object": b"{}",
    "no_password": b'{"username":"alice"}', "no_username": b'{"password":"x"}',
    "extra_key": b'{"username":"alice","password":"p","extra":"x"}', "invite_code_on_login": b'{"username":"alice","password":"p","invite_code":"x"}',
    "username_int": b'{"username":5,"password":"p"}', "password_int": b'{"username":"alice","password":5}', "password_null": b'{"username":"alice","password":null}',
    "password_list": b'{"username":"alice","password":["p"]}', "password_object": b'{"username":"alice","password":{"a":"p"}}', "password_bool": b'{"username":"alice","password":true}',
    "username_too_long": b'{"username":"' + b"a" * 257 + b'","password":"p"}', "password_too_long": b'{"username":"alice","password":"' + b"a" * 2049 + b'"}',
    "invalid_utf8": b'{"username":"\xff","password":"p"}', "nan": b'{"username":NaN,"password":"x"}', "nested": b"[" * 5000 + b"]" * 5000,
    "bom": b'\xef\xbb\xbf{"username":"alice","password":"p"}', "trailing_garbage": b'{"username":"alice","password":"p"} x',
    "echo_marker": b'{"username":"alice","password":"' + MARKER.encode() + b'","extra":1}', "marker_wrong_type": b'{"username":5,"password":"' + MARKER.encode() + b'"}',
}


@pytest.mark.parametrize("kind", BAD_BODIES)
def test_a_body_that_is_not_exactly_the_expected_object_is_422_with_nothing_echoed(rig, delays, kind):
    response = rig.post_json("/auth/login", BAD_BODIES[kind])
    assert response.status_code == 422 and response.json() == INVALID
    assert response.content == b'{"error":"The request is not valid.","code":"invalid_request"}'
    assert MARKER not in response.text and "input" not in response.text and response.headers.get_list("set-cookie") == []
    assert response.headers["cache-control"] == "no-store" and delays == []


def test_the_longest_accepted_fields_still_reach_the_check(rig):
    assert rig.post_json("/auth/login", {"username": "a" * 256, "password": "p" * 2048}).status_code == 401


def test_pinned_a_lone_surrogate_in_the_password_is_an_ordinary_bad_login(rig):
    body = b'{"username":"alice","password":"' + BACKSLASH.encode() + b'ud800' + b'"}'
    response = rig.post_json("/auth/login", body)
    assert response.status_code == 401 and response.json() == BAD_LOGIN


def test_pinned_a_lone_surrogate_in_the_username_is_an_ordinary_bad_login(rig):
    body = b'{"username":"' + BACKSLASH.encode() + b'ud800","password":"' + PASSWORD.encode() + b'"}'
    response = rig.post_json("/auth/login", body)
    assert response.status_code == 401 and response.json() == BAD_LOGIN


# --- the method and the media type ------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["GET", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_a_wrong_method_on_login_is_405_with_allow_post(rig, method):
    response = rig.send(method, "/auth/login")
    assert response.status_code == 405 and response.headers["allow"] == "POST" and response.headers["cache-control"] == "no-store"
    if method != "HEAD":
        assert response.json() == {"error": "Method Not Allowed", "code": "method_not_allowed"}


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_a_wrong_method_on_me_is_405_with_allow_get(rig, method):
    response = rig.send(method, "/auth/me")
    assert response.status_code == 405 and response.headers["allow"] == "GET"


def test_a_body_that_is_not_json_is_415_before_the_route_sees_it(rig, delays):
    response = rig.send("POST", "/auth/login", content=b"username=alice&password=x", headers=[("Content-Type", "application/x-www-form-urlencoded")])
    assert response.status_code == 415 and delays == []


def test_the_size_cap_applies_through_the_router(rig):
    pad = lambda n: b'{"username":"alice","password":"' + b"a" * n + b'"}'  # noqa: E731
    exact = pad(16384 - len(pad(0)))
    assert len(exact) == 16384
    assert rig.post_json("/auth/login", exact).status_code == 422  # past the cap check, then too long a password for the route
    assert rig.post_json("/auth/login", pad(16384 - len(pad(0)) + 1)).status_code == 413


# --- no secret anywhere ----------------------------------------------------------------------------------------------------------------------------------

def test_nothing_is_logged_and_no_record_holds_a_secret(rig):
    records = []

    class Collect(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler, root, app_logger = Collect(), logging.getLogger(), logging.getLogger("ops_rag")  # app.main stops "ops_rag" from propagating to the root, so listen there too
    old = root.level
    root.addHandler(handler)
    app_logger.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        good = rig.login_http("alice", PASSWORD)
        rig.login_http("alice", MARKER)
        rig.login_http("nobody", MARKER)
        rig.post_json("/auth/login", BAD_BODIES["echo_marker"])
        rig.post_json("/auth/login", BAD_BODIES["marker_wrong_type"])
        rig.env.svc._gate = FullGate(0, 1)
        rig.login_http("alice", MARKER)
    finally:
        root.removeHandler(handler)
        app_logger.removeHandler(handler)
        root.setLevel(old)
    cookies = cookies_of(good)
    secrets = [PASSWORD, MARKER, cookies["ops_session"][0], cookies["ops_csrf"][0]]
    assert [r.name for r in records if r.name.startswith(("app", "ops_rag"))] == []
    for record in records:
        text = record.getMessage() + repr(record.args)
        assert not any(secret in text for secret in secrets)


def test_no_failure_answer_holds_the_password_or_the_name(rig):
    for response in (rig.login_http("alice", MARKER), rig.login_http("nobody", MARKER), rig.post_json("/auth/login", BAD_BODIES["marker_wrong_type"])):
        text = response.text + repr(response.headers.multi_items())
        assert MARKER not in text and "nobody" not in text and "alice" not in text
