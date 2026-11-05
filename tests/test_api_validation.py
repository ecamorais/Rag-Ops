"""Top-level request validation of POST /chat and POST /search: question, k, history, extra fields, body shape, error bodies.

Written rules the expected values come from (not from running the code):
  README.md "Limits"       question 1 to 1000 characters; k 1 to 15 (default 6); history at most 12 turns of at most 4000
                           characters, roles user and assistant
  README.md "API", CLAUDE.md "API"
                           "Errors are {"error": "...", "code": "..."}; a 422 from request validation uses FastAPI's usual
                           `detail` list. Unknown request fields are rejected." JSON in and out.
  README.md "API" table    POST /search: body query, k, filters; POST /chat: body question, k, filters, history
  schemas.py               Text is "Trimmed first, then the length is checked, so "   " is rejected as empty."; an unknown
                           field is a 422
  CLAUDE.md "Known limits" a wrong-method request to /chat or /search is a 404 (the static mount matches first), not a 405
The filter checks (dates, values, extra fields INSIDE filters) live in test_filters.py and are not repeated here.
Real app on the temp copy of chroma_db (conftest.py), stubbed model. No 422 may reach the model: stub_model.calls == [].
Unwritten behaviour is pinned in tests named "pinned_".
"""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if not (ROOT / "chroma_db").is_dir():
    pytest.skip("chroma_db/ is missing: run python scripts/ingest.py first", allow_module_level=True)

FIELD = {"/chat": "question", "/search": "query"}
ROUTES = list(FIELD)
OTHER_FIELD = {"/chat": "query", "/search": "question"}
QUESTION = "vpn outage"
JSON_HEADERS = {"content-type": "application/json"}
SECURITY_HEADERS = ("content-security-policy", "x-content-type-options", "referrer-policy")


def body(route, **extra):
    return {FIELD[route]: QUESTION, **extra}


def post(client, route, payload):
    return client.post(route, json=payload)


def raw(client, route, content, content_type="application/json"):
    headers = {"content-type": content_type} if content_type else {"content-type": ""}
    return client.post(route, content=content, headers=headers)


def hit_ids(response):
    assert response.status_code == 200, response.text
    data = response.json()
    return [h["ticket_id"] for h in data["hits"]] if "hits" in data else [s["ticket_id"] for s in data["sources"]]


def assert_rejected(response, stub, loc=None):
    """A 422 in FastAPI's detail-list shape that never reached the model."""
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail
    if loc is not None:
        assert any(list(entry["loc"][: len(loc)]) == list(loc) for entry in detail), detail
    assert stub.calls == []


# --- the question -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("text", ["a", "a" * 1000, "é" * 1000, "日本" * 500], ids=["1 character", "1000 characters", "1000 accented characters", "1000 wide characters"])
def test_a_question_of_1_to_1000_characters_is_accepted(api_client, stub_model, route, text):
    assert post(api_client, route, {FIELD[route]: text}).status_code == 200


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("text", ["a" * 1001, "é" * 1001, "a" * 5000], ids=["1001 characters", "1001 accented characters", "5000 characters"])
def test_a_question_over_1000_characters_is_rejected(api_client, stub_model, route, text):
    assert_rejected(post(api_client, route, {FIELD[route]: text}), stub_model, ["body", FIELD[route]])


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("text", ["", " ", "     ", "\t\n\r ", "\n"], ids=["empty", "one space", "spaces", "tab newline cr space", "newline"])
def test_an_empty_or_blank_question_is_rejected(api_client, stub_model, route, text):
    assert_rejected(post(api_client, route, {FIELD[route]: text}), stub_model, ["body", FIELD[route]])


@pytest.mark.parametrize("route", ROUTES)
def test_the_question_is_trimmed_before_the_length_check(api_client, stub_model, route):
    assert post(api_client, route, {FIELD[route]: " " * 5 + "a" * 1000 + " " * 5}).status_code == 200
    assert_rejected(post(api_client, route, {FIELD[route]: " " + "a" * 1001 + " "}), stub_model, ["body", FIELD[route]])


@pytest.mark.parametrize("route", ROUTES)
def test_surrounding_spaces_do_not_change_what_is_retrieved(api_client, stub_model, route):
    plain = hit_ids(post(api_client, route, body(route, k=10)))
    padded = hit_ids(post(api_client, route, {FIELD[route]: f"  \t{QUESTION}\n ", "k": 10}))
    assert plain and padded == plain


@pytest.mark.parametrize("route", ROUTES)
def test_a_missing_question_is_rejected(api_client, stub_model, route):
    assert_rejected(post(api_client, route, {}), stub_model, ["body", FIELD[route]])
    assert_rejected(post(api_client, route, {"k": 3}), stub_model, ["body", FIELD[route]])


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("value", [None, 5, 1.5, True, ["vpn"], {"a": 1}], ids=["null", "int", "float", "bool", "list", "object"])
def test_a_question_that_is_not_a_string_is_rejected(api_client, stub_model, route, value):
    assert_rejected(post(api_client, route, {FIELD[route]: value}), stub_model, ["body", FIELD[route]])


# --- k ------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("k", [1, 2, 6, 14, 15])
def test_k_from_1_to_15_is_accepted(api_client, stub_model, route, k):
    assert post(api_client, route, body(route, k=k)).status_code == 200


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("k", [0, -1, 16, 100, 1000, 10 ** 20])
def test_k_outside_1_to_15_is_rejected(api_client, stub_model, route, k):
    assert_rejected(post(api_client, route, body(route, k=k)), stub_model, ["body", "k"])


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("k", [None, 3.5, "abc", "", [3], {"k": 3}], ids=["null", "fraction", "word", "empty string", "list", "object"])
def test_k_that_is_not_a_whole_number_is_rejected(api_client, stub_model, route, k):
    assert_rejected(post(api_client, route, body(route, k=k)), stub_model, ["body", "k"])


def test_search_returns_k_hits_and_six_by_default(api_client, stub_model):
    assert len(hit_ids(post(api_client, "/search", body("/search")))) == 6
    for k in (1, 3, 15):
        assert len(hit_ids(post(api_client, "/search", body("/search", k=k)))) == k


def test_chat_never_returns_more_sources_than_k_and_six_is_the_default(api_client, stub_model):
    default = hit_ids(post(api_client, "/chat", body("/chat")))
    assert 0 < len(default) <= 6
    assert default == hit_ids(post(api_client, "/chat", body("/chat", k=6)))
    for k in (1, 3, 15):
        response = post(api_client, "/chat", body("/chat", k=k))
        sources = hit_ids(response)
        assert 0 < len(sources) <= k and response.json()["retrieved_count"] == len(sources)


# --- fields that are not part of the request ------------------------------------------------------------------------

@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("name", ["x", "limit", "top_k", "Question", "K", "model"])
def test_an_unknown_top_level_field_is_rejected(api_client, stub_model, route, name):
    assert_rejected(post(api_client, route, body(route, **{name: 1})), stub_model, ["body", name])


@pytest.mark.parametrize("route", ROUTES)
def test_the_other_routes_question_field_is_not_accepted(api_client, stub_model, route):
    response = post(api_client, route, {OTHER_FIELD[route]: QUESTION})
    assert_rejected(response, stub_model, ["body", OTHER_FIELD[route]])
    types = {entry["type"] for entry in response.json()["detail"]}
    assert {"extra_forbidden", "missing"} <= types  # the unknown field and the missing question are both reported


def test_search_has_no_history_field(api_client, stub_model):
    assert_rejected(post(api_client, "/search", body("/search", history=[])), stub_model, ["body", "history"])


# --- history (/chat only) ------------------------------------------------------------------------------------------------

def turns(n, role="user", content="earlier question"):
    return [{"role": role, "content": content} for _ in range(n)]


@pytest.mark.parametrize("n", [0, 1, 11, 12])
def test_a_history_of_up_to_12_turns_is_accepted(api_client, stub_model, n):
    assert post(api_client, "/chat", body("/chat", history=turns(n))).status_code == 200


@pytest.mark.parametrize("n", [13, 14, 100])
def test_a_history_of_more_than_12_turns_is_rejected(api_client, stub_model, n):
    assert_rejected(post(api_client, "/chat", body("/chat", history=turns(n))), stub_model, ["body", "history"])


def test_the_history_may_be_left_out_or_null(api_client, stub_model):
    assert post(api_client, "/chat", body("/chat")).status_code == 200
    assert post(api_client, "/chat", body("/chat", history=None)).status_code == 200


@pytest.mark.parametrize("value", ["x", 5, {"role": "user", "content": "a"}, True], ids=["string", "int", "object", "bool"])
def test_a_history_that_is_not_a_list_is_rejected(api_client, stub_model, value):
    assert_rejected(post(api_client, "/chat", body("/chat", history=value)), stub_model, ["body", "history"])


@pytest.mark.parametrize("size", [1, 4000])
def test_a_history_turn_of_1_to_4000_characters_is_accepted(api_client, stub_model, size):
    assert post(api_client, "/chat", body("/chat", history=turns(1, content="a" * size))).status_code == 200


@pytest.mark.parametrize("content", ["a" * 4001, "é" * 4001, ""], ids=["4001 characters", "4001 accented characters", "empty"])
def test_a_history_turn_longer_than_4000_or_empty_is_rejected(api_client, stub_model, content):
    assert_rejected(post(api_client, "/chat", body("/chat", history=turns(1, content=content))), stub_model, ["body", "history", 0, "content"])


@pytest.mark.parametrize("role", ["user", "assistant"])
def test_the_roles_user_and_assistant_are_accepted(api_client, stub_model, role):
    assert post(api_client, "/chat", body("/chat", history=turns(2, role=role))).status_code == 200


@pytest.mark.parametrize("role", ["system", "tool", "User", "ASSISTANT", " user", "", None, 1],
                         ids=["system", "tool", "capitalised user", "upper-case assistant", "padded user", "empty", "null", "int"])
def test_any_other_role_is_rejected(api_client, stub_model, role):
    assert_rejected(post(api_client, "/chat", body("/chat", history=[{"role": role, "content": "a"}])), stub_model, ["body", "history", 0, "role"])


@pytest.mark.parametrize("item", [{"content": "a"}, {"role": "user"}, {}, "a", 5, None, ["user", "a"], {"role": "user", "content": "a", "x": 1}],
                         ids=["no role", "no content", "empty object", "string", "int", "null", "list", "extra field"])
def test_a_malformed_history_turn_is_rejected(api_client, stub_model, item):
    assert_rejected(post(api_client, "/chat", body("/chat", history=[item])), stub_model, ["body", "history", 0])


def test_the_bad_turn_is_named_by_its_position(api_client, stub_model):
    history = turns(3) + [{"role": "system", "content": "a"}] + turns(1)
    assert_rejected(post(api_client, "/chat", body("/chat", history=history)), stub_model, ["body", "history", 3, "role"])


# --- the shape of the body and the content type ---------------------------------------------------------------------------

BAD_BODIES = [b"{bad", b"{", b'{"question": ', b"", b"[1]", b'"hi"', b"null", b"5", b"true", b"[]"]
BAD_BODY_IDS = ["broken json", "open brace", "cut off", "empty", "list", "string", "null", "number", "bool", "empty list"]


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("content", BAD_BODIES, ids=BAD_BODY_IDS)
def test_a_body_that_is_not_a_json_object_is_rejected(api_client, stub_model, route, content):
    assert_rejected(raw(api_client, route, content), stub_model, ["body"])


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("content_type", ["text/plain", "text/html", "application/x-www-form-urlencoded", "application/xml", ""],
                         ids=["text/plain", "text/html", "form", "xml", "none"])
def test_valid_json_with_another_content_type_is_rejected(api_client, stub_model, route, content_type):
    assert_rejected(raw(api_client, route, json.dumps(body(route)).encode(), content_type), stub_model, ["body"])


@pytest.mark.parametrize("route", ROUTES)
def test_form_data_is_rejected(api_client, stub_model, route):
    assert_rejected(api_client.post(route, data={FIELD[route]: QUESTION}), stub_model, ["body"])


@pytest.mark.parametrize("route", ROUTES)
def test_json_with_a_charset_parameter_is_accepted(api_client, stub_model, route):
    assert raw(api_client, route, json.dumps(body(route)).encode(), "application/json; charset=utf-8").status_code == 200


@pytest.mark.parametrize("route", ROUTES)
def test_filters_that_are_not_an_object_are_rejected(api_client, stub_model, route):
    assert_rejected(post(api_client, route, body(route, filters="x")), stub_model, ["body", "filters"])


# --- what an error looks like (README "API": errors are {error, code}; a 422 uses FastAPI's detail list) -----------------------------

@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("payload", [{}, {"k": 0}, {"x": 1}], ids=["missing question", "k out of range", "unknown field"])
def test_a_422_has_the_detail_list_and_no_error_or_code(api_client, stub_model, route, payload):
    response = post(api_client, route, {**({FIELD[route]: QUESTION} if payload != {} else {}), **payload})
    assert response.status_code == 422 and response.headers["content-type"].startswith("application/json")
    data = response.json()
    assert list(data) == ["detail"] and isinstance(data["detail"], list) and data["detail"]
    for entry in data["detail"]:
        assert {"type", "loc", "msg"} <= set(entry) and isinstance(entry["loc"], list) and isinstance(entry["msg"], str)
    assert "error" not in data and "code" not in data


def test_an_unknown_ticket_is_a_404_with_error_and_code(api_client):
    response = api_client.get("/tickets/OPS-999999")
    assert response.status_code == 404 and response.headers["content-type"].startswith("application/json")
    data = response.json()
    assert set(data) == {"error", "code"} and isinstance(data["error"], str) and isinstance(data["code"], str)


def test_a_malformed_ticket_id_is_a_422_with_the_detail_list(api_client):
    response = api_client.get("/tickets/abc")
    assert response.status_code == 422 and list(response.json()) == ["detail"]


def test_an_unknown_path_is_a_404_with_error_and_code(api_client):
    response = api_client.get("/no-such-route")
    assert response.status_code == 404 and set(response.json()) == {"error", "code"}


@pytest.mark.parametrize("method, path", [("post", "/health"), ("delete", "/health"), ("put", "/chat"), ("delete", "/search"),
                                          ("post", "/filters"), ("post", "/tickets/OPS-1001")])
def test_a_wrong_method_on_an_api_route_is_a_405_with_error_and_code(api_client, method, path):
    response = getattr(api_client, method)(path)
    assert response.status_code == 405 and response.headers["content-type"].startswith("application/json")
    data = response.json()
    assert set(data) == {"error", "code"} and isinstance(data["error"], str) and isinstance(data["code"], str)


@pytest.mark.parametrize("path", ["/chat", "/search"])
def test_pinned_get_on_a_post_route_is_a_404_not_a_405(api_client, path):
    """CLAUDE.md, known limits: the static mount at / matches first. If this is ever fixed, change the test with it."""
    response = api_client.get(path)
    assert response.status_code == 404 and set(response.json()) == {"error", "code"}


def test_pinned_the_error_codes_are_not_found_and_method_not_allowed(api_client):
    """No document names the code strings; they are what the clients and the UI see today (app/main.py http_error_handler)."""
    assert api_client.get("/tickets/OPS-999999").json()["code"] == "not_found"
    assert api_client.get("/no-such-route").json()["code"] == "not_found"
    assert api_client.post("/health").json()["code"] == "method_not_allowed"


def test_errors_carry_the_security_headers(api_client, stub_model):
    for response in (post(api_client, "/chat", {}), api_client.get("/tickets/OPS-999999"), api_client.post("/health")):
        assert response.status_code in (404, 405, 422)
        for name in SECURITY_HEADERS:
            assert name in response.headers, (response.status_code, name)


# --- pinned: behaviour no written rule covers -----------------------------------------------------------------------------------

@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("k, same_as", [("3", 3), (3.0, 3), (True, 1)], ids=["string 3", "float 3.0", "true"])
def test_pinned_k_is_converted_not_type_checked(api_client, stub_model, route, k, same_as):
    """The README says k is 1 to 15, not that it must be an integer. FastAPI's lax mode converts these."""
    converted = hit_ids(post(api_client, route, body(route, k=k)))
    assert converted == hit_ids(post(api_client, route, body(route, k=same_as)))


def test_pinned_a_history_turn_of_only_spaces_is_accepted(api_client, stub_model):
    """HistoryTurn.content has min_length=1 and no strip (only Text strips), so "   " passes while a blank question does not."""
    assert post(api_client, "/chat", body("/chat", history=turns(1, content="    "))).status_code == 200
    assert post(api_client, "/chat", {"question": "    "}).status_code == 422


def test_pinned_a_422_echoes_the_input_it_rejected(api_client, stub_model):
    response = post(api_client, "/chat", {"question": "a" * 1001})
    assert [entry["input"] for entry in response.json()["detail"]] == ["a" * 1001]


def test_pinned_a_null_history_is_the_same_as_none(api_client, stub_model):
    with_null = hit_ids(post(api_client, "/chat", body("/chat", history=None)))
    assert with_null == hit_ids(post(api_client, "/chat", body("/chat"))) == hit_ids(post(api_client, "/chat", body("/chat", history=[])))


# =====================================================================================================================================
# A lone surrogate in a request body
# The rule: a request that fails validation is answered with 422 and FastAPI's {"detail": [...]} list (README.md "API", CLAUDE.md "API":
# "a 422 from request validation uses FastAPI's usual `detail` list"). A JSON string such as "\ud800" (a lone UTF-16 surrogate) is not valid
# text: pydantic rejects it, and the 422 body echoes the rejected input, which UTF-8 cannot encode. Before app/main.py got its
# validation_error_handler every such request ended in a 500 {"error": "Internal error", "code": "internal_error"} instead of the 422.
# The 422 body of every OTHER invalid input must stay exactly what FastAPI's own default handler returns: the expected bytes below are computed
# by calling fastapi.exception_handlers.request_validation_exception_handler itself (on a small reference app with the same request models),
# never from app/main.py. The surrogates are written as JSON escapes in raw strings, so no lone surrogate character is stored in this file.
# =====================================================================================================================================

SURROGATE_CASES = [  # (id, route, JSON text, the loc the bad value is reported at (None: no check), the rejected string as Python sees it)
    ("question high", "/chat", r'{"question": "a\ud800b"}', ["body", "question"], "a\ud800b"),
    ("question low", "/chat", r'{"question": "a\udc00b"}', ["body", "question"], "a\udc00b"),
    ("search query", "/search", r'{"query": "a\ud800b"}', ["body", "query"], "a\ud800b"),
    ("history content", "/chat", r'{"question": "q", "history": [{"role": "user", "content": "x\ud800y"}]}', ["body", "history", 0, "content"], "x\ud800y"),
    ("history role", "/chat", r'{"question": "q", "history": [{"role": "x\ud800", "content": "y"}]}', ["body", "history", 0, "role"], "x\ud800"),
    ("filters category", "/chat", r'{"question": "q", "filters": {"category": "\ud800"}}', ["body", "filters", "category"], "\ud800"),
    ("filters team", "/search", r'{"query": "q", "filters": {"team": "a\ud800"}}', ["body", "filters", "team"], "a\ud800"),
    ("filters status", "/chat", r'{"question": "q", "filters": {"status": "\udc00"}}', ["body", "filters", "status"], "\udc00"),
    ("filters date_from", "/chat", r'{"question": "q", "filters": {"date_from": "\ud800"}}', ["body", "filters", "date_from"], "\ud800"),
    ("filters date_to", "/search", r'{"query": "q", "filters": {"date_to": "2026-01-01\ud800"}}', ["body", "filters", "date_to"], "2026-01-01\ud800"),
    ("field name on chat", "/chat", r'{"question": "q", "\ud800": 1}', None, None),
    ("field name on search", "/search", r'{"query": "q", "\ud800": 1}', None, None),
]
SURROGATE_IDS = [case[0] for case in SURROGATE_CASES]
SURROGATE_VALUE_CASES = [case for case in SURROGATE_CASES if case[4] is not None]


def send_surrogate(client, route, text):
    return raw(client, route, text.encode("ascii"))


def assert_no_real_client():
    from app import llm

    assert llm._client is None  # conftest.py also fails the test if anything asked for a real client


@pytest.mark.parametrize("name, route, text, loc, original", SURROGATE_CASES, ids=SURROGATE_IDS)
def test_a_lone_surrogate_is_a_422_with_the_detail_list_and_never_reaches_the_model(api_client, stub_model, name, route, text, loc, original):
    response = send_surrogate(api_client, route, text)
    assert_rejected(response, stub_model, loc)
    assert response.headers["content-type"].startswith("application/json")
    payload = response.json()  # the body is valid JSON
    assert set(payload) == {"detail"}
    assert all({"type", "loc", "msg"} <= set(entry) for entry in payload["detail"])
    assert_no_real_client()


@pytest.mark.parametrize("name, route, text, loc, original", SURROGATE_CASES, ids=SURROGATE_IDS)
def test_the_422_for_a_lone_surrogate_carries_the_security_headers(api_client, stub_model, name, route, text, loc, original):
    """CLAUDE.md "API": every response except the docs carries them, the 422 included."""
    response = send_surrogate(api_client, route, text)
    assert response.status_code == 422
    for header in SECURITY_HEADERS:
        assert header in response.headers, header
    assert response.headers["x-content-type-options"] == "nosniff" and response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["content-security-policy"].startswith("default-src 'none'")


@pytest.mark.parametrize("name, route, text, loc, original", SURROGATE_VALUE_CASES, ids=[case[0] for case in SURROGATE_VALUE_CASES])
def test_pinned_the_422_echoes_the_surrogate_string_it_rejected(api_client, stub_model, name, route, text, loc, original):
    """Unwritten behaviour, not a deviation: the input is echoed, as for every other 422 (see test_pinned_a_422_echoes_the_input_it_rejected).
    After decoding the JSON the echoed input is the original string, surrogate included."""
    inputs = [entry.get("input") for entry in send_surrogate(api_client, route, text).json()["detail"]]
    assert original in inputs


@pytest.mark.parametrize("name, route, text, loc, original", SURROGATE_VALUE_CASES, ids=[case[0] for case in SURROGATE_VALUE_CASES])
def test_pinned_the_surrogate_is_written_as_an_ascii_escape_in_the_body(api_client, stub_model, name, route, text, loc, original):
    """Unwritten behaviour, not a deviation: UTF-8 cannot hold the character, so the body carries it as the escape \\udXXX (plain ASCII)."""
    content = send_surrogate(api_client, route, text).content
    surrogate = next(c for c in original if 0xD800 <= ord(c) <= 0xDFFF)
    assert content.isascii() and ("\\u%04x" % ord(surrogate)).encode() in content


@pytest.mark.parametrize("name, route, text, loc, original", SURROGATE_CASES, ids=SURROGATE_IDS)
def test_pinned_the_body_is_compact_json_like_every_other_422(api_client, stub_model, name, route, text, loc, original):
    """Unwritten behaviour, not a deviation: FastAPI's own 422 bodies have no spaces after "," and ":". The body for a lone surrogate keeps that
    form, so parsing it and writing it again the same way (ASCII escapes, compact) gives back the very same bytes."""
    content = send_surrogate(api_client, route, text).content
    assert json.dumps(json.loads(content), separators=(",", ":")).encode("ascii") == content


def test_the_app_still_answers_after_a_lone_surrogate_request(api_client, stub_model):
    for name, route, text, loc, original in SURROGATE_CASES[:3]:
        assert send_surrogate(api_client, route, text).status_code == 422
    assert api_client.get("/health").status_code == 200
    assert post(api_client, "/search", body("/search")).status_code == 200


PAIR = "\\u%04x\\u%04x" % (0xD83D, 0xDE00)  # the JSON escape of ONE emoji (a valid surrogate pair), built here so that no pair is written out


@pytest.mark.parametrize("text", [r'{"question": "a\u0000b"}', '{"question": "%s"}' % PAIR, '{"question": "vpn %s outage"}' % PAIR],
                         ids=["NUL", "emoji pair", "emoji pair in a sentence"])
def test_pinned_a_nul_or_an_emoji_pair_in_a_question_is_still_accepted(api_client, stub_model, text):
    """Unwritten behaviour, not a deviation. A valid surrogate PAIR is one character (an emoji), not a lone surrogate, and NUL is valid
    JSON text: both are accepted (200) as before the handler existed."""
    response = send_surrogate(api_client, "/chat", text)
    assert response.status_code == 200 and "answer" in response.json()


# The 422 body of every input WITHOUT a lone surrogate must be byte-identical to FastAPI's own default handler.

@pytest.fixture(scope="module")
def reference():
    """A reference app with the same request models and FastAPI's own request_validation_exception_handler (called directly, through a
    recorder). Returns (client, responses): the handler's response for every request the reference app rejected."""
    from fastapi import FastAPI
    from fastapi.exception_handlers import request_validation_exception_handler
    from fastapi.exceptions import RequestValidationError
    from fastapi.testclient import TestClient

    from app.schemas import ChatRequest, SearchRequest

    app = FastAPI()
    responses = []

    @app.exception_handler(RequestValidationError)
    async def default_handler(request, error):
        response = await request_validation_exception_handler(request, error)
        responses.append(response)
        return response

    @app.post("/chat")
    def chat_route(payload: ChatRequest):
        return {}

    @app.post("/search")
    def search_route(payload: SearchRequest):
        return {}

    with TestClient(app) as client:
        yield client, responses


def as_json(value):
    return json.dumps(value).encode("utf-8")


NON_SURROGATE_INPUTS = [  # (id, route, body bytes, a byte string the 422 body must contain, or None)
    ("empty question", "/chat", as_json({"question": ""}), None),
    ("blank question", "/chat", as_json({"question": "   "}), None),
    ("long question", "/chat", as_json({"question": "x" * 1001}), None),
    ("search empty query", "/search", as_json({"query": ""}), None),
    ("search missing query", "/search", b"{}", None),
    ("k too small", "/chat", as_json({"question": "q", "k": 0}), None),
    ("k too large", "/search", as_json({"query": "q", "k": 99}), None),
    ("bad role", "/chat", as_json({"question": "q", "history": [{"role": "system", "content": "x"}]}), None),
    ("extra field", "/chat", as_json({"question": "q", "extra": 1}), None),
    ("bad date", "/chat", as_json({"question": "q", "filters": {"date_from": "2026-13-45"}}), None),
    ("dates out of order", "/search", as_json({"query": "q", "filters": {"date_from": "2026-02-01", "date_to": "2026-01-01"}}), None),
    ("accents echoed", "/chat", json.dumps({"question": "q", "filters": {"date_from": "héllo"}}, ensure_ascii=False).encode("utf-8"),
     "héllo".encode("utf-8")),
    ("emoji echoed", "/chat", json.dumps({"question": "q", "filters": {"date_from": "\U0001f600"}}, ensure_ascii=False).encode("utf-8"),
     "\U0001f600".encode("utf-8")),
    ("emoji pair as escapes echoed", "/search", ('{"query": "q", "filters": {"date_to": "%s"}}' % PAIR).encode("ascii"), "\U0001f600".encode("utf-8")),
    ("NUL echoed", "/chat", rb'{"question": "q", "filters": {"date_from": "a\u0000b"}}', rb"a\u0000b"),
    ("not JSON", "/chat", b"{not json", None),
    ("not JSON, search", "/search", b"", None),
    ("list body", "/chat", b"[1, 2]", None),
    ("string body", "/search", b'"text"', None),
]


@pytest.mark.parametrize("name, route, content, needle", NON_SURROGATE_INPUTS, ids=[case[0] for case in NON_SURROGATE_INPUTS])
def test_the_422_for_any_other_invalid_input_is_byte_identical_to_fastapis_default(api_client, stub_model, reference, name, route, content, needle):
    ref_client, ref_responses = reference
    before = len(ref_responses)
    ref_client.post(route, content=content, headers=JSON_HEADERS)
    assert len(ref_responses) == before + 1, "the reference app did not reject this input"
    expected = ref_responses[-1]
    assert expected.status_code == 422
    if needle is not None:
        assert needle in expected.body, "the check would be vacuous: the expected body does not echo the characters this case is about"
    actual = raw(api_client, route, content)
    assert actual.status_code == expected.status_code == 422
    assert actual.content == bytes(expected.body)
    assert actual.headers["content-type"] == expected.headers["content-type"]
    assert stub_model.calls == []


# A non-finite number in the body (NaN, Infinity, -Infinity, or 1e999, which Python reads as infinity). Finding F10, fixed: the 422 body echoes the
# number, JSON cannot hold it and Starlette's JSONResponse refuses it (allow_nan=False, ValueError), which used to answer a 500 instead of the
# 422 the rule above asks for. The expected values come from the rule (422, detail list, valid JSON); the echoed strings are pinned_.

def strict_json(text):
    """Parses like a strict JSON reader: a bare NaN, Infinity or -Infinity is an error."""
    def refuse(constant):
        raise ValueError("not valid JSON: bare " + constant)

    return json.loads(text, parse_constant=refuse)


NONFINITE_CASES = [  # (id, route, body, expected loc, expected echoed input)
    ("k NaN", "/chat", b'{"question": "q", "k": NaN}', ["body", "k"], "NaN"),
    ("k Infinity", "/chat", b'{"question": "q", "k": Infinity}', ["body", "k"], "Infinity"),
    ("k -Infinity", "/search", b'{"query": "q", "k": -Infinity}', ["body", "k"], "-Infinity"),
    ("k 1e999", "/chat", b'{"question": "q", "k": 1e999}', ["body", "k"], "Infinity"),
    ("filter NaN", "/chat", b'{"question": "q", "filters": {"category": NaN}}', ["body", "filters", "category"], "NaN"),
    ("question NaN", "/chat", b'{"question": NaN}', ["body", "question"], "NaN"),
    ("history content Infinity", "/chat", b'{"question": "q", "history": [{"role": "user", "content": Infinity}]}',
     ["body", "history", 0, "content"], "Infinity"),
    ("history extra field NaN", "/chat", b'{"question": "q", "history": [{"role": "user", "content": "x", "extra": NaN}]}',
     ["body", "history", 0, "extra"], "NaN"),
    ("extra field -Infinity", "/search", b'{"query": "q", "x": -Infinity}', ["body", "x"], "-Infinity"),
    ("question is a list with NaN", "/chat", b'{"question": [1, [NaN, -Infinity], {"a": Infinity}]}', ["body", "question"],
     [1, ["NaN", "-Infinity"], {"a": "Infinity"}]),
    ("question is a dict with NaN", "/chat", b'{"question": {"a": {"b": [NaN, 1e999]}}}', ["body", "question"], {"a": {"b": ["NaN", "Infinity"]}}),
    ("filters is a list of dicts with NaN", "/chat", b'{"question": "q", "filters": [{"x": [NaN]}]}', ["body", "filters"], [{"x": ["NaN"]}]),
    ("deep list with NaN inside", "/chat", ('{"question": ' + "[" * 300 + "NaN" + "]" * 300 + "}").encode("ascii"), ["body", "question"], None),
    ("bare NaN body", "/chat", b"NaN", ["body"], "NaN"),
    ("bare -Infinity body", "/search", b"-Infinity", ["body"], "-Infinity"),
]
NONFINITE_IDS = [case[0] for case in NONFINITE_CASES]
NONFINITE_VALUE_CASES = [case for case in NONFINITE_CASES if case[4] is not None]


@pytest.mark.parametrize("name, route, content, loc, echoed", NONFINITE_CASES, ids=NONFINITE_IDS)
def test_a_non_finite_number_in_the_body_is_a_422_with_the_detail_list(api_client, stub_model, name, route, content, loc, echoed):
    response = raw(api_client, route, content)
    assert_rejected(response, stub_model, loc)
    assert response.headers["content-type"].startswith("application/json")
    payload = strict_json(response.text)  # valid JSON for a strict reader: no bare NaN or Infinity
    assert set(payload) == {"detail"}
    assert all({"type", "loc", "msg"} <= set(entry) for entry in payload["detail"])
    assert_no_real_client()


@pytest.mark.parametrize("name, route, content, loc, echoed", NONFINITE_CASES, ids=NONFINITE_IDS)
def test_the_422_for_a_non_finite_number_carries_the_security_headers(api_client, stub_model, name, route, content, loc, echoed):
    response = raw(api_client, route, content)
    assert response.status_code == 422
    for header in SECURITY_HEADERS:
        assert header in response.headers, header
    assert response.headers["x-content-type-options"] == "nosniff" and response.headers["referrer-policy"] == "no-referrer"
    assert stub_model.calls == []


@pytest.mark.parametrize("name, route, content, loc, echoed", NONFINITE_VALUE_CASES, ids=[case[0] for case in NONFINITE_VALUE_CASES])
def test_pinned_a_non_finite_number_is_echoed_as_its_name_in_a_string(api_client, stub_model, name, route, content, loc, echoed):
    """Unwritten behaviour, not a deviation (finding F10): JSON has no NaN or Infinity, so the echoed input carries the strings "NaN", "Infinity"
    and "-Infinity" (at any depth), and the sign is kept. 1e999 is read as infinity before the app sees it, so it is echoed as "Infinity"."""
    payload = strict_json(raw(api_client, route, content).text)
    assert any(entry["loc"] == loc and entry["input"] == echoed for entry in payload["detail"]), payload


def test_pinned_a_nested_non_finite_number_is_a_string_at_every_depth(api_client, stub_model):
    """Unwritten behaviour, not a deviation (finding F10). Only the innermost value changes; the structure around it is kept."""
    payload = strict_json(raw(api_client, "/chat", ('{"question": ' + "[" * 300 + "NaN" + "]" * 300 + "}").encode("ascii")).text)
    value = payload["detail"][0]["input"]
    for _ in range(300):
        assert isinstance(value, list) and len(value) == 1
        value = value[0]
    assert value == "NaN"


@pytest.mark.parametrize("text, loc", [
    (r'{"question": "a\ud800", "k": NaN}', [["body", "question"], ["body", "k"]]),
    (r'{"question": ["\ud800", NaN]}', [["body", "question"]]),
    (r'{"question": "q", "filters": {"category": "\ud800", "team": NaN}}', [["body", "filters", "category"], ["body", "filters", "team"]]),
], ids=["two fields", "one list", "two filters"])
def test_a_lone_surrogate_and_a_non_finite_number_in_one_request_are_one_422(api_client, stub_model, text, loc):
    response = send_surrogate(api_client, "/chat", text)
    assert_rejected(response, stub_model)
    assert response.headers["content-type"].startswith("application/json")
    payload = strict_json(response.text)
    assert set(payload) == {"detail"}
    for expected in loc:
        assert any(entry["loc"] == expected for entry in payload["detail"]), (expected, payload)
    response.content.decode("ascii")  # the body is plain ASCII: the surrogate is an escape


def test_a_4000_digit_integer_is_still_an_ordinary_422(api_client, stub_model):
    response = raw(api_client, "/chat", b'{"question": "q", "k": ' + b"9" * 4000 + b"}")
    assert_rejected(response, stub_model, ["body", "k"])
    assert response.headers["content-type"].startswith("application/json")


def test_pinned_a_5000_digit_integer_is_fastapis_own_400(api_client, stub_model):
    """Unwritten behaviour, not a deviation: Python refuses to read an integer of more than 4300 digits, and FastAPI answers 400 itself
    (the body cannot be parsed) before any validation. The handler for 422 is not involved."""
    for content in (b'{"question": "q", "k": ' + b"9" * 5000 + b"}", b'{"question": [' + b"9" * 5000 + b"]}"):
        response = raw(api_client, "/chat", content)
        assert response.status_code == 400
        assert set(response.json()) == {"error", "code"}
    assert stub_model.calls == []


def test_pinned_the_fallback_never_writes_a_bare_nan_even_if_the_sanitising_missed_one(api_client, stub_model, monkeypatch):
    """Unwritten behaviour, not a deviation: the fallback body is written with allow_nan=False as a second guard. If the replacement of
    non-finite numbers did nothing, the app answers its generic 500 and never a 422 whose body holds a bare NaN (which is not JSON)."""
    from app import main

    monkeypatch.setattr(main, "_finite", lambda value: value)
    response = raw(api_client, "/chat", b'{"question": "q", "k": NaN}')
    assert response.status_code == 500
    assert response.json() == {"error": "Internal error", "code": "internal_error"}
    assert stub_model.calls == []


def test_pinned_only_a_value_error_is_swallowed_by_the_validation_handler(api_client, stub_model, monkeypatch):
    """Unwritten behaviour, not a deviation. The handler falls back to the escaped body only for a ValueError (a lone surrogate, NaN or Infinity).
    Any other error from the response class (here a TypeError) is not hidden: the app answers its generic 500 internal_error."""
    from app import main

    class Failing(main.JSONResponse):
        def __init__(self, content=None, status_code=200, *args, **kwargs):
            if status_code == 422:
                raise TypeError("not a ValueError")
            super().__init__(content, status_code, *args, **kwargs)

    monkeypatch.setattr(main, "JSONResponse", Failing)
    response = raw(api_client, "/chat", b'{"question": ""}')
    assert response.status_code == 500
    assert response.json() == {"error": "Internal error", "code": "internal_error"}
    assert stub_model.calls == []
