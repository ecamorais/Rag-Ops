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
