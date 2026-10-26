"""Index data/tickets.json into the persistent Chroma collection.

Usage: python scripts/ingest.py [--reset]
Works from any working directory.
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest import ingest  # noqa: E402
from app.vectorstore import get_collection  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Index the ops tickets into ChromaDB.")
    parser.add_argument("--reset", action="store_true",
                        help="delete and recreate the collection before indexing")
    args = parser.parse_args()

    start = time.perf_counter()
    indexed = ingest(reset=args.reset)
    elapsed = time.perf_counter() - start

    print(f"Tickets indexed : {indexed}")
    print(f"Collection count: {get_collection().count()}")
    print(f"Time taken      : {elapsed:.1f}s")


if __name__ == "__main__":
    main()
