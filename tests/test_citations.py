"""Citation validation in app.rag.answer_question: what the model cites is checked against what was retrieved.

Written rules the expected values come from (not from running the code):
  README.md:39   "The model's cited ids are kept only if they are in the retrieved set. Ids it mentions that were not retrieved
                  are returned as `unverified_citations`."
  prompt.py:29   citations: "the list of ticket ids you cited (only ids from the provided tickets)"
  prompt.py:16   "If the tickets do not contain the answer, set found=false"
  schemas.py     ChatResponse fields; Source.score is None for a ticket fetched by exact reference
  rag.py         `_result`: cited tickets first in sources ("stable: cited first"); the note "Not found in the ticket data: ..."
                 in front of the answer for missing references
What is in the retrieved set is read from the prompt the stub model received (its <ticket id="..."> blocks), not from the result.

Behaviour that NO written rule covers is pinned in the tests whose names start with "pinned_": upper-casing and stripping of
ids, removal of duplicates, citations emptied when found=false, ids named only inline in the answer text. If one of those is
wrong, change the test together with the code.
Real retrieval on the temp copy of chroma_db (conftest.py), stubbed model.
"""
import json
import re
from pathlib import Path

import pytest

from app import rag

ROOT = Path(__file__).resolve().parent.parent
if not (ROOT / "chroma_db").is_dir():
    pytest.skip("chroma_db/ is missing: run python scripts/ingest.py first", allow_module_level=True)

TICKETS = {t["ticket_id"]: t for t in json.loads((ROOT / "data" / "tickets.json").read_text(encoding="utf-8"))}
IDS = sorted(TICKETS, key=lambda i: int(i.split("-")[1]))
A = IDS[0]
MISSING = f"OPS-{int(IDS[-1].split('-')[1]) + 8000}"
QUESTION = "What caused the recurring VPN outages?"  # no ticket id: a vector search with several hits
NOTHING = "zzzxqv qwrtp blorf"  # nothing relevant
TAG = re.compile(r'<ticket id="([^"]+)">')


def ask(stub, question, *, answer="Stub answer.", found=True, citations=(), **kwargs):
    stub.set(answer=answer, found=found, citations=citations)
    stub.calls.clear()
    return rag.answer_question(question, **kwargs)


def in_prompt(stub):
    """The ticket ids of the tickets the model was given, in the order they were given."""
    (system, prompt), = stub.calls
    return TAG.findall(prompt)


@pytest.fixture
def ctx(stub_model):
    """The retrieved set (in order) of QUESTION, learnt from the prompt of a first call."""
    ask(stub_model, QUESTION)
    ids = in_prompt(stub_model)
    assert len(ids) >= 5, "the tests need a question with several retrieved tickets"
    return ids


@pytest.fixture
def other(ctx):
    """A ticket that exists but is not in the retrieved set."""
    return next(i for i in IDS if i not in ctx)


def ids_of(result):
    return [s["ticket_id"] for s in result["sources"]]


# --- kept and dropped -------------------------------------------------------------------------------------------

def test_a_citation_of_a_retrieved_ticket_is_kept(stub_model, ctx):
    result = ask(stub_model, QUESTION, answer=f"Because of [{ctx[3]}].", citations=[ctx[3]])
    assert result["citations"] == [ctx[3]] and result["unverified_citations"] == []
    assert result["answer"] == f"Because of [{ctx[3]}]." and result["found"] is True


def test_a_citation_of_a_ticket_that_was_not_retrieved_is_dropped_and_reported(stub_model, ctx, other):
    result = ask(stub_model, QUESTION, answer=f"Because of [{other}].", citations=[other])
    assert result["citations"] == []
    assert result["unverified_citations"] == [other]
    assert result["answer"] == f"Because of [{other}]." and result["found"] is True  # the text and the flag are the model's


def test_kept_and_dropped_citations_are_split(stub_model, ctx, other):
    result = ask(stub_model, QUESTION, citations=[other, ctx[1], MISSING])
    assert result["citations"] == [ctx[1]]
    assert sorted(result["unverified_citations"]) == sorted([other, MISSING])


def test_a_citation_of_a_ticket_that_does_not_exist_is_reported(stub_model, ctx):
    result = ask(stub_model, QUESTION, citations=[MISSING])
    assert result["citations"] == [] and result["unverified_citations"] == [MISSING]


def test_found_true_with_no_citations(stub_model, ctx):
    result = ask(stub_model, QUESTION, citations=[])
    assert result["found"] is True and result["citations"] == [] and result["unverified_citations"] == []
    assert ids_of(result) == ctx  # nothing cited: sources stay in retrieval order


def test_the_exact_id_ticket_can_be_cited(stub_model):
    result = ask(stub_model, f"What happened in ticket {A}?", answer=f"See [{A}].", citations=[A])
    assert result["citations"] == [A] and result["unverified_citations"] == []
    assert ids_of(result) == [A] and result["sources"][0]["score"] is None


def test_a_missing_reference_next_to_citations(stub_model):
    result = ask(stub_model, f"Compare {A} and {MISSING}", answer="Only one.", citations=[A])
    assert result["citations"] == [A] and result["unverified_citations"] == []
    assert result["missing_references"] == [MISSING]
    assert result["answer"] == f"Not found in the ticket data: {MISSING}.\n\nOnly one."


def test_citing_the_missing_reference_is_unverified(stub_model):
    result = ask(stub_model, f"Compare {A} and {MISSING}", citations=[A, MISSING])
    assert result["citations"] == [A] and result["unverified_citations"] == [MISSING]


# --- found=false ------------------------------------------------------------------------------------------------

def test_found_false_with_retrieved_tickets(stub_model, ctx):
    result = ask(stub_model, QUESTION, answer="The tickets do not say.", found=False)
    assert result["found"] is False and result["citations"] == [] and result["unverified_citations"] == []
    assert ids_of(result) == ctx and result["retrieved_count"] == len(ctx)  # the retrieved tickets are still shown
    assert len(stub_model.calls) == 1


def test_found_false_without_any_retrieved_ticket(stub_model):
    result = ask(stub_model, NOTHING)
    assert stub_model.calls == [], "no context: the model must not be called"
    assert result["found"] is False and result["citations"] == [] and result["sources"] == []
    assert result["retrieved_count"] == 0 and result["answer"] == "No relevant tickets found for that question."


def test_found_false_for_a_missing_reference_only(stub_model):
    result = ask(stub_model, f"What happened in {MISSING}?")
    assert stub_model.calls == [] and result["found"] is False and result["sources"] == []
    assert result["missing_references"] == [MISSING] and result["unverified_citations"] == []


def test_pinned_found_false_empties_the_citations_but_still_reports_unverified_ids(stub_model, ctx, other):
    result = ask(stub_model, QUESTION, found=False, citations=[ctx[0], other])
    assert result["citations"] == []
    assert result["unverified_citations"] == [other]


# --- spelling of the ids ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("spell", [str.lower, lambda i: f"[{i}]", lambda i: f"  {i}  ", lambda i: f"[{i.lower()}] "],
                         ids=["lowercase", "in brackets", "with spaces", "lowercase in brackets with a space"])
def test_pinned_a_retrieved_id_in_another_spelling_is_kept_as_the_canonical_id(stub_model, ctx, spell):
    result = ask(stub_model, QUESTION, citations=[spell(ctx[2])])
    assert result["citations"] == [ctx[2]] and result["unverified_citations"] == []


@pytest.mark.parametrize("form", [
    lambda i: f"OPS {i.split('-')[1]}", lambda i: i.split("-")[1], lambda i: i + ".", lambda i: f"OPS-0{i.split('-')[1]}",
    lambda i: f"see {i}", lambda i: i + "x", lambda i: i.replace("OPS", "OP"),
], ids=["space instead of hyphen", "number only", "trailing point", "zero-padded", "with a word before", "with a letter after", "wrong prefix"])
def test_a_malformed_id_is_not_kept_and_is_reported(stub_model, ctx, form):
    bad = form(ctx[0])
    assert bad.upper() not in TICKETS
    result = ask(stub_model, QUESTION, citations=[bad])
    assert result["citations"] == [] and result["unverified_citations"] == [bad.upper()]


@pytest.mark.parametrize("blank", ["", "   ", "[ ]", "[]"], ids=["empty", "spaces", "brackets around a space", "empty brackets"])
def test_a_blank_citation_is_not_reported_as_an_unverified_id(stub_model, ctx, blank):
    result = ask(stub_model, QUESTION, citations=[blank])
    assert result["citations"] == [] and result["unverified_citations"] == []


def test_pinned_duplicates_are_reported_once(stub_model, ctx, other):
    result = ask(stub_model, QUESTION, answer=f"[{other}] [{other.lower()}]", citations=[ctx[1], ctx[1].lower(), f"[{ctx[1]}]", other, other.lower()])
    assert result["citations"] == [ctx[1]]
    assert result["unverified_citations"] == [other]


def test_pinned_citations_keep_the_order_the_model_gave(stub_model, ctx):
    result = ask(stub_model, QUESTION, citations=[ctx[4], ctx[1], ctx[3]])
    assert result["citations"] == [ctx[4], ctx[1], ctx[3]]


# --- ids that are only in the answer text ---------------------------------------------------------------------------

def test_pinned_an_id_only_in_the_text_that_was_not_retrieved_is_unverified(stub_model, ctx, other):
    result = ask(stub_model, QUESTION, answer=f"See [{other}] and [{other.lower()}].", citations=[])
    assert result["citations"] == [] and result["unverified_citations"] == [other]


def test_pinned_a_lowercase_inline_id_that_was_not_retrieved_is_unverified_as_the_canonical_id(stub_model, ctx, other):
    result = ask(stub_model, QUESTION, answer=f"See [{other.lower()}].", citations=[])
    assert result["unverified_citations"] == [other]


def test_pinned_an_id_only_in_the_text_that_was_retrieved_is_not_unverified_and_not_a_citation(stub_model, ctx):
    result = ask(stub_model, QUESTION, answer=f"See [{ctx[2]}].", citations=[])
    assert result["citations"] == [] and result["unverified_citations"] == []
    assert ids_of(result) == ctx  # it is not moved first: only the citations list orders the sources


# --- sources ----------------------------------------------------------------------------------------------------

def test_cited_tickets_come_first_in_sources_the_rest_in_retrieval_order(stub_model, ctx):
    cited = [ctx[3], ctx[1]]  # in a different order than the retrieval
    result = ask(stub_model, QUESTION, citations=cited)
    assert ids_of(result) == [i for i in ctx if i in cited] + [i for i in ctx if i not in cited]
    assert result["citations"] == cited  # the citations list keeps the model's order


def test_sources_are_exactly_the_retrieved_tickets_even_when_the_model_cites_others(stub_model, ctx, other):
    result = ask(stub_model, QUESTION, citations=[other, MISSING, ctx[0]])
    assert sorted(ids_of(result)) == sorted(in_prompt(stub_model)) == sorted(ctx)
    assert other not in ids_of(result) and MISSING not in ids_of(result)
    assert result["retrieved_count"] == len(ids_of(result)) == len(ctx)


def test_every_source_is_a_real_ticket_with_its_own_title(stub_model, ctx):
    result = ask(stub_model, QUESTION, citations=[ctx[2]])
    for source in result["sources"]:
        assert source["title"] == TICKETS[source["ticket_id"]]["title"]


# --- through the API ----------------------------------------------------------------------------------------------------

def test_the_chat_route_returns_the_validated_citations(api_client, stub_model, ctx, other):
    stub_model.set(answer=f"See [{ctx[1]}] and [{other}].", found=True, citations=[ctx[1], other])
    response = api_client.post("/chat", json={"question": QUESTION})
    assert response.status_code == 200
    body = response.json()
    assert body["citations"] == [ctx[1]] and body["unverified_citations"] == [other]
    assert [s["ticket_id"] for s in body["sources"]][0] == ctx[1] and other not in [s["ticket_id"] for s in body["sources"]]
    assert body["found"] is True and body["missing_references"] == []
