# ops-rag

A RAG chat system over a dummy "Operations Tickets" dataset (800 tickets).

**Status:** the dummy dataset is generated, indexed in ChromaDB, and searchable with metadata filters (no LLM calls yet). Gemini answer generation, the API server and the chat UI are not built yet.

## Stack
Python 3.11+, sentence-transformers (`all-MiniLM-L6-v2`), ChromaDB, Gemini (google-genai, free tier), FastAPI, plain HTML/CSS/JS.

## Setup
```
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env    # then set GEMINI_API_KEY
```

See [CLAUDE.md](CLAUDE.md) for the folder structure, commands and working rules.
