"""The login rules of the optional per-user accounts: sign-up, log-in, sessions, CSRF tokens, invites and the delay after failed logins.

No routes and no cookies here: a request handler calls these methods later. Everything that depends on time uses the injected clock and nothing ever
sleeps: a delay is returned as a number for the caller to apply. Nothing is read from the environment, nothing is created at import, and nothing is logged
(no user names, addresses, passwords, tokens or codes). Every public method survives odd argument types: it answers with the generic failure.

Brute-force protection (B1 of the approved design):
  * untrusted mode (the default, settings.trust_proxy False): a login is NEVER refused because of earlier failures and a correct password always logs in
    at once. Only a FAILED answer carries .delay (at most settings.delay_cap_untrusted_seconds). There is no per-client throttle: behind a proxy every
    client has the same address, so the failures of one name are counted per name for everybody.
  * trusted mode (settings.trust_proxy True): failures are counted per (user name, client address); after settings.delay_free failures the next attempts
    are refused (.retry_after) until a delay of min(cap, base * 2**(failures - free - 1)) seconds has passed; a success clears the count.
  * both modes: hashing goes through the gate; a full gate gives .busy and records no failure.
"""
import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass

from app.auth import passwords
from app.auth.passwords import Busy, RuleError

TOKEN_LENGTH = len(secrets.token_urlsafe(32))  # 43 characters
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{%d}" % TOKEN_LENGTH)
MAX_INVITE_USES = 1000
MAX_INVITE_NOTE = 200
MAX_INVITE_LIFETIME_SECONDS = 10 * 365 * 24 * 3600
MAX_INVITE_CODE_INPUT = 200
MAX_IP_INPUT = 64
MAX_CSRF_INPUT = 200
_MAX_DELAY_EXPONENT = 20


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def delay_for(settings, failures, trusted):
    """Seconds to wait after `failures` failures in the window: 0 up to settings.delay_free failures, then base * 2**(failures - free - 1), at most the cap
    of the mode (delay_cap_seconds when trusted, delay_cap_untrusted_seconds otherwise). With the defaults and trusted: 0, 0, 0, 2, 4, 8, 16, 32, 60, 60."""
    if failures <= settings.delay_free:
        return 0
    steps = min(failures - settings.delay_free - 1, _MAX_DELAY_EXPONENT)
    cap = settings.delay_cap_seconds if trusted else settings.delay_cap_untrusted_seconds
    return min(cap, settings.delay_base_seconds * 2**steps)


@dataclass(frozen=True)
class SignUpResult:
    """status: created | invalid_input (code is a RuleError code) | closed | rejected | busy.
    "rejected" is ONE answer for a missing or wrong or used-up invite code and for a taken name (the design: the answer must not tell them apart)."""
    status: str
    code: str = ""
    username: str | None = None


@dataclass(frozen=True)
class LoginResult:
    ok: bool
    session_token: str | None = None  # shown once; only its SHA-256 is stored
    csrf_token: str | None = None
    username: str | None = None
    delay: int = 0  # seconds the caller should wait before it sends a FAILED answer (never set on a success)
    retry_after: int | None = None  # set only when the attempt was refused (trusted mode) and not even checked
    busy: bool = False  # the hashing gate was full: nothing was checked and nothing was recorded


@dataclass(frozen=True)
class SessionInfo:
    user_id: int
    username: str
    csrf_token: str


class AuthService:
    def __init__(self, store, settings, clock=time.time, gate=None, *, token_factory=None):
        self._store = store
        self._settings = settings
        self._clock = clock
        self._gate = gate
        self._new_token = token_factory or (lambda: secrets.token_urlsafe(32))

    @property
    def settings(self):
        return self._settings

    def init(self):
        """Create the database if needed and make the provisioned users equal to AUTH_USERS (an empty or missing AUTH_USERS removes them). A bad AUTH_USERS
        raises ProvisionedUsersError (a config error naming only the entry number). Without a gate argument, the process gate is set from the settings."""
        self._store.init_schema()
        users = passwords.parse_provisioned_users(self._settings.users_text) if self._settings.provisioned else {}
        self._store.sync_provisioned(users)
        if self._gate is None:
            passwords.configure_gate(self._settings)

    # --- sign-up --------------------------------------------------------------------------------------------------------------------------------

    def sign_up(self, username, password, invite_code=None):
        """Create an account; never a session. The mode comes first, then the rules (before any hashing), then one hash through the gate, then one transaction.
        A missing, wrong, used-up or expired code and a taken name all give the same "rejected" answer after the same work."""
        mode = self._settings.effective_signup
        if mode == "closed":
            return SignUpResult("closed", "signup_closed")
        try:
            name = passwords.normalise_username(username)
            checked = passwords.check_password(password, name)
        except RuleError as error:
            return SignUpResult("invalid_input", error.code)
        try:
            pw_hash = passwords.hash_password(checked, self._settings, gate=self._gate)
        except Busy:
            return SignUpResult("busy", "busy")
        code_hash = None
        if mode == "invite":
            code = invite_code.strip() if isinstance(invite_code, str) and len(invite_code) <= MAX_INVITE_CODE_INPUT else ""
            code_hash = _digest(code)
        outcome = self._store.consume_invite_and_add_user(code_hash, name, pw_hash, int(self._clock()))
        return SignUpResult("created", "", name) if outcome == "created" else SignUpResult("rejected", "signup_rejected")

    def create_invite(self, uses=1, expires_in=None, note=""):
        """A new invite code (returned once; only its SHA-256 is stored). uses 1..1000, expires_in None or seconds up to ten years, note up to 200 characters."""
        if isinstance(uses, bool) or not isinstance(uses, int) or not 1 <= uses <= MAX_INVITE_USES:
            raise ValueError(f"uses must be a whole number from 1 to {MAX_INVITE_USES}")
        if expires_in is not None and (isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)) or not 1 <= expires_in <= MAX_INVITE_LIFETIME_SECONDS):
            raise ValueError("expires_in must be None or a number of seconds between 1 and ten years")
        if not isinstance(note, str) or len(note) > MAX_INVITE_NOTE:
            raise ValueError(f"note must be text of at most {MAX_INVITE_NOTE} characters")
        code = secrets.token_urlsafe(16)
        expires_at = None if expires_in is None else int(self._clock()) + int(expires_in)
        self._store.add_invite(_digest(code), uses, expires_at, note)
        return code

    # --- log-in -----------------------------------------------------------------------------------------------------------------------------------

    def _allowed(self, user):
        """Only provisioned users log in while users are provisioned; only local users otherwise (no mix-ups). A disabled user never does."""
        return user is not None and not user.disabled and (user.source == "provisioned") == self._settings.provisioned

    def _key(self, username, ip):
        trusted = self._settings.trust_proxy
        try:
            name = passwords.normalise_username(username)
        except RuleError:
            name = "raw:" + hashlib.sha256(repr(username)[:300].encode("utf-8", "replace")).hexdigest()[:32]
        address = ip[:MAX_IP_INPUT] if trusted and isinstance(ip, str) else ""
        return self._store.failure_key(name, address)

    def log_in(self, username, password, ip=""):
        settings, trusted = self._settings, self._settings.trust_proxy
        now = int(self._clock())
        key = self._key(username, ip)
        if trusted:
            row = self._store.get_failure(key)
            if row is not None and row.blocked_until > now:
                return LoginResult(False, retry_after=row.blocked_until - now)
        try:
            name = passwords.normalise_username(username)
        except RuleError:
            name = None
        user = self._store.get_user(name) if name is not None else None
        try:
            if self._allowed(user):
                success = passwords.verify_password(password, user.pw_hash, gate=self._gate)
            else:
                passwords.verify_unknown(password, settings, gate=self._gate)
                success = False
        except Busy:
            return LoginResult(False, busy=True)
        if not success:
            row = self._store.record_failure(key, now, settings.delay_window_seconds, (lambda f: delay_for(settings, f, True)) if trusted else None)
            return LoginResult(False, delay=0 if trusted else delay_for(settings, row.failures, False))
        if user.source == "local":
            try:
                if passwords.needs_rehash(user.pw_hash, settings):
                    self._store.set_pw_hash(user.id, passwords.hash_password(password, settings, gate=self._gate))
            except Busy:
                pass  # the login stands; the hash is upgraded at a later login
        self._store.mark_login(user.id)
        token, csrf = self._new_token(), self._new_token()
        self._store.create_session(user.id, _digest(token), csrf, now, settings.session_absolute_seconds)
        self._store.cap_sessions(user.id, settings.max_sessions_per_user)
        self._store.clear_failure(key)
        self.purge()
        return LoginResult(True, session_token=token, csrf_token=csrf, username=user.username)

    # --- sessions -------------------------------------------------------------------------------------------------------------------------------

    def validate_session(self, token):
        """The session behind a token, or None: unknown, past its absolute end, idle for too long, or its user is disabled or not allowed (the dead session is
        deleted). last_seen is rewritten only when at least settings.session_touch_seconds have passed. Garbage input gives None."""
        if not isinstance(token, str) or not _TOKEN_PATTERN.fullmatch(token):
            return None
        settings, now = self._settings, int(self._clock())
        token_hash = _digest(token)
        row = self._store.get_session(token_hash)
        if row is None:
            return None
        user = self._store.get_user_by_id(row.user_id)
        if now >= row.expires_at or row.last_seen_at + settings.session_idle_seconds <= now or not self._allowed(user):
            self._store.delete_session(token_hash)
            return None
        if now - row.last_seen_at >= settings.session_touch_seconds:
            self._store.touch_session(token_hash, now)
        return SessionInfo(user.id, user.username, row.csrf_token)

    def verify_csrf(self, session, supplied):
        """True if `supplied` equals the session's CSRF token (compared with hmac.compare_digest). `session` is a SessionInfo or a session token."""
        info = session if isinstance(session, SessionInfo) else self.validate_session(session)
        if info is None or not isinstance(supplied, str) or len(supplied) > MAX_CSRF_INPUT:
            return False
        try:
            return hmac.compare_digest(supplied.encode("utf-8"), info.csrf_token.encode("utf-8"))
        except UnicodeError:
            return False

    def log_out(self, token):
        if not isinstance(token, str) or not _TOKEN_PATTERN.fullmatch(token):
            return False
        return self._store.delete_session(_digest(token)) == 1

    def log_out_everywhere(self, token):
        """Delete every session of the token's user (the token must be a live session). Returns how many were deleted."""
        info = self.validate_session(token)
        return 0 if info is None else self._store.delete_user_sessions(info.user_id)

    def purge(self):
        """Delete expired sessions and forgotten failure rows. Cheap and safe to call often. Returns (sessions, failures) deleted."""
        now = int(self._clock())
        return (self._store.purge_expired(now, self._settings.session_idle_seconds), self._store.purge_failures(now - self._settings.delay_window_seconds))
