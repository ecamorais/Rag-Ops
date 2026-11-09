"""app/auth/config.py: the settings of the optional login.

Expected values come from the approved design (scratchpad auth_design_approved.md and the Phase 2 plan), not from running the code:
the defaults, the AUTH_* variable names, the rule that nothing is read at import, "fail closed on a wrong value", "the message names the setting and never
a value", the N floor of 2**14 for the environment only, "sign-up is closed whenever users are provisioned" and "AUTH_USERS is a secret".
The ranges of the integer settings are the ones stated in the approved step 1. A message for one wrong value names the variable (AUTH_...); a message
for a relation between two settings (idle longer than absolute) names the fields.
"""
import dataclasses
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from app.auth.config import AuthConfigError, AuthSettings, auth_enabled, cookie_secure

ROOT = Path(__file__).resolve().parents[2]
JUNK = "bogus-91827364550"  # a value no message may echo
BELOW, ABOVE = "-91827364550", "91827364550"  # outside every range


# --- defaults --------------------------------------------------------------------------------------------------------------------------

def test_the_defaults_are_the_designed_ones():
    s = AuthSettings()
    assert s.enabled is False
    assert s.signup == "invite" and s.cookie_secure == "auto" and s.trust_proxy is False
    assert (s.scrypt_n, s.scrypt_r, s.scrypt_p) == (2**15, 8, 1)
    assert s.session_idle_seconds == 12 * 3600 and s.session_absolute_seconds == 7 * 24 * 3600 and s.session_touch_seconds == 300
    assert s.max_sessions_per_user == 10
    assert (s.delay_free, s.delay_base_seconds, s.delay_cap_seconds, s.delay_window_seconds) == (3, 2, 60, 900)
    assert s.delay_cap_untrusted_seconds == 3 and s.hash_queue_max == 8 and s.hash_timeout_seconds == 10
    assert s.users_text == "" and s.provisioned is False and s.effective_signup == "invite"


def test_the_default_database_is_var_auth_db_under_the_project_root():
    s = AuthSettings.from_env({})
    assert s.db_path == ROOT / "var" / "auth.db"
    assert s.db_path.is_absolute()


def test_from_env_with_nothing_set_gives_the_defaults():
    assert dataclasses.replace(AuthSettings.from_env({}), db_path=Path("var/auth.db")) == AuthSettings()


# --- from_env: every variable --------------------------------------------------------------------------------------------------------------

FLAGS = [("AUTH_ENABLED", "enabled"), ("AUTH_TRUST_PROXY", "trust_proxy")]


@pytest.mark.parametrize("name, field", FLAGS)
@pytest.mark.parametrize("text", ["1", "true", "TRUE", " Yes ", "on"])
def test_a_flag_accepts_the_true_words(name, field, text):
    assert getattr(AuthSettings.from_env({name: text}), field) is True


@pytest.mark.parametrize("name, field", FLAGS)
@pytest.mark.parametrize("text", ["0", "false", "FALSE", " No ", "off"])
def test_a_flag_accepts_the_false_words(name, field, text):
    assert getattr(AuthSettings.from_env({name: text}), field) is False


@pytest.mark.parametrize("name, field", FLAGS)
@pytest.mark.parametrize("text", ["", "   "])
def test_a_blank_flag_means_the_default(name, field, text):
    assert getattr(AuthSettings.from_env({name: text}), field) is False


CHOICES = [("AUTH_SIGNUP", "signup", "invite", ["closed", "invite", "open"]), ("AUTH_COOKIE_SECURE", "cookie_secure", "auto", ["auto", "always"])]


@pytest.mark.parametrize("name, field, default, allowed", CHOICES)
def test_a_choice_accepts_its_words_in_any_case_and_a_blank_means_the_default(name, field, default, allowed):
    for word in allowed:
        assert getattr(AuthSettings.from_env({name: word}), field) == word
        assert getattr(AuthSettings.from_env({name: f"  {word.upper()} "}), field) == word
    assert getattr(AuthSettings.from_env({name: "  "}), field) == default
    assert getattr(AuthSettings.from_env({}), field) == default


# name, field, default, valid (text -> value), invalid texts (each must be refused)
INTEGERS = [
    ("AUTH_SCRYPT_N", "scrypt_n", 2**15, {"16384": 2**14, "65536": 2**16, "131072": 2**17}, ["8192", "1024", "16", "0", "20000", "262144", "-16384", "1.5", "abc"]),
    ("AUTH_SCRYPT_R", "scrypt_r", 8, {"1": 1, "16": 16, " 12 ": 12}, ["0", "17", "-1", "1.5", "abc"]),
    ("AUTH_SCRYPT_P", "scrypt_p", 1, {"1": 1, "4": 4}, ["0", "5", "-1", "abc"]),
    ("AUTH_SESSION_IDLE_SECONDS", "session_idle_seconds", 43200, {"1": 1, "604800": 604800}, ["0", "-5", "604801", "1000000001", "abc"]),
    ("AUTH_SESSION_ABSOLUTE_SECONDS", "session_absolute_seconds", 604800, {"43200": 43200, "1000000000": 10**9}, ["0", "43199", "1000000001", "abc"]),
    ("AUTH_SESSION_TOUCH_SECONDS", "session_touch_seconds", 300, {"0": 0, "1000000000": 10**9}, ["-1", "1000000001", "abc"]),
    ("AUTH_MAX_SESSIONS", "max_sessions_per_user", 10, {"1": 1, "1000": 1000}, ["0", "1001", "abc"]),
    ("AUTH_DELAY_FREE", "delay_free", 3, {"0": 0, "1000": 1000}, ["-1", "1001", "abc"]),
    ("AUTH_DELAY_BASE", "delay_base_seconds", 2, {"1": 1, "3600": 3600}, ["0", "3601", "abc"]),
    ("AUTH_DELAY_CAP", "delay_cap_seconds", 60, {"0": 0, "86400": 86400}, ["-1", "86401", "abc"]),
    ("AUTH_DELAY_CAP_UNTRUSTED", "delay_cap_untrusted_seconds", 3, {"0": 0, "5": 5, "10": 10}, ["-1", "11", "99", "abc"]),
    ("AUTH_DELAY_WINDOW", "delay_window_seconds", 900, {"1": 1, "1000000000": 10**9}, ["0", "1000000001", "abc"]),
    ("AUTH_HASH_QUEUE", "hash_queue_max", 8, {"0": 0, "1000": 1000}, ["-1", "1001", "abc"]),
    ("AUTH_HASH_TIMEOUT", "hash_timeout_seconds", 10, {"1": 1, "600": 600}, ["0", "601", "abc"]),
]
INTEGER_IDS = [row[0] for row in INTEGERS]


@pytest.mark.parametrize("name, field, default, valid, invalid", INTEGERS, ids=INTEGER_IDS)
def test_an_integer_setting_accepts_its_range_and_a_blank_means_the_default(name, field, default, valid, invalid):
    for text, expected in valid.items():
        assert getattr(AuthSettings.from_env({name: text}), field) == expected
    assert getattr(AuthSettings.from_env({name: "  "}), field) == default
    assert getattr(AuthSettings.from_env({}), field) == default


@pytest.mark.parametrize("name, field, default, valid, invalid", INTEGERS, ids=INTEGER_IDS)
def test_an_integer_setting_outside_its_range_fails_closed(name, field, default, valid, invalid):
    for text in invalid:
        with pytest.raises(AuthConfigError):
            AuthSettings.from_env({name: text})


ALL_VARIABLES = [row[0] for row in INTEGERS] + [name for name, _ in FLAGS] + [name for name, *_ in CHOICES]


@pytest.mark.parametrize("name", ALL_VARIABLES)
@pytest.mark.parametrize("junk", [JUNK, BELOW, ABOVE])
def test_a_wrong_value_is_refused_with_a_message_that_names_the_variable_and_never_the_value(name, junk):
    with pytest.raises(AuthConfigError) as error:
        AuthSettings.from_env({name: junk})
    message = str(error.value)
    assert name in message, message
    assert junk not in message, "the message echoes the value"
    assert "91827364550" not in message


def test_a_relation_error_names_the_fields_and_no_value():
    with pytest.raises(AuthConfigError) as error:
        AuthSettings.from_env({"AUTH_SESSION_IDLE_SECONDS": "604801"})
    assert "session_idle_seconds" in str(error.value) and "604801" not in str(error.value)


def test_the_error_is_a_value_error():
    assert issubclass(AuthConfigError, ValueError)


def test_every_variable_at_once_gives_every_field(tmp_path):
    env = {
        "AUTH_ENABLED": "true", "AUTH_DB": str(tmp_path / "x.db"), "AUTH_SIGNUP": "open", "AUTH_COOKIE_SECURE": "always", "AUTH_TRUST_PROXY": "yes",
        "AUTH_SCRYPT_N": "16384", "AUTH_SCRYPT_R": "4", "AUTH_SCRYPT_P": "2", "AUTH_SESSION_IDLE_SECONDS": "3600", "AUTH_SESSION_ABSOLUTE_SECONDS": "7200",
        "AUTH_SESSION_TOUCH_SECONDS": "60", "AUTH_MAX_SESSIONS": "3", "AUTH_DELAY_FREE": "5", "AUTH_DELAY_BASE": "4", "AUTH_DELAY_CAP": "120",
        "AUTH_DELAY_CAP_UNTRUSTED": "5", "AUTH_DELAY_WINDOW": "600", "AUTH_HASH_QUEUE": "2", "AUTH_HASH_TIMEOUT": "30", "AUTH_USERS": "alice:scrypt$1$2$3$s$h",
    }
    expected = AuthSettings(
        enabled=True, db_path=tmp_path / "x.db", signup="open", cookie_secure="always", trust_proxy=True, scrypt_n=16384, scrypt_r=4, scrypt_p=2,
        session_idle_seconds=3600, session_absolute_seconds=7200, session_touch_seconds=60, max_sessions_per_user=3, delay_free=5, delay_base_seconds=4,
        delay_cap_seconds=120, delay_cap_untrusted_seconds=5, delay_window_seconds=600, hash_queue_max=2, hash_timeout_seconds=30, users_text="alice:scrypt$1$2$3$s$h")
    assert AuthSettings.from_env(env) == expected


# --- the scrypt cost: the floor of the environment and the direct path ----------------------------------------------------------------------------

def test_the_environment_cannot_ask_for_a_cost_below_2_to_the_14():
    for text in ("8192", "1024", "16"):
        with pytest.raises(AuthConfigError, match="AUTH_SCRYPT_N"):
            AuthSettings.from_env({"AUTH_SCRYPT_N": text})
    assert AuthSettings.from_env({"AUTH_SCRYPT_N": "16384"}).scrypt_n == 2**14


def test_direct_construction_allows_a_cheap_cost_for_tests():
    assert AuthSettings(scrypt_n=2**10).scrypt_n == 1024
    assert AuthSettings(scrypt_n=16).scrypt_n == 16


@pytest.mark.parametrize("n", [8, 0, 24, 1000, 2**18, -16, 2**17 + 1])
def test_scrypt_n_must_be_a_power_of_two_from_16_to_2_to_the_17(n):
    with pytest.raises(AuthConfigError, match="scrypt_n"):
        AuthSettings(scrypt_n=n)


def test_the_largest_allowed_cost_is_accepted():
    assert AuthSettings(scrypt_n=2**17).scrypt_n == 131072


# --- validation of a directly built object ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("changes, word", [
    ({"signup": "everyone"}, "signup"), ({"cookie_secure": "never"}, "cookie_secure"), ({"enabled": "yes"}, "enabled"), ({"trust_proxy": 1}, "trust_proxy"),
    ({"scrypt_r": 0}, "scrypt_r"), ({"scrypt_r": 17}, "scrypt_r"), ({"scrypt_p": 0}, "scrypt_p"), ({"scrypt_p": 5}, "scrypt_p"),
    ({"session_idle_seconds": 0}, "session_idle_seconds"), ({"session_absolute_seconds": 0}, "session_absolute_seconds"),
    ({"session_idle_seconds": 100, "session_absolute_seconds": 99}, "session_idle_seconds"),
    ({"max_sessions_per_user": 0}, "max_sessions_per_user"), ({"delay_free": -1}, "delay_free"), ({"delay_base_seconds": 0}, "delay_base_seconds"),
    ({"delay_cap_untrusted_seconds": 11}, "delay_cap_untrusted_seconds"), ({"delay_cap_seconds": -1}, "delay_cap_seconds"),
    ({"hash_queue_max": -1}, "hash_queue_max"), ({"hash_timeout_seconds": 0}, "hash_timeout_seconds"), ({"users_text": 5}, "users_text"),
])
def test_a_wrong_field_of_a_directly_built_settings_is_refused(changes, word):
    with pytest.raises(AuthConfigError, match=word):
        AuthSettings(**changes)


def test_idle_may_equal_absolute():
    assert AuthSettings(session_idle_seconds=100, session_absolute_seconds=100).session_idle_seconds == 100


def test_the_settings_are_frozen():
    s = AuthSettings()
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.enabled = True
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.users_text = "x"


# --- provisioned users and the secret ----------------------------------------------------------------------------------------------------------

SECRET = "alice:scrypt$32768$8$1$c2VjcmV0U0FMVA$c2VjcmV0SEFTSA"


@pytest.mark.parametrize("signup", ["closed", "invite", "open"])
def test_sign_up_is_closed_whenever_users_are_provisioned_whatever_the_setting_says(signup):
    s = AuthSettings.from_env({"AUTH_USERS": SECRET, "AUTH_SIGNUP": signup})
    assert s.provisioned is True and s.effective_signup == "closed"
    assert s.signup == signup  # the configured value is kept; only the effective one is forced


@pytest.mark.parametrize("signup", ["closed", "invite", "open"])
@pytest.mark.parametrize("users", ["", "   ", "\n\t "])
def test_without_provisioned_users_the_configured_sign_up_mode_is_effective(signup, users):
    s = AuthSettings.from_env({"AUTH_USERS": users, "AUTH_SIGNUP": signup})
    assert s.provisioned is False and s.effective_signup == signup


def test_users_text_is_kept_exactly_as_given():
    """Through from_env, with blanks, tabs and newlines at both ends on purpose: a strip() anywhere between the variable and the setting must be seen."""
    text = "  \n\t" + SECRET + "\r\nbob:scrypt$32768$8$1$AAA$BBB \n\n  "
    s = AuthSettings.from_env({"AUTH_USERS": text})
    assert s.users_text == text and s.provisioned is True
    for shown in (repr(s), str(s)):
        assert "scrypt$" not in shown and "c2VjcmV0" not in shown and "alice" not in shown


def test_a_users_text_of_blanks_only_is_kept_as_given_and_is_not_provisioned():
    s = AuthSettings.from_env({"AUTH_USERS": "  \n\t \r\n "})
    assert s.users_text == "  \n\t \r\n " and s.provisioned is False and s.effective_signup == s.signup


def test_the_secret_is_not_in_repr_or_str_or_the_format_output():
    s = AuthSettings.from_env({"AUTH_USERS": SECRET})
    for shown in (repr(s), str(s), f"{s}", "%s" % (s,), "%r" % (s,)):
        assert "scrypt$" not in shown and "c2VjcmV0" not in shown and "alice" not in shown


def test_the_secret_is_never_echoed_in_an_error_even_when_it_is_set():
    with pytest.raises(AuthConfigError) as error:
        AuthSettings.from_env({"AUTH_USERS": SECRET, "AUTH_SIGNUP": JUNK})
    assert "alice" not in str(error.value) and "scrypt$" not in str(error.value) and "c2VjcmV0" not in str(error.value)
    with pytest.raises(AuthConfigError) as error:
        AuthSettings(users_text=5)
    assert "5" not in str(error.value).replace("scrypt_", "")


# --- cookie_secure, the database path, which mapping is read ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("mode, https, expected", [("auto", False, False), ("auto", True, True), ("always", False, True), ("always", True, True)])
def test_the_secure_flag_follows_the_mode(mode, https, expected):
    assert cookie_secure(AuthSettings(cookie_secure=mode), is_https=https) is expected


def test_a_relative_database_path_is_resolved_against_the_given_root(tmp_path):
    assert AuthSettings.from_env({"AUTH_DB": "data/x.db"}, root=tmp_path).db_path == tmp_path / "data" / "x.db"


def test_an_absolute_database_path_is_kept(tmp_path):
    other = tmp_path / "elsewhere"
    given = tmp_path / "abs" / "users.db"
    assert AuthSettings.from_env({"AUTH_DB": str(given)}, root=other).db_path == given


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_database_path_means_var_auth_db_under_the_root(tmp_path, blank):
    assert AuthSettings.from_env({"AUTH_DB": blank}, root=tmp_path).db_path == tmp_path / "var" / "auth.db"


class Recorder(dict):
    """A stand-in for os.environ that logs every read."""

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


def test_from_env_reads_only_the_mapping_it_is_given(monkeypatch):
    real = Recorder({"AUTH_ENABLED": "true", "AUTH_SIGNUP": "open"})
    monkeypatch.setattr(os, "environ", real)
    given = {"AUTH_ENABLED": "false", "AUTH_SIGNUP": "closed"}
    s = AuthSettings.from_env(given)
    assert s.enabled is False and s.signup == "closed"
    assert real.reads == [], f"the process environment was read: {real.reads}"
    assert AuthSettings.from_env(types.MappingProxyType(given)).signup == "closed"


def test_without_a_mapping_from_env_reads_the_process_environment(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_SIGNUP", "closed")
    s = AuthSettings.from_env()
    assert s.enabled is True and s.signup == "closed"


# --- auth_enabled: the switch is read on its own (Phase 3, step A) ---------------------------------------------------------------------------------------------

TRUE_WORDS, FALSE_WORDS = ["1", "true", "TRUE", " Yes ", "on"], ["0", "false", "FALSE", " No ", "off"]


@pytest.mark.parametrize("text", TRUE_WORDS)
def test_auth_enabled_accepts_the_true_words(text):
    assert auth_enabled({"AUTH_ENABLED": text}) is True


@pytest.mark.parametrize("text", FALSE_WORDS)
def test_auth_enabled_accepts_the_false_words(text):
    assert auth_enabled({"AUTH_ENABLED": text}) is False


@pytest.mark.parametrize("env", [{}, {"AUTH_ENABLED": ""}, {"AUTH_ENABLED": "   "}], ids=["unset", "empty", "blank"])
def test_auth_enabled_is_off_when_unset_or_blank(env):
    assert auth_enabled(env) is False


@pytest.mark.parametrize("text", [JUNK, "ture", "2", "yes please", "enabled"])
def test_auth_enabled_refuses_any_other_word_naming_the_variable_and_never_the_value(text):
    with pytest.raises(AuthConfigError) as caught:
        auth_enabled({"AUTH_ENABLED": text})
    assert "AUTH_ENABLED" in str(caught.value) and text.strip() not in str(caught.value)


@pytest.mark.parametrize("text", TRUE_WORDS + FALSE_WORDS + ["", "  "])
def test_auth_enabled_agrees_with_from_env(text):
    assert auth_enabled({"AUTH_ENABLED": text}) is AuthSettings.from_env({"AUTH_ENABLED": text}).enabled


def test_auth_enabled_ignores_every_other_variable_even_a_wrong_one():
    wrong = {name: JUNK for name in ALL_VARIABLES if name != "AUTH_ENABLED"}
    assert auth_enabled(wrong) is False
    assert auth_enabled({**wrong, "AUTH_ENABLED": "true"}) is True
    with pytest.raises(AuthConfigError):  # the whole set is still refused by from_env: the switch is what keeps a typo from stopping an auth-off server
        AuthSettings.from_env(wrong)


def test_auth_enabled_reads_only_auth_enabled_from_the_mapping_it_is_given():
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

    spy = Spy({"AUTH_ENABLED": "true", "AUTH_USERS": "alice:x", "AUTH_SCRYPT_N": JUNK})
    assert auth_enabled(spy) is True
    assert spy.reads == ["AUTH_ENABLED"]


def test_auth_enabled_with_a_mapping_does_not_touch_the_process_environment(monkeypatch):
    real = Recorder({"AUTH_ENABLED": "true"})
    monkeypatch.setattr(os, "environ", real)
    assert auth_enabled({"AUTH_ENABLED": "false"}) is False
    assert real.reads == []


def test_auth_enabled_without_a_mapping_reads_the_process_environment(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    assert auth_enabled() is True
    monkeypatch.setenv("AUTH_ENABLED", "off")
    assert auth_enabled() is False
    monkeypatch.delenv("AUTH_ENABLED")
    assert auth_enabled() is False


# --- importing reads nothing (a real subprocess with an environment-read recorder) ---------------------------------------------------------------------------

IMPORT_PROOF = r"""
import faulthandler, json, os, sys
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
before = set(sys.modules)
import app.auth, app.auth.config as cfg
imported = sorted(m for m in set(sys.modules) - before if m == "app" or m.startswith(("app.", "dotenv")))
import_reads = list(reads)
reads.clear()
cfg.AuthSettings.from_env()
print(json.dumps({"import_reads": import_reads, "imported": imported, "dotenv": "dotenv" in sys.modules, "app_config": "app.config" in sys.modules,
                  "from_env_reads": sorted(set(reads))}))
"""


@pytest.fixture(scope="module")
def import_proof():
    result = subprocess.run([sys.executable, "-B", "-c", IMPORT_PROOF, str(ROOT)], capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert result.returncode == 0, result.stderr[-800:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_importing_app_auth_reads_no_environment_variable(import_proof):
    assert import_proof["import_reads"] == []


def test_importing_app_auth_loads_nothing_but_its_own_modules(import_proof):
    assert import_proof["imported"] == ["app", "app.auth", "app.auth.config"]
    assert import_proof["dotenv"] is False and import_proof["app_config"] is False


def test_the_recorder_works_from_env_is_seen_reading_the_variables(import_proof):
    reads = import_proof["from_env_reads"]
    assert len(reads) > 0 and "AUTH_ENABLED" in reads and "AUTH_USERS" in reads


# --- the fixtures of tests/auth/conftest.py ------------------------------------------------------------------------------------------------------------------

def test_the_fake_clock_starts_where_it_says_and_only_moves_when_told(clock):
    assert clock() == 1_700_000_000.0 and clock() == clock()
    assert clock.advance(90) == 1_700_000_090.0 and clock() == 1_700_000_090.0
    assert clock.advance(0) == 1_700_000_090.0


def test_the_fake_clock_refuses_to_run_backwards(clock):
    with pytest.raises(AssertionError):
        clock.advance(-1)
    assert clock() == 1_700_000_000.0


def test_the_settings_fixture_is_cheap_enabled_and_keeps_its_database_under_tmp_path(auth_settings, tmp_path):
    assert auth_settings.enabled is True and auth_settings.scrypt_n == 2**10
    assert auth_settings.db_path == tmp_path / "auth.db" and ROOT not in auth_settings.db_path.parents


def test_the_settings_factory_overrides_any_field(settings_factory, tmp_path):
    s = settings_factory(enabled=False, scrypt_n=2**11, signup="open", db_path=tmp_path / "other.db")
    assert (s.enabled, s.scrypt_n, s.signup, s.db_path) == (False, 2048, "open", tmp_path / "other.db")


# --- the guards of tests/auth/conftest.py really fail a test -------------------------------------------------------------------------------------------------

INNER = {
    "clean": "def test_ok():\n    assert True\n",
    "sleeps": "import time\n\ndef test_sleeps():\n    time.sleep(0)\n",
    "creates_var": "from pathlib import Path\n\ndef test_makes_var():\n    (Path(__file__).resolve().parents[2] / 'var').mkdir()\n",
}


def run_inner(tmp_path, body):
    """Runs a tiny pytest session in a throw-away tree that holds a copy of the real tests/auth/conftest.py (the folder's parents[2] is the tree's root)."""
    folder = tmp_path / "tests" / "auth"
    folder.mkdir(parents=True)
    (folder / "conftest.py").write_text((Path(__file__).parent / "conftest.py").read_text(encoding="utf-8"), encoding="utf-8")
    (folder / "test_inner.py").write_text(body, encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, "-B", "-m", "pytest", str(folder), "-q", "-p", "no:cacheprovider", "--rootdir", str(tmp_path)],
                            capture_output=True, text=True, timeout=120, cwd=tmp_path, env=env)
    return result.returncode, result.stdout


def test_the_guards_let_a_clean_test_pass(tmp_path):
    code, out = run_inner(tmp_path, INNER["clean"])
    assert code == 0 and "1 passed" in out, out[-600:]


def test_the_no_sleep_guard_fails_a_test_that_sleeps(tmp_path):
    code, out = run_inner(tmp_path, INNER["sleeps"])
    assert code != 0 and "1 failed" in out and "time.sleep" in out, out[-600:]


def test_the_var_guard_errors_a_test_that_creates_the_default_database_folder(tmp_path):
    code, out = run_inner(tmp_path, INNER["creates_var"])
    assert code != 0 and "1 error" in out and "databases must live under tmp_path" in out, out[-600:]
    assert (tmp_path / "var").exists()  # it was the inner tree's var, not this repository's
    assert not (ROOT / "var").exists()
