"""FastAPI backend over the RAG core: health, filters, search, ticket detail and chat."""

import logging
import math
import re
import time
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Path, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import llm, rag
from app.config import settings
from app.ingest import TICKETS_PATH, load_tickets
from app.schemas import (
    ChatRequest,
    ChatResponse,
    FiltersResponse,
    HealthResponse,
    SearchRequest,
    SearchResponse,
    TicketDetail,
)
from app.vectorstore import get_collection, get_embedder, search

# localhost and 127.0.0.1 on any port, http or https. Nothing else may call the API from a browser.
LOCAL_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"

# Retry-After on a 429 is capped at one day.
MAX_RETRY_AFTER = 86400

# Sent on every response except the interactive API docs. The page needs nothing but its own files, so everything
# else is switched off: no inline script or style, no other origins, no framing, no <base>.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
# Swagger UI and ReDoc load their scripts and styles from a CDN (and Swagger runs an inline script), which this
# policy would block, so these paths are left alone.
NO_SECURITY_HEADERS_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"})

# First match wins, so subclasses are listed before LlmError itself.
LLM_ERROR_STATUS = (
    (llm.RateLimitError, 429, "rate_limited"),
    (llm.ServerError, 503, "upstream_unavailable"),
    (llm.NetworkError, 503, "upstream_unavailable"),
    (llm.BadResponseError, 502, "bad_upstream_response"),
    (llm.BadRequestError, 502, "bad_upstream_response"),
    (llm.MissingApiKeyError, 500, "server_misconfigured"),
    (llm.InvalidApiKeyError, 500, "server_misconfigured"),
    (llm.LlmError, 502, "bad_upstream_response"),
)

logger = logging.getLogger("ops_rag")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _redact(text):
    """Server-side log text must never contain the API key."""
    key = settings.gemini_api_key
    return text.replace(key, "***") if key else text


def _error(status, message, code, headers=None):
    return JSONResponse({"error": message, "code": code}, status_code=status, headers=headers)


def _priority_rank(priority):
    """P1, P2, ... in numeric order; anything not starting with P<number> sorts last."""
    match = re.match(r"P(\d+)", priority)
    return (int(match.group(1)) if match else 99, priority)


def build_filter_options(tickets):
    """Distinct filter values and the created-date range, computed once from the loaded tickets."""
    rows = tickets.values()
    created = sorted(t["created_at"][:10] for t in rows)  # YYYY-MM-DD
    return {
        "category": sorted({t["category"] for t in rows}),
        "priority": sorted({t["priority"] for t in rows}, key=_priority_rank),
        "status": sorted({t["status"] for t in rows}),
        "team": sorted({t["team"] for t in rows}),
        "created_min": created[0] if created else None,
        "created_max": created[-1] if created else None,
    }


@asynccontextmanager
async def lifespan(app):
    # Load everything once, at startup, not per request.
    get_embedder()
    indexed = get_collection().count()
    if indexed == 0:
        logger.warning("The Chroma collection is empty. Run: python scripts/ingest.py --reset")
    try:
        app.state.tickets = {t["ticket_id"]: t for t in load_tickets(TICKETS_PATH)}
    except FileNotFoundError:
        app.state.tickets = {}
        logger.warning(
            "Tickets file not found at %s. Run: python scripts/generate_tickets.py, then python scripts/ingest.py",
            TICKETS_PATH,
        )
    app.state.filter_options = build_filter_options(app.state.tickets)
    logger.info("Ready: %d tickets indexed, %d tickets loaded", indexed, len(app.state.tickets))
    yield


app = FastAPI(title="ops-rag", lifespan=lifespan)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """One log line per request: method, path, status, milliseconds. Never bodies, headers or query strings."""
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        # Anything not handled below. The details go to the log only (key redacted).
        logger.error("Unhandled error on %s %s\n%s", request.method, request.url.path, _redact(traceback.format_exc()))
        response = _error(500, "Internal error", "internal_error")
    logger.info("%s %s %d %.0f ms", request.method, request.url.path, response.status_code,
                (time.perf_counter() - start) * 1000)
    return response


# Defined AFTER the logging middleware on purpose: the last middleware added is the outermost one, so this wraps
# logging and the 500 it builds, and error responses (404, 422, 429, 500) carry the headers like any other.
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path not in NO_SECURITY_HEADERS_PATHS:
        response.headers.update(SECURITY_HEADERS)
    return response


# Added AFTER the two middlewares above on purpose: the last middleware added is the outermost one,
# so CORS wraps logging, the security headers and every response, including the 500 built above, and
# every one of them carries the CORS headers.
# Side effect: CORS answers preflight (OPTIONS) requests itself, so those are not logged.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=LOCAL_ORIGIN_REGEX,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)


@app.exception_handler(llm.LlmError)
async def llm_error_handler(request: Request, error: llm.LlmError):
    # The message of our own LlmError classes is already readable and never contains the key.
    for error_class, status, code in LLM_ERROR_STATUS:
        if isinstance(error, error_class):
            headers = None
            seconds = getattr(error, "retry_after_seconds", None)
            if status == 429 and seconds:
                headers = {"Retry-After": str(max(1, min(math.ceil(seconds), MAX_RETRY_AFTER)))}
            return _error(status, str(error), code, headers)
    return _error(500, "Internal error", "internal_error")  # unreachable: LlmError is in the table


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, error: StarletteHTTPException):
    codes = {404: "not_found", 405: "method_not_allowed"}
    return _error(error.status_code, str(error.detail), codes.get(error.status_code, "http_error"), error.headers)


@app.get("/health", response_model=HealthResponse)
async def health(request: Request):
    indexed = get_collection().count()
    return {
        "status": "ok" if indexed and request.app.state.tickets else "degraded",
        "indexed_tickets": indexed,
        "model": settings.gemini_model,
        "key_configured": bool(settings.gemini_api_key),
    }


@app.get("/filters", response_model=FiltersResponse)
async def filters(request: Request):
    return request.app.state.filter_options


@app.post("/search", response_model=SearchResponse)
def search_tickets(body: SearchRequest):
    """Raw vector search for debugging: nothing is dropped, each hit says if /chat would keep it."""
    filters = body.filters.model_dump(exclude_none=True) if body.filters else None
    hits = search(body.query, k=body.k, filters=filters)
    return {
        "threshold": settings.similarity_threshold,
        "hits": [
            {
                "ticket_id": hit["ticket_id"],
                "title": rag.title_of(hit["document"]),
                "category": hit["metadata"].get("category"),
                "priority": hit["metadata"].get("priority"),
                "status": hit["metadata"].get("status"),
                "similarity": hit["similarity"],
                "above_threshold": hit["similarity"] >= settings.similarity_threshold,
                "snippet": rag.snippet_of(hit["document"]),
            }
            for hit in hits
        ],
    }


@app.get("/tickets/{ticket_id}", response_model=TicketDetail)
async def get_ticket(request: Request, ticket_id: str = Path(pattern=r"^OPS-[0-9]+$", max_length=20)):
    """The full record from the tickets loaded at startup. A malformed id is a 422, a missing one a 404."""
    ticket = request.app.state.tickets.get(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    return ticket


@app.post("/chat", response_model=ChatResponse)
def chat(body: ChatRequest):
    """Answer a question from the tickets with the RAG core.

    A plain def on purpose: FastAPI runs it in a worker thread, so a slow Gemini call (retries can
    sleep for over a minute) does not block the event loop or other requests. If the client
    disconnects while the call is running, the thread is NOT cancelled: it runs until
    answer_question returns, so the Gemini call and its quota use still happen.
    """
    start = time.perf_counter()
    filters = body.filters.model_dump(exclude_none=True) if body.filters else None
    history = [turn.model_dump() for turn in body.history] if body.history else None
    try:
        result = rag.answer_question(body.question, filters=filters, k=body.k, history=history)
    except llm.LlmError:
        # The error reached the model layer. The handler above turns it into the JSON response.
        logger.info("chat failed llm_call=yes %.0f ms", (time.perf_counter() - start) * 1000)
        raise
    # answer_question calls Gemini exactly when it has at least one ticket in the context.
    # Only counts and timings are logged: never the question, the answer or the history.
    logger.info("chat found=%s retrieved=%d llm_call=%s %.0f ms", result["found"], result["retrieved_count"],
                "yes" if result["retrieved_count"] else "no", (time.perf_counter() - start) * 1000)
    return result


# Must stay LAST: a mount at "/" matches every path, so it would shadow any route registered after it.
# check_dir=False lets the server start even if static/ is missing; until Phase 5 it has no index.html.
app.mount("/", StaticFiles(directory=settings.project_root / "static", html=True, check_dir=False), name="static")
