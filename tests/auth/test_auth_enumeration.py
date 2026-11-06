"""Account enumeration: what a caller of AuthService can learn about which accounts exist (app/auth/service.py).

Expected values come from the approved design (scratchpad auth_design_approved.md and the Phase 2 plan): "login always gives ONE generic failure (the same
fields, the same delay) for an unknown name, a wrong password, a disabled user, a malformed name and a local/provisioned mix-up", "unknown names behave exactly
like real ones" (amendments A1 and B1), "the failure for an unknown or disabled user does the same scrypt work as a real verification", "sign-up answers one
generic 'rejected'", and "nothing returned or raised holds a user name, password, token, code, hash or address".
TIMING IS NOT ASSERTED anywhere in this file: wall-clock timing is flaky and proves little. "The same work" is shown by counting scrypt runs
(derive_counter), "the same answer" by comparing the whole result objects, and "no hidden side effect" by comparing the database tables.
Tests named pinned_ record behaviour the design does not state. This file has no non-ASCII characters on purpose: other characters are built with chr().
"""
import logging

import pytest

from app.auth import passwords
from app.auth.config import AuthSettings
from app.auth.service import LoginResult, SignUpResult

PW = "Sentinel-Passw0rd-9731!"
WRONG = "wrong-Passw0rd-1234"
IP = "203.0.113.77"
REJECTED = SignUpResult("rejected", "signup_rejected")


@pytest.fixture(params=["untrusted", "trusted"])
def mode(request):
    return request.param


def build(make_service, mode, provisioned=False):
    trusted = mode == "trusted"
    if provisioned:
        h = passwords.hash_password(PW, AuthSettings(scrypt_n=2**10))
        env = make_service(signup="open", trust_proxy=trusted, users_text=f"prov:{h} prov2:{h}")
        env.sql("UPDATE users SET disabled = 1 WHERE username = ?", ("prov2",))
        env.store.add_user("localguy", h)
    else:
        env = make_service(signup="open", trust_proxy=trusted)
        env.add_local_user("alice", PW)
        env.add_local_user("dave", PW)
        env.sql("UPDATE users SET disabled = 1 WHERE username = ?", ("dave",))
    return env


KINDS_PLAIN = {
    "unknown name": ("nobody", PW),
    "known name, wrong password": ("alice", WRONG),
    "disabled user, right password": ("dave", PW),
    "too short": ("ab", PW),
    "with spaces": ("a b c", PW),
    "very long": ("x" * 100, PW),
    "nul characters": ("\x00\x00\x00", PW),
    "accented letters": ("caf" + chr(233) + "-name", PW),
    "unknown with capitals": ("NoBody2", PW),
}
KINDS_PROVISIONED = {
    "unknown name": ("nobody", PW),
    "provisioned user, wrong password": ("prov", WRONG),
    "local user while users are provisioned": ("localguy", PW),
    "disabled provisioned user": ("prov2", PW),
}


def transcript(env, name, password):
    """Eight attempts from one address, the clock moved by two seconds after the sixth: (results, scrypt runs per attempt)."""
    results, counts = [], []
    for step in range(8):
        if step == 6:
            env.clock.advance(2)
        before = env.derive_counter.count
        results.append(env.svc.log_in(name, password, IP))
        counts.append(env.derive_counter.count - before)
    return results, counts


def run_kinds(make_service, derive_counter, mode, kinds, provisioned):
    env = build(make_service, mode, provisioned)
    env.derive_counter = derive_counter
    out = {}
    for label, (name, password) in kinds.items():
        out[label] = transcript(env, name, password)
    return env, out


# --- every kind of failed login gives the same answers after the same work ----------------------------------------------------------------------------------

def test_all_kinds_of_failed_logins_give_identical_results_step_by_step(make_service, derive_counter, mode):
    _, out = run_kinds(make_service, derive_counter, mode, KINDS_PLAIN, False)
    baseline = out["unknown name"][0]
    for label, (results, _) in out.items():
        assert results == baseline, f"{label} differs from an unknown name: {results} vs {baseline}"


def test_all_kinds_of_failed_logins_do_the_same_scrypt_work_step_by_step(make_service, derive_counter, mode):
    _, out = run_kinds(make_service, derive_counter, mode, KINDS_PLAIN, False)
    baseline = out["unknown name"][1]
    for label, (_, counts) in out.items():
        assert counts == baseline, f"{label}: {counts} vs {baseline}"


def test_the_transcript_of_an_unknown_name_is_the_designed_one(make_service, derive_counter, mode):
    """Anchors the equality tests above to the design: trusted = 3 free failures, the 4th blocks for 2 s (refusals do no scrypt), after 2 s one more
    failure blocks for 4 s; untrusted = never refused, delays 0, 0, 0, 2, then the cap of 3."""
    _, out = run_kinds(make_service, derive_counter, mode, {"unknown name": KINDS_PLAIN["unknown name"]}, False)
    results, counts = out["unknown name"]
    if mode == "trusted":
        assert results == [LoginResult(False)] * 4 + [LoginResult(False, retry_after=2)] * 2 + [LoginResult(False), LoginResult(False, retry_after=4)]
        assert counts == [1, 1, 1, 1, 0, 0, 1, 0]
    else:
        assert [r.delay for r in results] == [0, 0, 0, 2, 3, 3, 3, 3] and all(r.retry_after is None and r.ok is False for r in results)
        assert counts == [1] * 8


def test_pinned_a_refused_attempt_does_no_scrypt_for_real_and_unknown_names_alike(make_service, derive_counter):
    """Unwritten behaviour, not a deviation: in trusted mode a blocked key is refused before anything is hashed, whether or not the name exists."""
    _, out = run_kinds(make_service, derive_counter, "trusted", KINDS_PLAIN, False)
    for label, (results, counts) in out.items():
        assert [c for r, c in zip(results, counts) if r.retry_after is not None] == [0, 0, 0], label


def test_the_failures_while_users_are_provisioned_are_identical_too(make_service, derive_counter, mode):
    _, out = run_kinds(make_service, derive_counter, mode, KINDS_PROVISIONED, True)
    baseline_results, baseline_counts = out["unknown name"]
    for label, (results, counts) in out.items():
        assert results == baseline_results and counts == baseline_counts, label


def test_an_over_long_password_is_refused_with_the_same_answer_and_no_scrypt_for_every_kind(make_service, derive_counter, mode):
    env = build(make_service, mode)
    derive_counter.reset()
    results = {label: env.svc.log_in(name, "x" * 513, IP) for label, (name, _) in KINDS_PLAIN.items()}
    assert set(results.values()) == {LoginResult(False)} and derive_counter.count == 0


@pytest.mark.parametrize("name, password", [(None, None), (5, 6), (b"abc", b"x"), ([], {}), (object(), object()), ("alice", None), ("alice", 12345678901234)],
                         ids=[f"odd{i}" for i in range(7)])
def test_odd_types_give_the_same_first_failure_as_an_unknown_name(make_service, derive_counter, mode, name, password):
    env = build(make_service, mode)
    first_unknown = env.svc.log_in("nobody", PW, IP)
    assert env.svc.log_in(name, password, IP) == first_unknown == LoginResult(False)


def test_the_right_password_of_a_disabled_user_looks_exactly_like_a_wrong_password(make_service, mode):
    env = build(make_service, mode)
    assert env.svc.log_in("dave", PW, IP) == env.svc.log_in("alice", WRONG, IP) == env.svc.log_in("nobody", PW, IP)


# --- a failure leaves the same traces whether the name exists or not ------------------------------------------------------------------------------------

STATE_TABLES = ("users", "sessions", "invites", "meta")


def snapshot(env, tables):
    return {t: sorted(env.sql(f"SELECT * FROM {t}"), key=repr) for t in tables}


def test_failed_logins_for_a_real_and_an_unknown_name_leave_the_same_rows_except_for_the_failure_rows(make_service, mode):
    env = build(make_service, mode)
    before = snapshot(env, STATE_TABLES)
    for _ in range(4):
        env.svc.log_in("alice", WRONG, IP)
        env.svc.log_in("alicf", WRONG, IP)
    assert snapshot(env, STATE_TABLES) == before
    rows = env.sql("SELECT failures, last_failure_at, blocked_until FROM login_failures")
    assert len(rows) == 2 and rows[0] == rows[1], rows


def test_a_failure_row_exists_for_a_real_a_disabled_a_malformed_and_an_unknown_name_alike(make_service, mode):
    env = build(make_service, mode)
    for name in ("alice", "dave", "nobody", "a b c", "x" * 100):
        env.svc.log_in(name, WRONG, IP)
    rows = env.sql("SELECT failures, last_failure_at, blocked_until FROM login_failures")
    assert len(rows) == 5 and len(set(rows)) == 1


def test_sign_up_rejections_change_nothing_in_the_database(make_service):
    env = make_service(signup="invite")
    env.add_local_user("taken", PW)
    good = env.svc.create_invite(uses=3)
    tables = ("users", "sessions", "invites", "login_failures", "meta")
    before = snapshot(env, tables)
    assert env.svc.sign_up("taken", PW, good) == REJECTED
    assert env.svc.sign_up("newname", PW, "not-a-code") == REJECTED
    assert env.svc.sign_up("newname", PW) == REJECTED
    assert snapshot(env, tables) == before


# --- nothing returned or raised holds a secret ------------------------------------------------------------------------------------------------------------

def test_no_failure_result_rejection_or_error_holds_a_name_password_token_code_hash_or_address(make_service, mode, caplog):
    caplog.set_level(logging.DEBUG)
    env = make_service(signup="invite", trust_proxy=(mode == "trusted"))
    user, ghost = "sentinel-user-9731", "sentinel-ghost-9731"
    env.add_local_user(user, PW)
    code = env.svc.create_invite(uses=2)
    live = env.svc.log_in(user, PW, IP)
    stored_hash = env.store.get_user(user).pw_hash
    salt = stored_hash.split("$")[4]
    outputs = []
    for _ in range(6):
        outputs += [env.svc.log_in(ghost, PW, IP), env.svc.log_in(user, "wrong-" + PW, IP), env.svc.log_in(user + "x", PW, IP)]
    outputs += [env.svc.sign_up(user, PW, code), env.svc.sign_up(ghost, PW, "wrong-" + code), env.svc.sign_up(ghost, PW),
                env.svc.sign_up(ghost, "short", code), env.svc.sign_up(user, user, code)]
    outputs += [env.svc.validate_session(live.session_token[:-1] + "x"), env.svc.validate_session(ghost), env.svc.verify_csrf(live.session_token, "wrong-" + live.csrf_token),
                env.svc.log_out(live.session_token[::-1]), env.svc.log_out_everywhere(ghost)]
    gate = passwords.HashGate(0, 5)
    busy_env = make_service(signup="open", gate=gate)
    busy_env.add_local_user(user, PW)
    gate.acquire()
    try:
        outputs += [busy_env.svc.log_in(user, PW, IP), busy_env.svc.sign_up(ghost, PW)]
    finally:
        gate.release()
    errors = []
    for call in (lambda: env.svc.create_invite(note="x" * 201 + user), lambda: env.svc.create_invite(uses=10**6), lambda: env.svc.create_invite(expires_in=-1),
                 lambda: passwords.hash_password(user * 100, env.settings), lambda: passwords.normalise_username(ghost + " " + PW)):
        try:
            call()
        except Exception as error:  # noqa: BLE001
            errors += [str(error), repr(error), repr(error.args)]
    shown = [repr(o) for o in outputs] + errors
    secrets_ = [user, ghost, PW, "wrong-" + PW, IP, "203.0.113", code, "wrong-" + code, stored_hash, salt, live.session_token, live.csrf_token, "scrypt$"]
    for text in shown:
        for secret in secrets_:
            assert secret.lower() not in text.lower(), f"{secret!r} found in {text!r}"
    assert caplog.records == [] and len(shown) > 40
    assert not any(isinstance(o, SignUpResult) and o.username for o in outputs)  # a rejection or invalid input never carries a name


def test_the_service_and_its_settings_show_no_secret_in_their_repr(make_service):
    env = make_service(signup="invite", users_text="secretname:scrypt$1024$8$1$" + "A" * 22 + "$" + "B" * 43)
    for text in (repr(env.svc), repr(env.settings), str(env.settings), repr(env.store)):
        assert "secretname" not in text and "scrypt$" not in text and "A" * 22 not in text


# --- sessions: the same False or None for every kind of bad token -----------------------------------------------------------------------------------------------

def test_unknown_expired_disabled_deleted_and_malformed_tokens_all_give_the_same_none_and_false(make_service, clock):
    env = make_service(signup="open", session_idle_seconds=600, session_absolute_seconds=3600, session_touch_seconds=3000)
    for name in ("alice", "bobby", "carol", "dave"):
        env.add_local_user(name, PW)
    idle = env.svc.log_in("alice", PW, "").session_token
    disabled = env.svc.log_in("bobby", PW, "").session_token
    deleted_user = env.svc.log_in("carol", PW, "").session_token
    fresh = env.svc.log_in("dave", PW, "")
    env.sql("UPDATE users SET disabled = 1 WHERE username = ?", ("bobby",))
    env.sql("DELETE FROM users WHERE username = ?", ("carol",))
    clock.advance(600)  # alice's and dave's sessions are idle now; dave's is the one whose csrf we know
    tokens = {"unknown": "x" * 43, "idle-expired": idle, "disabled user": disabled, "deleted user": deleted_user, "too short": "x" * 42, "empty": "", "none": None,
              "number": 5, "nul": "\x00" * 43, "huge": "x" * 100_000, "bytes": b"x" * 43}
    for label, token in tokens.items():
        assert env.svc.validate_session(token) is None, label
        assert env.svc.verify_csrf(token, fresh.csrf_token) is False, label
        assert env.svc.verify_csrf(token, "x" * 43) is False, label
        assert env.svc.log_out_everywhere(token) == 0, label


def test_an_absolute_expired_token_is_indistinguishable_from_an_unknown_one(make_service, clock):
    env = make_service(signup="open", session_idle_seconds=3600, session_absolute_seconds=3600, session_touch_seconds=0)
    env.add_local_user("alice", PW)
    token = env.svc.log_in("alice", PW, "").session_token
    clock.advance(1800)
    assert env.svc.validate_session(token) is not None
    clock.advance(1800)
    assert env.svc.validate_session(token) is None and env.svc.validate_session("x" * 43) is None
    assert env.svc.verify_csrf(token, "x" * 43) is False


# --- sign-up cross-checks -------------------------------------------------------------------------------------------------------------------------------------

def test_every_sign_up_rejection_is_one_and_the_same_object_value_for_any_name_and_code(make_service):
    env = make_service(signup="invite")
    env.add_local_user("taken", PW)
    code = env.svc.create_invite(uses=9)
    results = {env.svc.sign_up(name, PW, c) for name, c in (("taken", code), ("brand-new", None), ("brand-new", "nope"), ("brand-new", "x" * 500), ("TAKEN", code), ("taken", "nope"))}
    assert results == {REJECTED}


def test_a_rejection_names_neither_the_user_nor_the_code_nor_a_hash(make_service):
    env = make_service(signup="invite")
    env.add_local_user("sentinel-taken-name", PW)
    code = env.svc.create_invite()
    stored = env.store.get_user("sentinel-taken-name").pw_hash
    text = repr(env.svc.sign_up("sentinel-taken-name", PW, code)) + repr(env.svc.sign_up("sentinel-free-name", PW, "wrong-" + code))
    for secret in ("sentinel", code, stored, "scrypt$", PW):
        assert secret not in text
