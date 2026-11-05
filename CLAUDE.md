# ops-rag

RAG system over a dummy "Operations Tickets" dataset (800 tickets) with a web chat interface.
Current status: Dataset generated, indexed in ChromaDB, answerable from the command line with Gemini (grounded answers with validated citations), and served by a FastAPI backend (health, filters, search, ticket detail, chat). The backend is validated against a running server over real sockets: health, filters, search, tickets, input validation, CORS, path traversal, the empty-collection startup warning, and the `/chat` paths that need no model. On 2026-10-26 four real `/chat` calls succeeded on `gemini-3.6-flash` (the VPN question, the root-cause follow-up, and the vendor question with and without history). Findings: the VPN answer slightly over-attributed a cause (it cited two tickets for a firmware cause they do not state), the root-cause follow-up ignored the history, and the vendor question could not be answered (aggregation: only 6 of 800 tickets are retrieved). The prompt rules, the follow-up retrieval rule and the ticket details line were changed or added after those calls and re-tested with 3 real calls (see the retest note under the follow-ups); the newest system-prompt rule has NOT been tested on real Gemini. The chat UI is built (`static/`, see the Chat UI section) and was tested in headless Edge 155 against a stub server; a Content-Security-Policy is in place. NOT verified: the UI with real Gemini answers after the prompt and retrieval changes (the browser run used stub answers), Firefox and Safari, screen readers, touch and IME input. Real Gemini was spot-checked on 2026-10-26 and 2026-10-27, not benchmarked (the 2026-10-27 calls are not recorded in this file). Tests and evaluation: see "Phase 6 status" below and the section "Tests, evaluation and findings (Phase 6)".

Phase 3 status (RAG core, CLI only):
- Validated on real Gemini: the VPN question, the OPS-1001 lookup, the OPS-1001 follow-up, the P1 filter (`--priority P1-Critical`). These ran on `gemini-3.6-flash`, except the P1 filter, which ran on `gemini-3.5-flash`.
- Validated without a model call: off-topic refusal, OPS-9999 (not-found answer), the prompt-injection escaping path, retrieval for inherited-id follow-ups.
- Real Gemini on 2026-10-26 (`gemini-3.6-flash`): four `/chat` calls succeeded (VPN question, root-cause follow-up, vendor question with and without history); the findings are in the status line above.
  - The changed prompt rules, follow-up retrieval and ticket details line were re-tested with 3 real calls later that day (see the follow-ups below). The newest system-prompt rule (who performed an action) has NOT been tested on real Gemini.
- Real Gemini was also spot-checked on 2026-10-27 (not recorded in this file; see README "Known limits"). The "who performed an action" rule is not confirmed on real Gemini.
- Known limits:
  - Vector search ranks exact ids poorly (handled by the exact-id lookup in `app/rag.py`).
  - Aggregation questions ("which vendor has the most...") are weak. The counting route (Phase 7c) is planned and NOT built; no code does counting.
  - Free-tier Gemini has small daily quotas.

Phase 6 status (tests and evaluation, as of 2026-11-05):
- Python tests: 11 files, 1,350 collected (1,345 Python tests + 5 wrapper tests that run the Node suites); the last full run gave 1349 passed, 1 skipped. Per-file counts are in the Tests section below.
- Front-end tests: 238 Node tests in `tests/ui` (fake DOM, not a browser), run by `tests/test_ui_node.py` or `node tests/ui/run_all.js`; Node v26.8.1 is the only version tested.
- "New chat" moved from the filter sidebar into the header (same id and behaviour) on 2026-11-05; `tests/ui/test_inert.js` now has 10 tests (one checks the placement).
- Golden set: 33 questions in `tests/golden_questions.json`, frozen on 2026-10-27, pinned by a sha256 in `tests/test_golden_labels.py` (`PIN_HISTORY`; every accepted edit needs one entry in `meta.changes_after_scores`).
- Evaluation: `python scripts/evaluate.py` (retrieval only, stub model); results in `docs/evaluation.md`. It says nothing about answer quality or citation correctness.
- Break controls: the Node tests have 107 mutations in `tests/ui/breaks` (kept in the repository). The Python test files were break-tested on temporary copies; those scripts are NOT kept.
- Findings F1 to F11 and their status: see the Tests section below.

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
- `rank_bm25` is intentionally not installed; add it only if hybrid search is wanted. `faker` is not used: the dataset generator does not need it.

## Folder structure
```
ops-rag/
├── .env.example
├── .gitignore
├── CLAUDE.md
├── LICENSE
├── README.md
├── pytest.ini
├── requirements.txt
├── requirements.lock.txt
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
│   ├── ask.py                # CLI: ask questions (interactive or --once), calls Gemini
│   └── evaluate.py           # CLI: retrieval-only evaluation of tests/golden_questions.json (stub model, no Gemini)
├── data/
│   ├── tickets.json     # 800 tickets (OPS-1001 to OPS-1800)
│   ├── tickets.csv      # same tickets, comments flattened to a JSON string
│   └── clusters.json    # cluster membership, near-duplicate pairs, vague ticket ids
├── chroma_db/           # persistent vector store (generated, gitignored)
├── .cache/huggingface/  # embedding model download (generated, gitignored)
├── static/              # frontend: index.html, style.css, app.js, favicon.svg (plain HTML/CSS/JS, no build step)
├── docs/
│   ├── evaluation.md    # retrieval evaluation report (generated by scripts/evaluate.py; regenerated by the owner)
│   └── img/             # screenshots used in the README
└── tests/
    ├── conftest.py      # temp COPY of chroma_db (prefix oh), stubbed model, guard against a real Gemini client
    ├── test_*.py        # pytest files (11, see the Tests section for counts); none calls Gemini
    ├── golden_questions.json   # the frozen evaluation set (hash-pinned by test_golden_labels.py)
    └── ui/              # Node.js front-end tests: fakedom.js, test_{pure,dom,inert,chat,drawer}.js, run_all.js
        └── breaks/      # their mutation checks: harness.js and mutate_{pure,dom,inert,chat,drawer}.js
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

# Run tests. The Python tests need chroma_db/, data/tickets.json and the local embedding model; they work on a temp copy of chroma_db,
# stub the model and never call Gemini. pytest also runs the 5 Node wrapper tests (skipped with a reason when node is missing).
pytest
node tests/ui/run_all.js                    # the 238 front-end tests alone (Node, no packages; tested with v26.8.1 only)
node tests/ui/breaks/mutate_chat.js         # mutation checks of one suite (also mutate_pure/dom/inert/drawer); temp copies only

# Retrieval-only evaluation (stub model, no Gemini call, no key; needs the index). Prints the report on stdout;
# --out PATH also writes it. docs/evaluation.md is regenerated by the owner, not by tests.
python scripts/evaluate.py
```

## API
JSON in and out. Errors are `{"error": "...", "code": "..."}`; validation errors (422) use FastAPI's
normal `detail` list. A body with a lone surrogate (JSON `"\ud800"`) or a non-finite number (`NaN`, `Infinity`, `1e999`) is also a normal 422 (`validation_error_handler` in `app/main.py`): the surrogate is written as an ASCII escape and the number as the string `"NaN"`, `"Infinity"` or `"-Infinity"` (findings F9 and F10, fixed). Interactive docs are at `/docs`.

- `GET /health`: `{status, indexed_tickets, model, key_configured}`; status is `degraded` when the index is empty or the tickets file is missing.
- `GET /filters`: distinct category, priority (P1 to P4), status and team values, plus `created_min` and `created_max`.
- `POST /search`: raw vector search for debugging, no LLM. Returns every hit with `similarity` and `above_threshold`.
- `GET /tickets/{id}`: the full ticket record. The id must look like `OPS-1001` (422 if malformed, 404 if missing).
- `POST /chat`: grounded answer from `answer_question`. The only route that can call Gemini.
- `GET /` and everything else: static files from `static/` (the chat UI); an unknown path is a 404 JSON.
- Security headers (`add_security_headers` in `app/main.py`): every response except `/docs`, `/docs/oauth2-redirect`, `/redoc` and `/openapi.json` carries `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`, `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`. The docs are left alone because Swagger UI and ReDoc load their scripts from a CDN. The middleware sits between CORS (outermost) and the request logger, so the 404, 422, 429, 503 and 500 responses carry the headers too and CORS keeps working; a CORS preflight is answered by CORS itself and carries none of them. Consequence for the frontend: no inline script, inline event handler, `style=` attribute, `eval` or string timer, and nothing from another origin (fonts, CDN scripts, images); `app.js` and `style.css` only toggle classes. Tested offline (`tests/test_security_headers.py`, and the 422 and error paths in `tests/test_api_validation.py` and `tests/test_errors.py`) and in headless Edge on 2026-10-27: zero policy violations across the whole UI run, and inline script, inline handler, eval, string timer, inline style attribute and foreign fetch and image are each blocked, and a page on another origin could not frame the app (frame-ancestors; the same framing works against a copy served without the headers).
- Quota guard (`app/llm.py`): after a Gemini 429 whose retry delay is above 120 s ("quota used up"), the reset time is remembered per model name. Until it passes, `/chat` questions that need the model are refused at once with 429 and a `Retry-After` header (whole seconds, at least 1, at most one day), with no Gemini request. A wait hint from Gemini (RetryInfo or `Retry-After`) counts for at most one day (`MAX_HINT = 86400.0` in `app/llm.py`, `_usable_wait`), so the remembered reset time and the "about N hours" text stay bounded; a hint that is negative or not finite is ignored (no hint). Questions that need no model are still answered. Tested offline only (`tests/test_quota_guard.py`, `tests/test_errors.py`); not yet against a real 429.

### Known limits / follow-ups
- There is no limit on the size of a request body: it is read whole before validation (a 20 MB question got a 422 in about 0.1 s; the only limit is the 1000-character field limit). The size of the 422 response for such input was NOT measured; the 422 echoes the rejected input.
- Finding F11 (open, unfixed, untested): a JSON body nested about 900 to 990 levels deep gets the generic 500 `internal_error` instead of a 422 (depth 900 gives a 422, depth 990 a 500; the exact depth was not located). Found by a probe in a scratch folder that is not in the repository.
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
- Tested: the pure functions and the DOM logic with the Node suites in `tests/ui` (238 tests on a fake DOM), each with deliberate-break controls (107 mutations in `tests/ui/breaks`); and in headless Edge 155 on 2026-10-27 against a stub server (real retrieval on a COPY of `chroma_db`, stubbed model, in-memory hostile ticket): about 140 checks covering first load, filters, every answer kind, the drawer, 429 (countdown, `Retry-After`), 503, offline and slow replies, New chat, history, hostile text in answers and tickets, narrow screens (390 and 360 px), dark mode, keyboard operation, and the CSP (0 violations, positive and negative controls, framing refused). Every screenshot was looked at.
- The Node suites are in the repository (`tests/ui`) and run under `pytest` through `tests/test_ui_node.py` (skipped with a reason when `node` is missing). The headless-Edge browser harness lives only in a scratch folder outside the repository.
- NOT verified:
  - The UI with real Gemini answers after the prompt, follow-up retrieval and details-line changes. The browser run used stub answers; the real calls on 2026-10-26 and 2026-10-27 went through the CLI and the API code path, not the UI. The newest system-prompt rule (who performed an action) and a real 429 against the quota guard are still untested.
  - Browsers other than Edge 155 (Firefox, Safari, Chrome), and the Claude desktop built-in browser.
  - Screen readers (NVDA, JAWS, VoiceOver): only roles, names, `aria` attributes and focus order were checked, not what is announced.
  - Touch input, a real phone (narrow widths were an emulated viewport on desktop Edge), and input methods (IME composition is covered only by simulated events).
  - Swagger UI and ReDoc rendering (see the API section).
  - Opening a ticket drawer for an id that cannot be retrieved (OPS-9999): the UI cannot reach it; the 404 path was tested with a ticket that is in the index but not served.

## Tests, evaluation and findings (Phase 6)
### Tests (counts as of 2026-11-05, from `pytest --collect-only -q`)
| File | Tests |
|---|---|
| `tests/test_api_validation.py` | 324 |
| `tests/test_citations.py` | 36 |
| `tests/test_errors.py` | 93 |
| `tests/test_exact_lookup.py` | 75 |
| `tests/test_filters.py` | 140 |
| `tests/test_followups.py` | 66 |
| `tests/test_golden_labels.py` | 208 |
| `tests/test_prompt_escaping.py` | 170 |
| `tests/test_quota_guard.py` | 207 |
| `tests/test_security_headers.py` | 26 |
| `tests/test_ui_node.py` (wrapper, see below) | 5 |
| **Collected** | **1,350** |

- Python tests: 1,345. The other 5 are wrapper tests, one per Node suite. The 238 Node tests (116 pure functions, 38 DOM, 10 inert panel, 47 chat, 27 drawer) run inside those 5 and are not counted by pytest.
- Last full `pytest` run: 1349 passed, 1 skipped. The skip is in `test_exact_lookup.py`: a test with nothing to check in this data set (no PO or INV id appears only in comments).
- No test calls Gemini: `tests/conftest.py` stubs the model, sets an empty key and fails any test that creates a real client. The Python tests run on a temp COPY of `chroma_db/` (prefix `oh`, short on purpose because of the Windows path limit).
- Tests named `pinned_*` record UNWRITTEN behaviour (no rule says what should happen). In `test_prompt_escaping.py` and the non-finite-number tests each one names its finding. A strict `xfail` that unexpectedly passes counts as a failure.
- `tests/test_golden_labels.py` checks the golden set against the data: type counts, ids, clusters, near-duplicate pairs, vague tickets, filter values, text rules, authorship wording, `frozen`/`frozen_on` (a real `YYYY-MM-DD` date not earlier than the newest `created_at`), and the `evaluate.py` frozen guard. It pins the file's LF-normalised sha256 in `PIN_HISTORY`, and requires `len(meta.changes_after_scores) == len(PIN_HISTORY) - 1`. The entry shape of `changes_after_scores` (an object with exactly `date`, `id`, `field`, `old`, `new`, `reason`) is a rule INTRODUCED BY THAT TEST; no earlier document defined it.
- To change the golden file on purpose: add a change-log entry, recompute the hash (the entry changes it), append it to `PIN_HISTORY`, and regenerate `docs/evaluation.md`.

### Evaluation
- The golden set has 33 questions: 4 exact id, 10 cluster, 3 vague, 4 near-duplicate, 4 with filters, 3 known weak (2 aggregation, 1 vocabulary) and 5 extra (assistant-drafted, not blind). It is frozen on 2026-10-27.
- Results: `docs/evaluation.md`. Headline without the extra type (n = 24): k = 6 hit 1.00, recall 0.83, capped recall 0.83, MRR 0.74; k = 3 hit 0.79, recall 0.66; k = 10 hit 1.00, recall 0.93. A re-run on 2026-11-05 reproduced the report apart from the run date.
- Weak spots: counting questions G23 and G24 (G23 finds 0 of 3 at k = 3, 6 and 10); G09 (3 of 6 at k = 6 and k = 10); A05 (1 of 14 at k = 10). G25 (vocabulary) did not fail (n = 1). G07 has unlabelled competing tickets (OPS-1335, 1385, 1408, 1451 and OPS-1220), found from the data.
- Limits: the model is a stub, so there is no claim about answer quality or citation correctness; `expected` is a lower bound; the 5 extra questions are not blind and are reported on their own row.

### Break controls
- Node: 107 mutations in `tests/ui/breaks` (12 pure, 20 DOM, 6 inert, 38 chat, 31 drawer). Each breaks a COPY of `static/app.js` in a temp folder and a suite must fail; an anchor that does not match exactly once is recorded INVALID. Last run (2026-11-05): 107 applied, 107 caught, 0 weak, 0 INVALID, 0 missed.
- Python: break-tested on temporary copies when each test file was written (the anchor must match exactly once, the mutated text must differ, the file on disk must equal it, otherwise INVALID). The scripts are NOT kept in the repository.

### Findings (F-labels come from the plan of `tests/test_prompt_escaping.py` and from later review)
| Finding | What | Status | Where |
|---|---|---|---|
| F1 | The user's question is neither escaped nor delimited in the prompt | pinned | `tests/test_prompt_escaping.py` |
| F2 | History escaping covers only the history tags | pinned | `tests/test_prompt_escaping.py` |
| F3 | History lines can be forged with newlines; client-supplied assistant turns look like any other | pinned | `tests/test_prompt_escaping.py` |
| F4 | The `id` attribute of a ticket block is not escaped (ids come from the store) | pinned | `tests/test_prompt_escaping.py` |
| F5 | Escapes are exact and case-sensitive (look-alikes pass) | pinned | `tests/test_prompt_escaping.py` |
| F6 | Ticket documents: no length cap, no control-character cleaning, no secret scan | pinned | `tests/test_prompt_escaping.py` |
| F7 | Unverified ids are only stripped and upper-cased | pinned | `tests/test_prompt_escaping.py` |
| F8 | The client controls its own history | not tested (nothing to observe) | `tests/test_prompt_escaping.py` docstring |
| F9 | A lone surrogate in a request gave a 500 instead of a 422 | fixed | `tests/test_api_validation.py`, section "A lone surrogate in a request body" |
| F10 | `NaN`, `Infinity` or `1e999` in a request gave a 500 instead of a 422 | fixed | `tests/test_api_validation.py`, section on non-finite numbers |
| F11 | JSON nested about 900 to 990 levels deep gives a generic 500 | open, unfixed, untested | none |

F1 to F8 are listed in the module docstring of `tests/test_prompt_escaping.py`; F10 is named in `tests/test_api_validation.py`. The label F9 is verified only in an earlier version of the `test_prompt_escaping.py` docstring (commit 17cc3b0) and is no longer in the current files; F11 is a label given in review and appears in no test.

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
