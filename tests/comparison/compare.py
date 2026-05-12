"""Aggregate per-provider ``findings_scale.yaml`` files into a markdown table.

Reads every ``findings_scale_lxc/<provider>/findings_scale.yaml`` and emits a
single comparison table to stdout. The table is the README's source of truth:
each cell is backed by the corresponding YAML; the YAML is backed by a real
session captured in ``transcript.md`` + ``raw/`` next to it.

Run::

    python -m tests.comparison.compare > /tmp/comparison.md
"""

from __future__ import annotations

import sys
from pathlib import Path

FINDINGS_DIR = Path(__file__).parent / "findings_scale_lxc"

# Order rows so this plugin's row is first (parity with the README layout),
# then alphabetical for the others — no preferential treatment within the
# "competitors" block.
ROW_ORDER = [
    "yantrikdb",
    "byterover", "hindsight", "holographic", "honcho", "mem0",
    "openviking", "retaindb", "supermemory",
]


def _parse_yaml(path: Path) -> dict:
    """Minimal YAML parser for our flat schema (no PyYAML dep)."""
    out: dict = {}
    section: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            section = None
            continue
        if line.startswith("  "):
            key, _, val = line.strip().partition(":")
            val = val.strip().strip('"')
            if section:
                out.setdefault(section, {})[key] = val
        elif line and not line.startswith("#"):
            key, _, val = line.partition(":")
            val = val.strip().strip('"')
            if val == "":
                section = key.strip()
            else:
                out[key.strip()] = val
                section = None
    return out


def _bool(v: str) -> bool:
    return str(v).lower() == "true"


def _format_row(provider: str, data: dict) -> str:
    is_self = provider == "yantrikdb"
    name = f"**{provider}** (this)" if is_self else f"[{provider}](https://github.com/NousResearch/hermes-agent/tree/main/plugins/memory/{provider})"
    backend = data.get("backend", {})
    hosting = backend.get("hosting", "unknown")
    requires_account = _bool(backend.get("requires_account", "false"))
    requires_server = _bool(backend.get("requires_separate_server", "false"))
    scale = data.get("scale", {})
    pk = data.get("precision_at_k", {})
    shape = data.get("response_shape", {})
    maint = data.get("maintenance", {})
    couldnt = data.get("couldnt_verify", {})

    # Distinguish "totally couldn't verify" (no data captured) from "partial
    # verify with notes" (real data + a notes line about what didn't finish).
    queries_completed = int(scale.get("queries_completed", "0") or 0)
    partial_note = ""
    if couldnt.get("reason") and queries_completed == 0:
        verified_at_scale = f"couldn't verify ({hosting})"
        writes = "—"
        recall_p50 = "—"
        pk_str = "—"
        why_str = "—"
        maint_str = "—"
    else:
        if couldnt.get("reason"):
            partial_note = f" *(partial: {couldnt['reason'][:80]})*"
        writes_n = scale.get("corpus_size_written", "0")
        writes_attempted = scale.get("corpus_size_attempted", "1000")
        write_p50 = scale.get("write_p50_ms", "0")
        write_p99 = scale.get("write_p99_ms", "0")
        recall_p50_v = scale.get("recall_p50_ms", "0")
        recall_p99 = scale.get("recall_p99_ms", "0")
        hits = pk.get("hits", "0")
        total = pk.get("total_queries", "0")
        pk_val = pk.get("value", "0.0")
        why = _bool(shape.get("why_retrieved_field", "false"))
        why_name = shape.get("why_retrieved_field_name", "")
        contra = maint.get("contradiction_surfaced", "unknown")
        contra_api = maint.get("contradiction_api", "")
        dup_canon = maint.get("duplicate_canonicalized", "unknown")

        verified_at_scale = f"yes ({writes_n}/{writes_attempted} writes){partial_note}"
        writes = f"{writes_n}/{writes_attempted}; p50 {write_p50}ms / p99 {write_p99}ms"
        recall_p50 = f"p50 {recall_p50_v}ms / p99 {recall_p99}ms"
        pk_str = f"**{pk_val}** ({hits}/{total})"
        why_str = f"yes — `{why_name}`" if why else "no"
        maint_str = ""
        if contra == "true" and contra_api:
            maint_str += f"contradiction API: `{contra_api}`; "
        if dup_canon == "false":
            maint_str += "duplicates kept separate (no synchronous canon)"
        elif dup_canon == "true":
            maint_str += "duplicates canonicalized synchronously"
        elif dup_canon == "possibly":
            maint_str += "duplicates partially canonicalized"
        else:
            maint_str += "—"

    return f"| {name} | {hosting} | {verified_at_scale} | {writes} | {recall_p50} | {pk_str} | {why_str} | {maint_str} |"


def main() -> int:
    rows: list[str] = []
    rows.append("| Provider | Hosting | Verified at 1000 scale | Writes (succeeded/attempted; latency) | Recall latency | Precision@5 | `why_retrieved` field | Maintenance behaviour observed |")
    rows.append("|---|---|---|---|---|---|---|---|")

    found = []
    for provider in ROW_ORDER:
        path = FINDINGS_DIR / provider / "findings_scale.yaml"
        if not path.exists():
            continue
        data = _parse_yaml(path)
        rows.append(_format_row(provider, data))
        found.append(provider)

    print("\n".join(rows))
    print("")
    print("**Methodology** — Each provider was instantiated against the same Hermes 0.9.0 install (LXC 129 / commit `4610551`), driven through a deterministic 1000-fact corpus (`tests/comparison/fixtures/corpus_1k.json` — 600 realistic facts + 300 noise + 50 planted duplicates + 50 planted contradictions, seed=20260512) and a 20-query set with planted target fact-ids. Precision@5 = (queries whose planted target appeared in the top-5 result set) / (queries completed). Writes are timed individually with backpressure-retry on transient queue-full errors. Per-provider call-shape mappings (`PROVIDER_CALL_SHAPES` in `probe.py`) are needed because providers expose genuinely different APIs (e.g. holographic's action-dispatched `fact_store`, hindsight's `*_retain`/`*_recall` pair). Cloud-only providers without configured accounts emit honest `couldn't_verify` rows; their full `plugin.yaml` description is preserved in their findings file.")
    print("")
    print("**Reproduce** — `python -m tests.comparison.runner_scale --all` (LXC) or `python -m tests.comparison.providers.<name>.adapter` (local). Findings + transcripts + raw responses live under [tests/comparison/findings_scale_lxc/](findings_scale_lxc/).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
