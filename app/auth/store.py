"""SQLite storage for the optional login (standard library only).

One short-lived connection per call (foreign keys on, busy timeout 5 s, default rollback journal, no WAL); every write runs inside BEGIN IMMEDIATE. All SQL
is a constant or a module-level name and every value goes in as a parameter. Nothing is created or read at import or in Store.__init__: init_schema() makes
the folder and the file. Stored: user names, scrypt hash strings, the SHA-256 of session tokens and invite codes (the caller hashes them), the CSRF token,
timestamps, and for failed logins only an HMAC key (never a user name or an address). Nothing here logs.
"""
import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 1
DATABASE_MODE = 0o600  # read and write for the owner only, where the file system has such modes (not enforced on Windows, see Store.init_schema)

SCHEMA = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        pw_hash TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        last_login_at INTEGER,
        disabled INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1)),
        source TEXT NOT NULL DEFAULT 'local' CHECK (source IN ('local', 'provisioned'))
    )""",
    """CREATE TABLE sessions (
        token_hash TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        csrf_token TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        last_seen_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL
    )""",
    "CREATE INDEX idx_sessions_user ON sessions(user_id)",
    "CREATE INDEX idx_sessions_expires ON sessions(expires_at)",
    """CREATE TABLE invites (
        code_hash TEXT PRIMARY KEY,
        uses_left INTEGER NOT NULL CHECK (uses_left >= 0),
        expires_at INTEGER,
        created_at INTEGER NOT NULL,
        note TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE login_failures (
        key TEXT PRIMARY KEY,
        failures INTEGER NOT NULL CHECK (failures >= 0),
        last_failure_at INTEGER NOT NULL,
        blocked_until INTEGER NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX idx_failures_last ON login_failures(last_failure_at)",
)

_TABLE_COUNT = "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
_HAS_META = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
_META_GET = "SELECT value FROM meta WHERE key = ?"
_META_ADD = "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)"
_USER_BY_NAME = "SELECT id, username, pw_hash, created_at, last_login_at, disabled, source FROM users WHERE username = ?"
_USER_BY_ID = "SELECT id, username, pw_hash, created_at, last_login_at, disabled, source FROM users WHERE id = ?"
_USER_ADD = "INSERT INTO users (username, pw_hash, created_at, source) VALUES (?, ?, ?, ?)"
_USER_SET_HASH = "UPDATE users SET pw_hash = ? WHERE id = ?"
_USER_MARK_LOGIN = "UPDATE users SET last_login_at = ? WHERE id = ?"
_USERS_PROVISIONED = "SELECT id, username, pw_hash FROM users WHERE source = 'provisioned'"
_USER_DELETE = "DELETE FROM users WHERE id = ?"
_SESSION_ADD = "INSERT INTO sessions (token_hash, user_id, csrf_token, created_at, last_seen_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)"
_SESSION_GET = "SELECT token_hash, user_id, csrf_token, created_at, last_seen_at, expires_at FROM sessions WHERE token_hash = ?"
_SESSION_TOUCH = "UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?"
_SESSION_DELETE = "DELETE FROM sessions WHERE token_hash = ?"
_SESSIONS_OF_USER_DELETE = "DELETE FROM sessions WHERE user_id = ?"
_SESSIONS_CAP = ("DELETE FROM sessions WHERE user_id = ? AND token_hash NOT IN "
                 "(SELECT token_hash FROM sessions WHERE user_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?)")
_SESSIONS_PURGE = "DELETE FROM sessions WHERE expires_at <= ? OR last_seen_at + ? <= ?"
_INVITE_ADD = "INSERT INTO invites (code_hash, uses_left, expires_at, created_at, note) VALUES (?, ?, ?, ?, ?)"
_INVITE_USE = "UPDATE invites SET uses_left = uses_left - 1 WHERE code_hash = ? AND uses_left > 0 AND (expires_at IS NULL OR expires_at > ?)"
_FAILURE_GET = "SELECT key, failures, last_failure_at, blocked_until FROM login_failures WHERE key = ?"
_FAILURE_SET = ("INSERT INTO login_failures (key, failures, last_failure_at, blocked_until) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET failures = excluded.failures, last_failure_at = excluded.last_failure_at, blocked_until = excluded.blocked_until")
_FAILURE_CLEAR = "DELETE FROM login_failures WHERE key = ?"
_FAILURES_PURGE = "DELETE FROM login_failures WHERE last_failure_at < ?"


class StoreError(RuntimeError):
    """The database cannot be used (unknown or newer schema, not initialised, a name clash during provisioning). Messages hold no user data."""


@dataclass(frozen=True)
class UserRow:
    id: int
    username: str
    pw_hash: str
    created_at: int
    last_login_at: int | None
    disabled: bool
    source: str


@dataclass(frozen=True)
class SessionRow:
    token_hash: str
    user_id: int
    csrf_token: str
    created_at: int
    last_seen_at: int
    expires_at: int


@dataclass(frozen=True)
class FailureRow:
    key: str
    failures: int
    last_failure_at: int
    blocked_until: int


def _user(row):
    return None if row is None else UserRow(row["id"], row["username"], row["pw_hash"], row["created_at"], row["last_login_at"], bool(row["disabled"]), row["source"])


def _session(row):
    return None if row is None else SessionRow(row["token_hash"], row["user_id"], row["csrf_token"], row["created_at"], row["last_seen_at"], row["expires_at"])


def _failure(row):
    return None if row is None else FailureRow(row["key"], row["failures"], row["last_failure_at"], row["blocked_until"])


def _is_unique_violation(error):
    name = getattr(error, "sqlite_errorname", "")
    return name == "SQLITE_CONSTRAINT_UNIQUE" or (not name and str(error).startswith("UNIQUE constraint failed"))


def _escape(part):
    return part.replace("\\", "\\\\").replace("|", "\\|")


class Store:
    def __init__(self, path, clock):
        self._path = Path(path)
        self._clock = clock
        self._key = None
        self._key_lock = threading.Lock()

    @property
    def path(self):
        return self._path

    # --- connections --------------------------------------------------------------------------------------------------------------------------

    @contextmanager
    def _connect(self):
        # mode=rw: a missing file is an error, never silently created (only init_schema creates it, with mode 0600). isolation_level=None: autocommit,
        # transactions are explicit.
        try:
            conn = sqlite3.connect(f"{self._path.resolve().as_uri()}?mode=rw", uri=True, timeout=5.0, isolation_level=None)
        except sqlite3.OperationalError:
            raise StoreError("The auth database cannot be opened: it does not exist yet (call init_schema()) or is not readable.") from None
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write(self):
        """A connection inside BEGIN IMMEDIATE: committed when the body ends, rolled back when it raises."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    # --- schema ---------------------------------------------------------------------------------------------------------------------------------

    def init_schema(self):
        """Create the folder, the file (mode 0600 where the OS has modes) and the tables, once. Safe to call again and from several Store objects.

        On Windows os.open() honours only the read-only bit of the mode, so the file is NOT restricted to its owner by this call: it keeps the permissions
        its folder gives it. A newer or unknown schema, or a file that is some other database, raises StoreError."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            os.close(os.open(self._path, os.O_RDWR | os.O_CREAT, DATABASE_MODE))
        try:
            self._prepare_schema()
        except sqlite3.DatabaseError as error:
            if isinstance(error, sqlite3.IntegrityError):
                raise
            raise StoreError("The file is not a usable SQLite database.") from None

    def _prepare_schema(self):
        with self._write() as conn:
            if conn.execute(_HAS_META).fetchone() is None:
                if conn.execute(_TABLE_COUNT).fetchone()[0] != 0:
                    raise StoreError("The file is a database, but not an auth database.")
                for statement in SCHEMA:
                    conn.execute(statement)
                conn.execute(_META_ADD, ("schema_version", str(SCHEMA_VERSION)))
                conn.execute(_META_ADD, ("hmac_key", secrets.token_bytes(32).hex()))
            else:
                version = conn.execute(_META_GET, ("schema_version",)).fetchone()
                if version is None or not version[0].isdigit():
                    raise StoreError("The auth database has an unknown schema version.")
                if int(version[0]) != SCHEMA_VERSION:
                    raise StoreError(f"The auth database has schema version {int(version[0])}; this program supports version {SCHEMA_VERSION}.")
                if conn.execute(_META_GET, ("hmac_key",)).fetchone() is None:
                    raise StoreError("The auth database has no HMAC key.")

    def _hmac_key(self):
        with self._key_lock:
            if self._key is None:
                with self._connect() as conn:
                    row = conn.execute(_META_GET, ("hmac_key",)).fetchone()
                if row is None:
                    raise StoreError("The auth database is not initialised: call init_schema().")
                self._key = bytes.fromhex(row[0])
            return self._key

    # --- users ----------------------------------------------------------------------------------------------------------------------------------

    def add_user(self, username, pw_hash, source="local"):
        """The new user's id, or None if the name is taken. Any other constraint failure (a bad source) raises."""
        try:
            with self._write() as conn:
                return conn.execute(_USER_ADD, (username, pw_hash, int(self._clock()), source)).lastrowid
        except sqlite3.IntegrityError as error:
            if _is_unique_violation(error):
                return None
            raise

    def get_user(self, username):
        with self._connect() as conn:
            return _user(conn.execute(_USER_BY_NAME, (username,)).fetchone())

    def get_user_by_id(self, user_id):
        with self._connect() as conn:
            return _user(conn.execute(_USER_BY_ID, (user_id,)).fetchone())

    def set_pw_hash(self, user_id, pw_hash):
        with self._write() as conn:
            return conn.execute(_USER_SET_HASH, (pw_hash, user_id)).rowcount == 1

    def mark_login(self, user_id):
        with self._write() as conn:
            conn.execute(_USER_MARK_LOGIN, (int(self._clock()), user_id))

    def sync_provisioned(self, users):
        """Make the provisioned users equal to `users` ({name: stored hash}) in one transaction. The environment is the source of truth:
        - a listed user that does not exist is added (source 'provisioned');
        - a listed user whose hash TEXT differs from the stored one gets the new hash and loses all sessions (the credential changed);
        - a listed user whose hash is unchanged is not touched (a hash is never rewritten, for example by a rehash);
        - a provisioned user that is no longer listed is deleted together with its sessions (ON DELETE CASCADE);
        - a listed name that belongs to a LOCAL account raises StoreError and nothing is changed: the two kinds are never merged."""
        now = int(self._clock())
        with self._write() as conn:
            existing = {row["username"]: row for row in conn.execute(_USERS_PROVISIONED)}
            for name, pw_hash in users.items():
                row = existing.pop(name, None)
                if row is None:
                    try:
                        conn.execute(_USER_ADD, (name, pw_hash, now, "provisioned"))
                    except sqlite3.IntegrityError as error:
                        if _is_unique_violation(error):
                            raise StoreError("A provisioned user has the same name as a local account; remove the local account first.") from None
                        raise
                elif row["pw_hash"] != pw_hash:
                    conn.execute(_USER_SET_HASH, (pw_hash, row["id"]))
                    conn.execute(_SESSIONS_OF_USER_DELETE, (row["id"],))
            for row in existing.values():
                conn.execute(_USER_DELETE, (row["id"],))

    # --- sessions -------------------------------------------------------------------------------------------------------------------------------

    def create_session(self, user_id, token_hash, csrf_token, now, absolute_seconds):
        now = int(now)
        with self._write() as conn:
            conn.execute(_SESSION_ADD, (token_hash, user_id, csrf_token, now, now, now + int(absolute_seconds)))

    def get_session(self, token_hash):
        with self._connect() as conn:
            return _session(conn.execute(_SESSION_GET, (token_hash,)).fetchone())

    def touch_session(self, token_hash, now):
        with self._write() as conn:
            conn.execute(_SESSION_TOUCH, (int(now), token_hash))

    def delete_session(self, token_hash):
        with self._write() as conn:
            return conn.execute(_SESSION_DELETE, (token_hash,)).rowcount

    def delete_user_sessions(self, user_id):
        with self._write() as conn:
            return conn.execute(_SESSIONS_OF_USER_DELETE, (user_id,)).rowcount

    def cap_sessions(self, user_id, max_n):
        """Keep the user's `max_n` newest sessions (by creation time, then insertion order) and delete the rest. Returns how many were deleted."""
        with self._write() as conn:
            return conn.execute(_SESSIONS_CAP, (user_id, user_id, int(max_n))).rowcount

    def purge_expired(self, now, idle_seconds):
        """Delete sessions that are past their absolute end (expires_at <= now) or idle for idle_seconds or more. Returns how many."""
        now = int(now)
        with self._write() as conn:
            return conn.execute(_SESSIONS_PURGE, (now, int(idle_seconds), now)).rowcount

    # --- invites and sign-up -----------------------------------------------------------------------------------------------------------------------

    def add_invite(self, code_hash, uses, expires_at, note=""):
        with self._write() as conn:
            conn.execute(_INVITE_ADD, (code_hash, int(uses), None if expires_at is None else int(expires_at), int(self._clock()), note))

    def consume_invite_and_add_user(self, code_hash, username, pw_hash, now):
        """One transaction: use one invite and create the user. Returns "created", "invite_invalid" (unknown, used up or expired code: no user is created) or
        "name_taken" (the transaction is rolled back, so the code is NOT used). code_hash None means open sign-up: no invite is involved."""
        now = int(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if code_hash is not None and conn.execute(_INVITE_USE, (code_hash, now)).rowcount != 1:
                    conn.execute("ROLLBACK")
                    return "invite_invalid"
                try:
                    conn.execute(_USER_ADD, (username, pw_hash, now, "local"))
                except sqlite3.IntegrityError as error:
                    if not _is_unique_violation(error):
                        raise
                    conn.execute("ROLLBACK")
                    return "name_taken"
                conn.execute("COMMIT")
                return "created"
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    # --- failed logins --------------------------------------------------------------------------------------------------------------------------

    def failure_key(self, username, ip):
        """HMAC-SHA-256 (hex) of "username|ip" under the random key kept in this database. A "|" or "\\" inside a part is escaped, so two different pairs cannot give
        the same message. The key is stable for one database file and different for another, and the table never holds a user name or an address."""
        message = f"{_escape(username)}|{_escape(ip)}".encode("utf-8", errors="surrogatepass")
        return hmac.new(self._hmac_key(), message, hashlib.sha256).hexdigest()

    def get_failure(self, key):
        with self._connect() as conn:
            return _failure(conn.execute(_FAILURE_GET, (key,)).fetchone())

    def record_failure(self, key, now, window_seconds, delay_for=None):
        """Count one more failed login for `key` and return the new row. If the last failure is older than window_seconds the count starts again at 1.
        delay_for(failures) -> seconds, if given, sets blocked_until = now + delay inside the same transaction (otherwise blocked_until is 0)."""
        now = int(now)
        with self._write() as conn:
            row = conn.execute(_FAILURE_GET, (key,)).fetchone()
            failures = 1 if row is None or now - row["last_failure_at"] > window_seconds else row["failures"] + 1
            blocked_until = now + int(delay_for(failures)) if delay_for is not None else 0
            conn.execute(_FAILURE_SET, (key, failures, now, blocked_until))
        return FailureRow(key, failures, now, blocked_until)

    def clear_failure(self, key):
        with self._write() as conn:
            conn.execute(_FAILURE_CLEAR, (key,))

    def purge_failures(self, older_than):
        """Delete failure rows whose last failure is before `older_than`. Returns how many."""
        with self._write() as conn:
            return conn.execute(_FAILURES_PURGE, (int(older_than),)).rowcount
