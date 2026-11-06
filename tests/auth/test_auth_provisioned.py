"""Provisioned users: AUTH_USERS, AuthService.init() and the rules for logging in while users are provisioned (app/auth/service.py).

Expected values come from the approved design (scratchpad auth_design_approved.md, amendment A2 and B3, and the Phase 2 plan): users come from the
environment variable AUTH_USERS (entries `name:scrypt$...`), sign-up is closed, the environment is the source of truth (a removed user disappears with its
sessions, a changed hash ends that user's sessions), an error in AUTH_USERS names only the entry number, hashes of provisioned users are never rewritten,
and (the owner's confirmation of step 7) only provisioned users can log in while users are provisioned. Tests named pinned_ record behaviour the design does
not state. This file has no non-ASCII characters on purpose.
"""
import pytest

from app.auth import passwords
from app.auth.config import AuthConfigError, AuthSettings
from app.auth.passwords import HashGate, ProvisionedUsersError
from app.auth.service import LoginResult, SignUpResult
from app.auth.store import StoreError

PW = "Sentinel-Passw0rd-9731!"
PW_B = "Another-Passw0rd-5521?"
WRONG = "wrong-Passw0rd-1234"
CHEAP = AuthSettings(scrypt_n=2**10)
HASH_A = passwords.hash_password(PW, CHEAP)
HASH_B = passwords.hash_password(PW_B, CHEAP)
HASH_A2 = passwords.hash_password("A-brand-new-Passw0rd-77", CHEAP)
TWO = f"alice:{HASH_A} bob:{HASH_B}"


def user_names(env):
    return sorted(r[0] for r in env.sql("SELECT username FROM users"))


def count(env, table):
    return env.sql(f"SELECT COUNT(*) FROM {table}")[0][0]


# --- init() syncs ----------------------------------------------------------------------------------------------------------------------------------

def test_init_creates_the_provisioned_users_with_the_listed_hashes(make_service):
    env = make_service(users_text=TWO)
    a, b = env.store.get_user("alice"), env.store.get_user("bob")
    assert (a.source, a.pw_hash, a.disabled) == ("provisioned", HASH_A, False)
    assert (b.source, b.pw_hash) == ("provisioned", HASH_B)
    assert user_names(env) == ["alice", "bob"]


def test_names_in_the_variable_are_normalised_and_any_case_can_log_in(make_service):
    env = make_service(users_text=f"Prov.User:{HASH_A}")
    assert env.store.get_user("prov.user") is not None
    assert env.svc.log_in("PROV.USER", PW, "").ok and env.svc.log_in(" Prov.User ", PW, "").ok


def test_init_twice_is_harmless_it_keeps_users_hashes_and_sessions(make_service):
    env = make_service(users_text=TWO)
    token = env.svc.log_in("alice", PW, "").session_token
    before = env.sql("SELECT id, username, pw_hash, source FROM users ORDER BY id")
    env.svc.init()
    again = env.reopen()
    assert env.sql("SELECT id, username, pw_hash, source FROM users ORDER BY id") == before
    assert again.svc.validate_session(token).username == "alice"
    assert count(env, "sessions") == 1


# --- logging in --------------------------------------------------------------------------------------------------------------------------------------

def test_provisioned_users_log_in_with_the_right_password_only(make_service, derive_counter):
    env = make_service(users_text=TWO)
    assert env.svc.log_in("alice", PW, "").ok and env.svc.log_in("bob", PW_B, "").ok
    derive_counter.reset()
    failures = [env.svc.log_in("alice", WRONG, ""), env.svc.log_in("alice", PW_B, ""), env.svc.log_in("bob", PW, ""), env.svc.log_in("carol", PW, "")]
    assert all(f == LoginResult(False) for f in failures)
    assert derive_counter.count == 4  # one scrypt each, also for the unknown name


def test_a_provisioned_user_gets_a_working_session_that_can_be_logged_out(make_service):
    env = make_service(users_text=TWO)
    result = env.svc.log_in("alice", PW, "")
    info = env.svc.validate_session(result.session_token)
    assert info.username == "alice" and env.svc.verify_csrf(info, result.csrf_token)
    assert env.svc.log_out(result.session_token) is True and env.svc.validate_session(result.session_token) is None


def test_sign_up_is_closed_whatever_the_setting_says(make_service):
    for configured in ("closed", "invite", "open"):
        env = make_service(users_text=TWO, signup=configured)
        assert env.svc.sign_up("carol", PW, "any") == SignUpResult("closed", "signup_closed")
        assert user_names(env) == ["alice", "bob"]


def test_local_users_cannot_log_in_while_users_are_provisioned_and_their_sessions_are_dead(make_service):
    env = make_service(signup="open")
    env.add_local_user("localguy", PW)
    token = env.svc.log_in("localguy", PW, "").session_token
    assert env.svc.validate_session(token) is not None
    provisioned = env.reopen(users_text=f"alice:{HASH_A}")
    assert provisioned.svc.log_in("localguy", PW, "") == LoginResult(False)
    assert provisioned.svc.validate_session(token) is None
    assert provisioned.store.get_user("localguy") is not None  # the local account itself is not deleted
    assert provisioned.svc.log_in("alice", PW, "").ok


def test_the_failure_for_a_local_account_is_identical_to_that_of_an_unknown_name_with_the_same_work(make_service, derive_counter):
    env = make_service(signup="open")
    env.add_local_user("localguy", PW)
    prov = env.reopen(users_text=f"alice:{HASH_A}")
    derive_counter.reset()
    local, unknown = prov.svc.log_in("localguy", PW, ""), prov.svc.log_in("nobody", PW, "")
    assert local == unknown == LoginResult(False) and derive_counter.count == 2


def test_without_provisioned_users_a_provisioned_row_left_behind_cannot_log_in_until_init_removes_it(make_service):
    env = make_service(users_text=f"alice:{HASH_A}")
    token = env.svc.log_in("alice", PW, "").session_token
    plain = AuthSettings(enabled=True, db_path=env.settings.db_path, scrypt_n=2**10, signup="open")
    from app.auth.service import AuthService
    from app.auth.store import Store

    store = Store(plain.db_path, env.clock)
    svc = AuthService(store, plain, env.clock, gate=env.gate)  # no init(): the row is still there
    assert store.get_user("alice") is not None
    assert svc.log_in("alice", PW, "") == LoginResult(False) and svc.validate_session(token) is None


# --- the environment is the source of truth ------------------------------------------------------------------------------------------------------

def test_an_empty_auth_users_removes_the_leftover_provisioned_users_and_their_sessions_but_not_local_users(make_service):
    env = make_service(users_text=TWO)
    token_a = env.svc.log_in("alice", PW, "").session_token
    env.svc.log_in("bob", PW_B, "")
    env.add_local_user("localguy", PW)
    assert count(env, "sessions") == 2
    plain = env.reopen(users_text="", signup="open")
    assert user_names(plain) == ["localguy"] and count(plain, "sessions") == 0
    assert plain.svc.validate_session(token_a) is None
    assert plain.svc.log_in("alice", PW, "") == LoginResult(False)


def test_removing_one_user_from_the_variable_removes_that_user_and_its_sessions_only(make_service):
    env = make_service(users_text=TWO)
    token_a, token_b = env.svc.log_in("alice", PW, "").session_token, env.svc.log_in("bob", PW_B, "").session_token
    after = env.reopen(users_text=f"alice:{HASH_A}")
    assert user_names(after) == ["alice"]
    assert after.svc.validate_session(token_a) is not None and after.svc.validate_session(token_b) is None


def test_a_changed_hash_replaces_the_password_and_ends_that_users_sessions_only(make_service):
    env = make_service(users_text=TWO)
    token_a, token_b = env.svc.log_in("alice", PW, "").session_token, env.svc.log_in("bob", PW_B, "").session_token
    after = env.reopen(users_text=f"alice:{HASH_A2} bob:{HASH_B}")
    assert after.store.get_user("alice").pw_hash == HASH_A2
    assert after.svc.validate_session(token_a) is None and after.svc.validate_session(token_b) is not None
    assert after.svc.log_in("alice", PW, "") == LoginResult(False)
    assert after.svc.log_in("alice", "A-brand-new-Passw0rd-77", "").ok


def test_an_unchanged_hash_keeps_its_sessions_when_the_service_restarts(make_service):
    env = make_service(users_text=TWO)
    token = env.svc.log_in("alice", PW, "").session_token
    after = env.reopen(users_text=f"bob:{HASH_B}   alice:{HASH_A}")  # another order, same values
    assert after.svc.validate_session(token) is not None


def test_a_new_user_in_the_variable_appears_at_the_next_start(make_service):
    env = make_service(users_text=f"alice:{HASH_A}")
    after = env.reopen(users_text=TWO)
    assert user_names(after) == ["alice", "bob"] and after.svc.log_in("bob", PW_B, "").ok


def test_a_local_account_with_the_name_of_a_listed_user_stops_the_start_and_nothing_changes(make_service):
    env = make_service(signup="open")
    env.add_local_user("clash-name", PW)
    env.add_local_user("other-local", PW)
    before = env.sql("SELECT id, username, pw_hash, source FROM users ORDER BY id")
    with pytest.raises(StoreError) as error:
        env.reopen(users_text=f"clash-name:{HASH_A} fresh-user:{HASH_B}")
    assert "clash-name" not in str(error.value) and "fresh-user" not in str(error.value) and "scrypt" not in str(error.value)
    assert env.sql("SELECT id, username, pw_hash, source FROM users ORDER BY id") == before


# --- a bad AUTH_USERS -------------------------------------------------------------------------------------------------------------------------------

SECRET_NAME = "zq-secret-name"
BAD = [  # (text, the entry number the error must name)
    (f"{SECRET_NAME}{HASH_A}", 1),
    (f"{SECRET_NAME}:{HASH_A[:-5]}", 1),
    (f"{SECRET_NAME}:{HASH_A}x", 1),
    (f"{SECRET_NAME}:", 1),
    (f"{SECRET_NAME}:garbage", 1),
    (f"alice:{HASH_A} {SECRET_NAME}:{HASH_A[:-5]}", 2),
    (f"alice:{HASH_A} bob:{HASH_B} {SECRET_NAME}{HASH_A}", 3),
    (f"alice:{HASH_A} bob:{HASH_B} Alice:{HASH_B}", 3),       # a repeated name after normalisation
    (f"x y:{HASH_A}", 1),
    (f"ab:{HASH_A}", 1),
]


@pytest.mark.parametrize("text, number", BAD, ids=[f"{i}->entry {n}" for i, (_, n) in enumerate(BAD)])
def test_a_bad_auth_users_stops_the_start_with_a_config_error_that_names_only_the_entry_number(make_service, text, number):
    with pytest.raises(AuthConfigError) as error:
        make_service(users_text=text)
    assert isinstance(error.value, ProvisionedUsersError) and error.value.entry == number
    message = str(error.value)
    assert message.startswith(f"AUTH_USERS entry {number} ")
    for secret in (SECRET_NAME, HASH_A, HASH_A[:20], HASH_B, "scrypt$", "alice", "bob", "garbage"):
        assert secret not in message, f"the message contains {secret!r}: {message}"


def test_a_bad_auth_users_leaves_the_existing_provisioned_users_untouched(make_service):
    env = make_service(users_text=TWO)
    token = env.svc.log_in("alice", PW, "").session_token
    before = env.sql("SELECT id, username, pw_hash, source FROM users ORDER BY id")
    with pytest.raises(AuthConfigError):
        env.reopen(users_text=f"alice:{HASH_A2} broken-entry")
    assert env.sql("SELECT id, username, pw_hash, source FROM users ORDER BY id") == before
    assert env.reopen(users_text=TWO).svc.validate_session(token) is not None


# --- the gate ----------------------------------------------------------------------------------------------------------------------------------------

def test_init_without_a_gate_argument_sets_the_process_gate_from_the_settings(tmp_path, clock):
    from app.auth.service import AuthService
    from app.auth.store import Store

    settings = AuthSettings(enabled=True, db_path=tmp_path / "g" / "auth.db", scrypt_n=2**10, hash_queue_max=3, hash_timeout_seconds=4)
    passwords.set_gate(None)
    AuthService(Store(settings.db_path, clock), settings, clock).init()
    gate = passwords.get_gate()
    assert (gate.max_waiting, gate.timeout_seconds) == (3, 4)


def test_init_with_a_gate_argument_leaves_the_process_gate_alone(tmp_path, clock):
    from app.auth.service import AuthService
    from app.auth.store import Store

    settings = AuthSettings(enabled=True, db_path=tmp_path / "g" / "auth.db", scrypt_n=2**10, hash_queue_max=3, hash_timeout_seconds=4)
    passwords.set_gate(None)
    own = HashGate(1, 1)
    AuthService(Store(settings.db_path, clock), settings, clock, gate=own).init()
    assert passwords._gate is None  # nothing was configured; the next use would create the default gate
    assert passwords.get_gate() is not own and (passwords.get_gate().max_waiting, passwords.get_gate().timeout_seconds) == (8, 10)


def test_pinned_init_creates_the_database_even_when_auth_users_turns_out_to_be_bad(make_service, tmp_path, clock):
    """Unwritten behaviour, not a deviation: init() creates the schema first and only then reads AUTH_USERS, so a bad variable leaves an empty database
    behind but never half-synced users."""
    from app.auth.service import AuthService
    from app.auth.store import Store

    settings = AuthSettings(enabled=True, db_path=tmp_path / "late" / "auth.db", scrypt_n=2**10, users_text="alice:garbage")
    with pytest.raises(AuthConfigError):
        AuthService(Store(settings.db_path, clock), settings, clock, gate=HashGate(8, 5)).init()
    assert settings.db_path.exists()
    assert Store(settings.db_path, clock).get_user("alice") is None
