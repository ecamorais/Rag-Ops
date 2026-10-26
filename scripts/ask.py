"""Ask questions about the ops tickets from the command line.

Usage:
  python scripts/ask.py                          interactive; type "exit" to quit
  python scripts/ask.py --once "What is OPS-1001?" --priority P1-Critical --k 6
Works from any working directory.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import llm, rag  # noqa: E402


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


def print_result(result):
    print(f"\n{result['answer']}\n")
    print(f"found={result['found']}  citations={result['citations'] or 'none'}")
    if result["unverified_citations"]:
        print(f"unverified citations (dropped): {result['unverified_citations']}")
    if result["missing_references"]:
        print(f"missing references: {result['missing_references']}")
    print(f"sources ({result['retrieved_count']} retrieved, cited first):")
    for source in result["sources"]:
        score = "exact" if source["score"] is None else f"{source['score']:.3f}"
        print(f"  {source['ticket_id']}  [{score}]  {source['priority']}  {source['status']}  "
              f"{source['category']}\n      {source['title']}")


def print_counters():
    print(f"[llm calls={rag.llm_call_count}  http attempts={llm.http_attempts}  retries={llm.retries}]")


def ask(question, filters, k, history):
    """Answer one question. Returns the result, or None when the LLM layer failed."""
    try:
        result = rag.answer_question(question, filters=filters, k=k, history=history)
    except llm.LlmError as error:
        print(f"\nError: {error}")
        return None
    print_result(result)
    return result


def main():
    parser = argparse.ArgumentParser(description="Ask the ops tickets a question.")
    parser.add_argument("--once", metavar="QUESTION", help="answer one question and exit")
    parser.add_argument("--k", type=positive_int, default=6, help="tickets in the context (default 6)")
    parser.add_argument("--category")
    parser.add_argument("--priority")
    parser.add_argument("--status")
    parser.add_argument("--team")
    args = parser.parse_args()

    filters = {key: getattr(args, key) for key in ("category", "priority", "status", "team")}
    filters = {key: value for key, value in filters.items() if value}

    sys.stdout.reconfigure(encoding="utf-8")
    if args.once:
        result = ask(args.once, filters, args.k, history=None)
        print_counters()
        sys.exit(0 if result else 1)

    print(f"Filters: {filters or 'none'}  k={args.k}. Type a question, or 'exit' to quit.")
    history = []
    while True:
        try:
            question = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not question:
            continue
        if question.lower() in ("exit", "quit"):
            break
        result = ask(question, filters, args.k, history)
        if result:
            history += [{"role": "user", "content": question},
                        {"role": "assistant", "content": result["answer"]}]
        print_counters()


if __name__ == "__main__":
    main()
