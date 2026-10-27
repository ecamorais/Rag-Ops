"""Exact ticket references: OPS-n by id, PO-n and INV-n by text contained in the stored ticket (app.rag._exact_lookup).

Real retrieval on the temp copy of chroma_db (see conftest.py), stubbed model. Every expected value is computed from
data/tickets.json when the tests run; no id or title is typed in from an earlier run. An exact hit is recognised by its
score: a ticket fetched by id has no similarity (score None), a vector-search hit has one.
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
REF = re.compile(r"\b(?:PO|INV)-\d+\b")


def indexed_text(ticket):
    """The free text that is stored in Chroma next to the header line (app.ingest.ticket_to_document). Comments are not."""
    return f"{ticket['title']}\n{ticket['description']}\n{ticket.get('resolution_notes') or ''}"


def holders_of(ref):
    return [i for i in IDS if re.search(rf"\b{re.escape(ref)}\b", indexed_text(TICKETS[i]))]


def refs_with(prefix, want_holders):
    """Sorted PO-/INV- references from the data held by exactly one ticket (want_holders=1) or by several (">1")."""
    found = sorted({m for t in TICKETS.values() for m in REF.findall(indexed_text(t)) if m.startswith(prefix)})
    return [r for r in found if (len(holders_of(r)) == 1) == (want_holders == 1)]


A, B = IDS[0], IDS[1]
SINGLE_PO, SINGLE_INV = refs_with("PO-", 1)[0], refs_with("INV-", 1)[0]
SHARED_PO, SHARED_INV = refs_with("PO-", ">1")[0], refs_with("INV-", ">1")[0]
MISSING_OPS = f"OPS-{int(IDS[-1].split('-')[1]) + 8000}"
MISSING_PO = f"PO-{max(int(r.split('-')[1]) for r in REF.findall(' '.join(indexed_text(t) for t in TICKETS.values())) if r.startswith('PO')) + 1}"


def ask(question, **kwargs):
    return rag.answer_question(question, **kwargs)


def exact_ids(result):
    return [s["ticket_id"] for s in result["sources"] if s["score"] is None]


def prefix_of(ref):
    """The reference with its last digits cut off: a real prefix of ref, itself not a reference of any ticket."""
    cut = ref[:-2]
    assert not holders_of(cut) and holders_of(ref), (cut, ref)
    return cut


def test_the_data_is_what_these_tests_assume():
    assert A not in (B, MISSING_OPS) and MISSING_OPS not in TICKETS and MISSING_PO not in set(REF.findall(" ".join(map(indexed_text, TICKETS.values()))))
    assert len(holders_of(SHARED_PO)) > 1 and len(holders_of(SHARED_INV)) > 1
    assert len(holders_of(SINGLE_PO)) == 1 and len(holders_of(SINGLE_INV)) == 1
    everything = sorted(set(REF.findall(" ".join(map(indexed_text, TICKETS.values())))))
    assert not [(x, y) for x in everything for y in everything if x != y and y.startswith(x)], "a PO/INV id is the prefix of another"


# --- OPS-n: looked up by id ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("form", [
    "{id}", "{id_lower}", "{id_mixed}", "What happened in {id}?", "Tell me about {id}, please.", "{id}.",
    "Is {id}, the one with the long title, resolved?", "see ({id})",
], ids=["plain", "lowercase", "mixed case", "in a question with ?", "followed by comma and point", "followed by point",
        "followed by comma", "in brackets"])
def test_an_ops_id_is_found_in_any_form(stub_model, form):
    question = form.format(id=A, id_lower=A.lower(), id_mixed=A.title())
    result = ask(question)
    assert exact_ids(result) == [A]
    assert [s["ticket_id"] for s in result["sources"]] == [A]  # an OPS id in the question skips the vector search
    assert result["missing_references"] == []
    assert result["sources"][0]["title"] == TICKETS[A]["title"]
    assert len(stub_model.calls) == 1


def test_the_model_receives_the_exact_ticket(stub_model):
    ask(f"What happened in {A}?")
    (system, prompt), = stub_model.calls
    assert A in prompt and TICKETS[A]["title"] in prompt and TICKETS[A]["description"] in prompt


@pytest.mark.parametrize("question, want", [
    (f"Compare {A} and {B}", [A, B]),
    (f"Compare {B} and {A}", [B, A]),
    (f"{A.lower()}, {B.lower()}?", [A, B]),
    (f"{A} and again {A}", [A]),
], ids=["two ids", "reversed order", "lowercase and punctuation", "the same id twice"])
def test_several_ids_come_back_in_question_order_once_each(question, want):
    result = ask(question)
    assert exact_ids(result) == want
    assert [s["ticket_id"] for s in result["sources"]] == want
    assert result["missing_references"] == []


def test_k_limits_the_exact_tickets():
    result = ask(f"{A} and {B}", k=1)
    assert [s["ticket_id"] for s in result["sources"]] == [A]


def test_a_missing_id_is_reported_and_no_ticket_is_invented(stub_model):
    result = ask(f"What happened in {MISSING_OPS}?")
    assert result["missing_references"] == [MISSING_OPS]
    assert result["sources"] == [] and result["citations"] == [] and result["found"] is False
    assert result["answer"] == f"Not found in the ticket data: {MISSING_OPS}."
    assert stub_model.calls == []  # nothing to ground an answer in: the model is not called


def test_a_missing_id_next_to_a_real_one(stub_model):
    result = ask(f"Compare {A} and {MISSING_OPS}")
    assert [s["ticket_id"] for s in result["sources"]] == [A]
    assert result["missing_references"] == [MISSING_OPS]
    assert result["answer"].startswith(f"Not found in the ticket data: {MISSING_OPS}.")
    assert len(stub_model.calls) == 1


@pytest.mark.parametrize("make", [lambda i: i[:-1], lambda i: i + "0", lambda i: i[:-2]], ids=["one digit short", "one digit longer", "two digits short"])
def test_an_ops_id_is_never_matched_by_prefix(make):
    other = make(A)
    assert other not in TICKETS, "the data changed: pick another derived id"
    result = ask(f"What happened in {other}?")
    assert result["sources"] == [] and result["missing_references"] == [other]


@pytest.mark.parametrize("text", ["XOPS-1001", "OPS-1001abc", "OPS1001", "OPS-"])
def test_text_that_is_not_an_id_is_not_read_as_one(text):
    assert rag._refs(text) == ([], [])


# --- PO-n and INV-n: found by the text stored in the ticket ----------------------------------------------------

@pytest.mark.parametrize("ref", [SINGLE_PO, SINGLE_INV, SHARED_PO, SHARED_INV], ids=["PO", "INV", "PO in two tickets", "INV in two tickets"])
@pytest.mark.parametrize("form", ["{ref}", "{ref_lower}", "Which ticket mentions {ref}?", "{ref}, {ref_lower}."], ids=["plain", "lowercase", "in a question", "twice, with punctuation"])
def test_a_po_or_inv_id_finds_every_ticket_that_contains_it(form, ref):
    holders = holders_of(ref)
    result = ask(form.format(ref=ref, ref_lower=ref.lower()))
    assert sorted(exact_ids(result)) == sorted(holders)
    assert result["missing_references"] == []
    first = [s["ticket_id"] for s in result["sources"][: len(holders)]]
    assert sorted(first) == sorted(holders), "the exact tickets come before any vector-search hit"


def test_exact_tickets_come_before_the_vector_hits():
    result = ask(f"Which ticket mentions {SINGLE_PO}?")
    scores = [s["score"] for s in result["sources"]]
    assert scores[0] is None and any(s is not None for s in scores), "the test needs some vector hits after the exact one"
    assert scores == sorted(scores, key=lambda s: s is not None)


def test_the_model_receives_the_ticket_found_by_a_po_id(stub_model):
    ask(f"Which ticket mentions {SINGLE_PO}?")
    (system, prompt), = stub_model.calls
    holder, = holders_of(SINGLE_PO)
    assert holder in prompt


def test_a_missing_po_id_is_reported():
    result = ask(f"Which ticket mentions {MISSING_PO}?")
    assert result["missing_references"] == [MISSING_PO]
    assert exact_ids(result) == []


def comment_only_refs():
    in_text = set(REF.findall(" ".join(map(indexed_text, TICKETS.values()))))
    return sorted({m for t in TICKETS.values() for c in t["comments"] for m in REF.findall(json.dumps(c))} - in_text)


@pytest.mark.skipif(not comment_only_refs(), reason="no PO/INV id appears only in comments in this data set, so there is nothing to check")
def test_an_id_that_is_only_in_comments_is_not_found():
    ref = comment_only_refs()[0]
    assert ask(f"Which ticket mentions {ref}?")["missing_references"] == [ref]  # comments are not stored in Chroma


# --- substring safety -------------------------------------------------------------------------------------------

def test_the_prefix_inputs_are_real_prefixes():
    for ref in (SINGLE_PO, SINGLE_INV):
        assert prefix_of(ref).startswith(ref[:3]) and ref.startswith(prefix_of(ref))


@pytest.mark.parametrize("ref", [SINGLE_PO, SINGLE_INV], ids=["PO", "INV"])
def test_a_prefix_of_a_po_or_inv_id_is_not_an_exact_match(ref):
    short = prefix_of(ref)
    result = ask(f"Which ticket mentions {short}?")
    assert exact_ids(result) == []
    assert result["missing_references"] == [short]


@pytest.mark.parametrize("ref", [SINGLE_PO, SINGLE_INV], ids=["PO", "INV"])
def test_a_one_digit_po_or_inv_id_is_not_an_exact_match(ref):
    short = ref[:ref.index("-") + 2]
    assert not holders_of(short)
    result = ask(short)
    assert exact_ids(result) == []
    assert result["missing_references"] == [short]


@pytest.mark.parametrize("make", [lambda r: r + "0", lambda r: r + "7", lambda r: r[:-1], lambda r: r[:-2]],
                         ids=["one digit added (0)", "one digit added (7)", "one digit short", "two digits short"])
@pytest.mark.parametrize("ref", [SINGLE_PO, SINGLE_INV], ids=["PO", "INV"])
def test_a_po_or_inv_id_is_never_matched_by_prefix_or_extension(make, ref):
    other = make(ref)
    assert not holders_of(other), "the data changed: pick another derived id"
    result = ask(f"Which ticket mentions {other}?")
    assert exact_ids(result) == [] and result["missing_references"] == [other]


@pytest.mark.parametrize("tail", [".", ",", "?", "'s", ")", ";", " and more"], ids=["point", "comma", "?", "'s", ")", ";", "space"])
@pytest.mark.parametrize("ref", [SINGLE_PO, SINGLE_INV], ids=["PO", "INV"])
def test_a_po_or_inv_id_followed_by_punctuation_is_still_found(tail, ref):
    result = ask(f"Which ticket mentions {ref}{tail}")
    assert sorted(exact_ids(result)) == sorted(holders_of(ref))
    assert result["missing_references"] == []


# --- exact id plus filters: the filters do not apply to a ticket fetched by id ----------------------------------

def other_category(ticket):
    return next(c for c in sorted({t["category"] for t in TICKETS.values()}) if c != ticket["category"])


@pytest.mark.parametrize("filters", [
    lambda t: {"category": other_category(t)},
    lambda t: {"date_from": "2099-01-01"},
    lambda t: {"priority": next(p for p in ("P1-Critical", "P4-Low") if p != t["priority"]), "status": "NoSuchStatus"},
], ids=["other category", "dates in the future", "other priority and unknown status"])
def test_an_ops_id_ignores_the_filters(filters):
    result = ask(f"What happened in {A}?", filters=filters(TICKETS[A]))
    assert exact_ids(result) == [A] and [s["ticket_id"] for s in result["sources"]] == [A]


def test_a_po_id_ignores_the_filters_but_the_vector_hits_obey_them():
    holder = holders_of(SINGLE_PO)[0]
    category = other_category(TICKETS[holder])
    result = ask(f"Which ticket mentions {SINGLE_PO}?", filters={"category": category})
    assert exact_ids(result) == [holder]
    extras = [s for s in result["sources"] if s["score"] is not None]
    assert extras, "the test needs vector hits next to the exact one"
    assert {s["category"] for s in extras} == {category}
