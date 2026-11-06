"""User name and password rules, scrypt hashing and the gate that keeps hashing from piling up (standard library only).

Stored format: scrypt$N$r$p$<salt>$<hash>, the last two as unpadded URL-safe base64 of 16 and 32 bytes. Passwords are NFKC-normalised inside
hash_password and verify_password, so a caller cannot forget it. Nothing here logs, and no exception message holds a password, a hash, a salt or a
user name.
"""
import base64
import hashlib
import hmac
import re
import secrets
import threading
import unicodedata
from contextlib import contextmanager

from app.auth.config import AuthConfigError

SALT_BYTES = 16
HASH_BYTES = 32
MIN_PASSWORD_CHARS = 12
MAX_PASSWORD_CHARS = 128  # counted after NFKC
MAX_PASSWORD_BYTES = 512  # UTF-8 bytes of the password as typed
MAX_USERNAME_INPUT = 256  # longer input is refused before it is normalised
MAX_STORED_LENGTH = 200
MAX_N = 2**17
MAX_R = 16
MAX_P = 4
USERNAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{2,31}")
_NUMBER = re.compile(r"[1-9][0-9]{0,9}")
_B64 = re.compile(r"[A-Za-z0-9_-]+")
_SALT_TEXT_LENGTH = 22  # 16 bytes, unpadded base64
_HASH_TEXT_LENGTH = 43  # 32 bytes, unpadded base64

MESSAGES = {
    "username_invalid": "The user name must be 3 to 32 characters: letters, digits, dot, underscore or hyphen, starting with a letter or digit.",
    "password_invalid": "The password is not valid text.",
    "password_too_long": "The password is too long (at most 128 characters).",
    "password_too_short": "The password is too short (at least 12 characters).",
    "password_nul": "The password must not contain a NUL character.",
    "password_repeated": "The password must not be one character repeated.",
    "password_equals_username": "The password must not be the same as the user name.",
}


class RuleError(ValueError):
    """A user name or password breaks a rule. `code` is stable (a key of MESSAGES); the message is fixed text with no value in it."""

    def __init__(self, code):
        super().__init__(MESSAGES[code])
        self.code = code


class Busy(RuntimeError):
    """The hashing gate is full or the wait ran out; nothing was hashed. `reason` is "queue_full" or "timeout"."""

    def __init__(self, reason):
        super().__init__("The server is busy; try again shortly.")
        self.reason = reason


class ProvisionedUsersError(AuthConfigError):
    """AUTH_USERS is wrong. Only the number of the entry (counted from 1) is named, never the text."""

    def __init__(self, entry, problem):
        super().__init__(f"AUTH_USERS entry {entry} {problem}")
        self.entry = entry


# --- the gate ---------------------------------------------------------------------------------------------------------------------------

class HashGate:
    """At most one scrypt at a time. Up to `max_waiting` callers may wait for the slot, each for at most `timeout_seconds`; one more caller, or a
    caller whose wait ran out, gets Busy without any hashing having been done. The slot is released on every path, exceptions included."""

    def __init__(self, max_waiting, timeout_seconds):
        self.max_waiting = max_waiting
        self.timeout_seconds = timeout_seconds
        self._cond = threading.Condition()
        self._running = False
        self._waiting = 0

    @property
    def running(self):
        with self._cond:
            return self._running

    @property
    def waiting(self):
        with self._cond:
            return self._waiting

    def acquire(self):
        with self._cond:
            if self._running or self._waiting:
                if self._waiting >= self.max_waiting:
                    raise Busy("queue_full")
                self._waiting += 1
                try:
                    got_it = self._cond.wait_for(lambda: not self._running, timeout=self.timeout_seconds)
                finally:
                    self._waiting -= 1
                if not got_it:
                    raise Busy("timeout")
            self._running = True

    def release(self):
        with self._cond:
            self._running = False
            self._cond.notify_all()

    @contextmanager
    def slot(self):
        self.acquire()
        try:
            yield
        finally:
            self.release()


# One gate per process, created on first use with the defaults of AuthSettings (8 waiting, 10 s) unless configure_gate() was called earlier. The service
# calls configure_gate(settings) once at start-up. Every public function also takes gate=..., and tests either pass their own gate or call set_gate().
_gate = None
_gate_lock = threading.Lock()


def get_gate():
    global _gate
    with _gate_lock:
        if _gate is None:
            _gate = HashGate(max_waiting=8, timeout_seconds=10)
        return _gate


def configure_gate(settings):
    return set_gate(HashGate(settings.hash_queue_max, settings.hash_timeout_seconds))


def set_gate(gate):
    """Replace the process gate (None forgets it, so the next use creates a default one). Returns the new gate."""
    global _gate
    with _gate_lock:
        _gate = gate
        return gate


# --- user names and passwords -------------------------------------------------------------------------------------------------------------

def normalise_username(raw):
    """NFKC, strip, lower-case; the result must match USERNAME_PATTERN. Raises RuleError("username_invalid")."""
    if not isinstance(raw, str) or len(raw) > MAX_USERNAME_INPUT:
        raise RuleError("username_invalid")
    name = unicodedata.normalize("NFKC", raw).strip().lower()
    if not USERNAME_PATTERN.fullmatch(name):
        raise RuleError("username_invalid")
    return name


def _normalise_for_use(password):
    """The NFKC form of a password after the input limits, before any hashing. Raises RuleError (password_invalid / password_too_long)."""
    if not isinstance(password, str):
        raise RuleError("password_invalid")
    if len(password) > MAX_PASSWORD_BYTES:  # every character is at least one byte
        raise RuleError("password_too_long")
    try:
        raw_bytes = password.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate
        raise RuleError("password_invalid") from None
    if len(raw_bytes) > MAX_PASSWORD_BYTES:
        raise RuleError("password_too_long")
    normalised = unicodedata.normalize("NFKC", password)
    if len(normalised) > MAX_PASSWORD_CHARS:
        raise RuleError("password_too_long")
    return normalised


def check_password(password, username):
    """The sign-up rules. Returns the NFKC-normalised password or raises RuleError. The username is only compared (case-insensitive)."""
    normalised = _normalise_for_use(password)
    if "\x00" in normalised:
        raise RuleError("password_nul")
    if len(normalised) < MIN_PASSWORD_CHARS:
        raise RuleError("password_too_short")
    if len(set(normalised)) == 1:
        raise RuleError("password_repeated")
    if isinstance(username, str) and normalised.lower() == unicodedata.normalize("NFKC", username).strip().lower():
        raise RuleError("password_equals_username")
    return normalised


# --- scrypt -----------------------------------------------------------------------------------------------------------------------------------

def _maxmem(n, r, p):
    """OpenSSL needs 128*r*(N + p + 2) bytes; one MiB of slack is added. (The default limit of hashlib.scrypt, 32 MiB, is too small for N=2**15, r=8.)"""
    return 128 * r * (n + p + 2) + 2**20


def _derive(password_bytes, salt, n, r, p):
    """The only place that calls hashlib.scrypt."""
    return hashlib.scrypt(password_bytes, salt=salt, n=n, r=r, p=p, maxmem=_maxmem(n, r, p), dklen=HASH_BYTES)


def _scrypt(password_bytes, salt, n, r, p, gate):
    with (gate or get_gate()).slot():
        return _derive(password_bytes, salt, n, r, p)


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def parse_hash(stored):
    """(n, r, p, salt, digest) of a stored hash, or None if it is malformed or its parameters are outside the bounds. Never raises."""
    if not isinstance(stored, str) or len(stored) > MAX_STORED_LENGTH:
        return None
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return None
    if not all(_NUMBER.fullmatch(x) for x in parts[1:4]):
        return None
    n, r, p = (int(x) for x in parts[1:4])
    if not (2 <= n <= MAX_N and n & (n - 1) == 0 and 1 <= r <= MAX_R and 1 <= p <= MAX_P):
        return None
    salt_text, hash_text = parts[4], parts[5]
    if not (_B64.fullmatch(salt_text) and len(salt_text) == _SALT_TEXT_LENGTH and _B64.fullmatch(hash_text) and len(hash_text) == _HASH_TEXT_LENGTH):
        return None
    try:
        return n, r, p, _unb64(salt_text), _unb64(hash_text)
    except ValueError:
        return None


def hash_password(password, settings, *, gate=None):
    """scrypt$N$r$p$salt$hash with the configured cost and a fresh 16-byte salt. Raises RuleError for an over-limit password, Busy when the gate is full."""
    normalised = _normalise_for_use(password)
    salt = secrets.token_bytes(SALT_BYTES)
    digest = _scrypt(normalised.encode("utf-8"), salt, settings.scrypt_n, settings.scrypt_r, settings.scrypt_p, gate)
    return f"scrypt${settings.scrypt_n}${settings.scrypt_r}${settings.scrypt_p}${_b64(salt)}${_b64(digest)}"


def verify_password(password, stored, *, gate=None):
    """True if the password matches the stored hash. An over-limit password or a malformed or out-of-bounds stored value gives False before any scrypt
    work and never raises. A hash made with weaker parameters still verifies. Raises Busy only when the gate is full."""
    try:
        normalised = _normalise_for_use(password)
    except RuleError:
        return False
    parsed = parse_hash(stored)
    if parsed is None:
        return False
    n, r, p, salt, digest = parsed
    return hmac.compare_digest(_scrypt(normalised.encode("utf-8"), salt, n, r, p, gate), digest)


_dummy = None
_dummy_lock = threading.Lock()


def _dummy_values():
    """A fixed random salt and digest, created on first use (not at import) and kept for the life of the process."""
    global _dummy
    with _dummy_lock:
        if _dummy is None:
            _dummy = (secrets.token_bytes(SALT_BYTES), secrets.token_bytes(HASH_BYTES))
        return _dummy


def verify_unknown(password, settings, *, gate=None):
    """For a user name that does not exist: the same scrypt work as verify_password with the configured cost, against a dummy value. Always False.
    An over-limit password is answered at once with no work, exactly like verify_password does."""
    try:
        normalised = _normalise_for_use(password)
    except RuleError:
        return False
    salt, digest = _dummy_values()
    hmac.compare_digest(_scrypt(normalised.encode("utf-8"), salt, settings.scrypt_n, settings.scrypt_r, settings.scrypt_p, gate), digest)
    return False


def needs_rehash(stored, settings):
    """True if the stored hash is weaker than the configured cost in any of N, r or p (that includes "different": a mix of higher and lower), or is not
    a valid hash; False if it is equal or stronger in all three."""
    parsed = parse_hash(stored)
    if parsed is None:
        return True
    n, r, p, _, _ = parsed
    return n < settings.scrypt_n or r < settings.scrypt_r or p < settings.scrypt_p


# --- provisioned users ------------------------------------------------------------------------------------------------------------------------

def parse_provisioned_users(text):
    """AUTH_USERS: entries `name:scrypt$...` separated by whitespace or commas. Returns {normalised name: stored hash}. A malformed entry, or a name that
    repeats after normalisation, raises ProvisionedUsersError naming only the entry number."""
    users = {}
    for number, entry in enumerate((e for e in re.split(r"[\s,]+", text.strip()) if e), start=1):
        name, colon, stored = entry.partition(":")
        if not colon or parse_hash(stored) is None:
            raise ProvisionedUsersError(number, "is not valid: expected user name, a colon and a scrypt hash")
        try:
            name = normalise_username(name)
        except RuleError:
            raise ProvisionedUsersError(number, "has a user name that breaks the user name rules") from None
        if name in users:
            raise ProvisionedUsersError(number, "repeats a user name")
        users[name] = stored
    return users
