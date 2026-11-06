"""app/auth/passwords.py: user name and password rules, scrypt hashing, the hashing gate, provisioned users.

Expected values come from the approved design (scratchpad auth_design_approved.md and the Phase 2 plan), not from running the code: the stored format
scrypt$N$r$p$salt$hash with a 16-byte salt and a 32-byte output, NFKC inside hash and verify, the input limits (512 UTF-8 bytes, 128 characters after NFKC)
before any scrypt, the parameter bounds (N a power of two from 2 to 2**17, r <= 16, p <= 4), the user name and password rules, the unknown-user dummy,
"at most one scrypt at a time" with a bounded queue and a timeout, and "AUTH_USERS errors name only the entry number".
Most tests use N=2**10. Scrypt is counted with a spy on passwords._derive (the only place that calls hashlib.scrypt). Nothing here sleeps and nothing
asserts an elapsed time: blocking cases run in a daemon worker thread that is joined with a limit of a few seconds (a healthy call takes milliseconds, so a
bug fails fast instead of hanging), "wait until the other thread is queued" is a poll on the gate's own state with a 5 s deadline of the real clock, and an
autouse fixture checks that no worker thread outlives its test.
"""
import base64
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app.auth import passwords
from app.auth.config import AuthConfigError, AuthSettings
from app.auth.passwords import (Busy, HashGate, ProvisionedUsersError, RuleError, check_password, configure_gate, get_gate, hash_password, needs_rehash,
                                normalise_username, parse_hash, parse_provisioned_users, set_gate, verify_password, verify_unknown)

ROOT = Path(__file__).resolve().parents[2]
GOOD = "correct horse battery"
SALT22, HASH43 = "A" * 22, "B" * 43


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def stored(n, r, p, salt=SALT22, digest=HASH43):
    """A well-formed stored value that was never produced by scrypt (for the parser, rehash and provisioned tests)."""
    return f"scrypt${n}${r}${p}${salt}${digest}"


# --- helpers: a spy on scrypt, threads that cannot hang the test run -------------------------------------------------------------------------------

class DeriveSpy:
    """Replaces passwords._derive: records every call, counts how many run at the same time, then runs the real one."""

    def __init__(self, real):
        self.real = real
        self.calls = []  # (n, r, p, salt)
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    @property
    def params(self):
        return [call[:3] for call in self.calls]

    def __call__(self, password_bytes, salt, n, r, p):
        with self._lock:
            self.calls.append((n, r, p, salt))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            return self.real(password_bytes, salt, n, r, p)
        finally:
            with self._lock:
                self.active -= 1


@pytest.fixture
def derive(monkeypatch):
    spy = DeriveSpy(passwords._derive)
    monkeypatch.setattr(passwords, "_derive", spy)
    return spy


@pytest.fixture(autouse=True)
def fresh_process_gate():
    set_gate(None)
    yield
    set_gate(None)


LIVE_CALLS = []  # every Call of the running test, so that none can outlive it


class Call:
    """Runs fn in a daemon thread. finish() fails the test (instead of hanging it) if the call has not returned within the limit, a few seconds: a healthy
    call returns in milliseconds, so a bug (a gate that is bypassed or never released) gives an assertion failure in seconds."""

    def __init__(self, fn):
        self.value = None
        self.error = None
        self._thread = threading.Thread(target=self._run, args=(fn,), daemon=True)
        LIVE_CALLS.append(self)
        self._thread.start()

    def _run(self, fn):
        try:
            self.value = fn()
        except BaseException as error:  # noqa: BLE001
            self.error = error

    def finish(self, limit=10):
        self._thread.join(limit)
        assert not self._thread.is_alive(), "the call is still blocked"
        return self


@pytest.fixture(autouse=True)
def no_thread_outlives_its_test():
    """After each test every worker thread it started must be finished (a test releases its gate in a finally block, so a healthy thread ends at once)."""
    LIVE_CALLS.clear()
    yield
    stuck = [call for call in LIVE_CALLS if (call._thread.join(5), call._thread.is_alive())[1]]
    LIVE_CALLS.clear()
    assert not stuck, f"{len(stuck)} worker thread(s) still running after the test"


def settle(predicate, what, limit=5):
    """Wait until predicate() is true, for at most `limit` seconds of the real clock. A deadline, not a count of polls: on Windows Event.wait(0.002) lasts about
    15 ms, so a count of 15000 polls (the old limit of 30) took about four minutes. No assertion depends on how long it took."""
    ticker = threading.Event()
    deadline = time.monotonic() + limit
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(f"gave up waiting for: {what}")
        ticker.wait(0.002)


class CountingGate(HashGate):
    def __init__(self, max_waiting=8, timeout_seconds=30):
        super().__init__(max_waiting, timeout_seconds)
        self.acquired = 0

    def acquire(self):
        self.acquired += 1
        super().acquire()


# --- the stored format -----------------------------------------------------------------------------------------------------------------------------

def test_the_stored_format_is_scrypt_n_r_p_salt_hash_with_the_designed_lengths(auth_settings):
    text = hash_password(GOOD, auth_settings)
    tag, n, r, p, salt, digest = text.split("$")
    assert (tag, n, r, p) == ("scrypt", "1024", "8", "1")
    assert len(salt) == 22 and len(digest) == 43 and "=" not in salt + digest
    assert len(base64.urlsafe_b64decode(salt + "==")) == 16 and len(base64.urlsafe_b64decode(digest + "=")) == 32
    assert all(c.isalnum() or c in "_-" for c in salt + digest)


def test_the_parameters_in_the_stored_value_are_the_configured_ones(settings_factory, derive):
    s = settings_factory(scrypt_n=2**10, scrypt_r=4, scrypt_p=2)
    text = hash_password(GOOD, s)
    assert text.startswith("scrypt$1024$4$2$")
    assert derive.params == [(1024, 4, 2)]
    assert verify_password(GOOD, text) is True
    assert derive.params[-1] == (1024, 4, 2)


def test_every_hash_has_its_own_salt(auth_settings):
    hashes = [hash_password(GOOD, auth_settings) for _ in range(12)]
    assert len({h.split("$")[4] for h in hashes}) == 12
    assert len(set(hashes)) == 12


def test_the_real_defaults_n_2_15_r_8_p_1_and_the_maxmem_formula(derive):
    """The one test with the real cost (about 0.1 s per scrypt). maxmem = 128*r*(N + p + 2) + 1 MiB."""
    s = AuthSettings()
    text = hash_password(GOOD, s)
    assert text.startswith("scrypt$32768$8$1$")
    assert derive.params == [(2**15, 8, 1)]
    assert passwords._maxmem(2**15, 8, 1) == 128 * 8 * (2**15 + 1 + 2) + 2**20 == 34_606_080
    assert verify_password(GOOD, text) is True and verify_password(GOOD + "x", text) is False
    assert needs_rehash(text, s) is False


def test_pinned_the_largest_allowed_cost_asks_for_about_257_mib(derive):
    """Pinned, not endorsed. The bounds allow a stored hash with N=2**17, r=16, p=4, and then scrypt needs 128*16*(2**17+4+2) + 1 MiB = 269,496,320
    bytes (257 MiB). A tampered database row can therefore ask for that much memory. The gate lets only one such scrypt run at a time, and the
    provisioned or stored values are the operator's own. This test only checks the numbers; it does not run such a scrypt."""
    assert passwords._maxmem(2**17, 16, 4) == 128 * 16 * (2**17 + 4 + 2) + 2**20 == 269_496_320
    assert parse_hash(stored(2**17, 16, 4)) is not None


def test_a_cost_that_needs_more_than_the_default_32_mib_is_not_refused_by_hashlib(auth_settings):
    """N=2**16, r=8 needs 64 MiB: only works if maxmem was raised above hashlib's default of 32 MiB."""
    text = stored(2**16, 8, 1)
    assert parse_hash(text) is not None
    assert passwords._maxmem(2**16, 8, 1) > 32 * 2**20


# --- round trip, wrong password, case, NFKC ---------------------------------------------------------------------------------------------------------------

def test_a_password_verifies_against_its_own_hash(auth_settings):
    assert verify_password(GOOD, hash_password(GOOD, auth_settings)) is True


@pytest.mark.parametrize("wrong", ["correct horse batterz", "Correct horse battery", "correct horse battery ", " correct horse battery", "", "x"])
def test_a_different_password_does_not_verify(auth_settings, wrong):
    assert verify_password(wrong, hash_password(GOOD, auth_settings)) is False


NFKC_PAIRS = [
    ("composed and decomposed e-acute", "Caf\u00e9-passw0rd-ok", "Cafe\u0301-passw0rd-ok"),
    ("fullwidth letters and ASCII", "\uff50\uff41\uff53\uff53-passw0rd-ok", "pass-passw0rd-ok"),
    ("a ligature and its letters", "\ufb01ne-passw0rd-ok", "fine-passw0rd-ok"),
    ("circled digits and digits", "passw0rd-\u2460\u2461\u2462", "passw0rd-123"),
]


@pytest.mark.parametrize("label, a, b", NFKC_PAIRS, ids=[p[0] for p in NFKC_PAIRS])
def test_equivalent_unicode_forms_verify_each_other_in_both_directions(auth_settings, label, a, b):
    """NFKC is applied inside hash_password and inside verify_password: neither a hash made from one form nor a check with the other can skip it."""
    assert verify_password(b, hash_password(a, auth_settings)) is True
    assert verify_password(a, hash_password(b, auth_settings)) is True


def test_nfkc_is_applied_inside_hash_password(auth_settings):
    """The stored hash of a decomposed password equals what a normalised one would give: verify it with a hash made by hand from the NFKC bytes."""
    composed, decomposed = "Caf\u00e9-passw0rd-ok", "Cafe\u0301-passw0rd-ok"
    text = hash_password(decomposed, auth_settings)
    _, n, r, p, salt, digest = text.split("$")
    import hashlib

    by_hand = hashlib.scrypt(composed.encode("utf-8"), salt=base64.urlsafe_b64decode(salt + "=="), n=int(n), r=int(r), p=int(p), maxmem=2**26, dklen=32)
    assert b64(by_hand) == digest


def test_the_length_limit_counts_characters_after_nfkc(auth_settings):
    assert verify_password("\ufb03" * 42, hash_password("\ufb03" * 42, auth_settings)) is True  # 42 ligatures -> 126 characters
    with pytest.raises(RuleError) as error:
        hash_password("\ufb03" * 43, auth_settings)  # -> 129 characters
    assert error.value.code == "password_too_long"


def test_pinned_a_password_with_a_nul_character_can_be_hashed_and_verified(auth_settings):
    """Unwritten behaviour, not a deviation: only check_password (sign-up) refuses NUL; hash and verify accept it."""
    assert verify_password("abc\x00defghijkl", hash_password("abc\x00defghijkl", auth_settings)) is True


# --- input limits come first -----------------------------------------------------------------------------------------------------------------------------

OVERSIZE = [
    ("513 ASCII characters", "a" * 513),
    ("129 ASCII characters", "a" * 129),
    ("200 two-byte characters (400 bytes, 200 characters)", "\u00e9" * 200),
    ("171 three-byte characters (513 bytes)", "\u20ac" * 171),
    ("129 four-byte characters (516 bytes)", "\U0001f600" * 129),
    ("50 ligatures that NFKC turns into 150 characters", "\ufb03" * 50),
    ("a lone surrogate", "abc\ud800defghijklmnop"),
    ("10 MB of text", "x" * (10 * 2**20)),
    ("not text (None)", None),
    ("not text (bytes)", b"correct horse battery"),
    ("not text (int)", 5),
]
OVERSIZE_IDS = [row[0] for row in OVERSIZE]


@pytest.mark.parametrize("label, text", OVERSIZE, ids=OVERSIZE_IDS)
def test_an_over_limit_password_is_refused_before_any_scrypt(auth_settings, derive, label, text):
    ok_hash = hash_password(GOOD, auth_settings)
    derive.calls.clear()
    assert verify_password(text, ok_hash) is False
    assert verify_unknown(text, auth_settings) is False
    with pytest.raises(RuleError) as error:
        hash_password(text, auth_settings)
    assert error.value.code in ("password_too_long", "password_invalid")
    assert derive.calls == [], "scrypt ran for an over-limit password"


def test_exactly_128_characters_and_exactly_512_bytes_are_accepted(auth_settings, derive):
    for text in ("x" * 128, "\U0001f600" * 128):  # 128 characters; 128 four-byte characters = 512 bytes
        assert verify_password(text, hash_password(text, auth_settings)) is True
    assert len(derive.calls) == 4


# --- user names ---------------------------------------------------------------------------------------------------------------------------------------

VALID_NAMES = [(" Alice.B_c-1 ", "alice.b_c-1"), ("abc", "abc"), ("a" * 32, "a" * 32), ("\uff21\uff22\uff23", "abc"), ("x.y-z", "x.y-z"), ("0day", "0day"), ("ab-", "ab-")]
INVALID_NAMES = ["", "ab", "a" * 33, "-abc", ".abc", "_abc", "ab cd", "ab@cd", "\u00e9tienne", "ab\ncd", "abc\x00", "a" * 300, "\uff21\uff22", 5, None, b"abc"]


@pytest.mark.parametrize("raw, expected", VALID_NAMES)
def test_a_valid_user_name_is_normalised(raw, expected):
    assert normalise_username(raw) == expected


@pytest.mark.parametrize("raw", INVALID_NAMES, ids=[repr(x)[:24] for x in INVALID_NAMES])
def test_an_invalid_user_name_is_refused_with_the_stable_code_and_a_fixed_message(raw):
    with pytest.raises(RuleError) as error:
        normalise_username(raw)
    assert error.value.code == "username_invalid"
    assert str(error.value) == passwords.MESSAGES["username_invalid"]
    if isinstance(raw, str) and len(raw) > 3:
        assert raw not in str(error.value)


# --- passwords: the sign-up rules -------------------------------------------------------------------------------------------------------------------------

PASSWORD_RULES = [  # (password, user name, code)
    ("elevenchars", "bob", "password_too_short"), ("", "bob", "password_too_short"), ("abcdefghijk", "bob", "password_too_short"),
    ("aaaaaaaaaaaa", "bob", "password_repeated"), ("\u00e9" * 20, "bob", "password_repeated"),
    ("abc\x00defghijkl", "bob", "password_nul"),
    ("AliceSmith.01", "alicesmith.01", "password_equals_username"), ("alice-smith-01", "Alice-Smith-01", "password_equals_username"),
    ("\uff41\uff4c\uff49\uff43\uff45-smith-01", "alice-smith-01", "password_equals_username"),
    ("a" * 129, "bob", "password_too_long"), ("\U0001f600" * 129, "bob", "password_too_long"), ("\ufb03" * 43, "bob", "password_too_long"),
    ("abc\ud800defghijklmnop", "bob", "password_invalid"), (None, "bob", "password_invalid"), (b"abcdefghijklm", "bob", "password_invalid"),
]


@pytest.mark.parametrize("text, username, code", PASSWORD_RULES, ids=[f"{repr(r[0])[:20]}->{r[2]}" for r in PASSWORD_RULES])
def test_a_password_that_breaks_a_rule_gets_its_stable_code_and_a_fixed_message(text, username, code):
    with pytest.raises(RuleError) as error:
        check_password(text, username)
    assert error.value.code == code
    assert str(error.value) == passwords.MESSAGES[code]
    if isinstance(text, str) and len(text) > 3:
        assert text not in str(error.value)
    assert username.lower() not in str(error.value).lower() or len(username) <= 3


@pytest.mark.parametrize("text", ["abcdefghijkl", "x" * 127 + "y", "Passw0rd with spaces!", "\U0001f600\U0001f601" * 6, "p" * 11 + "q"])
def test_a_password_inside_the_rules_is_accepted_and_returned_normalised(text):
    assert check_password(text, "bob") == text


def test_check_password_returns_the_nfkc_form():
    assert check_password("Cafe\u0301-passw0rd-ok", "bob") == "Caf\u00e9-passw0rd-ok"


def test_exactly_12_and_exactly_128_characters_pass_the_rules():
    assert len(check_password("abcdefghijkl", "bob")) == 12
    assert len(check_password("ab" * 64, "bob")) == 128


def test_a_password_that_differs_from_the_user_name_only_by_a_suffix_is_allowed():
    assert check_password("alicesmith.01x", "alicesmith.01") == "alicesmith.01x"


def test_the_set_of_error_codes_is_the_designed_one():
    assert set(passwords.MESSAGES) == {"username_invalid", "password_invalid", "password_too_long", "password_too_short", "password_nul", "password_repeated",
                                       "password_equals_username"}
    assert all(isinstance(m, str) and m and "{" not in m for m in passwords.MESSAGES.values())


# --- constant-time comparison -----------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("right", [True, False])
def test_verify_password_compares_with_hmac_compare_digest(auth_settings, monkeypatch, right):
    """A spy on hmac.compare_digest that records its arguments and calls the real one. If the code compares with == instead, the spy is never called."""
    text = hash_password(GOOD, auth_settings)
    seen = []
    real = passwords.hmac.compare_digest

    def spy(a, b):
        seen.append((type(a), type(b), len(a), len(b)))
        return real(a, b)

    monkeypatch.setattr(passwords.hmac, "compare_digest", spy)
    assert verify_password(GOOD if right else "wrong password!", text) is right
    assert seen == [(bytes, bytes, 32, 32)], "verify_password did not compare the 32-byte digests with hmac.compare_digest"


# --- parse_hash and malformed stored values ----------------------------------------------------------------------------------------------------------------

MALFORMED = {
    "empty": "", "None": None, "int": 123, "bytes": b"scrypt$1024$8$1$" + SALT22.encode() + b"$" + HASH43.encode(), "five fields": f"scrypt$1024$8$1${SALT22}",
    "seven fields": stored(1024, 8, 1) + "$x", "wrong tag": f"bcrypt$1024$8$1${SALT22}${HASH43}", "upper-case tag": f"SCRYPT$1024$8$1${SALT22}${HASH43}",
    "N=0": stored(0, 8, 1), "N=1": stored(1, 8, 1), "N=3": stored(3, 8, 1), "N=1000": stored(1000, 8, 1), "N=2^18": stored(2**18, 8, 1), "N=2^30": stored(2**30, 8, 1),
    "r=0": stored(1024, 0, 1), "r=17": stored(1024, 17, 1), "r=999": stored(1024, 999, 1), "p=0": stored(1024, 8, 0), "p=5": stored(1024, 8, 5),
    "negative N": f"scrypt$-1024$8$1${SALT22}${HASH43}", "underscore in N": f"scrypt$1_024$8$1${SALT22}${HASH43}", "leading zero in N": f"scrypt$01024$8$1${SALT22}${HASH43}",
    "leading zero in r": f"scrypt$1024$08$1${SALT22}${HASH43}", "plus sign": f"scrypt$+1024$8$1${SALT22}${HASH43}", "space in N": f"scrypt$ 1024$8$1${SALT22}${HASH43}",
    "letters in N": f"scrypt$1o24$8$1${SALT22}${HASH43}", "empty N": f"scrypt$$8$1${SALT22}${HASH43}", "11-digit N": f"scrypt$10240000000$8$1${SALT22}${HASH43}",
    "bad base64 character in the salt": stored(1024, 8, 1, salt="!" * 22), "bad base64 character in the hash": stored(1024, 8, 1, digest="+" * 43),
    "salt too short": stored(1024, 8, 1, salt="AAAA"), "salt too long": stored(1024, 8, 1, salt="A" * 23), "huge salt": stored(1024, 8, 1, salt="A" * 5000),
    "hash too short": stored(1024, 8, 1, digest="B" * 42), "hash too long": stored(1024, 8, 1, digest="B" * 44), "huge hash": stored(1024, 8, 1, digest="B" * 5000),
    "padding character in the salt": stored(1024, 8, 1, salt="A" * 21 + "="), "padding character in the hash": stored(1024, 8, 1, digest="B" * 42 + "="),
    "over-long value": stored(1024, 8, 1) + "x" * 300, "newline at the end": stored(1024, 8, 1) + "\n", "leading space": " " + stored(1024, 8, 1),
}


@pytest.mark.parametrize("label", list(MALFORMED))
def test_a_malformed_or_out_of_bounds_stored_value_is_none_and_never_raises(label):
    assert parse_hash(MALFORMED[label]) is None


@pytest.mark.parametrize("label", list(MALFORMED))
def test_verify_password_on_a_malformed_stored_value_is_false_without_scrypt_and_without_raising(label, derive):
    assert verify_password(GOOD, MALFORMED[label]) is False
    assert derive.calls == []


BOUNDS = [(2, 1, 1), (4, 8, 1), (1024, 8, 1), (2**15, 8, 1), (2**16, 16, 2), (2**17, 16, 4), (2**17, 1, 1), (1024, 16, 4)]


@pytest.mark.parametrize("n, r, p", BOUNDS)
def test_a_stored_value_at_or_inside_the_bounds_parses(n, r, p):
    parsed = parse_hash(stored(n, r, p, salt=b64(bytes(range(16))), digest=b64(bytes(range(32)))))
    assert parsed == (n, r, p, bytes(range(16)), bytes(range(32)))


def test_a_hash_made_by_hash_password_parses_back(auth_settings):
    n, r, p, salt, digest = parse_hash(hash_password(GOOD, auth_settings))
    assert (n, r, p, len(salt), len(digest)) == (1024, 8, 1, 16, 32)


def test_a_hash_with_weaker_parameters_than_the_configured_ones_still_verifies(settings_factory, derive):
    weak = hash_password(GOOD, settings_factory(scrypt_n=2**10, scrypt_r=4, scrypt_p=1))
    assert verify_password(GOOD, weak) is True
    assert needs_rehash(weak, AuthSettings()) is True


# --- needs_rehash ----------------------------------------------------------------------------------------------------------------------------------------------

REHASH = [  # (stored (n, r, p), configured (n, r, p), expected, label)
    ((2**15, 8, 1), (2**15, 8, 1), False, "equal"),
    ((2**16, 8, 1), (2**15, 8, 1), False, "stronger N"), ((2**15, 16, 1), (2**15, 8, 1), False, "stronger r"), ((2**15, 8, 2), (2**15, 8, 1), False, "stronger p"),
    ((2**17, 16, 4), (2**15, 8, 1), False, "stronger in all three"),
    ((2**14, 8, 1), (2**15, 8, 1), True, "weaker N"), ((2**15, 4, 1), (2**15, 8, 1), True, "weaker r"), ((2**12, 8, 1), (2**12, 8, 2), True, "weaker p"),
    ((2**10, 8, 1), (2**15, 8, 1), True, "much weaker N"), ((2**16, 4, 1), (2**15, 8, 1), True, "mixed: N higher, r lower"),
    ((2**14, 16, 1), (2**15, 8, 1), True, "mixed: N lower, r higher"), ((2**15, 8, 2), (2**15, 8, 2), False, "equal with p=2"), ((2**10, 8, 1), (2**10, 8, 1), False, "equal, cheap"),
]


@pytest.mark.parametrize("have, want, expected, label", REHASH, ids=[row[3] for row in REHASH])
def test_needs_rehash_table(have, want, expected, label):
    configured = AuthSettings(scrypt_n=want[0], scrypt_r=want[1], scrypt_p=want[2])
    assert needs_rehash(stored(*have), configured) is expected


@pytest.mark.parametrize("label", ["empty", "None", "N=3", "five fields", "bad base64 character in the salt"])
def test_needs_rehash_is_true_for_a_malformed_value(label):
    assert needs_rehash(MALFORMED[label], AuthSettings()) is True


# --- unknown users: the dummy --------------------------------------------------------------------------------------------------------------------------------------

def test_verify_unknown_runs_one_scrypt_with_the_configured_parameters_and_is_always_false(settings_factory, derive):
    configured = settings_factory(scrypt_n=2**11, scrypt_r=4, scrypt_p=2)
    assert verify_unknown(GOOD, configured) is False
    assert derive.params == [(2048, 4, 2)]


def test_pinned_verify_uses_the_stored_parameters_but_verify_unknown_uses_the_configured_ones(settings_factory, derive):
    """Unwritten behaviour, not a deviation: an existing user whose hash is weaker than the configuration costs less work than an unknown name until the
    next login rehashes it. The unknown-user work equals the work of a user whose hash is at the configured cost."""
    stored_cheap = hash_password(GOOD, settings_factory(scrypt_n=2**10, scrypt_r=8, scrypt_p=1))
    configured = settings_factory(scrypt_n=2**11, scrypt_r=4, scrypt_p=2)
    derive.calls.clear()
    verify_password(GOOD, stored_cheap)
    verify_unknown(GOOD, configured)
    assert derive.params == [(1024, 8, 1), (2048, 4, 2)]


def test_the_work_for_a_known_user_at_the_configured_cost_equals_the_work_for_an_unknown_one(auth_settings, derive):
    text = hash_password(GOOD, auth_settings)
    derive.calls.clear()
    verify_password("some other password!", text)
    verify_unknown("some other password!", auth_settings)
    assert len(derive.calls) == 2 and derive.params[0] == derive.params[1]


def test_the_dummy_is_created_on_first_use_and_then_reused(auth_settings, derive, monkeypatch):
    monkeypatch.setattr(passwords, "_dummy", None)
    assert passwords._dummy is None
    verify_unknown(GOOD, auth_settings)
    assert passwords._dummy is not None
    verify_unknown("another password here", auth_settings)
    assert derive.calls[0][3] == derive.calls[1][3], "the dummy salt changed between calls"
    assert passwords._dummy_values() is passwords._dummy_values()
    assert len(passwords._dummy[0]) == 16 and len(passwords._dummy[1]) == 32


def test_the_dummy_is_not_what_a_real_hash_of_the_same_password_gives(auth_settings):
    """Whatever is typed, verify_unknown is False; and a real stored hash is not accepted just because the dummy was used."""
    assert verify_unknown(GOOD, auth_settings) is False
    assert verify_password(GOOD, hash_password(GOOD, auth_settings)) is True


# --- the hashing gate ----------------------------------------------------------------------------------------------------------------------------------------------

def test_at_most_one_scrypt_runs_at_a_time_under_many_threads(settings_factory, derive):
    s = settings_factory(scrypt_n=2**12)
    text = hash_password(GOOD, s)
    derive.calls.clear()
    gate = HashGate(max_waiting=20, timeout_seconds=60)
    start = threading.Barrier(8)
    results = []

    def work():
        start.wait(10)
        for _ in range(4):
            results.append(verify_password(GOOD, text, gate=gate))

    threads = [Call(work) for _ in range(8)]
    for t in threads:
        t.finish(15)
        assert t.error is None, t.error
    assert results == [True] * 32 and len(derive.calls) == 32
    assert derive.max_active == 1, f"{derive.max_active} scrypts ran at the same time"
    assert not gate.running and gate.waiting == 0


def test_a_full_queue_gives_busy_at_once_and_no_hashing(auth_settings, derive):
    gate = HashGate(max_waiting=1, timeout_seconds=60)
    text = hash_password(GOOD, auth_settings)
    derive.calls.clear()
    gate.acquire()  # the slot is held
    try:
        queued = Call(lambda: verify_password(GOOD, text, gate=gate))
        settle(lambda: gate.waiting == 1, "one caller waiting for the slot")
        refused = Call(lambda: verify_password(GOOD, text, gate=gate)).finish()
        assert isinstance(refused.error, Busy) and refused.error.reason == "queue_full"
        assert derive.calls == [], "scrypt ran for a refused caller"
        assert gate.running and gate.waiting == 1, "the refused caller changed the gate"
    finally:
        gate.release()
    queued.finish()
    assert queued.value is True and len(derive.calls) == 1
    assert not gate.running and gate.waiting == 0


def test_a_wait_that_runs_out_gives_busy_timeout_no_hashing_and_a_clean_state(auth_settings, derive):
    gate = HashGate(max_waiting=2, timeout_seconds=0.05)
    text = hash_password(GOOD, auth_settings)
    derive.calls.clear()
    gate.acquire()
    try:
        waited = Call(lambda: verify_password(GOOD, text, gate=gate)).finish()
        assert isinstance(waited.error, Busy) and waited.error.reason == "timeout"
        assert derive.calls == []
        assert gate.waiting == 0 and gate.running
    finally:
        gate.release()
    assert verify_password(GOOD, text, gate=gate) is True
    assert not gate.running and gate.waiting == 0


def test_with_max_waiting_zero_a_busy_gate_refuses_at_once(auth_settings, derive):
    gate = HashGate(max_waiting=0, timeout_seconds=60)
    gate.acquire()
    try:
        refused = Call(lambda: verify_unknown(GOOD, auth_settings, gate=gate)).finish()
        assert isinstance(refused.error, Busy) and refused.error.reason == "queue_full"
        assert derive.calls == []
    finally:
        gate.release()


def test_every_waiting_caller_is_served_in_the_end(auth_settings, derive):
    gate = HashGate(max_waiting=3, timeout_seconds=60)
    text = hash_password(GOOD, auth_settings)
    derive.calls.clear()
    gate.acquire()
    try:
        calls = [Call(lambda: verify_password(GOOD, text, gate=gate)) for _ in range(3)]
        settle(lambda: gate.waiting == 3, "three callers waiting")
    finally:
        gate.release()
    for c in calls:
        c.finish()
        assert c.value is True
    assert len(derive.calls) == 3 and derive.max_active == 1 and not gate.running and gate.waiting == 0


def test_the_slot_is_released_when_the_body_raises():
    gate = HashGate(max_waiting=0, timeout_seconds=60)
    with pytest.raises(ValueError):
        with gate.slot():
            assert gate.running
            raise ValueError("boom")
    assert not gate.running and gate.waiting == 0
    with gate.slot():
        pass


def test_the_slot_is_released_when_scrypt_itself_raises(auth_settings, monkeypatch):
    gate = HashGate(max_waiting=0, timeout_seconds=60)

    def explode(*args):
        raise MemoryError("simulated")

    monkeypatch.setattr(passwords, "_derive", explode)
    with pytest.raises(MemoryError):
        hash_password(GOOD, auth_settings, gate=gate)
    assert not gate.running and gate.waiting == 0


def test_an_arrival_while_a_waiter_is_queued_counts_against_the_queue(auth_settings):
    """White-box: the state right after a release, before the notified waiter has run (slot free, one waiter registered). With max_waiting=1 a new arrival
    must be refused, not slip into the free slot ahead of the waiter."""
    gate = HashGate(max_waiting=1, timeout_seconds=60)
    gate._running, gate._waiting = False, 1
    with pytest.raises(Busy) as error:
        gate.acquire()
    assert error.value.reason == "queue_full"
    assert gate._running is False and gate._waiting == 1


def test_busy_carries_a_fixed_message_and_a_reason():
    for reason in ("queue_full", "timeout"):
        e = Busy(reason)
        assert e.reason == reason and str(e) == "The server is busy; try again shortly."
    assert issubclass(Busy, RuntimeError) and not issubclass(Busy, RuleError)


@pytest.mark.parametrize("operation", ["hash", "verify", "unknown"])
def test_every_scrypt_path_goes_through_the_gate_exactly_once(auth_settings, operation):
    gate = CountingGate()
    text = hash_password(GOOD, auth_settings)
    gate.acquired = 0

    def run():
        if operation == "hash":
            hash_password(GOOD, auth_settings, gate=gate)
        elif operation == "verify":
            verify_password(GOOD, text, gate=gate)
        else:
            verify_unknown(GOOD, auth_settings, gate=gate)

    done = Call(run).finish()  # in a worker thread with a join limit: a gate that is never released fails here in seconds
    assert done.error is None, done.error
    assert gate.acquired == 1


def test_no_gate_is_taken_when_no_scrypt_runs(auth_settings):
    gate = CountingGate()
    verify_password("x" * 200, stored(1024, 8, 1), gate=gate)
    verify_password(GOOD, "garbage", gate=gate)
    verify_unknown("x" * 200, auth_settings, gate=gate)
    with pytest.raises(RuleError):
        hash_password("x" * 200, auth_settings, gate=gate)
    assert gate.acquired == 0


def test_without_a_gate_argument_the_process_gate_is_used(auth_settings):
    gate = CountingGate()
    set_gate(gate)
    hash_password(GOOD, auth_settings)
    verify_unknown(GOOD, auth_settings)
    assert gate.acquired == 2


def test_the_process_gate_is_created_lazily_shared_and_has_the_default_queue():
    import app.auth.passwords as module

    assert module._gate is None
    first = get_gate()
    assert first is get_gate() is module._gate
    assert (first.max_waiting, first.timeout_seconds) == (8, 10)


def test_configure_gate_replaces_the_process_gate_from_the_settings():
    old = get_gate()
    new = configure_gate(AuthSettings(hash_queue_max=2, hash_timeout_seconds=7))
    assert new is get_gate() and new is not old
    assert (new.max_waiting, new.timeout_seconds) == (2, 7)


def test_set_gate_none_forgets_the_gate_and_the_next_use_creates_a_fresh_one():
    first = get_gate()
    assert set_gate(None) is None
    second = get_gate()
    assert second is not first and (second.max_waiting, second.timeout_seconds) == (8, 10)
    custom = HashGate(1, 1)
    assert set_gate(custom) is custom and get_gate() is custom


# --- provisioned users ----------------------------------------------------------------------------------------------------------------------------------------------

HA, HB = stored(2**15, 8, 1, salt="A" * 22, digest="B" * 43), stored(2**14, 8, 1, salt="C" * 22, digest="D" * 43)


@pytest.mark.parametrize("separator", [" ", ",", ", ", "\n", "\t", "\r\n", " \n ,\t"])
def test_provisioned_entries_may_be_separated_by_whitespace_or_commas(separator):
    assert parse_provisioned_users(f"alice:{HA}{separator}bob.smith:{HB}") == {"alice": HA, "bob.smith": HB}


def test_provisioned_names_are_normalised_and_leading_and_trailing_separators_ignored():
    text = f" , ALICE:{HA},\n\uff22\uff2f\uff22-x:{HB} \n,"
    assert parse_provisioned_users(text) == {"alice": HA, "bob-x": HB}


@pytest.mark.parametrize("text", ["", "   ", "\n\t \r\n", ",,,", " , , "])
def test_empty_or_blank_provisioned_text_gives_no_users(text):
    assert parse_provisioned_users(text) == {}


def test_weaker_provisioned_parameters_are_accepted_and_verify(auth_settings):
    weak = hash_password(GOOD, auth_settings)  # N=2**10, weaker than the defaults
    users = parse_provisioned_users(f"alice:{weak}")
    assert verify_password(GOOD, users["alice"]) is True


SECRET = "SECRETNAME"
MALFORMED_ENTRIES = [  # (text, entry number the error must name, word the message must contain)
    (f"{SECRET}{HA}", 1, "not valid"), (f"{SECRET}:scrypt$1024$8$1$short$short", 1, "not valid"), (f"{SECRET}:{HA}x", 1, "not valid"),
    (f"{SECRET}:", 1, "not valid"), (f"{SECRET}:garbage", 1, "not valid"), (f":{HA}", 1, "user name"), (f"ab:{HA}", 1, "user name"),
    (f"a b:{HA}", 1, "not valid"),  # split into two entries by the space: "a" has no colon
    (f"{SECRET}-{'x' * 40}:{HA}", 1, "user name"), (f"ok-one:{HA} {SECRET}:{HA}x", 2, "not valid"), (f"ok-one:{HA} ok-two:{HB} bad name:{HB}", 3, "not valid"),
    (f"ok-one:{HA}, {SECRET}!:{HB}", 2, "user name"), (f"alice:{HA} bob:{MALFORMED['N=3']}", 2, "not valid"),
]


@pytest.mark.parametrize("text, number, word", MALFORMED_ENTRIES, ids=[f"{i}:{m[1]}" for i, m in enumerate(MALFORMED_ENTRIES)])
def test_a_malformed_provisioned_entry_raises_naming_only_the_entry_number(text, number, word):
    with pytest.raises(ProvisionedUsersError) as error:
        parse_provisioned_users(text)
    message = str(error.value)
    assert error.value.entry == number and message.startswith(f"AUTH_USERS entry {number} "), message
    assert word in message
    for forbidden in (SECRET, "scrypt$", "AAAA", "BBBB", "alice", "bob", "ok-one", "ok-two"):
        assert forbidden not in message, f"the message contains {forbidden!r}: {message}"
    assert isinstance(error.value, AuthConfigError) and isinstance(error.value, ValueError)


DUPLICATES = [
    (f"alice:{HA} alice:{HB}", 2), (f"Alice:{HA} bob:{HB} ALICE:{HB}", 3), (f"alice:{HA},\uff41\uff4c\uff49\uff43\uff45:{HB}", 2),
    (f"a-b:{HA} c-d:{HB} a-b:{HA}", 3),
]


@pytest.mark.parametrize("text, number", DUPLICATES)
def test_a_name_that_repeats_after_normalisation_is_an_error_naming_the_entry(text, number):
    with pytest.raises(ProvisionedUsersError) as error:
        parse_provisioned_users(text)
    assert error.value.entry == number and "repeats a user name" in str(error.value) and str(error.value).startswith(f"AUTH_USERS entry {number} ")
    for forbidden in ("alice", "a-b", "c-d", "bob", "scrypt$", "AAAA"):
        assert forbidden not in str(error.value).lower().replace("auth_users", "")


# --- messages and logs never hold a secret ---------------------------------------------------------------------------------------------------------------------------

SENTINEL_PASSWORD = "Sentinel-Passw0rd-9731!"
SENTINEL_USER = "Sentinel User Name 9731"


def collect_errors(settings):
    """Every kind of error the module can raise, built from sentinel inputs."""
    errors = []
    for call in (
        lambda: normalise_username(SENTINEL_USER),
        lambda: check_password("Sentinel", SENTINEL_USER),
        lambda: check_password(SENTINEL_PASSWORD * 10, SENTINEL_USER),
        lambda: check_password(SENTINEL_PASSWORD.lower(), SENTINEL_PASSWORD),
        lambda: hash_password(SENTINEL_PASSWORD * 10, settings),
        lambda: parse_provisioned_users(f"{SENTINEL_USER}:garbage"),
        lambda: parse_provisioned_users(f"sentinel-ok:{HA} sentinel-ok:{HB}"),
        lambda: parse_provisioned_users(f"sentinel-ok:{HA} x:{SENTINEL_PASSWORD}"),
    ):
        try:
            call()
        except Exception as error:  # noqa: BLE001
            errors.append(error)
    gate = HashGate(0, 1)
    gate.acquire()
    try:
        verify_unknown(SENTINEL_PASSWORD, settings, gate=gate)
    except Busy as error:
        errors.append(error)
    gate.release()
    return errors


def test_no_exception_message_holds_a_password_a_hash_a_salt_or_a_user_name(auth_settings):
    errors = collect_errors(auth_settings)
    assert len(errors) == 9
    real_hash = hash_password(SENTINEL_PASSWORD, auth_settings)
    salt = real_hash.split("$")[4]
    for error in errors:
        text = f"{error} {error!r} {error.args}".lower()
        for secret in ("sentinel", "9731", real_hash.lower(), salt.lower(), "scrypt$"):
            assert secret not in text, f"{type(error).__name__}: {text}"


def test_nothing_is_logged_by_any_operation_and_no_log_holds_a_secret(auth_settings, caplog):
    caplog.set_level(logging.DEBUG)
    text = hash_password(SENTINEL_PASSWORD, auth_settings)
    verify_password(SENTINEL_PASSWORD, text)
    verify_password("wrong-Passw0rd-1234", text)
    verify_unknown(SENTINEL_PASSWORD, auth_settings)
    parse_hash(text)
    needs_rehash(text, auth_settings)
    parse_provisioned_users(f"sentinel-user:{text}")
    collect_errors(auth_settings)
    assert caplog.records == []
    assert SENTINEL_PASSWORD not in caplog.text and text not in caplog.text


# --- import: no environment, nothing created, no scrypt ---------------------------------------------------------------------------------------------------------------

IMPORT_PROOF = r"""
import faulthandler, hashlib, json, os, sys
faulthandler.dump_traceback_later(60, exit=True)
sys.path.insert(0, sys.argv[1])
reads, scrypts = [], []

class Recorder(dict):
    def __getitem__(self, k): reads.append(k); return super().__getitem__(k)
    def get(self, k, d=None): reads.append(k); return super().get(k, d)
    def __contains__(self, k): reads.append(k); return super().__contains__(k)
    def __iter__(self): reads.append("<iter>"); return super().__iter__()

os.environ = Recorder(dict(os.environ))
real = hashlib.scrypt
hashlib.scrypt = lambda *a, **k: (scrypts.append(1), real(*a, **k))[1]
before = set(sys.modules)
import app.auth.passwords as p
imported = sorted(m for m in set(sys.modules) - before if m == "app" or m.startswith(("app.", "dotenv")))
print(json.dumps({"reads": list(reads), "scrypts": len(scrypts), "imported": imported, "dotenv": "dotenv" in sys.modules, "app_config": "app.config" in sys.modules,
                  "dummy": p._dummy, "gate": p._gate is not None}))
"""


@pytest.fixture(scope="module")
def import_proof():
    result = subprocess.run([sys.executable, "-B", "-c", IMPORT_PROOF, str(ROOT)], capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert result.returncode == 0, result.stderr[-800:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_importing_passwords_reads_no_environment_and_loads_only_the_auth_modules(import_proof):
    assert import_proof["reads"] == []
    assert import_proof["imported"] == ["app", "app.auth", "app.auth.config", "app.auth.passwords"]
    assert import_proof["dotenv"] is False and import_proof["app_config"] is False


def test_importing_passwords_runs_no_scrypt_and_creates_neither_the_dummy_nor_the_gate(import_proof):
    assert import_proof["scrypts"] == 0
    assert import_proof["dummy"] is None
    assert import_proof["gate"] is False
