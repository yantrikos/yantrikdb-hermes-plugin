"""Deterministic generator for the 1000-memory comparison corpus.

Produces ``corpus_1k.json`` (1000 facts) + ``queries_1k.json`` (20 queries
with planted target fact-ids for precision@K). Run once; output is
checked into the repo so the comparison is reproducible across re-runs
and across machines.

Corpus shape (1000 facts total):
  600 "real" facts modelling agent memory:
    200 user preferences
    200 project facts
    200 person / relationship triples
  300 noise facts (general statements that shouldn't match the queries)
  50  planted duplicates:
    25 exact duplicates of real facts
    25 near-paraphrase duplicates
  50  planted contradictions:
    25 "user prefers A" baseline + 25 "user prefers B" contradiction

Each fact has: id, text, category, planted_kind (None|exact_dup|paraphrase|contradiction), source_id (when applicable).

Queries (20 total):
  10 query the real facts (precision@K against a known target_fact_id)
  5  query a duplicated fact (test whether duplicates skew rank)
  5  query a contradicting fact (test how providers handle conflicting context)

Everything is seeded so output is byte-identical across runs.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

SEED = 20260512
OUT_DIR = Path(__file__).parent

# ---------------------------------------------------------------------------
# Source vocabulary — small enough to grep, large enough for 1000 unique-ish
# combinations.

# User preferences: 200 facts.
PREF_SUBJECTS = [
    "VS Code", "Vim", "tmux", "fish shell", "zsh", "bash", "Neovim", "Emacs",
    "Sublime Text", "PyCharm", "IntelliJ", "Cursor", "Windsurf", "Zed",
    "iTerm2", "kitty", "alacritty", "Warp", "Hyper", "Terminal.app",
]
PREF_CHOICES = [
    ("color scheme", ["dark mode", "light mode", "high-contrast", "solarized dark", "gruvbox", "tokyo night", "catppuccin", "nord"]),
    ("font", ["JetBrains Mono", "Fira Code", "Cascadia Code", "Iosevka", "Hack", "Source Code Pro"]),
    ("font size", ["12pt", "13pt", "14pt", "15pt", "16pt"]),
    ("tab width", ["2 spaces", "4 spaces", "tabs"]),
    ("line endings", ["LF", "CRLF"]),
    ("auto-format", ["on save", "manually only", "never"]),
    ("git GUI", ["command line only", "Fork", "GitKraken", "lazygit", "tig"]),
]

# Project facts: 200 facts.
PROJECT_NAMES = [
    "atlas", "beacon", "compass", "delta", "echo", "fjord", "glacier",
    "harbor", "iris", "junction", "kestrel", "lattice", "monolith",
    "nexus", "orbit", "prism", "quartz", "ridge", "summit", "tundra",
]
PROJECT_FACTS = [
    ("uses {tech} for {purpose}", [
        ("PostgreSQL", "primary storage"), ("Redis", "caching"),
        ("Kafka", "event streaming"), ("ClickHouse", "analytics"),
        ("Elasticsearch", "search"), ("S3", "object storage"),
        ("Kubernetes", "orchestration"), ("Docker", "containerization"),
        ("Prometheus", "metrics"), ("Grafana", "dashboards"),
        ("OpenTelemetry", "tracing"), ("Sentry", "error tracking"),
        ("FastAPI", "the API layer"), ("React", "the frontend"),
        ("Next.js", "server-side rendering"), ("Rust", "performance-critical paths"),
        ("Go", "the orchestrator"), ("Python", "data pipelines"),
        ("TypeScript", "the client"), ("Tailwind", "styling"),
    ]),
    ("launches in {month} {year}", [
        (m, y) for m in ("March", "June", "September", "November") for y in ("2026", "2027")
    ]),
    ("supports {locales} locales", [(n,) for n in ("3", "5", "12", "27", "42")]),
    ("targets {users} monthly active users", [(n,) for n in ("1,000", "10,000", "100,000", "1,000,000")]),
]

# Person/relationship facts: 200 facts.
PERSON_NAMES = [
    "Alice", "Bob", "Carol", "Dave", "Eve", "Frank", "Grace", "Henry",
    "Ivy", "Jack", "Kate", "Liam", "Mia", "Noah", "Olivia", "Pat",
    "Quinn", "Riley", "Sam", "Taylor",
]
PERSON_FACTS = [
    "{person} is the {role} on the {project} team.",
    "{person} reports to {person2}.",
    "{person} prefers async over standups.",
    "{person} owns the {area} surface.",
    "{person} is on PTO from {start} to {end}.",
]
ROLES = ["tech lead", "engineering manager", "principal engineer", "staff engineer", "senior engineer", "PM", "designer", "QA lead"]
AREAS = ["auth", "billing", "search", "ingest", "API", "dashboard", "infra", "deploy"]

# Noise facts: 300 distractors that shouldn't keyword/semantic-match the queries.
NOISE_TEMPLATES = [
    "The {animal} crossed the {place} at {time}.",
    "{name1} and {name2} discussed the {topic} report on {day}.",
    "The {color} {object} sat on the {furniture} all morning.",
    "It rained for {duration} hours in {city} yesterday.",
    "{fruit} is the most-shipped fruit at the {market}.",
]
ANIMALS = ["fox", "raccoon", "heron", "otter", "lynx", "owl", "badger", "egret"]
PLACES = ["meadow", "ridge", "river", "trail", "fence", "highway", "bridge", "thicket"]
TIMES = ["dawn", "noon", "dusk", "midnight"]
NOISE_NAMES = ["Aria", "Beck", "Cale", "Drew", "Ezra", "Finn", "Gus", "Hana"]
TOPICS = ["quarterly", "annual", "audit", "compliance", "diversity", "inventory"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
COLORS_NOISE = ["amber", "crimson", "teal", "ochre", "indigo"]
OBJECTS = ["lamp", "kettle", "vase", "atlas", "compass", "globe"]
FURNITURE = ["desk", "shelf", "bench", "mantel", "windowsill"]
DURATIONS = ["two", "three", "four", "five", "six"]
CITIES = ["Portland", "Aarhus", "Lyon", "Kraków", "Hobart"]
FRUITS = ["fig", "guava", "lychee", "kumquat", "persimmon"]
MARKETS = ["downtown", "Wednesday", "harbor", "westside", "co-op"]


def _build_real(rng: random.Random) -> list[dict]:
    """600 'real' facts the queries will probe against."""
    out: list[dict] = []

    # Preferences — exactly 200
    for _ in range(200):
        subj = rng.choice(PREF_SUBJECTS)
        cat, choices = rng.choice(PREF_CHOICES)
        choice = rng.choice(choices)
        text = f"The user prefers {choice} as their {cat} in {subj}."
        out.append({"text": text, "category": "preference",
                    "subject": subj, "key": cat, "value": choice})

    # Projects — exactly 200
    while len([f for f in out if f["category"] == "project"]) < 200:
        proj = rng.choice(PROJECT_NAMES)
        tmpl, params_list = rng.choice(PROJECT_FACTS)
        params = rng.choice(params_list)
        if len(params) == 2:
            text = f"Project {proj} {tmpl.format(tech=params[0], purpose=params[1], month=params[0], year=params[1])}."
        elif len(params) == 1:
            text = f"Project {proj} {tmpl.format(locales=params[0], users=params[0])}."
        out.append({"text": text, "category": "project", "project": proj})

    # People — exactly 200
    while len([f for f in out if f["category"] == "person"]) < 200:
        person = rng.choice(PERSON_NAMES)
        tmpl = rng.choice(PERSON_FACTS)
        if "{role}" in tmpl:
            text = tmpl.format(person=person, role=rng.choice(ROLES), project=rng.choice(PROJECT_NAMES))
        elif "{person2}" in tmpl:
            other = rng.choice([p for p in PERSON_NAMES if p != person])
            text = tmpl.format(person=person, person2=other)
        elif "{area}" in tmpl:
            text = tmpl.format(person=person, area=rng.choice(AREAS))
        elif "{start}" in tmpl:
            text = tmpl.format(person=person, start=rng.choice(DAYS), end=rng.choice(DAYS))
        else:
            text = tmpl.format(person=person)
        out.append({"text": text, "category": "person", "person": person})

    return out[:600]


def _build_noise(rng: random.Random) -> list[dict]:
    """300 noise facts."""
    out: list[dict] = []
    for _ in range(300):
        t = rng.choice(NOISE_TEMPLATES)
        text = t.format(
            animal=rng.choice(ANIMALS),
            place=rng.choice(PLACES),
            time=rng.choice(TIMES),
            name1=rng.choice(NOISE_NAMES),
            name2=rng.choice(NOISE_NAMES),
            topic=rng.choice(TOPICS),
            day=rng.choice(DAYS),
            color=rng.choice(COLORS_NOISE),
            object=rng.choice(OBJECTS),
            furniture=rng.choice(FURNITURE),
            duration=rng.choice(DURATIONS),
            city=rng.choice(CITIES),
            fruit=rng.choice(FRUITS),
            market=rng.choice(MARKETS),
        )
        out.append({"text": text, "category": "noise"})
    return out


def _build_duplicates(real_facts: list[dict], rng: random.Random) -> list[dict]:
    """50 planted dups: 25 exact + 25 near-paraphrase."""
    out: list[dict] = []

    # 25 exact dups — pick 25 random real preferences/projects and repeat verbatim.
    candidates = [f for f in real_facts if f["category"] in ("preference", "project")]
    for src in rng.sample(candidates, 25):
        out.append({
            "text": src["text"], "category": "duplicate_exact",
            "planted_kind": "exact_dup", "source_text": src["text"],
        })

    # 25 near-paraphrase dups — small rewrites.
    for src in rng.sample(candidates, 25):
        original = src["text"]
        # Tiny rewrite rules
        paraphrased = original
        for find, repl in [
            ("The user prefers ", "The user's preferred "),
            (" as their ", " is "),
            ("Project ", "The "),
            ("launches in ", "ships in "),
            ("uses ", "is built with "),
        ]:
            if find in paraphrased:
                paraphrased = paraphrased.replace(find, repl, 1)
                break
        else:
            paraphrased = "Note: " + original
        out.append({
            "text": paraphrased, "category": "duplicate_paraphrase",
            "planted_kind": "paraphrase", "source_text": original,
        })
    return out


def _build_contradictions(real_facts: list[dict], rng: random.Random) -> list[dict]:
    """50 planted contradictions — 25 baselines + 25 contradicting them later in the stream."""
    out: list[dict] = []
    prefs = [f for f in real_facts if f["category"] == "preference"]
    picked = rng.sample(prefs, 25)
    for src in picked:
        # Baseline copy
        out.append({
            "text": src["text"], "category": "contradiction_baseline",
            "planted_kind": "contradiction_base", "source_text": src["text"],
        })
        # Contradicting version — flip the value to a different choice in same category
        cat = src["key"]
        choices = next(c for c_name, c in PREF_CHOICES if c_name == cat)
        alt = next((c for c in choices if c != src["value"]), None)
        if alt is None:
            alt = "something different"
        contradictory = src["text"].replace(src["value"], alt)
        out.append({
            "text": contradictory, "category": "contradiction_alt",
            "planted_kind": "contradiction_alt", "source_text": src["text"],
        })
    return out


def _build_queries(real_facts: list[dict], dup_facts: list[dict], contra_facts: list[dict], rng: random.Random) -> list[dict]:
    """20 queries with planted target fact-ids for precision@K."""
    queries: list[dict] = []

    # 10 queries hit a real fact directly.
    real_targets = rng.sample(real_facts, 10)
    for q_idx, target in enumerate(real_targets, start=1):
        cat = target["category"]
        if cat == "preference":
            query = f"What {target['key']} does the user prefer in {target['subject']}?"
        elif cat == "project":
            query = f"What technology does {target.get('project', 'the project')} use?"
        elif cat == "person":
            query = f"What is {target.get('person', 'the person')}'s role?"
        else:
            query = target["text"][:40]
        queries.append({
            "id": f"Q-real-{q_idx:02d}", "query": query,
            "target_text": target["text"], "target_kind": "real_fact",
            "top_k": 5,
        })

    # 5 queries should surface a duplicated fact.
    for q_idx, dup in enumerate(rng.sample(dup_facts[:25], 5), start=1):  # exact-dups only
        query = f"What does the user prefer about {dup['source_text'].split(' as their ')[0].replace('The user prefers ', '')[:30]}?"
        queries.append({
            "id": f"Q-dup-{q_idx:02d}", "query": query,
            "target_text": dup["source_text"], "target_kind": "duplicate",
            "top_k": 5,
        })

    # 5 queries hit a contradicting fact.
    bases = [f for f in contra_facts if f["category"] == "contradiction_baseline"]
    for q_idx, base in enumerate(rng.sample(bases, 5), start=1):
        # Use a deliberately ambiguous query that should surface BOTH the
        # baseline and the contradicting one.
        try:
            subject_part = base["source_text"].split(" in ")[-1].rstrip(".")
        except Exception:
            subject_part = "the user's environment"
        query = f"What is the user's preference for {subject_part}?"
        queries.append({
            "id": f"Q-contra-{q_idx:02d}", "query": query,
            "target_text": base["source_text"], "target_kind": "contradiction",
            "top_k": 5,
        })

    return queries


def main() -> None:
    rng = random.Random(SEED)
    real = _build_real(rng)
    noise = _build_noise(rng)
    dups = _build_duplicates(real, rng)
    contras = _build_contradictions(real, rng)

    # Interleave so contradictions come AFTER their baselines in stream order.
    baselines = [f for f in contras if f["category"] == "contradiction_baseline"]
    alts = [f for f in contras if f["category"] == "contradiction_alt"]
    facts: list[dict] = real + noise + dups + baselines + alts
    assert len(facts) == 1000, f"corpus length {len(facts)} != 1000"

    # Assign sequential ids and clean shape
    out_facts = []
    for i, f in enumerate(facts, start=1):
        out_facts.append({
            "id": i, "text": f["text"], "category": f["category"],
            "planted_kind": f.get("planted_kind"),
            "source_text": f.get("source_text"),
        })

    (OUT_DIR / "corpus_1k.json").write_text(
        json.dumps({"_meta": {"seed": SEED, "count": len(out_facts)},
                    "facts": out_facts}, indent=2), encoding="utf-8",
    )

    queries = _build_queries(real, dups, contras, rng)
    (OUT_DIR / "queries_1k.json").write_text(
        json.dumps({"_meta": {"seed": SEED, "count": len(queries)},
                    "queries": queries}, indent=2), encoding="utf-8",
    )

    print(f"wrote {OUT_DIR / 'corpus_1k.json'} ({len(out_facts)} facts)")
    print(f"wrote {OUT_DIR / 'queries_1k.json'} ({len(queries)} queries)")
    counts = {}
    for f in out_facts:
        counts[f["category"]] = counts.get(f["category"], 0) + 1
    for k, v in sorted(counts.items()):
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
