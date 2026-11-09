"""The /auth routes: log in, log out, sign up, who am I.

FastAPI/Starlette and app.auth only. Importing this module reads no environment and creates no file; a handler reads the service from app.state.auth when a
request arrives. Nothing here logs.

* Auth off (app.state.auth is None or missing): every route answers exactly like a path that does not exist (the static mount's answer in app/main.py's error
  shape): 404 for GET and HEAD, 405 with "Allow: GET, HEAD" for the other methods. No body is read, no cookie is set.
* Bodies are read and checked by the route itself (no FastAPI body model), so a password can never be echoed back by a framework validation error: anything that
  is not a JSON object with exactly the expected string fields of a bounded length is 422 {"error":"The request is not valid.","code":"invalid_request"}.
* A wrong method on one of these paths is 405 with the Allow header of that path (the static mount would answer 404, or a 405 that names GET and HEAD).
* Every answer is JSON with Cache-Control: no-store (except the auth-off ones, which must be the unknown-path answers).
* AUTH_POLICY is the class of each route for the middleware: login and sign-up are public, who-am-I is optional, logout needs a session.
"""
import asyncio
import json
import math

from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.auth import runtime
from app.auth.config import CSRF_COOKIE, SESSION_COOKIE
from app.auth.passwords import MESSAGES

router = APIRouter(prefix="/auth")

USERNAME_MAX, PASSWORD_MAX, CODE_MAX = 256, 2048, 256  # characters; the rules proper (3 to 32, 12 to 128) are the service's
BUSY_RETRY_AFTER = 2  # seconds; a starting value, not measured
_NO_STORE = {"Cache-Control": "no-store"}
_ALL_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")

AUTH_POLICY = {
    ("*", "/auth/login"): "public",  # "*": the wrong-method handlers at the end of this module, which only answer 405
    ("*", "/auth/signup"): "public",
    ("*", "/auth/me"): "optional",
    ("*", "/auth/logout"): "session",
    ("*", "/auth/logout-all"): "session",
    ("POST", "/auth/login"): "public",
    ("POST", "/auth/signup"): "public",
    ("GET", "/auth/me"): "optional",
    ("POST", "/auth/logout"): "session",
    ("POST", "/auth/logout-all"): "session",
}


async def apply_delay(seconds):
    """Wait before a FAILED login is answered. A module-level function so that tests can replace it."""
    if seconds > 0:
        await asyncio.sleep(seconds)


# --- answers ----------------------------------------------------------------------------------------------------------------------------------

def _answer(status, body, extra=None):
    return JSONResponse(body, status_code=status, headers={**_NO_STORE, **(extra or {})})


def _invalid():
    return _answer(422, {"error": "The request is not valid.", "code": "invalid_request"})


def _busy():
    return _answer(429, {"error": "The server is busy. Try again shortly.", "code": "busy"}, {"Retry-After": str(BUSY_RETRY_AFTER)})


def _unauthenticated():
    return _answer(401, {"error": "Please sign in.", "code": "unauthenticated"})


def _unknown_path(method):
    """What a path that does not exist answers (app/main.py: the static mount's 404 or 405 through http_error_handler)."""
    if method in ("GET", "HEAD"):
        return JSONResponse({"error": "Not Found", "code": "not_found"}, status_code=404)
    return JSONResponse({"error": "Method Not Allowed", "code": "method_not_allowed"}, status_code=405, headers={"Allow": "GET, HEAD"})


def _service(request):
    return getattr(request.app.state, "auth", None)


def _https(request, settings):
    return runtime.is_https(request.scope.get("scheme"), request.scope.get("headers", []), settings.trust_proxy)


def _clear_cookies(response, request, settings):
    https = _https(request, settings)
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        response.headers.append("set-cookie", runtime.build_set_cookie(name, "", settings, https, clear=True))
    return response


async def _fields(request, required, optional=()):
    """The JSON object in the body as a dict of text, or None when the body is anything else: not UTF-8 JSON, not an object, a missing, extra, non-text or too
    long field. Nothing of the body is kept or reported."""
    raw = await request.body()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError
        return None
    if not isinstance(data, dict) or not set(required) <= set(data) <= set(required) | set(optional):
        return None
    limits = {"username": USERNAME_MAX, "password": PASSWORD_MAX, "invite_code": CODE_MAX}
    for name, value in data.items():
        if not isinstance(value, str) or len(value) > limits[name]:
            return None
    return data


# --- log in -----------------------------------------------------------------------------------------------------------------------------------

@router.post("/login")
async def login(request: Request):
    service = _service(request)
    if service is None:
        return _unknown_path(request.method)
    fields = await _fields(request, ("username", "password"))
    if fields is None:
        return _invalid()
    settings, headers = service.settings, request.scope.get("headers", [])
    ip = runtime.client_ip(request.scope.get("client"), headers, settings.trust_proxy)
    old_token = runtime.parse_cookies(runtime.header_values(headers, "cookie")).get(SESSION_COOKIE)
    result = await run_in_threadpool(service.log_in, fields["username"], fields["password"], ip)
    if result.busy:
        return _busy()
    if result.retry_after is not None:
        return _answer(429, {"error": "Too many attempts. Try again later.", "code": "too_many_attempts"}, {"Retry-After": str(max(1, math.ceil(result.retry_after)))})
    if not result.ok:
        await apply_delay(result.delay)
        return _answer(401, {"error": "Invalid username or password.", "code": "invalid_credentials"})
    if old_token is not None:
        await run_in_threadpool(service.log_out, old_token)  # a login always replaces the session it was made from
    response = _answer(200, {"username": result.username, "csrf_token": result.csrf_token})
    https = _https(request, settings)
    response.headers.append("set-cookie", runtime.build_set_cookie(SESSION_COOKIE, result.session_token, settings, https))
    response.headers.append("set-cookie", runtime.build_set_cookie(CSRF_COOKIE, result.csrf_token, settings, https))
    return response


# --- sign up ----------------------------------------------------------------------------------------------------------------------------------

@router.post("/signup")
async def signup(request: Request):
    service = _service(request)
    if service is None:
        return _unknown_path(request.method)
    fields = await _fields(request, ("username", "password"), ("invite_code",))
    if fields is None:
        return _invalid()
    result = await run_in_threadpool(service.sign_up, fields["username"], fields["password"], fields.get("invite_code"))
    if result.status == "created":
        return _answer(201, {"username": result.username})
    if result.status == "closed":
        return _answer(403, {"error": "Sign-up is closed.", "code": "signup_closed"})
    if result.status == "invalid_input":
        return _answer(422, {"error": MESSAGES.get(result.code, "The request is not valid."), "code": result.code if result.code in MESSAGES else "invalid_request"})
    if result.status == "rejected":
        return _answer(400, {"error": "Could not create the account. Check the invite code and try another name.", "code": "signup_rejected"})
    if result.status == "busy":
        return _busy()
    return _answer(500, {"error": "Internal error", "code": "internal_error"})


# --- log out ----------------------------------------------------------------------------------------------------------------------------------

def _session_token(request):
    return runtime.parse_cookies(runtime.header_values(request.scope.get("headers", []), "cookie")).get(SESSION_COOKIE)


@router.post("/logout")
async def logout(request: Request):
    service = _service(request)
    if service is None:
        return _unknown_path(request.method)
    token = _session_token(request)
    if request.scope.get("auth_session") is None or token is None:  # the middleware has already checked; this route does not rely on that alone
        return _unauthenticated()
    await run_in_threadpool(service.log_out, token)
    return _clear_cookies(_answer(200, {"ok": True}), request, service.settings)


@router.post("/logout-all")
async def logout_all(request: Request):
    service = _service(request)
    if service is None:
        return _unknown_path(request.method)
    token = _session_token(request)
    if request.scope.get("auth_session") is None or token is None:
        return _unauthenticated()
    closed = await run_in_threadpool(service.log_out_everywhere, token)
    return _clear_cookies(_answer(200, {"ok": True, "sessions_closed": closed}), request, service.settings)


# --- who am I ---------------------------------------------------------------------------------------------------------------------------------

@router.get("/me")
async def me(request: Request):
    service = _service(request)
    if service is None:
        return _unknown_path(request.method)
    session = request.scope.get("auth_session")
    if session is not None:
        return _answer(200, {"authenticated": True, "username": session.username, "csrf_token": session.csrf_token})
    return _answer(200, {"authenticated": False, "signup": service.settings.effective_signup})


# --- a wrong method ---------------------------------------------------------------------------------------------------------------------------

def _wrong_method(allow):
    async def handler(request: Request):
        if _service(request) is None:
            return _unknown_path(request.method)
        return _answer(405, {"error": "Method Not Allowed", "code": "method_not_allowed"}, {"Allow": allow})

    return handler


for _path, _allowed in (("/login", "POST"), ("/signup", "POST"), ("/logout", "POST"), ("/logout-all", "POST"), ("/me", "GET")):
    router.add_api_route(_path, _wrong_method(_allowed), methods=[m for m in _ALL_METHODS if m != _allowed], include_in_schema=False)
