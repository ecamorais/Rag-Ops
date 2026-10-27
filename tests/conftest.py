"""Shared test setup: a temp COPY of chroma_db, a stubbed language model, and a guard against a real Gemini client.

pytest imports this file before any test module, so the copy exists and CHROMA_DIR points at it before app.config is
imported anywhere (app.config reads the environment once). The real chroma_db/ is only read. The copy lives in a short
folder (prefix "oh") in the temp directory, because the Windows path limit is easy to hit with the Chroma file names, and it
is removed when the session ends. The tests need chroma_db/, data/tickets.json and the local embedding model
(HF_HUB_OFFLINE=1, so nothing is downloaded). Nothing here, or in any test, may call Gemini.
"""
import atexit
import gc
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CHROMA_COPY = None  # the temp folder that holds the copy; None when chroma_db/ is missing (the tests then skip themselves)

if (ROOT / "chroma_db").is_dir():
    CHROMA_COPY = Path(tempfile.mkdtemp(prefix="oh"))
    atexit.register(shutil.rmtree, CHROMA_COPY, ignore_errors=True)  # fallback if the session dies before pytest_sessionfinish
    shutil.copytree(ROOT / "chroma_db", CHROMA_COPY / "chroma_db")
    os.environ["CHROMA_DIR"] = str(CHROMA_COPY / "chroma_db")
os.environ["GEMINI_API_KEY"] = ""  # an empty key makes a real call impossible even if the guard below failed
os.environ.setdefault("HF_HUB_OFFLINE", "1")

real_client_touches = []  # every attempt to create or fetch a real Gemini client; must stay empty


def release_chroma():
    """Close the sqlite file of the copy: Windows cannot delete a file that is still open."""
    from chromadb.api.shared_system_client import SharedSystemClient

    vectorstore = sys.modules.get("app.vectorstore")
    SharedSystemClient.clear_system_cache()
    if vectorstore is not None:
        vectorstore._client.cache_clear()
    gc.collect()


def remove_copy(path, attempts=6, pause=0.5):
    """Delete the temp copy. Windows refuses while a file handle is still open, so release Chroma, collect garbage and retry
    a few times. Returns True when the folder is gone; otherwise says so on stderr (never silent)."""
    for attempt in range(attempts):
        release_chroma()
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return True
        time.sleep(pause)
    print(f"\nWARNING: the temp Chroma copy could not be removed after {attempts} attempts: {path}", file=sys.stderr)
    return False


def pytest_sessionfinish(session, exitstatus):
    if CHROMA_COPY is not None:
        remove_copy(CHROMA_COPY)


@pytest.fixture(scope="session", autouse=True)
def session_guard():
    """The copy is what the app opens, the key is empty, and at the end no real Gemini client was ever created."""
    if CHROMA_COPY is None:
        pytest.skip("chroma_db/ is missing: run python scripts/ingest.py first")
    from app import llm
    from app.config import settings

    assert Path(settings.chroma_dir).resolve() == (CHROMA_COPY / "chroma_db").resolve(), (
        f"Chroma must be the copy, not {settings.chroma_dir} (was app.config imported before tests/conftest.py?)")
    assert settings.gemini_api_key == ""
    yield
    assert llm._client is None, "a real Gemini client was created during the session"
    assert real_client_touches == []


@pytest.fixture(autouse=True)
def no_real_client(monkeypatch):
    """Creating or fetching a real client fails the test, even when the code under test swallows the exception: the touch is
    recorded and checked again at teardown. A test that needs a fake client replaces llm.get_client itself."""
    from app import llm

    def refuse(*args, **kwargs):
        real_client_touches.append("touched")
        raise AssertionError("REAL GEMINI CLIENT TOUCHED")

    monkeypatch.setattr(llm, "get_client", refuse)
    monkeypatch.setattr(llm.genai, "Client", refuse)
    before = len(real_client_touches)
    yield
    assert len(real_client_touches) == before, "a test touched the real Gemini client"


class StubModel:
    """Stands in for llm.generate_answer. Records every call; by default answers without citing anything.
    set() changes the answer, fail() makes the call raise."""

    def __init__(self):
        self.calls = []  # (system, prompt) of every call
        self.reply = None
        self.error = None
        self.set()

    def set(self, answer="Stub answer.", found=True, citations=()):
        from app import llm

        self.reply = llm.GeminiAnswer(answer=answer, found=found, citations=list(citations))
        self.error = None

    def fail(self, error):
        self.error = error

    def __call__(self, system, prompt):
        self.calls.append((system, prompt))
        if self.error is not None:
            raise self.error
        return self.reply


@pytest.fixture(autouse=True)
def stub_model(monkeypatch):
    from app import llm

    stub = StubModel()
    monkeypatch.setattr(llm, "generate_answer", stub)
    return stub


@pytest.fixture(scope="module")
def api_client():
    """The real app (real startup, real retrieval on the copy). raise_server_exceptions=False: an unexpected error comes
    back as the app's own 500 response, as in production."""
    from fastapi.testclient import TestClient

    from app import main

    with TestClient(main.app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="session")
def tickets_by_id():
    return {t["ticket_id"]: t for t in json.loads((ROOT / "data" / "tickets.json").read_text(encoding="utf-8"))}
