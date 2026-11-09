"""app/auth/middleware.py: the body cap and the media type on the /auth routes (Phase 3, step C).

Expected values come from the Phase 3 plan and the step C request: under /auth, for every method that is not GET, HEAD or OPTIONS, a body above 16384 bytes is 413
{"error":"The request is too large.","code":"payload_too_large"} (a Content-Length above the cap without reading the body; any other body counted on the stream and cut at
the cap so the handler never receives more), and a body that is not application/json is 415 {"error":"The request must be JSON.","code":"unsupported_media_type"}.
The size comes first. Nothing outside /auth is limited (no global cap). raw_call() drives the app like an ASGI server, so the tests can count how often the body was
asked for. Tests named pinned_* record behaviour the plan leaves open.
"""
import pytest

from gate_support import SAME, Rig, build_app, raw_call

CAP = 16384
TOO_LARGE = {"error": "The request is too large.", "code": "payload_too_large"}
NOT_JSON = {"error": "The request must be JSON.", "code": "unsupported_media_type"}
JSON_TYPE = ("Content-Type", "application/json")


@pytest.fixture
def rig(tmp_path, make_service, clock):
    return Rig(build_app(tmp_path / "static"), make_service(), clock)


@pytest.fixture
def alice(rig):
    return rig.login("alice")


def body(n):
    """Valid JSON of exactly n bytes."""
    return b'{"a":"' + b"x" * (n - 8) + b'"}'


def post(rig, path, payload, *headers, method="POST", declare=True, chunks=None):
    """Through raw_call: the headers given, plus a Content-Length of the payload unless declare is False. chunks replaces the single chunk of the payload."""
    sent = list(headers)
    if declare:
        sent.append(("Content-Length", str(len(payload))))
    return raw_call(rig.app, method, path, sent, chunks if chunks is not None else [payload])


def session_headers(session):
    return [("Cookie", f"ops_session={session.token}"), ("X-CSRF-Token", session.csrf), ("Origin", SAME)]


# --- the size: a declared length ----------------------------------------------------------------------------------------------------------------------

def test_a_body_of_exactly_the_cap_passes_and_one_more_byte_is_413_through_the_test_client(rig):
    ok = rig.client.post("/auth/echo", content=body(CAP), headers=[JSON_TYPE, ("Origin", SAME)])
    assert ok.status_code == 200 and ok.json() == {"length": CAP}
    big = rig.client.post("/auth/echo", content=body(CAP + 1), headers=[JSON_TYPE, ("Origin", SAME)])
    assert big.status_code == 413 and big.json() == TOO_LARGE
    assert [c[1] for c in rig.calls] == [CAP]  # the handler ran once, for the small one


def test_a_declared_length_above_the_cap_is_413_without_reading_the_body_at_all(rig):
    result = post(rig, "/auth/echo", body(CAP + 1), JSON_TYPE)
    assert result.status == 413 and result.receive_calls == 0 and rig.calls == []
    at_cap = post(rig, "/auth/echo", body(CAP), JSON_TYPE)
    assert at_cap.status == 200 and at_cap.receive_calls >= 1 and rig.calls == [("/auth/echo", CAP)]


@pytest.mark.parametrize("length", ["16385", "99999", "1000000000", "9" * 18, "9" * 19, "9" * 30, "9" * 400, "0016385", " 16385 "])
def test_any_declared_length_above_the_cap_is_413_before_anything_is_read(rig, length):
    result = raw_call(rig.app, "POST", "/auth/echo", [JSON_TYPE, ("Content-Length", length)], [b"{}"])
    assert result.status == 413 and result.receive_calls == 0 and rig.calls == []


@pytest.mark.parametrize("name", ["Content-Length", "content-length", "CONTENT-LENGTH"])
def test_the_length_header_is_found_in_any_case(rig, name):
    result = raw_call(rig.app, "POST", "/auth/echo", [JSON_TYPE, (name, "20000")], [b"x" * 20000])
    assert result.status == 413 and result.receive_calls == 0


@pytest.mark.parametrize("length", ["abc", "-1", "1e3", "1_000", "0x10", "12.0", "", "\xb2", "1 2"])
def test_pinned_a_length_that_is_not_a_plain_number_is_ignored_and_the_stream_is_counted_instead(rig, length):
    small = raw_call(rig.app, "POST", "/auth/echo", [JSON_TYPE, ("Content-Length", length)], [b"{}"])
    assert small.status == 200
    big = raw_call(rig.app, "POST", "/auth/echo", [JSON_TYPE, ("Content-Length", length)], [b"x" * (CAP + 1)])
    assert big.status == 413 and big.receive_calls >= 1


def test_pinned_two_length_lines_are_ignored_and_the_stream_is_counted_instead(rig):
    ok = raw_call(rig.app, "POST", "/auth/echo", [JSON_TYPE, ("Content-Length", "2"), ("Content-Length", "2")], [b"{}"])
    assert ok.status == 200
    big = raw_call(rig.app, "POST", "/auth/echo", [JSON_TYPE, ("Content-Length", "10"), ("Content-Length", "10")], [b"x" * (CAP + 1)])
    assert big.status == 413


def test_pinned_a_length_that_lies_is_caught_by_the_count(rig):
    result = raw_call(rig.app, "POST", "/auth/echo", [JSON_TYPE, ("Content-Length", "10")], [b"x" * (CAP + 1)])
    assert result.status == 413


# --- the size: a stream with no usable length ---------------------------------------------------------------------------------------------------------

def chunked(total, size):
    out, left = [], total
    while left > 0:
        out.append(b"x" * min(size, left))
        left -= size
    return out


@pytest.mark.parametrize("total, status", [(CAP, 200), (CAP + 1, 413), (CAP - 1, 200), (2 * CAP, 413)])
def test_a_streamed_body_is_counted_and_cut_at_the_cap(rig, total, status):
    result = post(rig, "/auth/echo", b"", JSON_TYPE, ("Transfer-Encoding", "chunked"), declare=False, chunks=chunked(total, 4096))
    assert result.status == status


def test_a_streamed_body_over_the_cap_stops_being_read_and_the_handler_never_sees_more_than_the_cap(rig, alice):
    chunks = chunked(80000, 5000)  # 16 chunks
    result = post(rig, "/auth/stream", b"", JSON_TYPE, ("Transfer-Encoding", "chunked"), *session_headers(alice), declare=False, chunks=chunks)
    assert result.status == 413 and result.receive_calls == 4  # 5000, 10000, 15000, then 20000 > 16384: the chunk is withheld and the rest is never asked for
    (path, detail), = rig.calls
    assert path == "/auth/stream" and detail == ("disconnected", 15000) and detail[1] <= CAP


def test_a_streamed_body_at_exactly_the_cap_is_seen_whole(rig, alice):
    result = post(rig, "/auth/stream", b"", JSON_TYPE, ("Transfer-Encoding", "chunked"), *session_headers(alice), declare=False, chunks=chunked(CAP, 4096))
    assert result.status == 200 and rig.calls == [("/auth/stream", CAP)]


def test_a_streamed_body_over_the_cap_is_413_even_when_the_handler_parses_it_with_pydantic(rig):
    result = post(rig, "/auth/model", b"", JSON_TYPE, ("Transfer-Encoding", "chunked"), declare=False, chunks=[b'{"username":"' + b"x" * 20000 + b'"}'])
    assert result.status == 413 and rig.calls == []  # not the 400 FastAPI makes of a failed read


def test_a_streamed_body_through_the_test_client_is_cut_too(rig):
    def generate(total):
        for chunk in chunked(total, 3000):
            yield chunk

    assert rig.client.post("/auth/echo", content=generate(CAP), headers=[JSON_TYPE, ("Origin", SAME)]).status_code == 200
    assert rig.client.post("/auth/echo", content=generate(CAP + 1), headers=[JSON_TYPE, ("Origin", SAME)]).status_code == 413


def test_the_answer_is_sent_once_whatever_the_handler_does_after_the_cut(rig):
    result = post(rig, "/auth/echo", b"", JSON_TYPE, declare=False, chunks=chunked(60000, 20000))
    assert result.status == 413 and result.sent == ["http.response.start", "http.response.body"]


# --- what the 413 and the 415 look like --------------------------------------------------------------------------------------------------------------------

def test_the_413_is_json_no_store_and_has_no_cookie(rig):
    result = post(rig, "/auth/echo", body(CAP + 1), JSON_TYPE)
    assert result.body == b'{"error":"The request is too large.","code":"payload_too_large"}'
    assert result.header("content-type") == ["application/json"] and result.header("cache-control") == ["no-store"]
    assert result.header("set-cookie") == [] and result.header("www-authenticate") == []


def test_the_415_is_json_no_store_and_has_no_cookie(rig):
    result = post(rig, "/auth/echo", b"{}", ("Content-Type", "text/plain"))
    assert result.status == 415 and result.body == b'{"error":"The request must be JSON.","code":"unsupported_media_type"}'
    assert result.header("content-type") == ["application/json"] and result.header("cache-control") == ["no-store"]
    assert result.header("set-cookie") == [] and result.header("www-authenticate") == []


# --- the media type -----------------------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["application/json", "application/json; charset=utf-8", "application/json;charset=UTF-8", "APPLICATION/JSON", " application/json ",
                                   "Application/Json; x=y; z=w"])
def test_json_in_any_spelling_passes(rig, value):
    assert post(rig, "/auth/echo", b"{}", ("Content-Type", value)).status == 200


@pytest.mark.parametrize("value", ["text/plain", "text/plain; charset=utf-8", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x", "application/jsonx",
                                   "application/json, text/plain", "application/vnd.api+json", "application/x-json", "json", "application", "*/*", ""])
def test_any_other_media_type_is_415_and_the_handler_never_runs(rig, value):
    result = post(rig, "/auth/echo", b"{}", ("Content-Type", value))
    assert result.status == 415 and rig.calls == []


def test_a_body_with_no_media_type_is_415(rig):
    assert post(rig, "/auth/echo", b"{}").status == 415 and rig.calls == []


def test_pinned_two_media_type_lines_are_415_even_when_both_are_json(rig):
    assert post(rig, "/auth/echo", b"{}", JSON_TYPE, JSON_TYPE).status == 415


@pytest.mark.parametrize("name", ["Content-Type", "content-type", "CONTENT-TYPE"])
def test_the_media_type_header_is_found_in_any_case(rig, name):
    assert post(rig, "/auth/echo", b"{}", (name, "application/json")).status == 200 and post(rig, "/auth/echo", b"{}", (name, "text/plain")).status == 415


def test_a_request_without_a_body_needs_no_media_type(rig, alice):
    assert raw_call(rig.app, "POST", "/auth/logout", session_headers(alice), []).status == 200  # no length, no type: logout sends neither
    other = rig.login("bob")
    assert raw_call(rig.app, "POST", "/auth/logout", session_headers(other) + [("Content-Length", "0")], []).status == 200
    assert post(rig, "/auth/echo", b"").status == 200 and rig.calls[-1] == ("/auth/echo", 0)


def test_a_chunked_body_needs_a_media_type_because_it_might_have_content(rig):
    assert post(rig, "/auth/echo", b"", ("Transfer-Encoding", "chunked"), declare=False, chunks=[b"{}"]).status == 415
    assert post(rig, "/auth/echo", b"", JSON_TYPE, ("Transfer-Encoding", "chunked"), declare=False, chunks=[b"{}"]).status == 200


def test_the_media_type_is_checked_on_session_routes_too_and_json_passes_there(rig, alice):
    assert post(rig, "/auth/stream", b"{}", ("Content-Type", "text/plain"), *session_headers(alice)).status == 415
    assert post(rig, "/auth/stream", b"{}", JSON_TYPE, *session_headers(alice)).status == 200


# --- the order -----------------------------------------------------------------------------------------------------------------------------------------------

def test_pinned_the_size_is_checked_before_the_media_type(rig):
    assert post(rig, "/auth/echo", body(CAP + 1), ("Content-Type", "text/plain")).status == 413


def test_pinned_both_limits_come_before_the_session_and_the_origin_checks(rig, alice):
    assert post(rig, "/auth/logout", body(CAP + 1), JSON_TYPE).status == 413  # anonymous
    assert post(rig, "/auth/logout", b"{}", ("Content-Type", "text/plain")).status == 415  # anonymous
    assert post(rig, "/auth/echo", body(CAP + 1), JSON_TYPE, ("Origin", "http://evil.test")).status == 413
    assert post(rig, "/auth/logout", b"{}", JSON_TYPE).status == 401  # within the limits the session check follows
    assert post(rig, "/auth/echo", b"{}", JSON_TYPE, ("Origin", "http://evil.test")).status == 403
    assert rig.calls == []


# --- which paths and methods are limited --------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("path", ["/auth/echo", "/auth/login", "/auth/anything", "/auth/a/b/c", "/auth", "/auth/"])
def test_every_unsafe_method_under_the_prefix_is_limited_even_when_no_route_exists(rig, method, path):
    assert post(rig, path, body(CAP + 1), JSON_TYPE, method=method).status == 413
    assert post(rig, path, b"{}", ("Content-Type", "text/plain"), method=method).status == 415


@pytest.mark.parametrize("path", ["/authentic", "/auth2/x", "/auth-x", "/AUTH/echo", "/xauth/echo", "/chat", "/search", "/filters", "/", "/index.html"])
def test_the_prefix_is_matched_on_whole_path_segments_and_nothing_else_is_limited(rig, alice, path):
    result = post(rig, path, body(CAP * 2), ("Content-Type", "text/plain"), *session_headers(alice))
    assert result.status not in (413, 415)


def test_pinned_there_is_no_global_cap_a_big_text_body_reaches_a_route_outside_the_prefix(rig, alice):
    assert post(rig, "/chat", body(100000), ("Content-Type", "text/plain"), *session_headers(alice)).status == 200


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_a_safe_method_is_not_limited(rig, alice, method):
    result = post(rig, "/auth/me", body(CAP * 2), ("Content-Type", "text/plain"), method=method)
    assert result.status not in (413, 415)


def test_pinned_the_limits_apply_whether_or_not_the_user_is_signed_in(rig, alice):
    for headers in ([], session_headers(alice)):
        assert post(rig, "/auth/stream", body(CAP + 1), JSON_TYPE, *headers).status == 413


def test_nothing_is_limited_when_auth_is_off(rig):
    rig.app.state.auth = None
    result = post(rig, "/auth/echo", body(CAP * 3), ("Content-Type", "text/plain"), ("Origin", "http://evil.test"))
    assert result.status == 200 and rig.calls == [("/auth/echo", CAP * 3)]
