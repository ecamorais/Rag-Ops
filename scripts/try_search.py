"""Try the ticket search from the command line.

Usage: python scripts/try_search.py "database timeouts after failover" --k 5 --priority P1-Critical
Works from any working directory.
"""

import argparse
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.vectorstore import search  # noqa: E402


def iso_date(value):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise argparse.ArgumentTypeError(f"{value!r} is not a YYYY-MM-DD date")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a valid date")
    return value


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


def format_hit(rank, hit):
    meta = hit["metadata"]
    # The title is not in the metadata; it is the "Title:" line of the stored document.
    match = re.search(r"^Title: (.*)$", hit["document"], re.MULTILINE)
    title = match.group(1) if match else "(no title)"
    return (
        f"{rank:>2}. {hit['ticket_id']}  similarity={hit['similarity']:.3f}  {meta['priority']}  "
        f"{meta['status']}  {meta['category']}  {meta['created_at']}\n"
        f"    {title}"
    )


def main():
    parser = argparse.ArgumentParser(description="Search the ops tickets.")
    parser.add_argument("query", help="search text")
    parser.add_argument("--k", type=positive_int, default=6, help="number of hits (default 6)")
    parser.add_argument("--category")
    parser.add_argument("--priority")
    parser.add_argument("--status")
    parser.add_argument("--team")
    parser.add_argument("--date-from", type=iso_date, metavar="YYYY-MM-DD",
                        help="created on or after this day (UTC)")
    parser.add_argument("--date-to", type=iso_date, metavar="YYYY-MM-DD",
                        help="created on or before this day (UTC)")
    args = parser.parse_args()

    filters = {
        "category": args.category,
        "priority": args.priority,
        "status": args.status,
        "team": args.team,
        "date_from": args.date_from,
        "date_to": args.date_to,
    }
    active = {key: value for key, value in filters.items() if value}

    sys.stdout.reconfigure(encoding="utf-8")
    print(f"Query  : {args.query}")
    print(f"Filters: {active or 'none'}")
    hits = search(args.query, k=args.k, filters=active)
    if not hits:
        print("No results.")
        return
    for rank, hit in enumerate(hits, start=1):
        print(format_hit(rank, hit))


if __name__ == "__main__":
    main()
