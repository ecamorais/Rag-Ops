"""AuthService.log_in (success path), validate_session, verify_csrf, log_out, log_out_everywhere, rehash and purge (app/auth/service.py).

Expected values come from the approved design (scratchpad auth_design_approved.md and the Phase 2 plan), not from running the code: opaque random session
tokens of which only the SHA-256 is stored, idle timeout (a session is dead when last_seen + idle <= now), absolute timeout (dead when now >= expires_at),
"last_seen is written at most every touch interval", "at most 10 sessions per user, the newest are kept", logout and logout-everywhere, the CSRF token compared
with hmac.compare_digest, "rehash when the stored cost is weaker than the configured one, never for a provisioned user", and the failure window of 900 s.
Every boundary is tested on both sides. All time comes from the FakeClock. Tests named pinned_ record behaviour the design does not state.
This file has no non-ASCII characters on purpose: other characters are built with chr().
"""
import dataclasses
import re
import sqlite3
from contextlib import closing

import pytest

from app.auth import passwords
from app.auth import service as service_module
from app.auth.config import AuthSettings
from app.auth.service import LoginResult, SessionInfo

PW = "Sentinel-Passw0rd-9731!"
WRONG = "wrong-Passw0rd-1234"
IDLE, ABSOLUTE = 600, 3600


def make(make_service, **overrides):
    env = make_service(signup="open", **overrides)
    env.add_local_user("alice", PW)
    return env


def login(env, name="alice", password=PW, ip=""):
    result = env.svc.log_in(name, password, ip)
    assert result.ok, result
    return result


def row_of(env, token):
    return env.store.get_session(env.digest(token))


def session_count(env):
    return env.sql("SELECT COUNT(*) FROM sessions")[0][0]


# --- what a login returns -------------------------------------------------------------------------------------------------------------------------------

def test_a_successful_login_returns_the_token_the_csrf_token_and_the_name(make_service):
    env = make_service(signup="open")
    env.add_local_user("alice", PW)
    result = env.svc.log_in(" Alice ", PW, "10.0.0.1")
    assert result.ok is True and result.username == "alice"
    assert result.delay == 0 and result.retry_after is None and result.busy is False
    assert result.session_token != result.csrf_token and result.session_token and result.csrf_token


def test_pinned_the_session_token_and_the_csrf_token_are_43_url_safe_characters(make_service):
    """Unwritten behaviour, not a deviation: the design says 32 random bytes; as URL-safe base64 that is 43 characters."""
    env = make_service(signup="open")
    env.add_local_user("alice", PW)
    result = login(env)
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", result.session_token) and re.fullmatch(r"[A-Za-z0-9_-]{43}", result.csrf_token)


def test_every_login_gets_a_new_token_and_a_new_csrf_token(make_service):
    env = make_service(signup="open")
    env.add_local_user("alice", PW)
    results = [login(env) for _ in range(5)]
    assert len({r.session_token for r in results}) == 5 and len({r.csrf_token for r in results}) == 5
    assert not {r.session_token for r in results} & {r.csrf_token for r in results}


def test_a_login_marks_the_last_login_time_and_creates_one_session_row(make_service, clock):
    env = make_service(signup="open")
    env.add_local_user("alice", PW)
    assert env.store.get_user("alice").last_login_at is None
    result = login(env)
    assert env.store.get_user("alice").last_login_at == int(clock())
    row = row_of(env, result.session_token)
    assert (row.created_at, row.last_seen_at, row.expires_at, row.csrf_token) == (int(clock()), int(clock()), int(clock()) + env.settings.session_absolute_seconds, result.csrf_token)


def test_tokens_come_from_the_token_factory_when_one_is_given(make_service):
    counter = iter(range(1, 100))
    env = make_service(signup="open", token_factory=lambda: str(next(counter)).zfill(43))
    env.add_local_user("alice", PW)
    result = login(env)
    assert (result.session_token, result.csrf_token) == ("0" * 42 + "1", "0" * 42 + "2")
    assert env.svc.validate_session("0" * 42 + "1").username == "alice"


def test_results_are_frozen_dataclasses(make_service):
    env = make_service(signup="open")
    env.add_local_user("alice", PW)
    result = login(env)
    info = env.svc.validate_session(result.session_token)
    assert isinstance(result, LoginResult) and isinstance(info, SessionInfo)
    for item, field in ((result, "ok"), (info, "username")):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(item, field, "x")
    assert info == SessionInfo(env.store.get_user("alice").id, "alice", result.csrf_token)


# --- only the hash of the token is stored ------------------------------------------------------------------------------------------------------------

def test_only_the_sha256_of_the_token_is_in_the_database_never_the_token_or_the_password(make_service):
    env = make_service(signup="open")
    env.add_local_user("alice", PW)
    result = login(env)
    stored = [r[0] for r in env.sql("SELECT token_hash FROM sessions")]
    assert stored == [env.digest(result.session_token)]
    data = env.db_bytes()
    assert result.session_token.encode() not in data and PW.encode() not in data
    assert env.digest(result.session_token).encode() in data and result.csrf_token.encode() in data  # positive controls (the CSRF token is stored by design)


# --- idle, absolute and touch boundaries --------------------------------------------------------------------------------------------------------------

def timed(make_service, touch):
    return make(make_service, session_idle_seconds=IDLE, session_absolute_seconds=ABSOLUTE, session_touch_seconds=touch)


def test_a_session_idle_for_599_seconds_is_alive_and_at_exactly_600_it_is_dead_and_deleted(make_service, clock):
    for seconds, alive in ((IDLE - 1, True), (IDLE, False)):
        env = timed(make_service, touch=3000)  # touch is so long that validation never rewrites last_seen here
        token = login(env).session_token
        clock.advance(seconds)
        assert (env.svc.validate_session(token) is not None) is alive, seconds
        assert (row_of(env, token) is not None) is alive


def test_activity_extends_idle_but_never_absolute(make_service, clock):
    env = timed(make_service, touch=0)
    token = login(env).session_token
    for _ in range(7):  # every 500 s, which is inside the idle limit of 600 s
        clock.advance(500)
        assert env.svc.validate_session(token) is not None
    assert clock() - 1_700_000_000 == 3500
    clock.advance(99)   # +3599
    assert env.svc.validate_session(token) is not None
    clock.advance(1)    # +3600: the absolute end, however recently it was used
    assert env.svc.validate_session(token) is None and row_of(env, token) is None


def test_activity_keeps_a_session_alive_past_the_idle_limit_since_creation_and_it_dies_idle_seconds_after_the_last_use(make_service, clock):
    env = timed(make_service, touch=0)
    token = login(env).session_token
    clock.advance(500)
    assert env.svc.validate_session(token) is not None
    clock.advance(500)  # +1000 since creation, 500 since the last use
    assert env.svc.validate_session(token) is not None
    clock.advance(599)
    assert env.svc.validate_session(token) is not None
    clock.advance(600)
    assert env.svc.validate_session(token) is None


def test_the_absolute_end_is_exactly_created_plus_absolute(make_service, clock):
    for seconds, alive in ((ABSOLUTE - 1, True), (ABSOLUTE, False)):
        env = make(make_service, session_idle_seconds=ABSOLUTE, session_absolute_seconds=ABSOLUTE, session_touch_seconds=0)
        token = login(env).session_token
        clock.advance(seconds)
        assert (env.svc.validate_session(token) is not None) is alive, seconds


def test_the_absolute_end_kills_a_session_whose_idle_limit_is_far_away(make_service, clock):
    """Separate from the test above. There idle == absolute and the session is never used, so at created + 3600 it is dead for TWO reasons and a flipped
    absolute comparison (>= to >) goes unnoticed. Here recent activity moves the idle end to +7000, so only the absolute end can kill the session at +3600."""
    env = make(make_service, session_idle_seconds=ABSOLUTE, session_absolute_seconds=ABSOLUTE)  # touch interval: the default 300 s
    token = login(env).session_token
    clock.advance(3400)  # 3400 s since the last write: this validation rewrites last_seen to +3400
    assert env.svc.validate_session(token) is not None
    clock.advance(199)   # +3599, 199 s after the last use: inside the touch window, so nothing is rewritten
    assert env.svc.validate_session(token) is not None and row_of(env, token) is not None
    clock.advance(1)     # +3600 = created + absolute; the idle end would be 3400 + 3600
    assert env.svc.validate_session(token) is None and row_of(env, token) is None


def test_pinned_last_seen_is_rewritten_only_after_300_seconds(make_service, clock):
    """Unwritten behaviour, not a deviation: the design says "at most every touch interval"; the default is 300 s."""
    env = make(make_service)
    assert env.settings.session_touch_seconds == 300
    token = login(env).session_token
    base = int(clock())
    clock.advance(299)
    assert env.svc.validate_session(token) is not None and row_of(env, token).last_seen_at == base
    clock.advance(1)
    assert env.svc.validate_session(token) is not None and row_of(env, token).last_seen_at == base + 300
    clock.advance(299)
    env.svc.validate_session(token)
    assert row_of(env, token).last_seen_at == base + 300
    clock.advance(1)
    env.svc.validate_session(token)
    assert row_of(env, token).last_seen_at == base + 600


def test_validation_never_changes_the_creation_or_expiry_time(make_service, clock):
    env = make(make_service)
    token = login(env).session_token
    before = row_of(env, token)
    clock.advance(400)
    env.svc.validate_session(token)
    after = row_of(env, token)
    assert (after.created_at, after.expires_at, after.csrf_token) == (before.created_at, before.expires_at, before.csrf_token)


def test_a_session_survives_a_new_service_and_store_on_the_same_file(make_service):
    env = make(make_service)
    token = login(env).session_token
    again = env.reopen()
    assert again.svc.validate_session(token).username == "alice"


# --- disabled users ----------------------------------------------------------------------------------------------------------------------------------

def test_a_disabled_users_session_is_dead_and_deleted_and_the_user_cannot_log_in(make_service):
    env = make(make_service)
    token = login(env).session_token
    env.sql("UPDATE users SET disabled = 1 WHERE username = ?", ("alice",))
    assert env.svc.validate_session(token) is None and row_of(env, token) is None
    assert env.svc.log_in("alice", PW, "") == LoginResult(False)


# --- the session cap ----------------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("cap", [10, 3])
def test_a_user_may_hold_exactly_the_capped_number_of_sessions_the_next_login_drops_the_oldest(make_service, cap):
    env = make(make_service, max_sessions_per_user=cap)
    tokens = [login(env).session_token for _ in range(cap)]
    assert all(env.svc.validate_session(t) is not None for t in tokens)
    tokens.append(login(env).session_token)
    alive = [env.svc.validate_session(t) is not None for t in tokens]
    assert alive == [False] + [True] * cap
    assert session_count(env) == cap


def test_the_cap_keeps_the_newest_by_creation_time_and_leaves_other_users_alone(make_service, clock):
    env = make(make_service, max_sessions_per_user=3)
    env.add_local_user("bob", PW)
    bobs = login(env, "bob").session_token
    tokens = []
    for _ in range(5):
        tokens.append(login(env).session_token)
        clock.advance(1)
    assert [env.svc.validate_session(t) is not None for t in tokens] == [False, False, True, True, True]
    assert env.svc.validate_session(bobs) is not None


# --- log_out and log_out_everywhere --------------------------------------------------------------------------------------------------------------

def test_log_out_deletes_one_session_true_then_false(make_service):
    env = make(make_service)
    a, b = login(env).session_token, login(env).session_token
    assert env.svc.log_out(a) is True
    assert env.svc.log_out(a) is False
    assert env.svc.validate_session(a) is None and env.svc.validate_session(b) is not None


@pytest.mark.parametrize("junk", [None, 5, "", "x" * 43, "x" * 1000, b"y" * 43, [], "A" * 42 + "="])
def test_log_out_with_garbage_or_an_unknown_token_is_false_and_deletes_nothing(make_service, junk):
    env = make(make_service)
    token = login(env).session_token
    assert env.svc.log_out(junk) is False
    assert env.svc.validate_session(token) is not None


def test_log_out_everywhere_deletes_all_of_that_users_sessions_and_none_of_anybody_elses(make_service):
    env = make(make_service)
    env.add_local_user("bob", PW)
    mine = [login(env).session_token for _ in range(4)]
    bobs = [login(env, "bob").session_token for _ in range(2)]
    assert env.svc.log_out_everywhere(mine[1]) == 4
    assert all(env.svc.validate_session(t) is None for t in mine)
    assert all(env.svc.validate_session(t) is not None for t in bobs)
    assert env.svc.log_out_everywhere(mine[1]) == 0  # the token is dead now


@pytest.mark.parametrize("junk", [None, 5, "", "x" * 43, "x" * 5000, b"y" * 43, {}, "A" * 44])
def test_log_out_everywhere_needs_a_live_token_and_otherwise_deletes_nothing(make_service, junk):
    env = make(make_service)
    token = login(env).session_token
    other = login(env).session_token
    assert env.svc.log_out_everywhere(junk) == 0
    assert env.svc.validate_session(token) is not None and env.svc.validate_session(other) is not None


def test_log_out_everywhere_with_an_expired_token_does_not_touch_the_users_other_sessions(make_service, clock):
    env = timed(make_service, touch=3000)
    old = login(env).session_token
    clock.advance(ABSOLUTE - 1)
    fresh = env.svc.log_in("alice", PW, "").session_token
    clock.advance(1)  # the first session is now past its absolute end; the second one is not
    assert env.svc.log_out_everywhere(old) == 0
    assert env.svc.validate_session(fresh) is not None


@pytest.mark.parametrize("kind", ["absolute", "idle", "disabled", "deleted"])
def test_log_out_everywhere_with_a_dead_token_returns_zero_and_deletes_no_other_session(make_service, clock, kind):
    """The token is dead but its ROW is still in the table: nothing validated it, logged in or purged since it died. (The two older tests never have such a row:
    one uses malformed or unknown tokens, in the other the later login purges the old session before log_out_everywhere runs.) Only the presented token's own
    row may go (validate_session cleans up the session it is given); every other session of the user and of bob must stay."""
    env = make(make_service, session_idle_seconds=IDLE, session_absolute_seconds=ABSOLUTE, session_touch_seconds=0)
    env.add_local_user("bob", PW)
    dead = login(env).session_token
    if kind == "absolute":
        for _ in range(7):  # use every 500 s: the idle end stays far away, only the absolute end can kill it
            clock.advance(500)
            assert env.svc.validate_session(dead) is not None
        siblings = [login(env).session_token, login(env).session_token]
        bob = login(env, "bob").session_token
        clock.advance(100)  # +3600 = created + absolute
    elif kind == "idle":
        clock.advance(500)
        siblings = [login(env).session_token, login(env).session_token]
        bob = login(env, "bob").session_token
        clock.advance(100)  # `dead` was last used 600 s ago, the siblings 100 s ago
    else:
        siblings = [login(env).session_token, login(env).session_token]
        bob = login(env, "bob").session_token
        if kind == "disabled":
            env.sql("UPDATE users SET disabled = 1 WHERE username = 'alice'")
        else:  # a plain connection has foreign keys off, so the user goes and its sessions stay behind
            with closing(sqlite3.connect(env.settings.db_path)) as conn:
                conn.execute("DELETE FROM users WHERE username = 'alice'")
                conn.commit()
    assert row_of(env, dead) is not None, "the dead session's row must still be there, or this case proves nothing"
    before = {r[0] for r in env.sql("SELECT token_hash FROM sessions")}
    assert env.svc.log_out_everywhere(dead) == 0
    after = {r[0] for r in env.sql("SELECT token_hash FROM sessions")}
    assert {env.digest(t) for t in siblings} | {env.digest(bob)} <= after
    assert before - after <= {env.digest(dead)}


# --- garbage tokens ------------------------------------------------------------------------------------------------------------------------------------

MALFORMED = [None, 5, 5.5, True, b"A" * 43, [], {}, object(), "", " " * 43, "\x00" * 43, "x" * 1_000_000, "A" * 42 + "=", "A" * 44, "A" * 42,
             "A" * 42 + chr(233), chr(0xD800) * 43]
WELL_FORMED_UNKNOWN = ["x" * 43, "A" * 43, "-" * 43]


@pytest.fixture
def get_session_spy(monkeypatch):
    class Spy:
        calls = 0

    real = None

    def install(env):
        nonlocal real
        real = env.store.get_session

        def counting(token_hash):
            Spy.calls += 1
            return real(token_hash)

        monkeypatch.setattr(env.store, "get_session", counting)
        return Spy

    return install


@pytest.mark.parametrize("token", MALFORMED, ids=[f"m{i}" for i in range(len(MALFORMED))])
def test_a_malformed_token_gives_none_and_never_reaches_the_database(make_service, get_session_spy, token):
    env = make(make_service)
    spy = get_session_spy(env)
    assert env.svc.validate_session(token) is None
    assert env.svc.verify_csrf(token, "x" * 43) is False
    assert spy.calls == 0


@pytest.mark.parametrize("token", WELL_FORMED_UNKNOWN)
def test_a_well_formed_unknown_token_gives_none_after_one_lookup(make_service, get_session_spy, token):
    env = make(make_service)
    spy = get_session_spy(env)
    assert env.svc.validate_session(token) is None
    assert spy.calls == 1


# --- verify_csrf --------------------------------------------------------------------------------------------------------------------------------------

def test_verify_csrf_is_true_for_the_sessions_own_token_and_false_for_everything_else(make_service):
    env = make(make_service)
    env.add_local_user("bob", PW)
    a, b = login(env), login(env, "bob")
    info = env.svc.validate_session(a.session_token)
    assert env.svc.verify_csrf(info, a.csrf_token) is True
    assert env.svc.verify_csrf(a.session_token, a.csrf_token) is True
    for wrong in (b.csrf_token, a.csrf_token + "x", a.csrf_token[:-1], a.csrf_token.swapcase(), a.session_token):
        assert env.svc.verify_csrf(info, wrong) is False and env.svc.verify_csrf(a.session_token, wrong) is False


def test_verify_csrf_compares_with_hmac_compare_digest_on_bytes(make_service, monkeypatch):
    env = make(make_service)
    result = login(env)
    info = env.svc.validate_session(result.session_token)
    seen = []
    real = service_module.hmac.compare_digest

    def spy(a, b):
        seen.append((type(a), type(b), len(a), len(b)))
        return real(a, b)

    monkeypatch.setattr(service_module.hmac, "compare_digest", spy)
    assert env.svc.verify_csrf(info, result.csrf_token) is True and env.svc.verify_csrf(info, result.csrf_token[::-1]) is False
    assert seen == [(bytes, bytes, 43, 43), (bytes, bytes, 43, 43)]


JUNK_SUPPLIED = [None, 5, b"x" * 43, "", "x" * 5000, "caf" + chr(233) + "x" * 40, "\x00" * 43, chr(0xD800) * 43, [], object()]


@pytest.mark.parametrize("supplied", JUNK_SUPPLIED, ids=[f"s{i}" for i in range(len(JUNK_SUPPLIED))])
def test_verify_csrf_is_false_for_a_missing_non_text_non_ascii_empty_or_huge_value(make_service, supplied):
    env = make(make_service)
    result = login(env)
    info = env.svc.validate_session(result.session_token)
    assert env.svc.verify_csrf(info, supplied) is False and env.svc.verify_csrf(result.session_token, supplied) is False


def test_verify_csrf_is_false_without_a_live_session(make_service):
    env = make(make_service)
    result = login(env)
    env.svc.log_out(result.session_token)
    assert env.svc.verify_csrf(result.session_token, result.csrf_token) is False
    assert env.svc.verify_csrf(None, result.csrf_token) is False and env.svc.verify_csrf("junk", result.csrf_token) is False


# --- rehash --------------------------------------------------------------------------------------------------------------------------------------------

def weak_hash(password=PW, n=2**10, r=8, p=1):
    return passwords.hash_password(password, AuthSettings(scrypt_n=n, scrypt_r=r, scrypt_p=p))


def test_a_weak_stored_hash_is_upgraded_at_login_and_the_next_login_does_no_rehash(make_service, derive_counter):
    env = make_service(signup="open", scrypt_n=2**11)
    old = weak_hash()
    env.store.add_user("old", old)
    derive_counter.reset()
    assert env.svc.log_in("old", PW, "").ok
    assert [c[:3] for c in derive_counter.calls] == [(1024, 8, 1), (2048, 8, 1)]  # verify with the stored cost, then hash with the configured one
    upgraded = env.store.get_user("old").pw_hash
    assert upgraded != old and upgraded.startswith("scrypt$2048$8$1$") and passwords.verify_password(PW, upgraded)
    derive_counter.reset()
    assert env.svc.log_in("old", PW, "").ok
    assert derive_counter.count == 1 and env.store.get_user("old").pw_hash == upgraded


def test_a_hash_with_a_weaker_r_or_p_is_upgraded_too(make_service):
    env = make_service(signup="open", scrypt_n=2**10, scrypt_r=8, scrypt_p=2)
    env.store.add_user("weak-p", weak_hash(r=8, p=1))
    env.store.add_user("weak-r", weak_hash(r=4, p=2))
    for name in ("weak-p", "weak-r"):
        assert env.svc.log_in(name, PW, "").ok
        assert env.store.get_user(name).pw_hash.startswith("scrypt$1024$8$2$")


def test_a_hash_that_is_equal_or_stronger_is_left_alone(make_service, derive_counter):
    env = make_service(signup="open", scrypt_n=2**10)
    strong = weak_hash(n=2**11)
    env.store.add_user("strong", strong)
    env.add_local_user("same", PW)
    same = env.store.get_user("same").pw_hash
    derive_counter.reset()
    assert env.svc.log_in("strong", PW, "").ok and env.svc.log_in("same", PW, "").ok
    assert derive_counter.count == 2
    assert env.store.get_user("strong").pw_hash == strong and env.store.get_user("same").pw_hash == same


def test_a_wrong_password_never_rewrites_a_hash(make_service):
    env = make_service(signup="open", scrypt_n=2**11)
    old = weak_hash()
    env.store.add_user("old", old)
    assert env.svc.log_in("old", WRONG, "") == LoginResult(False)
    assert env.store.get_user("old").pw_hash == old


def test_a_provisioned_hash_is_never_rewritten_even_when_it_is_weaker_than_configured(make_service, derive_counter):
    text = weak_hash()
    env = make_service(signup="open", scrypt_n=2**11, users_text=f"prov.user:{text}")
    derive_counter.reset()
    assert env.svc.log_in("prov.user", PW, "").ok
    assert derive_counter.count == 1
    assert env.store.get_user("prov.user").pw_hash == text and env.store.get_user("prov.user").source == "provisioned"


def test_a_full_gate_during_the_rehash_does_not_fail_the_login(make_service, monkeypatch):
    env = make_service(signup="open", scrypt_n=2**11)
    old = weak_hash()
    env.store.add_user("old", old)
    real = passwords.hash_password

    def busy(*args, **kwargs):
        raise passwords.Busy("queue_full")

    monkeypatch.setattr(service_module.passwords, "hash_password", busy)
    assert env.svc.log_in("old", PW, "").ok
    monkeypatch.setattr(service_module.passwords, "hash_password", real)
    assert env.store.get_user("old").pw_hash == old


# --- purge ---------------------------------------------------------------------------------------------------------------------------------------------

def test_purge_deletes_a_session_idle_for_exactly_the_idle_limit_and_not_one_second_earlier(make_service, clock):
    env = timed(make_service, touch=3000)
    login(env)
    clock.advance(IDLE - 1)
    assert env.svc.purge() == (0, 0)
    clock.advance(1)
    assert env.svc.purge() == (1, 0)
    assert env.svc.purge() == (0, 0) and session_count(env) == 0


def test_purge_deletes_a_session_at_exactly_its_absolute_end(make_service, clock):
    env = make(make_service, session_idle_seconds=ABSOLUTE, session_absolute_seconds=ABSOLUTE, session_touch_seconds=0)
    token = login(env).session_token
    clock.advance(ABSOLUTE - 1)
    env.svc.validate_session(token)  # touches last_seen, so only the absolute end can delete it
    assert env.svc.purge() == (0, 0)
    clock.advance(1)
    assert env.svc.purge() == (1, 0)


def test_purge_keeps_a_failure_row_900_seconds_old_and_deletes_it_at_901(make_service, clock):
    env = make(make_service)
    env.svc.log_in("nobody", WRONG, "")
    assert env.sql("SELECT COUNT(*) FROM login_failures")[0][0] == 1
    clock.advance(900)
    assert env.svc.purge() == (0, 0)
    clock.advance(1)
    assert env.svc.purge() == (0, 1)
    assert env.sql("SELECT COUNT(*) FROM login_failures")[0][0] == 0


def test_a_successful_login_purges_so_dead_sessions_do_not_pile_up(make_service, clock):
    env = timed(make_service, touch=3000)
    for _ in range(3):
        login(env)
    clock.advance(IDLE)  # all three are idle now
    login(env)
    assert session_count(env) == 1


def test_purge_is_safe_to_call_often_and_on_an_empty_database(make_service):
    env = make(make_service)
    assert [env.svc.purge() for _ in range(5)] == [(0, 0)] * 5
