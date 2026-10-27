"""Follow-up questions: which ticket a question without an id inherits from the history (app.rag._inherited_refs).

Expected values come from the written rules, not from running the code:
  CLAUDE.md:121 "the most recent user turn that named a ticket within the last 6 history messages. A later user turn that was
                 a topic change (by the same rule) ends the inheritance, and so does a ticket that does not exist. Ids that
                 appear only in assistant replies are ignored."
  CLAUDE.md:120 "the ticket from an earlier user turn is kept first in the context. On a topic change that ticket still takes
                 slot 1, and the rest of the context comes from searching with the new question alone."
  CLAUDE.md:122 "Inherited tickets ignore the sidebar filters".
  app/rag.py    "a reference only inherited from the previous turn is not reported again";
                "A follow-up that is clearly about the inherited ticket needs nothing else" (no vector fill);
                "without one [an inherited ticket], the previous question is added as context" (to the vector query).
What is inherited: references of both kinds, OPS-n and PO-n/INV-n (_inherited_refs returns (ops_ids, other_ids)).

Real retrieval on the temp copy of chroma_db (conftest.py), stubbed model. Tickets are taken from data/tickets.json when the
tests run; the word-list rule (about_inherited_ticket) is tested on a handwritten document so it does not depend on the data.
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
A, B, C, X = IDS[0], IDS[1], IDS[5], IDS[10]  # X: only ever named by the assistant
MISSING = f"OPS-{int(IDS[-1].split('-')[1]) + 8000}"
REF = re.compile(r"\b(?:PO|INV)-\d+\b")
WORD = re.compile(r"[a-z0-9]+")

WHO = "Who fixed it?"
ROOTQ = "What was the root cause?"
RESOLVED = "When was it resolved?"
STATUS = "What is its status?"
FIXED = "How was it fixed?"
ASSIGNEE = "Who was the assignee?"
VENDOR = "Which vendor has the most delayed shipment tickets?"
COOLING = "Show me P1 cooling or power problems"
VPN = "What caused the recurring VPN outages?"


def about(ticket_id):
    return f"What happened in ticket {ticket_id}?"


def conv(*user_turns, assistant=""):
    history = []
    for turn in user_turns:
        history += [{"role": "user", "content": turn}, {"role": "assistant", "content": "Stub answer. " + assistant}]
    return history


def ask(question, history=None, **kwargs):
    return rag.answer_question(question, history=history, **kwargs)


def exact_ids(result):
    return [s["ticket_id"] for s in result["sources"] if s["score"] is None]


def all_ids(result):
    return [s["ticket_id"] for s in result["sources"]]


def body_words(ticket_id):
    t = TICKETS[ticket_id]
    return set(WORD.findall(f"{t['title']} {t['description']} {t.get('resolution_notes') or ''}".lower()))


def test_the_chosen_tickets_do_not_already_contain_the_topic_words():
    """The topic-change questions below only count as a topic change if they bring a word the ticket does not have."""
    for ticket_id in (A, B, C):
        for question in (VENDOR, COOLING):
            new = {w for w in WORD.findall(question.lower()) if len(w) > 1 and w not in rag.FILLER_WORDS
                   and w not in rag.TICKET_ASK_WORDS and w not in body_words(ticket_id)}
            assert new, f"{ticket_id} already contains every word of {question!r}: pick other tickets"
    assert len({A, B, C, X}) == 4 and MISSING not in TICKETS


# --- the 16 chains (plus one) --------------------------------------------------------------------------------------
# (name, history, final question, tickets that must come back by id, True when the context is exactly those tickets)

CHAINS = [
    ("1 id, root cause", conv(about(A), ROOTQ), WHO, [A], True),
    ("2 id, root cause, resolved", conv(about(A), ROOTQ, RESOLVED), WHO, [A], True),
    ("3 id, then a topic change: nothing inherited", conv(about(A), VENDOR), WHO, [], False),
    ("4 id, then another id: the other one", conv(about(A), about(B)), WHO, [B], True),
    ("5 id outside the 6-message window", conv(about(A), ROOTQ, RESOLVED, STATUS), WHO, [], False),
    ("6 an id-free question before the id turn", conv(VPN, about(A), ROOTQ), WHO, [A], True),
    ("7 id, topic change, root cause: nothing inherited", conv(about(A), VENDOR, ROOTQ), WHO, [], False),
    ("9 id, other id, root cause: the other one", conv(about(A), about(B), ROOTQ), WHO, [B], True),
    ("10 two tickets named in one turn", conv(f"Compare {A} and {C}"), WHO, [A, C], True),
    ("11 a ticket that does not exist", conv(about(MISSING), ROOTQ), WHO, [], False),
    ("12 id 12 messages back", conv(about(A), ROOTQ, RESOLVED, STATUS, FIXED, ASSIGNEE), WHO, [], False),
    ("13 only the assistant named a ticket", conv(VPN, assistant=f"[{X}]"), WHO, [], False),
    ("15 a chatty turn between", conv(about(A), "Thanks!"), WHO, [A], True),
    ("16 no history", [], WHO, [], False),
    ("17 an older real ticket behind a missing one: nothing inherited", conv(about(A), about(MISSING), ROOTQ), WHO, [], False),
]


@pytest.mark.parametrize("name, history, question, want, only_those", CHAINS, ids=[c[0] for c in CHAINS])
def test_chain(name, history, question, want, only_those):
    result = ask(question, history)
    assert exact_ids(result) == want
    if only_those:
        assert all_ids(result) == want, "a follow-up about the inherited ticket needs no vector hits"


def ticket_with_po_and_inv():
    """The first ticket whose title/description names both a PO and an INV id, with the two numbers."""
    for ticket_id in IDS:
        text = f"{TICKETS[ticket_id]['title']} {TICKETS[ticket_id]['description']}"
        po, inv = re.search(r"\bPO-(\d+)", text), re.search(r"\bINV-(\d+)", text)
        if po and inv:
            return ticket_id, po.group(1), inv.group(1)


D, D_PO, D_INV = ticket_with_po_and_inv()


@pytest.mark.parametrize("missing", [f"INV-{D_PO}", f"PO-{D_INV}"], ids=["INV id made of the PO number", "PO id made of the INV number"])
def test_chain_18_a_missing_po_or_inv_turn_that_is_about_the_ticket_still_ends_the_inheritance(missing):
    """CLAUDE.md:121 "so does a ticket that does not exist". The reference is built from words of the older ticket's own text,
    so by the word rule the turn is "about" that ticket: only the missing-ticket rule can end the inheritance here (an OPS-9999
    turn cannot show this: its words "ops" and "9999" are never in a ticket, so the topic-change rule ends it anyway)."""
    turn = f"Is it {missing}?"
    assert not [i for i in IDS if re.search(rf"\b{re.escape(missing)}\b", f"{TICKETS[i]['title']} {TICKETS[i]['description']} {TICKETS[i].get('resolution_notes') or ''}")]
    assert rag.about_inherited_ticket(turn, [rag._exact_lookup([D], [])[0][0]["document"]]) is True  # precondition: not a topic change
    result = ask(WHO, conv(about(D), turn, ROOTQ))
    assert exact_ids(result) == []


def test_chain_8_a_new_topic_keeps_the_ticket_first_and_searches_with_the_new_question_alone():
    result = ask(COOLING, conv(about(A), ROOTQ))
    baseline = all_ids(ask(COOLING))  # the same question with no history: the vector search it alone would run
    assert len(baseline) > 2, "the test needs vector hits"
    assert all_ids(result)[0] == A and exact_ids(result) == [A]
    assert all_ids(result)[1:] == [i for i in baseline if i != A][:5]
    scores = {s["ticket_id"]: s["score"] for s in ask(COOLING)["sources"]}
    assert {s["ticket_id"]: s["score"] for s in result["sources"][1:]} == {i: scores[i] for i in all_ids(result)[1:]}


def test_chain_14_a_new_id_replaces_the_inherited_one_and_nothing_else_is_searched():
    result = ask(about(C), conv(about(A), ROOTQ))
    assert all_ids(result) == [C] and exact_ids(result) == [C]
    assert A not in all_ids(result) and result["retrieved_count"] == 1


def test_without_an_inherited_ticket_the_previous_question_is_added_to_the_vector_query():
    result = ask(WHO, conv(about(A), VENDOR))
    assert exact_ids(result) == []
    assert all_ids(result) == all_ids(ask(f"{VENDOR} {WHO}"))


# --- what is inherited --------------------------------------------------------------------------------------------

def po_with_holders():
    found = sorted({m for t in TICKETS.values() for m in REF.findall(f"{t['title']} {t['description']} {t.get('resolution_notes') or ''}")
                    if m.startswith("PO-")})
    for ref in found:
        holders = [i for i in IDS if re.search(rf"\b{re.escape(ref)}\b", f"{TICKETS[i]['title']} {TICKETS[i]['description']}")]
        return ref, holders


def test_a_po_reference_is_inherited_too():
    ref, holders = po_with_holders()
    result = ask(WHO, conv(f"Which ticket mentions {ref}?", ROOTQ))
    assert sorted(exact_ids(result)) == sorted(holders)
    assert sorted(all_ids(result)) == sorted(holders)


def test_an_inherited_reference_that_is_missing_is_not_reported_again():
    result = ask(WHO, conv(f"Compare {A} and {MISSING}", ROOTQ))
    assert exact_ids(result) == [A]
    assert result["missing_references"] == []


def test_the_missing_reference_of_the_new_question_is_still_reported():
    result = ask(about(MISSING), conv(about(A), ROOTQ))
    assert result["missing_references"] == [MISSING] and result["sources"] == []


def test_an_inherited_ticket_ignores_the_filters():
    other = next(c for c in sorted({t["category"] for t in TICKETS.values()}) if c != TICKETS[A]["category"])
    result = ask(WHO, conv(about(A), ROOTQ), filters={"category": other})
    assert exact_ids(result) == [A]


# --- the 6-message window ---------------------------------------------------------------------------------------

def id_at(position, total=None):
    """A history where the user message that names A is `position` messages from the end (1 = the last message), followed
    by alternating assistant/user messages that only ask about it. Padded at the front to `total` messages if given."""
    history = [{"role": "user", "content": about(A)}]
    role = "assistant"
    for _ in range(position - 1):
        history.append({"role": role, "content": "Stub answer." if role == "assistant" else ROOTQ})
        role = "user" if role == "assistant" else "assistant"
    while total and len(history) < total:
        history = [{"role": "assistant", "content": "Stub answer."}, *history] if len(history) % 2 else [{"role": "user", "content": "Hello"}, *history]
    return history


@pytest.mark.parametrize("position, total, inherited", [
    (6, None, True), (7, None, False), (8, None, False), (6, 12, True), (8, 12, False), (12, 12, False),
], ids=["id is the 6th message from the end", "7th", "8th", "6th in a 12-message history", "8th in a 12-message history", "12th in a 12-message history"])
def test_the_window_is_the_last_6_messages(position, total, inherited):
    history = id_at(position, total)
    if total:
        assert len(history) == total
    result = ask(WHO, history)
    assert exact_ids(result) == ([A] if inherited else [])


# --- about_inherited_ticket: the word-list rule, on a handwritten ticket text -----------------------------------------

DOC = (
    "Ticket OPS-0001 | Category: Vendor Management | Priority: P2-High | Status: Open\n"
    "Title: Price mismatch between invoice and purchase order\n"
    "Description: The invoice from Orion Supplies was placed on hold because the price list differs from the purchase order.\n"
    "Resolution: Finance corrected the price list and released the invoice."
)

# (question, about the ticket?) Each answer follows the rule in about_inherited_ticket's docstring: a word that is not filler,
# not a ticket-property word and not in the ticket text (header line excluded) is a new topic; "about" also needs a
# back-reference or ticket-property word, or a question of at most 6 words.
RULE = [
    (ROOTQ, True), (ASSIGNEE, True), (RESOLVED, True), (STATUS, True), (FIXED, True), ("Why was the invoice on hold?", True),
    (VENDOR, False), (COOLING, False), ("What caused the recurring VPN outages and how were they fixed?", False),
    ("What about cooling?", False), ("How many tickets are still open?", False), ("Who handles database failover incidents?", False),
    ("What invoice is on hold?", True), ("Is it related to the VPN outage?", False), ("Show me closed Vendor Management tickets", False),
    ("Which vendor?", False), ("Which invoices are on hold?", False), ("What was the root cause of the delays?", False),
    ("Tell me more", True), ("Is it with Orion?", True), ("Why?", True), ("And who fixed the database failover?", False),
    ("What is the price list issue?", True),
    ("Who was it assigned to, and when did the supplier ship the replacement pallets?", False),
    ("Thanks!", True), ("Ok, thanks", True), ("Great, thank you", True), ("Hi, who fixed it?", True), ("Hello", True),
    ("Thanks, show me cooling problems", False), ("Ok what about VPN outages?", False),
    ("Sure, which vendor has the most delayed shipments?", False),
    ("tell me more and also show us", False),  # only filler words, but 7 distinct words and no back-reference: not clearly about it
]


@pytest.mark.parametrize("question, want", RULE, ids=[q[:50] for q, _ in RULE])
def test_about_inherited_ticket_rule(question, want):
    assert rag.about_inherited_ticket(question, [DOC]) is want


def test_the_ticket_header_line_does_not_count_as_ticket_text():
    assert rag.about_inherited_ticket("Which management?", [DOC]) is False  # "management" is only in the header line of DOC


def test_the_words_of_every_inherited_ticket_count():
    other = DOC.replace("Orion Supplies", "Lotus Wrap")
    assert rag.about_inherited_ticket("Is it with Lotus?", [DOC]) is False
    assert rag.about_inherited_ticket("Is it with Lotus?", [DOC, other]) is True
