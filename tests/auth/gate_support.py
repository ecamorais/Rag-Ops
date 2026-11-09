"""Shared by the middleware tests (test_http_gate, test_http_csrf, test_http_limits). Not a test file.

build_app() makes a small throwaway FastAPI app whose routes mirror the KINDS of route in the real app: public (login, sign-up), optional (health, who-am-I),
session (the data routes and logout), a route that is not in the table at all, a websocket, and a static mount at "/" (a real StaticFiles on a temp folder, so
an unknown path answers exactly as in the real app). It has the same JSON error handler for HTTP errors as app/main.py. Rig puts the real AuthService (on a
database under tmp_path, with the FakeClock) on app.state.auth and sends requests with the cookies and headers set by hand, because routes.py does not exist yet.
"""
import asyncio
import threading
from dataclasses import dataclass

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect
from starlette.staticfiles import StaticFiles

from app.auth.middleware import AuthMiddleware

PASSWORD = "correct horse battery staple"
SAME = "http://testserver"  # the Origin of a page served by the test client (its Host header is "testserver")

POLICY = {
    ("GET", "/health"): "optional",
    ("GET", "/auth/me"): "optional",
    ("POST", "/auth/login"): "public",
    ("POST", "/auth/signup"): "public",
    ("POST", "/auth/echo"): "public",
    ("POST", "/auth/model"): "public",
    ("POST", "/auth/logout"): "session",
    ("POST", "/auth/logout-all"): "session",
    ("POST", "/auth/stream"): "session",
    ("GET", "/filters"): "session",
    ("POST", "/search"): "session",
    ("GET", "/tickets/{ticket_id}"): "session",
    ("POST", "/chat"): "session",
}
HIDDEN = ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect")
# the paths of the routes above that a test can call (the template of the one with a parameter filled in), with the class the table gives them
PATHS = {
    "/health": "optional", "/auth/me": "optional", "/auth/login": "public", "/auth/signup": "public", "/auth/echo": "public", "/auth/model": "public",
    "/auth/logout": "session", "/auth/logout-all": "session", "/auth/stream": "session", "/filters": "session", "/search": "session",
    "/tickets/OPS-1001": "session", "/chat": "session", "/unlisted": "session",
}
SESSION_ROUTES = [p for p, k in PATHS.items() if k == "session"]
UNSAFE = ("POST", "PUT", "PATCH", "DELETE")


class Model(BaseModel):
    username: str


def build_app(static_dir, *, middleware=True, policy=POLICY, lifespan=None):
    app = FastAPI(lifespan=lifespan)
    app.state.calls = []  # (path, detail) of every handler that ran
    app.state.lifespan_ran = False

    def who(request):
        session = request.scope.get("auth_session")
        answer = {"user": session.username if session is not None else None}
        if "thread" in request.query_params:  # the thread the event loop runs on, for the test that the session lookup is not on it
            answer["thread"] = threading.get_ident()
        return answer

    def note(request, detail=None):
        request.app.state.calls.append((request.url.path, detail))

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request, error):  # the same shape as app/main.py
        codes = {404: "not_found", 405: "method_not_allowed"}
        return JSONResponse({"error": str(error.detail), "code": codes.get(error.status_code, "http_error")}, status_code=error.status_code, headers=error.headers)

    @app.get("/health")
    async def health(request: Request):
        note(request)
        return who(request)

    @app.get("/auth/me")
    async def me(request: Request):
        note(request)
        return who(request)

    @app.post("/auth/login")
    async def login(request: Request):
        note(request)
        return who(request)

    @app.post("/auth/signup")
    async def signup(request: Request):
        note(request)
        return who(request)

    @app.post("/auth/echo")
    async def echo(request: Request):
        try:
            body = await request.body()
        except ClientDisconnect:
            note(request, "disconnected")
            return JSONResponse({"disconnected": True}, status_code=400)
        note(request, len(body))
        return {"length": len(body)}

    @app.post("/auth/model")
    async def model(request: Request, body: Model):
        note(request, body.username)
        return {"username": body.username}

    @app.post("/auth/logout")
    async def logout(request: Request):
        note(request)
        return who(request)

    @app.post("/auth/logout-all")
    async def logout_all(request: Request):
        note(request)
        return who(request)

    @app.post("/auth/stream")
    async def stream(request: Request):
        total = 0
        try:
            async for chunk in request.stream():
                total += len(chunk)
        except ClientDisconnect:
            note(request, ("disconnected", total))
            return JSONResponse({"disconnected": True, "seen": total}, status_code=400)
        note(request, total)
        return {"seen": total}

    @app.get("/filters")
    async def filters(request: Request):
        note(request)
        return who(request)

    @app.post("/search")
    def search(request: Request):
        note(request)
        return who(request)

    @app.get("/tickets/{ticket_id}")
    async def ticket(request: Request, ticket_id: str):
        note(request)
        return who(request)

    @app.post("/chat")
    def chat(request: Request):  # a plain def like the real one: it runs in the threadpool
        note(request)
        return who(request)

    @app.get("/unlisted")  # on purpose NOT in the policy table: default-deny must make it a session route
    async def unlisted(request: Request):
        note(request)
        return who(request)

    @app.websocket("/ws")
    async def socket(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("hello")
        await websocket.close()

    static_dir.mkdir(exist_ok=True)
    for name, text in (("index.html", "<p>hello</p>"), ("app.js", "// js")):
        if not (static_dir / name).exists():  # two apps may share the folder: the files (and so their ETag) must be the same
            (static_dir / name).write_text(text)
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")  # last, like the real app
    if middleware:
        app.add_middleware(AuthMiddleware, policy=policy, hidden=HIDDEN)
    return app


@dataclass
class Session:
    name: str
    token: str
    csrf: str


class Rig:
    """The throwaway app plus the real service. send() builds the headers by hand."""

    def __init__(self, app, env, clock):
        self.app, self.env, self.clock = app, env, clock
        self.client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
        app.state.auth = env.svc

    def login(self, name="alice"):
        if self.env.store.get_user(name) is None:
            self.env.add_local_user(name, PASSWORD)
        result = self.env.svc.log_in(name, PASSWORD)
        assert result.ok
        return Session(name, result.session_token, result.csrf_token)

    @property
    def calls(self):
        return self.app.state.calls

    def send(self, method, path, session=None, *, token=None, csrf=None, origin=SAME, fetch=None, cookie=None, headers=(), **kwargs):
        """token / csrf default to the session's. Pass token="" or csrf="" to leave the cookie or the header out. `cookie` is the whole Cookie header value
        (text or bytes), replacing the one built from the token. `headers` are extra (name, value) pairs, sent after the others (so repeats are possible)."""
        if token is None and session is not None:
            token = session.token
        if csrf is None and session is not None:
            csrf = session.csrf
        pairs = []
        if cookie is not None and cookie != "":
            pairs.append(("Cookie", cookie))
        elif token:
            pairs.append(("Cookie", f"ops_session={token}"))
        if csrf:
            pairs.append(("X-CSRF-Token", csrf))
        if origin is not None:
            pairs.append(("Origin", origin))
        if fetch is not None:
            pairs.append(("Sec-Fetch-Site", fetch))
        pairs.extend(headers)
        return self.client.request(method, path, headers=pairs, **kwargs)


class RawResult:
    def __init__(self):
        self.status, self.headers, self.body, self.receive_calls, self.sent = None, [], b"", 0, []

    def header(self, name):
        return [v for k, v in self.headers if k.lower() == name.lower()]


def raw_call(app, method, path, headers, chunks=()):
    """Call the app as an ASGI server would, with a body in the given chunks, and count how many times it asked for the body."""
    result = RawResult()
    pending = [{"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1} for i, chunk in enumerate(chunks)] or [{"type": "http.request", "body": b"", "more_body": False}]

    async def receive():
        result.receive_calls += 1
        return pending.pop(0) if pending else {"type": "http.disconnect"}

    async def send(message):
        result.sent.append(message["type"])
        if message["type"] == "http.response.start":
            result.status = message["status"]
            result.headers = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in message["headers"]]
        elif message["type"] == "http.response.body":
            result.body += message.get("body", b"")

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": b"", "root_path": "", "client": ("testclient", 50000), "server": ("testserver", 80),
        "headers": [(b"host", b"testserver")] + [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers],
    }
    asyncio.run(app(scope, receive, send))
    return result
