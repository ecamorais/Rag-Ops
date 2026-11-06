"""app/auth/store.py: the SQLite storage of the optional login.

Expected values come from the approved design (scratchpad auth_design_approved.md and the Phase 2 plan), not from running the code: the five tables and
their columns, the CHECKs and the cascade, "an invite is used in the same transaction that creates the user", the session cap that keeps the newest, the
purge boundaries (expires_at <= now, idle for idle_seconds or more), "the failure key is an HMAC of username|ip with a per-database key", "a failure
window restarts when the last failure is OLDER than the window", "provisioned users follow the environment", and "a newer or unknown schema, a foreign
database or a missing database raises StoreError". Tests marked pinned_ record behaviour the design does not state.
Every database lives under tmp_path. Every raw connection of the tests is closed, and one test removes the folder after heavy use (a handle left open
would fail it on Windows). Nothing sleeps: time comes from the FakeClock, and the concurrency tests join their threads with a limit so a hang fails.
This file has no non-ASCII characters on purpose: other characters are built with chr().
"""
import ast
import dataclasses
import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from app.auth import store as store_module
from app.auth.store import FailureRow, SessionRow, Store, StoreError, UserRow

ROOT = Path(__file__).resolve().parents[2]
STORE_SOURCE = ROOT / "app" / "auth" / "store.py"
NOW = 1_700_000_000


@pytest.fixture
def db(tmp_path):
    return tmp_path / "data" / "auth.db"


@pytest.fixture
def store(db, clock):
    s = Store(db, clock)
    s.init_schema()
    return s


@contextmanager
def raw(path, foreign_keys=True):
    conn = sqlite3.connect(path)
    try:
        if foreign_keys:
            conn.execute("PRAGMA foreign_keys = ON")
        yield conn
        conn.commit()
    finally:
        conn.close()


def dump(path):
    """Every row of every table, for "nothing changed" comparisons."""
    with raw(path) as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {name: sorted(conn.execute(f"SELECT * FROM {name}").fetchall(), key=repr) for name in names}


def uses_left(path, code):
    with raw(path) as conn:
        return conn.execute("SELECT uses_left FROM invites WHERE code_hash = ?", (code,)).fetchone()[0]


# --- the constructor and the import create nothing -----------------------------------------------------------------------------------------------

def test_constructing_a_store_creates_neither_folder_nor_file(db, clock):
    s = Store(db, clock)
    assert s.path == db
    assert not db.parent.exists() and not db.exists()


IMPORT_PROOF = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
reads = []

class Recorder(dict):
    def __getitem__(self, k): reads.append(k); return super().__getitem__(k)
    def get(self, k, d=None): reads.append(k); return super().get(k, d)
    def __contains__(self, k): reads.append(k); return super().__contains__(k)
    def __iter__(self): reads.append("<iter>"); return super().__iter__()

os.environ = Recorder(dict(os.environ))
before = set(sys.modules)
os.chdir(sys.argv[2])
import app.auth.store
imported = sorted(m for m in set(sys.modules) - before if m == "app" or m.startswith(("app.", "dotenv")))
print(json.dumps({"reads": reads, "imported": imported, "files": sorted(os.listdir(".")), "dotenv": "dotenv" in sys.modules}))
"""


def test_importing_the_module_reads_no_environment_loads_only_auth_modules_and_creates_no_file(tmp_path):
    result = subprocess.run([sys.executable, "-B", "-c", IMPORT_PROOF, str(ROOT), str(tmp_path)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-600:]
    info = json.loads(result.stdout.strip().splitlines()[-1])
    assert info == {"reads": [], "imported": ["app", "app.auth", "app.auth.store"], "files": [], "dotenv": False}


# --- schema, idempotence, two Store objects -------------------------------------------------------------------------------------------------------

EXPECTED_COLUMNS = {
    "meta": ["key", "value"],
    "users": ["id", "username", "pw_hash", "created_at", "last_login_at", "disabled", "source"],
    "sessions": ["token_hash", "user_id", "csrf_token", "created_at", "last_seen_at", "expires_at"],
    "invites": ["code_hash", "uses_left", "expires_at", "created_at", "note"],
    "login_failures": ["key", "failures", "last_failure_at", "blocked_until"],
}


def test_init_schema_creates_the_parent_folders_and_the_file(store, db):
    assert db.parent.is_dir() and db.is_file()


def test_the_five_tables_have_the_designed_columns(store, db):
    with raw(db) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
        assert tables == set(EXPECTED_COLUMNS)
        for table, columns in EXPECTED_COLUMNS.items():
            assert [r[1] for r in conn.execute(f"PRAGMA table_info({table})")] == columns, table


def test_the_three_indexes_cover_session_user_session_expiry_and_failure_time(store, db):
    covered = set()
    with raw(db) as conn:
        for table in EXPECTED_COLUMNS:
            for _, name, _, origin, _ in conn.execute(f"PRAGMA index_list({table})"):
                if origin == "c":  # created with CREATE INDEX (not the automatic ones of PRIMARY KEY and UNIQUE)
                    covered |= {(table, r[2]) for r in conn.execute(f"PRAGMA index_info({name})")}
    assert covered == {("sessions", "user_id"), ("sessions", "expires_at"), ("login_failures", "last_failure_at")}


def test_the_meta_table_holds_the_schema_version_and_a_32_byte_hex_key(store, db):
    with raw(db) as conn:
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    assert set(meta) == {"schema_version", "hmac_key"}
    assert meta["schema_version"] == "1"
    assert len(meta["hmac_key"]) == 64 and len(bytes.fromhex(meta["hmac_key"])) == 32


def test_the_default_rollback_journal_is_used_not_wal(store, db):
    with raw(db) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_calling_init_schema_again_from_two_store_objects_changes_nothing(store, db, clock):
    before = dump(db)
    Store(db, clock).init_schema()
    store.init_schema()
    assert dump(db) == before


def test_two_store_objects_on_one_file_see_the_same_data(store, db, clock):
    other = Store(db, clock)
    other.init_schema()
    uid = store.add_user("alice", "hash-a")
    assert other.get_user("alice").id == uid
    assert other.add_user("alice", "x") is None


@pytest.mark.skipif(sys.platform == "win32", reason="os.open() mode bits are not enforced on Windows; the 0600 guarantee only exists where the OS has file modes")
def test_the_database_file_is_not_readable_by_group_or_others_on_posix(store, db):
    assert stat.S_IMODE(os.stat(db).st_mode) & 0o077 == 0


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows pin")
def test_pinned_on_windows_the_file_is_created_but_its_mode_is_not_restricted(store, db):
    """Unwritten behaviour, not a deviation: on Windows os.open() honours only the read-only bit, so no owner-only mode is claimed. The file exists, is a
    regular file and is writable by the creator; its other permissions are whatever its folder gives it."""
    mode = os.stat(db).st_mode
    assert stat.S_ISREG(mode) and mode & stat.S_IWRITE


# --- constraints, uniqueness, foreign keys -----------------------------------------------------------------------------------------------------

BAD_ROWS = [
    ("users.disabled = 2", "INSERT INTO users (username, pw_hash, created_at, disabled) VALUES (?, ?, ?, ?)", ("n1", "h", 1, 2)),
    ("users.disabled = -1", "INSERT INTO users (username, pw_hash, created_at, disabled) VALUES (?, ?, ?, ?)", ("n2", "h", 1, -1)),
    ("users.source = 'admin'", "INSERT INTO users (username, pw_hash, created_at, source) VALUES (?, ?, ?, ?)", ("n3", "h", 1, "admin")),
    ("users.username NULL", "INSERT INTO users (username, pw_hash, created_at) VALUES (?, ?, ?)", (None, "h", 1)),
    ("users.pw_hash NULL", "INSERT INTO users (username, pw_hash, created_at) VALUES (?, ?, ?)", ("n4", None, 1)),
    ("invites.uses_left = -1", "INSERT INTO invites (code_hash, uses_left, created_at) VALUES (?, ?, ?)", ("c1", -1, 1)),
    ("login_failures.failures = -1", "INSERT INTO login_failures (key, failures, last_failure_at) VALUES (?, ?, ?)", ("k1", -1, 1)),
    ("sessions.user_id of a user that does not exist", "INSERT INTO sessions (token_hash, user_id, csrf_token, created_at, last_seen_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
     ("t1", 424242, "c", 1, 1, 2)),
    ("sessions.csrf_token NULL", "INSERT INTO sessions (token_hash, user_id, csrf_token, created_at, last_seen_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)", ("t2", 1, None, 1, 1, 2)),
]


@pytest.mark.parametrize("label, sql, args", BAD_ROWS, ids=[r[0] for r in BAD_ROWS])
def test_the_database_rejects_a_row_that_breaks_a_constraint(store, db, label, sql, args):
    store.add_user("exists", "h")  # user id 1, so only the column under test is wrong
    with raw(db) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql, args)


def test_the_database_accepts_the_boundary_values_of_the_checks(store, db):
    with raw(db) as conn:
        conn.execute("INSERT INTO users (username, pw_hash, created_at, disabled, source) VALUES ('a', 'h', 1, 1, 'provisioned')")
        conn.execute("INSERT INTO users (username, pw_hash, created_at, disabled, source) VALUES ('b', 'h', 1, 0, 'local')")
        conn.execute("INSERT INTO invites (code_hash, uses_left, created_at) VALUES ('c', 0, 1)")
        conn.execute("INSERT INTO login_failures (key, failures, last_failure_at) VALUES ('k', 0, 1)")


def test_user_names_are_unique_in_the_database_and_through_add_user(store, db):
    assert isinstance(store.add_user("alice", "h"), int)
    assert store.add_user("alice", "other") is None
    assert store.get_user("alice").pw_hash == "h"
    with raw(db) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO users (username, pw_hash, created_at) VALUES ('alice', 'x', 1)")


def test_pinned_the_store_compares_names_exactly_so_the_service_must_lower_case_them(store):
    """Unwritten behaviour, not a deviation: the UNIQUE index is case-sensitive; the lower-casing is done by normalise_username before a name arrives."""
    assert store.add_user("Alice", "h") is not None and store.add_user("alice", "h") is not None


def test_a_bad_source_is_an_integrity_error_and_not_reported_as_a_taken_name(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.add_user("bad", "h", source="admin")
    assert store.get_user("bad") is None


def test_deleting_a_user_removes_its_sessions_through_the_foreign_key(store, db):
    uid = store.add_user("alice", "h")
    store.create_session(uid, "tok", "csrf", NOW, 3600)
    with raw(db) as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (uid,))
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_the_store_itself_enforces_the_foreign_key(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.create_session(424242, "tok", "csrf", NOW, 3600)
    assert store.get_session("tok") is None


# --- rows --------------------------------------------------------------------------------------------------------------------------------------------

def test_rows_are_frozen_dataclasses_with_the_stored_fields(store, clock):
    uid = store.add_user("alice", "hash-a")
    store.create_session(uid, "tok", "csrf", NOW, 100)
    store.record_failure("key", NOW, 900)
    user, session, failure = store.get_user("alice"), store.get_session("tok"), store.get_failure("key")
    assert isinstance(user, UserRow) and isinstance(session, SessionRow) and isinstance(failure, FailureRow)
    assert user == UserRow(uid, "alice", "hash-a", NOW, None, False, "local")
    assert session == SessionRow("tok", uid, "csrf", NOW, NOW, NOW + 100)
    assert failure == FailureRow("key", 1, NOW, 0)
    for row, field in ((user, "pw_hash"), (session, "csrf_token"), (failure, "failures")):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(row, field, "x")


def test_a_missing_row_is_none(store):
    assert store.get_user("nobody") is None and store.get_user_by_id(12345) is None and store.get_session("nope") is None and store.get_failure("nope") is None


# --- users ---------------------------------------------------------------------------------------------------------------------------------------------

def test_add_user_returns_increasing_ids_and_stores_the_clock_time(store, clock):
    first = store.add_user("alice", "h")
    clock.advance(60)
    second = store.add_user("bob", "h")
    assert second > first
    assert store.get_user("alice").created_at == NOW and store.get_user("bob").created_at == NOW + 60
    assert store.get_user_by_id(second).username == "bob"


def test_mark_login_and_set_pw_hash(store, clock):
    uid = store.add_user("alice", "h1")
    assert store.get_user("alice").last_login_at is None
    clock.advance(90)
    store.mark_login(uid)
    assert store.set_pw_hash(uid, "h2") is True and store.set_pw_hash(999999, "x") is False
    user = store.get_user("alice")
    assert (user.last_login_at, user.pw_hash, user.created_at) == (NOW + 90, "h2", NOW)


# --- sessions ------------------------------------------------------------------------------------------------------------------------------------------

def test_a_session_expires_created_plus_absolute_and_starts_unused_since_creation(store):
    uid = store.add_user("alice", "h")
    store.create_session(uid, "tok", "csrf", 1000, 3600)
    s = store.get_session("tok")
    assert (s.user_id, s.csrf_token, s.created_at, s.last_seen_at, s.expires_at) == (uid, "csrf", 1000, 1000, 4600)


def test_a_duplicate_token_hash_is_refused(store):
    uid = store.add_user("alice", "h")
    store.create_session(uid, "tok", "csrf", 1000, 3600)
    with pytest.raises(sqlite3.IntegrityError):
        store.create_session(uid, "tok", "csrf2", 1000, 3600)


def test_touch_session_moves_only_the_last_seen_time(store):
    uid = store.add_user("alice", "h")
    store.create_session(uid, "tok", "csrf", 1000, 3600)
    store.touch_session("tok", 1500)
    s = store.get_session("tok")
    assert (s.last_seen_at, s.created_at, s.expires_at) == (1500, 1000, 4600)


def make_sessions(store, uid, names, created):
    for name, when in zip(names, created):
        store.create_session(uid, name, "c", when, 100_000)


def test_cap_sessions_keeps_the_newest_and_leaves_other_users_alone(store):
    a, b = store.add_user("alice", "h"), store.add_user("bob", "h")
    make_sessions(store, a, ["a0", "a1", "a2", "a3", "a4"], [100, 101, 102, 103, 104])
    make_sessions(store, b, ["b0"], [50])
    assert store.cap_sessions(a, 2) == 3
    assert [t for t in ("a0", "a1", "a2", "a3", "a4") if store.get_session(t)] == ["a3", "a4"]
    assert store.get_session("b0") is not None
    assert store.cap_sessions(a, 2) == 0


def test_cap_sessions_breaks_ties_by_insertion_order_the_last_inserted_is_kept(store):
    uid = store.add_user("alice", "h")
    make_sessions(store, uid, ["t0", "t1", "t2"], [500, 500, 500])
    assert store.cap_sessions(uid, 1) == 2
    assert [t for t in ("t0", "t1", "t2") if store.get_session(t)] == ["t2"]


def test_cap_sessions_with_zero_deletes_all_of_that_users_sessions(store):
    a, b = store.add_user("alice", "h"), store.add_user("bob", "h")
    make_sessions(store, a, ["a0", "a1"], [1, 2])
    make_sessions(store, b, ["b0"], [1])
    assert store.cap_sessions(a, 0) == 2
    assert store.get_session("a0") is None and store.get_session("b0") is not None


def test_purge_deletes_a_session_whose_expires_at_equals_now_but_keeps_one_second_more(store):
    uid = store.add_user("alice", "h")
    store.create_session(uid, "ends-now", "c", 1000, 100)       # expires_at 1100
    store.create_session(uid, "ends-later", "c", 1000, 101)     # expires_at 1101
    assert store.purge_expired(1100, 10_000) == 1
    assert store.get_session("ends-now") is None and store.get_session("ends-later") is not None


def test_purge_deletes_a_session_idle_for_exactly_idle_seconds_but_keeps_one_second_less(store):
    uid = store.add_user("alice", "h")
    store.create_session(uid, "idle-exact", "c", 1000, 100_000)
    store.create_session(uid, "idle-less", "c", 1000, 100_000)
    store.touch_session("idle-exact", 2000)
    store.touch_session("idle-less", 2001)
    assert store.purge_expired(2600, 600) == 1  # idle-exact: 2000 + 600 <= 2600; idle-less: 2001 + 600 > 2600
    assert store.get_session("idle-exact") is None and store.get_session("idle-less") is not None


def test_delete_counts_and_cascade_through_sync(store):
    uid = store.add_user("alice", "h")
    make_sessions(store, uid, ["x1", "x2"], [1, 2])
    assert store.delete_session("x1") == 1 and store.delete_session("x1") == 0
    assert store.delete_user_sessions(uid) == 1 and store.delete_user_sessions(uid) == 0


# --- invites and sign-up ---------------------------------------------------------------------------------------------------------------------------

def test_a_valid_code_creates_the_user_and_is_used_up(store, db):
    store.add_invite("code", 1, None, "for carol")
    assert store.consume_invite_and_add_user("code", "carol", "h", NOW) == "created"
    user = store.get_user("carol")
    assert (user.source, user.created_at, user.disabled) == ("local", NOW, False)
    assert uses_left(db, "code") == 0


def test_a_used_up_code_creates_no_user(store, db):
    store.add_invite("code", 1, None)
    assert store.consume_invite_and_add_user("code", "carol", "h", NOW) == "created"
    assert store.consume_invite_and_add_user("code", "dave", "h", NOW) == "invite_invalid"
    assert store.get_user("dave") is None and uses_left(db, "code") == 0


def test_an_unknown_code_creates_no_user(store):
    assert store.consume_invite_and_add_user("no-such-code", "dave", "h", NOW) == "invite_invalid"
    assert store.get_user("dave") is None


def test_an_expired_code_creates_no_user_and_is_not_used(store, db):
    store.add_invite("code", 5, NOW - 1)
    assert store.consume_invite_and_add_user("code", "dave", "h", NOW) == "invite_invalid"
    assert store.get_user("dave") is None and uses_left(db, "code") == 5


def test_a_code_that_expires_exactly_now_is_expired_and_one_second_earlier_than_expiry_is_valid(store, db):
    store.add_invite("code", 2, NOW)
    assert store.consume_invite_and_add_user("code", "dave", "h", NOW) == "invite_invalid"
    assert store.consume_invite_and_add_user("code", "dave", "h", NOW - 1) == "created"
    assert uses_left(db, "code") == 1


def test_a_code_without_an_expiry_never_expires(store):
    store.add_invite("code", 1, None)
    assert store.consume_invite_and_add_user("code", "erin", "h", NOW + 10**9) == "created"


def test_a_taken_name_returns_name_taken_and_leaves_the_code_unused(store, db):
    store.add_user("carol", "h")
    store.add_invite("code", 1, None)
    assert store.consume_invite_and_add_user("code", "carol", "other", NOW) == "name_taken"
    assert uses_left(db, "code") == 1
    assert store.get_user("carol").pw_hash == "h"
    assert store.consume_invite_and_add_user("code", "erin", "h", NOW) == "created"
    assert uses_left(db, "code") == 0


def test_a_code_is_checked_before_the_name_so_a_bad_code_with_a_taken_name_is_invite_invalid(store):
    store.add_user("carol", "h")
    assert store.consume_invite_and_add_user("no-such-code", "carol", "h", NOW) == "invite_invalid"


def test_without_a_code_open_sign_up_creates_a_user_and_reports_a_taken_name(store, db):
    assert store.consume_invite_and_add_user(None, "frank", "h", NOW) == "created"
    assert store.consume_invite_and_add_user(None, "frank", "h", NOW) == "name_taken"
    with raw(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM invites").fetchone()[0] == 0


def test_a_code_with_three_uses_works_three_times_and_then_not(store, db):
    store.add_invite("code", 3, None)
    results = [store.consume_invite_and_add_user("code", f"user-{i}", "h", NOW) for i in range(4)]
    assert results == ["created", "created", "created", "invite_invalid"]
    assert uses_left(db, "code") == 0


def test_a_bad_value_inside_the_transaction_rolls_the_use_of_the_code_back(store, db):
    store.add_invite("code", 1, None)
    with pytest.raises(sqlite3.IntegrityError):  # pw_hash NULL breaks NOT NULL, not UNIQUE: not a "taken name"
        store.consume_invite_and_add_user("code", "carol", None, NOW)
    assert uses_left(db, "code") == 1 and store.get_user("carol") is None


ROUNDS, THREADS = 20, 8


def run_threads(targets, limit=60):
    threads = [threading.Thread(target=t, daemon=True) for t in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(limit)
    assert not any(t.is_alive() for t in threads), "a thread is still running: a hang, not a result"


def test_eight_threads_racing_for_one_single_use_code_give_exactly_one_winner_every_round(store, db, clock):
    for rnd in range(ROUNDS):
        code = f"race-{rnd}"
        store.add_invite(code, 1, None)
        barrier = threading.Barrier(THREADS, timeout=30)
        out, errors = [None] * THREADS, []

        def worker(i, rnd=rnd, code=code, barrier=barrier, out=out, errors=errors):
            try:
                local = Store(db, clock)  # its own object, as separate requests would have
                barrier.wait()
                out[i] = local.consume_invite_and_add_user(code, f"racer-{rnd}-{i}", "h", NOW)
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

        run_threads([lambda i=i: worker(i) for i in range(THREADS)])
        assert errors == [], errors
        assert out.count("created") == 1 and out.count("invite_invalid") == THREADS - 1, out
        with raw(db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM users WHERE username LIKE ?", (f"racer-{rnd}-%",)).fetchone()[0] == 1
        assert uses_left(db, code) == 0


def test_eight_threads_writing_at_once_never_hit_database_is_locked(store, db, clock):
    """The busy timeout lets writers wait for each other: 8 threads add 15 users each."""
    barrier = threading.Barrier(THREADS, timeout=30)
    errors, ids = [], []

    def worker(i):
        try:
            local = Store(db, clock)
            barrier.wait()
            for n in range(15):
                ids.append(local.add_user(f"w{i}-{n}", "h"))
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    run_threads([lambda i=i: worker(i) for i in range(THREADS)])
    assert errors == [] and len(ids) == 120 and len(set(ids)) == 120 and None not in ids


def test_a_write_holds_the_database_write_lock_from_its_first_statement(store, db):
    """BEGIN IMMEDIATE takes the write lock when it runs; a plain (deferred) BEGIN takes none until the first write statement. The body below runs NO statement,
    so the only way the second connection's BEGIN IMMEDIATE can fail with "locked" is that _write already holds the lock. With a deferred BEGIN it succeeds and
    pytest.raises reports "DID NOT RAISE". One thread is enough (the second connection simply is another connection), so there is nothing to join or to
    release; the only wait is SQLite's own busy timeout of 50 ms, which ends by itself."""
    other = sqlite3.connect(db, timeout=0.05, isolation_level=None)
    try:
        with store._write():
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
        other.execute("BEGIN IMMEDIATE")  # the lock is gone once the write has ended
        other.execute("ROLLBACK")
    finally:
        other.close()


# --- provisioned users -------------------------------------------------------------------------------------------------------------------------------

def test_sync_adds_listed_users_as_provisioned(store):
    store.sync_provisioned({"prov-a": "hash-a", "prov-b": "hash-b"})
    a, b = store.get_user("prov-a"), store.get_user("prov-b")
    assert (a.source, a.pw_hash, b.source, b.pw_hash) == ("provisioned", "hash-a", "provisioned", "hash-b")


def test_sync_leaves_an_unchanged_hash_and_its_sessions_alone(store):
    store.sync_provisioned({"prov-a": "hash-a"})
    uid = store.get_user("prov-a").id
    store.create_session(uid, "tok", "c", NOW, 100_000)
    store.sync_provisioned({"prov-a": "hash-a"})
    assert store.get_user("prov-a") == store.get_user_by_id(uid) and store.get_user("prov-a").pw_hash == "hash-a"
    assert store.get_session("tok") is not None


def test_sync_replaces_a_changed_hash_and_deletes_that_users_sessions_only(store):
    store.sync_provisioned({"prov-a": "hash-a", "prov-b": "hash-b"})
    a, b = store.get_user("prov-a"), store.get_user("prov-b")
    store.create_session(a.id, "ta", "c", NOW, 100_000)
    store.create_session(b.id, "tb", "c", NOW, 100_000)
    store.sync_provisioned({"prov-a": "hash-a", "prov-b": "hash-b-new"})
    assert store.get_user("prov-b").pw_hash == "hash-b-new" and store.get_user("prov-b").id == b.id
    assert store.get_session("tb") is None and store.get_session("ta") is not None


def test_sync_deletes_a_provisioned_user_that_is_no_longer_listed_with_its_sessions(store):
    store.sync_provisioned({"prov-a": "hash-a", "prov-b": "hash-b"})
    store.create_session(store.get_user("prov-b").id, "tb", "c", NOW, 100_000)
    store.sync_provisioned({"prov-a": "hash-a"})
    assert store.get_user("prov-b") is None and store.get_session("tb") is None and store.get_user("prov-a") is not None


def test_sync_never_deletes_local_users(store):
    local = store.add_user("alice", "h")
    store.sync_provisioned({"prov-a": "hash-a"})
    store.sync_provisioned({})
    assert store.get_user("alice").id == local and store.get_user("prov-a") is None


# Break-control note (mutation S04): taking the ROLLBACK out of Store._write is an EQUIVALENT mutant. Closing a sqlite3 connection that still has an open
# transaction rolls it back, and _connect() always closes, so "nothing changed after an error" holds either way and no test can tell the two apart. There is
# deliberately no test for it.
def test_sync_refuses_a_listed_name_that_is_a_local_account_without_naming_it_and_changes_nothing(store, db):
    store.add_user("sentinel-local", "h")
    store.sync_provisioned({"prov-a": "hash-a"})
    before = dump(db)
    with pytest.raises(StoreError) as error:
        store.sync_provisioned({"prov-a": "hash-a2", "sentinel-local": "hash-x", "prov-new": "hash-n"})
    assert "sentinel" not in str(error.value).lower() and "prov-" not in str(error.value) and "hash" not in str(error.value).lower()
    assert dump(db) == before


# --- failed logins: the key --------------------------------------------------------------------------------------------------------------------------

def key_bytes(db):
    with raw(db) as conn:
        return bytes.fromhex(conn.execute("SELECT value FROM meta WHERE key = 'hmac_key'").fetchone()[0])


def test_the_failure_key_is_the_hmac_sha256_hex_of_username_pipe_ip_under_the_databases_key(store, db):
    expected = hmac.new(key_bytes(db), b"attacker-name-xyz|203.0.113.77", hashlib.sha256).hexdigest()
    assert store.failure_key("attacker-name-xyz", "203.0.113.77") == expected
    assert len(expected) == 64


def test_the_failure_key_is_stable_across_store_objects_and_reopening(store, db, clock):
    k = store.failure_key("alice", "10.0.0.1")
    assert Store(db, clock).failure_key("alice", "10.0.0.1") == k
    store2 = Store(db, clock)
    store2.init_schema()
    assert store2.failure_key("alice", "10.0.0.1") == k


def test_the_failure_key_differs_for_another_database_file(store, tmp_path, clock):
    other = Store(tmp_path / "other" / "auth.db", clock)
    other.init_schema()
    assert other.failure_key("alice", "10.0.0.1") != store.failure_key("alice", "10.0.0.1")


def test_the_failure_key_differs_for_another_user_or_another_address(store):
    keys = {store.failure_key("alice", "10.0.0.1"), store.failure_key("alice", "10.0.0.2"), store.failure_key("alicf", "10.0.0.1"), store.failure_key("alice", ""), store.failure_key("", "10.0.0.1")}
    assert len(keys) == 5


COLLISIONS = [("a|b", "c", "a", "b|c"), ("a\\", "|c", "a", "\\|c"), ("bob|9.9.9.9", "", "bob", "9.9.9.9"), ("a\\|", "b", "a", "|\\b"), ("x|", "|y", "x||", "y")]


@pytest.mark.parametrize("u1, i1, u2, i2", COLLISIONS)
def test_pairs_that_would_collide_without_escaping_give_different_keys(store, db, u1, i1, u2, i2):
    assert store.failure_key(u1, i1) != store.failure_key(u2, i2)


def test_escaping_leaves_ordinary_values_untouched_so_the_message_is_exactly_username_pipe_ip(store, db):
    for user, ip in (("alice", "10.0.0.1"), ("bob.smith-2", "2001:db8::1"), ("x_y", "")):
        expected = hmac.new(key_bytes(db), f"{user}|{ip}".encode("utf-8"), hashlib.sha256).hexdigest()
        assert store.failure_key(user, ip) == expected


def test_a_lone_surrogate_in_the_address_gives_a_64_hex_key_that_differs_from_other_surrogates_and_from_a_question_mark(store):
    key = store.failure_key("alice", "10.0." + chr(0xD800))
    assert len(key) == 64 and all(c in "0123456789abcdef" for c in key)
    assert key != store.failure_key("alice", "10.0." + chr(0xD801))
    assert key != store.failure_key("alice", "10.0.?")
    assert key == store.failure_key("alice", "10.0." + chr(0xD800))  # and it is stable


def test_pinned_a_lone_surrogate_is_hashed_as_its_three_surrogatepass_bytes_in_the_message(store, db):
    expected = hmac.new(key_bytes(db), b"alice|10.0." + b"\xed\xa0\x80", hashlib.sha256).hexdigest()
    assert store.failure_key("alice", "10.0." + chr(0xD800)) == expected


def test_pinned_a_lone_surrogate_in_the_user_name_also_gives_a_64_hex_key_and_keeps_names_apart(store):
    key = store.failure_key("al" + chr(0xD800) + "ce", "10.0.0.1")
    assert len(key) == 64 and all(c in "0123456789abcdef" for c in key)
    assert key != store.failure_key("al" + chr(0xD801) + "ce", "10.0.0.1")
    assert key != store.failure_key("al?ce", "10.0.0.1")
    assert key != store.failure_key("alce", "10.0.0.1")


# --- failed logins: the counter ----------------------------------------------------------------------------------------------------------------------

def test_record_failure_counts_one_then_two_and_returns_the_new_row(store):
    first = store.record_failure("k", 3000, 900)
    second = store.record_failure("k", 3010, 900)
    assert (first.failures, first.last_failure_at, first.blocked_until) == (1, 3000, 0)
    assert (second.failures, second.last_failure_at) == (2, 3010)
    assert store.get_failure("k") == second


def test_a_failure_exactly_window_seconds_after_the_last_still_counts(store):
    store.record_failure("k", 1000, 900)
    assert store.record_failure("k", 1900, 900).failures == 2


def test_a_failure_one_second_more_than_the_window_after_the_last_starts_again_at_one(store):
    store.record_failure("k", 1000, 900)
    store.record_failure("k", 1010, 900)
    assert store.record_failure("k", 1910 + 1, 900).failures == 1


def test_delay_for_is_called_once_per_failure_with_the_new_count_only_and_sets_blocked_until(store):
    seen = []

    def delay_for(count):
        seen.append(count)
        return 2 * count

    first = store.record_failure("k", 5000, 900, delay_for)
    second = store.record_failure("k", 5100, 900, delay_for)
    third = store.record_failure("k", 5100 + 901, 900, delay_for)  # the window restarted
    assert seen == [1, 2, 1]
    assert (first.blocked_until, second.blocked_until, third.blocked_until) == (5000 + 2, 5100 + 4, 6001 + 2)
    assert store.get_failure("k") == third


def test_without_delay_for_blocked_until_is_zero_and_a_restart_clears_an_old_block(store):
    store.record_failure("k", 5000, 900, lambda n: 100)
    assert store.get_failure("k").blocked_until == 5100
    assert store.record_failure("k", 5001, 900).blocked_until == 0


def test_failures_of_different_keys_do_not_affect_each_other(store):
    store.record_failure("k1", 100, 900)
    store.record_failure("k1", 101, 900)
    assert store.record_failure("k2", 102, 900).failures == 1 and store.get_failure("k1").failures == 2


def test_clear_failure_removes_the_row_and_is_harmless_for_a_missing_key(store):
    store.record_failure("k", 100, 900)
    store.clear_failure("k")
    store.clear_failure("never-seen")
    assert store.get_failure("k") is None


def test_purge_failures_deletes_rows_last_seen_before_the_limit_and_keeps_the_boundary(store):
    for key, when in (("old", 100), ("edge", 300), ("new", 5000)):
        store.record_failure(key, when, 900)
    assert store.purge_failures(300) == 1
    assert store.get_failure("old") is None and store.get_failure("edge") is not None and store.get_failure("new") is not None


def test_the_database_file_never_holds_a_failed_user_name_or_an_address(store, db):
    for _ in range(3):
        store.record_failure(store.failure_key("attacker-name-xyz", "203.0.113.77"), NOW, 900, lambda n: n)
    data = b"".join(p.read_bytes() for p in db.parent.glob("auth.db*"))
    assert b"attacker-name-xyz" not in data and b"203.0.113.77" not in data and b"203.0.113" not in data


# --- hostile strings ---------------------------------------------------------------------------------------------------------------------------------

HOSTILE = ["x'; DROP TABLE users; --", "' OR '1'='1", 'a"b', "a%b_c", "line1\nline2", "tab\there", "back\\slash", "caf" + chr(233) + chr(0x1F600), "  spaces  ", "%", "_", ""]


@pytest.mark.parametrize("text", HOSTILE, ids=[repr(t)[:22] for t in HOSTILE])
def test_hostile_text_is_stored_and_read_back_literally(store, text):
    uid = store.add_user(text, "h-" + text)
    assert store.get_user(text).username == text and store.get_user(text).pw_hash == "h-" + text and store.get_user_by_id(uid).username == text
    store.create_session(uid, text, text, NOW, 100)
    assert store.get_session(text).token_hash == text and store.get_session(text).csrf_token == text
    store.record_failure(text, NOW, 900)
    assert store.get_failure(text).key == text
    store.add_invite(text, 1, None, text)
    assert store.consume_invite_and_add_user(text, text + "-new", "h", NOW) == "created"


def test_a_pattern_or_injection_string_does_not_match_other_rows(store):
    store.add_user("alice", "h")
    store.add_user("bob", "h")
    for pattern in ("%", "_____", "' OR '1'='1", "alice' --", "al%"):
        assert store.get_user(pattern) is None
    assert store.get_session("' OR '1'='1") is None and store.get_failure("%") is None
    assert store.get_user("alice") is not None and store.get_user("bob") is not None


# --- bad, newer or missing databases ----------------------------------------------------------------------------------------------------------------

def set_meta(path, key, value):
    with raw(path) as conn:
        if value is None:
            conn.execute("DELETE FROM meta WHERE key = ?", (key,))
        else:
            conn.execute("UPDATE meta SET value = ? WHERE key = ?", (value, key))


VERSIONS = [("2", "version 2"), ("99", "version 99"), ("0", "version 0"), ("abc", "unknown schema version"), ("1.0", "unknown schema version"), ("-1", "unknown schema version"),
            (None, "unknown schema version")]


@pytest.mark.parametrize("value, word", VERSIONS, ids=[repr(v[0]) for v in VERSIONS])
def test_a_newer_or_unknown_schema_version_raises_store_error_and_leaves_the_file_unchanged(store, db, clock, value, word):
    set_meta(db, "schema_version", value)
    before = db.read_bytes()
    with pytest.raises(StoreError, match=word):
        Store(db, clock).init_schema()
    assert db.read_bytes() == before
    assert not list(db.parent.glob("auth.db-*"))


def test_a_database_without_its_key_raises_store_error_and_is_left_unchanged(store, db, clock):
    set_meta(db, "hmac_key", None)
    before = db.read_bytes()
    with pytest.raises(StoreError, match="HMAC key"):
        Store(db, clock).init_schema()
    assert db.read_bytes() == before


def test_some_other_sqlite_database_raises_store_error_and_is_left_unchanged(tmp_path, clock):
    foreign = tmp_path / "foreign.db"
    with raw(foreign) as conn:
        conn.execute("CREATE TABLE other (x)")
        conn.execute("INSERT INTO other VALUES (1)")
    before = foreign.read_bytes()
    with pytest.raises(StoreError, match="not an auth database"):
        Store(foreign, clock).init_schema()
    assert foreign.read_bytes() == before
    with raw(foreign) as conn:
        assert [r[0] for r in conn.execute("SELECT name FROM sqlite_master")] == ["other"]


def test_a_file_that_is_not_sqlite_raises_store_error_and_is_left_unchanged(tmp_path, clock):
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"this is not a sqlite file at all, just text" * 10)
    before = garbage.read_bytes()
    with pytest.raises(StoreError, match="not a usable SQLite database"):
        Store(garbage, clock).init_schema()
    assert garbage.read_bytes() == before


def test_store_error_is_a_runtime_error_and_its_messages_hold_no_data(store, db, clock):
    assert issubclass(StoreError, RuntimeError)
    messages = []
    set_meta(db, "schema_version", "2")
    for call in (lambda: Store(db, clock).init_schema(), lambda: Store(db.parent / "missing.db", clock).get_user("sentinel-user-9731")):
        with pytest.raises(StoreError) as error:
            call()
        messages.append(str(error.value))
    assert all("sentinel" not in m and "9731" not in m for m in messages)


MISSING_CALLS = [
    ("get_user", lambda s: s.get_user("a")), ("get_user_by_id", lambda s: s.get_user_by_id(1)), ("add_user", lambda s: s.add_user("a", "h")),
    ("set_pw_hash", lambda s: s.set_pw_hash(1, "h")), ("mark_login", lambda s: s.mark_login(1)), ("sync_provisioned", lambda s: s.sync_provisioned({"a": "h"})),
    ("create_session", lambda s: s.create_session(1, "t", "c", 1, 1)), ("get_session", lambda s: s.get_session("t")), ("touch_session", lambda s: s.touch_session("t", 1)),
    ("delete_session", lambda s: s.delete_session("t")), ("delete_user_sessions", lambda s: s.delete_user_sessions(1)), ("cap_sessions", lambda s: s.cap_sessions(1, 1)),
    ("purge_expired", lambda s: s.purge_expired(1, 1)), ("add_invite", lambda s: s.add_invite("c", 1, None)),
    ("consume_invite_and_add_user", lambda s: s.consume_invite_and_add_user("c", "a", "h", 1)), ("failure_key", lambda s: s.failure_key("a", "b")),
    ("get_failure", lambda s: s.get_failure("k")), ("record_failure", lambda s: s.record_failure("k", 1, 1)), ("clear_failure", lambda s: s.clear_failure("k")),
    ("purge_failures", lambda s: s.purge_failures(1)),
]


@pytest.mark.parametrize("name, call", MISSING_CALLS, ids=[c[0] for c in MISSING_CALLS])
def test_any_call_on_a_database_that_does_not_exist_raises_store_error_and_creates_nothing(tmp_path, clock, name, call):
    path = tmp_path / "not-yet" / "auth.db"
    with pytest.raises(StoreError):
        call(Store(path, clock))
    assert not path.exists() and not path.parent.exists()
    existing_folder = tmp_path / "exists"
    existing_folder.mkdir()
    with pytest.raises(StoreError):
        call(Store(existing_folder / "auth.db", clock))
    assert list(existing_folder.iterdir()) == []


def test_a_path_with_spaces_an_ampersand_a_hash_and_a_non_ascii_letter_works(tmp_path, clock):
    path = tmp_path / ("a folder with spaces & #hash " + "caf" + chr(233)) / "auth.db"
    s = Store(path, clock)
    s.init_schema()
    assert s.add_user("spacey", "h") is not None and s.get_user("spacey").username == "spacey"


# --- handles are closed ---------------------------------------------------------------------------------------------------------------------------------

def test_every_connection_is_closed_so_the_whole_folder_can_be_removed_after_heavy_use(tmp_path, clock):
    folder = tmp_path / "removable"
    s = Store(folder / "auth.db", clock)
    s.init_schema()
    uid = s.add_user("alice", "h")
    s.add_user("alice", "h")  # a taken name
    s.create_session(uid, "t", "c", NOW, 10)
    s.add_invite("code", 1, None)
    s.consume_invite_and_add_user("code", "bob", "h", NOW)
    s.consume_invite_and_add_user("code", "carol", "h", NOW)  # invalid
    s.consume_invite_and_add_user(None, "bob", "h", NOW)       # taken
    s.record_failure(s.failure_key("a", "b"), NOW, 900, lambda n: n)
    s.sync_provisioned({"p": "h"})
    with pytest.raises(sqlite3.IntegrityError):
        s.add_user("x", "h", source="bad")
    with pytest.raises(StoreError):
        Store(folder / "missing.db", clock).get_user("x")
    shutil.rmtree(folder)
    assert not folder.exists()


# --- no string-built SQL -------------------------------------------------------------------------------------------------------------------------------

ALLOWED_FSTRING_MARKERS = ("as_uri", "_escape(", "schema version")


def sql_problems(source):
    """Problems in a module's SQL handling: an execute() whose first argument is not a string literal or a module-level constant, a % or + or .format() on a
    string literal, and an f-string other than the three allowed ones (database URI, HMAC message, error text)."""
    tree = ast.parse(source)
    constants = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and isinstance(node.value, (ast.Constant, ast.Tuple, ast.List)):
            constants.add(node.targets[0].id)
    loop_vars = {n.target.id for n in ast.walk(tree) if isinstance(n, ast.For) and isinstance(n.target, ast.Name) and isinstance(n.iter, ast.Name) and n.iter.id in constants}
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "execute":
            arg = node.args[0]
            literal = isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            named = isinstance(arg, ast.Name) and (arg.id in constants or arg.id in loop_vars)
            if not (literal or named):
                problems.append("execute() with a built or unknown first argument: " + ast.unparse(node)[:80])
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mod, ast.Add)):
            sides = [node.left] + ([node.right] if isinstance(node.op, ast.Add) else [])
            if any(isinstance(s, ast.Constant) and isinstance(s.value, str) for s in sides):
                problems.append("string built with % or +: " + ast.unparse(node)[:80])
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format" and isinstance(node.func.value, ast.Constant):
            problems.append(".format() on a string literal: " + ast.unparse(node)[:80])
        if isinstance(node, ast.JoinedStr) and not any(marker in ast.unparse(node) for marker in ALLOWED_FSTRING_MARKERS):
            problems.append("unexpected f-string: " + ast.unparse(node)[:80])
    return problems


def test_the_store_has_no_string_built_sql():
    assert sql_problems(STORE_SOURCE.read_text(encoding="utf-8")) == []


def test_the_store_has_a_realistic_number_of_execute_calls():
    """A guard against the checker silently looking at nothing."""
    tree = ast.parse(STORE_SOURCE.read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "execute"]
    assert len(calls) >= 30


BAD_SOURCES = {
    "an f-string argument": 'def f(conn, x):\n    conn.execute(f"SELECT {x}")\n',
    "a concatenation": 'def f(conn, x):\n    conn.execute("SELECT " + x)\n',
    "a % format": 'def f(conn, x):\n    conn.execute("SELECT %s" % x)\n',
    "a .format call": 'def f(conn, x):\n    conn.execute("SELECT {}".format(x))\n',
    "a local variable": 'def f(conn, x):\n    query = "SELECT " + x\n    conn.execute(query)\n',
    "an unrelated f-string": 'def f(x):\n    return f"hello {x}"\n',
}
GOOD_SOURCES = {
    "a literal": 'def f(conn):\n    conn.execute("SELECT 1")\n',
    "a module constant": 'Q = "SELECT 1"\n\ndef f(conn):\n    conn.execute(Q)\n',
    "a loop over a module tuple": 'SCHEMA = ("A", "B")\n\ndef f(conn):\n    for statement in SCHEMA:\n        conn.execute(statement)\n',
}


@pytest.mark.parametrize("label", list(BAD_SOURCES))
def test_the_sql_checker_flags_string_built_sql(label):
    assert sql_problems(BAD_SOURCES[label]) != []


@pytest.mark.parametrize("label", list(GOOD_SOURCES))
def test_the_sql_checker_accepts_literals_and_constants(label):
    assert sql_problems(GOOD_SOURCES[label]) == []


def test_the_store_does_not_log_and_does_not_import_logging():
    tree = ast.parse(STORE_SOURCE.read_text(encoding="utf-8"))
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert "logging" not in imported
    assert "print(" not in STORE_SOURCE.read_text(encoding="utf-8")


def test_the_store_imports_only_the_standard_library():
    tree = ast.parse(STORE_SOURCE.read_text(encoding="utf-8"))
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert imported == {"hashlib", "hmac", "os", "secrets", "sqlite3", "threading", "contextlib", "dataclasses", "pathlib"}
    assert imported <= set(sys.stdlib_module_names)
