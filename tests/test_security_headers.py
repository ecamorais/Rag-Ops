"""Security headers: every response except the interactive API docs carries them.

Runs the real app (real startup, real retrieval) against a COPY of chroma_db, never the original: the copy is made when
this file is imported and CHROMA_DIR points at it. The language model is stubbed (llm.generate_answer), the API key is
forced empty, and nothing is sent to Gemini. The test needs chroma_db/, data/tickets.json and the local embedding model
(HF_HUB_OFFLINE=1, so it never downloads).
"""
import atexit
import os
import shutil
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if not (ROOT / "chroma_db").is_dir():
    pytest.skip("chroma_db/ is missing: run python scripts/ingest.py first", allow_module_level=True)

# Must happen before app.config is imported: it reads these variables once.
_COPY_DIR = Path(tempfile.mkdtemp(prefix="ops-rag-chroma-copy-"))
atexit.register(shutil.rmtree, _COPY_DIR, ignore_errors=True)
shutil.copytree(ROOT / "chroma_db", _COPY_DIR / "chroma_db")
os.environ["CHROMA_DIR"] = str(_COPY_DIR / "chroma_db")
os.environ["GEMINI_API_KEY"] = ""  # the stub below is the only "model"; an empty key makes a real call impossible
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from fastapi.testclient import TestClient  # noqa: E402

from app import llm, main  # noqa: E402
from app.config import settings  # noqa: E402

if Path(settings.chroma_dir).resolve() != (_COPY_DIR / "chroma_db").resolve():
    raise RuntimeError(f"Chroma must be the copy, not {settings.chroma_dir} (was app.config imported before this file?)")

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "content-security-policy": CSP,
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
}
DOCS_PATHS = ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"]
ORIGIN = "http://127.0.0.1:8000"


def release_chroma():
    """Close the sqlite file of the copy: Windows cannot delete a file that is still open, so without this the copy
    would stay behind in the temp folder after every run."""
    import gc

    from chromadb.api.shared_system_client import SharedSystemClient

    from app import vectorstore

    SharedSystemClient.clear_system_cache()
    vectorstore._client.cache_clear()
    gc.collect()


@pytest.fixture(scope="module")
def client():
    # raise_server_exceptions=False: an unexpected error must come back as the app's own 500 response, as in production
    with TestClient(main.app, raise_server_exceptions=False) as c:
        yield c
    release_chroma()


@pytest.fixture(autouse=True)
def stub_model(monkeypatch):
    """The language model: a fixed answer that cites OPS-1001. Individual tests replace it to make it fail."""
    monkeypatch.setattr(llm, "generate_answer", lambda system, prompt: llm.GeminiAnswer(answer="See [OPS-1001].", found=True, citations=["OPS-1001"]))


def model_fails(monkeypatch, error):
    def fail(system, prompt):
        raise error
    monkeypatch.setattr(llm, "generate_answer", fail)


def assert_security_headers(response):
    for name, value in SECURITY_HEADERS.items():
        assert response.headers.get(name) == value, f"{name} on {response.request.method} {response.request.url.path}"


def parse_csp(value):
    directives = {}
    for part in value.split(";"):
        name, _, rest = part.strip().partition(" ")
        if name:
            directives[name] = rest.strip()
    return directives


def test_the_chroma_copy_is_what_the_app_opened():
    assert Path(settings.chroma_dir).resolve() == (_COPY_DIR / "chroma_db").resolve()
    assert Path(settings.chroma_dir).resolve() != (ROOT / "chroma_db").resolve()
    assert settings.gemini_api_key == ""


@pytest.mark.parametrize("path", ["/", "/app.js", "/style.css", "/favicon.svg", "/health", "/filters", "/tickets/OPS-1001"])
def test_get_responses_carry_the_headers(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert_security_headers(response)


def test_search_response_carries_the_headers(client):
    response = client.post("/search", json={"query": "vpn outage", "k": 3})
    assert response.status_code == 200
    assert_security_headers(response)


def test_chat_response_carries_the_headers(client):
    response = client.post("/chat", json={"question": "What happened in OPS-1001?"})
    assert response.status_code == 200
    assert response.json()["answer"].endswith("See [OPS-1001].")
    assert_security_headers(response)


def test_a_cached_static_file_answer_carries_the_headers(client):
    first = client.get("/app.js")
    assert first.headers.get("etag")
    cached = client.get("/app.js", headers={"If-None-Match": first.headers["etag"]})
    assert cached.status_code == 304
    assert_security_headers(cached)


@pytest.mark.parametrize("call, status", [
    (lambda c: c.get("/nope"), 404),
    (lambda c: c.get("/tickets/OPS-9999"), 404),
    (lambda c: c.post("/chat", json={}), 422),
    (lambda c: c.get("/tickets/abc"), 422),
], ids=["404 static", "404 ticket", "422 chat body", "422 ticket id"])
def test_error_responses_carry_the_headers(client, call, status):
    response = call(client)
    assert response.status_code == status
    assert_security_headers(response)


def test_429_carries_the_headers_and_keeps_retry_after(client, monkeypatch):
    model_fails(monkeypatch, llm.RateLimitError("Quota used up.", retry_after_seconds=90))
    response = client.post("/chat", json={"question": "What happened in OPS-1001?"})
    assert response.status_code == 429
    assert response.headers["retry-after"] == "90"
    assert response.json()["code"] == "rate_limited"
    assert_security_headers(response)


def test_503_carries_the_headers(client, monkeypatch):
    model_fails(monkeypatch, llm.ServerError("The model service is overloaded."))
    response = client.post("/chat", json={"question": "What happened in OPS-1001?"})
    assert response.status_code == 503
    assert_security_headers(response)


def test_unexpected_500_is_the_normal_json_error_and_carries_the_headers(client, monkeypatch):
    model_fails(monkeypatch, RuntimeError("unexpected failure inside the chat route"))
    response = client.post("/chat", json={"question": "What happened in OPS-1001?"})
    assert response.status_code == 500
    assert response.json() == {"error": "Internal error", "code": "internal_error"}
    assert_security_headers(response)


def test_the_policy_is_exactly_the_approved_one(client):
    value = client.get("/").headers["content-security-policy"]
    assert parse_csp(value) == {
        "default-src": "'none'", "script-src": "'self'", "style-src": "'self'", "connect-src": "'self'",
        "img-src": "'self' data:", "base-uri": "'none'", "form-action": "'self'", "frame-ancestors": "'none'",
    }
    assert "unsafe" not in value and "*" not in value


@pytest.mark.parametrize("path", DOCS_PATHS)
def test_the_api_docs_are_left_alone(client, path):
    response = client.get(path)
    assert response.status_code == 200
    for name in SECURITY_HEADERS:
        assert name not in response.headers, f"{name} must not be set on {path} (Swagger UI and ReDoc load a CDN)"


def test_cors_preflight_is_unchanged(client):
    response = client.options("/chat", headers={
        "Origin": ORIGIN, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type",
    })
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert response.headers["access-control-allow-methods"] == "GET, POST, OPTIONS"
    assert response.headers["access-control-allow-headers"] == "Accept, Accept-Language, Content-Language, Content-Type"
    assert response.headers["access-control-max-age"] == "600"
    for name in SECURITY_HEADERS:  # CORS answers a preflight itself, before the page middleware
        assert name not in response.headers


def test_cors_still_works_next_to_the_headers(client, monkeypatch):
    allowed = client.post("/chat", json={"question": "What happened in OPS-1001?"}, headers={"Origin": ORIGIN})
    assert allowed.headers["access-control-allow-origin"] == ORIGIN
    assert_security_headers(allowed)
    model_fails(monkeypatch, llm.RateLimitError("Quota used up.", retry_after_seconds=90))
    rate_limited = client.post("/chat", json={"question": "What happened in OPS-1001?"}, headers={"Origin": ORIGIN})
    assert rate_limited.status_code == 429
    assert rate_limited.headers["access-control-allow-origin"] == ORIGIN
    assert_security_headers(rate_limited)


def test_a_foreign_origin_gets_no_cors_header_but_still_the_security_headers(client):
    response = client.post("/chat", json={"question": "What happened in OPS-1001?"}, headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in response.headers
    assert_security_headers(response)
