# ops-rag

RAG system over a dummy "Operations Tickets" dataset (800 tickets) with a web chat interface.
Current status: Dataset generated, indexed in ChromaDB, answerable from the command line with Gemini (grounded answers with validated citations), and served by a FastAPI backend (health, filters, search, ticket detail, chat). The backend is validated against a running server over real sockets: health, filters, search, tickets, input validation, CORS, path traversal, the empty-collection startup warning, and the `/chat` paths that need no model. On 2026-10-26 four real `/chat` calls succeeded on `gemini-3.6-flash` (the VPN question, the root-cause follow-up, and the vendor question with and without history). Findings: the VPN answer slightly over-attributed a cause (it cited two tickets for a firmware cause they do not state), the root-cause follow-up ignored the history, and the vendor question could not be answered (aggregation: only 6 of 800 tickets are retrieved). The prompt rules, the follow-up retrieval rule and the ticket details line were changed or added after those calls and re-tested with 3 real calls (see the retest note under the follow-ups); the newest system-prompt rule has NOT been tested on real Gemini. The chat UI is built (`static/`, see the Chat UI section) and was tested in headless Edge 155 against a stub server; a Content-Security-Policy is in place. NOT verified: the UI with real Gemini answers after the prompt and retrieval changes (the browser run used stub answers), Firefox and Safari, screen readers, touch and IME input.

Phase 3 status (RAG core, CLI only):
- Validated on real Gemini: the VPN question, the OPS-1001 lookup, the OPS-1001 follow-up, the P1 filter (`--priority P1-Critical`). These ran on `gemini-3.6-flash`, except the P1 filter, which ran on `gemini-3.5-flash`.
- Validated without a model call: off-topic refusal, OPS-9999 (not-found answer), the prompt-injection escaping path, retrieval for inherited-id follow-ups.
- Real Gemini on 2026-10-26 (`gemini-3.6-flash`): four `/chat` calls succeeded (VPN question, root-cause follow-up, vendor question with and without history); the findings are in the status line above.
  - The changed prompt rules, follow-up retrieval and ticket details line were re-tested with 3 real calls later that day (see the follow-ups below). The newest system-prompt rule (who performed an action) has NOT been tested on real Gemini.
- Known limits:
  - Vector search ranks exact ids poorly (handled by the exact-id lookup in `app/rag.py`).
  - Aggregation questions ("which vendor has the most...") are weak (Phase 7c).
  - Free-tier Gemini has small daily quotas.

## Stack and decisions
- Python 3.11+, virtualenv in `.venv`
- Embeddings: local sentence-transformers, model `all-MiniLM-L6-v2`
- Vector store: ChromaDB, persistent, directory from `CHROMA_DIR`
- Generation: Google Gemini (free tier) via the google-genai SDK (`GEMINI_MODEL`, default `gemini-3.6-flash`)
- Backend: FastAPI (served with uvicorn)
- Frontend: plain HTML/CSS/JS served from `/static`, no build step
- Config: python-dotenv, loaded into a settings object in `app/config.py`
- Settings via `.env` (see `.env.example`): `GEMINI_API_KEY`, `GEMINI_MODEL`, `EMBED_MODEL`, `CHROMA_DIR`, `SIMILARITY_THRESHOLD`
- Free-tier Gemini has rate limits. Wrap every LLM call with retry plus exponential backoff for 429/503, and show a clear message if it still fails. Never call the LLM in tests; the retrieval-only evaluation must not use it.
- Rule 4 (validate with a real Gemini call) means manual validation runs; "never call the LLM in tests" applies to pytest and the retrieval evaluation.
- `faker` and `rank_bm25` are intentionally not installed yet. Add `faker` when building dataset generation, `rank_bm25` only if hybrid search is wanted.

## Folder structure
```
ops-rag/
├── .env.example
├── .gitignore
├── CLAUDE.md
├── README.md
├── requirements.txt
├── app/
│   ├── __init__.py
│   ├── config.py        # settings object (python-dotenv), PROJECT_ROOT
│   ├── vectorstore.py   # Chroma collection, embedder, build_where, search
│   ├── ingest.py        # ticket -> document + metadata, batched upsert
│   ├── prompt.py        # SYSTEM_PROMPT, build_prompt (tickets as delimited data; no LLM)
│   ├── llm.py           # Gemini client, generate_answer (JSON schema, own retry, SDK retry off)
│   ├── rag.py           # answer_question: exact-id lookup + vector search, citation validation; title_of, snippet_of
│   ├── schemas.py       # Pydantic request/response models for the API (validation limits)
│   └── main.py          # FastAPI app: lifespan, CORS, security headers, error handling, routes, static mount
├── scripts/
│   ├── ticket_scenarios.py   # hand-written scenario templates and slot pools (data only)
│   ├── generate_tickets.py   # fills the templates, writes data/*, runs the data-quality checks
│   ├── ingest.py             # CLI: index data/tickets.json into Chroma (--reset)
│   ├── try_search.py         # CLI: run a search with optional filters
│   └── ask.py                # CLI: ask questions (interactive or --once), calls Gemini
├── data/
│   ├── tickets.json     # 800 tickets (OPS-1001 to OPS-1800)
│   ├── tickets.csv      # same tickets, comments flattened to a JSON string
│   └── clusters.json    # cluster membership, near-duplicate pairs, vague ticket ids
├── chroma_db/           # persistent vector store (generated, gitignored)
├── .cache/huggingface/  # embedding model download (generated, gitignored)
├── static/              # frontend: index.html, style.css, app.js, favicon.svg (plain HTML/CSS/JS, no build step)
└── tests/               # pytest tests: test_security_headers.py (real app on a temp COPY of chroma_db via CHROMA_DIR, model stubbed, no Gemini)
```

## Commands
Run from the `ops-rag/` folder.

```
# Create venv and install (once)
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt

# Activate venv
.venv\Scripts\Activate.ps1        # PowerShell
source .venv/Scripts/activate     # Git Bash

# Regenerate the dummy dataset (writes data/tickets.json, tickets.csv, clusters.json)
python scripts/generate_tickets.py

# Run ingestion (first run downloads the ~90 MB embedding model into .cache/)
python scripts/ingest.py --reset     # drop and rebuild the collection
python scripts/ingest.py             # re-run in place (idempotent, upserts by ticket id)

# Try a search (optional: --k, --category, --priority, --status, --team, --date-from, --date-to)
python scripts/try_search.py "query"

# Ask a question (makes a real Gemini call; optional: --k, --category, --priority, --status, --team)
python scripts/ask.py                       # interactive, keeps history; type "exit" to quit
python scripts/ask.py --once "question"     # single question, prints counters, then exits

# Run the API server. Check first that port 8000 is free: another project on this machine
# may use it (empty output from the next line means free). If it is busy, use another port.
netstat -ano | findstr :8000
uvicorn app.main:app --port 8000 --no-access-log     # the app logs its own one line per request

# Run tests (test_security_headers.py needs chroma_db/, data/tickets.json and the local embedding model; it works on a temp copy of chroma_db, stubs the model and never calls Gemini)
pytest
```

## API
JSON in and out. Errors are `{"error": "...", "code": "..."}`; validation errors (422) use FastAPI's
normal `detail` list. Interactive docs are at `/docs`.

- `GET /health`: `{status, indexed_tickets, model, key_configured}`; status is `degraded` when the index is empty or the tickets file is missing.
- `GET /filters`: distinct category, priority (P1 to P4), status and team values, plus `created_min` and `created_max`.
- `POST /search`: raw vector search for debugging, no LLM. Returns every hit with `similarity` and `above_threshold`.
- `GET /tickets/{id}`: the full ticket record. The id must look like `OPS-1001` (422 if malformed, 404 if missing).
- `POST /chat`: grounded answer from `answer_question`. The only route that can call Gemini.
- `GET /` and everything else: static files from `static/` (the chat UI); an unknown path is a 404 JSON.
- Security headers (`add_security_headers` in `app/main.py`): every response except `/docs`, `/docs/oauth2-redirect`, `/redoc` and `/openapi.json` carries `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`, `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`. The docs are left alone because Swagger UI and ReDoc load their scripts from a CDN. The middleware sits between CORS (outermost) and the request logger, so the 404, 422, 429, 503 and 500 responses carry the headers too and CORS keeps working; a CORS preflight is answered by CORS itself and carries none of them. Consequence for the frontend: no inline script, inline event handler, `style=` attribute, `eval` or string timer, and nothing from another origin (fonts, CDN scripts, images); `app.js` and `style.css` only toggle classes. Tested offline (`tests/test_security_headers.py`) and in headless Edge on 2026-10-27: zero policy violations across the whole UI run, and inline script, inline handler, eval, string timer, inline style attribute and foreign fetch and image are each blocked, and a page on another origin could not frame the app (frame-ancestors; the same framing works against a copy served without the headers).
- Quota guard (`app/llm.py`): after a Gemini 429 whose retry delay is above 120 s ("quota used up"), the reset time is remembered per model name. Until it passes, `/chat` questions that need the model are refused at once with 429 and a `Retry-After` header (whole seconds, at most one day), with no Gemini request. Questions that need no model are still answered. Tested offline only; not yet against a real 429.

### Known limits / follow-ups
- The `llm_call` field in the `/chat` log line is derived from `retrieved_count`. It should become a real flag in the `answer_question` result.
- Wrong-method requests to an API route (for example `GET /chat`) return 404 instead of 405, because the static mount at `/` matches first.
- Anything placed in `static/` is public (this includes `.gitkeep`).
- The policy is enforced, not report-only, and there is no violation reporting endpoint. Safari does not use `favicon.svg` (it asks for `/favicon.ico` and gets the JSON 404). Swagger UI and ReDoc were not rendered in the browser test (their CDN requests were blocked on purpose); only that they get no security header and raise no violation was checked. If the docs paths are ever changed, update `NO_SECURITY_HEADERS_PATHS`.
- A client disconnect does not cancel a running `/chat` call; the Gemini call and its quota use still happen.
- `/chat` was tested with four real Gemini calls on 2026-10-26 (`gemini-3.6-flash`): the VPN question, the root-cause follow-up, and the vendor question with and without history. Findings: the VPN answer slightly over-attributed a cause (two tickets cited for a firmware cause they do not state); the root-cause follow-up ignored the history and answered for six unrelated tickets; the vendor question could not be answered (aggregation: only 6 tickets are retrieved, and the likely answer, Lotus Stretch Wrap with 3 tickets whose titles say delayed or late, was not among them). The prompt rules, the follow-up retrieval rule and the ticket details line were changed or added after these calls and were re-tested with 3 real calls (see the retest note below).
- Gemini calls are serialised per model (one HTTP attempt at a time), so a hung request delays others for up to the 60 s timeout.
- The quota guard forgets on restart (it is in memory only).
- Follow-ups: when the new question names no ticket, the ticket from an earlier user turn is kept first in the context. On a topic change that ticket still takes slot 1, and the rest of the context comes from searching with the new question alone.
- How far back the inheritance looks: the most recent user turn that named a ticket within the last 6 history messages. A later user turn that was a topic change (by the same rule) ends the inheritance, and so does a ticket that does not exist. Ids that appear only in assistant replies are ignored.
- Inherited tickets ignore the sidebar filters (like any ticket fetched by id), so with a filter set the inherited ticket can still appear first even if it does not match.
- The follow-up rule (`about_inherited_ticket` in `app/rag.py`) is word-list based, not semantic: a question made only of filler words, ticket-property words and words from the inherited ticket's own text is treated as being about that ticket, and any other word counts as a topic change, so unusual wording can end the inheritance.
- Each ticket in the prompt now has one details line (assignee, team, reporter, created, resolved), taken from `data/tickets.json` and not from the embedded document. The file is read once per process, on the first question, so regenerating it needs a restart. It reached the model in all 3 retest calls and the model used it.
- Retest on 2026-10-26 (`gemini-3.6-flash`, 3 real `/chat` calls after the prompt, follow-up retrieval and details-line changes). Call 1, "What was the root cause?" after the OPS-1001 turn, passed: the context was OPS-1001 alone with one correct citation (earlier it answered for six unrelated tickets). Call 2, "Who fixed it?", named the assignee and team from the details line and cited OPS-1001, but over-attributed the fix to the assignee (the ticket text names nobody). Call 3, the vendor question, got a clean topic-change search (OPS-1001 first, then vendor-delay tickets) and an honest `found: false`; it is still unanswerable (aggregation: only 6 tickets are retrieved). No 429 occurred, so the quota guard is still untested against a real one. The newest system-prompt rule (an action is attributed to someone only if the ticket's text says so) has NOT been tested on real Gemini.

## Chat UI
Files in `static/`, served at `/` by the static mount: `index.html`, `style.css`, `app.js`, `favicon.svg`. Plain HTML, CSS and JS: no build step, no framework, no external requests, no inline script or style, no `innerHTML` (everything that comes from the API is shown as text). The Content-Security-Policy (see the API section) enforces this in the browser.
- Features: chat thread with markdown-lite answers; ticket chips only for tickets that were retrieved (other ids are shown as muted text with a marker); a callout for references that were not found; sources with cited tickets first; a ticket drawer (native modal `<dialog>`) fed by `GET /tickets/{id}`; sidebar filters (category, priority, status, team, dates, results per question) that are sent with each question; follow-up history (last 12 messages, only after a successful answer); one request at a time with a 150 s timeout; errors as an inline bubble with Retry and, for a 429, a countdown from `Retry-After`; New chat; light and dark theme; a slide-over filter panel on narrow screens (header and page are inert while it is open); skip link.
- Tested: the pure functions and the DOM logic with Node suites on a fake DOM, each with deliberate-break controls; and in headless Edge 155 on 2026-10-27 against a stub server (real retrieval on a COPY of `chroma_db`, stubbed model, in-memory hostile ticket): about 140 checks covering first load, filters, every answer kind, the drawer, 429 (countdown, `Retry-After`), 503, offline and slow replies, New chat, history, hostile text in answers and tickets, narrow screens (390 and 360 px), dark mode, keyboard operation, and the CSP (0 violations, positive and negative controls, framing refused). Every screenshot was looked at.
- Those Node suites and the browser harness live only in a scratch folder outside the repository and are not part of `pytest`. Only `tests/test_security_headers.py` is in the repo.
- NOT verified:
  - The UI with real Gemini answers after the prompt, follow-up retrieval and details-line changes. The browser run used stub answers; the real calls on 2026-10-26 went through the CLI and the API, not the UI. The newest system-prompt rule (who performed an action) and a real 429 against the quota guard are still untested.
  - Browsers other than Edge 155 (Firefox, Safari, Chrome), and the Claude desktop built-in browser.
  - Screen readers (NVDA, JAWS, VoiceOver): only roles, names, `aria` attributes and focus order were checked, not what is announced.
  - Touch input, a real phone (narrow widths were an emulated viewport on desktop Edge), and input methods (IME composition is covered only by simulated events).
  - Swagger UI and ReDoc rendering (see the API section).
  - Opening a ticket drawer for an id that cannot be retrieved (OPS-9999): the UI cannot reach it; the 404 path was tested with a ticket that is in the index but not served.

## Dataset
The dataset is seeded (seed 42) with a fixed "as of" date (2026-10-25), so it is reproducible: running
`python scripts/generate_tickets.py` again with unchanged code (and the same Python version) rewrites
byte-identical files. Any edit to `scripts/ticket_scenarios.py` or `scripts/generate_tickets.py` changes
the data and its ticket/cluster IDs, so it needs approval first. `generate_tickets.py` runs its own
data-quality checks and fails instead of writing bad data.

## Working rules (HILP)
1. Plan before code. For every task, first reply with a plan only: files to create/change, order, approach, assumptions, and open questions. Write NO code until I reply "approved".
2. One file at a time. Implement one file, show me the diff, then STOP and wait for my confirmation before the next file.
3. Surface ambiguity. If the spec is unclear, ask. Never silently pick an interpretation.
4. Validate against the real system: real Chroma store, real Gemini call, real running server. Synthetic or mocked tests alone don't count as validation. Show me the actual output.
5. Report honestly. State partial results, failures, and what you did NOT verify. After writing any file, confirm it is non-empty on disk (wc -l).
6. Clean up. Remove test collections, temp files, test records and stray processes (check port 8000 is free before starting the server).
7. No scope creep. Only do what the current step asks.
