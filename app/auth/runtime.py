"""The glue between app/auth and a web request, as plain functions: build the service at start-up, read the client address, decide https, make and read the
cookies, and check where a request came from.

Standard library and app.auth only (no FastAPI, no Starlette). Importing this module reads no environment, creates no file and starts nothing.
Nothing here logs a user name, an address, a token, a hash or the text of AUTH_USERS.

`headers` below means the raw ASGI list of (name, value) pairs (bytes or str), or a mapping; a name matches in any case; a header that appears several
times is seen as several values. The functions that read headers never raise: odd input gives the safe answer (see each function).
"""
import ipaddress
import logging
import re
import time
from collections.abc import Mapping

from app.auth.config import CSRF_COOKIE, SESSION_COOKIE, AuthConfigError, AuthSettings, auth_enabled, cookie_secure
from app.auth.service import AuthService
from app.auth.store import Store, StoreError

logger = logging.getLogger("ops_rag.auth")

MAX_FORWARDED_LENGTH = 2048  # characters of X-Forwarded-For (all its header lines together) that are looked at at all
MAX_PEER_LENGTH = 64  # the same limit the service puts on an address
MAX_COOKIE_HEADER = 16384  # a longer Cookie header line is ignored whole
MAX_COOKIE_VALUE = 512
_COOKIE_VALUE = re.compile(r"[A-Za-z0-9_-]{1,%d}" % MAX_COOKIE_VALUE)
_COOKIE_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")  # an RFC 6265 token
_HOST = re.compile(r"(?P<host>[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::(?P<port>[0-9]{1,5}))?")
_ORIGIN = re.compile(r"(?P<scheme>https?)://(?P<host>[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::(?P<port>[0-9]{1,5}))?", re.IGNORECASE)
_DEFAULT_PORT = {"http": 80, "https": 443}


# --- start-up ---------------------------------------------------------------------------------------------------------------------------------

def build_service(environ=None, root=None):
    """None when AUTH_ENABLED is off (nothing else is read and nothing is created). When it is on: the settings from the environment, the store, the service,
    init() and one purge(), and the service is returned. A wrong setting, a bad AUTH_USERS, an unusable database or an unwritable folder is logged as ONE line
    (the type and the message, which name the variable and never its value) and re-raised unchanged: the caller must stop, never carry on without login."""
    try:
        if not auth_enabled(environ):
            return None
        settings = AuthSettings.from_env(environ, root)
        service = AuthService(Store(settings.db_path, time.time), settings)
        service.init()
        service.purge()
        return service
    except (AuthConfigError, StoreError, OSError) as error:  # ProvisionedUsersError is an AuthConfigError
        logger.error("Auth is enabled but cannot start: %s: %s", type(error).__name__, error)
        raise


# --- headers ----------------------------------------------------------------------------------------------------------------------------------

def _usable(headers):
    return isinstance(headers, (list, tuple, Mapping))


def _text(value):
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("latin-1")
    return value if isinstance(value, str) else None


def header_values(headers, name):
    """Every value of the header `name`, in order, as text. Anything that is not headers gives []."""
    if not _usable(headers):
        return []
    wanted, found = name.lower(), []
    try:
        for key, value in (headers.items() if isinstance(headers, Mapping) else headers):
            key, value = _text(key), _text(value)
            if key is not None and value is not None and key.lower() == wanted:
                found.append(value)
    except (TypeError, ValueError):
        return []
    return found


def _last_entry(values):
    """The rightmost non-empty comma-separated entry of all the header lines taken together (in order), stripped; None when there is none."""
    for entry in reversed(",".join(values).split(",")):
        if entry.strip():
            return entry.strip()
    return None


def _normalised(text):
    """A valid IPv4 or IPv6 address in one canonical spelling (IPv6 compressed and lower-case, IPv4-mapped IPv6 written as IPv4), or None. No port, no
    brackets, no zone."""
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return None
    if address.version == 6:
        if getattr(address, "scope_id", None):
            return None
        if address.ipv4_mapped is not None:
            address = address.ipv4_mapped
    return str(address)


def _peer_text(peer):
    if isinstance(peer, (tuple, list)):
        peer = peer[0] if peer else None
    if not isinstance(peer, str) or not 0 < len(peer.strip()) <= MAX_PEER_LENGTH:
        return ""
    return _normalised(peer.strip()) or peer.strip()


def client_ip(peer, headers, trust_proxy):
    """The address of the client. `peer` is the socket peer: an ASGI (host, port) pair, a host string or None.

    Not trusted (trust_proxy is not exactly True): the peer, whatever the headers say. Trusted: the rightmost non-empty X-Forwarded-For entry, which is the one the
    single trusted proxy appended (the entries to its left are written by the client). If that entry is empty, not an IP address, or carries a port, brackets or
    a zone, the peer is used; a valid entry further left is NEVER used. Several X-Forwarded-For header lines count as one list in the order received, so the
    rightmost entry of the LAST line wins. All the lines together longer than MAX_FORWARDED_LENGTH characters are not looked at (the peer is used). The peer is
    written canonically when it is an IP address, and is "" when it is missing, not text, empty or longer than MAX_PEER_LENGTH."""
    fallback = _peer_text(peer)
    if trust_proxy is not True:
        return fallback
    values = header_values(headers, "x-forwarded-for")
    if not values or sum(len(v) for v in values) + len(values) - 1 > MAX_FORWARDED_LENGTH:
        return fallback
    entry = _last_entry(values)
    return (_normalised(entry) if entry is not None else None) or fallback


def is_https(scheme, headers, trust_proxy):
    """True when the request came over https: the ASGI scheme says so, or (only when trust_proxy is exactly True) the rightmost X-Forwarded-Proto entry is
    "https". The header can only turn it on, never off: a request whose scheme is https stays https whatever the header says. Words other than http and
    https are ignored."""
    if isinstance(scheme, str) and scheme.strip().lower() == "https":
        return True
    if trust_proxy is not True:
        return False
    entry = _last_entry(header_values(headers, "x-forwarded-proto"))
    return entry is not None and entry.lower() == "https"


# --- cookies ----------------------------------------------------------------------------------------------------------------------------------

def build_set_cookie(name, value, settings, is_https, clear=False):
    """The value of a Set-Cookie header. The session cookie is HttpOnly and the CSRF cookie is not (the page reads it); both are SameSite=Lax, Path=/ and
    Secure per cookie_secure(); Max-Age is the absolute session limit. With clear=True the value is empty (the `value` argument is ignored) and Max-Age is 0,
    with the same other attributes. A name other than the two cookie names, or a value that is not 1 to 512 characters of A-Za-z0-9_-, raises ValueError."""
    if name not in (SESSION_COOKIE, CSRF_COOKIE):
        raise ValueError("unknown cookie name")
    if clear:
        value, max_age = "", 0
    else:
        if not isinstance(value, str) or _COOKIE_VALUE.fullmatch(value) is None:
            raise ValueError("a cookie value may only hold letters, digits, _ and -")
        max_age = settings.session_absolute_seconds
    parts = [f"{name}={value}", "Path=/", f"Max-Age={max_age}"]
    if name == SESSION_COOKIE:
        parts.append("HttpOnly")
    parts.append("SameSite=Lax")
    if cookie_secure(settings, is_https=is_https):
        parts.append("Secure")
    return "; ".join(parts)


def parse_cookies(lines):
    """{name: value} from the values of the Cookie header lines (one text, or an iterable of text or bytes). Never raises. Pairs without a name or without "="
    are ignored, and so are names that are not tokens; the value is everything after the first "=", stripped, quotes kept. A name that appears TWICE (in one line
    or across lines, with the same value or not) is absent from the result: the request cannot be trusted to mean one of them. Names are case-sensitive. A line
    longer than MAX_COOKIE_HEADER characters is ignored whole."""
    if isinstance(lines, (str, bytes, bytearray)):
        lines = [lines]
    seen, repeated, cookies = set(), set(), {}
    try:
        for line in lines:
            line = _text(line)
            if line is None or len(line) > MAX_COOKIE_HEADER:
                continue
            for pair in line.split(";"):
                name, equals, value = pair.partition("=")
                name, value = name.strip(), value.strip()
                if not equals or _COOKIE_NAME.fullmatch(name) is None or any(ord(c) < 32 or ord(c) == 127 for c in value):
                    continue
                if name in seen:
                    repeated.add(name)
                seen.add(name)
                cookies[name] = value
    except (TypeError, ValueError):
        return {}
    return {name: value for name, value in cookies.items() if name not in repeated}


# --- where a request came from ----------------------------------------------------------------------------------------------------------------

def _host_and_port(match, default_port):
    port = int(match.group("port")) if match.group("port") else default_port
    return (match.group("host").lower(), port) if 1 <= port <= 65535 else None


def origin_ok(headers):
    """True when the request may come from a page of this site. Never raises; anything unclear is False, except that no information at all is True (a client
    that is not a browser sends neither header).

    Origin present (exactly one line): it must be scheme://host[:port] and nothing else, and its host and port must equal the single Host header (host in any case,
    a missing port meaning 80 for http and 443 for https). "null", an empty or unparsable Origin, a path, user name or query, or a missing or repeated Host is
    refused. The scheme is not compared with the real one (the server cannot see it behind a proxy). Sec-Fetch-Site is then not consulted.
    Origin absent: Sec-Fetch-Site, if present (exactly one line), must be same-origin or none, in any case.
    Neither: True. Not headers at all: False."""
    if not _usable(headers):
        return False
    try:
        origins = header_values(headers, "origin")
        if origins:
            if len(origins) != 1:
                return False
            origin = _ORIGIN.fullmatch(origins[0].strip())
            hosts = header_values(headers, "host")
            host = _HOST.fullmatch(hosts[0].strip()) if len(hosts) == 1 else None
            if origin is None or host is None:
                return False
            default = _DEFAULT_PORT[origin.group("scheme").lower()]
            wanted = _host_and_port(origin, default)
            return wanted is not None and wanted == _host_and_port(host, default)
        fetch_site = header_values(headers, "sec-fetch-site")
        if fetch_site:
            return len(fetch_site) == 1 and fetch_site[0].strip().lower() in ("same-origin", "none")
        return True
    except (TypeError, ValueError, AttributeError):
        return False
