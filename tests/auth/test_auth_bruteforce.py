"""Brute-force protection of AuthService.log_in (app/auth/service.py), amendments B1 and B2 of the approved design.

Expected values come from the approved design (scratchpad auth_design_approved.md and the Phase 2 plan), not from running the code:
  * trusted-proxy mode: failures counted per (user name, client address); 3 free failures, then a refusal for min(60, 2 * 2**(failures - 4)) seconds
    (2, 4, 8, 16, 32, 60, 60 ...); window 900 s; success clears; no scrypt during a refusal.
  * untrusted mode (the default): NEVER refuse because of earlier failures, always check the password, a correct password always logs in at once, only a FAILED
    answer carries a delay (default cap 3 s, never above 10 s), failures are counted per user name only, no address is stored (there is NO per-client throttle).
  * B2: the failure key is an HMAC (a random key per database) of the name and the address: no user name or address is in any database byte.
  * a full hashing gate gives busy and records nothing.
Nothing here sleeps: waiting is the FakeClock. The service returns delays as numbers. Tests named pinned_ record behaviour the design does not state.
Threads are joined with a limit so a hang fails the test. This file has no non-ASCII characters on purpose: other characters are built with chr().
"""
import ast
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from app.auth import passwords
from app.auth.config import AuthSettings
from app.auth.service import LoginResult, delay_for

PW = "Sentinel-Passw0rd-9731!"
WRONG = "wrong-Passw0rd-1234"
IP = "10.1.1.1"
OK_FAIL = LoginResult(False)
ROOT = Path(__file__).resolve().parents[2]
SERVICE_SOURCE = ROOT / "app" / "auth" / "service.py"


def trusted_env(make_service, **overrides):
    env = make_service(signup="open", trust_proxy=True, **overrides)
    env.add_local_user("alice", PW)
    return env


def untrusted_env(make_service, **overrides):
    env = make_service(signup="open", trust_proxy=False, **overrides)
    env.add_local_user("alice", PW)
    return env


def key(env, name="alice", ip=""):
    return env.store.failure_key(name, ip)


def row(env, name="alice", ip=""):
    return env.store.get_failure(key(env, name, ip))


def wrong(env, name="alice", ip=IP):
    return env.svc.log_in(name, WRONG, ip)


def right(env, name="alice", ip=IP):
    return env.svc.log_in(name, PW, ip)


def sessions(env):
    return env.sql("SELECT COUNT(*) FROM sessions")[0][0]


def failure_rows(env):
    return env.sql("SELECT COUNT(*) FROM login_failures")[0][0]


# --- delay_for: the pure function ----------------------------------------------------------------------------------------------------------------------

def test_the_trusted_schedule_for_1_to_12_failures_is_0_0_0_2_4_8_16_32_60_60_60_60():
    s = AuthSettings()
    assert [delay_for(s, f, True) for f in range(1, 13)] == [0, 0, 0, 2, 4, 8, 16, 32, 60, 60, 60, 60]


def test_zero_or_negative_failures_have_no_delay():
    s = AuthSettings()
    assert [delay_for(s, f, True) for f in (0, -1, -100)] == [0, 0, 0] and [delay_for(s, f, False) for f in (0, -5)] == [0, 0]


def test_the_untrusted_schedule_uses_the_same_steps_but_stops_at_the_small_cap_of_3_seconds():
    s = AuthSettings()
    assert s.delay_cap_untrusted_seconds == 3
    assert [delay_for(s, f, False) for f in range(1, 11)] == [0, 0, 0, 2, 3, 3, 3, 3, 3, 3]


@pytest.mark.parametrize("failures", [10**3, 10**6])
def test_a_huge_failure_count_does_not_overflow_and_stays_at_the_cap(failures):
    s = AuthSettings()
    assert delay_for(s, failures, True) == 60 and delay_for(s, failures, False) == 3


class Tripwire(int):
    """A delay_base_seconds that records the bit length of every number it is multiplied with."""
    seen = []

    def __mul__(self, other):
        Tripwire.seen.append(other.bit_length())
        return int(self) * other

    __rmul__ = __mul__


def test_delay_for_never_builds_a_big_power_of_two():
    """The bounded exponent, checked without a huge count: with 10**6 failures an unbounded exponent builds a number of a million bits (still fast), a bounded
    one stays small. The result is the same either way, so the size of the number is what the tripwire looks at."""
    Tripwire.seen.clear()
    s = AuthSettings(delay_base_seconds=Tripwire(2))
    for trusted in (True, False):
        delay_for(s, 10**6, trusted)
    assert Tripwire.seen, "the tripwire saw no multiplication: this test no longer checks anything"
    assert max(Tripwire.seen) <= 64, f"delay_for built a number of {max(Tripwire.seen)} bits"


CHILD = ("import sys; sys.path.insert(0, sys.argv[1]); from app.auth.config import AuthSettings; from app.auth.service import delay_for; "
         "s = AuthSettings(); n = int(sys.argv[2]); print(delay_for(s, n, True), delay_for(s, n, False))")


@pytest.mark.parametrize("failures", [10**9, 10**18, 10**100])
def test_a_huge_failure_count_returns_the_cap_in_a_child_process_within_the_time_limit(failures):
    """Each huge count in its own child process with a hard timeout. A thread cannot do this job: an unbounded 2**(10**18) is one long C call that holds the
    GIL, so a join limit in the same process never fires (measured: the whole process just stops). A child process is killed when the limit is over."""
    try:
        done = subprocess.run([sys.executable, "-B", "-c", CHILD, str(ROOT), str(failures)], capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:
        pytest.fail(f"delay_for({failures}) did not return within 5 s: the exponent is not bounded")
    assert done.returncode == 0, done.stderr[-300:]
    assert done.stdout.split() == ["60", "3"]


CUSTOM = [  # (settings, trusted, expected delays for failures 1..8)
    (dict(delay_free=0), True, [2, 4, 8, 16, 32, 60, 60, 60]),
    (dict(delay_free=1, delay_base_seconds=5, delay_cap_seconds=100), True, [0, 5, 10, 20, 40, 80, 100, 100]),
    (dict(delay_free=5, delay_base_seconds=1, delay_cap_seconds=3), True, [0, 0, 0, 0, 0, 1, 2, 3]),
    (dict(delay_cap_untrusted_seconds=10), False, [0, 0, 0, 2, 4, 8, 10, 10]),
    (dict(delay_cap_untrusted_seconds=0), False, [0] * 8),
    (dict(delay_cap_untrusted_seconds=1), False, [0, 0, 0, 1, 1, 1, 1, 1]),
    (dict(delay_cap_seconds=0), True, [0] * 8),
]


@pytest.mark.parametrize("changes, trusted, expected", CUSTOM, ids=[f"{i}" for i in range(len(CUSTOM))])
def test_the_schedule_follows_free_base_and_cap_from_the_settings(changes, trusted, expected):
    s = AuthSettings(**changes)
    assert [delay_for(s, f, trusted) for f in range(1, 9)] == expected


def test_the_untrusted_cap_is_clamped_at_10_seconds_by_the_configuration():
    assert AuthSettings(delay_cap_untrusted_seconds=10).delay_cap_untrusted_seconds == 10
    with pytest.raises(Exception):
        AuthSettings(delay_cap_untrusted_seconds=11)
    s = AuthSettings(delay_cap_untrusted_seconds=10)
    assert max(delay_for(s, f, False) for f in range(0, 2000)) == 10


def test_an_untrusted_delay_never_exceeds_its_cap_and_a_trusted_one_never_its_own():
    s = AuthSettings()
    assert max(delay_for(s, f, False) for f in range(0, 3000)) == 3 and max(delay_for(s, f, True) for f in range(0, 3000)) == 60


# --- trusted mode ---------------------------------------------------------------------------------------------------------------------------------------

def test_trusted_the_first_three_failures_are_free_and_answered_normally(make_service):
    env = trusted_env(make_service)
    for count in (1, 2, 3):
        assert wrong(env) == OK_FAIL
        assert row(env, "alice", IP).failures == count and row(env, "alice", IP).blocked_until == int(env.clock())
    assert right(env).ok  # nothing was blocked


def test_trusted_the_fourth_failure_is_answered_normally_and_blocks_the_next_attempt_for_two_seconds(make_service, clock):
    env = trusted_env(make_service)
    for _ in range(3):
        wrong(env)
    assert wrong(env) == OK_FAIL
    assert row(env, "alice", IP).blocked_until == int(clock()) + 2
    assert wrong(env) == LoginResult(False, retry_after=2)


def test_trusted_retry_after_counts_down_with_the_clock_and_the_block_ends_exactly_at_blocked_until(make_service, clock):
    env = trusted_env(make_service)
    for _ in range(4):
        wrong(env)
    assert wrong(env).retry_after == 2
    clock.advance(1)
    assert wrong(env).retry_after == 1
    clock.advance(1)  # now == blocked_until: no longer blocked
    result = wrong(env)
    assert result == OK_FAIL and row(env, "alice", IP).failures == 5


def test_trusted_a_correct_password_during_a_block_is_refused_with_zero_scrypt_work_and_no_session(make_service, derive_counter):
    env = trusted_env(make_service)
    for _ in range(4):
        wrong(env)
    derive_counter.reset()
    refused = right(env)
    assert refused == LoginResult(False, retry_after=2)
    assert derive_counter.count == 0 and sessions(env) == 0 and refused.session_token is None


def test_trusted_after_the_block_the_correct_password_logs_in_and_clears_the_row(make_service, clock, derive_counter):
    env = trusted_env(make_service)
    for _ in range(4):
        wrong(env)
    clock.advance(2)
    derive_counter.reset()
    result = right(env)
    assert result.ok and result.delay == 0 and result.retry_after is None and derive_counter.count == 1
    assert row(env, "alice", IP) is None
    assert [wrong(env) for _ in range(3)] == [OK_FAIL] * 3  # free again after the success


def test_trusted_the_block_grows_2_4_8_16_32_60_60_seconds(make_service, clock):
    env = trusted_env(make_service)
    for _ in range(3):
        wrong(env)
    retries = []
    for _ in range(7):
        assert wrong(env) == OK_FAIL  # failure number 4, 5, ... 10
        retry = wrong(env).retry_after
        retries.append(retry)
        clock.advance(retry)
    assert retries == [2, 4, 8, 16, 32, 60, 60]


def test_pinned_trusted_a_refused_attempt_is_not_a_failure_and_does_not_extend_the_block(make_service, clock):
    """Unwritten behaviour, not a deviation (stated here as the owner asked): a refused attempt returns before anything is recorded, so repeated tries during
    a block neither raise the failure count nor push blocked_until further away. The block ends when its own time has passed."""
    env = trusted_env(make_service)
    for _ in range(4):
        wrong(env)
    before = row(env, "alice", IP)
    for _ in range(10):
        assert wrong(env).retry_after == 2 and right(env).retry_after == 2
    assert row(env, "alice", IP) == before and before.failures == 4
    clock.advance(1)
    assert wrong(env).retry_after == 1 and row(env, "alice", IP) == before
    clock.advance(1)
    assert right(env).ok


def test_trusted_the_window_keeps_a_failure_that_is_exactly_900_seconds_old(make_service, clock):
    env = trusted_env(make_service)
    for _ in range(3):
        wrong(env)
    clock.advance(900)
    assert wrong(env) == OK_FAIL
    assert row(env, "alice", IP).failures == 4
    assert wrong(env).retry_after == 2


def test_trusted_a_failure_901_seconds_after_the_last_starts_again_at_one_and_nothing_is_blocked(make_service, clock):
    env = trusted_env(make_service)
    for _ in range(3):
        wrong(env)
    clock.advance(901)
    assert wrong(env) == OK_FAIL
    assert row(env, "alice", IP).failures == 1 and row(env, "alice", IP).blocked_until == int(clock())
    assert right(env).ok


def test_trusted_a_wrong_password_after_a_long_block_restarts_the_count(make_service, clock):
    env = trusted_env(make_service)
    for _ in range(4):
        wrong(env)
    clock.advance(2 + 901)
    assert wrong(env) == OK_FAIL and row(env, "alice", IP).failures == 1


def test_trusted_another_address_another_user_and_an_unknown_name_are_not_blocked(make_service):
    env = trusted_env(make_service)
    env.add_local_user("carol", PW)
    for _ in range(6):
        wrong(env, "alice", "6.6.6.6")
    assert wrong(env, "alice", "6.6.6.6").retry_after is not None
    assert right(env, "alice", "8.8.8.8").ok          # the same user from another address
    assert right(env, "carol", "6.6.6.6").ok          # another user from the blocked address
    assert wrong(env, "nobody", "6.6.6.6") == OK_FAIL  # an unknown name has its own count


def test_trusted_a_success_clears_only_its_own_key(make_service):
    env = trusted_env(make_service)
    env.add_local_user("carol", PW)
    wrong(env, "alice", "1.1.1.1"); wrong(env, "alice", "2.2.2.2"); wrong(env, "carol", "1.1.1.1")
    assert right(env, "alice", "1.1.1.1").ok
    assert row(env, "alice", "1.1.1.1") is None
    assert row(env, "alice", "2.2.2.2").failures == 1 and row(env, "carol", "1.1.1.1").failures == 1


def test_trusted_the_failure_count_is_per_name_and_address(make_service):
    env = trusted_env(make_service)
    wrong(env, "alice", "1.1.1.1"); wrong(env, "alice", "1.1.1.1"); wrong(env, "alice", "2.2.2.2")
    assert row(env, "alice", "1.1.1.1").failures == 2 and row(env, "alice", "2.2.2.2").failures == 1 and failure_rows(env) == 2


ODD_IPS = ["", None, 5, 5.5, b"1.2.3.4", object(), "2001:db8::1", " 1.2.3.4 ", "caf" + chr(233), "x" * 10_000, "\x00", "a b"]


@pytest.mark.parametrize("ip", ODD_IPS, ids=[f"ip{i}" for i in range(len(ODD_IPS))])
def test_trusted_an_odd_address_is_handled_without_an_exception_and_still_counts(make_service, ip):
    env = trusted_env(make_service)
    assert wrong(env, "alice", ip) == OK_FAIL
    assert failure_rows(env) == 1
    assert right(env, "alice", ip).ok


def test_pinned_trusted_an_address_that_is_not_text_counts_like_the_empty_address(make_service):
    """Unwritten behaviour, not a deviation: only a text address is used in the key; None, numbers and bytes all fall back to the empty address."""
    env = trusted_env(make_service)
    wrong(env, "alice", None)
    wrong(env, "alice", 5)
    wrong(env, "alice", "")  # three failures in all: still inside the free ones, so none of them is refused
    assert row(env, "alice", "").failures == 3 and failure_rows(env) == 1
    env2 = trusted_env(make_service)
    for odd in (b"1.2.3.4", 5.5):
        wrong(env2, "alice", odd)
    wrong(env2, "alice", "")
    assert row(env2, "alice", "").failures == 3 and failure_rows(env2) == 1


def test_pinned_trusted_only_the_first_64_characters_of_an_address_are_used(make_service):
    """Unwritten behaviour, not a deviation: two addresses that differ only after character 64 share one count."""
    env = trusted_env(make_service)
    wrong(env, "alice", "a" * 64 + "1"); wrong(env, "alice", "a" * 64 + "2")
    assert row(env, "alice", "a" * 64).failures == 2 and failure_rows(env) == 1
    wrong(env, "alice", "b" + "a" * 64)  # differs inside the first 64
    assert failure_rows(env) == 2


def test_trusted_a_lone_surrogate_in_the_address_does_not_crash_the_login(make_service):
    env = trusted_env(make_service)
    assert wrong(env, "alice", "10.0." + chr(0xD800)) == OK_FAIL
    assert failure_rows(env) == 1


def test_pinned_trusted_a_refusal_wins_over_a_full_hashing_gate(make_service):
    """Unwritten behaviour, not a deviation: the refusal is decided before the gate is asked, so a blocked key says retry_after even when the gate is full."""
    gate = passwords.HashGate(0, 5)
    env = trusted_env(make_service, gate=gate)
    for _ in range(4):
        wrong(env)
    gate.acquire()
    try:
        assert right(env) == LoginResult(False, retry_after=2)
    finally:
        gate.release()


# --- untrusted mode (the default) ---------------------------------------------------------------------------------------------------------------------

def test_untrusted_is_the_default_mode():
    assert AuthSettings().trust_proxy is False


def test_untrusted_sixty_failures_are_never_refused(make_service):
    env = untrusted_env(make_service)
    results = [wrong(env) for _ in range(60)]
    assert all(r.ok is False and r.retry_after is None and r.busy is False and r.session_token is None for r in results)
    assert row(env).failures == 60


def test_pinned_untrusted_the_delays_of_the_failed_answers_are_0_0_0_2_then_the_cap_of_3(make_service):
    """The design fixes the cap (3 s by default); the exact early steps are the shared schedule: three free failures, then base 2."""
    env = untrusted_env(make_service)
    assert [wrong(env).delay for _ in range(10)] == [0, 0, 0, 2, 3, 3, 3, 3, 3, 3]


def test_untrusted_a_correct_password_always_logs_in_at_once_with_exactly_one_scrypt_and_no_delay(make_service, derive_counter):
    env = untrusted_env(make_service)
    for _ in range(60):
        wrong(env)
    derive_counter.reset()
    result = right(env)
    assert result.ok and result.delay == 0 and result.retry_after is None and result.busy is False and derive_counter.count == 1
    assert sessions(env) == 1 and row(env) is None  # the success cleared the count


def test_untrusted_a_correct_password_logs_in_between_failures_whatever_came_before(make_service):
    env = untrusted_env(make_service)
    for _ in range(7):
        wrong(env)
        result = right(env)
        assert result.ok and result.delay == 0
        wrong(env), wrong(env)


def test_untrusted_only_failed_answers_carry_a_delay(make_service):
    env = untrusted_env(make_service)
    for _ in range(8):
        wrong(env)
    assert wrong(env).delay == 3 and right(env).delay == 0
    assert all(r.delay == 0 for r in (right(env), right(env, "alice", "5.5.5.5")))


def test_untrusted_the_count_is_per_user_name_only_whatever_the_address(make_service):
    env = untrusted_env(make_service)
    for n in range(5):
        assert wrong(env, "alice", f"10.0.0.{n}") != None  # noqa: E711
    assert failure_rows(env) == 1 and row(env).failures == 5


def test_untrusted_an_attackers_failures_never_delay_another_user_or_block_the_owner(make_service):
    env = untrusted_env(make_service)
    env.add_local_user("carol", PW)
    for _ in range(60):
        wrong(env, "alice", "6.6.6.6")
    first_fail_of_carol = wrong(env, "carol", "7.7.7.7")
    assert first_fail_of_carol == OK_FAIL  # delay 0: carol's own count starts at 1
    assert right(env, "carol", "7.7.7.7").ok
    assert right(env, "alice", "9.9.9.9").ok  # and the owner of the attacked name logs in at once


def test_pinned_untrusted_the_failed_answers_of_a_name_that_is_under_attack_are_slower_for_everybody(make_service):
    """Unwritten behaviour, not a deviation (the documented limit of the default mode): with no per-client throttle the failures of one name are counted for
    everybody, so a wrong password for that name gets the capped delay whoever sends it. A correct password is never delayed."""
    env = untrusted_env(make_service)
    for _ in range(10):
        wrong(env, "alice", "6.6.6.6")
    assert wrong(env, "alice", "9.9.9.9").delay == 3
    assert right(env, "alice", "9.9.9.9").delay == 0


def test_untrusted_no_address_is_stored(make_service):
    env = untrusted_env(make_service)
    for ip in ("203.0.113.77", "203.0.113.78", "2001:db8::77"):
        wrong(env, "attacker-name-xyz", ip)
        wrong(env, "alice", ip)
    data = env.db_bytes()
    assert b"203.0.113" not in data and b"2001:db8" not in data
    assert failure_rows(env) == 2


# --- B2: the key is an HMAC ---------------------------------------------------------------------------------------------------------------------------

def test_the_failure_table_key_is_64_hex_characters_of_the_stores_hmac(make_service):
    env = trusted_env(make_service)
    wrong(env, "attacker-name-xyz", "203.0.113.77")
    keys = [r[0] for r in env.sql("SELECT key FROM login_failures")]
    assert keys == [env.store.failure_key("attacker-name-xyz", "203.0.113.77")]
    assert re.fullmatch(r"[0-9a-f]{64}", keys[0])


def test_untrusted_the_key_is_the_hmac_of_the_name_and_the_empty_address(make_service):
    env = untrusted_env(make_service)
    wrong(env, "attacker-name-xyz", "203.0.113.77")
    assert [r[0] for r in env.sql("SELECT key FROM login_failures")] == [env.store.failure_key("attacker-name-xyz", "")]


@pytest.mark.parametrize("trusted", [True, False])
def test_no_user_name_or_address_of_a_failed_login_is_in_any_database_byte(make_service, trusted):
    env = make_service(signup="open", trust_proxy=trusted)
    env.add_local_user("alice", PW)
    wrong(env, "attacker-name-xyz", "203.0.113.77")
    env.svc.log_in("secret marker with spaces", WRONG, "203.0.113.78")   # a malformed name: its key comes from a hash of the raw text
    env.svc.log_in("alice", WRONG, "203.0.113.79")
    data = env.db_bytes()
    assert b"attacker-name-xyz" not in data and b"203.0.113" not in data and b"secret marker" not in data
    assert b"alice" in data and env.store.failure_key("alice", "203.0.113.79" if trusted else "").encode() in data  # positive controls
    assert failure_rows(env) == 3


def test_the_key_of_the_same_name_and_address_differs_per_database(make_service):
    one, two = trusted_env(make_service), trusted_env(make_service)
    assert key(one, "alice", IP) != key(two, "alice", IP)
    wrong(one), wrong(two)
    assert one.sql("SELECT key FROM login_failures") != two.sql("SELECT key FROM login_failures")


def test_the_key_is_stable_after_a_restart(make_service):
    env = trusted_env(make_service)
    wrong(env)
    again = env.reopen()
    assert key(again, "alice", IP) == key(env, "alice", IP) and again.store.get_failure(key(env, "alice", IP)).failures == 1
    assert wrong(again).ok is False and again.store.get_failure(key(env, "alice", IP)).failures == 2


# --- busy ------------------------------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("trusted", [True, False])
@pytest.mark.parametrize("name, password", [("alice", PW), ("alice", WRONG), ("nobody", PW)], ids=["known-right", "known-wrong", "unknown"])
def test_a_full_hashing_gate_gives_busy_with_no_scrypt_no_session_and_no_failure_recorded(make_service, derive_counter, trusted, name, password):
    gate = passwords.HashGate(0, 5)
    env = make_service(signup="open", trust_proxy=trusted, gate=gate)
    env.add_local_user("alice", PW)
    gate.acquire()
    try:
        derive_counter.reset()
        assert env.svc.log_in(name, password, IP) == LoginResult(False, busy=True)
        assert derive_counter.count == 0 and sessions(env) == 0 and failure_rows(env) == 0
    finally:
        gate.release()
    assert right(env).ok


def test_busy_does_not_count_towards_a_block(make_service):
    gate = passwords.HashGate(0, 5)
    env = trusted_env(make_service, gate=gate)
    gate.acquire()
    try:
        for _ in range(10):
            assert env.svc.log_in("alice", WRONG, IP).busy
    finally:
        gate.release()
    assert wrong(env) == OK_FAIL and row(env, "alice", IP).failures == 1


# --- concurrency -----------------------------------------------------------------------------------------------------------------------------------------

def run_threads(work, count=8, limit=60):
    barrier = threading.Barrier(count, timeout=30)
    results, errors = [], []
    lock = threading.Lock()

    def target(i):
        try:
            barrier.wait()
            out = work(i)
            with lock:
                results.extend(out)
        except BaseException as error:  # noqa: BLE001
            with lock:
                errors.append(error)

    threads = [threading.Thread(target=target, args=(i,), daemon=True) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(limit)
    assert not any(t.is_alive() for t in threads), "a thread is still running: a hang, not a result"
    assert errors == [], errors
    return results


def test_untrusted_eight_threads_of_wrong_passwords_leave_one_consistent_row(make_service):
    env = untrusted_env(make_service)
    results = run_threads(lambda i: [env.svc.log_in("alice", WRONG, f"10.0.0.{i}") for _ in range(5)])
    assert len(results) == 40 and all(r.ok is False and r.retry_after is None and 0 <= r.delay <= 3 for r in results)
    assert failure_rows(env) == 1 and row(env).failures == 40 and row(env).last_failure_at == int(env.clock())


def test_trusted_eight_threads_of_wrong_passwords_leave_a_row_that_matches_its_own_block(make_service):
    env = trusted_env(make_service)
    results = run_threads(lambda i: [env.svc.log_in("alice", WRONG, IP) for _ in range(5)])
    final = row(env, "alice", IP)
    processed = [r for r in results if r.retry_after is None]
    assert len(results) == 40 and failure_rows(env) == 1
    assert 4 <= final.failures <= 40 and final.failures == len(processed), (final, len(processed))
    assert final.blocked_until == final.last_failure_at + delay_for(env.settings, final.failures, True)
    assert all(r.retry_after in (None, 2, 4, 8, 16, 32, 60) for r in results)


def test_eight_threads_logging_in_correctly_all_succeed_and_clear_the_row(make_service):
    env = untrusted_env(make_service)
    for _ in range(5):
        wrong(env)
    results = run_threads(lambda i: [right(env, "alice", f"10.0.0.{i}")])
    assert all(r.ok for r in results) and len({r.session_token for r in results}) == 8 and row(env) is None


# --- no sleeping ---------------------------------------------------------------------------------------------------------------------------------------

def test_the_service_never_sleeps_it_returns_delays_as_numbers():
    tree = ast.parse(SERVICE_SOURCE.read_text(encoding="utf-8"))
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "sleep" not in names and "wait" not in names


def test_a_failed_answer_carries_its_delay_as_an_int_and_the_clock_is_not_moved_by_the_service(make_service, clock):
    env = untrusted_env(make_service)
    start = clock()
    results = [wrong(env) for _ in range(6)]
    assert all(isinstance(r.delay, int) for r in results) and clock() == start
