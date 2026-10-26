# ops-rag

RAG system over a dummy "Operations Tickets" dataset (800 tickets) with a web chat interface.
Current status: Dataset generated, indexed in ChromaDB, answerable from the command line with Gemini (grounded answers with validated citations), and served by a FastAPI backend (health, filters, search, ticket detail, chat). The backend is validated against a running server over real sockets: health, filters, search, tickets, input validation, CORS, path traversal, the empty-collection startup warning, and the `/chat` paths that need no model. On 2026-10-26 four real `/chat` calls succeeded on `gemini-3.6-flash` (the VPN question, the root-cause follow-up, and the vendor question with and without history). Findings: the VPN answer slightly over-attributed a cause (it cited two tickets for a firmware cause they do not state), the root-cause follow-up ignored the history, and the vendor question could not be answered (aggregation: only 6 of 800 tickets are retrieved). The prompt rules, the follow-up retrieval rule and the ticket details line were changed or added after those calls and re-tested with 3 real calls (see the retest note under the follow-ups); the newest system-prompt rule has NOT been tested on real Gemini. The chat UI is only started: `static/index.html` and `static/style.css` exist, `static/app.js` does not yet.

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
│   └── main.py          # FastAPI app: lifespan, CORS, error handling, routes, static mount
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
├── static/              # frontend: HTML/CSS/JS (only .gitkeep until Phase 5)
└── tests/               # pytest tests (empty for now)
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

# Run tests            (NO TESTS YET)
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
- `GET /` and everything else: static files from `static/` (404 JSON until Phase 5).
- Quota guard (`app/llm.py`): after a Gemini 429 whose retry delay is above 120 s ("quota used up"), the reset time is remembered per model name. Until it passes, `/chat` questions that need the model are refused at once with 429 and a `Retry-After` header (whole seconds, at most one day), with no Gemini request. Questions that need no model are still answered. Tested offline only; not yet against a real 429.

### Known limits / follow-ups
- The `llm_call` field in the `/chat` log line is derived from `retrieved_count`. It should become a real flag in the `answer_question` result.
- Wrong-method requests to an API route (for example `GET /chat`) return 404 instead of 405, because the static mount at `/` matches first.
- Anything placed in `static/` is public (this includes `.gitkeep`).
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
