"""Notes-as-memory baseline — the honest version of "why not just use Obsidian?"

Obsidian is not a Hermes memory provider and this does not pretend it is. But
it is what a large share of the community actually does: the agent writes
durable facts into a markdown vault and searches it back. Ten community
stories describe exactly that, and Hermes issue #2736 asks for it as a
first-class memory layer. So "why do I need a memory substrate when I already
have my notes?" is a fair question, and it deserves a measured answer rather
than a rhetorical one.

This runs a markdown vault over the SAME 1,000-fact corpus and the SAME 20
queries the provider harness uses, scoring it the same way. Two retrieval
modes, because arguing against only the weakest one would be a strawman:

  keyword  — ranked term overlap, i.e. what vault search / ripgrep gives you
  semantic — the same bundled embedder this plugin uses, over note bodies,
             i.e. an Obsidian user who has wired up a semantic-search plugin

The corpus carries 25 exact duplicates, 25 paraphrases and 25
contradiction pairs precisely so the interesting columns are not recall@k —
where a good vault does fine — but what happens to duplicates and
contradictions, which a filesystem has no concept of.

Run:
    python tests/comparison/notes_baseline.py            # both modes
    python tests/comparison/notes_baseline.py --mode keyword
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
_WORD = re.compile(r"[a-z0-9]+")
_STOP = {
    "the", "a", "an", "is", "are", "was", "were", "to", "of", "in", "on", "for",
    "and", "or", "with", "at", "by", "from", "as", "that", "this", "it", "its",
    "what", "which", "who", "does", "do", "did", "user", "their", "they",
}


def _tokens(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 2]


# ---------------------------------------------------------------------------
# The vault
# ---------------------------------------------------------------------------

class NotesVault:
    """A markdown vault, written and searched the way an agent would.

    One note per fact, because that is what "write it to Obsidian" produces.
    No deduplication and no contradiction handling — not as a handicap, but
    because a directory of files genuinely has no such notion. That absence
    is the finding, not a flaw in the setup.
    """

    def __init__(self, root: Path, mode: str = "keyword"):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self._notes: list[tuple[Path, str]] = []
        self._vectors: list = []
        self._embedder = None
        if mode == "semantic":
            self._embedder = _load_embedder()

    def write(self, idx: int, text: str) -> None:
        slug = re.sub(r"[^a-z0-9]+", "-", text.lower())[:60].strip("-") or f"note-{idx}"
        path = self.root / f"{idx:04d}-{slug}.md"
        path.write_text(f"# {text}\n\ncaptured: fact {idx}\n", encoding="utf-8")
        self._notes.append((path, text))
        if self._embedder is not None:
            self._vectors.append(self._embedder(text))

    def search(self, query: str, top_k: int = 5) -> list[str]:
        if self.mode == "semantic" and self._embedder is not None:
            qv = self._embedder(query)
            scored = [(_cosine(qv, v), t) for v, (_, t) in zip(self._vectors, self._notes)]
        else:
            q = Counter(_tokens(query))
            if not q:
                return []
            scored = []
            for _, text in self._notes:
                t = Counter(_tokens(text))
                overlap = sum(min(c, t[w]) for w, c in q.items())
                if overlap:
                    # length-normalised overlap: the same shape a vault's
                    # ranked search gives, not raw hit counting
                    scored.append((overlap / math.sqrt(len(t) + 1), text))
        scored.sort(key=lambda x: -x[0])
        return [t for _, t in scored[:top_k]]

    # A vault cannot answer these. Recorded rather than skipped, because the
    # absence is the substantive difference.
    def duplicates_of(self, text: str) -> int:
        return sum(1 for _, t in self._notes if t.strip() == text.strip())

    def surfaces_contradictions(self) -> bool:
        return False


def _cosine(a, b) -> float:
    num = sum(x * y for x, y in zip(a, b))
    da = math.sqrt(sum(x * x for x in a)) or 1.0
    db = math.sqrt(sum(y * y for y in b)) or 1.0
    return num / (da * db)


def _load_embedder():
    """The same bundled embedder the plugin uses, so semantic mode is a fair
    fight rather than a weaker model losing to a stronger one."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "benchmarks"))
    from _bootstrap import _pin_engine_import
    _pin_engine_import()
    from model2vec import StaticModel  # noqa: PLC0415
    model = StaticModel.from_pretrained("minishlab/potion-base-2M")
    return lambda text: list(model.encode([text])[0])


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def run(mode: str) -> dict:
    corpus = json.loads((FIXTURES / "corpus_1k.json").read_text(encoding="utf-8"))
    queries = json.loads((FIXTURES / "queries_1k.json").read_text(encoding="utf-8"))["queries"]
    facts = corpus["facts"]

    root = Path(tempfile.mkdtemp()) / "vault"
    vault = NotesVault(root, mode=mode)

    writes = []
    for f in facts:
        t0 = time.perf_counter()
        vault.write(f["id"], f["text"])
        writes.append((time.perf_counter() - t0) * 1000)

    hits, reads = 0, []
    for q in queries:
        t0 = time.perf_counter()
        got = vault.search(q["query"], top_k=q.get("top_k", 5))
        reads.append((time.perf_counter() - t0) * 1000)
        target = (q.get("target_text") or "").strip()
        if target and any(target in g or g in target for g in got):
            hits += 1

    dup_examples = [f for f in facts if f.get("planted_kind") == "exact_dup"][:5]
    dup_copies = [vault.duplicates_of(f["text"]) for f in dup_examples]

    return {
        "provider": f"notes-vault ({mode})",
        "notes_written": len(vault._notes),
        "vault_files": len(list(root.glob("*.md"))),
        "write_p50_ms": round(statistics.median(writes), 3),
        "recall_p50_ms": round(statistics.median(reads), 2),
        "recall_p99_ms": round(sorted(reads)[int(len(reads) * 0.99) - 1], 2),
        "precision_at_k": round(hits / len(queries), 3),
        "queries": len(queries),
        "duplicate_copies_kept": max(dup_copies) if dup_copies else None,
        "canonicalises_duplicates": bool(dup_copies and max(dup_copies) <= 1),
        "surfaces_contradictions": vault.surfaces_contradictions(),
        "explains_why_retrieved": False,
        "tracks_supersession": False,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mode", choices=["keyword", "semantic", "both"], default="both")
    args = ap.parse_args()
    modes = ["keyword", "semantic"] if args.mode == "both" else [args.mode]
    out = []
    for m in modes:
        try:
            out.append(run(m))
        except ImportError as e:
            print(f"  skipping {m}: {e}", file=sys.stderr)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
