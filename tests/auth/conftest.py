"""Fixtures for the tests in tests/auth only (they are not visible to the other test folders).

They sit on top of tests/conftest.py, which still applies to these tests (its session Chroma copy and the guard against a real Gemini client). What is added here:
  * clock: a FakeClock. The auth code takes its clock as an argument, so no test needs the real time.
  * settings_factory / auth_settings: AuthSettings with a cheap scrypt cost (N=2**10, a few milliseconds) and a database under pytest's tmp_path.
  * an autouse guard that makes time.sleep fail the test (a test that waits for the real clock must not pass),
  * an autouse guard that fails a test if it created <repo>/var (the default database folder): databases must live under tmp_path,
  * derive_counter: counts scrypt work by wrapping passwords._derive (the only caller of hashlib.scrypt); never by timing,
  * make_service: builds an AuthService (and its Store, on a fresh database under tmp_path, with the cheap cost and a roomy hashing gate) around the clock,
  * an autouse reset of the process hashing gate, so no test sees the gate another one configured.
"""
import dataclasses
import hashlib
import sqlite3
import time
from pathlib import Path

import pytest

from app.auth import passwords
from app.auth.config import AuthSettings
from app.auth.service import AuthService
from app.auth.store import Store

PROJECT_VAR = Path(__file__).resolve().parents[2] / "var"
CLOCK_START = 1_700_000_000.0


class FakeClock:
    """A callable clock that only moves when the test says so."""

    def __init__(self, start=CLOCK_START):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        assert seconds >= 0, "time does not run backwards in these tests"
        self.now += seconds
        return self.now


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def settings_factory(tmp_path):
    """make(**overrides) -> AuthSettings: enabled, cheap scrypt, database at tmp_path/auth.db. Everything can be overridden."""

    def make(**overrides):
        values = {"enabled": True, "db_path": tmp_path / "auth.db", "scrypt_n": 2**10}
        values.update(overrides)
        return AuthSettings(**values)

    return make


@pytest.fixture
def auth_settings(settings_factory):
    return settings_factory()


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    def refuse(seconds=0, *args, **kwargs):
        raise AssertionError(f"time.sleep({seconds!r}) was called in an auth test: use the FakeClock instead")

    monkeypatch.setattr(time, "sleep", refuse)


@pytest.fixture(autouse=True)
def no_var_folder():
    existed = PROJECT_VAR.exists()
    yield
    assert existed or not PROJECT_VAR.exists(), f"a test created {PROJECT_VAR}: databases must live under tmp_path"


@pytest.fixture(autouse=True)
def reset_process_gate():
    passwords.set_gate(None)
    yield
    passwords.set_gate(None)


class DeriveCounter:
    """Records every scrypt run: (n, r, p, salt). count is how many; reset() forgets them."""

    def __init__(self, real):
        self.real = real
        self.calls = []

    @property
    def count(self):
        return len(self.calls)

    def reset(self):
        self.calls.clear()

    def __call__(self, password_bytes, salt, n, r, p):
        self.calls.append((n, r, p, salt))
        return self.real(password_bytes, salt, n, r, p)


@pytest.fixture
def derive_counter(monkeypatch):
    counter = DeriveCounter(passwords._derive)
    monkeypatch.setattr(passwords, "_derive", counter)
    return counter


class Env:
    """An AuthService with its Store, clock and settings, plus a few helpers that read the database directly."""

    def __init__(self, svc, store, clock, settings, gate):
        self.svc, self.store, self.clock, self.settings, self.gate = svc, store, clock, settings, gate

    def sql(self, query, args=()):
        conn = sqlite3.connect(self.settings.db_path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            rows = conn.execute(query, args).fetchall()
            conn.commit()
            return rows
        finally:
            conn.close()

    def db_bytes(self):
        return b"".join(p.read_bytes() for p in self.settings.db_path.parent.glob(self.settings.db_path.name + "*"))

    @staticmethod
    def digest(text):
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def add_local_user(self, name, password):
        """A local account made through the store (no sign-up): the hash uses this env's settings."""
        return self.store.add_user(name, passwords.hash_password(password, self.settings, gate=self.gate))

    def reopen(self, **overrides):
        """The same database, a new Store and AuthService with changed settings (a restart with another environment)."""
        settings = dataclasses.replace(self.settings, **overrides)
        store = Store(settings.db_path, self.clock)
        svc = AuthService(store, settings, self.clock, gate=self.gate)
        svc.init()
        return Env(svc, store, self.clock, settings, self.gate)


@pytest.fixture
def make_service(tmp_path, clock):
    """make(**settings_overrides, gate=None, token_factory=None) -> Env on a new database. init() has been called."""
    made = []

    def make(*, gate=None, token_factory=None, **overrides):
        values = {"enabled": True, "db_path": tmp_path / f"auth{len(made)}" / "auth.db", "scrypt_n": 2**10}
        values.update(overrides)
        settings = AuthSettings(**values)
        gate = gate or passwords.HashGate(64, 30)
        store = Store(settings.db_path, clock)
        svc = AuthService(store, settings, clock, gate=gate, token_factory=token_factory)
        svc.init()
        env = Env(svc, store, clock, settings, gate)
        made.append(env)
        return env

    return make
