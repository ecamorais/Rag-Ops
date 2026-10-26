"""The RAG core: retrieve tickets, build a grounded prompt, call Gemini once, validate citations.

LlmError (from app.llm) is not caught here; its message is always safe to show,
so the caller (scripts/ask.py, later the API) prints it as is.
"""

import re

from app import llm
from app.config import settings
from app.ingest import TICKETS_PATH, load_tickets
from app.prompt import SYSTEM_PROMPT, build_prompt
from app.vectorstore import get_collection, search

HISTORY_TURNS = 6
SNIPPET_CHARS = 200

OPS_RE = re.compile(r"\bOPS-\d+\b", re.IGNORECASE)
PO_INV_RE = re.compile(r"\b(?:PO|INV)-\d+\b", re.IGNORECASE)
BRACKET_ID_RE = re.compile(r"\[(OPS-\d+)\]", re.IGNORECASE)
WORD_RE = re.compile(r"[a-z0-9]+")

# Used by about_inherited_ticket. Words that say nothing about the topic: question words, fillers, and
# words that point back at "the ticket".
FILLER_WORDS = frozenset(
    "a an the is was were are be been am do does did done has have had will would can could should "
    "what which whom whose where how whether it its this that these those they them their "
    "of in on at to for from by with about into than then so and or but not no yes also too just only "
    "thanks thank ok okay great sure cool hello hi "
    "more most less other else another me my us we you your please tell show give list explain describe "
    "mention mentioned ticket issue incident problem case request one any some all there here".split()
)
# Words that ask about a property of a ticket rather than about a new subject.
TICKET_ASK_WORDS = frozenset(
    "who when why root cause resolution resolve resolved fix fixed status priority assignee assigned "
    "reporter reported team category created date comments comment notes happened details summary long".split()
)
BACK_REFERENCE_WORDS = frozenset("it its this that these those".split())
SHORT_QUESTION_WORDS = 6

# Number of times answer_question called the LLM. Questions refused before
# retrieval-to-LLM (nothing relevant found) leave it unchanged.
llm_call_count = 0


_ticket_records = None


def ticket_records():
    """data/tickets.json as {ticket_id: record} (the same loader and path the API uses), read once per process.

    Chroma's metadata has no reporter or resolved date, so the prompt's details line comes from here. An
    empty dict when the file is missing: the details lines are then left out. A missing file is not remembered.
    """
    global _ticket_records
    if _ticket_records is None:
        try:
            _ticket_records = {t["ticket_id"]: t for t in load_tickets(TICKETS_PATH)}
        except FileNotFoundError:
            return {}
    return _ticket_records


def _refs(text):
    """Ordered, de-duplicated (ops_ids, po_inv_ids) found in text, upper-cased."""
    ops = list(dict.fromkeys(m.upper() for m in OPS_RE.findall(text or "")))
    other = list(dict.fromkeys(m.upper() for m in PO_INV_RE.findall(text or "")))
    return ops, other


def _body_words(document):
    """Words of a stored ticket document without its first line (the header: id, category, priority, status)."""
    return set(WORD_RE.findall(" ".join(document.split("\n")[1:]).lower()))


def about_inherited_ticket(question, documents):
    """True when a follow-up with no ticket id of its own is clearly about the ticket(s) carried over from the previous turn.

    Conservative on purpose. A word that is not filler, not a question about a ticket property and not a word of the
    inherited ticket's own text is a NEW topic, and then the answer is False (the vector fill still runs).
    A True also needs a back-reference ("it", "this", "root cause", "who", ...) or a short question.
    """
    words = set(WORD_RE.findall(question.lower()))
    known = set()
    for document in documents:
        known |= _body_words(document)
    new_topic = {w for w in words if len(w) > 1 and w not in FILLER_WORDS and w not in TICKET_ASK_WORDS and w not in known}
    if new_topic:
        return False
    return bool(words & (BACK_REFERENCE_WORDS | TICKET_ASK_WORDS)) or len(words) <= SHORT_QUESTION_WORDS


def _inherited_refs(history):
    """Ticket references a follow-up (with no ticket id of its own) inherits from the history.

    Walks back through the user turns of the history (already limited to the last HISTORY_TURNS messages) to the most
    recent one that named a ticket. Every user turn after it must have been about that ticket (about_inherited_ticket);
    if one was a topic change, or if the ticket does not exist, nothing is inherited. A turn that named a different
    ticket is itself the most recent one that named a ticket, so that other ticket is the one inherited.
    """
    user_turns = [t["content"] for t in history if t["role"] == "user"]
    for i in range(len(user_turns) - 1, -1, -1):
        ops_ids, other_ids = _refs(user_turns[i])
        if not (ops_ids or other_ids):
            continue
        hits, _ = _exact_lookup(ops_ids, other_ids)
        if not hits:
            return [], []
        documents = [hit["document"] for hit in hits]
        if all(about_inherited_ticket(turn, documents) for turn in user_turns[i + 1:]):
            return ops_ids, other_ids
        return [], []
    return [], []


def _hit(ticket_id, document, metadata, similarity=None):
    return {"ticket_id": ticket_id, "document": document, "metadata": metadata, "similarity": similarity}


def _exact_lookup(ops_ids, other_ids):
    """Fetch tickets by id (OPS-n) or by text contained in the stored document (PO-n, INV-n).

    Returns (hits, missing): hits in reference order, missing = references with no ticket.
    """
    collection = get_collection()
    hits, missing, seen = [], [], set()

    def add(ids, documents, metadatas):
        for ticket_id, document, metadata in zip(ids, documents, metadatas):
            if ticket_id not in seen:
                seen.add(ticket_id)
                hits.append(_hit(ticket_id, document, metadata))

    if ops_ids:
        found = collection.get(ids=ops_ids, include=["documents", "metadatas"])
        by_id = dict(zip(found["ids"], zip(found["documents"], found["metadatas"])))
        for ref in ops_ids:
            if ref in by_id:
                add([ref], [by_id[ref][0]], [by_id[ref][1]])
            else:
                missing.append(ref)
    for ref in other_ids:
        found = collection.get(where_document={"$contains": ref}, include=["documents", "metadatas"])
        if found["ids"]:
            add(found["ids"], found["documents"], found["metadatas"])
        else:
            missing.append(ref)
    return hits, missing


def title_of(document):
    match = re.search(r"^Title:\s*(.+)$", document, re.MULTILINE)
    return match.group(1).strip() if match else ""


def snippet_of(document):
    match = re.search(r"^Description:\s*(.+?)(?=^Resolution:|\Z)", document, re.MULTILINE | re.DOTALL)
    text = " ".join((match.group(1) if match else document).split())
    return text if len(text) <= SNIPPET_CHARS else text[: SNIPPET_CHARS - 1].rstrip() + "…"


def _source(hit):
    meta = hit["metadata"]
    return {
        "ticket_id": hit["ticket_id"],
        "title": title_of(hit["document"]),
        "category": meta.get("category"),
        "priority": meta.get("priority"),
        "status": meta.get("status"),
        "score": None if hit["similarity"] is None else round(hit["similarity"], 3),
        "snippet": snippet_of(hit["document"]),
    }


def _result(answer, found, citations, context, unverified, missing):
    cited = set(citations)
    ordered = sorted(context, key=lambda hit: hit["ticket_id"] not in cited)  # stable: cited first
    return {
        "answer": answer,
        "found": found,
        "citations": citations,
        "sources": [_source(hit) for hit in ordered],
        "retrieved_count": len(context),
        "unverified_citations": unverified,
        "missing_references": missing,
    }


def answer_question(question, filters=None, k=6, history=None):
    global llm_call_count
    history = (history or [])[-HISTORY_TURNS:]
    previous_question = next((t["content"] for t in reversed(history) if t["role"] == "user"), None)

    # Exact references: from the new question; only if it has none, from the history (see _inherited_refs).
    ops_ids, other_ids = _refs(question)
    from_question = bool(ops_ids or other_ids)
    if not from_question:
        ops_ids, other_ids = _inherited_refs(history)
    exact, missing = _exact_lookup(ops_ids, other_ids)
    exact = exact[:k]
    # True only when the NEW question names an OPS ticket; an id inherited from the previous turn
    # does not count, so a topic change after an id question still gets a vector search.
    named_ops = from_question and bool(ops_ids)
    # Ticket(s) carried over from the previous user turn (found in the store); they stay first in the context.
    inherited = bool(exact) and not from_question
    if not from_question:
        missing = []  # a reference only inherited from the previous turn is not reported again

    context = list(exact)
    # A follow-up that is clearly about the inherited ticket needs nothing else.
    about_it = inherited and about_inherited_ticket(question, [hit["document"] for hit in exact])
    # Vector search. Skipped when the new question names an OPS ticket (that ticket is the whole context) and when
    # the follow-up is about the inherited ticket. With an inherited ticket the NEW question alone is searched, because
    # the previous question would pull in the previous topic; without one, the previous question is added as context.
    if len(context) < k and not named_ops and not about_it:
        query = question if inherited or not previous_question else f"{previous_question} {question}"
        have = {hit["ticket_id"] for hit in context}
        for hit in search(query, k, filters):
            if len(context) >= k:
                break
            if hit["ticket_id"] not in have and hit["similarity"] >= settings.similarity_threshold:
                context.append(hit)

    note = ""
    if missing:
        note = "Not found in the ticket data: " + ", ".join(missing) + ".\n\n"

    if not context:
        message = note.strip() or "No relevant tickets found for that question."
        return _result(message, False, [], [], [], missing)

    llm_call_count += 1
    reply = llm.generate_answer(SYSTEM_PROMPT, build_prompt(question, context, history, ticket_records()))

    in_context = {hit["ticket_id"] for hit in context}
    cited_by_model = [c.strip().strip("[]").upper() for c in reply.citations]
    in_text = [m.upper() for m in BRACKET_ID_RE.findall(reply.answer)]
    citations = list(dict.fromkeys(c for c in cited_by_model if c in in_context)) if reply.found else []
    unverified = list(dict.fromkeys(c for c in cited_by_model + in_text if c not in in_context))

    return _result(note + reply.answer, reply.found, citations, context, unverified, missing)
