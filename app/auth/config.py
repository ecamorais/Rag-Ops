"""Settings for the optional per-user login (app/auth).

Nothing here reads the environment when the module is imported: call AuthSettings.from_env() explicitly. With enabled=False (the default) the rest of
the app does not use any of this. Every time and delay value is a starting value that has not been measured against real traffic.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

SESSION_COOKIE = "ops_session"
CSRF_COOKIE = "ops_csrf"
CSRF_HEADER = "x-csrf-token"

SIGNUP_MODES = ("closed", "invite", "open")
COOKIE_SECURE_MODES = ("auto", "always")
MIN_SCRYPT_N_FROM_ENV = 2**14  # the environment cannot ask for a weaker cost; tests build AuthSettings directly with a cheaper one
MAX_SCRYPT_N = 2**17
MAX_UNTRUSTED_DELAY_SECONDS = 10


class AuthConfigError(ValueError):
    """A setting is wrong. The message names the setting, never a secret value (AUTH_USERS is never echoed)."""


@dataclass(frozen=True)
class AuthSettings:
    enabled: bool = False
    db_path: Path = Path("var/auth.db")
    signup: str = "invite"  # closed | invite | open (always closed when users are provisioned)
    cookie_secure: str = "auto"  # auto: Secure only on https | always
    trust_proxy: bool = False  # True: the client IP is trusted, so failures are tracked per (username, IP); False: no per-client throttle
    scrypt_n: int = 2**15
    scrypt_r: int = 8
    scrypt_p: int = 1
    session_idle_seconds: int = 12 * 3600
    session_absolute_seconds: int = 7 * 24 * 3600
    session_touch_seconds: int = 300  # last_seen is rewritten at most this often
    max_sessions_per_user: int = 10
    delay_free: int = 3  # failures per key before any delay
    delay_base_seconds: int = 2
    delay_cap_seconds: int = 60  # trusted-IP mode: the longest refusal
    delay_cap_untrusted_seconds: int = 3  # untrusted mode: the longest delay of a FAILED answer (a correct password is never delayed or refused)
    delay_window_seconds: int = 900  # failures older than this are forgotten
    hash_queue_max: int = 8  # logins allowed to wait for the one hashing slot; more get "busy"
    hash_timeout_seconds: int = 10  # the longest a login waits for the hashing slot
    users_text: str = field(default="", repr=False)  # AUTH_USERS: provisioned users, a secret (kept out of repr)

    def __post_init__(self):
        problems = []
        if not isinstance(self.enabled, bool) or not isinstance(self.trust_proxy, bool):
            problems.append("enabled and trust_proxy must be true or false")
        if self.signup not in SIGNUP_MODES:
            problems.append("signup must be one of " + ", ".join(SIGNUP_MODES))
        if self.cookie_secure not in COOKIE_SECURE_MODES:
            problems.append("cookie_secure must be one of " + ", ".join(COOKIE_SECURE_MODES))
        n = self.scrypt_n
        if not (isinstance(n, int) and 16 <= n <= MAX_SCRYPT_N and n & (n - 1) == 0):
            problems.append(f"scrypt_n must be a power of two from 16 to {MAX_SCRYPT_N}")
        if not (isinstance(self.scrypt_r, int) and 1 <= self.scrypt_r <= 16):
            problems.append("scrypt_r must be from 1 to 16")
        if not (isinstance(self.scrypt_p, int) and 1 <= self.scrypt_p <= 4):
            problems.append("scrypt_p must be from 1 to 4")
        for name in ("session_idle_seconds", "session_absolute_seconds", "delay_window_seconds", "hash_timeout_seconds", "delay_base_seconds"):
            if not (isinstance(getattr(self, name), int) and getattr(self, name) >= 1):
                problems.append(f"{name} must be a whole number of at least 1")
        for name in ("session_touch_seconds", "delay_free", "delay_cap_seconds", "delay_cap_untrusted_seconds", "hash_queue_max"):
            if not (isinstance(getattr(self, name), int) and getattr(self, name) >= 0):
                problems.append(f"{name} must be a whole number of at least 0")
        if not (isinstance(self.max_sessions_per_user, int) and self.max_sessions_per_user >= 1):
            problems.append("max_sessions_per_user must be at least 1")
        if isinstance(self.session_idle_seconds, int) and isinstance(self.session_absolute_seconds, int) and self.session_idle_seconds > self.session_absolute_seconds:
            problems.append("session_idle_seconds must not be longer than session_absolute_seconds")
        if isinstance(self.delay_cap_untrusted_seconds, int) and self.delay_cap_untrusted_seconds > MAX_UNTRUSTED_DELAY_SECONDS:
            problems.append(f"delay_cap_untrusted_seconds must be at most {MAX_UNTRUSTED_DELAY_SECONDS}")
        if not isinstance(self.users_text, str):
            problems.append("users_text must be text")
        if problems:
            raise AuthConfigError("; ".join(problems))

    @property
    def provisioned(self) -> bool:
        """True when AUTH_USERS names users: the server then loads them at start-up and sign-up is closed."""
        return bool(self.users_text.strip())

    @property
    def effective_signup(self) -> str:
        return "closed" if self.provisioned else self.signup

    @classmethod
    def from_env(cls, environ=None, root=None):
        """Read the AUTH_* variables (the process environment unless `environ` is given). Wrong values raise AuthConfigError: the app fails closed."""
        env = os.environ if environ is None else environ
        base = Path(root) if root is not None else PROJECT_ROOT
        raw_db = _text(env, "AUTH_DB", "").strip()
        db_path = (Path(raw_db) if Path(raw_db).is_absolute() else base / raw_db) if raw_db else base / "var" / "auth.db"
        n = _integer(env, "AUTH_SCRYPT_N", 2**15, 1, MAX_SCRYPT_N)
        if n < MIN_SCRYPT_N_FROM_ENV:
            raise AuthConfigError(f"AUTH_SCRYPT_N must be at least {MIN_SCRYPT_N_FROM_ENV}")
        return cls(
            enabled=_flag(env, "AUTH_ENABLED", False),
            db_path=db_path,
            signup=_choice(env, "AUTH_SIGNUP", "invite", SIGNUP_MODES),
            cookie_secure=_choice(env, "AUTH_COOKIE_SECURE", "auto", COOKIE_SECURE_MODES),
            trust_proxy=_flag(env, "AUTH_TRUST_PROXY", False),
            scrypt_n=n,
            scrypt_r=_integer(env, "AUTH_SCRYPT_R", 8, 1, 16),
            scrypt_p=_integer(env, "AUTH_SCRYPT_P", 1, 1, 4),
            session_idle_seconds=_integer(env, "AUTH_SESSION_IDLE_SECONDS", 12 * 3600, 1, 10**9),
            session_absolute_seconds=_integer(env, "AUTH_SESSION_ABSOLUTE_SECONDS", 7 * 24 * 3600, 1, 10**9),
            session_touch_seconds=_integer(env, "AUTH_SESSION_TOUCH_SECONDS", 300, 0, 10**9),
            max_sessions_per_user=_integer(env, "AUTH_MAX_SESSIONS", 10, 1, 1000),
            delay_free=_integer(env, "AUTH_DELAY_FREE", 3, 0, 1000),
            delay_base_seconds=_integer(env, "AUTH_DELAY_BASE", 2, 1, 3600),
            delay_cap_seconds=_integer(env, "AUTH_DELAY_CAP", 60, 0, 86400),
            delay_cap_untrusted_seconds=_integer(env, "AUTH_DELAY_CAP_UNTRUSTED", 3, 0, MAX_UNTRUSTED_DELAY_SECONDS),
            delay_window_seconds=_integer(env, "AUTH_DELAY_WINDOW", 900, 1, 10**9),
            hash_queue_max=_integer(env, "AUTH_HASH_QUEUE", 8, 0, 1000),
            hash_timeout_seconds=_integer(env, "AUTH_HASH_TIMEOUT", 10, 1, 600),
            users_text=_text(env, "AUTH_USERS", ""),
        )


def cookie_secure(settings, *, is_https):
    """Whether the cookies get the Secure flag: always with 'always', else only when the request is https (the route decides is_https)."""
    return True if settings.cookie_secure == "always" else bool(is_https)


def auth_enabled(environ=None):
    """Whether AUTH_ENABLED is on, reading that one variable only (the process environment unless `environ` is given). The app asks this first: with auth off no
    other AUTH_* variable is read, so a typo in one of them cannot stop a server that runs without login. A value that is neither true nor false raises AuthConfigError."""
    env = os.environ if environ is None else environ
    return _flag(env, "AUTH_ENABLED", False)


def _text(env, name, default):
    value = env.get(name)
    return default if value is None else value


def _flag(env, name, default):
    value = env.get(name)
    if value is None or not value.strip():
        return default
    word = value.strip().lower()
    if word in ("1", "true", "yes", "on"):
        return True
    if word in ("0", "false", "no", "off"):
        return False
    raise AuthConfigError(f"{name} must be true or false")


def _integer(env, name, default, low, high):
    value = env.get(name)
    if value is None or not value.strip():
        return default
    try:
        number = int(value.strip())
    except ValueError:
        raise AuthConfigError(f"{name} must be a whole number") from None
    if not low <= number <= high:
        raise AuthConfigError(f"{name} must be between {low} and {high}")
    return number


def _choice(env, name, default, allowed):
    value = env.get(name)
    if value is None or not value.strip():
        return default
    word = value.strip().lower()
    if word not in allowed:
        raise AuthConfigError(f"{name} must be one of " + ", ".join(allowed))
    return word
