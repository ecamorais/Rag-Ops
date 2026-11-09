"""app/auth/middleware.py: who gets through (Phase 3, step C). The class of each route, the session check, the cookie clearing, auth off, and the plumbing.

The app under test is the throwaway one of gate_support.py (routes of the same kinds as the real app, a real StaticFiles mount, the real AuthService on a
database under tmp_path with the FakeClock); the real-app route inventory and the security headers on every response belong to the main.py step. Expected
values come from the Phase 3 plan and the step C request: 401 {"error":"Please sign in.","code":"unauthenticated"} with no WWW-Authenticate; a dead session
cookie is cleared in the 401; a route matched but not in the table is a session route; auth off changes nothing. Tests named pinned_* record behaviour the plan
leaves open.
"""
import logging
import sqlite3
import threading
from contextlib import asynccontextmanager

import pytest

import app.auth.middleware as middleware_module
from app.auth.middleware import AuthMiddleware
from app.auth.runtime import build_set_cookie
from app.auth.store import Store
from fastapi.testclient import TestClient
from gate_support import HIDDEN, PATHS, POLICY, SAME, SESSION_ROUTES, UNSAFE, Rig, build_app

ANON_BODY = {"error": "Please sign in.", "code": "unauthenticated"}
NOT_FOUND = {"error": "Not Found", "code": "not_found"}
GET_PATHS = ["/health", "/auth/me", "/filters", "/tickets/OPS-1001", "/unlisted"]


@pytest.fixture
def make_rig(tmp_path, make_service, clock):
    made = []

    def make(**overrides):
        env = make_service(**overrides)
        made.append(env)
        return Rig(build_app(tmp_path / f"static{len(made)}"), env, clock)

    return make


@pytest.fixture
def rig(make_rig):
    return make_rig(session_idle_seconds=600, session_absolute_seconds=3600)


def signature(response):
    """Everything a client can see of a response."""
    return response.status_code, response.content, sorted(response.headers.multi_items())


def clearing(settings, https=False):
    return sorted(build_set_cookie(name, "", settings, https, clear=True) for name in ("ops_session", "ops_csrf"))


# --- who gets through: the class of each route --------------------------------------------------------------------------------------------------------

def test_the_table_of_the_throwaway_app_covers_every_kind_of_route():
    assert set(POLICY.values()) == {"public", "session", "optional"}
    assert set(PATHS.values()) == {"public", "session", "optional"}


@pytest.mark.parametrize("path", [p for p in GET_PATHS if PATHS[p] == "session"])
def test_anonymous_gets_401_on_every_session_route_and_the_handler_never_runs(rig, path):
    response = rig.send("GET", path)
    assert response.status_code == 401 and response.json() == ANON_BODY
    assert rig.calls == []


@pytest.mark.parametrize("path", [p for p, k in PATHS.items() if k == "session" and p not in GET_PATHS])
def test_anonymous_gets_401_on_every_session_post_route(rig, path):
    response = rig.send("POST", path)
    assert response.status_code == 401 and response.json() == ANON_BODY
    assert rig.calls == []


@pytest.mark.parametrize("path", ["/health", "/auth/me"])
def test_an_optional_route_lets_anonymous_through_without_a_user(rig, path):
    response = rig.send("GET", path)
    assert response.status_code == 200 and response.json()["user"] is None and "set-cookie" not in response.headers


@pytest.mark.parametrize("path", ["/auth/login", "/auth/signup", "/auth/echo"])
def test_a_public_post_lets_anonymous_through_when_the_origin_is_fine(rig, path):
    response = rig.send("POST", path, content=b"{}", headers=[("Content-Type", "application/json")])
    assert response.status_code == 200 and rig.calls and rig.calls[-1][0] == path


@pytest.mark.parametrize("path, status", [("/", 200), ("/index.html", 200), ("/app.js", 200), ("/nope.css", 404), ("/a/b/c", 404)])
def test_the_static_mount_is_public(rig, path, status):
    assert rig.send("GET", path).status_code == status


def test_default_deny_a_route_that_is_not_in_the_table_is_a_session_route(rig):
    assert ("GET", "/unlisted") not in POLICY
    assert rig.send("GET", "/unlisted").status_code == 401 and rig.calls == []
    assert rig.send("GET", "/unlisted", rig.login()).status_code == 200


def test_pinned_with_no_route_and_no_mount_at_all_the_class_is_session():
    from starlette.applications import Starlette

    bare = Starlette()
    bare.state.auth = object()
    bare.add_middleware(AuthMiddleware, policy={})
    response = TestClient(bare, raise_server_exceptions=False).get("/anything")
    assert response.status_code == 401 and response.json() == ANON_BODY


@pytest.mark.parametrize("method", ["HEAD", "OPTIONS", "PUT", "PATCH", "DELETE", "POST"])
@pytest.mark.parametrize("path", ["/filters", "/tickets/OPS-1001", "/unlisted"])
def test_a_wrong_method_on_a_session_path_gets_the_class_of_the_path_not_the_static_mounts(rig, method, path):
    response = rig.send(method, path)
    assert response.status_code == 401 and rig.calls == []


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("path", ["/chat", "/search", "/auth/logout"])
def test_a_wrong_method_on_a_session_post_path_is_also_401_for_anonymous(rig, method, path):
    assert rig.send(method, path).status_code == 401 and rig.calls == []


@pytest.mark.parametrize("method, path, status", [("GET", "/auth/login", 404), ("PUT", "/auth/login", 405), ("DELETE", "/auth/signup", 405), ("OPTIONS", "/auth/me", 405),
                                                   ("PUT", "/health", 405), ("HEAD", "/auth/login", 404)])
def test_a_wrong_method_on_a_public_or_optional_path_is_not_a_401_the_static_mount_answers(rig, method, path, status):
    response = rig.send(method, path)
    assert response.status_code == status
    assert rig.calls == []


def test_pinned_head_and_options_need_the_session_on_a_session_route_but_no_csrf_token(rig):
    session = rig.login()
    for method in ("HEAD", "OPTIONS"):
        assert rig.send(method, "/filters").status_code == 401
        response = rig.send(method, "/filters", session, csrf="", origin=None)
        assert response.status_code not in (401, 403)


def test_a_route_with_a_different_class_per_method_is_looked_up_per_method(tmp_path):
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    async def ok(request):
        return PlainTextResponse("ok")

    bare = Starlette(routes=[Route("/x", ok, methods=["GET", "POST"])])
    bare.state.auth = type("S", (), {"validate_session": lambda self, t: None, "settings": replace_settings()})()
    bare.add_middleware(AuthMiddleware, policy={("GET", "/x"): "public", ("POST", "/x"): "session"})
    client = TestClient(bare, raise_server_exceptions=False)
    assert client.get("/x").status_code == 200
    assert client.post("/x").status_code == 401
    assert client.request("PUT", "/x").status_code == 401  # a method the table does not name: the strictest class of the path


def replace_settings():
    from app.auth.config import AuthSettings

    return AuthSettings()


def test_a_star_method_entry_covers_every_method_of_a_path():
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    async def ok(request):
        return PlainTextResponse("ok")

    bare = Starlette(routes=[Route("/x", ok, methods=["GET", "POST"])])
    bare.state.auth = type("S", (), {"validate_session": lambda self, t: None, "settings": replace_settings()})()
    bare.add_middleware(AuthMiddleware, policy={("*", "/x"): "public"})
    client = TestClient(bare, raise_server_exceptions=False)
    assert client.get("/x").status_code == 200 and client.post("/x", headers={"Origin": SAME}).status_code == 200


@pytest.mark.parametrize("policy", [{("GET", "/x"): "open"}, {("GET", "/x"): "Session"}, {("GET", "/x"): None}, {"GET /x": "session"}, {(1, "/x"): "session"},
                                    {("GET", 5): "session"}])
def test_a_bad_policy_entry_is_refused_when_the_middleware_is_built(policy):
    with pytest.raises((ValueError, TypeError)):
        AuthMiddleware(lambda *a: None, policy)


# --- the 401 -------------------------------------------------------------------------------------------------------------------------------------------------

def test_the_401_is_json_no_store_and_has_no_authenticate_header_and_no_cookie(rig):
    response = rig.send("GET", "/filters")
    assert response.status_code == 401 and response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-store"
    assert response.content == b'{"error":"Please sign in.","code":"unauthenticated"}'
    assert "www-authenticate" not in response.headers and response.headers.get_list("set-cookie") == []


# --- a dead cookie: the same 401, and both cookies cleared --------------------------------------------------------------------------------------------------

def make_dead(rig, kind):
    """The value of a Cookie header whose session is not alive, of the given kind. Sessions are made with the real service."""
    env, clock = rig.env, rig.clock
    if kind == "unknown":
        return "ops_session=" + "B" * 43
    if kind == "empty":
        return "ops_session="
    if kind in ("short42", "long44"):
        token = rig.login("carol").token
        return "ops_session=" + (token[:42] if kind == "short42" else token + "A")
    if kind == "non_ascii":
        return b"ops_session=" + b"\xe9" * 43
    if kind == "garbage":
        return "ops_session=%00%00 not a token; at all"
    if kind == "quoted":
        return 'ops_session="' + "C" * 43 + '"'
    session = rig.login(f"u-{kind}")
    if kind == "absolute":
        clock.advance(env.settings.session_absolute_seconds)
    elif kind == "idle":
        clock.advance(env.settings.session_idle_seconds)
    elif kind == "disabled":
        env.sql("UPDATE users SET disabled = 1 WHERE username = ?", ("u-disabled",))
    elif kind == "deleted":
        conn = sqlite3.connect(env.settings.db_path)  # foreign keys off: the session row stays behind
        conn.execute("DELETE FROM users WHERE username = 'u-deleted'")
        conn.commit()
        conn.close()
    return "ops_session=" + session.token


DEAD = ["unknown", "empty", "short42", "long44", "non_ascii", "garbage", "quoted", "absolute", "idle", "disabled", "deleted"]


@pytest.mark.parametrize("kind", DEAD)
def test_a_dead_cookie_gets_the_401_and_both_cookies_are_cleared(rig, kind):
    response = rig.send("GET", "/filters", cookie=make_dead(rig, kind))
    assert response.status_code == 401 and response.json() == ANON_BODY
    assert sorted(response.headers.get_list("set-cookie")) == clearing(rig.env.settings)
    assert rig.calls == []


def test_enumeration_dead_unknown_and_malformed_cookies_give_identical_responses(rig):
    seen = {kind: signature(rig.send("GET", "/filters", cookie=make_dead(rig, kind))) for kind in DEAD}
    assert len(set(map(repr, seen.values()))) == 1, {k: v[0] for k, v in seen.items()}


def test_the_same_holds_for_an_unsafe_method_even_with_a_matching_looking_csrf_header(rig):
    first = signature(rig.send("POST", "/chat", cookie=make_dead(rig, "unknown"), csrf="x" * 43))
    second = signature(rig.send("POST", "/chat", cookie=make_dead(rig, "absolute"), csrf="y" * 43))
    assert first == second and first[0] == 401


@pytest.mark.parametrize("mode, https, secure", [("auto", False, False), ("always", False, True)])
def test_the_cleared_cookies_follow_the_secure_mode(make_rig, mode, https, secure):
    rig = make_rig(cookie_secure=mode)
    cookies = rig.send("GET", "/filters", cookie="ops_session=" + "B" * 43).headers.get_list("set-cookie")
    assert len(cookies) == 2 and all(("Secure" in c) is secure and "Max-Age=0" in c for c in cookies)
    assert any(c.startswith("ops_session=;") and "HttpOnly" in c for c in cookies) and any(c.startswith("ops_csrf=;") and "HttpOnly" not in c for c in cookies)


def test_the_cleared_cookies_are_secure_on_https_and_behind_a_trusted_proxy_that_says_so(make_rig):
    rig = make_rig(trust_proxy=True)
    forwarded = rig.send("GET", "/filters", cookie="ops_session=" + "B" * 43, headers=[("X-Forwarded-Proto", "https")]).headers.get_list("set-cookie")
    assert all("Secure" in c for c in forwarded)
    plain = rig.send("GET", "/filters", cookie="ops_session=" + "B" * 43).headers.get_list("set-cookie")
    assert not any("Secure" in c for c in plain)
    untrusted = make_rig(trust_proxy=False).send("GET", "/filters", cookie="ops_session=" + "B" * 43, headers=[("X-Forwarded-Proto", "https")]).headers.get_list("set-cookie")
    assert not any("Secure" in c for c in untrusted)


def test_pinned_a_duplicated_session_cookie_counts_as_no_cookie_so_the_401_clears_nothing(rig):
    token = rig.login().token
    response = rig.send("GET", "/filters", cookie=f"ops_session={token}; ops_session={token}")
    assert response.status_code == 401 and response.headers.get_list("set-cookie") == []


def test_pinned_the_csrf_cookie_alone_is_not_a_session_and_clears_nothing(rig):
    session = rig.login()
    response = rig.send("GET", "/filters", cookie=f"ops_csrf={session.csrf}")
    assert response.status_code == 401 and response.headers.get_list("set-cookie") == []


# --- a live session -----------------------------------------------------------------------------------------------------------------------------------------

def test_a_live_session_reaches_the_handler_and_the_user_is_in_the_scope(rig):
    session = rig.login("alice")
    response = rig.send("GET", "/filters", session)
    assert response.status_code == 200 and response.json()["user"] == "alice"
    assert rig.send("GET", "/health", session).json()["user"] == "alice"
    assert rig.send("GET", "/auth/me", session).json()["user"] == "alice"
    assert rig.send("GET", "/tickets/OPS-1001", session).json()["user"] == "alice"


def test_each_session_reaches_the_handler_as_its_own_user(rig):
    alice, bob = rig.login("alice"), rig.login("bob")
    assert rig.send("GET", "/filters", alice).json()["user"] == "alice" and rig.send("GET", "/filters", bob).json()["user"] == "bob"


def test_no_session_is_left_in_the_scope_of_a_request_without_one(rig):
    assert rig.send("GET", "/health").json()["user"] is None


def test_an_optional_route_never_rejects_and_attaches_nothing_for_a_bad_cookie(rig):
    for kind in DEAD:
        response = rig.send("GET", "/health", cookie=make_dead(rig, kind))
        assert response.status_code == 200 and response.json()["user"] is None and response.headers.get_list("set-cookie") == []


def test_a_public_route_does_not_look_at_the_cookie_at_all(rig, monkeypatch):
    seen = []
    monkeypatch.setattr(middleware_module, "parse_cookies", lambda lines: seen.append(lines) or {})
    rig.send("POST", "/auth/echo", cookie="ops_session=" + "B" * 43, content=b"{}", headers=[("Content-Type", "application/json")])
    assert seen == []


def test_the_cookie_may_stand_among_others_and_the_header_name_in_any_case(rig):
    token = rig.login().token
    assert rig.send("GET", "/filters", cookie=f"a=1; ops_session={token}; b=2").status_code == 200
    for name in ("cookie", "COOKIE", "Cookie"):
        assert rig.send("GET", "/filters", cookie="", headers=[(name, f"ops_session={token}")]).status_code == 200
    assert rig.send("GET", "/filters", cookie=f"OPS_SESSION={token}").status_code == 401  # names are case-sensitive


def test_the_cookie_may_come_in_several_header_lines(rig):
    token = rig.login().token
    assert rig.send("GET", "/filters", cookie="", headers=[("Cookie", "a=1"), ("Cookie", f"ops_session={token}")]).status_code == 200


def test_a_token_of_exactly_43_characters_works_and_42_and_44_do_not(rig):
    token = rig.login().token
    assert len(token) == 43
    assert rig.send("GET", "/filters", cookie=f"ops_session={token}").status_code == 200
    assert rig.send("GET", "/filters", cookie=f"ops_session={token[:42]}").status_code == 401
    assert rig.send("GET", "/filters", cookie=f"ops_session={token}A").status_code == 401


# --- exact seconds, through the middleware -------------------------------------------------------------------------------------------------------------------

def test_the_idle_end_is_exact_599_seconds_alive_600_dead_and_the_row_is_deleted(rig):
    alive, doomed = rig.login("alice"), rig.login("bob")
    rig.clock.advance(599)
    assert rig.send("GET", "/filters", alive).status_code == 200
    rig.clock.advance(1)  # alice was touched at +599, so bob is the one idle for 600 seconds
    response = rig.send("GET", "/filters", doomed)
    assert response.status_code == 401 and sorted(response.headers.get_list("set-cookie")) == clearing(rig.env.settings)
    assert rig.env.sql("SELECT COUNT(*) FROM sessions WHERE csrf_token = ?", (doomed.csrf,)) == [(0,)]
    assert rig.send("GET", "/filters", alive).status_code == 200


def test_the_absolute_end_is_exact_3599_alive_3600_dead_when_idle_is_not_the_limit(make_rig):
    rig = make_rig(session_idle_seconds=3600, session_absolute_seconds=3600)
    session = rig.login()
    rig.clock.advance(3400)
    assert rig.send("GET", "/filters", session).status_code == 200  # touched: the idle end moves to +7000, only the absolute end is left
    rig.clock.advance(199)
    assert rig.send("GET", "/filters", session).status_code == 200  # +3599
    rig.clock.advance(1)
    assert rig.send("GET", "/filters", session).status_code == 401  # +3600


# --- auth off and a swapped service -------------------------------------------------------------------------------------------------------------------------

def comparable(response):
    """status, body, headers: the same request must give the same of these in two apps."""
    return signature(response)


OFF_REQUESTS = [
    ("GET", "/filters", []), ("GET", "/health", []), ("GET", "/auth/me", []), ("GET", "/", []), ("GET", "/nope", []), ("GET", "/unlisted", []),
    ("GET", "/tickets/OPS-1001", [("Cookie", "ops_session=" + "B" * 43)]), ("GET", "/filters", [("Cookie", "ops_session=junk; ops_session=junk")]),
    ("POST", "/chat", []), ("POST", "/chat", [("Origin", "http://evil.test")]), ("POST", "/chat", [("Origin", "null"), ("Sec-Fetch-Site", "cross-site")]),
    ("POST", "/chat", [("X-CSRF-Token", "x")]), ("POST", "/search", [("Cookie", "ops_session=" + "B" * 43), ("Origin", "http://evil.test")]),
    ("POST", "/auth/login", [("Origin", "http://evil.test")]), ("POST", "/auth/signup", [("Content-Type", "text/plain")]), ("PUT", "/filters", []),
    ("DELETE", "/chat", []), ("HEAD", "/filters", []), ("OPTIONS", "/chat", []), ("GET", "/docs", []), ("GET", "/openapi.json", []), ("POST", "/docs", []),
    ("GET", "/redoc", []), ("GET", "/docs/oauth2-redirect", []),
]


@pytest.mark.parametrize("how", ["no_attribute", "none"])
@pytest.mark.parametrize("method, path, headers", OFF_REQUESTS)
def test_auth_off_answers_exactly_like_the_bare_app(tmp_path, make_service, clock, how, method, path, headers):
    bare = TestClient(build_app(tmp_path / "static", middleware=False), raise_server_exceptions=False, follow_redirects=False)
    guarded_app = build_app(tmp_path / "static")
    if how == "none":
        guarded_app.state.auth = None
    guarded = TestClient(guarded_app, raise_server_exceptions=False, follow_redirects=False)
    expected, got = comparable(bare.request(method, path, headers=headers)), comparable(guarded.request(method, path, headers=headers))
    assert got == expected and "set-cookie" not in dict(got[2])


def test_auth_off_has_no_limits_on_the_auth_routes_and_no_media_type_rule(tmp_path):
    guarded = TestClient(build_app(tmp_path / "guarded"), raise_server_exceptions=False)
    response = guarded.post("/auth/echo", content=b"x" * 20000, headers={"Content-Type": "text/plain", "Origin": "http://evil.test"})
    assert response.status_code == 200 and response.json() == {"length": 20000}


def test_auth_off_does_no_work_no_cookie_parsing_no_database_no_service_call(tmp_path, make_service, monkeypatch):
    env = make_service()  # a real service exists, but it is not on the app
    touched = []
    monkeypatch.setattr(Store, "_connect", lambda self: touched.append("connect") or (_ for _ in ()).throw(AssertionError("database touched")))
    for name in ("parse_cookies", "origin_ok", "header_values", "build_set_cookie", "is_https"):
        monkeypatch.setattr(middleware_module, name, lambda *a, _n=name, **k: touched.append(_n) or (_ for _ in ()).throw(AssertionError(_n)))
    app = build_app(tmp_path / "guarded")
    client = TestClient(app, raise_server_exceptions=False)
    for method, path, headers in OFF_REQUESTS:
        client.request(method, path, headers=headers)
    assert touched == [] and env.svc is not None


def test_the_service_is_read_at_request_time_so_a_swap_is_honoured(rig, make_service, tmp_path):
    session = rig.login()
    assert rig.send("GET", "/filters", session).status_code == 200
    other = make_service()  # another service on another database: it has never heard of this session
    rig.app.state.auth = other.svc
    assert rig.send("GET", "/filters", session).status_code == 401
    rig.app.state.auth = None  # switched off at run time: straight through
    assert rig.send("GET", "/filters").status_code == 200 and rig.send("POST", "/chat", origin="http://evil.test").status_code == 200
    rig.app.state.auth = rig.env.svc  # and back on
    assert rig.send("GET", "/filters", session).status_code == 200 and rig.send("GET", "/filters").status_code == 401


def test_deleting_the_attribute_switches_auth_off_too(rig):
    del rig.app.state.auth
    assert rig.send("GET", "/filters").status_code == 200


# --- websocket, lifespan ------------------------------------------------------------------------------------------------------------------------------------

def test_a_websocket_is_closed_with_1008_when_auth_is_on(rig):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as caught:
        with rig.client.websocket_connect("/ws"):
            pass
    assert caught.value.code == 1008


def test_a_websocket_passes_when_auth_is_off(rig):
    rig.app.state.auth = None
    with rig.client.websocket_connect("/ws") as socket:
        assert socket.receive_text() == "hello"


def test_the_lifespan_passes_through_with_auth_on(tmp_path, make_service):
    env = make_service()
    ran = []

    @asynccontextmanager
    async def lifespan(app):
        ran.append("startup")
        yield
        ran.append("shutdown")

    app = build_app(tmp_path / "static", lifespan=lifespan)
    app.state.auth = env.svc
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/filters").status_code == 401
    assert ran == ["startup", "shutdown"]


# --- the session lookup is off the event loop ---------------------------------------------------------------------------------------------------------------

def test_the_session_lookup_runs_in_the_threadpool_never_on_the_event_loop(rig):
    session = rig.login()
    lookups = []
    real = rig.env.svc.validate_session
    rig.env.svc.validate_session = lambda token: lookups.append(threading.get_ident()) or real(token)
    response = rig.send("GET", "/filters?thread=1", session)  # an async handler: it runs ON the event loop and reports that thread
    loop_thread = response.json()["thread"]
    assert len(lookups) == 1 and lookups[0] != loop_thread
    rig.send("GET", "/health?thread=1", session)
    assert len(lookups) == 2 and all(t != loop_thread for t in lookups)
    rig.send("GET", "/health")  # no cookie: no lookup at all
    rig.send("POST", "/auth/echo", content=b"{}", headers=[("Content-Type", "application/json")])  # public: none either
    assert len(lookups) == 2


def test_a_store_failure_is_a_500_and_the_handler_does_not_run(rig):
    session = rig.login()
    rig.env.settings.db_path.unlink()
    response = rig.send("GET", "/filters", session)
    assert response.status_code == 500 and rig.calls == []


# --- the paths that are hidden while auth is on --------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", HIDDEN)
def test_the_api_docs_answer_like_an_unknown_path_while_auth_is_on_even_for_a_signed_in_user(rig, path):
    unknown = rig.send("GET", "/no-such-page")
    assert signature(rig.send("GET", path)) == signature(unknown) and unknown.status_code == 404 and unknown.json() == NOT_FOUND
    assert signature(rig.send("GET", path, rig.login())) == signature(unknown)
    assert signature(rig.send("HEAD", path)) == signature(rig.send("HEAD", "/no-such-page"))


@pytest.mark.parametrize("path", HIDDEN)
def test_the_docs_post_answer_is_the_405_of_an_unknown_path(rig, path):
    assert signature(rig.send("POST", path)) == signature(rig.send("POST", "/no-such-page"))
    assert rig.send("POST", path).status_code == 405


@pytest.mark.parametrize("path", ["/docs", "/openapi.json", "/redoc"])
def test_the_api_docs_work_when_auth_is_off(rig, path):
    rig.app.state.auth = None
    assert rig.send("GET", path).status_code == 200


# --- nothing is logged, nothing leaks ----------------------------------------------------------------------------------------------------------------------------

def test_nothing_is_logged_by_the_middleware_whatever_happens(rig):
    records = []

    class Collect(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler, root = Collect(), logging.getLogger()
    old = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        session = rig.login()
        for kind in DEAD:
            rig.send("GET", "/filters", cookie=make_dead(rig, kind))
        rig.send("GET", "/filters", session)
        rig.send("POST", "/chat", session, csrf="wrong")
        rig.send("POST", "/chat", session, origin="http://evil.test")
        rig.send("POST", "/auth/echo", content=b"x" * 20000, headers=[("Content-Type", "application/json")])
        rig.send("POST", "/auth/echo", content=b"x", headers=[("Content-Type", "text/plain")])
    finally:
        root.removeHandler(handler)
        root.setLevel(old)
    assert [r.name for r in records if r.name.startswith(("app", "ops_rag"))] == []


def test_a_refusal_never_holds_a_token_a_csrf_value_a_user_name_or_an_address(rig):
    session = rig.login("zelda")
    answers = [
        rig.send("POST", "/chat", session, csrf="wrong-" + session.csrf[:10]), rig.send("POST", "/chat", session, origin="http://evil.test"),
        rig.send("POST", "/auth/echo", content=b"x" * 20000, headers=[("Content-Type", "application/json")]),
        rig.send("POST", "/auth/echo", content=b"x", headers=[("Content-Type", "text/plain")]),
        rig.send("GET", "/filters", cookie=make_dead(rig, "absolute")),  # last: making it moves the clock past the live session
    ]
    assert [a.status_code for a in answers] == [403, 403, 413, 415, 401]
    for answer in answers:
        text = answer.text + repr(answer.headers.multi_items())
        for secret in (session.token, session.csrf, "zelda", "testclient", "127.0.0.1", "wrong-"):
            assert secret not in text
        assert answer.headers["cache-control"] == "no-store" and answer.headers["content-type"] == "application/json" and "www-authenticate" not in answer.headers
