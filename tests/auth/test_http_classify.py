"""app/auth/middleware.py: how a request is classified, however the routes were added (the fix after step C).

FastAPI 0.162 keeps an included router as ONE opaque entry in app.router.routes (no path, no methods), so the first version of the middleware, which looked
routes up by their own path, saw every route of include_router as "not in the table" and made it a session route. The lookup is now by PATH against the table's
templates (starlette.routing.compile_path). These tests build the same routes five ways (decorators on the app, include_router plain, include_router with a prefix
given at include time, a nested router, a router with its own prefix) and check the same things each time. The safety invariant is tested as a grid: for every path
trick and every method, an ANONYMOUS request never runs a handler that is not public (the handlers record their calls). Expected values come from the approved
design (default-deny; only StaticFiles is public without an entry; a wrong method is never a way round). Tests named pinned_* record behaviour it leaves open.
"""
import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import PlainTextResponse
from starlette.routing import BaseRoute, Match, Route
from starlette.staticfiles import StaticFiles

from app.auth.middleware import AuthMiddleware
from gate_support import SAME, Rig, raw_call

KINDS = ["app", "included", "prefixed", "nested", "own_prefix"]
PREFIX = {"app": "", "included": "", "prefixed": "/api", "nested": "/outer/inner", "own_prefix": "/own"}
PROTECTED = {"x", "item", "unlisted", "dual_slash", "pub2x", "s_fixed", "n", "m_post", "casing_upper", "weird", "plain", "sub"}
METHODS = ("GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE")


def add_routes(target):
    """The routes, relative to `target` (the app, or a router the app includes). Each handler records its name in app.state.calls."""

    def handler(name):
        async def run(request: Request):
            request.app.state.calls.append(name)
            session = request.scope.get("auth_session")
            return {"user": session.username if session is not None else None}

        run.__name__ = f"handle_{name}"
        return run

    for method, path, name in (
        ("get", "/x", "x"), ("post", "/x", "x"), ("post", "/pub", "pub"), ("get", "/opt", "opt"), ("get", "/unlisted", "unlisted"), ("get", "/dual", "dual"),
        ("get", "/dual/", "dual_slash"), ("get", "/pub2", "pub2"), ("get", "/pub2x", "pub2x"), ("get", "/m", "m_get"), ("post", "/m", "m_post"), ("get", "/casing", "casing"), ("get", "/CASING", "casing_upper"),
        ("get", "/s/fixed", "s_fixed"), ("get", "/s/{a}", "s_any"),
    ):
        getattr(target, method)(path)(handler(name))
    target.get("/item/{item_id}")(handler("item"))
    target.get("/n/{n:int}")(handler("n"))


def policy_for(prefix):
    return {
        ("GET", prefix + "/x"): "session", ("POST", prefix + "/x"): "session", ("POST", prefix + "/pub"): "public", ("GET", prefix + "/opt"): "optional",
        ("GET", prefix + "/item/{item_id}"): "session", ("GET", prefix + "/dual"): "public", ("GET", prefix + "/pub2"): "public",
        ("*", prefix + "/m"): "session", ("GET", prefix + "/m"): "public",  # the exact method beats "*"
        ("GET", prefix + "/s/{a}"): "public", ("GET", prefix + "/s/fixed"): "session",  # two templates match /s/fixed: the strictest wins, not the first
        ("GET", prefix + "/n/{n:int}"): "session",
        ("GET", prefix + "/casing"): "public",  # routes are case-sensitive: /CASING is another route, unlisted
    }


def build(kind, static_dir, *, mount=True):
    app = FastAPI()
    app.state.calls = []

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request, error):
        codes = {404: "not_found", 405: "method_not_allowed"}
        return JSONResponse({"error": str(error.detail), "code": codes.get(error.status_code, "http_error")}, status_code=error.status_code, headers=error.headers)

    if kind == "app":
        add_routes(app)
    elif kind in ("included", "prefixed", "own_prefix"):
        router = APIRouter(prefix="/own" if kind == "own_prefix" else "")
        add_routes(router)
        app.include_router(router, prefix="/api" if kind == "prefixed" else "")
    else:
        inner, outer = APIRouter(), APIRouter()
        add_routes(inner)
        outer.include_router(inner, prefix="/inner")
        app.include_router(outer, prefix="/outer")
    if mount:
        static_dir.mkdir(exist_ok=True)
        (static_dir / "index.html").write_text("<p>hello</p>")
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    app.add_middleware(AuthMiddleware, policy=policy_for(PREFIX[kind]))
    return app


@pytest.fixture(params=KINDS)
def variant(request, tmp_path, make_service, clock):
    app = build(request.param, tmp_path / "static")
    return Rig(app, make_service(), clock), PREFIX[request.param]


# --- the same cases as the step C tests, whichever way the routes were added ----------------------------------------------------------------------------------------

CASES = [
    ("GET", "/x", 401), ("POST", "/x", 401), ("PUT", "/x", 401), ("PATCH", "/x", 401), ("DELETE", "/x", 401), ("HEAD", "/x", 401), ("OPTIONS", "/x", 401),
    ("GET", "/item/5", 401), ("PUT", "/item/5", 401),
    ("GET", "/unlisted", 401), ("POST", "/unlisted", 401),  # default-deny: a route that is not in the table is a session route
    ("POST", "/pub", 200), ("GET", "/opt", 200), ("GET", "/dual", 200),
    ("GET", "/dual/", 401),  # a different route from /dual that the table does not name: the public entry of /dual must not cover it
    ("GET", "/pub2", 200), ("GET", "/pub2x", 401),  # a template is matched whole, never as a prefix
    ("GET", "/casing", 200), ("GET", "/CASING", 401),  # the path is matched as it is: the case is not folded
    ("GET", "/m", 200), ("POST", "/m", 401), ("PUT", "/m", 401),  # the entry of the exact method beats the "*" entry
    ("GET", "/s/other", 200), ("GET", "/s/fixed", 401),  # two templates match: the strictest class wins
    ("GET", "/n/5", 401), ("GET", "/n/abc", 404),  # a converter: /n/abc is no route at all, the static mount answers
]


@pytest.mark.parametrize("method, suffix, status", CASES)
def test_anonymous_gets_the_class_of_the_route_however_it_was_added(variant, method, suffix, status):
    rig, prefix = variant
    response = rig.send(method, prefix + suffix)
    assert response.status_code == status
    if status == 401:
        assert not (set(rig.calls) & PROTECTED)


def test_the_static_files_stay_public(variant):
    rig, _ = variant
    assert rig.send("GET", "/index.html").status_code == 200 and rig.send("GET", "/").status_code == 200 and rig.send("GET", "/nope.css").status_code == 404


@pytest.mark.parametrize("method, suffix", [("GET", "/x"), ("POST", "/x"), ("GET", "/item/5"), ("GET", "/unlisted"), ("GET", "/dual/"), ("GET", "/pub2x"),
                                            ("POST", "/m"), ("GET", "/s/fixed"), ("GET", "/n/5")])
def test_a_signed_in_user_reaches_every_protected_route_with_the_token(variant, method, suffix):
    rig, prefix = variant
    session = rig.login()
    assert rig.send(method, prefix + suffix, session).status_code == 200 and rig.calls and rig.calls[-1] in PROTECTED
    if method != "GET":
        rig.calls.clear()
        assert rig.send(method, prefix + suffix, session, csrf="wrong").status_code == 403 and rig.calls == []


# --- the safety invariant: no path trick makes an anonymous request run a protected handler -----------------------------------------------------------------------

def tricks(path):
    head, _, last = path.rstrip("/").rpartition("/")
    first = last[0]
    found = [
        path, path + "/", "/" + path, head + "/%" + format(ord(first), "02x") + last[1:], head + "/%" + format(ord(first), "02X") + last[1:],
        head + "/./" + last, head + "/a/../" + last, head + "/" + last.upper(), head + "/" + last.swapcase(), path + ";param", path + "?query=1", path + chr(0xe9),
        head + "//" + last, path + "%00", path + ".", path + " ", head + "%2F" + last, "/" + path.strip("/").replace("/", "//"), path.replace("/", chr(92)),
        path + "/" + "/", "/" + path + "/..", path + "/.", path.replace("/", "/./"),
    ]
    return list(dict.fromkeys(found))


@pytest.mark.parametrize("suffix", ["/x", "/item/5", "/unlisted", "/dual/", "/pub2x", "/s/fixed", "/n/5", "/CASING"])
def test_no_path_trick_lets_an_anonymous_request_run_a_protected_handler(variant, suffix):
    rig, prefix = variant
    statuses = set()
    for path in tricks(prefix + suffix):
        for method in METHODS:
            try:
                statuses.add(raw_call(rig.app, method, path, [("Origin", SAME)]).status)
            except Exception as error:  # the static mount refusing an odd file name (Windows) is not a way past the gate; it is reported, not hidden
                statuses.add(type(error).__name__)
    assert not (set(rig.calls) & PROTECTED), set(rig.calls) & PROTECTED
    assert 500 not in statuses


@pytest.mark.parametrize("suffix", ["/x", "/unlisted", "/dual/"])
def test_the_same_tricks_through_the_test_client_never_run_a_protected_handler(variant, suffix):
    rig, prefix = variant
    for path in tricks(prefix + suffix):
        for method in ("GET", "POST", "HEAD"):
            try:
                rig.send(method, path)
            except Exception:  # a path the HTTP client library itself refuses to send
                continue
    assert not (set(rig.calls) & PROTECTED)


def test_the_grid_is_not_vacuous_a_signed_in_user_runs_the_handler_on_the_plain_path(variant):
    rig, prefix = variant
    session = rig.login()
    assert raw_call(rig.app, "GET", prefix + "/x", [("Cookie", f"ops_session={session.token}")]).status == 200 and rig.calls == ["x"]


def test_pinned_a_signed_in_user_never_gets_a_500_from_any_trick(variant):
    rig, prefix = variant
    session = rig.login()
    for path in tricks(prefix + "/x") + tricks(prefix + "/item/5"):
        for method in METHODS:
            result = raw_call(rig.app, method, path, [("Cookie", f"ops_session={session.token}"), ("X-CSRF-Token", session.csrf), ("Origin", SAME)])
            assert result.status != 500, (method, path)


# --- the trailing slash ---------------------------------------------------------------------------------------------------------------------------------------

def test_a_redirect_by_the_router_is_classified_again_on_the_new_path(tmp_path, make_service, clock, monkeypatch):
    app = build("app", tmp_path / "static", mount=False)  # without the static mount the router redirects /x/ to /x
    rig = Rig(app, make_service(), clock)
    seen = []
    real = AuthMiddleware._classify
    monkeypatch.setattr(AuthMiddleware, "_classify", lambda self, scope, method: _record(seen, real, self, scope, method))
    assert rig.send("GET", "/x/").status_code == 401 and rig.calls == []  # anonymous: refused at the first hop, there is no redirect to follow
    assert seen == [("/x/", "session")]
    seen.clear()
    session = rig.login()
    cookie = [("Cookie", f"ops_session={session.token}")]
    first = rig.client.get("/x/", headers=cookie, follow_redirects=False)  # followed by hand: the client drops a Cookie header when it follows a redirect
    assert first.status_code == 307 and first.headers["location"] == "http://testserver/x"
    second = rig.client.get(first.headers["location"], headers=cookie, follow_redirects=False)
    assert second.status_code == 200
    assert seen == [("/x/", "session"), ("/x", "session")] and rig.calls == ["x"]


def _record(seen, real, middleware, scope, method):
    result = real(middleware, scope, method)
    seen.append((scope["path"], result))
    return result


def test_pinned_anonymous_a_slash_variant_of_a_public_path_is_refused_not_redirected_to_the_public_path(tmp_path, make_service, clock):
    rig = Rig(build("app", tmp_path / "static", mount=False), make_service(), clock)
    assert rig.send("GET", "/pub2").status_code == 200
    assert rig.send("GET", "/pub2/").status_code == 401  # no route matches /pub2/ by itself, so default-deny
    assert rig.send("GET", "/dual/").status_code == 401 and "dual_slash" not in rig.calls


# --- routes of a kind the middleware does not know ------------------------------------------------------------------------------------------------------------

class WeirdRoute(BaseRoute):
    """Not a Route, not an APIRoute, not a Mount, not an included router."""

    def matches(self, scope):
        return (Match.FULL, {}) if scope["type"] == "http" and scope["path"] == "/weird" else (Match.NONE, {})

    async def handle(self, scope, receive, send):
        scope["app"].state.calls.append("weird")
        await PlainTextResponse("weird")(scope, receive, send)


def test_a_route_of_an_unknown_kind_is_a_session_route_never_public(tmp_path, make_service, clock):
    app = build("app", tmp_path / "static")
    app.router.routes.insert(0, WeirdRoute())
    rig = Rig(app, make_service(), clock)
    assert rig.send("GET", "/weird").status_code == 401 and rig.calls == []
    assert rig.send("GET", "/weird", rig.login()).text == "weird" and rig.calls == ["weird"]


def test_a_plain_starlette_route_that_is_not_in_the_table_is_a_session_route(tmp_path, make_service, clock):
    app = build("app", tmp_path / "static")

    async def plain(request):
        request.app.state.calls.append("plain")
        return PlainTextResponse("plain")

    app.router.routes.insert(0, Route("/plain", plain))
    rig = Rig(app, make_service(), clock)
    assert rig.send("GET", "/plain").status_code == 401 and rig.calls == []
    assert rig.send("GET", "/plain", rig.login()).status_code == 200


def test_a_mounted_application_that_is_not_static_files_is_a_session_route(tmp_path, make_service, clock):
    from starlette.applications import Starlette
    from starlette.routing import Mount

    async def sub_page(request):
        request.app.state.calls.append("sub")
        return PlainTextResponse("sub")

    app = build("app", tmp_path / "static")
    sub = Starlette(routes=[Route("/anything", sub_page)])
    app.router.routes.insert(0, Mount("/sub", app=sub))
    rig = Rig(app, make_service(), clock)
    assert rig.send("GET", "/sub/anything").status_code == 401 and rig.send("GET", "/sub/nothing").status_code == 401
    assert rig.calls == []


def test_a_mount_of_static_files_below_the_root_is_public(tmp_path, make_service, clock):
    from starlette.routing import Mount

    files = tmp_path / "more"
    files.mkdir()
    (files / "a.txt").write_text("a")
    app = build("app", tmp_path / "static")
    app.router.routes.insert(0, Mount("/files", app=StaticFiles(directory=files), name="files"))
    rig = Rig(app, make_service(), clock)
    assert rig.send("GET", "/files/a.txt").status_code == 200 and rig.send("GET", "/files/none.txt").status_code == 404


def test_an_entry_in_the_table_can_make_a_path_of_a_mounted_application_public_explicitly(tmp_path, make_service, clock):
    from starlette.applications import Starlette
    from starlette.routing import Mount

    async def ok(request):
        return PlainTextResponse("ok")

    app = FastAPI()
    app.state.calls = []
    app.router.routes.append(Mount("/sub", app=Starlette(routes=[Route("/ok", ok), Route("/secret", ok)])))
    app.add_middleware(AuthMiddleware, policy={("GET", "/sub/ok"): "public"})
    rig = Rig(app, make_service(), clock)
    assert rig.send("GET", "/sub/ok").status_code == 200 and rig.send("GET", "/sub/secret").status_code == 401


def test_pinned_with_no_static_mount_at_all_an_unknown_path_is_a_401_not_a_404(tmp_path, make_service, clock):
    rig = Rig(build("app", tmp_path / "static", mount=False), make_service(), clock)
    assert rig.send("GET", "/nothing-here").status_code == 401


# --- the table itself -----------------------------------------------------------------------------------------------------------------------------------------

def test_a_template_with_a_converter_is_compiled_like_the_routers_own(variant):
    rig, prefix = variant
    session = rig.login()
    assert rig.send("GET", prefix + "/n/5", session).status_code == 200
    assert rig.send("GET", prefix + "/n/5", rig.login("bob")).status_code == 200
    assert rig.send("GET", prefix + "/n/5").status_code == 401
