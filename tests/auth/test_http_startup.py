"""app/auth/runtime.py: building the service at start-up, the client address, https, the cookies and the origin check (Phase 3, step B).

Unit tests only: no TestClient, no app.main. Expected values come from the approved Phase 3 plan and the step B request, not from running the code:
build_service reads only AUTH_ENABLED when auth is off and never falls back to "off" on an error; the client address is the socket peer unless the proxy is
trusted, then the rightmost X-Forwarded-For entry; the cookie flags are HttpOnly (session only), SameSite=Lax, Path=/, Secure per the mode, Max-Age the absolute
limit; a cookie name seen twice is absent; Origin must equal Host, "null" is refused. Tests named pinned_* record behaviour the plan leaves open.
"""
import json
import logging
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app.auth import passwords, runtime
from app.auth.config import CSRF_COOKIE, SESSION_COOKIE, AuthConfigError, AuthSettings
from app.auth.passwords import ProvisionedUsersError
from app.auth.runtime import (MAX_COOKIE_HEADER, MAX_COOKIE_VALUE, MAX_FORWARDED_LENGTH, MAX_PEER_LENGTH, build_service, build_set_cookie, client_ip, header_values,
                              is_https, origin_ok, parse_cookies)
from app.auth.service import AuthService
from app.auth.store import StoreError

ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "correct horse battery staple"
TOKEN = "A" * 43
CHEAP_N = 2**14  # the smallest cost the environment may ask for


def h(*pairs):
    """Headers as a list of (name, value) text pairs (the functions accept text and bytes; the bytes form is tested on its own)."""
    return [(name, value) for name, value in pairs]


# --- importing reads and creates nothing -----------------------------------------------------------------------------------------------------------------

IMPORT_PROOF = r"""
import faulthandler, json, os, sys, threading
faulthandler.dump_traceback_later(60, exit=True)
sys.path.insert(0, sys.argv[1])
reads = []

class Recorder(dict):
    def __getitem__(self, k): reads.append(k); return super().__getitem__(k)
    def get(self, k, d=None): reads.append(k); return super().get(k, d)
    def __contains__(self, k): reads.append(k); return super().__contains__(k)
    def __iter__(self): reads.append("<iter>"); return super().__iter__()
    def keys(self): reads.append("<keys>"); return super().keys()
    def items(self): reads.append("<items>"); return super().items()
    def __len__(self): reads.append("<len>"); return super().__len__()

os.environ = Recorder(dict(os.environ))
import logging
before = set(sys.modules)
import app.auth.runtime
imported = sorted(m for m in set(sys.modules) - before if m == "app" or m.startswith(("app.", "dotenv", "fastapi", "starlette", "uvicorn")))
logger = logging.getLogger("ops_rag.auth")
print(json.dumps({"reads": reads, "imported": imported, "dotenv": "dotenv" in sys.modules, "web": [m for m in ("fastapi", "starlette", "uvicorn") if m in sys.modules],
                  "threads": threading.active_count(), "handlers": len(logger.handlers), "cwd": sorted(os.listdir("."))}))
"""


@pytest.fixture(scope="module")
def import_proof(tmp_path_factory):
    work = tmp_path_factory.mktemp("import_proof")
    result = subprocess.run([sys.executable, "-B", "-c", IMPORT_PROOF, str(ROOT)], capture_output=True, text=True, timeout=120, cwd=work)
    assert result.returncode == 0, result.stderr[-800:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_importing_runtime_reads_no_environment_variable(import_proof):
    assert import_proof["reads"] == []


def test_importing_runtime_loads_only_the_standard_library_and_app_auth(import_proof):
    assert set(import_proof["imported"]) <= {"app", "app.auth", "app.auth.config", "app.auth.passwords", "app.auth.service", "app.auth.store", "app.auth.runtime"}
    assert import_proof["web"] == [] and import_proof["dotenv"] is False


def test_importing_runtime_starts_nothing_and_creates_no_file(import_proof):
    assert import_proof["threads"] == 1 and import_proof["handlers"] == 0
    assert import_proof["cwd"] == []  # the folder the process ran in is still empty


def test_the_cookie_names_are_the_approved_ones():
    assert (SESSION_COOKIE, CSRF_COOKIE) == ("ops_session", "ops_csrf")
    assert (runtime.SESSION_COOKIE, runtime.CSRF_COOKIE) == ("ops_session", "ops_csrf")


# --- build_service: auth off -----------------------------------------------------------------------------------------------------------------------------

class Spy(dict):
    def __init__(self, *args):
        super().__init__(*args)
        self.reads = []

    def get(self, key, default=None):
        self.reads.append(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.reads.append(key)
        return super().__getitem__(key)

    def __contains__(self, key):
        self.reads.append(key)
        return super().__contains__(key)

    def __iter__(self):
        self.reads.append("<iter>")
        return super().__iter__()


@pytest.fixture
def log():
    """Every record sent to the logger of runtime.py. A handler of our own: the app's logger may stop propagation, which would hide it from caplog."""
    records = []

    class Collect(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler, logger = Collect(), logging.getLogger("ops_rag.auth")
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield records
    logger.removeHandler(handler)
    logger.setLevel(old_level)


@pytest.mark.parametrize("env", [{}, {"AUTH_ENABLED": ""}, {"AUTH_ENABLED": "false"}, {"AUTH_ENABLED": " OFF "}], ids=["unset", "empty", "false", "off"])
def test_auth_off_gives_none_reads_only_the_switch_and_creates_nothing(env, tmp_path, log):
    spy = Spy({**env, "AUTH_DB": str(tmp_path / "never.db"), "AUTH_SCRYPT_N": "bogus", "AUTH_USERS": "garbage", "AUTH_SIGNUP": "bogus"})
    assert build_service(spy, root=tmp_path) is None
    assert spy.reads == ["AUTH_ENABLED"]
    assert list(tmp_path.iterdir()) == []
    assert log == []


def test_auth_off_with_the_process_environment_gives_none(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("AUTH_DB", str(tmp_path / "never.db"))
    assert build_service() is None
    assert list(tmp_path.iterdir()) == []


# --- build_service: auth on ------------------------------------------------------------------------------------------------------------------------------

def on(tmp_path, **extra):
    return {"AUTH_ENABLED": "true", "AUTH_DB": str(tmp_path / "db" / "auth.db"), "AUTH_SCRYPT_N": str(CHEAP_N), "AUTH_SIGNUP": "open", **extra}


def test_auth_on_returns_a_working_service_with_its_database_under_tmp_path_only(tmp_path, log):
    service = build_service(on(tmp_path), root=tmp_path)
    assert isinstance(service, AuthService) and service.settings.enabled is True
    assert (tmp_path / "db" / "auth.db").is_file()
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == ["db", "db/auth.db"]
    assert service.sign_up("alice", PASSWORD).status == "created"
    result = service.log_in("alice", PASSWORD)
    assert result.ok and result.username == "alice" and len(result.session_token) == 43
    assert service.validate_session(result.session_token).username == "alice"
    assert log == []  # nothing is logged on success


def test_a_relative_database_path_is_resolved_against_the_root_argument(tmp_path):
    env = {"AUTH_ENABLED": "1", "AUTH_SCRYPT_N": str(CHEAP_N)}
    build_service(env, root=tmp_path)
    assert (tmp_path / "var" / "auth.db").is_file()  # the default folder, under the root given, never under the repository


def test_init_and_purge_each_run_once_in_that_order(tmp_path, monkeypatch):
    calls = []
    for name in ("init", "purge"):
        real = getattr(AuthService, name)
        monkeypatch.setattr(AuthService, name, lambda self, _real=real, _name=name: (calls.append(_name), _real(self))[1])
    build_service(on(tmp_path), root=tmp_path)
    assert calls == ["init", "purge"]


def test_the_provisioned_users_of_auth_users_can_log_in_and_sign_up_is_closed(tmp_path):
    stored = passwords.hash_password(PASSWORD, AuthSettings(scrypt_n=CHEAP_N))
    service = build_service(on(tmp_path, AUTH_USERS=f"  Bob:{stored} \n"), root=tmp_path)
    assert service.settings.provisioned is True
    assert service.log_in("bob", PASSWORD).ok
    assert service.sign_up("carol", PASSWORD).status == "closed"


def test_the_service_uses_the_real_clock(tmp_path):
    import time

    service = build_service(on(tmp_path), root=tmp_path)
    assert service._clock is time.time and service._store._clock is time.time


# --- build_service: every error path -----------------------------------------------------------------------------------------------------------------------

SECRET_ENTRY = "mallory:scrypt$16384$8$1$SECRETSALTSECRETSALT$SECRETHASHSECRETHASH"  # the right shape for a hash, but never a valid one
REAL_HASH_MARK = "SECRETHASH"


def blocked_db(tmp_path):
    (tmp_path / "file").write_text("x")
    return {"AUTH_DB": str(tmp_path / "file" / "sub" / "auth.db")}  # the parent is a file: the folder cannot be made


def newer_schema(tmp_path):
    env = on(tmp_path)
    build_service(env, root=tmp_path)
    with sqlite3.connect(env["AUTH_DB"]) as conn:
        conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    conn.close()
    return {}


ERROR_CASES = [
    ("bad_switch", lambda p: {"AUTH_ENABLED": "ture"}, AuthConfigError, "AUTH_ENABLED"),
    ("n_not_a_number", lambda p: {"AUTH_SCRYPT_N": "bogus"}, AuthConfigError, "AUTH_SCRYPT_N"),
    ("n_below_the_floor", lambda p: {"AUTH_SCRYPT_N": "1024"}, AuthConfigError, "AUTH_SCRYPT_N"),
    ("bad_signup_word", lambda p: {"AUTH_SIGNUP": "bogus"}, AuthConfigError, "AUTH_SIGNUP"),
    ("bad_auth_users", lambda p: {"AUTH_USERS": SECRET_ENTRY}, ProvisionedUsersError, "AUTH_USERS entry 1"),
    ("blocked_database_folder", blocked_db, OSError, None),
    ("newer_schema", newer_schema, StoreError, "schema version 99"),
]


@pytest.mark.parametrize("make, expected_type, word", [c[1:] for c in ERROR_CASES], ids=[c[0] for c in ERROR_CASES])
def test_an_error_is_logged_once_and_re_raised_unchanged_never_swallowed(make, expected_type, word, tmp_path, log):
    env = on(tmp_path, AUTH_USERS=SECRET_ENTRY)  # a secret-looking AUTH_USERS is in the environment of every case: it must never reach the message or the log
    env.update(make(tmp_path))
    with pytest.raises(expected_type) as caught:
        build_service(env, root=tmp_path)
    if expected_type is not OSError:  # the OSError subclass depends on the platform; every other error is re-raised as the very same type
        assert type(caught.value) is expected_type
    if word:
        assert word in str(caught.value)
    assert len(log) == 1 and log[0].levelno == logging.ERROR
    line = log[0].getMessage()
    assert line == f"Auth is enabled but cannot start: {type(caught.value).__name__}: {caught.value}" and "\n" not in line
    for secret in (SECRET_ENTRY, REAL_HASH_MARK, "SECRETSALT", "mallory", PASSWORD):
        assert secret not in line and secret not in str(caught.value)
    assert log[0].exc_info is None  # no traceback with locals or the environment


def test_an_error_with_a_real_hash_in_auth_users_never_shows_the_hash_or_the_password(tmp_path, log):
    stored = passwords.hash_password(PASSWORD, AuthSettings(scrypt_n=CHEAP_N))
    env = on(tmp_path, AUTH_USERS=f"dave:{stored}", AUTH_SCRYPT_N="bogus")
    with pytest.raises(AuthConfigError):
        build_service(env, root=tmp_path)
    text = log[0].getMessage()
    assert stored not in text and stored.split("$")[-1] not in text and "dave" not in text and PASSWORD not in text


def test_a_failed_start_creates_no_database(tmp_path, log):
    with pytest.raises(AuthConfigError):
        build_service(on(tmp_path, AUTH_SCRYPT_N="bogus"), root=tmp_path)
    assert not (tmp_path / "db").exists()


def test_a_database_that_is_not_an_auth_database_is_refused_and_left_alone(tmp_path, log):
    path = tmp_path / "db" / "auth.db"
    path.parent.mkdir()
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE other (x)")
    conn.close()
    before = path.read_bytes()
    with pytest.raises(StoreError):
        build_service(on(tmp_path), root=tmp_path)
    assert path.read_bytes() == before and len(log) == 1


# --- client_ip -------------------------------------------------------------------------------------------------------------------------------------------

PEER = ("10.0.0.9", 51234)


def xff(*values):
    return h(*[("X-Forwarded-For", v) for v in values])


@pytest.mark.parametrize("headers", [xff("1.2.3.4"), xff("9.9.9.9, 1.2.3.4"), xff(""), [], None, 5, "x-forwarded-for: 1.2.3.4"])
@pytest.mark.parametrize("trust", [False, None, 0, 1, "yes", "true", [True], 2])
def test_when_the_proxy_is_not_trusted_the_peer_is_used_whatever_the_headers_say(headers, trust):
    assert client_ip(PEER, headers, trust) == "10.0.0.9"


@pytest.mark.parametrize("peer, expected", [
    (("10.0.0.9", 1), "10.0.0.9"), (["10.0.0.9", 1], "10.0.0.9"), ("10.0.0.9", "10.0.0.9"), (("testclient", 50000), "testclient"),
    (None, ""), ("", ""), (("", 1), ""), ((), ""), ([], ""), (123, ""), ((5, 1), ""), ((None, 1), ""), (b"1.2.3.4", ""), (object(), ""),
    (("  10.0.0.9 ", 1), "10.0.0.9"),
    (("x" * MAX_PEER_LENGTH, 1), "x" * MAX_PEER_LENGTH), (("x" * (MAX_PEER_LENGTH + 1), 1), ""),
    (("::ffff:10.0.0.9", 1), "10.0.0.9"), (("2001:DB8:0:0:0:0:0:1", 1), "2001:db8::1"), (("::1", 1), "::1"),
])
def test_pinned_the_peer_written_canonically_when_it_is_an_ip_and_empty_when_odd(peer, expected):
    assert client_ip(peer, [], False) == expected
    assert client_ip(peer, [], True) == expected  # no header: the same


@pytest.mark.parametrize("value, expected", [
    ("1.2.3.4", "1.2.3.4"),
    ("9.9.9.9, 1.2.3.4", "1.2.3.4"),  # the rightmost, not the leftmost
    ("1.2.3.4, 9.9.9.9", "9.9.9.9"),
    ("  9.9.9.9 ,\t1.2.3.4  ", "1.2.3.4"),
    ("9.9.9.9,1.2.3.4", "1.2.3.4"),
    ("1.2.3.4,", "1.2.3.4"),  # a trailing comma leaves an empty entry that is skipped
    ("1.2.3.4, ,  ,", "1.2.3.4"),
    ("2001:DB8::0001", "2001:db8::1"), ("2001:db8:0:0:0:0:0:1", "2001:db8::1"), ("::1", "::1"), ("::", "::"),
    ("::ffff:1.2.3.4", "1.2.3.4"), ("::FFFF:0102:0304", "1.2.3.4"),
    ("0.0.0.0", "0.0.0.0"), ("255.255.255.255", "255.255.255.255"),
])
def test_when_trusted_the_rightmost_x_forwarded_for_entry_is_used_and_written_canonically(value, expected):
    assert client_ip(PEER, xff(value), True) == expected


@pytest.mark.parametrize("value", [
    "", "   ", ",", ",,,", "unknown", "null", "evil", "1.2.3", "1.2.3.4.5", "256.1.1.1", "1.2.3.4:8080", "[::1]", "[::1]:80", "fe80::1%eth0", "01.2.3.4", "1.2.3.4/32",
    "1.2.3.4 5.6.7.8", "1.2.3.4;", "\u0661\u0662.1.1.1", "1.2.3.4\x00", "0x7f.0.0.1", "1.2.3.4, evil", "9.9.9.9, 1.2.3.4:80",
])
def test_when_trusted_an_empty_or_invalid_rightmost_entry_gives_the_peer(value):
    assert client_ip(PEER, xff(value), True) == "10.0.0.9"


def test_pinned_a_valid_entry_left_of_an_invalid_one_is_not_used():
    assert client_ip(PEER, xff("9.9.9.9, evil"), True) == "10.0.0.9"
    assert client_ip(PEER, xff("9.9.9.9, 1.2.3.4:80"), True) == "10.0.0.9"


def test_pinned_several_x_forwarded_for_lines_count_as_one_list_in_order_the_last_entry_of_the_last_line_wins():
    assert client_ip(PEER, xff("9.9.9.9", "1.2.3.4"), True) == "1.2.3.4"
    assert client_ip(PEER, xff("1.2.3.4", "9.9.9.9, 8.8.8.8"), True) == "8.8.8.8"
    assert client_ip(PEER, xff("1.2.3.4", ""), True) == "1.2.3.4"  # an empty last line adds no entry
    assert client_ip(PEER, xff("1.2.3.4", "evil"), True) == "10.0.0.9"  # the last entry is invalid: the peer, not the line before
    assert client_ip(PEER, xff("evil", "1.2.3.4"), True) == "1.2.3.4"


def test_pinned_the_x_forwarded_for_length_cap_counts_all_lines_together_and_both_sides_of_it():
    exact = " " * (MAX_FORWARDED_LENGTH - len("1.2.3.4")) + "1.2.3.4"
    assert len(exact) == MAX_FORWARDED_LENGTH
    assert client_ip(PEER, xff(exact), True) == "1.2.3.4"
    assert client_ip(PEER, xff(" " + exact), True) == "10.0.0.9"
    first = " " * (MAX_FORWARDED_LENGTH - len("1.2.3.4") - 1)  # two lines: first + 7 characters + 1 for the joining comma = exactly the cap
    assert client_ip(PEER, xff(first, "1.2.3.4"), True) == "1.2.3.4"
    assert client_ip(PEER, xff(first + " ", "1.2.3.4"), True) == "10.0.0.9"  # one character over the cap


@pytest.mark.parametrize("name", ["X-Forwarded-For", "x-forwarded-for", "X-FORWARDED-FOR"])
def test_the_header_name_is_matched_in_any_case_in_text_bytes_and_mappings(name):
    assert client_ip(PEER, [(name.encode(), b"1.2.3.4")], True) == "1.2.3.4"
    assert client_ip(PEER, [(name, "1.2.3.4")], True) == "1.2.3.4"
    assert client_ip(PEER, {name: "1.2.3.4"}, True) == "1.2.3.4"
    assert client_ip(PEER, ((name.encode(), b"1.2.3.4"),), True) == "1.2.3.4"


@pytest.mark.parametrize("headers", [None, 5, "x", [5], [(1, 2)], [("x-forwarded-for", 5)], [("x-forwarded-for",)], [object()], {"x-forwarded-for": ["1.2.3.4"]},
                                     [(b"x-forwarded-for", None)], object()])
def test_weird_header_input_gives_the_peer_and_never_raises(headers):
    assert client_ip(PEER, headers, True) == "10.0.0.9"


def test_client_ip_never_raises_on_odd_arguments():
    for peer in (None, 1, object(), ("a",), (None,), "\udc80"):
        for headers in (None, [], [("x-forwarded-for", "\udc80")], object()):
            for trust in (True, False, object()):
                assert isinstance(client_ip(peer, headers, trust), str)


# --- is_https --------------------------------------------------------------------------------------------------------------------------------------------

def xfp(*values):
    return h(*[("X-Forwarded-Proto", v) for v in values])


@pytest.mark.parametrize("scheme, expected", [("https", True), ("HTTPS", True), (" https ", True), ("http", False), ("ws", False), ("wss", False), ("", False),
                                              (None, False), (5, False), (b"https", False)])
def test_the_scheme_decides_without_a_trusted_proxy(scheme, expected):
    assert is_https(scheme, [], False) is expected
    assert is_https(scheme, xfp("http"), True) is expected


@pytest.mark.parametrize("trust", [False, None, 0, 1, "yes", "true"])
def test_x_forwarded_proto_is_ignored_unless_the_proxy_is_trusted(trust):
    assert is_https("http", xfp("https"), trust) is False


@pytest.mark.parametrize("value, expected", [
    ("https", True), ("HTTPS", True), (" https ", True), ("http", False), ("http,https", True), ("http, https", True), ("https, http", False), ("https,", True),
    ("", False), (",", False), ("ftp", False), ("wss", False), ("on", False), ("1", False), ("true", False), ("httpss", False), ("xhttps", False),
])
def test_when_trusted_the_rightmost_x_forwarded_proto_entry_may_turn_https_on(value, expected):
    assert is_https("http", xfp(value), True) is expected


def test_pinned_the_header_never_turns_https_off_and_several_lines_are_read_as_one_list():
    for value in ("http", "ftp", "", "https, http"):
        assert is_https("https", xfp(value), True) is True
    assert is_https("http", xfp("http", "https"), True) is True
    assert is_https("http", xfp("https", "http"), True) is False


@pytest.mark.parametrize("headers", [None, 5, "x", [5], [(1, 2)], object()])
def test_is_https_with_weird_headers_is_the_scheme_only(headers):
    assert is_https("http", headers, True) is False and is_https("https", headers, True) is True


# --- cookies: building ------------------------------------------------------------------------------------------------------------------------------------

def attributes(header):
    first, *rest = header.split("; ")
    return first, rest


@pytest.fixture
def cfg():
    return AuthSettings(session_absolute_seconds=86400, session_idle_seconds=3600)


def test_the_session_cookie_has_the_approved_flags_on_plain_http(cfg):
    first, rest = attributes(build_set_cookie(SESSION_COOKIE, TOKEN, cfg, False))
    assert first == f"ops_session={TOKEN}"
    assert sorted(rest) == sorted(["Path=/", "Max-Age=86400", "HttpOnly", "SameSite=Lax"])


def test_the_csrf_cookie_is_not_http_only_but_has_the_other_flags(cfg):
    first, rest = attributes(build_set_cookie(CSRF_COOKIE, "tok_-9", cfg, False))
    assert first == "ops_csrf=tok_-9"
    assert sorted(rest) == sorted(["Path=/", "Max-Age=86400", "SameSite=Lax"])


@pytest.mark.parametrize("name", [SESSION_COOKIE, CSRF_COOKIE])
@pytest.mark.parametrize("mode, https, secure", [("auto", False, False), ("auto", True, True), ("always", False, True), ("always", True, True)])
def test_secure_follows_the_mode_and_the_scheme(name, mode, https, secure):
    cfg = AuthSettings(cookie_secure=mode)
    _, rest = attributes(build_set_cookie(name, TOKEN, cfg, https))
    assert ("Secure" in rest) is secure
    assert not [a for a in rest if a.lower().startswith(("domain", "expires", "partitioned"))]


@pytest.mark.parametrize("seconds", [1, 2, 86400, 604800, 10**9])
def test_max_age_is_exactly_the_absolute_session_limit(seconds):
    cfg = AuthSettings(session_idle_seconds=1, session_absolute_seconds=seconds)
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        assert f"Max-Age={seconds}" in attributes(build_set_cookie(name, TOKEN, cfg, False))[1]


def test_the_default_max_age_is_seven_days_not_the_idle_limit():
    assert "Max-Age=604800" in build_set_cookie(SESSION_COOKIE, TOKEN, AuthSettings(), False)


@pytest.mark.parametrize("name", [SESSION_COOKIE, CSRF_COOKIE])
@pytest.mark.parametrize("mode, https, secure", [("auto", False, False), ("always", False, True), ("auto", True, True)])
def test_clearing_keeps_every_attribute_but_the_value_and_max_age(name, mode, https, secure):
    cfg = AuthSettings(cookie_secure=mode, session_idle_seconds=100, session_absolute_seconds=5000)
    live = build_set_cookie(name, TOKEN, cfg, https)
    cleared = build_set_cookie(name, TOKEN, cfg, https, clear=True)  # the value argument is ignored
    first, rest = attributes(cleared)
    assert first == f"{name}=" and TOKEN not in cleared
    assert "Max-Age=0" in rest and "Max-Age=5000" not in cleared
    assert sorted(a for a in rest if not a.startswith("Max-Age")) == sorted(a for a in attributes(live)[1] if not a.startswith("Max-Age"))
    assert ("Secure" in rest) is secure and ("HttpOnly" in rest) is (name == SESSION_COOKIE)


def test_clearing_needs_no_valid_value():
    assert build_set_cookie(SESSION_COOKIE, None, AuthSettings(), False, clear=True).startswith("ops_session=;")


GOOD_VALUES = ["a", "A", "0", "_", "-", "A_-9", TOKEN, "x" * MAX_COOKIE_VALUE]
BAD_VALUES = ["", " ", "a b", "a;b", "a,b", "a=b", 'a"b', "a\\b", "a\r\nSet-Cookie: x=y", "a\n", "\na", "a\r", "a\x00", "\u00e9", "\u0661", "a/b", "a+b", "a.b", "a%20b",
              "x" * (MAX_COOKIE_VALUE + 1), None, 5, b"abc", ["a"], 1.5, True]


@pytest.mark.parametrize("value", GOOD_VALUES)
def test_a_value_of_token_characters_is_accepted_up_to_the_cap(value, cfg):
    assert build_set_cookie(SESSION_COOKIE, value, cfg, False).startswith(f"ops_session={value};")


@pytest.mark.parametrize("value", BAD_VALUES, ids=[repr(v)[:30] for v in BAD_VALUES])
def test_any_other_value_is_refused_so_a_header_can_never_be_injected(value, cfg):
    with pytest.raises(ValueError):
        build_set_cookie(SESSION_COOKIE, value, cfg, False)


@pytest.mark.parametrize("name", ["ops_other", "", "OPS_SESSION", "ops_session ", "ops_session=x", None, 5, b"ops_session"])
def test_an_unknown_cookie_name_is_refused(name, cfg):
    with pytest.raises(ValueError):
        build_set_cookie(name, TOKEN, cfg, False)


# --- cookies: reading ------------------------------------------------------------------------------------------------------------------------------------

def test_simple_cookies_are_read():
    assert parse_cookies(["ops_session=abc; ops_csrf=def"]) == {"ops_session": "abc", "ops_csrf": "def"}
    assert parse_cookies("ops_session=abc") == {"ops_session": "abc"}
    assert parse_cookies(b"ops_session=abc") == {"ops_session": "abc"}
    assert parse_cookies(iter(["a=1", "b=2"])) == {"a": "1", "b": "2"}
    assert parse_cookies(["a=1; b=2", "c=3"]) == {"a": "1", "b": "2", "c": "3"}


def test_a_name_that_appears_twice_is_absent_whatever_the_values():
    assert parse_cookies(["ops_session=a; ops_session=b; ops_csrf=c"]) == {"ops_csrf": "c"}
    assert parse_cookies(["ops_session=a; ops_session=a"]) == {}
    assert parse_cookies(["ops_session=a", "ops_session=b"]) == {}  # across header lines (HTTP/2 sends cookies as several lines)
    assert parse_cookies(["ops_session=a; x=1", "y=2; ops_session=a"]) == {"x": "1", "y": "2"}
    assert parse_cookies(["a=1; a=2; a=3"]) == {}  # a third one does not bring it back
    assert parse_cookies(["a=1", "a=2", "a=3", "a=4"]) == {}
    assert parse_cookies(["a=1; a=; b=2"]) == {"b": "2"}


def test_names_are_case_sensitive_so_a_different_case_is_a_different_cookie():
    assert parse_cookies(["ops_session=a; OPS_SESSION=b"]) == {"ops_session": "a", "OPS_SESSION": "b"}


@pytest.mark.parametrize("line, expected", [
    ("", {}), (";", {}), (";;;", {}), ("novalue", {}), ("=x", {}), (" =x", {}), ("a b=c", {}), ("a\tb=c", {}), ("a(b)=c", {}), ('a"b=c', {}), ("a,b=c", {}),
    ("a=b=c", {"a": "b=c"}), ("a=", {"a": ""}), ("a= ", {"a": ""}), ("  a  =  b  ", {"a": "b"}), ("a=1;b", {"a": "1"}), ("bad; a=1; ; =; b=2", {"a": "1", "b": "2"}),
    ('a="quoted"', {"a": '"quoted"'}),
    ("a=b\x00c", {}), ("a=b\x7fc", {}), ("a=b\x01", {}), ("a=1; b=2\r\n", {"a": "1", "b": "2"}), ("\u00e9=1", {}), ("a=\u00e9", {"a": "\u00e9"}),
])
def test_pinned_malformed_pairs_are_ignored_and_the_rest_is_kept(line, expected):
    assert parse_cookies([line]) == expected


def test_a_cookie_line_is_ignored_whole_when_longer_than_the_cap_and_kept_at_the_cap():
    prefix = "ops_session="
    exact = prefix + "a" * (MAX_COOKIE_HEADER - len(prefix))
    assert len(exact) == MAX_COOKIE_HEADER
    assert parse_cookies([exact]) == {"ops_session": "a" * (MAX_COOKIE_HEADER - len(prefix))}
    assert parse_cookies([exact + "a"]) == {}
    assert parse_cookies([exact + "a", "x=1"]) == {"x": "1"}  # only the long line is dropped


@pytest.mark.parametrize("junk", [None, 5, object(), [None], [5, object()], [["a=1"]], 1.5, True, [b"a=1", 5, None, "b=2"], {"a=1": 1}, [b"a=\xff\xfe"]])
def test_reading_cookies_never_raises_on_odd_input_and_returns_a_dict(junk):
    result = parse_cookies(junk)
    assert type(result) is dict


def test_bytes_lines_are_read_as_latin_1_like_asgi_headers():
    assert parse_cookies([b"a=1; b=2"]) == {"a": "1", "b": "2"}
    assert parse_cookies([b"a=\xe9"]) == {"a": "\u00e9"}


def test_header_values_gives_every_value_in_order_and_ignores_the_rest():
    headers = h(("Cookie", "a=1"), ("Host", "x"), ("cookie", "b=2"), ("COOKIE", "c=3"))
    assert header_values(headers, "cookie") == ["a=1", "b=2", "c=3"]
    assert header_values(headers, "COOKIE") == ["a=1", "b=2", "c=3"]
    assert header_values(headers, "missing") == []
    assert header_values({"Cookie": "a=1", b"cookie": b"b=2"}, "cookie") == ["a=1", "b=2"]
    assert header_values(None, "cookie") == [] and header_values("cookie: a", "cookie") == [] and header_values([5, (1,)], "cookie") == []


# --- origin_ok --------------------------------------------------------------------------------------------------------------------------------------------

def req(origin=None, host=None, fetch=None, extra=()):
    pairs = list(extra)
    if origin is not None:
        pairs.append(("Origin", origin))
    if host is not None:
        pairs.append(("Host", host))
    if fetch is not None:
        pairs.append(("Sec-Fetch-Site", fetch))
    return h(*pairs)


@pytest.mark.parametrize("origin, host, expected", [
    ("http://localhost:8000", "localhost:8000", True),
    ("HTTP://LocalHost:8000", "localhost:8000", True),
    ("http://localhost:8000", "LOCALHOST:8000", True),
    ("http://127.0.0.1:8000", "127.0.0.1:8000", True),
    ("http://[::1]:8000", "[::1]:8000", True),
    ("http://[::1]", "[::1]:80", True),
    ("https://app.example.com", "app.example.com", True),
    ("https://app.example.com", "app.example.com:443", True),
    ("https://app.example.com:443", "app.example.com", True),
    ("http://app.example.com", "app.example.com", True),
    ("http://app.example.com:80", "app.example.com", True),
    ("http://app.example.com", "app.example.com:80", True),
    ("https://app.example.com:80", "app.example.com:80", True),  # an explicit port equal on both sides
    ("  http://localhost:8000  ", "localhost:8000", True),
    # the other side of each boundary
    ("http://localhost:8001", "localhost:8000", False),
    ("http://localhost:8000", "localhost:8001", False),
    ("http://localhost", "localhost:8000", False),
    ("http://localhost:8000", "localhost", False),
    ("http://app.example.com:443", "app.example.com", False),  # http with 443 is not the default of http
    ("https://app.example.com", "app.example.com:80", False),
    ("https://app.example.com:80", "app.example.com", False),
    ("http://app.example.com", "app.example.com:443", False),
    ("http://other.example.com", "app.example.com", False),
    ("http://evil.localhost:8000", "localhost:8000", False),
    ("http://localhost:8000.evil.com", "localhost:8000", False),
    ("http://xlocalhost:8000", "localhost:8000", False),
    ("http://localhost:80000", "localhost:80000", False),
    ("http://localhost:0", "localhost:0", False),
    ("http://localhost:65535", "localhost:65535", True), ("http://localhost:65536", "localhost:65536", False),
    # not an origin
    ("null", "localhost:8000", False), ("NULL", "localhost:8000", False), ("", "localhost:8000", False), ("   ", "localhost:8000", False),
    ("localhost:8000", "localhost:8000", False), ("//localhost:8000", "localhost:8000", False), ("ftp://localhost:8000", "localhost:8000", False),
    ("file://localhost:8000", "localhost:8000", False), ("http://", "", False), ("http://localhost:8000/", "localhost:8000", False),
    ("http://localhost:8000/path", "localhost:8000", False), ("http://localhost:8000?x=1", "localhost:8000", False),
    ("http://localhost:8000#f", "localhost:8000", False), ("http://user@localhost:8000", "localhost:8000", False),
    ("http://localhost:8000@evil.com", "localhost:8000", False), ("http://evil.com@localhost:8000", "localhost:8000", False),
    ("http://local host:8000", "localhost:8000", False), ("http://localhost:8000 http://evil.com", "localhost:8000", False),
    ("http://localhost:8000, http://evil.com", "localhost:8000", False), ("http://\u0430pple.com", "xn--pple-43d.com", False),
    ("http://localhost:abc", "localhost:abc", False), ("http://localhost:", "localhost:", False),
    # a bad Host
    ("http://localhost:8000", "", False), ("http://localhost:8000", "localhost:8000/", False), ("http://localhost:8000", "localhost:8000 evil", False),
    ("http://localhost:8000", "evil.com@localhost:8000", False),
])
def test_an_origin_must_equal_the_host_header(origin, host, expected):
    assert origin_ok(req(origin, host)) is expected


def test_pinned_a_trailing_dot_is_not_normalised():
    assert origin_ok(req("http://example.com.", "example.com.")) is True
    assert origin_ok(req("http://example.com.", "example.com")) is False
    assert origin_ok(req("http://example.com", "example.com.")) is False


def test_pinned_the_scheme_itself_is_not_compared_with_the_real_one():
    assert origin_ok(req("https://localhost:8000", "localhost:8000")) is True  # behind a proxy the server cannot tell


def test_an_origin_without_a_host_header_is_refused():
    assert origin_ok(req("http://localhost:8000", None)) is False


def test_pinned_two_origin_lines_or_two_host_lines_are_refused_even_when_both_are_fine():
    assert origin_ok(h(("Origin", "http://a.test"), ("Origin", "http://a.test"), ("Host", "a.test"))) is False
    assert origin_ok(h(("Origin", "http://a.test"), ("Host", "a.test"), ("Host", "a.test"))) is False
    assert origin_ok(h(("Origin", "http://a.test"), ("Host", "a.test"))) is True


@pytest.mark.parametrize("fetch, expected", [
    ("same-origin", True), ("none", True), ("SAME-ORIGIN", True), (" None ", True), ("Same-Origin", True),
    ("same-site", False), ("cross-site", False), ("", False), ("   ", False), ("same-origin, none", False), ("garbage", False), ("same_origin", False),
    ("same-origins", False), ("xsame-origin", False),
])
def test_without_an_origin_sec_fetch_site_must_be_same_origin_or_none(fetch, expected):
    assert origin_ok(req(None, "localhost:8000", fetch)) is expected


def test_pinned_without_an_origin_two_sec_fetch_site_lines_are_refused():
    assert origin_ok(h(("Sec-Fetch-Site", "same-origin"), ("Sec-Fetch-Site", "same-origin"))) is False


def test_with_neither_header_the_request_passes_a_non_browser_client():
    assert origin_ok([]) is True
    assert origin_ok(req(None, "localhost:8000")) is True
    assert origin_ok(req(None, None, None, extra=[("Accept", "*/*"), ("Cookie", "a=1")])) is True
    assert origin_ok(()) is True and origin_ok({}) is True


def test_pinned_a_matching_origin_wins_and_sec_fetch_site_is_not_consulted():
    assert origin_ok(req("http://localhost:8000", "localhost:8000", "cross-site")) is True
    assert origin_ok(req("http://evil.test", "localhost:8000", "same-origin")) is False


@pytest.mark.parametrize("name", ["Origin", "origin", "ORIGIN"])
def test_header_names_are_matched_in_any_case(name):
    assert origin_ok([(name.encode(), b"http://a.test"), (b"Host", b"a.test")]) is True
    assert origin_ok([(name.encode(), b"http://b.test"), (b"Host", b"a.test")]) is False
    assert origin_ok({name: "http://a.test", "host": "a.test"}) is True


@pytest.mark.parametrize("headers", [None, 5, "origin: x", object(), 1.5, True])
def test_pinned_something_that_is_not_headers_at_all_is_refused(headers):
    assert origin_ok(headers) is False


@pytest.mark.parametrize("headers", [[5], [(1, 2)], [("origin", 5)], [("origin",)], [object()], [(b"origin", None)], [("origin", "\udc80"), ("host", "a")],
                                     [("origin", "http://a.test\x00"), ("host", "a.test")], {"origin": ["http://a.test"]}])
def test_origin_ok_never_raises_on_odd_header_items(headers):
    assert origin_ok(headers) in (True, False)
