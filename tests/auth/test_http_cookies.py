"""app/auth/routes.py: the cookies, logout, logout-all and GET /auth/me at HTTP level (Phase 3, step D).

Same setup as test_http_login.py. The Set-Cookie headers are read raw from the response as separate lines. Expected values come from the Phase 3 plan and the step D
request: the session cookie is HttpOnly and the CSRF cookie is not; both are SameSite=Lax, Path=/, Max-Age the ABSOLUTE session limit, Secure for "always" or on
https (or behind a trusted proxy that says https); logout and logout-all answer 200, clear BOTH cookies with the same attributes and Max-Age=0, and end the
sessions; /auth/me is {"authenticated":true,"username","csrf_token"} or {"authenticated":false,"signup":mode}. Tests named pinned_* record behaviour the plan leaves open.
"""
import pytest

from app.auth import passwords, routes
from app.auth.config import AuthSettings
from gate_support import PASSWORD, RouteRig, build_routes_app, cookies_of, parse_set_cookie

ANON = {"error": "Please sign in.", "code": "unauthenticated"}
REFUSED = {"error": "The request was refused.", "code": "csrf_failed"}
ABSOLUTE, IDLE = 7200, 600  # different on purpose: Max-Age must be the first


@pytest.fixture(autouse=True)
def no_real_delay(monkeypatch):
    async def record(seconds):
        return None

    monkeypatch.setattr(routes, "apply_delay", record)


@pytest.fixture
def make_rig(tmp_path, make_service, clock):
    made = []

    def make(*, base_url="http://testserver", users=("alice",), **overrides):
        values = {"session_idle_seconds": IDLE, "session_absolute_seconds": ABSOLUTE}
        values.update(overrides)
        env = make_service(**values)
        for name in users:
            env.add_local_user(name, PASSWORD)
        made.append(env)
        return RouteRig(build_routes_app(tmp_path / f"static{len(made)}"), env, clock, base_url=base_url)

    return make


@pytest.fixture
def rig(make_rig):
    return make_rig(users=("alice", "bob"))


def login(rig, name="alice", **kwargs):
    """Log in over HTTP: (response, session token, csrf token)."""
    response = rig.login_http(name, PASSWORD, **kwargs)
    assert response.status_code == 200
    cookies = cookies_of(response)
    return response, cookies["ops_session"][0], cookies["ops_csrf"][0]


def logout(rig, token, csrf, path="/auth/logout", **kwargs):
    return rig.send("POST", path, token=token, csrf=csrf, **kwargs)


def alive(rig, token):
    return rig.send("GET", "/filters", token=token).status_code == 200


# --- the flags --------------------------------------------------------------------------------------------------------------------------------------

def test_the_login_cookies_are_two_separate_headers_with_exactly_the_approved_attributes(rig):
    response, token, csrf = login(rig)
    lines = response.headers.get_list("set-cookie")
    assert len(lines) == 2 and all(line.count("ops_") == 1 for line in lines)
    session, csrf_cookie = (parse_set_cookie(line) for line in lines)
    assert session[0] == "ops_session" and csrf_cookie[0] == "ops_csrf"
    assert session[2] == {"Path": "/", "Max-Age": str(ABSOLUTE), "HttpOnly": True, "SameSite": "Lax"}
    assert csrf_cookie[2] == {"Path": "/", "Max-Age": str(ABSOLUTE), "SameSite": "Lax"}


def test_max_age_is_the_absolute_limit_and_not_the_idle_limit(rig):
    cookies = cookies_of(login(rig)[0])
    assert all(attrs["Max-Age"] == str(ABSOLUTE) != str(IDLE) for _, attrs in cookies.values())


@pytest.mark.parametrize("absolute", [1, 2, 3600, 604800])
def test_max_age_follows_the_configured_absolute_limit(make_rig, absolute):
    rig = make_rig(session_idle_seconds=1, session_absolute_seconds=absolute)
    assert all(attrs["Max-Age"] == str(absolute) for _, attrs in cookies_of(login(rig)[0]).values())


def test_only_the_session_cookie_is_http_only(rig):
    cookies = cookies_of(login(rig)[0])
    assert cookies["ops_session"][1].get("HttpOnly") is True and "HttpOnly" not in cookies["ops_csrf"][1]


def test_no_cookie_has_a_domain_expires_or_partitioned_attribute(rig):
    for _, attrs in cookies_of(login(rig)[0]).values():
        assert not {k.lower() for k in attrs} & {"domain", "expires", "partitioned", "priority"}


def test_the_cookie_values_are_plain_tokens(rig):
    _, token, csrf = login(rig)
    assert len(token) == 43 and len(csrf) == 43 and token != csrf
    assert all(c.isalnum() or c in "_-" for c in token + csrf)


SECURE_CASES = [
    # (cookie_secure, base_url, trust_proxy, X-Forwarded-Proto, secure?)
    ("auto", "http://testserver", False, None, False), ("auto", "https://testserver", False, None, True),
    ("always", "http://testserver", False, None, True), ("always", "https://testserver", False, None, True),
    ("auto", "http://testserver", True, None, False), ("auto", "http://testserver", True, "https", True), ("auto", "http://testserver", True, "HTTPS", True),
    ("auto", "http://testserver", True, "http", False), ("auto", "http://testserver", True, "http, https", True), ("auto", "http://testserver", True, "https, http", False),
    ("auto", "http://testserver", True, "ftp", False),
    ("auto", "http://testserver", False, "https", False),  # the header counts only when the proxy is trusted
    ("auto", "https://testserver", True, "http", True),  # and it can only turn Secure on
    ("always", "http://testserver", False, "http", True),
]


@pytest.mark.parametrize("mode, base_url, trust, forwarded, secure", SECURE_CASES)
def test_secure_follows_the_mode_the_scheme_and_a_trusted_proxy_for_both_cookies_and_for_the_clearing_ones_too(make_rig, mode, base_url, trust, forwarded, secure):
    rig = make_rig(base_url=base_url, cookie_secure=mode, trust_proxy=trust)
    extra = [("X-Forwarded-Proto", forwarded)] if forwarded else []
    response, token, csrf = login(rig, headers=extra)
    assert all(("Secure" in attrs) is secure for _, attrs in cookies_of(response).values())
    cleared = logout(rig, token, csrf, headers=extra)
    assert cleared.status_code == 200 and all(("Secure" in attrs) is secure for _, attrs in cookies_of(cleared).values())


def test_a_failed_login_a_refusal_and_a_sign_up_set_no_cookie_in_any_mode(make_rig):
    rig = make_rig(cookie_secure="always", signup="open", trust_proxy=True)
    answers = [rig.login_http("alice", "the wrong password") for _ in range(5)] + [rig.post_json("/auth/signup", {"username": "newuser", "password": PASSWORD})]
    assert [a.status_code for a in answers] == [401, 401, 401, 401, 429, 201]
    assert all(a.headers.get_list("set-cookie") == [] for a in answers)


# --- log out ------------------------------------------------------------------------------------------------------------------------------------------

def test_logout_is_200_ok_clears_both_cookies_with_the_same_attributes_and_ends_the_session(rig):
    response, token, csrf = login(rig)
    live = cookies_of(response)
    cleared = logout(rig, token, csrf)
    assert cleared.status_code == 200 and cleared.json() == {"ok": True} and cleared.content == b'{"ok":true}'
    assert cleared.headers["cache-control"] == "no-store" and cleared.headers["content-type"] == "application/json"
    lines = cleared.headers.get_list("set-cookie")
    assert len(lines) == 2
    out = cookies_of(cleared)
    assert set(out) == {"ops_session", "ops_csrf"}
    for name in out:
        value, attrs = out[name]
        assert value == "" and attrs["Max-Age"] == "0"
        assert {k: v for k, v in attrs.items() if k != "Max-Age"} == {k: v for k, v in live[name][1].items() if k != "Max-Age"}
    assert not alive(rig, token) and rig.env.sql("SELECT COUNT(*) FROM sessions") == [(0,)]


def test_after_logout_the_csrf_token_is_useless_and_a_second_logout_is_401_with_the_cookies_cleared(rig):
    _, token, csrf = login(rig)
    logout(rig, token, csrf)
    again = logout(rig, token, csrf)
    assert again.status_code == 401 and again.json() == ANON
    assert all(attrs["Max-Age"] == "0" for _, attrs in cookies_of(again).values()) and len(again.headers.get_list("set-cookie")) == 2


def test_logout_ends_only_its_own_session(rig):
    _, first, first_csrf = login(rig)
    _, second, _ = login(rig)
    _, bobs, _ = login(rig, "bob")
    logout(rig, first, first_csrf)
    assert not alive(rig, first) and alive(rig, second) and alive(rig, bobs)


def test_logout_needs_a_session_and_the_token_and_a_good_origin(rig):
    _, token, csrf = login(rig)
    anonymous = rig.send("POST", "/auth/logout")
    assert anonymous.status_code == 401 and anonymous.headers.get_list("set-cookie") == []
    assert logout(rig, token, "wrong").status_code == 403 and logout(rig, token, "").status_code == 403
    assert logout(rig, token, csrf, origin="http://evil.test").status_code == 403
    assert logout(rig, token, csrf, origin=None, fetch="cross-site").status_code == 403
    assert alive(rig, token)  # nothing above ended it
    assert logout(rig, token, csrf).status_code == 200


def test_logout_without_a_body_or_a_content_type_works_and_with_a_body_needs_json(rig):
    _, token, csrf = login(rig)
    assert logout(rig, token, csrf, content=b"x", headers=[("Content-Type", "text/plain")]).status_code == 415
    assert alive(rig, token)
    assert logout(rig, token, csrf).status_code == 200


# --- log out everywhere -------------------------------------------------------------------------------------------------------------------------------

def test_logout_all_ends_every_session_of_the_user_and_says_how_many(rig):
    sessions = [login(rig) for _ in range(3)]
    _, bobs, _ = login(rig, "bob")
    token, csrf = sessions[1][1], sessions[1][2]
    response = logout(rig, token, csrf, "/auth/logout-all")
    assert response.status_code == 200 and response.json() == {"ok": True, "sessions_closed": 3} and response.content == b'{"ok":true,"sessions_closed":3}'
    assert response.headers["cache-control"] == "no-store"
    assert not any(alive(rig, s[1]) for s in sessions) and alive(rig, bobs)
    cookies = cookies_of(response)
    assert set(cookies) == {"ops_session", "ops_csrf"} and all(v == "" and a["Max-Age"] == "0" for v, a in cookies.values())
    assert len(response.headers.get_list("set-cookie")) == 2


def test_logout_all_with_one_session_closes_one(rig):
    _, token, csrf = login(rig)
    assert logout(rig, token, csrf, "/auth/logout-all").json()["sessions_closed"] == 1


def test_logout_all_needs_a_session_and_the_token(rig):
    _, token, csrf = login(rig)
    assert rig.send("POST", "/auth/logout-all").status_code == 401
    assert logout(rig, token, "wrong", "/auth/logout-all").status_code == 403
    assert alive(rig, token)


def test_logout_all_clearing_attributes_equal_the_login_ones_except_max_age(rig):
    response, token, csrf = login(rig)
    live = cookies_of(response)
    out = cookies_of(logout(rig, token, csrf, "/auth/logout-all"))
    for name in live:
        assert {k: v for k, v in out[name][1].items() if k != "Max-Age"} == {k: v for k, v in live[name][1].items() if k != "Max-Age"}


@pytest.mark.parametrize("path", ["/auth/logout", "/auth/logout-all"])
def test_the_route_itself_does_not_rely_on_the_middleware_alone(rig, path):
    # called without the middleware's session in the scope the route refuses
    from fastapi import FastAPI

    from app.auth.routes import router

    bare = FastAPI()
    bare.include_router(router)
    bare.state.auth = rig.env.svc
    from fastapi.testclient import TestClient

    _, token, csrf = login(rig)
    response = TestClient(bare, raise_server_exceptions=False).post(path, headers={"Cookie": f"ops_session={token}"})
    assert response.status_code == 401 and response.json() == ANON and alive(rig, token)


# --- who am I ---------------------------------------------------------------------------------------------------------------------------------------

def test_me_for_a_signed_in_user_has_exactly_three_keys_and_the_stored_token(rig):
    _, token, csrf = login(rig)
    response = rig.send("GET", "/auth/me", token=token)
    assert response.status_code == 200 and response.json() == {"authenticated": True, "username": "alice", "csrf_token": csrf}
    assert response.headers["cache-control"] == "no-store" and response.headers.get_list("set-cookie") == []
    assert rig.env.sql("SELECT csrf_token FROM sessions") == [(csrf,)]


@pytest.mark.parametrize("mode", ["closed", "invite", "open"])
def test_me_anonymous_has_exactly_two_keys_and_no_token_or_name(make_rig, mode):
    response = make_rig(signup=mode).send("GET", "/auth/me")
    assert response.status_code == 200 and response.json() == {"authenticated": False, "signup": mode}
    assert response.content == b'{"authenticated":false,"signup":"%s"}' % mode.encode()
    assert response.headers["cache-control"] == "no-store" and response.headers.get_list("set-cookie") == []


def test_me_anonymous_says_closed_when_users_are_provisioned(make_rig):
    stored = passwords.hash_password(PASSWORD, AuthSettings(scrypt_n=2**10))
    assert make_rig(signup="open", users_text=f"bob:{stored}").send("GET", "/auth/me").json() == {"authenticated": False, "signup": "closed"}


@pytest.mark.parametrize("cookie", ["ops_session=" + "B" * 43, "ops_session=garbage", "ops_session=", "ops_csrf=x", "ops_session=a; ops_session=a"])
def test_me_with_a_dead_or_odd_cookie_is_the_anonymous_answer_and_sets_nothing(rig, cookie):
    response = rig.send("GET", "/auth/me", cookie=cookie)
    assert response.status_code == 200 and response.json() == {"authenticated": False, "signup": "invite"}
    assert response.headers.get_list("set-cookie") == []


def test_me_after_logout_is_anonymous(rig):
    _, token, csrf = login(rig)
    logout(rig, token, csrf)
    assert rig.send("GET", "/auth/me", token=token).json()["authenticated"] is False


def test_me_shows_the_token_of_the_session_it_was_asked_with_not_another_users(rig):
    _, a_token, a_csrf = login(rig)
    _, b_token, b_csrf = login(rig, "bob")
    assert rig.send("GET", "/auth/me", token=a_token).json()["csrf_token"] == a_csrf
    assert rig.send("GET", "/auth/me", token=b_token).json() == {"authenticated": True, "username": "bob", "csrf_token": b_csrf}


def test_the_csrf_token_from_me_works_on_an_unsafe_request(rig):
    _, token, _ = login(rig)
    csrf = rig.send("GET", "/auth/me", token=token).json()["csrf_token"]
    assert rig.send("POST", "/chat", token=token, csrf=csrf).status_code == 200
