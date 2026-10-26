"""Load tickets from data/tickets.json and index them in the Chroma collection."""

import json
from datetime import datetime, timezone

from tqdm import tqdm

from app.config import settings
from app.vectorstore import embed, get_collection, reset_collection

TICKETS_PATH = settings.project_root / "data" / "tickets.json"
BATCH_SIZE = 64


def load_tickets(path=TICKETS_PATH):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def ticket_to_document(ticket):
    """The text that gets embedded and stored: one document per ticket.

    One ticket = one chunk, because tickets are short (well under the embedding
    model's 256-token limit). Very long descriptions would need to be split into
    overlapping chunks, each keeping the ticket id so hits can be merged back.

    Comments are deliberately NOT embedded. They are generic filler repeated across
    many tickets and would dilute matches. They stay available in data/tickets.json
    for the UI.
    """
    lines = [
        f"Ticket {ticket['ticket_id']} | Category: {ticket['category']} "
        f"| Priority: {ticket['priority']} | Status: {ticket['status']}",
        f"Title: {ticket['title']}",
        f"Description: {ticket['description']}",
    ]
    if ticket.get("resolution_notes"):
        lines.append(f"Resolution: {ticket['resolution_notes']}")
    return "\n".join(lines)


def _unix_utc(iso):
    # created_at carries no timezone. It is read as UTC so the value does not
    # depend on the machine's local time, and date filters use UTC days too.
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp())


def ticket_to_metadata(ticket):
    """Chroma metadata values must be str/int/float/bool, so no None values."""
    return {
        "ticket_id": ticket["ticket_id"],
        "category": ticket["category"],
        "priority": ticket["priority"],
        "status": ticket["status"],
        "team": ticket["team"],
        "assignee": ticket["assignee"],
        "created_at": ticket["created_at"],
        "created_ts": _unix_utc(ticket["created_at"]),
        "has_resolution": bool(ticket.get("resolution_notes")),
    }


def ingest(reset=False, path=TICKETS_PATH):
    """Embed and upsert every ticket. Returns the number of tickets indexed.

    The ticket id is the Chroma id, so re-running updates in place (idempotent).
    reset=True deletes and recreates the collection first.
    """
    tickets = load_tickets(path)
    ids = [t["ticket_id"] for t in tickets]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate ticket_id in input")
    collection = reset_collection() if reset else get_collection()
    with tqdm(total=len(tickets), unit="ticket", desc="Indexing") as bar:
        for start in range(0, len(tickets), BATCH_SIZE):
            batch = tickets[start:start + BATCH_SIZE]
            documents = [ticket_to_document(t) for t in batch]
            collection.upsert(
                ids=[t["ticket_id"] for t in batch],
                embeddings=embed(documents, batch_size=BATCH_SIZE),
                documents=documents,
                metadatas=[ticket_to_metadata(t) for t in batch],
            )
            bar.update(len(batch))
    return len(tickets)
