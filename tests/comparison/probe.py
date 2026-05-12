"""The probe — exercises any Hermes ``MemoryProvider`` against the canonical fixtures.

Loads ``fixtures/remember.json`` + ``fixtures/recall.json`` and drives them
through a provider, capturing observable behaviour (not subjective
quality). Output is a ``findings.yaml`` per the schema documented in
``tests/comparison/README.md``.

Design constraint: the probe must work against *any* ``MemoryProvider``
implementation (this plugin, hindsight, holographic, …) without provider-
specific branching. Per-provider quirks (config, install) live in the
provider's ``adapter.py``; the probe only sees the ABC surface.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# Per-provider call shapes. Different providers use genuinely different APIs:
#   * yantrikdb/mem0/hindsight: paired (remember_tool, recall_tool) with simple args.
#   * holographic: single fact_store tool, action-dispatched via args.action.
#   * byterover/supermemory: depend on each plugin's __init__.py — added when verified.
#
# Each entry maps to (tool_name, args_factory). The args_factory takes the
# fact-text or query string and returns the args dict the provider expects.
#
# When a provider isn't listed here, the probe falls back to keyword discovery
# (looking for 'remember/store/add' for write, 'recall/search/query' for read).
PROVIDER_CALL_SHAPES: dict[str, dict[str, Any]] = {
    "yantrikdb": {
        "remember_tool": "yantrikdb_remember",
        # yantrikdb_remember requires `text` (verified from the tool schema
        # via "Missing required parameter: text" error on first probe run).
        "remember_args": lambda text: {"text": text},
        "recall_tool": "yantrikdb_recall",
        "recall_args": lambda query, top_k: {"query": query, "top_k": top_k},
    },
    "holographic": {
        "remember_tool": "fact_store",
        # Holographic uses action-dispatch: same tool, action argument selects op.
        "remember_args": lambda text: {"action": "add", "content": text, "category": "general"},
        "recall_tool": "fact_store",
        "recall_args": lambda query, top_k: {"action": "search", "query": query, "limit": top_k},
    },
    "hindsight": {
        # Tools observed on LXC: hindsight_retain, hindsight_recall, hindsight_reflect.
        # Arg keys verified by reading plugins/memory/hindsight/__init__.py on LXC.
        "remember_tool": "hindsight_retain",
        "remember_args": lambda text: {"content": text},
        "recall_tool": "hindsight_recall",
        "recall_args": lambda query, top_k: {"query": query, "limit": top_k},
    },
    # mem0, honcho, openviking, byterover, retaindb, supermemory — added as
    # each one is verified. The probe records "couldn't fit shape" honestly
    # when no entry exists and keyword discovery fails.
}


def _load_fixtures() -> tuple[list[dict], list[dict]]:
    remember = json.loads((FIXTURES_DIR / "remember.json").read_text(encoding="utf-8"))
    recall = json.loads((FIXTURES_DIR / "recall.json").read_text(encoding="utf-8"))
    return remember["facts"], recall["queries"]


# Field-name candidates we look for when checking whether the recall
# response includes a "why did this rank" surface. The list is deliberately
# generous — provider authors picked different names. Adding a candidate
# means "this name, if present on a recall result, counts as a why_retrieved-
# style field for the comparison".
_WHY_RETRIEVED_CANDIDATES: tuple[str, ...] = (
    "why_retrieved",
    "reasoning",
    "reason",
    "reasons",
    "explanation",
    "explain",
    "ranking_reason",
    "ranking_reasons",
    "match_reasons",
    "match_reason",
)

# Field-name candidates for per-result score.
_SCORE_CANDIDATES: tuple[str, ...] = ("score", "similarity", "relevance", "rank")

# Tool-name regex that catches *_skill_* / *skill* / procedure exposures.
_SKILL_TOOL_RE = re.compile(r"skill|procedure|procedural", re.IGNORECASE)


@dataclass
class FindingsRow:
    """Structured per-provider findings — serialised to findings.yaml."""

    provider: str
    version_under_test: str = ""
    verified_at: str = ""
    verified_against: str = ""

    # backend (observable, not declared)
    backend_hosting: str = "unknown"            # cloud | self-hosted | embedded
    backend_requires_account: str = "unknown"   # true | false | unknown (as string for yaml clarity)
    backend_requires_separate_server: str = "unknown"
    backend_pip_footprint_mb: float | None = None

    # contract
    initialize_ok: bool = False
    remember_ok: bool = False
    recall_ok: bool = False
    recall_returned_results: int = 0
    contract_notes: str = ""

    # response shape
    why_retrieved_field: bool = False
    why_retrieved_field_name: str = ""
    per_result_score: bool = False
    per_result_metadata: bool = False

    # maintenance
    duplicate_canonicalized: str = "unknown"    # true | false | unknown
    contradiction_surfaced: str = "unknown"
    contradiction_api: str = ""

    # skills
    skill_tools_in_schema: bool = False
    skill_tool_names: list[str] = field(default_factory=list)

    # evidence pointers
    transcript_file: str = ""
    raw_responses_dir: str = ""

    # explicit-skip path
    couldnt_verify_reason: str = ""
    couldnt_verify_what_we_know_anyway: str = ""


def probe_provider(
    provider: Any,
    provider_name: str,
    *,
    raw_responses_out: Path | None = None,
    transcript_out: Path | None = None,
) -> FindingsRow:
    """Drive a single ``MemoryProvider`` instance through the canonical fixtures.

    The provider must already be initialised by the caller (the adapter
    knows the provider's config story; the probe should not). All raw
    responses are dumped to ``raw_responses_out/`` for later inspection;
    a human-readable session log is written to ``transcript_out``.
    """
    row = FindingsRow(provider=provider_name)
    facts, queries = _load_fixtures()

    transcript_lines: list[str] = []
    raw_dir = raw_responses_out or Path("/tmp") / f"comparison-{provider_name}-raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    def _log(msg: str) -> None:
        transcript_lines.append(msg)

    def _dump_raw(name: str, obj: Any) -> None:
        try:
            (raw_dir / f"{name}.json").write_text(
                json.dumps(obj, indent=2, default=str), encoding="utf-8",
            )
        except Exception as e:
            _log(f"[raw-dump fail] {name}: {e}")

    # -- 1. initialize ---------------------------------------------------
    _log(f"# {provider_name} — comparison probe")
    _log(f"started: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    try:
        provider.initialize(session_id="comparison-probe")
        row.initialize_ok = True
        _log("✓ initialize() ok")
    except Exception as e:
        row.contract_notes = f"initialize() raised: {e!r}"
        _log(f"✗ initialize() FAILED: {e!r}")
        return _finalise(row, transcript_lines, transcript_out, raw_dir)

    # -- 2. tool-schema introspection (skills) ---------------------------
    try:
        schemas = provider.get_tool_schemas() or []
    except Exception as e:
        schemas = []
        _log(f"[get_tool_schemas raised: {e!r}]")
    skill_tools = [
        s.get("name", "") for s in schemas
        if isinstance(s, dict) and _SKILL_TOOL_RE.search(s.get("name", ""))
    ]
    row.skill_tools_in_schema = bool(skill_tools)
    row.skill_tool_names = skill_tools
    _log(
        f"tool schemas: {len(schemas)} total; "
        f"skill-related: {skill_tools or '(none)'}"
    )

    # The remember/recall tool names and arg shapes vary per provider. Use the
    # explicit PROVIDER_CALL_SHAPES registry where available; fall back to
    # keyword discovery otherwise (records the fallback in notes).
    shape = PROVIDER_CALL_SHAPES.get(provider_name)
    if shape:
        remember_tool = shape["remember_tool"]
        recall_tool = shape["recall_tool"]
        remember_args_fn = shape["remember_args"]
        recall_args_fn = shape["recall_args"]
        _log(f"using explicit call shape: remember={remember_tool} recall={recall_tool}")
    else:
        remember_tool = _find_tool(schemas, ("remember", "store", "add"))
        recall_tool = _find_tool(schemas, ("recall", "search", "query", "retrieve"))
        remember_args_fn = lambda text: {"content": text, "text": text}  # noqa: E731
        recall_args_fn = lambda query, top_k: {"query": query, "top_k": top_k}  # noqa: E731
        _log(
            f"fallback keyword discovery: remember={remember_tool!r} "
            f"recall={recall_tool!r}"
        )
        if not remember_tool or not recall_tool:
            row.contract_notes = (
                f"could not locate remember/recall tools in schema and no entry in "
                f"PROVIDER_CALL_SHAPES; saw {[s.get('name') for s in schemas]}"
            )
            _log(f"✗ {row.contract_notes}")
            return _finalise(row, transcript_lines, transcript_out, raw_dir)

    # -- 3. remember the canonical facts ---------------------------------
    contradiction_response: Any = None
    for fact in facts:
        args = remember_args_fn(fact["text"])
        try:
            resp = provider.handle_tool_call(remember_tool, args)
            _dump_raw(f"remember-{fact['id']}-{fact['category']}", resp)
            row.remember_ok = True
            _log(f"✓ remember #{fact['id']} ({fact['category']}): ok")
            if fact["category"] == "contradiction":
                contradiction_response = resp
        except Exception as e:
            _log(f"✗ remember #{fact['id']} FAILED: {e!r}")

    # -- 4. recall the canonical queries ---------------------------------
    all_recall_responses: list[Any] = []
    for q in queries:
        args = recall_args_fn(q["query"], q["top_k"])
        try:
            resp = provider.handle_tool_call(recall_tool, args)
            _dump_raw(f"recall-{q['id']}", resp)
            all_recall_responses.append(resp)
            row.recall_ok = True
            _log(f"✓ recall {q['id']} ({q['query']!r}): captured")
        except Exception as e:
            _log(f"✗ recall {q['id']} FAILED: {e!r}")

    # -- 5. response-shape introspection on the FIRST recall response ----
    if all_recall_responses:
        first = all_recall_responses[0]
        items = _extract_result_items(first)
        row.recall_returned_results = len(items)
        if items:
            sample = items[0] if isinstance(items[0], dict) else {}
            for cand in _WHY_RETRIEVED_CANDIDATES:
                if cand in sample:
                    row.why_retrieved_field = True
                    row.why_retrieved_field_name = cand
                    break
            for cand in _SCORE_CANDIDATES:
                if cand in sample:
                    row.per_result_score = True
                    break
            row.per_result_metadata = "metadata" in sample
        _log(
            f"shape: results={row.recall_returned_results} "
            f"why_retrieved_field={row.why_retrieved_field}"
            f"({row.why_retrieved_field_name!r}) "
            f"score={row.per_result_score} metadata={row.per_result_metadata}"
        )

    # -- 6. duplicate-canonicalization observation -----------------------
    # Heuristic: after writing fact #1 + fact #3 (exact duplicate), query Q1
    # ("editor color scheme") and see if both records came back or one.
    if all_recall_responses:
        items = _extract_result_items(all_recall_responses[0])
        dark_mode_hits = [
            it for it in items
            if isinstance(it, dict)
            and "dark mode" in (
                (it.get("text") or it.get("content") or it.get("memory") or "")
            ).lower()
        ]
        if dark_mode_hits:
            row.duplicate_canonicalized = "true" if len(dark_mode_hits) == 1 else "false"
            _log(
                f"duplicate-canonicalization: {len(dark_mode_hits)} 'dark mode' result(s) "
                f"after writing the same fact twice → canonicalized={row.duplicate_canonicalized}"
            )

    # -- 7. contradiction-surfacing observation --------------------------
    # If the contradiction-write response carried any conflict/contradiction
    # signal in its envelope, that's a yes. Otherwise look for a
    # *_conflicts tool in the schema.
    contradiction_signal = False
    if isinstance(contradiction_response, dict):
        text_blob = json.dumps(contradiction_response).lower()
        if any(k in text_blob for k in ("conflict", "contradiction")):
            contradiction_signal = True
    conflict_tool = _find_tool(schemas, ("conflict", "contradiction"))
    if conflict_tool:
        contradiction_signal = True
        row.contradiction_api = conflict_tool
    row.contradiction_surfaced = "true" if contradiction_signal else "false"
    _log(
        f"contradiction-surfacing: signal_in_response={contradiction_response is not None} "
        f"conflict_tool={conflict_tool!r} → surfaced={row.contradiction_surfaced}"
    )

    return _finalise(row, transcript_lines, transcript_out, raw_dir)


def _find_tool(schemas: list[dict], keywords: tuple[str, ...]) -> str:
    """Return the first tool name whose name contains any of the keywords."""
    for s in schemas:
        if not isinstance(s, dict):
            continue
        name = s.get("name", "")
        if any(k in name.lower() for k in keywords):
            return name
    return ""


def _extract_result_items(resp: Any) -> list[Any]:
    """Coerce a ``handle_tool_call`` recall return into a list of items.

    Providers return: JSON string of {"results": [...]} (Hermes convention),
    or a dict {"results": [...]}, or a bare list. We unwrap all three.
    """
    if isinstance(resp, str):
        try:
            resp = json.loads(resp)
        except json.JSONDecodeError:
            return []
    if isinstance(resp, dict):
        for key in ("results", "memories", "items", "data", "hits"):
            if key in resp and isinstance(resp[key], list):
                return resp[key]
        return []
    if isinstance(resp, list):
        return resp
    return []


def _finalise(
    row: FindingsRow,
    transcript_lines: list[str],
    transcript_out: Path | None,
    raw_dir: Path,
) -> FindingsRow:
    row.raw_responses_dir = str(raw_dir)
    if transcript_out is not None:
        transcript_out.write_text("\n".join(transcript_lines) + "\n", encoding="utf-8")
        row.transcript_file = str(transcript_out)
    return row


def findings_to_yaml(row: FindingsRow) -> str:
    """Serialise a FindingsRow to the schema documented in README.md.

    Hand-rolled rather than `yaml.safe_dump` to keep the diffs readable
    (no Python tag pollution, key order matches the schema).
    """
    def _q(s: str) -> str:
        # quote values that look like yaml-special tokens
        if s == "" or any(c in s for c in (":", "#", "[", "]", "{", "}", ",", "&", "*", "!", "|", ">")) or s in ("true", "false", "null", "unknown"):
            return f'"{s}"' if s != "" else '""'
        return s

    lines: list[str] = []
    lines.append(f"provider: {_q(row.provider)}")
    lines.append(f"version_under_test: {_q(row.version_under_test)}")
    lines.append(f"verified_at: {_q(row.verified_at)}")
    lines.append(f"verified_against: {_q(row.verified_against)}")
    lines.append("")
    lines.append("backend:")
    lines.append(f"  hosting: {_q(row.backend_hosting)}")
    lines.append(f"  requires_account: {_q(row.backend_requires_account)}")
    lines.append(f"  requires_separate_server: {_q(row.backend_requires_separate_server)}")
    lines.append(f"  pip_footprint_mb: {row.backend_pip_footprint_mb if row.backend_pip_footprint_mb is not None else 'null'}")
    lines.append("")
    lines.append("contract:")
    lines.append(f"  initialize_ok: {str(row.initialize_ok).lower()}")
    lines.append(f"  remember_ok: {str(row.remember_ok).lower()}")
    lines.append(f"  recall_ok: {str(row.recall_ok).lower()}")
    lines.append(f"  recall_returned_results: {row.recall_returned_results}")
    lines.append(f"  notes: {_q(row.contract_notes)}")
    lines.append("")
    lines.append("response_shape:")
    lines.append(f"  why_retrieved_field: {str(row.why_retrieved_field).lower()}")
    lines.append(f"  why_retrieved_field_name: {_q(row.why_retrieved_field_name)}")
    lines.append(f"  per_result_score: {str(row.per_result_score).lower()}")
    lines.append(f"  per_result_metadata: {str(row.per_result_metadata).lower()}")
    lines.append("")
    lines.append("maintenance:")
    lines.append(f"  duplicate_canonicalized: {_q(row.duplicate_canonicalized)}")
    lines.append(f"  contradiction_surfaced: {_q(row.contradiction_surfaced)}")
    lines.append(f"  contradiction_api: {_q(row.contradiction_api)}")
    lines.append("")
    lines.append("skills:")
    lines.append(f"  skill_tools_in_schema: {str(row.skill_tools_in_schema).lower()}")
    lines.append("  skill_tool_names:")
    if row.skill_tool_names:
        for n in row.skill_tool_names:
            lines.append(f"    - {_q(n)}")
    else:
        lines[-1] = "  skill_tool_names: []"
    lines.append("")
    lines.append("evidence:")
    lines.append(f"  transcript_file: {_q(row.transcript_file)}")
    lines.append(f"  raw_responses_dir: {_q(row.raw_responses_dir)}")
    lines.append("")
    lines.append("couldnt_verify:")
    lines.append(f"  reason: {_q(row.couldnt_verify_reason)}")
    lines.append(f"  what_we_know_anyway: {_q(row.couldnt_verify_what_we_know_anyway)}")
    return "\n".join(lines) + "\n"


__all__ = ["FindingsRow", "probe_provider", "findings_to_yaml"]
