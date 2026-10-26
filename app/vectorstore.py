"""ChromaDB collection, local embedding model and search for the ops tickets."""

import os
from datetime import date, datetime, time, timezone
from functools import lru_cache

from app.config import settings

# Keep the model download inside the project, not in the user's home folder.
# Must be set before huggingface_hub is imported.
MODEL_CACHE_DIR = settings.project_root / ".cache" / "huggingface"
os.environ.setdefault("HF_HOME", str(MODEL_CACHE_DIR))

import chromadb  # noqa: E402
from chromadb.config import Settings as ChromaSettings  # noqa: E402

COLLECTION_NAME = "ops_tickets"
EQUALITY_FILTERS = ("category", "priority", "status", "team")


@lru_cache(maxsize=1)
def _client():
    return chromadb.PersistentClient(
        path=str(settings.chroma_dir),
        settings=ChromaSettings(anonymized_telemetry=False),
    )


def get_collection():
    # embedding_function=None: we always pass our own embeddings, never Chroma's default.
    return _client().get_or_create_collection(
        COLLECTION_NAME,
        configuration={"hnsw": {"space": "cosine"}},
        embedding_function=None,
    )


def reset_collection():
    """Delete the collection if it exists and return a new, empty one."""
    client = _client()
    existing = {getattr(c, "name", c) for c in client.list_collections()}
    if COLLECTION_NAME in existing:
        client.delete_collection(COLLECTION_NAME)
    return get_collection()


@lru_cache(maxsize=1)
def get_embedder():
    """Load the sentence-transformers model once. The first call downloads it."""
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(settings.embed_model, cache_folder=str(MODEL_CACHE_DIR / "hub"))


def embed(texts, batch_size=64, show_progress_bar=False):
    """Normalized embeddings (unit length), so cosine distance = 1 - dot product."""
    return get_embedder().encode(
        list(texts),
        batch_size=batch_size,
        normalize_embeddings=True,
        show_progress_bar=show_progress_bar,
    )


def _day_ts(day, end_of_day=False):
    """Unix timestamp (UTC) for the start or end of a YYYY-MM-DD day."""
    d = date.fromisoformat(day) if isinstance(day, str) else day
    t = time(23, 59, 59) if end_of_day else time.min
    return int(datetime.combine(d, t, tzinfo=timezone.utc).timestamp())


def build_where(filters=None):
    """Turn a filter dict into a Chroma `where` clause, or None when there is nothing to filter.

    Chroma needs at least two items inside $and, so a single condition is returned as is.
    date_from / date_to are inclusive UTC days applied to the integer created_ts field.
    """
    filters = filters or {}
    conditions = [{key: filters[key]} for key in EQUALITY_FILTERS if filters.get(key)]
    if filters.get("date_from"):
        conditions.append({"created_ts": {"$gte": _day_ts(filters["date_from"])}})
    if filters.get("date_to"):
        conditions.append({"created_ts": {"$lte": _day_ts(filters["date_to"], end_of_day=True)}})
    if not conditions:
        return None
    return conditions[0] if len(conditions) == 1 else {"$and": conditions}


def search(query, k=6, filters=None):
    """Return up to k hits as {ticket_id, document, metadata, distance, similarity}."""
    collection = get_collection()
    total = collection.count()
    if total == 0:
        return []
    result = collection.query(
        query_embeddings=embed([query]),
        n_results=min(k, total),
        where=build_where(filters),
        include=["documents", "metadatas", "distances"],
    )
    return [
        {
            "ticket_id": ticket_id,
            "document": document,
            "metadata": metadata,
            "distance": distance,
            "similarity": 1 - distance,
        }
        for ticket_id, document, metadata, distance in zip(
            result["ids"][0], result["documents"][0], result["metadatas"][0], result["distances"][0]
        )
    ]
