"""Scale-mode probe: 1000-memory comparison.

Drives a ``MemoryProvider`` through 1000 canonical facts + 20 queries with
planted targets, capturing:

  - Write timing (p50 / p99) across 1000 writes
  - Recall timing (p50 / p99) across 20 queries
  - Precision@K for each query against its planted target_text
  - Response-shape probes (why_retrieved field, score, metadata) on the first
    non-empty result — same as the small probe
  - Duplicate-canonicalization behaviour (count of duplicate fact-text in
    the 1000 stored corpus, sampled via a known-duplicate query)
  - Contradiction surfacing (does the provider expose any *conflicts* /
    *contradiction* tool, or does the contradiction-write response include
    a signal)

Output is a richer ``ScaleFindingsRow`` serialised to ``findings_scale.yaml``.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from probe import (
    _SCORE_CANDIDATES,
    _SKILL_TOOL_RE,
    _WHY_RETRIEVED_CANDIDATES,
    PROVIDER_CALL_SHAPES,
    _extract_result_items,
    _find_tool,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@dataclass
class ScaleFindingsRow:
    provider: str
    version_under_test: str = ""
    verified_at: str = ""
    verified_against: str = ""

    backend_hosting: str = "unknown"
    backend_requires_account: str = "unknown"
    backend_requires_separate_server: str = "unknown"

    corpus_size_attempted: int = 0
    corpus_size_written: int = 0
    write_failures: int = 0
    write_p50_ms: float = 0.0
    write_p99_ms: float = 0.0

    queries_attempted: int = 0
    queries_completed: int = 0
    recall_p50_ms: float = 0.0
    recall_p99_ms: float = 0.0

    # Precision@K: of the queries that completed, how many had the planted
    # target_text appear in the top-K results?
    precision_at_k_hits: int = 0          # numerator
    precision_at_k_queries: int = 0       # denominator (queries that completed)
    precision_at_k_value: float = 0.0     # hits / queries

    # Response shape (sampled from first non-empty recall)
    why_retrieved_field: bool = False
    why_retrieved_field_name: str = ""
    per_result_score: bool = False
    per_result_metadata: bool = False

    # Maintenance
    duplicate_canonicalized: str = "unknown"
    duplicate_count_observed: int = 0      # how many results match a duplicate query
    contradiction_surfaced: str = "unknown"
    contradiction_api: str = ""

    # Skills
    skill_tools_in_schema: bool = False
    skill_tool_names: list[str] = field(default_factory=list)

    # Evidence
    transcript_file: str = ""
    raw_responses_dir: str = ""

    couldnt_verify_reason: str = ""
    couldnt_verify_what_we_know_anyway: str = ""

    # Per-query results — captured for the transcript
    per_query: list[dict[str, Any]] = field(default_factory=list)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * pct
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[int(k)]
    return s[f] + (s[c] - s[f]) * (k - f)


def probe_at_scale(
    provider: Any,
    provider_name: str,
    *,
    corpus_path: Path | None = None,
    queries_path: Path | None = None,
    raw_responses_out: Path | None = None,
    transcript_out: Path | None = None,
    write_timeout_s: float = 600.0,
) -> ScaleFindingsRow:
    row = ScaleFindingsRow(provider=provider_name)
    corpus_path = corpus_path or (FIXTURES_DIR / "corpus_1k.json")
    queries_path = queries_path or (FIXTURES_DIR / "queries_1k.json")

    corpus = json.loads(corpus_path.read_text())["facts"]
    queries = json.loads(queries_path.read_text())["queries"]
    row.corpus_size_attempted = len(corpus)
    row.queries_attempted = len(queries)

    transcript_lines: list[str] = []
    raw_dir = raw_responses_out or Path("/tmp") / f"scale-{provider_name}-raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    def _log(msg: str) -> None:
        transcript_lines.append(msg)

    _log(f"# {provider_name} — 1000-memory scale probe")
    _log(f"started: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")

    # Initialize provider
    try:
        provider.initialize(session_id="scale-probe")
    except Exception as e:
        row.couldnt_verify_reason = f"initialize() raised: {e!r}"
        return _finalise(row, transcript_lines, transcript_out, raw_dir)

    # Tool-schema introspection
    try:
        schemas = provider.get_tool_schemas() or []
    except Exception:
        schemas = []
    skill_tools = [
        s.get("name", "") for s in schemas
        if isinstance(s, dict) and _SKILL_TOOL_RE.search(s.get("name", ""))
    ]
    row.skill_tools_in_schema = bool(skill_tools)
    row.skill_tool_names = skill_tools
    _log(f"tools: {[s.get('name') for s in schemas]}")

    # Resolve call shape (registry → keyword fallback)
    shape = PROVIDER_CALL_SHAPES.get(provider_name)
    if shape:
        remember_tool = shape["remember_tool"]
        recall_tool = shape["recall_tool"]
        remember_args_fn = shape["remember_args"]
        recall_args_fn = shape["recall_args"]
    else:
        remember_tool = _find_tool(schemas, ("remember", "store", "add", "retain"))
        recall_tool = _find_tool(schemas, ("recall", "search", "query", "retrieve"))
        remember_args_fn = lambda text: {"content": text, "text": text}  # noqa: E731
        recall_args_fn = lambda query, top_k: {"query": query, "top_k": top_k}  # noqa: E731

    if not remember_tool or not recall_tool:
        row.couldnt_verify_reason = (
            f"could not resolve remember/recall tools; saw {[s.get('name') for s in schemas]}"
        )
        return _finalise(row, transcript_lines, transcript_out, raw_dir)

    # -- WRITE PHASE -----------------------------------------------------
    _log(f"writing {len(corpus)} facts via {remember_tool}...")
    write_times: list[float] = []
    failures = 0
    contradiction_responses: list[Any] = []
    write_start = time.perf_counter()

    backpressure_retries = 0
    for fact in corpus:
        if time.perf_counter() - write_start > write_timeout_s:
            _log(f"WRITE TIMEOUT at fact #{fact['id']} after {write_timeout_s}s")
            row.couldnt_verify_reason = (
                f"write phase exceeded {write_timeout_s}s; aborted at fact {fact['id']}"
            )
            break
        args = remember_args_fn(fact["text"])
        # Honest backpressure handling — engines like yantrikdb expose a
        # bounded ingest queue. An agent wouldn't drop writes; it would
        # retry. Loop until the write succeeds OR a non-backpressure error
        # surfaces OR cumulative backpressure-wait exceeds 30 s for this
        # one fact (then give up).
        attempts = 0
        max_attempts = 60       # 60 × 100ms = 6 s max per fact
        succeeded = False
        while attempts < max_attempts:
            attempts += 1
            t0 = time.perf_counter()
            try:
                resp = provider.handle_tool_call(remember_tool, args)
                dt = (time.perf_counter() - t0) * 1000  # ms
                write_times.append(dt)
                if fact.get("planted_kind") == "contradiction_alt":
                    contradiction_responses.append(resp)
                succeeded = True
                break
            except Exception as e:
                err_text = str(e).lower()
                if any(k in err_text for k in ("queue full", "rate limit", "429", "too many")):
                    backpressure_retries += 1
                    time.sleep(0.1)
                    continue
                failures += 1
                if failures <= 3:
                    _log(f"write failed for fact #{fact['id']}: {e!r}")
                break
        if not succeeded and attempts >= max_attempts:
            failures += 1
            if failures <= 3:
                _log(f"write retries exhausted for fact #{fact['id']} after {max_attempts} attempts")
    if backpressure_retries:
        _log(f"backpressure retries: {backpressure_retries}")

    row.corpus_size_written = len(write_times)
    row.write_failures = failures
    if write_times:
        row.write_p50_ms = _percentile(write_times, 0.5)
        row.write_p99_ms = _percentile(write_times, 0.99)
    _log(
        f"writes: {row.corpus_size_written}/{row.corpus_size_attempted} ok "
        f"(failures={failures}); p50={row.write_p50_ms:.2f}ms p99={row.write_p99_ms:.2f}ms"
    )

    if row.corpus_size_written < 100:
        # Provider couldn't take the load; honest stop.
        row.couldnt_verify_reason = (
            f"only {row.corpus_size_written} writes succeeded of {row.corpus_size_attempted}; "
            "provider can't sustain the corpus load"
        )
        return _finalise(row, transcript_lines, transcript_out, raw_dir)

    # -- RECALL PHASE ----------------------------------------------------
    _log(f"running {len(queries)} queries via {recall_tool}...")
    recall_times: list[float] = []
    per_query_results: list[dict[str, Any]] = []
    first_non_empty_response: Any = None

    for q in queries:
        args = recall_args_fn(q["query"], q["top_k"])
        t0 = time.perf_counter()
        try:
            resp = provider.handle_tool_call(recall_tool, args)
            dt = (time.perf_counter() - t0) * 1000
            recall_times.append(dt)
            items = _extract_result_items(resp)
            if items and first_non_empty_response is None:
                first_non_empty_response = resp
            # Precision@K: does any returned item's text contain the target?
            target = q.get("target_text", "")
            hit = False
            if target and items:
                target_words = set(target.lower().split())
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    item_text = (
                        it.get("text") or it.get("content")
                        or it.get("memory") or it.get("body") or ""
                    )
                    if not item_text:
                        continue
                    # Exact substring OR strong word overlap
                    if target.lower() in item_text.lower():
                        hit = True
                        break
                    item_words = set(item_text.lower().split())
                    overlap = len(target_words & item_words)
                    if overlap >= max(3, len(target_words) // 2):
                        hit = True
                        break
            per_query_results.append({
                "id": q["id"], "query": q["query"], "target_kind": q.get("target_kind"),
                "returned": len(items), "hit": hit, "latency_ms": round(dt, 2),
            })
            (raw_dir / f"recall-{q['id']}.json").write_text(
                json.dumps(resp, indent=2, default=str), encoding="utf-8",
            )
        except Exception as e:
            per_query_results.append({
                "id": q["id"], "query": q["query"],
                "error": repr(e), "hit": False, "latency_ms": -1,
            })

    row.queries_completed = sum(1 for r in per_query_results if "error" not in r)
    if recall_times:
        row.recall_p50_ms = _percentile(recall_times, 0.5)
        row.recall_p99_ms = _percentile(recall_times, 0.99)

    hits = sum(1 for r in per_query_results if r.get("hit"))
    row.precision_at_k_hits = hits
    row.precision_at_k_queries = row.queries_completed
    row.precision_at_k_value = (hits / row.queries_completed) if row.queries_completed else 0.0
    row.per_query = per_query_results
    _log(
        f"recalls: {row.queries_completed}/{row.queries_attempted} ok; "
        f"p50={row.recall_p50_ms:.2f}ms p99={row.recall_p99_ms:.2f}ms; "
        f"precision@K={hits}/{row.queries_completed}"
    )

    # -- RESPONSE SHAPE introspection on first non-empty response --------
    if first_non_empty_response is not None:
        items = _extract_result_items(first_non_empty_response)
        if items and isinstance(items[0], dict):
            sample = items[0]
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
            f"shape (first non-empty result): why_retrieved={row.why_retrieved_field}"
            f"({row.why_retrieved_field_name!r}) score={row.per_result_score} "
            f"metadata={row.per_result_metadata}"
        )

    # -- Contradiction surfacing -----------------------------------------
    contra_signal = False
    for cr in contradiction_responses[:5]:
        if isinstance(cr, str):
            try:
                cr = json.loads(cr)
            except Exception:
                pass
        if isinstance(cr, dict) and any(
            k in json.dumps(cr).lower() for k in ("conflict", "contradiction")
        ):
            contra_signal = True
            break
    conflict_tool = _find_tool(schemas, ("conflict", "contradiction"))
    if conflict_tool:
        contra_signal = True
        row.contradiction_api = conflict_tool
    row.contradiction_surfaced = "true" if contra_signal else "false"

    # -- Duplicate canonicalization observation --------------------------
    # Count how many results came back from a Q-dup-* query — if 1, possibly
    # canonicalized; if 2+, definitely not canonicalized synchronously.
    dup_returns = [r for r in per_query_results if r["id"].startswith("Q-dup-")]
    if dup_returns:
        avg_dup_count = sum(r.get("returned", 0) for r in dup_returns) / len(dup_returns)
        row.duplicate_count_observed = round(avg_dup_count, 1)
        # If each dup query returns >= 2 results that include the duplicated
        # text, that's strong evidence of no synchronous canonicalization.
        if avg_dup_count >= 2.0:
            row.duplicate_canonicalized = "false"
        elif avg_dup_count == 0:
            row.duplicate_canonicalized = "unknown"  # query didn't surface anything
        else:
            row.duplicate_canonicalized = "possibly"
    _log(f"duplicate-canonicalization: avg results per Q-dup-* query = {row.duplicate_count_observed} → {row.duplicate_canonicalized}")

    return _finalise(row, transcript_lines, transcript_out, raw_dir)


def _finalise(
    row: ScaleFindingsRow,
    transcript_lines: list[str],
    transcript_out: Path | None,
    raw_dir: Path,
) -> ScaleFindingsRow:
    row.raw_responses_dir = str(raw_dir)
    if transcript_out is not None:
        transcript_out.write_text("\n".join(transcript_lines) + "\n", encoding="utf-8")
        row.transcript_file = str(transcript_out)
    return row


def scale_findings_to_yaml(row: ScaleFindingsRow) -> str:
    def _q(s: str) -> str:
        if s == "" or any(c in s for c in (":", "#", "[", "]", "{", "}", ",")) or s in ("true", "false", "null", "unknown"):
            return f'"{s}"' if s != "" else '""'
        return s

    L: list[str] = []
    L.append(f"provider: {_q(row.provider)}")
    L.append(f"version_under_test: {_q(row.version_under_test)}")
    L.append(f"verified_at: {_q(row.verified_at)}")
    L.append(f"verified_against: {_q(row.verified_against)}")
    L.append("")
    L.append("backend:")
    L.append(f"  hosting: {_q(row.backend_hosting)}")
    L.append(f"  requires_account: {_q(row.backend_requires_account)}")
    L.append(f"  requires_separate_server: {_q(row.backend_requires_separate_server)}")
    L.append("")
    L.append("scale:")
    L.append(f"  corpus_size_attempted: {row.corpus_size_attempted}")
    L.append(f"  corpus_size_written: {row.corpus_size_written}")
    L.append(f"  write_failures: {row.write_failures}")
    L.append(f"  write_p50_ms: {round(row.write_p50_ms, 2)}")
    L.append(f"  write_p99_ms: {round(row.write_p99_ms, 2)}")
    L.append(f"  queries_attempted: {row.queries_attempted}")
    L.append(f"  queries_completed: {row.queries_completed}")
    L.append(f"  recall_p50_ms: {round(row.recall_p50_ms, 2)}")
    L.append(f"  recall_p99_ms: {round(row.recall_p99_ms, 2)}")
    L.append("")
    L.append("precision_at_k:")
    L.append(f"  hits: {row.precision_at_k_hits}")
    L.append(f"  total_queries: {row.precision_at_k_queries}")
    L.append(f"  value: {round(row.precision_at_k_value, 3)}")
    L.append("")
    L.append("response_shape:")
    L.append(f"  why_retrieved_field: {str(row.why_retrieved_field).lower()}")
    L.append(f"  why_retrieved_field_name: {_q(row.why_retrieved_field_name)}")
    L.append(f"  per_result_score: {str(row.per_result_score).lower()}")
    L.append(f"  per_result_metadata: {str(row.per_result_metadata).lower()}")
    L.append("")
    L.append("maintenance:")
    L.append(f"  duplicate_canonicalized: {_q(row.duplicate_canonicalized)}")
    L.append(f"  duplicate_count_observed: {row.duplicate_count_observed}")
    L.append(f"  contradiction_surfaced: {_q(row.contradiction_surfaced)}")
    L.append(f"  contradiction_api: {_q(row.contradiction_api)}")
    L.append("")
    L.append("skills:")
    L.append(f"  skill_tools_in_schema: {str(row.skill_tools_in_schema).lower()}")
    L.append("  skill_tool_names:")
    if row.skill_tool_names:
        for n in row.skill_tool_names:
            L.append(f"    - {_q(n)}")
    else:
        L[-1] = "  skill_tool_names: []"
    L.append("")
    L.append("evidence:")
    L.append(f"  transcript_file: {_q(row.transcript_file)}")
    L.append(f"  raw_responses_dir: {_q(row.raw_responses_dir)}")
    L.append("")
    L.append("couldnt_verify:")
    L.append(f"  reason: {_q(row.couldnt_verify_reason)}")
    L.append(f"  what_we_know_anyway: {_q(row.couldnt_verify_what_we_know_anyway)}")
    return "\n".join(L) + "\n"


__all__ = ["ScaleFindingsRow", "probe_at_scale", "scale_findings_to_yaml"]
