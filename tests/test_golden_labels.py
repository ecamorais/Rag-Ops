"""The golden question set (tests/golden_questions.json) against the data it is labelled from. No retrieval, no model, no app import.

What is checked, and where every expected value comes from:
  * Ticket ids, clusters, near-duplicate pairs, vague ids, filter value lists and the created range: data/tickets.json and data/clusters.json.
  * Text rules: the rule each question states in its own `source` field (quoted next to each test), recomputed from the tickets. A cluster id is
    read from `source` with CL-\\d+ and only used to look the cluster up in data/clusters.json; the expected ids are never taken from the
    question's own `expected` list when that list is the thing being checked.
  * The type counts (4 / 10 / 3 / 4 / 4 / 3 / 5): the repository owner's brief and the group table in docs/evaluation.md.
  * `frozen_on`: a well-formed YYYY-MM-DD date that is not earlier than the newest created_at in data/tickets.json (no clock).
  * The file's own sha256 (LF-normalised, like scripts/evaluate.py) is pinned in PIN_HISTORY. Editing the frozen file therefore fails the pin test
    until the test is updated together with a change-log entry in meta.changes_after_scores.
  * scripts/evaluate.py refuses a golden set that is not frozen (stub-free: it exits before it builds anything).
Tests named "consistency (own rule)" check rules that no document states; they are this file's own additions.
"""
import collections
import copy
import hashlib
import importlib
import importlib.util
import json
import re
import shutil
import tempfile
from datetime import date
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
GOLDEN_PATH = TESTS_DIR / "golden_questions.json"
TICKETS_PATH = ROOT / "data" / "tickets.json"
CLUSTERS_PATH = ROOT / "data" / "clusters.json"
EVALUATE_PATH = ROOT / "scripts" / "evaluate.py"

# Every accepted version of the golden file, oldest first. The first entry is the frozen state. Each later entry needs one entry in
# meta.changes_after_scores, so len(changes_after_scores) == len(PIN_HISTORY) - 1.
PIN_HISTORY = [
    "b97b10372e08b9da3fe96c9a7c294b8d40c7f8f015337d198c1efa4bbcf9ba01",
]

EXTRA = "extra (assistant-drafted, not blind)"
EXPECTED_TYPE_COUNTS = {"exact_id": 4, "cluster": 10, "vague": 3, "near_duplicate": 4, "filter": 4,
                        "known_weak_aggregation": 2, "known_weak_vocabulary": 1, EXTRA: 5}
FILTER_KEYS = {"category", "priority", "status", "team", "date_from", "date_to"}
AUTHOR_WORDING = "drafted by the assistant and adopted unchanged by the repository owner"

GOLDEN_BYTES = GOLDEN_PATH.read_bytes()
GOLDEN = json.loads(GOLDEN_BYTES.decode("utf-8"))
META = GOLDEN["meta"]
QUESTIONS = GOLDEN["questions"]
TICKETS = {t["ticket_id"]: t for t in json.loads(TICKETS_PATH.read_text(encoding="utf-8"))}
CLUSTER_DATA = json.loads(CLUSTERS_PATH.read_text(encoding="utf-8"))
CLUSTERS = {c["cluster_id"]: c for c in CLUSTER_DATA["clusters"]}
DUPLICATE_PAIRS = {frozenset((d["original_id"], d["duplicate_id"])) for d in CLUSTER_DATA["near_duplicates"]}
VAGUE_IDS = set(CLUSTER_DATA["vague_ticket_ids"])


def sha256_lf(data):
    """Same as sha256_lf in scripts/evaluate.py: line endings normalised to LF before hashing."""
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def valid_date(text):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return False
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def day_of(ticket):
    return ticket["created_at"][:10]


def passes(ticket, filters):
    """Whether a ticket passes a question's filters: equal category / priority / status / team, created day inside the date window."""
    for key in ("category", "priority", "status", "team"):
        if key in filters and ticket[key] != filters[key]:
            return False
    return filters.get("date_from", "0000-00-00") <= day_of(ticket) <= filters.get("date_to", "9999-99-99")


def cluster_ids_in(text):
    return re.findall(r"CL-\d+", text)


def ids_in(text):
    return re.findall(r"OPS-\d+", text)


def of_type(kind):
    return [q for q in QUESTIONS if q["type"] == kind]


def each(questions):
    return pytest.mark.parametrize("q", questions, ids=[q["id"] for q in questions])


def single_cluster(q):
    """The one cluster a question's source names. More than one is a failure on purpose: the rule is not loosened."""
    named = cluster_ids_in(q["source"])
    assert len(named) == 1, f"{q['id']}: source must name exactly one CL id, found {named} in {q['source']!r}"
    assert named[0] in CLUSTERS, f"{q['id']}: {named[0]} is not in data/clusters.json"
    return CLUSTERS[named[0]]


# --- the file is frozen ---------------------------------------------------------------------------------------------------------

def test_the_golden_set_is_frozen():
    assert META["frozen"] is True


def test_frozen_on_is_a_well_formed_date_not_earlier_than_the_newest_ticket():
    frozen_on = META["frozen_on"]
    assert isinstance(frozen_on, str) and valid_date(frozen_on), f"frozen_on is not a YYYY-MM-DD date: {frozen_on!r}"
    newest = max(day_of(t) for t in TICKETS.values())
    assert frozen_on >= newest, f"frozen_on {frozen_on} is earlier than the newest ticket day {newest}"


def test_the_set_says_it_was_written_before_any_retrieval_run():
    assert META["written_before_any_retrieval_run"] is True


def test_the_file_has_no_carriage_return():
    assert b"\r" not in GOLDEN_BYTES


def test_the_false_statement_written_by_the_repository_owner_does_not_appear():
    """The extra questions were drafted by the assistant; a corrected wrong statement must not come back."""
    assert b"written by the repository owner" not in GOLDEN_BYTES


def test_the_golden_file_matches_the_pinned_hash():
    current = sha256_lf(GOLDEN_BYTES)
    assert current == PIN_HISTORY[-1], (
        f"tests/golden_questions.json changed. Its sha256 (LF-normalised) is now {current}. If the edit is intended: add an entry "
        f"(date, id, field, old, new, reason) to meta.changes_after_scores, recompute this hash (adding the entry changes it), append "
        f"it to PIN_HISTORY in tests/test_golden_labels.py and regenerate docs/evaluation.md. If it is not intended: restore the file "
        f"from git.")


def test_the_pin_history_is_well_formed():
    assert all(re.fullmatch(r"[0-9a-f]{64}", h) for h in PIN_HISTORY)
    assert len(set(PIN_HISTORY)) == len(PIN_HISTORY)


def test_every_accepted_edit_has_a_change_log_entry():
    changes = META["changes_after_scores"]
    assert isinstance(changes, list)
    assert len(changes) == len(PIN_HISTORY) - 1, f"{len(changes)} change-log entries for {len(PIN_HISTORY)} pinned versions"


# The change-log entry shape is a rule INTRODUCED BY THIS TEST (nothing else defines it): an object with exactly the keys date, id, field, old,
# new, reason; date a real YYYY-MM-DD date; id a question id (G or A and two digits); field and reason non-empty text; old and new different.
CHANGE_KEYS = ("date", "id", "field", "old", "new", "reason")


def change_entry_problems(entry):
    """The ways an entry of meta.changes_after_scores breaks the shape rule introduced by this test. An empty list means it is well formed."""
    if not isinstance(entry, dict):
        return ["entry must be an object"]
    problems = []
    if set(entry) != set(CHANGE_KEYS):
        problems.append(f"keys must be exactly {list(CHANGE_KEYS)}, found {sorted(entry)}")
    if not (isinstance(entry.get("date"), str) and valid_date(entry["date"])):
        problems.append("date must be a real YYYY-MM-DD date")
    if not (isinstance(entry.get("id"), str) and re.fullmatch(r"[GA]\d\d", entry["id"])):
        problems.append("id must be a question id like G05 or A02")
    if not (isinstance(entry.get("field"), str) and entry["field"].strip()):
        problems.append("field must be non-empty text")
    if "old" in entry and "new" in entry and entry["old"] == entry["new"]:
        problems.append("old and new must differ")
    if not (isinstance(entry.get("reason"), str) and entry["reason"].strip()):
        problems.append("reason must be non-empty text")
    return problems


def test_the_change_log_entries_have_the_stated_shape():
    for entry in META["changes_after_scores"]:
        assert change_entry_problems(entry) == [], entry


GOOD_ENTRY = {"date": "2026-11-01", "id": "G05", "field": "expected", "old": ["OPS-1266"], "new": ["OPS-1266", "OPS-1267"], "reason": "label added"}


def test_a_well_formed_change_entry_has_no_problems():
    assert change_entry_problems(GOOD_ENTRY) == []


BAD_ENTRIES = [  # (id, entry, text the problem list must mention)
    ("missing key", {k: v for k, v in GOOD_ENTRY.items() if k != "reason"}, "keys must be exactly"),
    ("extra key", {**GOOD_ENTRY, "author": "x"}, "keys must be exactly"),
    ("impossible date", {**GOOD_ENTRY, "date": "2026-02-30"}, "date"),
    ("date not ISO", {**GOOD_ENTRY, "date": "1 Nov 2026"}, "date"),
    ("bad question id", {**GOOD_ENTRY, "id": "Q5"}, "id must be"),
    ("blank reason", {**GOOD_ENTRY, "reason": "   "}, "reason"),
    ("empty field", {**GOOD_ENTRY, "field": ""}, "field"),
    ("old equals new", {**GOOD_ENTRY, "new": GOOD_ENTRY["old"]}, "old and new"),
    ("not an object", "G05 expected changed", "entry must be an object"),
]


@pytest.mark.parametrize("entry, needle", [(e, n) for _, e, n in BAD_ENTRIES], ids=[i for i, _, _ in BAD_ENTRIES])
def test_a_malformed_change_entry_is_reported(entry, needle):
    assert any(needle in p for p in change_entry_problems(entry)), change_entry_problems(entry)


# --- shape of the set -----------------------------------------------------------------------------------------------------------

REQUIRED_FIELDS = {"id", "type", "question", "filters", "expected", "known_weak", "source", "rationale"}


@each(QUESTIONS)
def test_every_question_has_the_required_fields(q):
    assert REQUIRED_FIELDS <= set(q), sorted(REQUIRED_FIELDS - set(q))
    assert isinstance(q["question"], str) and q["question"].strip(), "a frozen set has no empty question slot"
    assert isinstance(q["filters"], dict) and isinstance(q["expected"], list) and isinstance(q["known_weak"], bool)
    assert q["source"].strip() and q["rationale"].strip()


def test_question_ids_are_unique_and_well_formed():
    ids = [q["id"] for q in QUESTIONS]
    assert len(ids) == len(set(ids)), [i for i, n in collections.Counter(ids).items() if n > 1]
    assert all(re.fullmatch(r"[GA]\d\d", i) for i in ids), ids


def test_the_type_counts_are_the_stated_ones():
    assert dict(collections.Counter(q["type"] for q in QUESTIONS)) == EXPECTED_TYPE_COUNTS
    assert len(QUESTIONS) == 33


def load_evaluate():
    spec = importlib.util.spec_from_file_location("evaluate_under_test", EVALUATE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def evaluate():
    return load_evaluate()


def test_every_type_is_known_to_evaluate_py(evaluate):
    """An unknown type would silently drop out of every group of the report."""
    known = set(evaluate.TYPE_LABELS) | set(evaluate.WEAK_TYPES) | {evaluate.EXTRA}
    assert {q["type"] for q in QUESTIONS} <= known
    assert evaluate.EXTRA == EXTRA


def test_the_author_field_exists_for_the_extra_questions_only():
    assert {q["id"] for q in QUESTIONS if "author" in q} == {q["id"] for q in of_type(EXTRA)}


# --- every id exists in the data -------------------------------------------------------------------------------------------------

def test_the_dataset_is_800_tickets_with_contiguous_ids():
    """G04 source: 'ids run OPS-1001 to OPS-1800'; the dataset line says 800 tickets."""
    assert sorted(TICKETS) == [f"OPS-{n}" for n in range(1001, 1801)]


@each(QUESTIONS)
def test_every_listed_id_exists_and_the_declared_missing_ones_do_not(q):
    for field in ("expected", "also_relevant", "unlabelled_competitors"):
        missing = [i for i in q.get(field, []) if i not in TICKETS]
        assert not missing, f"{field} ids that do not exist: {missing}"
    declared_missing = q.get("expect_missing", [])
    assert not [i for i in declared_missing if i in TICKETS], "expect_missing names an id that exists"
    named = set(ids_in(q["source"] + " " + q["rationale"])) - set(declared_missing)
    assert not [i for i in named if i not in TICKETS], "source or rationale names an id that does not exist"


@each(QUESTIONS)
def test_the_id_lists_do_not_repeat_or_overlap(q):
    lists = [q["expected"], q.get("also_relevant", []), q.get("unlabelled_competitors", [])]
    for ids in lists:
        assert len(ids) == len(set(ids)), "an id is listed twice"
    a, b, c = (set(ids) for ids in lists)
    assert not (a & b or a & c or b & c), "expected, also_relevant and unlabelled_competitors overlap"


# --- recompute the labels from the data -----------------------------------------------------------------------------------------

@each(of_type("exact_id"))
def test_exact_id_expected_is_whoever_holds_the_reference(q):
    """Sources: 'ticket id in the question', 'PO-165700 appears in OPS-1013 only', 'INV-323839 appears in OPS-1013 only', 'OPS-9999 does
    not exist'. The reference holders are computed from the ticket records."""
    refs = re.findall(r"(?:OPS|PO|INV)-\d+", q["question"])
    assert refs
    holders, missing = set(), []
    for ref in refs:
        if ref.startswith("OPS-"):
            found = [ref] if ref in TICKETS else []
        else:
            pattern = re.compile(r"(?<![\w-])" + re.escape(ref) + r"(?!\d)")
            found = [i for i, t in TICKETS.items() if pattern.search(json.dumps(t, ensure_ascii=False))]
        if not found:
            missing.append(ref)
        holders |= set(found)
    assert sorted(q["expected"]) == sorted(holders)
    assert sorted(q.get("expect_missing", [])) == sorted(missing)


@each(of_type("cluster"))
def test_a_cluster_question_expects_exactly_the_cluster_its_source_names(q):
    cluster = single_cluster(q)
    assert sorted(q["expected"]) == sorted(cluster["ticket_ids"])


@each(of_type("near_duplicate"))
def test_a_near_duplicate_question_expects_a_listed_pair_named_in_its_source(q):
    """Source form: 'near_duplicates: OPS-1048 / OPS-1050'."""
    assert frozenset(q["expected"]) in DUPLICATE_PAIRS
    assert set(ids_in(q["source"])) == set(q["expected"])


@each(of_type("vague"))
def test_a_vague_question_expects_the_vague_ticket_its_source_quotes(q):
    """Source form: "vague_ticket_ids: OPS-1471 'Slow' / 'Everything is slow today.'". The quotes are the ticket's title and description."""
    assert len(q["expected"]) == 1 and set(q["expected"]) <= VAGUE_IDS
    assert ids_in(q["source"]) == q["expected"]
    title, description = re.findall(r"'([^']*)'", q["source"])
    ticket = TICKETS[q["expected"][0]]
    assert (ticket["title"], ticket["description"]) == (title, description)


@each(of_type("filter"))
def test_a_filter_question_expects_the_cluster_members_that_pass_its_filters(q):
    """Source form: 'CL-26 members whose category is Infrastructure'. The members are recomputed from the tickets."""
    cluster = single_cluster(q)
    passing = [i for i in cluster["ticket_ids"] if passes(TICKETS[i], q["filters"])]
    assert sorted(q["expected"]) == sorted(passing)


def test_g23_expects_the_lotus_late_delivery_tickets():
    """Source: "titles 'Lotus Stretch Wrap delivery late against PO-...' (3 tickets); NorthStar Components and Apex Packaging Ltd have one
    each"."""
    q = next(x for x in QUESTIONS if x["id"] == "G23")
    late = {i: t["title"].split(" delivery late")[0] for i, t in TICKETS.items() if "delivery late" in t["title"]}
    assert sorted(q["expected"]) == sorted(i for i, vendor in late.items() if vendor == "Lotus Stretch Wrap")
    assert collections.Counter(late.values()) == {"Lotus Stretch Wrap": 3, "NorthStar Components": 1, "Apex Packaging Ltd": 1}


def test_g24_expects_the_five_tickets_of_the_cluster_its_source_names():
    """Source: 'CL-22 (5 tickets)'."""
    q = next(x for x in QUESTIONS if x["id"] == "G24")
    cluster = single_cluster(q)
    assert sorted(q["expected"]) == sorted(cluster["ticket_ids"]) and len(cluster["ticket_ids"]) == 5


def test_g25_expects_the_badge_tickets_at_the_hq_server_room():
    """Source: "tickets titled 'cannot badge in' / 'Badge access failing' at the HQ server room"."""
    q = next(x for x in QUESTIONS if x["id"] == "G25")
    badge = [i for i, t in TICKETS.items() if re.search(r"badge (in|access)", t["title"], re.I) and "HQ server room" in t["title"]]
    assert sorted(q["expected"]) == sorted(badge)


def test_a01_expects_the_tickets_that_name_northstar_components():
    """Source: 'rule: NorthStar Components named in title or description (12 tickets, found by text search)'. The competitors name the vendor
    only in the resolution text (rationale of A01)."""
    q = next(x for x in QUESTIONS if x["id"] == "A01")
    named = [i for i, t in TICKETS.items() if re.search("northstar components", t["title"] + " " + t["description"], re.I)]
    assert sorted(q["expected"]) == sorted(named)
    assert len(named) == int(re.search(r"\((\d+) tickets", q["source"]).group(1))
    for i in q["unlabelled_competitors"]:
        t = TICKETS[i]
        assert "northstar components" not in (t["title"] + " " + t["description"]).lower()
        assert "northstar components" in t["resolution_notes"].lower()


@each([q for q in of_type(EXTRA) if q["id"] != "A01"])
def test_an_extra_question_expects_all_tickets_of_the_clusters_its_source_names(q):
    """A02 to A05 sources name their clusters (A05 names three); the expected ids are the members of all of them."""
    named = cluster_ids_in(q["source"])
    assert named and all(c in CLUSTERS for c in named), named
    assert sorted(q["expected"]) == sorted(i for c in named for i in CLUSTERS[c]["ticket_ids"])


# --- filters against the data ---------------------------------------------------------------------------------------------------

DISTINCT = {key: {t[key] for t in TICKETS.values()} for key in ("category", "priority", "status", "team")}
CREATED_MIN = min(day_of(t) for t in TICKETS.values())
CREATED_MAX = max(day_of(t) for t in TICKETS.values())


@each(QUESTIONS)
def test_filter_keys_and_values_exist_in_the_data(q):
    filters = q["filters"]
    assert set(filters) <= FILTER_KEYS, sorted(set(filters) - FILTER_KEYS)
    for key in ("category", "priority", "status", "team"):
        if key in filters:
            assert filters[key] in DISTINCT[key], f"{key} {filters[key]!r} is not one of {sorted(DISTINCT[key])}"
    for key in ("date_from", "date_to"):
        if key in filters:
            assert valid_date(filters[key]), f"{key} {filters[key]!r} is not a YYYY-MM-DD date"
            assert CREATED_MIN <= filters[key] <= CREATED_MAX, f"{key} {filters[key]} is outside the created range {CREATED_MIN} to {CREATED_MAX}"
    if "date_from" in filters and "date_to" in filters:
        assert filters["date_from"] <= filters["date_to"]


@each([q for q in QUESTIONS if q["filters"]])
def test_a_filtered_question_has_tickets_and_its_expected_ids_pass_the_filter(q):
    assert any(passes(t, q["filters"]) for t in TICKETS.values())
    failing = [i for i in q["expected"] if not passes(TICKETS[i], q["filters"])]
    assert not failing, f"expected ids that fail the filters: {failing}"


def test_consistency_own_rule_filter_must_hold_is_true_exactly_where_filters_exist():
    """Consistency (own rule): no document states it. Today it holds for all 33 questions."""
    assert [q["id"] for q in QUESTIONS if bool(q["filters"]) != (q.get("filter_must_hold") is True)] == []


def test_consistency_own_rule_known_weak_is_true_exactly_for_the_known_weak_types():
    """Consistency (own rule): scripts/evaluate.py reads the type, not the flag, so a mismatch would stay silent."""
    assert [q["id"] for q in QUESTIONS if q["known_weak"] != q["type"].startswith("known_weak")] == []


# --- authorship of the extra questions ------------------------------------------------------------------------------------------

@each(of_type(EXTRA))
def test_an_extra_question_carries_the_authorship_wording(q):
    """meta.notes: 'the questions were drafted by the assistant and adopted unchanged by the repository owner; the expected ids were drafted
    by the assistant ... so they are not blind'."""
    assert q["type"] == EXTRA
    assert AUTHOR_WORDING in q["author"] and "not blind" in q["author"]


def test_the_notes_state_the_authorship_rule():
    notes = " ".join(META["notes"])
    assert AUTHOR_WORDING in notes and "not blind" in notes
    assert "author 'repository owner'" in notes


# --- scripts/evaluate.py refuses a golden set that is not frozen ---------------------------------------------------------------

class GuardPassed(Exception):
    """Raised by the stand-in for the first call evaluate.main makes after its frozen guard."""


@pytest.fixture
def guard_run(evaluate, tmp_path, monkeypatch):
    """Returns run(edit): runs evaluate.main on a copy of the golden file whose meta was changed by edit(meta). Nothing it could build exists:
    the index copy, the temp folder and the Gemini client all fail the test if touched. Returns (what main raised, number of guard passes)."""
    reached = []

    def sentinel(*args, **kwargs):
        reached.append(1)
        raise GuardPassed()

    def forbidden(*args, **kwargs):
        raise AssertionError("evaluate.main touched something it must not touch before the frozen guard")

    genai = importlib.import_module("google.genai")
    monkeypatch.setattr(evaluate, "retrieval_only_environment", sentinel)
    monkeypatch.setattr(tempfile, "mkdtemp", forbidden)
    monkeypatch.setattr(shutil, "copytree", forbidden)
    monkeypatch.setattr(genai, "Client", forbidden)

    def run(edit):
        golden = copy.deepcopy(GOLDEN)
        edit(golden["meta"])
        path = tmp_path / "golden.json"
        path.write_text(json.dumps(golden), encoding="utf-8")
        try:
            evaluate.main(["--golden", str(path)])
        except BaseException as error:  # SystemExit is a BaseException
            return error, len(reached)
        return None, len(reached)

    return run


def test_evaluate_py_accepts_the_real_golden_file_past_the_guard(guard_run):
    error, passes_ = guard_run(lambda meta: None)
    assert isinstance(error, GuardPassed) and passes_ == 1


def set_frozen(value):
    return lambda meta: meta.__setitem__("frozen", value)


def test_evaluate_py_refuses_a_golden_set_with_frozen_false(guard_run):
    error, passes_ = guard_run(set_frozen(False))
    assert isinstance(error, SystemExit) and isinstance(error.code, str) and "not frozen" in error.code
    assert passes_ == 0


def test_evaluate_py_refuses_a_golden_set_without_frozen(guard_run):
    error, passes_ = guard_run(lambda meta: meta.pop("frozen"))
    assert isinstance(error, SystemExit) and isinstance(error.code, str) and "not frozen" in error.code
    assert passes_ == 0


@pytest.mark.parametrize("value", ["false", 1], ids=["the string false", "the number 1"])
def test_pinned_evaluate_py_lets_a_non_boolean_frozen_through(guard_run, value):
    """Unwritten behaviour, not a deviation: the guard tests truthiness (meta.get("frozen")), so the string "false" or the number 1 passes."""
    error, passes_ = guard_run(set_frozen(value))
    assert isinstance(error, GuardPassed) and passes_ == 1


def test_pinned_evaluate_py_proceeds_when_frozen_on_is_missing(guard_run):
    """Unwritten behaviour, not a deviation: evaluate.py never reads frozen_on for its guard; a frozen set without it is scored (the report
    then says 'frozen on None')."""
    error, passes_ = guard_run(lambda meta: meta.pop("frozen_on"))
    assert isinstance(error, GuardPassed) and passes_ == 1
