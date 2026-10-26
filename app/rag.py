"""The RAG core: retrieve tickets, build a grounded prompt, call Gemini once, validate citations.

LlmError (from app.llm) is not caught here; its message is always safe to show,
so the caller (scripts/ask.py, later the API) prints it as is.
"""

import re

from app import llm
from app.config import settings
from app.prompt import SYSTEM_PROMPT, build_prompt
from app.vectorstore import get_collection, search

HISTORY_TURNS = 6
SNIPPET_CHARS = 200

OPS_RE = re.compile(r"\bOPS-\d+\b", re.IGNORECASE)
PO_INV_RE = re.compile(r"\b(?:PO|INV)-\d+\b", re.IGNORECASE)
BRACKET_ID_RE = re.compile(r"\[(OPS-\d+)\]", re.IGNORECASE)

# Number of times answer_question called the LLM. Questions refused before
# retrieval-to-LLM (nothing relevant found) leave it unchanged.
llm_call_count = 0


def _refs(text):
    """Ordered, de-duplicated (ops_ids, po_inv_ids) found in text, upper-cased."""
    ops = list(dict.fromkeys(m.upper() for m in OPS_RE.findall(text or "")))
    other = list(dict.fromkeys(m.upper() for m in PO_INV_RE.findall(text or "")))
    return ops, other


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

    # Exact references: from the new question; only if it has none, from the previous user question.
    ops_ids, other_ids = _refs(question)
    from_question = bool(ops_ids or other_ids)
    if not from_question and previous_question:
        ops_ids, other_ids = _refs(previous_question)
    exact, missing = _exact_lookup(ops_ids, other_ids)
    exact = exact[:k]
    # True only when the NEW question names an OPS ticket; an id inherited from the previous turn
    # does not count, so a topic change after an id question still gets a vector search.
    named_ops = from_question and bool(ops_ids)
    if not from_question:
        missing = []  # a reference only inherited from the previous turn is not reported again

    # Vector search: a follow-up is searched with the previous question plus the new one.
    # Skipped when the new question names an OPS ticket (that ticket is the whole context).
    context = list(exact)
    if len(context) < k and not named_ops:
        query = f"{previous_question} {question}" if previous_question else question
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
    reply = llm.generate_answer(SYSTEM_PROMPT, build_prompt(question, context, history))

    in_context = {hit["ticket_id"] for hit in context}
    cited_by_model = [c.strip().strip("[]").upper() for c in reply.citations]
    in_text = [m.upper() for m in BRACKET_ID_RE.findall(reply.answer)]
    citations = list(dict.fromkeys(c for c in cited_by_model if c in in_context)) if reply.found else []
    unverified = list(dict.fromkeys(c for c in cited_by_model + in_text if c not in in_context))

    return _result(note + reply.answer, reply.found, citations, context, unverified, missing)
