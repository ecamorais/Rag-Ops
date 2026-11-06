"""AuthService.sign_up and create_invite (app/auth/service.py).

Expected values come from the approved design (scratchpad auth_design_approved.md and the Phase 2 plan), not from running the code: sign-up modes
closed | invite | open, "provisioned users force closed", "sign-up never creates a session", "ONE generic answer for a bad invite and a taken name", "the
rules run before any hashing", "an invite is used in the same transaction that creates the user", "invite codes are random and only their SHA-256 is
stored", "usernames are NFKC-normalised and lower-cased". Scrypt work is counted by wrapping passwords._derive (derive_counter), never by timing.
Tests named pinned_ record behaviour the design does not state. Threads are joined with a limit so a hang fails the test instead of blocking the run.
This file has no non-ASCII characters on purpose: other characters are built with chr().
"""
import dataclasses
import threading

import pytest

from app.auth import passwords
from app.auth.config import AuthSettings
from app.auth.service import SignUpResult

PW = "Sentinel-Passw0rd-9731!"
REJECTED = SignUpResult("rejected", "signup_rejected")
CLOSED = SignUpResult("closed", "signup_closed")
MAX_LIFETIME = 10 * 365 * 24 * 3600


def users(env):
    return env.sql("SELECT COUNT(*) FROM users")[0][0]


def sessions(env):
    return env.sql("SELECT COUNT(*) FROM sessions")[0][0]


def uses_left(env, code):
    return env.sql("SELECT uses_left FROM invites WHERE code_hash = ?", (env.digest(code),))[0][0]


def provisioned_entry(name="prov.user"):
    return f"{name}:{passwords.hash_password(PW, AuthSettings(scrypt_n=2**10))}"


# --- closed ---------------------------------------------------------------------------------------------------------------------------------------

CLOSED_INPUTS = [("alice", PW, None), ("alice", PW, "some-code"), ("a", "x", None), ("alice", "short", None), (None, None, None), (5, 6, 7), (b"alice", b"pw", [])]


@pytest.mark.parametrize("username, password, code", CLOSED_INPUTS, ids=[f"{i}" for i in range(len(CLOSED_INPUTS))])
def test_in_closed_mode_every_sign_up_is_closed_whatever_it_holds_and_nothing_is_done(make_service, derive_counter, username, password, code):
    env = make_service(signup="closed")
    assert env.svc.sign_up(username, password, code) == CLOSED
    assert derive_counter.count == 0 and users(env) == 0 and sessions(env) == 0


@pytest.mark.parametrize("configured", ["closed", "invite", "open"])
def test_provisioned_users_force_closed_even_when_the_setting_says_open(make_service, derive_counter, configured):
    env = make_service(signup=configured, users_text=provisioned_entry())
    assert env.settings.effective_signup == "closed"
    code = env.svc.create_invite()
    derive_counter.reset()
    assert env.svc.sign_up("alice", PW) == CLOSED
    assert env.svc.sign_up("alice", PW, code) == CLOSED
    assert derive_counter.count == 0
    assert users(env) == 1 and env.store.get_user("alice") is None and uses_left(env, code) == 1


# --- invite mode: the statuses --------------------------------------------------------------------------------------------------------------------

def test_a_valid_code_creates_the_account_with_a_normalised_name_and_returns_the_name(make_service):
    env = make_service(signup="invite")
    code = env.svc.create_invite()
    result = env.svc.sign_up("  Alice.B_c-1 ", PW, code)
    assert result == SignUpResult("created", "", "alice.b_c-1")
    user = env.store.get_user("alice.b_c-1")
    assert user.source == "local" and user.disabled is False and user.pw_hash.startswith("scrypt$1024$8$1$")
    assert passwords.verify_password(PW, user.pw_hash) is True


def test_a_created_account_has_a_session_of_nobody_sign_up_never_logs_in(make_service):
    env = make_service(signup="invite")
    for n in range(3):
        env.svc.sign_up(f"user-{n}", PW, env.svc.create_invite())
    env.svc.sign_up("user-0", PW, "wrong")
    assert users(env) == 3 and sessions(env) == 0
    assert {f.name for f in dataclasses.fields(SignUpResult)} == {"status", "code", "username"}


def test_names_are_nfkc_normalised_and_lower_cased_by_the_service(make_service):
    env = make_service(signup="open")
    assert env.svc.sign_up("ALICE", PW) == SignUpResult("created", "", "alice")
    assert env.svc.sign_up(chr(0xFF21) + chr(0xFF2C) + chr(0xFF29) + chr(0xFF23) + chr(0xFF25), PW) == REJECTED  # fullwidth ALICE is the same name
    assert env.svc.sign_up("Alice", PW) == REJECTED
    assert env.store.get_user("alice") is not None and env.store.get_user("ALICE") is None and users(env) == 1


def test_pinned_the_store_alone_would_accept_two_cases_of_a_name_so_only_the_service_keeps_them_unique(make_service):
    """Unwritten behaviour, not a deviation: uniqueness of names ignoring case is the service's job (normalise_username), not the database's."""
    env = make_service(signup="open")
    assert env.store.add_user("Bob", "h") is not None and env.store.add_user("bob", "h") is not None
    assert users(env) == 2
    assert env.svc.sign_up("BOB", PW) == REJECTED  # the service lower-cases to "bob", which exists


# --- the five reasons for a rejection are indistinguishable -----------------------------------------------------------------------------------------

def test_the_five_rejection_reasons_return_the_identical_result_after_exactly_one_scrypt_each(make_service, clock, derive_counter):
    env = make_service(signup="invite")
    svc = env.svc
    env.add_local_user("taken", PW)
    good = svc.create_invite(uses=5)
    expired = svc.create_invite(expires_in=100)
    used = svc.create_invite(uses=1)
    assert svc.sign_up("first", PW, used).status == "created"
    clock.advance(100)  # now equals expires_at exactly: the code is expired
    cases = {
        "missing code": lambda: svc.sign_up("newname", PW),
        "wrong code": lambda: svc.sign_up("newname", PW, "not-a-code"),
        "used-up code": lambda: svc.sign_up("newname", PW, used),
        "expired code": lambda: svc.sign_up("newname", PW, expired),
        "taken name": lambda: svc.sign_up("taken", PW, good),
    }
    results = {}
    for label, call in cases.items():
        derive_counter.reset()
        results[label] = call()
        assert derive_counter.count == 1, f"{label}: {derive_counter.count} scrypt runs"
    assert all(r == REJECTED for r in results.values()), results
    assert len({(r.status, r.code, r.username) for r in results.values()}) == 1
    assert users(env) == 2  # taken, first
    assert uses_left(env, good) == 5, "a taken name used up the code"


def test_a_successful_sign_up_does_the_same_scrypt_work_as_a_rejected_one(make_service, derive_counter):
    env = make_service(signup="invite")
    code = env.svc.create_invite(uses=2)
    derive_counter.reset()
    assert env.svc.sign_up("ok-one", PW, code).status == "created"
    created = derive_counter.count
    derive_counter.reset()
    assert env.svc.sign_up("ok-two", PW, "wrong") == REJECTED
    assert created == derive_counter.count == 1


def test_a_taken_name_leaves_the_code_unused_and_it_still_works_for_a_free_name(make_service):
    env = make_service(signup="invite")
    env.add_local_user("carol", PW)
    code = env.svc.create_invite()
    assert env.svc.sign_up("carol", PW, code) == REJECTED
    assert uses_left(env, code) == 1
    assert env.svc.sign_up("erin", PW, code).status == "created"
    assert uses_left(env, code) == 0


def test_a_used_up_code_creates_no_account_and_a_three_use_code_works_three_times(make_service):
    env = make_service(signup="invite")
    code = env.svc.create_invite(uses=3)
    results = [env.svc.sign_up(f"user-{n}", PW, code) for n in range(4)]
    assert [r.status for r in results] == ["created", "created", "created", "rejected"]
    assert users(env) == 3 and uses_left(env, code) == 0


def test_a_code_expiring_at_exactly_expires_at_is_rejected_and_one_second_earlier_it_works(make_service, clock):
    env = make_service(signup="invite")
    early, exact = env.svc.create_invite(expires_in=100), env.svc.create_invite(expires_in=100)
    clock.advance(99)
    assert env.svc.sign_up("early", PW, early).status == "created"
    clock.advance(1)
    assert env.svc.sign_up("exact", PW, exact) == REJECTED and env.store.get_user("exact") is None


def test_pinned_whitespace_around_a_code_is_ignored_and_an_oversized_code_is_just_rejected(make_service):
    """Unwritten behaviour, not a deviation: codes are stripped; a code over 200 characters is not looked up at all."""
    env = make_service(signup="invite")
    code = env.svc.create_invite(uses=2)
    assert env.svc.sign_up("padded", PW, "  " + code + "\n").status == "created"
    assert env.svc.sign_up("huge", PW, code + "x" * 300) == REJECTED
    assert uses_left(env, code) == 1


# --- the rules run first -----------------------------------------------------------------------------------------------------------------------------

INVALID = [
    ("ab", PW, "username_invalid"), ("a b", PW, "username_invalid"), ("-abc", PW, "username_invalid"), ("a" * 33, PW, "username_invalid"),
    ("alice", "short", "password_too_short"), ("alice", "a" * 12, "password_repeated"), ("alice", "abc\x00defghijkl", "password_nul"),
    ("alice", "x" * 129, "password_too_long"), ("alice", "ab" * 300, "password_too_long"), ("alicealicealice", "AliceAliceAlice", "password_equals_username"),
    (None, PW, "username_invalid"), (5, PW, "username_invalid"), ("alice", None, "password_invalid"), ("alice", 12345678901234, "password_invalid"),
    ("alice", b"abcdefghijklmnop", "password_invalid"), ("alice", "abc" + chr(0xD800) + "defghijklmnop", "password_invalid"),
]


@pytest.mark.parametrize("username, password, code", INVALID, ids=[f"{i}-{c}" for i, (_, _, c) in enumerate(INVALID)])
def test_pinned_invalid_input_is_reported_with_its_rule_code_and_does_zero_scrypt_work(make_service, derive_counter, username, password, code):
    """Unwritten behaviour, not a deviation: the rules are checked before any hashing, so a bad request costs no scrypt (and says which rule it broke)."""
    env = make_service(signup="invite")
    invite = env.svc.create_invite()
    derive_counter.reset()
    assert env.svc.sign_up(username, password, invite) == SignUpResult("invalid_input", code)
    assert derive_counter.count == 0
    assert uses_left(env, invite) == 1 and users(env) == 0


def test_the_mode_is_checked_before_the_rules(make_service):
    assert make_service(signup="closed").svc.sign_up("a", "x") == CLOSED


# --- busy --------------------------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["invite", "open"])
def test_a_full_hashing_gate_gives_busy_with_no_scrypt_and_the_code_unused(make_service, derive_counter, mode):
    gate = passwords.HashGate(0, 5)
    env = make_service(signup=mode, gate=gate)
    code = env.svc.create_invite()
    gate.acquire()
    try:
        derive_counter.reset()
        assert env.svc.sign_up("alice", PW, code) == SignUpResult("busy", "busy")
        assert derive_counter.count == 0 and users(env) == 0 and uses_left(env, code) == 1
    finally:
        gate.release()
    assert env.svc.sign_up("alice", PW, code).status == "created"


# --- open mode ---------------------------------------------------------------------------------------------------------------------------------------

def test_open_sign_up_needs_no_code_ignores_one_and_reports_a_taken_name_as_rejected(make_service):
    env = make_service(signup="open")
    assert env.svc.sign_up("alice", PW).status == "created"
    assert env.svc.sign_up("bob", PW, "anything at all").status == "created"
    assert env.svc.sign_up("alice", PW) == REJECTED
    assert env.sql("SELECT COUNT(*) FROM invites")[0][0] == 0


# --- concurrency -------------------------------------------------------------------------------------------------------------------------------------

def test_eight_threads_signing_up_with_one_single_use_code_give_exactly_one_created_every_round(make_service):
    env = make_service(signup="invite", gate=passwords.HashGate(64, 60))
    for rnd in range(6):
        code = env.svc.create_invite(uses=1)
        barrier = threading.Barrier(8, timeout=30)
        out = [None] * 8

        def work(i, code=code, rnd=rnd, out=out, barrier=barrier):
            barrier.wait()
            out[i] = env.svc.sign_up(f"racer{rnd}x{i}", PW, code)

        threads = [threading.Thread(target=work, args=(i,), daemon=True) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert not any(t.is_alive() for t in threads), "a thread is still running: a hang, not a result"
        assert sorted(r.status for r in out) == ["created"] + ["rejected"] * 7, out
        assert env.sql("SELECT COUNT(*) FROM users WHERE username LIKE ?", (f"racer{rnd}x%",))[0][0] == 1
        assert uses_left(env, code) == 0


# --- create_invite -----------------------------------------------------------------------------------------------------------------------------------

def test_an_invite_code_is_22_url_safe_characters_random_and_only_its_sha256_is_stored(make_service):
    env = make_service(signup="invite")
    codes = [env.svc.create_invite(note=f"note {n}") for n in range(5)]
    assert len(set(codes)) == 5
    assert all(len(c) == 22 and all(ch.isalnum() or ch in "_-" for ch in c) for c in codes)
    stored = sorted(r[0] for r in env.sql("SELECT code_hash FROM invites"))
    assert stored == sorted(env.digest(c) for c in codes)


def test_the_plain_invite_code_the_password_and_the_name_of_a_failed_try_are_in_no_database_byte(make_service):
    env = make_service(signup="invite")
    code = env.svc.create_invite(note="for erin")
    assert env.svc.sign_up("erin", PW, code).status == "created"
    env.svc.sign_up("somebody-else", PW, "a-wrong-code-0123456789")
    data = env.db_bytes()
    assert code.encode() not in data and PW.encode() not in data and b"a-wrong-code-0123456789" not in data
    assert env.digest(code).encode() in data and b"erin" in data and b"for erin" in data  # positive controls


def test_an_invite_stores_its_uses_note_expiry_and_creation_time(make_service, clock):
    env = make_service(signup="invite")
    code = env.svc.create_invite(uses=7, expires_in=3600, note="a note")
    row = env.sql("SELECT uses_left, expires_at, created_at, note FROM invites WHERE code_hash = ?", (env.digest(code),))[0]
    assert row == (7, int(clock()) + 3600, int(clock()), "a note")
    forever = env.svc.create_invite()
    assert env.sql("SELECT uses_left, expires_at, note FROM invites WHERE code_hash = ?", (env.digest(forever),))[0] == (1, None, "")


@pytest.mark.parametrize("uses, ok", [(1, True), (1000, True), (0, False), (1001, False), (-1, False), (True, False), (False, False), (1.0, False), ("3", False), (None, False)])
def test_create_invite_uses_must_be_a_whole_number_from_1_to_1000(make_service, uses, ok):
    env = make_service(signup="invite")
    if ok:
        assert uses_left(env, env.svc.create_invite(uses=uses)) == uses
    else:
        with pytest.raises(ValueError):
            env.svc.create_invite(uses=uses)
        assert env.sql("SELECT COUNT(*) FROM invites")[0][0] == 0


@pytest.mark.parametrize("seconds, ok", [(None, True), (1, True), (1.0, True), (1.5, True), (MAX_LIFETIME, True), (0, False), (0.5, False), (0.999, False), (-1, False),
                                         (MAX_LIFETIME + 1, False), (float("inf"), False), (float("nan"), False), (True, False), ("100", False)])
def test_create_invite_expires_in_must_be_none_or_between_1_second_and_ten_years(make_service, clock, seconds, ok):
    env = make_service(signup="invite")
    if ok:
        code = env.svc.create_invite(expires_in=seconds)
        expected = None if seconds is None else int(clock()) + int(seconds)
        assert env.sql("SELECT expires_at FROM invites WHERE code_hash = ?", (env.digest(code),))[0][0] == expected
    else:
        with pytest.raises(ValueError):
            env.svc.create_invite(expires_in=seconds)


@pytest.mark.parametrize("note, ok", [("", True), ("x" * 200, True), ("x" * 201, False), (5, False), (None, False), (b"note", False)])
def test_create_invite_note_must_be_text_of_at_most_200_characters(make_service, note, ok):
    env = make_service(signup="invite")
    if ok:
        code = env.svc.create_invite(note=note)
        assert env.sql("SELECT note FROM invites WHERE code_hash = ?", (env.digest(code),))[0][0] == note
    else:
        with pytest.raises(ValueError):
            env.svc.create_invite(note=note)


def test_create_invite_error_messages_hold_no_value(make_service):
    env = make_service(signup="invite")
    for kwargs in ({"note": "SECRETNOTE" * 30}, {"uses": 99999}, {"expires_in": 10**12}):
        with pytest.raises(ValueError) as error:
            env.svc.create_invite(**kwargs)
        assert "SECRETNOTE" not in str(error.value) and "99999" not in str(error.value) and "1000000000000" not in str(error.value)


def test_an_expiry_of_one_second_stores_now_plus_one_and_the_code_works_now_and_not_one_second_later(make_service, clock):
    env = make_service(signup="invite")
    code = env.svc.create_invite(expires_in=1)
    assert env.sql("SELECT expires_at FROM invites WHERE code_hash = ?", (env.digest(code),))[0][0] == int(clock()) + 1
    assert env.svc.sign_up("just-in-time", PW, code).status == "created"
    again = env.svc.create_invite(expires_in=1)
    clock.advance(1)
    assert env.svc.sign_up("too-late", PW, again) == REJECTED


@pytest.mark.parametrize("seconds", [0.5, 0.999, 0, 0.0])
def test_an_expiry_below_one_second_is_refused_and_no_invite_is_stored(make_service, seconds):
    env = make_service(signup="invite")
    with pytest.raises(ValueError):
        env.svc.create_invite(expires_in=seconds)
    assert env.sql("SELECT COUNT(*) FROM invites")[0][0] == 0
