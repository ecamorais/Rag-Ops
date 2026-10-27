"""Retrieval-only evaluation of the golden question set (tests/golden_questions.json). No model call, no quota.

Every question goes through rag.answer_question with llm.generate_answer replaced by a stub that cites nothing, so
result["sources"] is the retrieved context in retrieval order. Any attempt to create a real Gemini client raises. The
index is a COPY of chroma_db in a short temp folder (oh*), deleted at the end; the original is never opened.

Usage (from the ops-rag folder):
    python scripts/evaluate.py                 # markdown report on stdout
    python scripts/evaluate.py --out report.md # also write it to a file (nothing is written without --out)
"""
import argparse
import contextlib
import hashlib
import json
import os
import shutil
import sys
import tempfile
import types
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
K_VALUES = (3, 6, 10)  # 6 is the app default
DEFAULT_K = 6
EXTRA = "extra (assistant-drafted, not blind)"
WEAK_TYPES = {"known_weak_aggregation": "known weak: aggregation / counting", "known_weak_vocabulary": "known weak: vocabulary mismatch"}
TYPE_LABELS = {"exact_id": "exact id", "cluster": "cluster / topic", "vague": "vague ticket", "near_duplicate": "near-duplicate pair", "filter": "with filters"}


# ---------- metrics (pure functions) ----------
def first_rank(ranked, expected):
    """1-based position of the first expected id in the ranked list, or None."""
    wanted = set(expected)
    for position, ticket_id in enumerate(ranked, 1):
        if ticket_id in wanted:
            return position
    return None


def score(ranked, expected, k):
    """Metrics of one question at cut-off k. recall = found / n_expected; capped = found / min(k, n_expected)."""
    if not expected:
        raise ValueError("score() needs at least one expected id")
    top = list(ranked)[:k]
    found = len(set(top) & set(expected))
    rank = first_rank(top, expected)
    return {"found": found, "total": len(expected), "hit": found > 0, "recall": found / len(expected),
            "capped": found / min(k, len(expected)), "rr": 1 / rank if rank else 0.0, "rank": rank}


def summarise(scores):
    n = len(scores)
    if n == 0:
        return {"n": 0}
    return {"n": n, "hit": sum(s["hit"] for s in scores) / n, "recall": sum(s["recall"] for s in scores) / n,
            "capped": sum(s["capped"] for s in scores) / n, "mrr": sum(s["rr"] for s in scores) / n}


def violated_keys(record, filters):
    """Which filter keys a ticket record breaks (the same meaning as app/vectorstore.build_where)."""
    bad = [key for key in ("category", "priority", "status", "team") if key in filters and record[key] != filters[key]]
    day = record["created_at"][:10]
    if "date_from" in filters and day < filters["date_from"]:
        bad.append("date_from")
    if "date_to" in filters and day > filters["date_to"]:
        bad.append("date_to")
    return bad


def evaluate(questions, answer_fn, records):
    """Run every question at every k. answer_fn(question, filters, k) -> the dict that rag.answer_question returns."""
    results = {}
    for k in K_VALUES:
        rows = []
        for q in questions:
            result = answer_fn(q["question"], q.get("filters") or None, k)
            ranked = [s["ticket_id"] for s in result["sources"]]
            row = {"q": q, "ranked": ranked, "sources": result["sources"], "missing": result.get("missing_references", []),
                   "score": score(ranked, q["expected"], k) if q["expected"] else None, "missing_ok": None, "violations": []}
            if q.get("expect_missing"):
                row["missing_ok"] = not ranked and all(m in row["missing"] for m in q["expect_missing"])
            if q.get("filter_must_hold"):
                row["violations"] = [t for t in ranked if violated_keys(records[t], q["filters"])]
            rows.append(row)
        results[k] = rows
    return results


# ---------- environment: temp COPY of the index, stub model, guard against a real client ----------
def release_chroma():
    """Close the sqlite file of the copy (Windows cannot delete an open file)."""
    import gc

    from chromadb.api.shared_system_client import SharedSystemClient

    from app import vectorstore

    SharedSystemClient.clear_system_cache()
    vectorstore._client.cache_clear()
    gc.collect()


@contextlib.contextmanager
def retrieval_only_environment(chroma_src):
    """Yields a namespace with answer_fn, guard() and info. The temp copy is removed on exit; env.leftover says whether it is gone."""
    copy_root = Path(tempfile.mkdtemp(prefix="oh"))
    env = types.SimpleNamespace(copy_root=copy_root, leftover=None)
    try:
        shutil.copytree(chroma_src, copy_root / "c")
        os.environ["CHROMA_DIR"] = str(copy_root / "c")
        os.environ["GEMINI_API_KEY"] = ""  # an empty key also makes a real call impossible
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        sys.path.insert(0, str(ROOT))
        from app import llm, rag, vectorstore
        from app.config import settings

        if Path(settings.chroma_dir).resolve() != (copy_root / "c").resolve():
            raise RuntimeError(f"Chroma must be the copy, not {settings.chroma_dir}")
        calls = []

        def stub(system, prompt):
            calls.append(1)
            return llm.GeminiAnswer(answer="(retrieval-only stub)", found=False, citations=[])

        def forbidden(*args, **kwargs):
            raise RuntimeError("REAL GEMINI CLIENT TOUCHED: evaluate.py must never call the model")

        env.answer_fn = lambda question, filters, k: rag.answer_question(question, filters=filters, k=k)
        env.info = {"indexed": vectorstore.get_collection().count(), "threshold": settings.similarity_threshold,
                    "embed_model": getattr(settings, "embed_model", "?")}
        env.guard = lambda: {"stub_calls": len(calls), "real_client_created": llm._client is not None,
                             "http_attempts": getattr(llm, "http_attempts", 0)}
        with mock.patch.object(llm, "generate_answer", stub), mock.patch.object(llm, "get_client", forbidden), \
                mock.patch.object(llm.genai, "Client", forbidden):
            yield env
    finally:
        try:
            release_chroma()
        except Exception:  # nothing was opened yet
            pass
        shutil.rmtree(copy_root, ignore_errors=True)
        env.leftover = copy_root.exists()


# ---------- report ----------
def sha256_lf(path):
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def f2(x):
    return f"{x:.2f}"


def label_of(row):
    return row["q"]["id"]


def render(golden, results, env_info, guard, tickets_sha, golden_sha):
    questions = [r["q"] for r in results[K_VALUES[0]]]
    meta = golden["meta"]
    out = ["# Retrieval evaluation", "",
           f"- Run date: {date.today().isoformat()} | golden set frozen on {meta.get('frozen_on')} | {len(questions)} questions",
           f"- sha256 of data/tickets.json (line endings normalised to LF): {tickets_sha}",
           f"- sha256 of the golden set (LF): {golden_sha}",
           f"- Index: {env_info['indexed']} tickets, embedding model {env_info['embed_model']}, similarity threshold {env_info['threshold']}",
           f"- Model: stub only; calls to the stub {guard['stub_calls']}, real client created: {'YES' if guard['real_client_created'] else 'no'}, HTTP attempts: {guard['http_attempts']}",
           "- Measures retrieval only, not answer quality. `expected` is a lower bound (unlabelled tickets can also fit a question).", ""]

    def rows_where(rows, pick):
        return [r for r in rows if r["score"] and pick(r["q"]["type"])]

    groups = [(TYPE_LABELS[t], lambda ty, t=t: ty == t) for t in TYPE_LABELS]
    groups.append(("HEADLINE: all types except known-weak", lambda ty: ty not in WEAK_TYPES))
    groups.append(("HEADLINE without the extra type", lambda ty: ty not in WEAK_TYPES and ty != EXTRA))
    groups += [(label, lambda ty, t=t: ty == t) for t, label in WEAK_TYPES.items()]
    groups.append((EXTRA + " *", lambda ty: ty == EXTRA))

    for k in K_VALUES:
        rows = results[k]
        out += [f"## k = {k}" + (" (app default)" if k == DEFAULT_K else ""), "",
                "| Group | n | Hit rate | Recall | Capped recall | MRR |", "|---|---|---|---|---|---|"]
        for label, pick in groups:
            s = summarise([r["score"] for r in rows_where(rows, pick)])
            out.append(f"| {label} | {s['n']} | " + (" | ".join(f2(s[m]) for m in ("hit", "recall", "capped", "mrr")) if s["n"] else "- | - | - | -") + " |")
        out.append("")
        found = total = 0
        names = []
        for r in rows:
            also = r["q"].get("also_relevant") or []
            if r["q"]["type"] == EXTRA and also:
                names.append(label_of(r))
                found += len(set(r["ranked"][:k]) & set(also))
                total += len(also)
        out.append(f"\\* also_relevant (not in any headline row), {', '.join(names) or 'none'}: found {found} of {total} at k={k}.")
        for r in rows:
            if r["missing_ok"] is not None:
                out.append(f"- {label_of(r)} (expect_missing {r['q']['expect_missing']}): {'PASS' if r['missing_ok'] else 'FAIL'}; retrieved {r['ranked']}, reported missing {r['missing']}.")
        checked = [r for r in rows if r["q"].get("filter_must_hold")]
        out.append(f"- filter_must_hold: {sum(len(r['ranked']) for r in checked)} hits checked in {len(checked)} questions, "
                   f"{sum(len(r['violations']) for r in checked)} violations" + "".join(f" ({label_of(r)}: {r['violations']})" for r in checked if r["violations"]) + ".")
        out.append("")

    out += ["## Per question", "", "Found = expected ids found in the top k / number expected. First hit = rank within the top 10.", "",
            "| Id | Type | Flags | Question | First hit | Found @3 | Found @6 | Found @10 |", "|---|---|---|---|---|---|---|---|"]
    for i, q in enumerate(questions):
        flags = ", ".join(x for x, on in (("weak", q["type"] in WEAK_TYPES), ("vocab_gap", q.get("vocab_gap")), ("filter", bool(q.get("filters"))), ("n/a labels", not q["expected"])) if on)
        by_k = {k: results[k][i] for k in K_VALUES}
        top = by_k[K_VALUES[-1]]
        rank = top["score"]["rank"] if top["score"] else None
        cells = [(f"{by_k[k]['score']['found']}/{by_k[k]['score']['total']}" if by_k[k]["score"] else ("ok" if by_k[k]["missing_ok"] else "FAIL")) for k in K_VALUES]
        text = q["question"] if len(q["question"]) <= 60 else q["question"][:57] + "..."
        out.append(f"| {q['id']} | {q['type'] if q['type'] != EXTRA else 'extra'} | {flags or '-'} | {text.replace('|', '/')} | {rank or '-'} | " + " | ".join(cells) + " |")
    out.append("")

    rows = results[DEFAULT_K]
    wide = {r["q"]["id"]: r for r in results[K_VALUES[-1]]}
    out += [f"## Misses at k = {DEFAULT_K}", ""]
    for k in K_VALUES:
        if k != DEFAULT_K:
            out.append(f"- k={k}: no hit for {[label_of(r) for r in results[k] if r['score'] and not r['score']['hit']] or 'none'}")
    out.append("")
    misses = [r for r in rows if r["score"] and not r["score"]["hit"]]
    for r in misses:
        q = r["q"]
        out.append(f"### {q['id']} ({q['type']}{', known weak' if q['type'] in WEAK_TYPES else ''}): {q['question']}")
        out.append(f"- expected {q['expected']}; first hit within the top 10: rank {wide[q['id']]['score']['rank'] or 'none'}")
        out.append("- retrieved at k=6: " + ("; ".join(f"{i}. {s['ticket_id']} ({s['score']}) {s['title']}" for i, s in enumerate(r["sources"], 1)) or "nothing"))
        comp = [t for t in r["ranked"] if t in (q.get("unlabelled_competitors") or [])]
        if q.get("unlabelled_competitors") is not None:
            out.append(f"- retrieved ids that are listed unlabelled competitors: {comp or 'none'}")
        out.append("")
    if not misses:
        out += ["No question missed completely.", ""]
    out += [f"## Incomplete at k = {DEFAULT_K} (hit, but not every expected id found)", ""]
    for r in rows:
        s = r["score"]
        if s and s["hit"] and s["found"] < s["total"]:
            out.append(f"- {label_of(r)} {s['found']}/{s['total']}, not found: {sorted(set(r['q']['expected']) - set(r['ranked']))}")
    return "\n".join(out) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Retrieval-only evaluation (no model call).")
    parser.add_argument("--golden", default=str(ROOT / "tests" / "golden_questions.json"))
    parser.add_argument("--chroma-src", default=str(ROOT / "chroma_db"), help="the index to COPY (never opened directly)")
    parser.add_argument("--out", help="also write the report to this file (default: stdout only)")
    args = parser.parse_args(argv)

    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    if not golden["meta"].get("frozen"):
        sys.exit("The golden set is not frozen (meta.frozen is false): refusing to score it.")
    questions = [q for q in golden["questions"] if q["question"]]
    skipped = len(golden["questions"]) - len(questions)
    records = {t["ticket_id"]: t for t in json.loads((ROOT / "data" / "tickets.json").read_text(encoding="utf-8"))}

    with retrieval_only_environment(args.chroma_src) as env:
        results = evaluate(questions, env.answer_fn, records)
        guard = env.guard()
        report = render(golden, results, env.info, guard, sha256_lf(ROOT / "data" / "tickets.json"), sha256_lf(args.golden))
    if skipped:
        report += f"\nNote: {skipped} empty question slot(s) skipped.\n"
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # LF also on Windows, so a redirected report matches --out
    sys.stdout.write(report)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8", newline="\n")
    print(f"temp copy {env.copy_root} removed: {not env.leftover}", file=sys.stderr)
    return 1 if guard["real_client_created"] or guard["http_attempts"] else 0


if __name__ == "__main__":
    sys.exit(main())
