"""Filters: category, priority, status, team and an inclusive created-date range, and the /filters options.

Written rules the expected values come from (not from running the code):
  vectorstore.build_where docstring   a single condition is returned as is; date_from / date_to are "inclusive UTC days applied
                                      to the integer created_ts field"
  ingest._unix_utc                    created_at has no timezone and is read as UTC
  schemas.Filters                     "Equality filters and an inclusive created-date range. Unset or empty means no filter."
                                      extra fields are a 422; dates are YYYY-MM-DD and real calendar dates; date_from must not
                                      be after date_to; values are stripped and at most 100 characters
  main.build_filter_options, README.md:101, schemas.FiltersResponse
                                      the distinct values, priority in P1..P4 order, created_min / created_max as YYYY-MM-DD
Every expected set is computed from data/tickets.json in plain Python (equality and string comparison of the date), never with
the code under test. Real Chroma (the temp copy from conftest.py), stubbed model. Unwritten behaviour is pinned in tests named
"pinned_". The filter-schema checks live here only; test_api_validation.py covers the top-level request fields.
"""
import calendar
import itertools
import json
import re
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas import Filters
from app.vectorstore import build_where, get_collection

ROOT = Path(__file__).resolve().parent.parent
if not (ROOT / "chroma_db").is_dir():
    pytest.skip("chroma_db/ is missing: run python scripts/ingest.py first", allow_module_level=True)

TICKETS = {t["ticket_id"]: t for t in json.loads((ROOT / "data" / "tickets.json").read_text(encoding="utf-8"))}
IDS = sorted(TICKETS, key=lambda i: int(i.split("-")[1]))
FIELDS = ("category", "priority", "status", "team")
VALUES = {f: sorted({t[f] for t in TICKETS.values()}) for f in FIELDS}
DAYS = sorted({t["created_at"][:10] for t in TICKETS.values()})
QUERY = "vpn outage"


def expected(filters):
    """The ids of the tickets that match, from the data alone. Empty values are no filter."""
    f = {k: v for k, v in filters.items() if v}
    return {
        i for i, t in TICKETS.items()
        if all(t[k] == f[k] for k in FIELDS if k in f)
        and f.get("date_from", "0000-00-00") <= t["created_at"][:10] <= f.get("date_to", "9999-12-31")
    }


def ids_for(filters):
    """Every ticket the where clause selects (no ranking, no k): this is the filter alone."""
    return set(get_collection().get(where=build_where(filters), include=[])["ids"])


def shift(day, n):
    return (date.fromisoformat(day) + timedelta(days=n)).isoformat()


def post(client, route, filters, **extra):
    body = {"query": QUERY} if route == "/search" else {"question": QUERY}
    return client.post(route, json={**body, "filters": filters, **extra})


def hit_ids(response):
    return [h["ticket_id"] for h in response.json()["hits"]]


# --- build_where: the shape -------------------------------------------------------------------------------------

@pytest.mark.parametrize("filters", [None, {}, {"category": ""}, {"category": None}, {"date_from": "", "date_to": None}],
                         ids=["None", "empty dict", "empty value", "None value", "empty dates"])
def test_nothing_to_filter_gives_no_where_clause(filters):
    assert build_where(filters) is None


def test_a_single_condition_is_not_wrapped():
    assert build_where({"category": "Facilities"}) == {"category": "Facilities"}


def test_several_conditions_are_all_required():
    where = build_where({"category": "Facilities", "team": "Facilities", "priority": "P1-Critical"})
    assert list(where) == ["$and"] and len(where["$and"]) == 3
    assert {"category": "Facilities"} in where["$and"] and {"team": "Facilities"} in where["$and"] and {"priority": "P1-Critical"} in where["$and"]


def test_the_date_conditions_are_inclusive_utc_days():
    day = "2026-03-02"
    start = calendar.timegm(date.fromisoformat(day).timetuple())  # 00:00:00 UTC
    assert build_where({"date_from": day}) == {"created_ts": {"$gte": start}}
    assert build_where({"date_to": day}) == {"created_ts": {"$lte": start + 86399}}  # up to 23:59:59 UTC
    both = build_where({"date_from": day, "date_to": day})["$and"]
    assert {"created_ts": {"$gte": start}} in both and {"created_ts": {"$lte": start + 86399}} in both


def test_the_stored_timestamp_is_the_created_at_read_as_utc():
    """ingest._unix_utc: the ticket data has no timezone and is read as UTC."""
    some = IDS[0]
    stored = get_collection().get(ids=[some], include=["metadatas"])["metadatas"][0]["created_ts"]
    created = TICKETS[some]["created_at"]
    assert stored == calendar.timegm(date.fromisoformat(created[:10]).timetuple()) + int(created[11:13]) * 3600 + int(created[14:16]) * 60 + int(created[17:19])


# --- one filter at a time, every value in the data ---------------------------------------------------------------

@pytest.mark.parametrize("field, value", [(f, v) for f in FIELDS for v in VALUES[f]], ids=[f"{f}={v}" for f in FIELDS for v in VALUES[f]])
def test_one_filter_selects_exactly_the_matching_tickets(field, value):
    want = expected({field: value})
    assert want, "every value in the data has tickets"
    assert ids_for({field: value}) == want


def test_no_filter_selects_everything():
    assert ids_for({}) == set(TICKETS) and len(TICKETS) == 800


# --- combinations --------------------------------------------------------------------------------------------------

FIRST = TICKETS[IDS[0]]
COMBOS = [{f: TICKETS[i][f] for f in FIELDS} for i in IDS[:5]]
COMBOS += [{a: FIRST[a], b: FIRST[b]} for a, b in itertools.combinations(FIELDS, 2)]
COMBOS += [{"category": FIRST["category"], "date_from": FIRST["created_at"][:7] + "-01", "date_to": shift(FIRST["created_at"][:10], 30)}]


@pytest.mark.parametrize("filters", COMBOS, ids=[",".join(sorted(c)) + f"#{n}" for n, c in enumerate(COMBOS)])
def test_combined_filters_select_the_intersection(filters):
    want = expected(filters)
    assert want, "each combination is built from a real ticket"
    assert ids_for(filters) == want


def test_a_combination_that_no_ticket_has_selects_nothing():
    pair = next((c, t) for c in VALUES["category"] for t in VALUES["team"] if not expected({"category": c, "team": t}))
    assert ids_for({"category": pair[0], "team": pair[1]}) == set()


# --- dates: inclusive at both ends, in UTC ---------------------------------------------------------------------------

LATE = max(TICKETS.values(), key=lambda t: t["created_at"][11:])    # created latest in the UTC day
EARLY = min(TICKETS.values(), key=lambda t: t["created_at"][11:])   # created earliest in the UTC day
PROBE_DAYS = sorted({DAYS[0], DAYS[-1], DAYS[len(DAYS) // 2], LATE["created_at"][:10], EARLY["created_at"][:10],
                     shift(LATE["created_at"][:10], 1), shift(EARLY["created_at"][:10], -1)})


@pytest.mark.parametrize("day", PROBE_DAYS)
def test_a_day_as_date_from_date_to_and_both(day):
    assert ids_for({"date_from": day}) == expected({"date_from": day})
    assert ids_for({"date_to": day}) == expected({"date_to": day})
    assert ids_for({"date_from": day, "date_to": day}) == expected({"date_from": day, "date_to": day})


def test_the_data_has_tickets_near_both_edges_of_a_utc_day():
    assert LATE["created_at"][11:] > "23:00:00" and EARLY["created_at"][11:] < "01:00:00", (LATE["created_at"], EARLY["created_at"])


def test_a_ticket_created_late_in_the_utc_day_belongs_to_that_day():
    day = LATE["created_at"][:10]
    assert LATE["ticket_id"] in ids_for({"date_to": day})
    assert LATE["ticket_id"] in ids_for({"date_from": day})
    assert LATE["ticket_id"] in ids_for({"date_from": day, "date_to": day})
    assert LATE["ticket_id"] not in ids_for({"date_from": shift(day, 1)})
    assert LATE["ticket_id"] not in ids_for({"date_to": shift(day, -1)})


def test_a_ticket_created_early_in_the_utc_day_belongs_to_that_day():
    day = EARLY["created_at"][:10]
    assert EARLY["ticket_id"] in ids_for({"date_from": day})
    assert EARLY["ticket_id"] in ids_for({"date_to": day})
    assert EARLY["ticket_id"] in ids_for({"date_from": day, "date_to": day})
    assert EARLY["ticket_id"] not in ids_for({"date_to": shift(day, -1)})
    assert EARLY["ticket_id"] not in ids_for({"date_from": shift(day, 1)})


def test_the_whole_data_range_selects_everything_and_outside_it_nothing():
    assert ids_for({"date_from": DAYS[0], "date_to": DAYS[-1]}) == set(TICKETS)
    assert ids_for({"date_to": shift(DAYS[0], -1)}) == set()
    assert ids_for({"date_from": shift(DAYS[-1], 1)}) == set()


def test_a_date_range_and_a_field_filter_together():
    day = LATE["created_at"][:10]
    f = {"date_from": shift(day, -20), "date_to": shift(day, 20), "status": LATE["status"]}
    assert ids_for(f) == expected(f) and LATE["ticket_id"] in ids_for(f)


# --- unknown and blank values --------------------------------------------------------------------------------------

@pytest.mark.parametrize("filters", [{"category": "Nope"}, {"priority": "P1"}, {"status": "Pending Review"}, {"team": "Nobody"}],
                         ids=["category", "priority short form", "status", "team"])
def test_an_unknown_value_selects_nothing_and_is_not_an_error(api_client, filters):
    assert ids_for(filters) == set()
    response = post(api_client, "/search", filters)
    assert response.status_code == 200 and response.json()["hits"] == []


def test_pinned_values_are_matched_exactly_including_case():
    assert ids_for({"category": VALUES["category"][0].lower()}) == set()
    assert ids_for({"category": VALUES["category"][0] + " "}) == set()


def test_an_unknown_value_in_chat_finds_nothing_and_does_not_call_the_model(api_client, stub_model):
    response = post(api_client, "/chat", {"category": "Nope"})
    body = response.json()
    assert response.status_code == 200 and body["found"] is False and body["sources"] == []
    assert body["answer"] == "No relevant tickets found for that question."
    assert stub_model.calls == []


@pytest.mark.parametrize("filters", [None, {}, {"category": ""}, {"category": "   "}, {"category": None, "date_from": None}],
                         ids=["None", "empty object", "empty value", "blank value", "None values"])
def test_empty_filters_mean_no_filter(api_client, filters):
    plain = hit_ids(api_client.post("/search", json={"query": QUERY, "k": 10}))
    assert hit_ids(post(api_client, "/search", filters, k=10)) == plain and len(plain) == 10


def test_values_are_stripped(api_client):
    value = VALUES["category"][0]
    padded = hit_ids(post(api_client, "/search", {"category": f"  {value}\t"}, k=15))
    assert padded == hit_ids(post(api_client, "/search", {"category": value}, k=15)) and padded
    assert set(padded) <= expected({"category": value})


# --- the filter is applied before the ranking (search, chat) --------------------------------------------------------

def small_pair():
    counts = Counter((t["category"], t["priority"]) for t in TICKETS.values())
    return min((n, k) for k, n in counts.items() if n >= 3)


def test_a_filter_with_fewer_than_k_matches_returns_all_of_them(api_client):
    n, (category, priority) = small_pair()
    assert 3 <= n < 15
    filters = {"category": category, "priority": priority}
    response = post(api_client, "/search", filters, k=15)
    assert response.status_code == 200
    assert sorted(hit_ids(response)) == sorted(expected(filters)) and len(hit_ids(response)) == n  # not just the matches among the top 15


@pytest.mark.parametrize("filters", [{"status": "Open"}, {"team": "Facilities", "priority": "P2-High"}, {"category": "Inventory", "date_from": "2026-01-01"}],
                         ids=["status", "team and priority", "category and date"])
def test_search_hits_all_match_the_filter(api_client, filters):
    hits = hit_ids(post(api_client, "/search", filters, k=15))
    assert hits and set(hits) <= expected(filters)


def single_ticket_day():
    per_day = Counter(t["created_at"][:10] for t in TICKETS.values())
    return next(t for i, t in sorted(TICKETS.items()) if per_day[t["created_at"][:10]] == 1 and not re.search(r"\b(?:OPS|PO|INV)-\d+\b", t["title"]))


def test_chat_with_a_date_range_that_matches_one_ticket(api_client, stub_model):
    ticket = single_ticket_day()
    day = ticket["created_at"][:10]
    assert expected({"date_from": day, "date_to": day}) == {ticket["ticket_id"]}
    response = api_client.post("/chat", json={"question": ticket["title"], "filters": {"date_from": day, "date_to": day}})
    body = response.json()
    assert response.status_code == 200
    assert [s["ticket_id"] for s in body["sources"]] == [ticket["ticket_id"]] and body["retrieved_count"] == 1
    assert len(stub_model.calls) == 1 and ticket["ticket_id"] in stub_model.calls[0][1]


# --- the filter schema ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("route", ["/search", "/chat"])
def test_date_from_after_date_to_is_rejected(api_client, stub_model, route):
    response = post(api_client, route, {"date_from": "2026-03-03", "date_to": "2026-03-02"})
    assert response.status_code == 422
    assert "date_from must not be after date_to" in json.dumps(response.json())
    assert stub_model.calls == []


def test_the_range_check_is_also_on_the_model():
    with pytest.raises(ValidationError, match="date_from must not be after date_to"):
        Filters(date_from="2026-03-03", date_to="2026-03-02")
    assert Filters(date_from="2026-03-02", date_to="2026-03-02")  # equal days are fine


@pytest.mark.parametrize("route", ["/search", "/chat"])
def test_an_unknown_field_inside_filters_is_rejected(api_client, stub_model, route):
    response = post(api_client, route, {"colour": "red"})
    assert response.status_code == 422 and "colour" in json.dumps(response.json())
    assert stub_model.calls == []


BAD_DATES = ["2026-1-5", "05/01/2026", "2026-02-30", "2026-13-01", "2026-00-10", "20260105", "2026-W01-1", " 2026-01-05",
             "2026-01-05 ", "2026-01-05T00:00:00", "yesterday", "2026-01-5", "26-01-05"]


@pytest.mark.parametrize("key", ["date_from", "date_to"])
@pytest.mark.parametrize("bad", BAD_DATES)
def test_a_date_must_be_a_real_yyyy_mm_dd(api_client, key, bad):
    response = post(api_client, "/search", {key: bad})
    assert response.status_code == 422 and key in json.dumps(response.json())


@pytest.mark.parametrize("good", ["2026-01-05", "2028-02-29", "2025-12-31"])
def test_a_real_date_is_accepted(api_client, good):
    assert post(api_client, "/search", {"date_from": good}).status_code == 200
    assert post(api_client, "/search", {"date_to": good}).status_code == 200


@pytest.mark.parametrize("key", ["date_from", "date_to"])
@pytest.mark.parametrize("blank", ["", "   "], ids=["empty", "blank"])
def test_an_empty_date_means_no_filter(api_client, key, blank):
    plain = hit_ids(api_client.post("/search", json={"query": QUERY, "k": 10}))
    response = post(api_client, "/search", {key: blank}, k=10)
    assert response.status_code == 200 and hit_ids(response) == plain


def test_a_value_may_have_100_characters_but_not_101(api_client):
    assert post(api_client, "/search", {"category": "x" * 100}).status_code == 200
    response = post(api_client, "/search", {"category": "x" * 101})
    assert response.status_code == 422 and "category" in json.dumps(response.json())
    assert post(api_client, "/search", {"team": " " * 5 + "x" * 100 + " " * 5}).status_code == 200  # the limit counts the stripped text


def test_pinned_a_filter_value_that_is_not_a_string_is_rejected_not_converted(api_client):
    for bad in (5, True, ["Facilities"], {"a": 1}):
        assert post(api_client, "/search", {"category": bad}).status_code == 422


# --- GET /filters ------------------------------------------------------------------------------------------------------

def test_the_filters_endpoint_lists_exactly_the_values_in_the_data(api_client):
    body = api_client.get("/filters").json()
    assert set(body) == {"category", "priority", "status", "team", "created_min", "created_max"}
    for field in ("category", "status", "team"):
        assert body[field] == sorted({t[field] for t in TICKETS.values()})
    assert body["priority"] == sorted({t["priority"] for t in TICKETS.values()}, key=lambda p: int(re.match(r"P(\d+)", p).group(1)))
    assert body["priority"][0].startswith("P1") and body["priority"][-1].startswith("P4")
    assert body["created_min"] == min(t["created_at"][:10] for t in TICKETS.values()) == DAYS[0]
    assert body["created_max"] == max(t["created_at"][:10] for t in TICKETS.values()) == DAYS[-1]


@pytest.mark.parametrize("field, value", [(f, v) for f in FIELDS for v in VALUES[f]], ids=[f"{f}={v}" for f in FIELDS for v in VALUES[f]])
def test_every_listed_value_selects_at_least_one_ticket(api_client, field, value):
    assert value in api_client.get("/filters").json()[field]
    assert ids_for({field: value})


def test_every_value_of_every_ticket_is_listed(api_client):
    body = api_client.get("/filters").json()
    for t in TICKETS.values():
        for field in FIELDS:
            assert t[field] in body[field]
