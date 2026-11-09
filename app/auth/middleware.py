"""The gate in front of the app: a pure ASGI middleware that decides, per request, who may pass.

Standard library, Starlette and app.auth only. It reads the service from scope["app"].state.auth at request time: None (or no attribute) means auth is off and
the request goes straight through with no work at all (no cookie is read, no database is touched). It logs nothing, and none of its answers holds a cookie
value, a token, a user name or an address.

Route classes come from a table given to the constructor, {(METHOD or "*", path template): "public" | "session" | "optional"}, with the paths as the app
serves them (a prefix given to include_router is part of the path). The templates are compiled with starlette.routing.compile_path and matched against the
request path, so the lookup does not depend on how the framework stores its routes (FastAPI keeps an included router as one opaque entry):
  public    no session needed (login, sign-up, the static files). An unsafe method still has to pass origin_ok().
  optional  a valid session is attached if a cookie is present, and nobody is ever rejected (health, who-am-I). Unsafe methods: as public.
  session   a live session is needed; an unsafe method also needs origin_ok() and a matching X-CSRF-Token.
The class of a request is, in this order: the entries whose template matches the whole path and whose method is the request's (the strictest if several), else
the "*" entries, else the strictest class of the entries that match the path (a wrong method must not be a way round). With no entry at all it is default-deny:
"session" if ANY route that is not the static files matches the path (route.matches, included routers and routes of a kind this module does not know
included), "public" only if just a Mount of StaticFiles matches, and "session" if nothing matches. A Mount of anything else than StaticFiles is not public. The
class assigned is therefore never weaker than the one of the route the router dispatches to, unless the table itself says so. The session is put in
scope["auth_session"] (a SessionInfo); the token itself is not.

Safe methods (GET, HEAD, OPTIONS) never need a CSRF check, but a "session" route still needs the session for them: an anonymous HEAD or OPTIONS gets the 401.
Under limited_prefix (default /auth), for every unsafe method: a Content-Length above max_body gives 413 without reading the body; a body of any other kind is
counted on the stream and cut at max_body (the handler never receives more); a body (Content-Length above 0, or chunked) must be application/json, else 415.
The size is checked before the media type. A request with no body needs no Content-Type (logout sends none).
"""
import re

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.routing import Match, Mount, compile_path
from starlette.staticfiles import StaticFiles

from app.auth.config import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from app.auth.runtime import build_set_cookie, header_values, is_https, origin_ok, parse_cookies

CLASSES = ("public", "session", "optional")
_STRICTNESS = {"public": 0, "optional": 1, "session": 2}
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
MAX_BODY = 16384
_DIGITS = re.compile(r"[0-9]+")
_NO_STORE = {"Cache-Control": "no-store"}


def _strictest(classes):
    return max(classes, key=_STRICTNESS.get)


class _Replied(Exception):
    pass


class AuthMiddleware:
    def __init__(self, app, policy, *, hidden=(), limited_prefix="/auth", max_body=MAX_BODY):
        table = {}
        for key, klass in dict(policy).items():
            method, path = key
            if klass not in CLASSES or not isinstance(path, str) or not isinstance(method, str):
                raise ValueError("bad auth policy entry")
            table[(method.upper(), path)] = klass
        self.app = app
        self._policy = table
        self._entries = tuple((method, compile_path(path)[0], klass) for (method, path), klass in table.items())
        self._hidden = frozenset(hidden)
        self._prefix = limited_prefix.rstrip("/")
        self._max_body = max_body

    # --- entry --------------------------------------------------------------------------------------------------------------------------------

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        service = getattr(getattr(scope.get("app"), "state", None), "auth", None) if kind in ("http", "websocket") else None
        if service is None:
            await self.app(scope, receive, send)
            return
        if kind == "websocket":
            await receive()  # the connect message; closing before accept is refused by the server with this code
            await send({"type": "websocket.close", "code": 1008})
            return
        await self._http(service, scope, receive, send)

    async def _http(self, service, scope, receive, send):
        method, path, headers = scope["method"].upper(), scope["path"], scope.get("headers", [])
        if path in self._hidden:
            await self._unknown_path(method)(scope, receive, send)
            return
        limited = method not in SAFE_METHODS and (path == self._prefix or path.startswith(self._prefix + "/"))
        if limited:
            refusal = self._limit_refusal(headers)
            if refusal is not None:
                await refusal(scope, receive, send)
                return
        klass = self._classify(scope, method)
        try:
            await self._admit(service, scope, klass, method, headers)
        except _Replied as reply:
            await reply.args[0](scope, receive, send)
            return
        if limited:
            await self._run_counted(scope, receive, send)
        else:
            await self.app(scope, receive, send)

    # --- classification -----------------------------------------------------------------------------------------------------------------------

    def _classify(self, scope, method):
        path = scope["path"]
        found = [(m, klass) for m, regex, klass in self._entries if regex.match(path)]
        if found:
            for wanted in (method, "*"):
                chosen = [klass for m, klass in found if m == wanted]
                if chosen:
                    return _strictest(chosen)
            return _strictest([klass for _, klass in found])
        static = False
        for route in scope["app"].router.routes:
            if isinstance(route, Mount) and isinstance(route.app, StaticFiles):
                static = static or route.matches(scope)[0] == Match.FULL
            elif route.matches(scope)[0] != Match.NONE:
                return "session"
        return "public" if static else "session"

    # --- the decision -------------------------------------------------------------------------------------------------------------------------

    async def _admit(self, service, scope, klass, method, headers):
        unsafe = method not in SAFE_METHODS
        cookies = parse_cookies(header_values(headers, "cookie")) if klass != "public" else {}
        token = cookies.get(SESSION_COOKIE)
        if klass == "optional":
            if token is not None:
                info = await run_in_threadpool(service.validate_session, token)
                if info is not None:
                    scope["auth_session"] = info
            if unsafe and not origin_ok(headers):
                raise _Replied(_csrf_refused())
            return
        if klass == "public":
            if unsafe and not origin_ok(headers):
                raise _Replied(_csrf_refused())
            return
        info = await run_in_threadpool(service.validate_session, token) if token is not None else None
        if info is None:
            raise _Replied(self._unauthenticated(service, scope, clear=token is not None))
        if unsafe:
            supplied = header_values(headers, CSRF_HEADER)
            if not origin_ok(headers) or len(supplied) != 1 or not service.verify_csrf(info, supplied[0]):
                raise _Replied(_csrf_refused())
        scope["auth_session"] = info

    def _unauthenticated(self, service, scope, *, clear):
        response = JSONResponse({"error": "Please sign in.", "code": "unauthenticated"}, status_code=401, headers=_NO_STORE)
        if clear:
            settings = service.settings
            https = is_https(scope.get("scheme"), scope.get("headers", []), settings.trust_proxy)
            for name in (SESSION_COOKIE, CSRF_COOKIE):
                response.headers.append("set-cookie", build_set_cookie(name, "", settings, https, clear=True))
        return response

    @staticmethod
    def _unknown_path(method):
        """What the static mount answers for a path that is not there, in the JSON shape of the app's own error handler."""
        if method in ("GET", "HEAD"):
            return JSONResponse({"error": "Not Found", "code": "not_found"}, status_code=404)
        return JSONResponse({"error": "Method Not Allowed", "code": "method_not_allowed"}, status_code=405, headers={"Allow": "GET, HEAD"})

    # --- the limits on the auth routes --------------------------------------------------------------------------------------------------------

    def _limit_refusal(self, headers):
        lengths = header_values(headers, "content-length")
        declared = None
        if len(lengths) == 1 and _DIGITS.fullmatch(lengths[0].strip()):
            declared = int(lengths[0].strip()) if len(lengths[0].strip()) <= 18 else self._max_body + 1
        if declared is not None and declared > self._max_body:
            return JSONResponse({"error": "The request is too large.", "code": "payload_too_large"}, status_code=413, headers=_NO_STORE)
        has_body = (declared or 0) > 0 or bool(header_values(headers, "transfer-encoding")) or (declared is None and bool(lengths))
        if has_body:
            types = header_values(headers, "content-type")
            if len(types) != 1 or types[0].split(";", 1)[0].strip().lower() != "application/json":
                return JSONResponse({"error": "The request must be JSON.", "code": "unsupported_media_type"}, status_code=415, headers=_NO_STORE)
        return None

    async def _run_counted(self, scope, receive, send):
        """Run the app with the body counted. Past max_body the chunk is withheld and the app sees a disconnect; whatever the app then answers is dropped and
        replaced by the 413, unless it had already started its answer."""
        seen, too_large, started, replied = 0, False, False, False

        async def counted_receive():
            nonlocal seen, too_large
            if too_large:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self._max_body:
                    too_large = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message):
            nonlocal started, replied
            if too_large and not started:
                if not replied:
                    replied = True
                    await JSONResponse({"error": "The request is too large.", "code": "payload_too_large"}, status_code=413, headers=_NO_STORE)(scope, receive, send)
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counted_receive, guarded_send)
        except Exception:
            if not (too_large and not started):
                raise
        if too_large and not started and not replied:
            await JSONResponse({"error": "The request is too large.", "code": "payload_too_large"}, status_code=413, headers=_NO_STORE)(scope, receive, send)


def _csrf_refused():
    return JSONResponse({"error": "The request was refused.", "code": "csrf_failed"}, status_code=403, headers=_NO_STORE)
