"""app/auth/routes.py: POST /auth/signup at HTTP level (Phase 3, step D).

Same setup as test_http_login.py (the throwaway app with the real router and middleware, the real AuthService on a tmp_path database, the FakeClock). Expected
values come from the Phase 3 plan and the step D request: 201 {"username"} with no session and no cookie; 403 {"error":"Sign-up is closed.","code":"signup_closed"}
when the mode is closed (provisioned users force it); a rule failure is 422 {"error": the fixed RuleError text, "code": the rule code} and echoes nothing; every
rejection (missing, wrong, used or expired invite code, taken name) is the same 400 with the same text; busy is 429 + Retry-After 2; the body is validated by the
route (422 invalid_request). Tests named pinned_* record behaviour the plan leaves open.
"""
import logging

import pytest

from app.auth import passwords, routes
from app.auth.config import AuthSettings
from app.auth.passwords import MESSAGES, Busy, HashGate
from gate_support import PASSWORD, RouteRig, build_routes_app

CLOSED = {"error": "Sign-up is closed.", "code": "signup_closed"}
REJECTED = {"error": "Could not create the account. Check the invite code and try another name.", "code": "signup_rejected"}
INVALID = {"error": "The request is not valid.", "code": "invalid_request"}
BUSY = {"error": "The server is busy. Try again shortly.", "code": "busy"}
MARKER = "marker-password-marker"
CODE_MARKER = "invite-code-marker-0123456789"
UNICODE_PASSWORD = "caf" + chr(0xE9) + " au lait 2026"


@pytest.fixture(autouse=True)
def delays(monkeypatch):
    recorded = []

    async def record(seconds):
        recorded.append(seconds)

    monkeypatch.setattr(routes, "apply_delay", record)
    return recorded


@pytest.fixture
def make_rig(tmp_path, make_service, clock):
    made = []

    def make(*, base_url="http://testserver", users=(), **overrides):
        values = {"session_idle_seconds": 600, "session_absolute_seconds": 3600, "signup": "open"}
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
    def acquire(self):
        raise Busy("queue_full")


def signup(rig, username, password=PASSWORD, code=None, **kwargs):
    body = {"username": username, "password": password}
    if code is not None:
        body["invite_code"] = code
    return rig.post_json("/auth/signup", body, **kwargs)


def signature(response):
    return response.status_code, response.content, sorted(response.headers.multi_items())


def user_count(rig):
    return rig.env.sql("SELECT COUNT(*) FROM users")[0][0]


# --- created ---------------------------------------------------------------------------------------------------------------------------------------

def test_open_sign_up_is_201_with_the_username_only_and_creates_no_session_and_no_cookie(rig):
    response = signup(rig, "newuser")
    assert response.status_code == 201 and response.json() == {"username": "newuser"}
    assert response.content == b'{"username":"newuser"}'
    assert response.headers["cache-control"] == "no-store" and response.headers["content-type"] == "application/json"
    assert response.headers.get_list("set-cookie") == []
    assert rig.env.sql("SELECT COUNT(*) FROM sessions") == [(0,)] and rig.env.store.get_user("newuser") is not None


def test_the_new_account_can_log_in(rig):
    signup(rig, "newuser", UNICODE_PASSWORD)
    assert rig.login_http("newuser", UNICODE_PASSWORD).status_code == 200
    assert rig.login_http("newuser", PASSWORD).status_code == 401


def test_the_user_name_in_the_answer_is_the_normalised_one(rig):
    assert signup(rig, "  NewUser ").json() == {"username": "newuser"}


def test_pinned_open_mode_ignores_an_invite_code(rig):
    assert signup(rig, "newuser", code=CODE_MARKER).status_code == 201


def test_pinned_open_sign_up_has_no_throttle_yet(rig):
    assert [signup(rig, f"user{i:03d}").status_code for i in range(8)] == [201] * 8  # phase 4 adds the limits


# --- closed ----------------------------------------------------------------------------------------------------------------------------------------

def test_a_closed_sign_up_is_403_and_creates_nothing(make_rig):
    rig = make_rig(signup="closed")
    response = signup(rig, "newuser")
    assert response.status_code == 403 and response.json() == CLOSED
    assert response.content == b'{"error":"Sign-up is closed.","code":"signup_closed"}' and response.headers.get_list("set-cookie") == []
    assert user_count(rig) == 0


def test_pinned_closed_comes_before_the_rules(make_rig):
    rig = make_rig(signup="closed")
    assert signup(rig, "x", "short").json() == CLOSED


def test_provisioned_users_force_the_sign_up_closed_whatever_the_setting(make_rig):
    stored = passwords.hash_password(PASSWORD, AuthSettings(scrypt_n=2**10))
    for mode in ("open", "invite", "closed"):
        rig = make_rig(signup=mode, users_text=f"bob:{stored}")
        assert signup(rig, "newuser").json() == CLOSED
        assert rig.login_http("bob", PASSWORD).status_code == 200
        assert rig.env.store.get_user("newuser") is None


# --- invite mode ---------------------------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def invite_rig(make_rig):
    return make_rig(signup="invite", users=("taken",))


def test_a_good_invite_code_creates_the_account_once(invite_rig):
    code = invite_rig.env.svc.create_invite(uses=1)
    assert signup(invite_rig, "newuser", code=code).status_code == 201
    assert signup(invite_rig, "second", code=code).status_code == 400  # used up
    assert invite_rig.env.store.get_user("second") is None


def test_a_code_with_several_uses_is_used_that_many_times(invite_rig):
    code = invite_rig.env.svc.create_invite(uses=2)
    assert [signup(invite_rig, f"user{i:03d}", code=code).status_code for i in range(3)] == [201, 201, 400]


def test_pinned_the_code_is_stripped_of_surrounding_blanks(invite_rig):
    code = invite_rig.env.svc.create_invite()
    assert signup(invite_rig, "newuser", code=f"  {code}  ").status_code == 201


def test_a_code_works_until_the_second_it_expires_and_not_after(invite_rig):
    code = invite_rig.env.svc.create_invite(uses=5, expires_in=60)
    invite_rig.clock.advance(59)
    assert signup(invite_rig, "early", code=code).status_code == 201
    invite_rig.clock.advance(1)
    assert signup(invite_rig, "late", code=code).status_code == 400


def test_a_taken_name_is_rejected_and_the_code_stays_usable(invite_rig):
    code = invite_rig.env.svc.create_invite(uses=1)
    assert signup(invite_rig, "taken", code=code).status_code == 400
    assert signup(invite_rig, "free", code=code).status_code == 201


def test_every_rejection_reason_gives_the_identical_answer(invite_rig):
    good = invite_rig.env.svc.create_invite(uses=1)
    used = invite_rig.env.svc.create_invite(uses=1)
    signup(invite_rig, "firstuser", code=used)
    expired = invite_rig.env.svc.create_invite(expires_in=10)
    invite_rig.clock.advance(10)
    answers = {
        "no_code": signup(invite_rig, "newuser1"), "wrong_code": signup(invite_rig, "newuser2", code="not-a-code"), "empty_code": signup(invite_rig, "newuser3", code=""),
        "used_up": signup(invite_rig, "newuser4", code=used), "expired": signup(invite_rig, "newuser5", code=expired), "taken_name": signup(invite_rig, "taken", code=good),
        "taken_name_no_code": signup(invite_rig, "taken"), "long_code": signup(invite_rig, "newuser6", code="c" * 256),
    }
    assert {k: v.status_code for k, v in answers.items()} == {k: 400 for k in answers}
    assert len({repr(signature(v)) for v in answers.values()}) == 1
    only = next(iter(answers.values()))
    assert only.json() == REJECTED and only.content == b'{"error":"Could not create the account. Check the invite code and try another name.","code":"signup_rejected"}'
    assert only.headers.get_list("set-cookie") == []


def test_pinned_in_open_mode_a_taken_name_is_the_one_possible_rejection_so_names_can_be_told_apart(make_rig):
    rig = make_rig(users=("taken",))
    assert signup(rig, "taken").json() == REJECTED and signup(rig, "free").status_code == 201


def test_a_wrong_code_does_not_say_whether_the_name_is_free(invite_rig):
    assert signature(signup(invite_rig, "taken", code="wrong")) == signature(signup(invite_rig, "free", code="wrong"))


# --- the rules ------------------------------------------------------------------------------------------------------------------------------------------

RULE_CASES = [
    ("username_invalid", "ab", PASSWORD), ("username_invalid", "UPPER CASE", PASSWORD), ("username_invalid", "x" * 33, PASSWORD), ("username_invalid", "-leading", PASSWORD),
    ("username_invalid", "has space", PASSWORD), ("username_invalid", "", PASSWORD), ("username_invalid", "bad/name", PASSWORD),
    ("password_too_short", "gooduser", "a1" * 5 + "b"), ("password_too_short", "gooduser", ""), ("password_too_long", "gooduser", "a1" * 64 + "b"),
    ("password_nul", "gooduser", "abcdefgh" + chr(0) + "ijklmno"), ("password_repeated", "gooduser", "a" * 12), ("password_repeated", "gooduser", "z" * 100),
    ("password_equals_username", "samename12345", "samename12345"), ("password_invalid", "gooduser", "abcdefgh" + chr(0xD800) + "ijklmn"),
]


@pytest.mark.parametrize("code, username, password", RULE_CASES, ids=[f"{c[0]}-{i}" for i, c in enumerate(RULE_CASES)])
def test_a_rule_failure_is_422_with_the_fixed_text_and_the_code_and_echoes_nothing(rig, code, username, password):
    response = signup(rig, username, password)
    assert response.status_code == 422 and response.json() == {"error": MESSAGES[code], "code": code}
    assert response.headers["cache-control"] == "no-store" and response.headers.get_list("set-cookie") == []
    shown = response.text
    for echoed in (username, password):
        if len(echoed) > 3:
            assert echoed not in shown
    assert user_count(rig) == 0


@pytest.mark.parametrize("password", ["a1" * 5 + "bc", "a1" * 64, UNICODE_PASSWORD])
def test_the_edges_of_the_password_rule_pass(rig, password):
    assert len(password) in (12, 128, 17) and signup(rig, "gooduser", password).status_code == 201


@pytest.mark.parametrize("username", ["abc", "a" * 32, "a.b-c_d", "0abc"])
def test_the_edges_of_the_user_name_rule_pass(rig, username):
    assert signup(rig, username).status_code == 201


def test_every_message_in_the_table_is_reachable_and_unchanged():
    assert set(MESSAGES) == {c[0] for c in RULE_CASES}


# --- the body ---------------------------------------------------------------------------------------------------------------------------------------------

BAD_BODIES = {
    "empty": b"", "not_json": b"{", "list": b"[]", "no_password": b'{"username":"newuser"}', "no_username": b'{"password":"x"}', "extra_key": b'{"username":"newuser","password":"p","admin":"1"}',
    "invite_null": b'{"username":"newuser","password":"p","invite_code":null}', "invite_int": b'{"username":"newuser","password":"p","invite_code":5}',
    "invite_too_long": b'{"username":"newuser","password":"p","invite_code":"' + b"c" * 257 + b'"}', "username_int": b'{"username":5,"password":"p"}',
    "password_list": b'{"username":"newuser","password":["p"]}', "username_too_long": b'{"username":"' + b"a" * 257 + b'","password":"p"}',
    "password_too_long": b'{"username":"newuser","password":"' + b"a" * 2049 + b'"}', "invalid_utf8": b'{"username":"\xff","password":"p"}',
    "marker": b'{"username":"newuser","password":"' + MARKER.encode() + b'","invite_code":"' + CODE_MARKER.encode() + b'","extra":true}',
}


@pytest.mark.parametrize("kind", BAD_BODIES)
def test_a_body_that_is_not_exactly_the_expected_object_is_422_invalid_request_with_nothing_echoed(rig, kind):
    response = rig.post_json("/auth/signup", BAD_BODIES[kind])
    assert response.status_code == 422 and response.json() == INVALID
    assert MARKER not in response.text and CODE_MARKER not in response.text and "input" not in response.text
    assert user_count(rig) == 0


def test_the_longest_accepted_fields_reach_the_rules(rig):
    response = rig.post_json("/auth/signup", {"username": "a" * 256, "password": "p" * 2048, "invite_code": "c" * 256})
    assert response.status_code == 422 and response.json()["code"] == "username_invalid"


def test_the_size_cap_applies_through_the_router(rig):
    pad = lambda n: b'{"username":"newuser","password":"' + b"a" * n + b'"}'  # noqa: E731
    exact = pad(16384 - len(pad(0)))
    assert len(exact) == 16384 and rig.post_json("/auth/signup", exact).status_code == 422  # past the cap, then too long for the route
    assert rig.post_json("/auth/signup", pad(16384 - len(pad(0)) + 1)).status_code == 413


def test_a_body_that_is_not_json_is_415(rig):
    assert rig.send("POST", "/auth/signup", content=b"username=newuser", headers=[("Content-Type", "text/plain")]).status_code == 415
    assert rig.send("POST", "/auth/signup", content=b'{"username":"newuser","password":"x"}').status_code == 415
    assert user_count(rig) == 0


@pytest.mark.parametrize("method", ["GET", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
def test_a_wrong_method_on_signup_is_405_with_allow_post(rig, method):
    response = rig.send(method, "/auth/signup")
    assert response.status_code == 405 and response.headers["allow"] == "POST"


# --- busy ------------------------------------------------------------------------------------------------------------------------------------------------

def test_a_full_hashing_gate_is_429_busy_with_retry_after_2_and_creates_nothing(rig):
    rig.env.svc._gate = FullGate(0, 1)
    response = signup(rig, "newuser")
    assert response.status_code == 429 and response.json() == BUSY and response.headers["retry-after"] == "2" and response.headers["cache-control"] == "no-store"
    assert user_count(rig) == 0 and response.headers.get_list("set-cookie") == []


# --- no secret anywhere --------------------------------------------------------------------------------------------------------------------------------

def test_nothing_is_logged_and_no_answer_or_record_holds_the_password_or_the_code(invite_rig):
    records, answers = [], []

    class Collect(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler, root, app_logger = Collect(), logging.getLogger(), logging.getLogger("ops_rag")  # app.main stops "ops_rag" from propagating to the root, so listen there too
    old = root.level
    root.addHandler(handler)
    app_logger.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        code = invite_rig.env.svc.create_invite()
        answers += [signup(invite_rig, "newuser", MARKER, code=code), signup(invite_rig, "taken", MARKER, code=CODE_MARKER), signup(invite_rig, "short", "x", code=CODE_MARKER),
                    invite_rig.post_json("/auth/signup", BAD_BODIES["marker"])]
        invite_rig.env.svc._gate = FullGate(0, 1)
        answers.append(signup(invite_rig, "another", MARKER, code=CODE_MARKER))
    finally:
        root.removeHandler(handler)
        app_logger.removeHandler(handler)
        root.setLevel(old)
    assert [r.name for r in records if r.name.startswith(("app", "ops_rag"))] == []
    for secret in (MARKER, CODE_MARKER, code):
        assert not any(secret in a.text + repr(a.headers.multi_items()) for a in answers)
        assert not any(secret in r.getMessage() + repr(r.args) for r in records)
