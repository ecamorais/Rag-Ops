"""app/auth/middleware.py: CSRF on unsafe requests (Phase 3, step C).

Expected values come from the Phase 3 plan and the step C request: every request that is not GET, HEAD or OPTIONS to a session route needs a live session, then
origin_ok(), then an X-CSRF-Token equal to the token STORED with the session (compared by service.verify_csrf); the CSRF cookie alone never counts. Public POSTs
(login, sign-up) get the Origin step only. Failure is 403 {"error":"The request was refused.","code":"csrf_failed"}. The app is the throwaway one of
gate_support.py; the test client's Host is "testserver", so its own Origin is http://testserver. Tests named pinned_* record behaviour the plan leaves open.
"""
import pytest

from gate_support import POLICY, SAME, SESSION_ROUTES, UNSAFE, Rig, build_app

REFUSED = {"error": "The request was refused.", "code": "csrf_failed"}
ANON = {"error": "Please sign in.", "code": "unauthenticated"}
REAL_UNSAFE = [(m, p) for (m, p), k in POLICY.items() if k == "session" and m != "GET"]


@pytest.fixture
def make_rig(tmp_path, make_service, clock):
    made = []

    def make(**overrides):
        env = make_service(**overrides)
        made.append(env)
        return Rig(build_app(tmp_path / f"static{len(made)}"), env, clock)

    return make


@pytest.fixture
def rig(make_rig):
    return make_rig()


@pytest.fixture
def alice(rig):
    return rig.login("alice")


def path_of(template):
    return template.replace("{ticket_id}", "OPS-1001")


# --- every unsafe method on every session route -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("method", UNSAFE)
@pytest.mark.parametrize("path", SESSION_ROUTES)
def test_an_unsafe_method_on_a_session_route_is_401_anonymous_403_without_the_token_and_passes_with_it(rig, alice, method, path):
    assert rig.send(method, path).status_code == 401
    response = rig.send(method, path, alice, csrf="")
    assert response.status_code == 403 and response.json() == REFUSED
    assert rig.calls == []
    response = rig.send(method, path, alice)
    assert response.status_code not in (401, 403)  # the handler for the methods it has, the static mount's 405 for the others


@pytest.mark.parametrize("method, template", REAL_UNSAFE)
def test_the_handler_runs_only_with_the_right_token(rig, alice, method, template):
    path = path_of(template)
    rig.send(method, path, alice, csrf="not-the-token")
    assert rig.calls == []
    assert rig.send(method, path, alice).status_code == 200 and [c[0] for c in rig.calls] == [path]


# --- the token ----------------------------------------------------------------------------------------------------------------------------------------

def other_session_token(rig, alice):
    return rig.login("bob").csrf


NEGATIVE_TOKENS = {
    "none": lambda rig, s: {"csrf": ""},
    "empty_header": lambda rig, s: {"csrf": "", "headers": [("X-CSRF-Token", "")]},
    "wrong": lambda rig, s: {"csrf": "x" * len(s.csrf)},
    "one_char_longer": lambda rig, s: {"csrf": s.csrf + "A"},
    "one_char_shorter": lambda rig, s: {"csrf": s.csrf[:-1]},
    "other_case": lambda rig, s: {"csrf": s.csrf.swapcase()},
    "padded": lambda rig, s: {"csrf": " " + s.csrf + " "},
    "another_users_token": lambda rig, s: {"csrf": rig.login("bob").csrf},
    "another_session_of_the_same_user": lambda rig, s: {"csrf": rig.login("alice").csrf},
    "the_session_token_itself": lambda rig, s: {"csrf": s.token},
    "too_long": lambda rig, s: {"csrf": "A" * 201},
    "very_long": lambda rig, s: {"csrf": "A" * 5000},
    "non_ascii": lambda rig, s: {"csrf": "", "headers": [("X-CSRF-Token", b"\xe9" * 43)]},
    "two_header_lines_both_right": lambda rig, s: {"csrf": "", "headers": [("X-CSRF-Token", s.csrf), ("X-CSRF-Token", s.csrf)]},
    "another_header_name": lambda rig, s: {"csrf": "", "headers": [("X-XSRF-Token", s.csrf), ("X-CSRF", s.csrf)]},
    "cookie_only": lambda rig, s: {"csrf": "", "cookie": f"ops_session={s.token}; ops_csrf={s.csrf}"},
    "double_submit_forgery": lambda rig, s: {"csrf": "forged-value", "cookie": f"ops_session={s.token}; ops_csrf=forged-value"},
}


@pytest.mark.parametrize("kind", NEGATIVE_TOKENS)
def test_a_wrong_or_missing_token_is_403_and_the_handler_never_runs(rig, alice, kind):
    rig.login("bob")
    response = rig.send("POST", "/chat", alice, **NEGATIVE_TOKENS[kind](rig, alice))
    assert response.status_code == 403 and response.json() == REFUSED
    assert rig.calls == []


def test_the_token_in_the_query_string_or_the_body_does_not_count(rig, alice):
    assert rig.send("POST", f"/chat?csrf_token={alice.csrf}&x-csrf-token={alice.csrf}", alice, csrf="").status_code == 403
    assert rig.send("POST", "/chat", alice, csrf="", json={"csrf_token": alice.csrf}).status_code == 403
    assert rig.send("POST", "/chat", alice, csrf="", data={"csrf_token": alice.csrf}).status_code == 403
    assert rig.calls == []


def test_the_right_token_passes_in_any_header_name_case(rig, alice):
    for name in ("X-CSRF-Token", "x-csrf-token", "X-Csrf-Token", "X-CSRF-TOKEN"):
        assert rig.send("POST", "/chat", alice, csrf="", headers=[(name, alice.csrf)]).status_code == 200
    assert len(rig.calls) == 4


def test_the_token_belongs_to_the_session_it_is_stored_with(rig):
    first, second = rig.login("alice"), rig.login("alice")
    assert first.csrf != second.csrf
    assert rig.send("POST", "/chat", first).status_code == 200 and rig.send("POST", "/chat", second).status_code == 200
    assert rig.send("POST", "/chat", first, csrf=second.csrf).status_code == 403
    assert rig.send("POST", "/chat", second, csrf=first.csrf).status_code == 403


def test_the_stored_token_is_what_is_compared_not_a_copy(rig, alice):
    rig.env.sql("UPDATE sessions SET csrf_token = ? WHERE csrf_token = ?", ("replaced-in-the-database", alice.csrf))
    assert rig.send("POST", "/chat", alice).status_code == 403
    assert rig.send("POST", "/chat", alice, csrf="replaced-in-the-database").status_code == 200


def test_the_middleware_defers_to_the_service_for_the_comparison_and_only_for_unsafe_session_requests(rig, alice):
    seen = []

    def verdict(answer):
        def verify(info, supplied):
            seen.append((info.username, supplied))
            return answer
        return verify

    rig.env.svc.verify_csrf = verdict(True)
    assert rig.send("POST", "/chat", alice, csrf="anything").status_code == 200  # the service said yes: the middleware has no opinion of its own
    assert seen == [("alice", "anything")]
    rig.env.svc.verify_csrf = verdict(False)
    assert rig.send("POST", "/chat", alice).status_code == 403  # the service said no, although the header is the real token
    seen.clear()
    rig.send("GET", "/filters", alice)  # safe
    rig.send("POST", "/auth/echo", content=b"{}", headers=[("Content-Type", "application/json")])  # public
    rig.send("POST", "/chat")  # anonymous: refused before the comparison
    assert seen == []


def test_a_session_that_has_ended_is_401_not_403_whatever_the_token(rig, alice):
    rig.env.svc.log_out_everywhere(alice.token)
    response = rig.send("POST", "/chat", alice)
    assert response.status_code == 401 and response.json() == ANON


# --- where the request came from ----------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("origin, status", [
    (SAME, 200), ("http://testserver:80", 200), ("HTTP://TESTSERVER", 200), ("http://TestServer", 200), (" http://testserver ", 200),
    ("https://testserver", 200),  # pinned: the scheme is not compared, behind a proxy the server cannot see it
    ("http://evil.test", 403), ("null", 403), ("NULL", 403), ("", 403), ("http://testserver:8000", 403), ("http://testserver:443", 403), ("http://testserver.evil.test", 403),
    ("http://evil.test/http://testserver", 403), ("http://testserver@evil.test", 403), ("http://evil.test@testserver", 403), ("http://testserver/", 403),
    ("testserver", 403), ("ftp://testserver", 403), ("http://testserver, http://evil.test", 403),
])
def test_an_origin_must_be_this_site(rig, alice, origin, status):
    assert rig.send("POST", "/chat", alice, origin=origin).status_code == status
    assert len(rig.calls) == (status == 200)


def test_pinned_two_origin_lines_are_refused_even_when_both_are_this_site(rig, alice):
    assert rig.send("POST", "/chat", alice, origin=None, headers=[("Origin", SAME), ("Origin", SAME)]).status_code == 403


@pytest.mark.parametrize("fetch, status", [("same-origin", 200), ("none", 200), ("SAME-ORIGIN", 200), (" none ", 200), ("same-site", 403), ("cross-site", 403), ("", 403),
                                           ("garbage", 403), ("same-origin, cross-site", 403)])
def test_without_an_origin_sec_fetch_site_must_be_same_origin_or_none(rig, alice, fetch, status):
    assert rig.send("POST", "/chat", alice, origin=None, fetch=fetch).status_code == status


def test_pinned_two_sec_fetch_site_lines_are_refused(rig, alice):
    assert rig.send("POST", "/chat", alice, origin=None, headers=[("Sec-Fetch-Site", "same-origin"), ("Sec-Fetch-Site", "same-origin")]).status_code == 403


def test_with_neither_header_the_token_is_still_required(rig, alice):
    assert rig.send("POST", "/chat", alice, origin=None, csrf="").status_code == 403
    assert rig.send("POST", "/chat", alice, origin=None, csrf="wrong").status_code == 403
    assert rig.send("POST", "/chat", alice, origin=None).status_code == 200


def test_pinned_a_matching_origin_wins_over_sec_fetch_site_and_a_foreign_one_over_the_token(rig, alice):
    assert rig.send("POST", "/chat", alice, origin=SAME, fetch="cross-site").status_code == 200
    assert rig.send("POST", "/chat", alice, origin="http://evil.test", fetch="same-origin").status_code == 403


def test_a_foreign_origin_with_the_right_token_is_refused(rig, alice):
    response = rig.send("POST", "/chat", alice, origin="http://evil.test")
    assert response.status_code == 403 and response.json() == REFUSED and rig.calls == []


# --- order: the session first, then where it came from, then the token ---------------------------------------------------------------------------------------

def test_the_session_is_checked_before_anything_else(rig, alice):
    assert rig.send("POST", "/chat", origin="http://evil.test", csrf="x").status_code == 401
    assert rig.send("POST", "/chat", cookie="ops_session=" + "B" * 43, origin="http://evil.test", csrf="x").status_code == 401
    assert rig.send("POST", "/chat", alice, origin="http://evil.test", csrf="wrong").status_code == 403


# --- safe methods need no token -----------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_a_safe_method_needs_no_token_and_no_good_origin_but_still_the_session(rig, alice, method):
    answer = rig.send(method, "/filters", alice, csrf="", origin="http://evil.test", fetch="cross-site")
    assert answer.status_code not in (401, 403)
    assert rig.send(method, "/filters", alice, csrf="wrong").status_code not in (401, 403)
    assert rig.send(method, "/filters", csrf="", origin="http://evil.test").status_code == 401


# --- public POSTs: the Origin step only --------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/auth/login", "/auth/signup", "/auth/echo"])
class TestPublicPosts:
    def test_no_token_is_needed_and_a_wrong_one_is_ignored(self, rig, path):
        assert rig.send("POST", path).status_code == 200
        assert rig.send("POST", path, csrf="wrong").status_code == 200
        assert rig.send("POST", path, origin=None).status_code == 200

    @pytest.mark.parametrize("origin", ["http://evil.test", "null", "", "http://testserver:9999"])
    def test_a_foreign_origin_is_403(self, rig, path, origin):
        response = rig.send("POST", path, origin=origin)
        assert response.status_code == 403 and response.json() == REFUSED

    @pytest.mark.parametrize("fetch, status", [("cross-site", 403), ("same-site", 403), ("same-origin", 200), ("none", 200)])
    def test_without_an_origin_the_fetch_site_decides(self, rig, path, fetch, status):
        assert rig.send("POST", path, origin=None, fetch=fetch).status_code == status

    def test_a_live_session_does_not_change_the_rule(self, rig, alice, path):
        assert rig.send("POST", path, alice, origin="http://evil.test").status_code == 403
        assert rig.send("POST", path, alice, csrf="").status_code == 200

    def test_a_dead_cookie_is_not_looked_at(self, rig, path):
        response = rig.send("POST", path, cookie="ops_session=" + "B" * 43)
        assert response.status_code == 200 and response.headers.get_list("set-cookie") == []


# --- the 403 itself ------------------------------------------------------------------------------------------------------------------------------------------

def test_the_403_is_json_no_store_without_cookies_or_authenticate_and_names_nothing(rig, alice):
    response = rig.send("POST", "/chat", alice, csrf="wrong-" + alice.csrf)
    assert response.status_code == 403 and response.content == b'{"error":"The request was refused.","code":"csrf_failed"}'
    assert response.headers["content-type"] == "application/json" and response.headers["cache-control"] == "no-store"
    assert response.headers.get_list("set-cookie") == [] and "www-authenticate" not in response.headers
    assert alice.csrf not in response.text and alice.token not in response.text and "alice" not in response.text


def test_the_token_and_origin_failures_look_the_same_from_outside(rig, alice):
    wrong_token = rig.send("POST", "/chat", alice, csrf="wrong")
    wrong_origin = rig.send("POST", "/chat", alice, origin="http://evil.test")
    no_token = rig.send("POST", "/chat", alice, csrf="")
    assert (wrong_token.content, sorted(wrong_token.headers.items())) == (wrong_origin.content, sorted(wrong_origin.headers.items())) == (no_token.content, sorted(no_token.headers.items()))


def test_pinned_a_refused_request_still_counts_as_activity_for_the_idle_limit_because_the_session_is_validated_first(rig, alice):
    rig.clock.advance(400)  # past the touch interval of 300 s
    assert rig.send("POST", "/chat", alice, csrf="wrong").status_code == 403
    assert rig.env.sql("SELECT last_seen_at FROM sessions") == [(int(rig.clock()),)]
