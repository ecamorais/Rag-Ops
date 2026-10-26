"""Generate the dummy Operations Tickets dataset.

Reads the templates in ticket_scenarios.py and writes
  data/tickets.json   800 tickets
  data/tickets.csv    same tickets, comments flattened to a JSON string
  data/clusters.json  cluster membership, near-duplicate pairs, vague ticket ids

Fully reproducible: fixed random seed and a fixed "as of" date.
Run from anywhere:  python scripts/generate_tickets.py
"""

import csv
import itertools
import json
import random
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import ticket_scenarios as sc

SEED = 42
NOW = datetime(2026, 10, 25, 12, 0, 0)  # fixed "today" so reruns are identical
N_TICKETS = 800
PER_CATEGORY = 100
N_SINGLE_CLUSTERS = 25
N_NEAR_DUPS = 15
N_VAGUE = 20
ID_START = 1001

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

CATEGORIES = sc.CATEGORIES
PRIORITIES = ["P1-Critical", "P2-High", "P3-Medium", "P4-Low"]
STATUSES = ["Open", "In Progress", "Resolved", "Closed"]
TEAMS = [
    "Platform Ops", "Warehouse Ops", "Procurement", "Customer Ops",
    "IT Security", "Facilities", "Finance Ops",
]

# Overall priority mix the per-template profiles are tilted towards.
PRIORITY_TARGET = (5, 20, 45, 30)
TILT = 0.2 # 0 = use template profiles as-is, 1 = full pull towards the target
# Hours from creation to resolution, by priority index.
RESOLVE_HOURS = {0: (1, 24), 1: (4, 72), 2: (12, 240), 3: (24, 480)}
# Comment-count weights for 0..4 comments: [normal, P1].
COMMENT_WEIGHTS = ([25, 25, 25, 15, 10], [5, 15, 25, 30, 25])
TEAM_NOISE = 0.0

# Clusters starting per month, Nov 2025 .. Oct 2026 (30 in total, shuffled across clusters).
CLUSTER_START_PLAN = [
    ("2025-11", 2), ("2025-12", 2), ("2026-01", 2), ("2026-02", 2), ("2026-03", 2),
    ("2026-04", 3), ("2026-05", 3), ("2026-06", 3), ("2026-07", 3), ("2026-08", 3),
    ("2026-09", 3), ("2026-10", 2),
]
MIN_ROOM_DAYS = 4       # a cluster must start at least this long before NOW
FORCE_OPEN_DAYS = 60    # clusters starting this recently keep their latest ticket unresolved
MAX_REDRAWS = 30        # attempts to draw a description that is not already used

DUP_PREFIXES = [
    "We are seeing the same problem on our side.",
    "Reporting this separately as it is affecting our team too.",
    "Raising this in case it has not been logged yet.",
]
REPEAT_SENTENCE = "This looks like a repeat of an earlier incident."

SLOT_RE = re.compile(r"\{(\w+)\}")
SENTENCE_END_RE = re.compile(r"[.!?](?:\s|$)")

BY_ID = {s["id"]: s for s in sc.SCENARIOS}
BY_CATEGORY = {c: [s for s in sc.SCENARIOS if s["category"] == c] for c in CATEGORIES}
NON_LINKED = {c: [s for s in BY_CATEGORY[c] if s["link"] is None] for c in CATEGORIES}


# ------------------------------- slot filling -------------------------------
def slots_in(*texts):
    return {name for text in texts for name in SLOT_RE.findall(text)}


def template_texts(t):
    return t["titles"] + t["description"] + t["extra"] + [t["cause"], t["fix"]]


def draw_slot(rng, spec):
    if isinstance(spec, list):
        return rng.choice(spec)
    kind = spec[0]
    if kind == "range":
        return f"{rng.randint(spec[1], spec[2]):,}"
    if kind == "pattern":
        return spec[1].format(rng.randint(spec[2], spec[3]))
    raise ValueError(f"unknown slot kind {kind}")


def fixed_slots_for(rng, t, preset=None):
    """Values held constant across a cluster: the anchor slots plus every slot in
    the cause and fix, so all members share an identical root cause and fix."""
    values = dict(preset or {})
    for name in sorted(set(t["anchor"]) | slots_in(t["cause"], t["fix"])):
        if name not in values:
            values[name] = draw_slot(rng, sc.SLOTS[name])
    return values


def fill_slots(rng, t, fixed=None):
    values = dict(fixed or {})
    for name in sorted(slots_in(*template_texts(t))):
        if name not in values:
            values[name] = draw_slot(rng, sc.SLOTS[name])
    return values


# ------------------------------- random helpers -----------------------------
def sample_priority(rng, profile):
    weights = [p * t ** TILT for p, t in zip(sc.PRIORITY_PROFILES[profile], PRIORITY_TARGET)]
    return rng.choices(range(4), weights=weights)[0]


def shift_priority(rng, base):
    if rng.random() < 0.7:
        return base
    return min(3, max(0, base + rng.choice([-1, 1])))


def random_created(rng, lo, hi):
    """Random working-hours timestamp in [lo, hi]."""
    span = (hi - lo).total_seconds()
    while True:
        day = lo + timedelta(seconds=rng.uniform(0, span))
        ts = day.replace(hour=rng.randint(6, 20), minute=rng.randint(0, 59),
                         second=rng.randint(0, 59), microsecond=0)
        if lo <= ts <= hi:
            return ts


def plan_start_dates(rng):
    """One cluster start timestamp per CLUSTER_START_PLAN slot, in shuffled order."""
    starts = []
    for month, n in CLUSTER_START_PLAN:
        lo = datetime.strptime(month, "%Y-%m")
        nxt = lo.replace(year=lo.year + (lo.month == 12), month=lo.month % 12 + 1)
        hi = min(nxt - timedelta(seconds=1), NOW - timedelta(days=MIN_ROOM_DAYS))
        starts.extend(random_created(rng, lo, hi) for _ in range(n))
    rng.shuffle(starts)
    return starts


def mark_latest_open(members, start):
    """Recent clusters are still being worked, so their latest ticket stays unresolved."""
    if start >= NOW - timedelta(days=FORCE_OPEN_DAYS):
        members[-1]["force_open"] = True


def pick_team(rng, category):
    team = sc.CATEGORY_TEAM[category]
    if rng.random() < TEAM_NOISE:
        return rng.choice([t for t in TEAMS if t != team])
    return team


def build_people(rng):
    pairs = rng.sample(list(itertools.product(sc.FIRST_NAMES, sc.LAST_NAMES)), 80)
    roster = [f"{first} {last}" for first, last in pairs]
    team_assignees = {team: rng.sample(roster, 6) for team in TEAMS}
    return roster, team_assignees


# --------------------------------- drafts -----------------------------------
def make_draft(rng, t, fixed, created_at, prio, kind, seen, cluster=None, repeat=False):
    """Draw slots and wording until the description is not already in `seen`."""
    pool = t["extra"]
    for _ in range(MAX_REDRAWS):
        slots = fill_slots(rng, t, fixed)
        variant = rng.randrange(2)
        n_extra = rng.randint(1, 2) if len(pool) >= 3 else rng.randint(0, len(pool))
        sentences = [s.format_map(slots) for s in t["description"] + rng.sample(pool, n_extra)]
        if repeat:
            sentences.append(REPEAT_SENTENCE)
        if " ".join(sentences) not in seen:
            break
    else:
        raise RuntimeError(f"no unique description for {t['id']} after {MAX_REDRAWS} draws")
    seen.add(" ".join(sentences))
    return {
        "kind": kind,
        "cluster": cluster,
        "dup_of": None,
        "category": t["category"],
        "team": pick_team(rng, t["category"]),
        "scenario_id": t["id"],
        "title": t["titles"][variant].format_map(slots),
        "title_variant": variant,
        "titles": [x.format_map(slots) for x in t["titles"]],
        "sentences": sentences,
        "priority": prio,
        "created_at": created_at,
        "resolution": "Root cause: {} {}".format(
            t["cause"].format_map(slots), t["fix"].format_map(slots)),
    }


def make_vague_draft(rng, entry, created_at):
    title, description, category = entry
    return {
        "kind": "vague", "cluster": None, "dup_of": None,
        "category": category,
        "team": pick_team(rng, category),
        "scenario_id": None,
        "title": title, "title_variant": 0, "titles": [title],
        "sentences": [description],
        "priority": sample_priority(rng, "low"),
        "created_at": created_at,
        "resolution": rng.choice(sc.VAGUE_RESOLUTIONS),
    }


def make_near_duplicate(rng, orig):
    dup = dict(orig)
    other = 1 - orig["title_variant"] if len(orig["titles"]) == 2 else 0
    sentences = list(orig["sentences"])
    shuffled = rng.sample(sentences, len(sentences))
    if shuffled == sentences and len(sentences) > 1:
        shuffled = sentences[1:] + sentences[:1]
    hi = min(orig["created_at"] + timedelta(hours=48), NOW - timedelta(hours=1))
    gap = (hi - orig["created_at"]).total_seconds() * rng.uniform(0.1, 1.0)
    dup.update(
        kind="near_duplicate", cluster=None, dup_of=orig,
        title=orig["titles"][other], title_variant=other,
        sentences=[rng.choice(DUP_PREFIXES)] + shuffled,
        created_at=(orig["created_at"] + timedelta(seconds=gap)).replace(microsecond=0),
    )
    return dup


# ------------------------------ ticket assembly ------------------------------
def status_weights(age_days):
    # Open, In Progress, Resolved, Closed
    if age_days > 30:
        return (5, 3, 52, 40)
    if age_days > 7:
        return (15, 20, 40, 25)
    return (40, 35, 20, 5)


def pick_text(rng, pool, used):
    options = [x for x in pool if x not in used] or pool
    text = rng.choice(options)
    used.add(text)
    return text


def make_comments(rng, created, resolved_at, prio, assignee, reporter):
    end = resolved_at or NOW
    start = created + timedelta(minutes=5)
    window = (end - start).total_seconds()
    k = rng.choices(range(5), weights=COMMENT_WEIGHTS[1 if prio == 0 else 0])[0]
    if window < 600:
        k = 0
    offsets = sorted(rng.uniform(0, window) for _ in range(k))
    used, comments = set(), []
    for i, off in enumerate(offsets):
        if resolved_at and i == k - 1:
            pool, author = "resolved", assignee
        elif i == 0:
            pool, author = "triage", assignee
        else:
            pool = rng.choice(["progress", "progress", "requester"])
            author = reporter if pool == "requester" else assignee
        comments.append({
            "author": author,
            "timestamp": (start + timedelta(seconds=off)).replace(microsecond=0).isoformat(),
            "text": pick_text(rng, sc.COMMENT_POOLS[pool], used),
        })
    return comments


def finalize(rng, d, roster, team_assignees, assignee=None, exclude_reporter=()):
    assignee = assignee or rng.choice(team_assignees[d["team"]])
    reporter = rng.choice(
        [p for p in roster if p != assignee and p not in exclude_reporter])
    created = d["created_at"]
    age_h = (NOW - created).total_seconds() / 3600
    if d.get("force_open"):
        status = rng.choice(STATUSES[:2])
    else:
        status = rng.choices(STATUSES, weights=status_weights(age_h / 24))[0]
    resolved_at, notes = None, None
    if status in ("Resolved", "Closed"):
        lo, hi = RESOLVE_HOURS[d["priority"]]
        lag = rng.uniform(lo, hi)
        if lag > age_h - 0.5:
            lag = max(0.25, age_h * rng.uniform(0.3, 0.9))
        resolved_at = (created + timedelta(hours=lag)).replace(microsecond=0)
        notes = d["resolution"]
    return {
        "title": d["title"],
        "description": " ".join(d["sentences"]),
        "category": d["category"],
        "priority": PRIORITIES[d["priority"]],
        "status": status,
        "team": d["team"],
        "reporter": reporter,
        "assignee": assignee,
        "created_at": created.isoformat(),
        "resolved_at": resolved_at.isoformat() if resolved_at else None,
        "resolution_notes": notes,
        "comments": make_comments(rng, created, resolved_at, d["priority"], assignee, reporter),
    }


# ------------------------------- cluster building ----------------------------
def build_single_clusters(rng, drafts, clusters, used, seen, starts):
    cats = CATEGORIES * 3 + [rng.choice(CATEGORIES)]
    rng.shuffle(cats)
    counts = Counter(cats)
    chosen = {c: rng.sample(NON_LINKED[c], counts[c]) for c in CATEGORIES}
    for cat in cats:
        t = chosen[cat].pop()
        cid = f"CL-{len(clusters) + 1:02d}"
        size = rng.choices([3, 4, 5, 6], weights=[30, 30, 25, 15])[0]
        fixed = fixed_slots_for(rng, t)
        base_prio = sample_priority(rng, t["priority"])
        base = next(starts)
        span = min(45.0, 0.9 * (NOW - base).total_seconds() / 86400)
        offsets = [0.0] + sorted(rng.uniform(0.2, span) for _ in range(size - 1))
        members = []
        for i, off in enumerate(offsets):
            created = min(base + timedelta(days=off), NOW - timedelta(hours=2))
            d = make_draft(rng, t, fixed, created.replace(microsecond=0),
                           shift_priority(rng, base_prio), "cluster", seen, cid,
                           repeat=i >= 1 and rng.random() < 0.5)
            members.append(d)
        mark_latest_open(members, base)
        drafts.extend(members)
        used[cat] += size
        clusters.append({
            "cluster_id": cid, "type": "single_category", "categories": [cat],
            "scenario_ids": [t["id"]],
            "root_cause": t["cause"].format_map(fixed),
            "members": members,
        })


def build_cross_clusters(rng, drafts, clusters, used, seen, starts):
    for key, link in sc.CROSS_CATEGORY_LINKS.items():
        ta, tb = (BY_ID[i] for i in link["scenarios"])
        cid = f"CL-{len(clusters) + 1:02d}"
        shared = {n: draw_slot(rng, sc.SLOTS[n]) for n in link["shared_slots"]}
        fixed = {ta["id"]: fixed_slots_for(rng, ta, shared),
                 tb["id"]: fixed_slots_for(rng, tb, shared)}
        n = rng.randint(4, 6)
        a = rng.randint(2, n - 2)
        seq = [ta] * a + [tb] * (n - a)
        rng.shuffle(seq)
        if seq[0] is not ta:
            first = seq.index(ta)
            seq[0], seq[first] = seq[first], seq[0]
        base_prio = sample_priority(rng, ta["priority"])
        base = next(starts)
        offsets = [0.0] + sorted(rng.uniform(0.2, 30) for _ in range(n - 1))
        members = []
        for i, (t, off) in enumerate(zip(seq, offsets)):
            created = min(base + timedelta(hours=off), NOW - timedelta(hours=2))
            d = make_draft(rng, t, fixed[t["id"]], created.replace(microsecond=0),
                           shift_priority(rng, base_prio), "cluster", seen, cid)
            members.append(d)
            used[t["category"]] += 1
        mark_latest_open(members, base)
        drafts.extend(members)
        clusters.append({
            "cluster_id": cid, "type": "cross_category", "event": link["name"],
            "categories": [ta["category"], tb["category"]],
            "scenario_ids": [ta["id"], tb["id"]],
            "root_cause": ta["cause"].format_map(fixed[ta["id"]]),
            "root_causes_by_category": {
                ta["category"]: ta["cause"].format_map(fixed[ta["id"]]),
                tb["category"]: tb["cause"].format_map(fixed[tb["id"]]),
            },
            "members": members,
        })


# ----------------------------------- checks ----------------------------------
def n_sentences(text):
    return len(SENTENCE_END_RE.findall(text))


def check(records, clusters, dup_pairs, vague_ids):
    tickets = [r["ticket"] for r in records]
    assert len(tickets) == N_TICKETS, len(tickets)
    ids = [r["id"] for r in records]
    assert ids == [f"OPS-{ID_START + i}" for i in range(N_TICKETS)], "ids not contiguous"
    cat_counts = Counter(t["category"] for t in tickets)
    assert all(cat_counts[c] == PER_CATEGORY for c in CATEGORIES), cat_counts
    lo = NOW - timedelta(days=366)
    for r in records:
        t = r["ticket"]
        assert t["priority"] in PRIORITIES and t["status"] in STATUSES and t["team"] in TEAMS
        created = datetime.fromisoformat(t["created_at"])
        assert lo <= created <= NOW, t["created_at"]
        resolved = t["status"] in ("Resolved", "Closed")
        assert (t["resolved_at"] is not None) == resolved
        assert (t["resolution_notes"] is not None) == resolved
        end = NOW
        if resolved:
            end = datetime.fromisoformat(t["resolved_at"])
            assert end > created and end <= NOW
            assert 1 <= n_sentences(t["resolution_notes"]) <= 4, t["resolution_notes"]
        assert t["reporter"] != t["assignee"]
        assert len(t["comments"]) <= 4
        stamps = [datetime.fromisoformat(c["timestamp"]) for c in t["comments"]]
        assert stamps == sorted(stamps) and all(created < s <= end for s in stamps)
        if r["kind"] != "vague":
            assert 2 <= n_sentences(t["description"]) <= 5, t["description"]
        for field in (t["title"], t["description"], t["resolution_notes"] or ""):
            assert "{" not in field and "}" not in field, field
    category_of = {r["id"]: r["ticket"]["category"] for r in records}
    for c in clusters:
        assert 3 <= len(c["ticket_ids"]) <= 6, c["cluster_id"]
        assert {category_of[i] for i in c["ticket_ids"]} == set(c["categories"]), c["cluster_id"]
    created_of = {r["id"]: r["ticket"]["created_at"] for r in records}
    start_months = Counter(min(created_of[i] for i in c["ticket_ids"])[:7] for c in clusters)
    wanted = {"2025-11", "2025-12"} | {f"2026-{m:02d}" for m in range(1, 11)}
    assert wanted <= set(start_months), f"no cluster starts in {sorted(wanted - set(start_months))}"
    assert sum(start_months[m] for m in ("2026-08", "2026-09", "2026-10")) >= 3
    recent = (NOW - timedelta(days=FORCE_OPEN_DAYS)).isoformat()
    open_recent = [r for r in records if r["cluster"] and r["ticket"]["created_at"] >= recent
                   and r["ticket"]["status"] in ("Open", "In Progress")]
    assert len(open_recent) >= 3, len(open_recent)
    designed = {i for p in dup_pairs for i in (p["original_id"], p["duplicate_id"])}
    desc_counts = Counter(r["ticket"]["description"] for r in records if r["id"] not in designed)
    n_dup = sum(n for n in desc_counts.values() if n > 1)
    assert n_dup / (N_TICKETS - len(designed)) < 0.03, f"{n_dup} tickets share a description"
    assert max(desc_counts.values()) <= 2, "group of identical descriptions larger than 2"
    assert len(clusters) == N_SINGLE_CLUSTERS + len(sc.CROSS_CATEGORY_LINKS)
    assert len(dup_pairs) == N_NEAR_DUPS and len(vague_ids) == N_VAGUE


# ------------------------------------ main -----------------------------------
def main():
    rng = random.Random(SEED)
    roster, team_assignees = build_people(rng)
    drafts, clusters = [], []
    used = Counter()
    seen = set()
    starts = iter(plan_start_dates(rng))

    build_single_clusters(rng, drafts, clusters, used, seen, starts)
    build_cross_clusters(rng, drafts, clusters, used, seen, starts)

    for entry in rng.sample(sc.VAGUE_TICKETS, N_VAGUE):
        drafts.append(make_vague_draft(
            rng, entry, random_created(rng, NOW - timedelta(days=365), NOW - timedelta(hours=2))))
        used[entry[2]] += 1

    remaining = {c: PER_CATEGORY - used[c] for c in CATEGORIES}
    dup_counts = Counter()
    while sum(dup_counts.values()) < N_NEAR_DUPS:
        c = rng.choice(CATEGORIES)
        if 2 * (dup_counts[c] + 1) <= remaining[c]:
            dup_counts[c] += 1

    for cat in CATEGORIES:
        n_normal = remaining[cat] - dup_counts[cat]
        pool = []
        while len(pool) < n_normal:
            batch = list(NON_LINKED[cat])
            rng.shuffle(batch)
            pool.extend(batch)
        normals = []
        for t in pool[:n_normal]:
            normals.append(make_draft(
                rng, t, None,
                random_created(rng, NOW - timedelta(days=365), NOW - timedelta(hours=2)),
                sample_priority(rng, t["priority"]), "normal", seen))
        drafts.extend(normals)
        for orig in rng.sample(normals, dup_counts[cat]):
            drafts.append(make_near_duplicate(rng, orig))

    # Finalise: originals first so near-duplicates can reuse the assignee.
    records = []
    for d in drafts:
        if d["dup_of"] is None:
            d["ticket"] = finalize(rng, d, roster, team_assignees)
    for d in drafts:
        if d["dup_of"] is not None:
            orig = d["dup_of"]["ticket"]
            d["ticket"] = finalize(rng, d, roster, team_assignees,
                                   assignee=orig["assignee"],
                                   exclude_reporter={orig["reporter"]})
    drafts.sort(key=lambda d: d["ticket"]["created_at"])
    for i, d in enumerate(drafts):
        d["id"] = f"OPS-{ID_START + i}"
        records.append(d)

    cluster_out = []
    for c in clusters:
        entry = {k: v for k, v in c.items() if k != "members"}
        entry["ticket_ids"] = sorted(m["id"] for m in c["members"])
        cluster_out.append(entry)
    dup_pairs = [{"original_id": d["dup_of"]["id"], "duplicate_id": d["id"]}
                 for d in records if d["dup_of"] is not None]
    vague_ids = [d["id"] for d in records if d["kind"] == "vague"]

    check(records, cluster_out, dup_pairs, vague_ids)

    tickets = [{"ticket_id": d["id"], **d["ticket"]} for d in records]
    DATA_DIR.mkdir(exist_ok=True)
    with open(DATA_DIR / "tickets.json", "w", encoding="utf-8") as f:
        json.dump(tickets, f, indent=2, ensure_ascii=False)
    columns = list(tickets[0].keys())
    with open(DATA_DIR / "tickets.csv", "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for t in tickets:
            row = dict(t)
            row["comments"] = json.dumps(t["comments"], ensure_ascii=False)
            writer.writerow(row)
    with open(DATA_DIR / "clusters.json", "w", encoding="utf-8") as f:
        json.dump({
            "seed": SEED,
            "as_of": NOW.isoformat(),
            "clusters": cluster_out,
            "near_duplicates": dup_pairs,
            "vague_ticket_ids": vague_ids,
        }, f, indent=2, ensure_ascii=False)

    print_summary(tickets, cluster_out, dup_pairs, vague_ids)


def print_summary(tickets, clusters, dup_pairs, vague_ids):
    print(f"tickets: {len(tickets)}  ({tickets[0]['created_at'][:10]} .. {tickets[-1]['created_at'][:10]})")
    for label, key, order in (("category", "category", CATEGORIES),
                              ("priority", "priority", PRIORITIES),
                              ("status", "status", STATUSES),
                              ("team", "team", TEAMS)):
        counts = Counter(t[key] for t in tickets)
        print(f"\nby {label}:")
        for v in order:
            print(f"  {v:<22}{counts[v]:>5}  {counts[v] / len(tickets):6.1%}")
    sizes = Counter(len(c["ticket_ids"]) for c in clusters)
    n_cross = sum(c["type"] == "cross_category" for c in clusters)
    print(f"\nclusters: {len(clusters)} ({len(clusters) - n_cross} single-category, "
          f"{n_cross} cross-category), sizes {dict(sorted(sizes.items()))}, "
          f"tickets in clusters: {sum(len(c['ticket_ids']) for c in clusters)}")
    print(f"near-duplicate pairs: {len(dup_pairs)}   vague tickets: {len(vague_ids)}")


if __name__ == "__main__":
    main()
