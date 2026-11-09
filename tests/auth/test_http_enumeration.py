"""app/auth/routes.py: what the routes do NOT tell, at HTTP level (Phase 3, step D).

Same setup as test_http_login.py. Expected values come from the Phase 3 plan and the step D request: a failed login is the same status, body AND headers for an
unknown name, a wrong password, a disabled account and odd input, in both brute-force modes; the delay schedule depends on the submitted name only; with auth off
every /auth path and method answers exactly like a path that does not exist, sets no cookie and touches no database. Tests named pinned_* record behaviour the plan
leaves open.
"""
import pytest

from app.auth import routes
from app.auth.passwords import Busy, HashGate
from gate_support import ALL_METHODS, AUTH_PATHS, PASSWORD, RouteRig, build_routes_app

BACKSLASH = chr(92)


@pytest.fixture(autouse=True)
def delays(monkeypatch):
    recorded = []

    async def record(seconds):
        recorded.append(seconds)

    monkeypatch.setattr(routes, "apply_delay", record)
    return recorded


@pytest.fixture
def make_rig(tmp_path, make_service, clock):
    made = []

    def make(*, users=("alice", "dave"), **overrides):
        values = {"session_idle_seconds": 600, "session_absolute_seconds": 3600}
        values.update(overrides)
        env = make_service(**values)
        for name in users:
            env.add_local_user(name, PASSWORD)
        if "dave" in users:
            env.sql("UPDATE users SET disabled = 1 WHERE username = ?", ("dave",))
        made.append(env)
        return RouteRig(build_routes_app(tmp_path / f"static{len(made)}"), env, clock)

    return make


class FullGate(HashGate):
    def acquire(self):
        raise Busy("queue_full")


def signature(response):
    return response.status_code, response.content, tuple(sorted(response.headers.multi_items()))


def raw_login(rig, username_json, password_json):
    """A login whose two fields are given as JSON text already (so a lone surrogate escape can be sent)."""
    return rig.post_json("/auth/login", b'{"username":' + username_json + b',"password":' + password_json + b"}")


# --- failed logins are indistinguishable ------------------------------------------------------------------------------------------------------------------

ATTEMPTS = [
    ("unknown_name", "nobody", PASSWORD), ("wrong_password", "alice", "the wrong password"), ("disabled_right_password", "dave", PASSWORD),
    ("disabled_wrong_password", "dave", "the wrong password"), ("too_short_name", "a", PASSWORD), ("name_with_space", "has space", PASSWORD),
    ("name_200_chars", "x" * 200, PASSWORD), ("name_with_nul", chr(0) + "bad", PASSWORD), ("name_with_slash", "bad/name", PASSWORD), ("name_with_backslash", "bad" + BACKSLASH + "name", PASSWORD),
    ("empty_name", "", PASSWORD), ("name_256_chars", "x" * 256, PASSWORD), ("name_non_ascii", "caf" + chr(0xE9), PASSWORD), ("name_emoji", chr(0x1F600) * 4, PASSWORD),
    ("empty_password", "ghost1", ""), ("long_password", "ghost2", "p" * 2048), ("nul_password", "ghost3", "pass" + chr(0) + "word-123"), ("unicode_password", "ghost4", "caf" + chr(0xE9) + " 2026 au lait"),
    ("real_name_other_case_wrong_password", "ALICE", "the wrong password"),
]


@pytest.mark.parametrize("trusted", [False, True], ids=["untrusted", "trusted"])
def test_every_kind_of_failed_login_gives_the_identical_status_body_and_headers(make_rig, trusted):
    rig = make_rig(trust_proxy=trusted)
    answers = {name: rig.login_http(user, password) for name, user, password in ATTEMPTS}
    assert {k: v.status_code for k, v in answers.items()} == {k: 401 for k in answers}
    assert len({repr(signature(v)) for v in answers.values()}) == 1
    only = next(iter(answers.values()))
    assert only.content == b'{"error":"Invalid username or password.","code":"invalid_credentials"}' and only.headers.get_list("set-cookie") == []


@pytest.mark.parametrize("trusted", [False, True], ids=["untrusted", "trusted"])
def test_a_lone_surrogate_in_either_field_is_the_same_failure(make_rig, trusted):
    rig = make_rig(trust_proxy=trusted)
    surrogate = b'"' + BACKSLASH.encode() + b'ud800"'
    reference = signature(rig.login_http("nobody", PASSWORD))
    for user, password in ((b'"alice"', surrogate), (surrogate, b'"' + PASSWORD.encode() + b'"'), (surrogate, surrogate), (b'"nobody"', surrogate)):
        assert signature(raw_login(rig, user, password)) == reference


@pytest.mark.parametrize("trusted", [False, True], ids=["untrusted", "trusted"])
def test_each_failed_login_does_exactly_one_scrypt_whatever_the_reason(make_rig, derive_counter, trusted):
    rig = make_rig(trust_proxy=trusted)
    for name, user, password in ATTEMPTS:
        if name == "long_password":
            continue  # see the next test
        derive_counter.reset()
        assert rig.login_http(user, password).status_code == 401
        assert derive_counter.count == 1, name


def test_pinned_a_password_over_the_length_limit_is_refused_without_hashing_for_every_kind_of_name_alike(make_rig, derive_counter):
    rig = make_rig()
    answers = []
    for user in ("alice", "nobody", "dave", "x" * 200):
        derive_counter.reset()
        answers.append(rig.login_http(user, "p" * 2048))
        assert derive_counter.count == 0  # no work either way, so the work does not tell whether the name exists
    assert {a.status_code for a in answers} == {401} and len({repr(signature(a)) for a in answers}) == 1


def test_a_refusal_does_no_scrypt_for_a_real_name_or_an_unknown_one(make_rig, derive_counter):
    rig = make_rig(trust_proxy=True)
    for user in ("alice", "nobody", "dave"):
        for _ in range(4):
            rig.login_http(user, "the wrong password")
        derive_counter.reset()
        assert rig.login_http(user, "the wrong password").status_code == 429 and derive_counter.count == 0


@pytest.mark.parametrize("user", ["alice", "nobody", "dave"])
def test_the_delay_schedule_is_the_same_for_a_real_an_unknown_and_a_disabled_name(make_rig, delays, user):
    rig = make_rig()
    for _ in range(6):
        rig.login_http(user, "the wrong password")
    assert delays == [0, 0, 0, 2, 3, 3]


def test_the_delay_depends_on_the_submitted_name_only_not_on_whether_it_exists(make_rig, delays):
    rig = make_rig()
    for _ in range(5):
        rig.login_http("nobody", "the wrong password")
    delays.clear()
    rig.login_http("alice", "the wrong password")
    assert delays == [0]  # alice has her own count, though "nobody" has delays by now


def test_trusted_the_refusal_is_the_same_answer_for_a_real_an_unknown_and_a_disabled_name(make_rig):
    rig = make_rig(trust_proxy=True)
    refusals = []
    for user in ("alice", "nobody", "dave"):
        for _ in range(4):
            rig.login_http(user, "the wrong password")
        refusals.append(rig.login_http(user, "the wrong password"))
    assert {r.status_code for r in refusals} == {429} and len({repr(signature(r)) for r in refusals}) == 1


def test_trusted_the_refusal_also_refuses_the_right_password_alike_for_every_kind_of_name(make_rig):
    rig = make_rig(trust_proxy=True)
    answers = []
    for user in ("alice", "nobody", "dave"):
        for _ in range(4):
            rig.login_http(user, "the wrong password")
        answers.append(rig.login_http(user, PASSWORD))
    assert len({repr(signature(a)) for a in answers}) == 1 and answers[0].status_code == 429


@pytest.mark.parametrize("trusted", [False, True], ids=["untrusted", "trusted"])
def test_a_full_hashing_gate_gives_the_same_busy_answer_for_every_kind_of_name(make_rig, trusted):
    rig = make_rig(trust_proxy=trusted)
    rig.env.svc._gate = FullGate(0, 1)
    answers = [rig.login_http(user, password) for name, user, password in ATTEMPTS if name != "long_password"]  # that one never reaches the gate
    assert {a.status_code for a in answers} == {429} and len({repr(signature(a)) for a in answers}) == 1


# --- a body that is not valid is the same 422 whoever the name is --------------------------------------------------------------------------------------

@pytest.mark.parametrize("make", [lambda user: {"username": user, "password": 5}, lambda user: {"username": user}, lambda user: {"username": user, "password": "p", "extra": 1},
                                  lambda user: {"username": user, "password": None}])
def test_an_invalid_body_gives_the_same_422_for_a_real_an_unknown_and_a_disabled_name(make_rig, make):
    rig = make_rig()
    answers = [rig.post_json("/auth/login", make(user)) for user in ("alice", "nobody", "dave")]
    assert {a.status_code for a in answers} == {422} and len({repr(signature(a)) for a in answers}) == 1


# --- auth off: the routes are not there --------------------------------------------------------------------------------------------------------------------

OFF_PATHS = list(AUTH_PATHS) + ["/auth", "/auth/", "/auth/nothing-here", "/auth/login/", "/AUTH/login"]
OFF_VARIANTS = {
    "bare": {},
    "json_body": {"content": b'{"username":"alice","password":"correct horse battery staple"}', "headers": [("Content-Type", "application/json")]},
    "cookie_and_token": {"cookie": "ops_session=" + "B" * 43, "csrf": "x" * 43, "origin": "http://evil.test"},
    "big_text_body": {"content": b"x" * 20000, "headers": [("Content-Type", "text/plain")]},
}


@pytest.fixture
def off_rig(make_rig):
    rig = make_rig()
    rig.app.state.auth = None
    return rig


@pytest.mark.parametrize("variant", OFF_VARIANTS)
@pytest.mark.parametrize("method", list(ALL_METHODS) + ["TRACE"])
@pytest.mark.parametrize("path", OFF_PATHS)
def test_with_auth_off_every_auth_path_and_method_answers_exactly_like_a_path_that_does_not_exist(off_rig, path, method, variant):
    kwargs = OFF_VARIANTS[variant]
    answer = off_rig.send(method, path, **kwargs)
    unknown = off_rig.send(method, "/no-such-page", **kwargs)
    assert signature(answer) == signature(unknown)
    assert answer.headers.get_list("set-cookie") == []


def test_with_auth_off_the_unknown_path_answers_are_the_ones_of_app_main(off_rig):
    assert off_rig.send("GET", "/auth/login").json() == {"error": "Not Found", "code": "not_found"}
    post = off_rig.send("POST", "/auth/login")
    assert post.status_code == 405 and post.json() == {"error": "Method Not Allowed", "code": "method_not_allowed"} and post.headers["allow"] == "GET, HEAD"
    assert off_rig.send("HEAD", "/auth/me").status_code == 404 and off_rig.send("GET", "/auth/me").status_code == 404


def test_with_auth_off_a_valid_login_does_nothing_and_touches_no_database(off_rig, monkeypatch):
    from app.auth.store import Store

    before_bytes, before_files = off_rig.env.db_bytes(), sorted(p.name for p in off_rig.env.settings.db_path.parent.iterdir())
    monkeypatch.setattr(Store, "_connect", lambda self: (_ for _ in ()).throw(AssertionError("the database was touched while auth is off")))
    for path in AUTH_PATHS:
        for method in ("GET", "POST"):
            off_rig.post_json(path, {"username": "alice", "password": PASSWORD}) if method == "POST" else off_rig.send(method, path)
    assert off_rig.env.db_bytes() == before_bytes and sorted(p.name for p in off_rig.env.settings.db_path.parent.iterdir()) == before_files
    assert off_rig.calls == []


def test_with_auth_off_no_body_is_read_and_a_huge_or_odd_one_changes_nothing(off_rig):
    assert off_rig.post_json("/auth/login", b"x" * 100000).status_code == 405
    assert off_rig.send("POST", "/auth/signup", content=b"\xff\xfe", headers=[("Content-Type", "application/json")]).status_code == 405


def test_with_auth_off_the_body_of_an_auth_post_is_never_asked_for(off_rig):
    from gate_support import raw_call

    for path in ("/auth/login", "/auth/signup", "/auth/logout", "/auth/logout-all"):
        result = raw_call(off_rig.app, "POST", path, [("Content-Type", "application/json"), ("Content-Length", "2")], [b"{}"])
        assert result.status == 405 and result.receive_calls == 0, path


def test_turning_auth_on_again_brings_the_routes_back(off_rig):
    assert off_rig.login_http("alice", PASSWORD).status_code == 405
    off_rig.app.state.auth = off_rig.env.svc
    assert off_rig.login_http("alice", PASSWORD).status_code == 200


def test_unknown_paths_under_auth_stay_unknown_with_auth_on(make_rig):
    rig = make_rig()
    assert rig.send("GET", "/auth/nothing-here").json() == {"error": "Not Found", "code": "not_found"}
    assert rig.send("GET", "/auth/nothing-here").status_code == rig.send("GET", "/no-such-page").status_code == 404
