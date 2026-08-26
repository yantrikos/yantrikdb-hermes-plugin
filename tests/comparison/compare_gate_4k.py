"""Compare canonical gate v1.4 reports from two isolated Python environments."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GATE_NAME = "hermes-plugin/competing-distractors"
GATE_VERSION = "1.4.0"
COMPARATOR_VERSION = "1.4.0"
DEFAULT_TOP_K = 5
DEFAULT_STABILITY_QUERY = "What is Taylor's role?"

_ENGINE_KEYS = {
    "distribution_version", "module_file", "import_source", "wheel_sha256",
    "module_sha256", "extension_file", "extension_sha256",
}
_HOST_KEYS = {"platform", "python"}
_FIXTURE_KEYS = {"corpus_sha256", "queries_sha256", "probes_sha256"}
_SEED_KEYS = {"path", "sha256", "bytes", "mtime_unix", "created_at_min",
              "created_at_max", "record_count"}
_CONFIG_KEYS = {"metric_repeats", "stability_runs", "drift_probe_seconds",
                "top_k", "stability_query", "seed_created_at"}

# v1.5: the instant every seeded row is written at. Both arms must agree on
# it, because it is what removes the clock from the comparison — see
# gate_4k.SEED_CREATED_AT. A report that omits it is a v1.4 report and is
# refused by the key check above rather than silently compared.
DEFAULT_SEED_CREATED_AT = 1_600_000_000.0


class ComparisonRefusal(ValueError):
    """Raised when reports are not demonstrably comparable."""


@dataclass(frozen=True)
class GateConfig:
    metric_repeats: int
    stability_runs: int
    drift_probe_seconds: int
    top_k: int = DEFAULT_TOP_K
    stability_query: str = DEFAULT_STABILITY_QUERY
    seed_created_at: float = DEFAULT_SEED_CREATED_AT

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric_repeats": self.metric_repeats,
            "stability_runs": self.stability_runs,
            "drift_probe_seconds": self.drift_probe_seconds,
            "top_k": self.top_k,
            "stability_query": self.stability_query,
            "seed_created_at": self.seed_created_at,
        }


@dataclass(frozen=True)
class ArmRun:
    arm: str
    round_number: int
    report: Mapping[str, Any]


Runner = Callable[[str, Path, Path, GateConfig], Mapping[str, Any]]
Hasher = Callable[[Path], str]


def _refuse(message: str) -> None:
    raise ComparisonRefusal(message)


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _refuse(f"{field} must be a JSON object")
    return value


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        _refuse(f"{field} must be a JSON array")
    return value


def _require_keys(value: Mapping[str, Any], keys: set[str], field: str) -> None:
    missing = sorted(keys - set(value))
    if missing:
        _refuse(f"{field} is missing canonical fields: {', '.join(missing)}")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


def _seed_fingerprint(db_path: Path) -> str:
    """Use the runner's exact name+NUL+bytes digest over one stable main file."""
    for suffix in ("-wal", "-journal"):
        sidecar = Path(f"{db_path}{suffix}")
        if sidecar.is_file() and sidecar.stat().st_size > 0:
            _refuse(
                f"seed has a non-empty {suffix} sidecar; checkpoint it or use VACUUM INTO "
                "before comparing builds")
    files = [db_path]
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _refuse(f"{field} must be numeric")
    return float(value)


def _validate_signature(item: Any, field: str) -> Mapping[str, Any]:
    signature = _mapping(item, field)
    _require_keys(signature, {"ordering_rids", "ordering_texts", "count",
                              "observed_at_unix"}, field)
    rids = _list(signature["ordering_rids"], f"{field}.ordering_rids")
    texts = _list(signature["ordering_texts"], f"{field}.ordering_texts")
    if len(rids) != len(texts):
        _refuse(f"{field} RID/text ordering lengths differ")
    count = signature["count"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        _refuse(f"{field}.count must be a positive integer")
    timestamps = _list(signature["observed_at_unix"], f"{field}.observed_at_unix")
    if len(timestamps) != count:
        _refuse(f"{field}.count does not match observed_at_unix length")
    for index, timestamp in enumerate(timestamps):
        _number(timestamp, f"{field}.observed_at_unix[{index}]")
    return signature


def _validate_report(report: Mapping[str, Any], label: str, expected: GateConfig) -> None:
    if report.get("gate") != GATE_NAME:
        _refuse(f"{label} gate mismatch: {report.get('gate')!r}")
    if report.get("gate_version") != GATE_VERSION:
        _refuse(f"{label} gate version mismatch: expected {GATE_VERSION!r}, "
                f"got {report.get('gate_version')!r}")

    engine = _mapping(report.get("engine"), f"{label}.engine")
    host = _mapping(report.get("host"), f"{label}.host")
    fixtures = _mapping(report.get("fixtures"), f"{label}.fixtures")
    seed = _mapping(report.get("seed"), f"{label}.seed")
    config = _mapping(report.get("config"), f"{label}.config")
    observed = _mapping(report.get("observed"), f"{label}.observed")
    _require_keys(engine, _ENGINE_KEYS, f"{label}.engine")
    _require_keys(host, _HOST_KEYS, f"{label}.host")
    _require_keys(fixtures, _FIXTURE_KEYS, f"{label}.fixtures")
    _require_keys(seed, _SEED_KEYS, f"{label}.seed")
    _require_keys(config, _CONFIG_KEYS, f"{label}.config")
    _require_keys(observed, {"started_unix", "finished_unix", "seed_age_s_at_start"},
                  f"{label}.observed")
    if _canonical_bytes(config) != _canonical_bytes(expected.as_dict()):
        _refuse(f"{label} config mismatch: expected {expected.as_dict()!r}, "
                f"got {dict(config)!r}")
    if engine["import_source"] not in {"site-packages", "editable", "other"}:
        _refuse(f"{label}.engine.import_source is not canonical: {engine['import_source']!r}")
    started = _number(observed["started_unix"], f"{label}.observed.started_unix")
    finished = _number(observed["finished_unix"], f"{label}.observed.finished_unix")
    if finished < started:
        _refuse(f"{label} observed window ends before it starts")

    metrics = _mapping(report.get("metrics"), f"{label}.metrics")
    if not metrics:
        _refuse(f"{label}.metrics must not be empty")
    for name, raw_metric in metrics.items():
        metric = _mapping(raw_metric, f"{label}.metrics.{name}")
        _require_keys(metric, {"values", "mean", "stdev", "spread", "window",
                               "burst_span_s", "drift_probe"},
                      f"{label}.metrics.{name}")
        values = _list(metric["values"], f"{label}.metrics.{name}.values")
        if len(values) != expected.metric_repeats:
            _refuse(f"{label}.metrics.{name}.values count mismatch: "
                    f"expected {expected.metric_repeats}, got {len(values)}")
        for index, value in enumerate(values):
            _number(value, f"{label}.metrics.{name}.values[{index}]")
        window = _mapping(metric["window"], f"{label}.metrics.{name}.window")
        _require_keys(window, {"t_start_unix", "t_end_unix", "seed_age_s"},
                      f"{label}.metrics.{name}.window")
        drift = _mapping(metric["drift_probe"], f"{label}.metrics.{name}.drift_probe")
        _require_keys(drift, {"before", "after", "moved", "seconds"},
                      f"{label}.metrics.{name}.drift_probe")

    stability = _mapping(report.get("stability"), f"{label}.stability")
    _require_keys(stability, {"query", "runs", "distinct", "signatures"},
                  f"{label}.stability")
    if stability["query"] != expected.stability_query:
        _refuse(f"{label} stability query does not match config")
    if stability["runs"] != expected.stability_runs:
        _refuse(f"{label} stability run count does not match config")
    signatures = _list(stability["signatures"], f"{label}.stability.signatures")
    if stability["distinct"] != len(signatures):
        _refuse(f"{label} stability distinct count does not match signatures")
    total = 0
    for index, item in enumerate(signatures):
        signature = _validate_signature(item, f"{label}.stability.signatures[{index}]")
        total += signature["count"]
    if total != expected.stability_runs:
        _refuse(f"{label} stability signature counts total {total}, "
                f"expected {expected.stability_runs}")
    _mapping(report.get("lexical_degeneracy"), f"{label}.lexical_degeneracy")


def _signature_key(signature: Mapping[str, Any]) -> bytes:
    return _canonical_bytes({"ordering_rids": signature["ordering_rids"],
                             "ordering_texts": signature["ordering_texts"]})


def _collect_signatures(reports: Sequence[Mapping[str, Any]]) -> dict[bytes, dict[str, Any]]:
    collected: dict[bytes, dict[str, Any]] = {}
    for report in reports:
        stability = _mapping(report["stability"], "stability")
        for item in _list(stability["signatures"], "stability.signatures"):
            signature = _mapping(item, "stability.signature")
            key = _signature_key(signature)
            current = collected.setdefault(key, {
                "ordering_rids": signature["ordering_rids"],
                "ordering_texts": signature["ordering_texts"], "count": 0})
            current["count"] += signature["count"]
    return collected


def _stability_timestamps(report: Mapping[str, Any]) -> list[float]:
    timestamps = []
    stability = _mapping(report["stability"], "stability")
    for item in _list(stability["signatures"], "stability.signatures"):
        signature = _mapping(item, "stability.signature")
        timestamps.extend(float(value) for value in signature["observed_at_unix"])
    return sorted(timestamps)


def _range(values: Sequence[float]) -> dict[str, float | int]:
    return {"count": len(values), "min": min(values), "max": max(values),
            "range": max(values) - min(values)}


def compare_reports(runs: Sequence[ArmRun], *, expected_config: GateConfig,
                    process_orders: Sequence[Sequence[str]]) -> dict[str, Any]:
    if not runs:
        _refuse("no gate reports were supplied")
    by_round: dict[int, dict[str, Mapping[str, Any]]] = {}
    by_arm: dict[str, list[Mapping[str, Any]]] = {"baseline": [], "candidate": []}
    for run in runs:
        if run.arm not in by_arm:
            _refuse(f"unknown arm {run.arm!r}")
        _validate_report(run.report, f"{run.arm} round {run.round_number}", expected_config)
        pair = by_round.setdefault(run.round_number, {})
        if run.arm in pair:
            _refuse(f"duplicate {run.arm} report for round {run.round_number}")
        pair[run.arm] = run.report
        by_arm[run.arm].append(run.report)

    if len(by_round) < 2:
        _refuse("at least 2 paired rounds are required")
    if len(process_orders) != len(by_round):
        _refuse("process order count does not match paired round count")
    for round_number, pair in by_round.items():
        if set(pair) != {"baseline", "candidate"}:
            _refuse(f"round {round_number} does not contain both arms")

    first = runs[0].report
    # FTS5 is supplied by each interpreter's SQLite build. Requiring the exact
    # degeneracy object can conservatively refuse unlike Python environments;
    # it can never silently bless different lexical behavior as comparable.
    comparable = {"gate_version": first["gate_version"], "fixtures": first["fixtures"],
                  "seed.sha256": _mapping(first["seed"], "seed")["sha256"],
                  "config": first["config"],
                  "lexical_degeneracy": first["lexical_degeneracy"]}
    for run in runs[1:]:
        candidate = {"gate_version": run.report["gate_version"],
                     "fixtures": run.report["fixtures"],
                     "seed.sha256": _mapping(run.report["seed"], "seed")["sha256"],
                     "config": run.report["config"],
                     "lexical_degeneracy": run.report["lexical_degeneracy"]}
        for field, reference in comparable.items():
            if _canonical_bytes(candidate[field]) != _canonical_bytes(reference):
                _refuse(f"{field} mismatch across reports")

    arm_metadata = {}
    for arm, reports in by_arm.items():
        reference = {"engine": reports[0]["engine"], "host": reports[0]["host"]}
        for report in reports[1:]:
            candidate = {"engine": report["engine"], "host": report["host"]}
            if _canonical_bytes(candidate) != _canonical_bytes(reference):
                _refuse(f"{arm} engine/host metadata changed across rounds")
        arm_metadata[arm] = reference

    if arm_metadata["baseline"]["host"]["platform"] != arm_metadata["candidate"]["host"]["platform"]:
        _refuse("baseline and candidate host.platform differ")

    baseline_engine = _mapping(arm_metadata["baseline"]["engine"], "baseline.engine")
    candidate_engine = _mapping(arm_metadata["candidate"]["engine"], "candidate.engine")
    same_version = (
        baseline_engine["distribution_version"] == candidate_engine["distribution_version"])
    baseline_extension = baseline_engine.get("extension_sha256")
    candidate_extension = candidate_engine.get("extension_sha256")
    same_native_bytes = (
        baseline_extension == candidate_extension
        if baseline_extension is not None and candidate_extension is not None else None)
    if same_native_bytes is True:
        _refuse(
            "baseline and candidate use the same native extension bytes; "
            "this is a self-comparison, not an engine comparison")
    artifact_warning = (
        "SAME_VERSION_DIFFERENT_NATIVE_BYTES"
        if same_version and same_native_bytes is False else None)

    signature_sets = {arm: _collect_signatures(reports) for arm, reports in by_arm.items()}
    baseline_keys, candidate_keys = set(signature_sets["baseline"]), set(signature_sets["candidate"])

    def partition(keys: set[bytes], *, shared: bool = False) -> list[dict[str, Any]]:
        output = []
        for key in sorted(keys):
            baseline, candidate = signature_sets["baseline"].get(key), signature_sets["candidate"].get(key)
            source = baseline or candidate
            item = {"ordering_rids": source["ordering_rids"],
                    "ordering_texts": source["ordering_texts"]}
            if shared:
                item.update(baseline_count=baseline["count"], candidate_count=candidate["count"])
            elif baseline:
                item["baseline_count"] = baseline["count"]
            else:
                item["candidate_count"] = candidate["count"]
            output.append(item)
        return output

    metric_names = set(_mapping(first["metrics"], "metrics"))
    for run in runs[1:]:
        if set(_mapping(run.report["metrics"], "metrics")) != metric_names:
            _refuse("metric names mismatch across reports")
    metric_ranges = {}
    for name in sorted(metric_names):
        metric_ranges[name] = {}
        for arm, reports in by_arm.items():
            values = [float(value) for report in reports for value in
                      _mapping(_mapping(report["metrics"], "metrics")[name],
                               f"metrics.{name}")["values"]]
            metric_ranges[name][arm] = _range(values)

    # Per-round, per-repeat-index paired readings.
    #
    # `metric_ranges` above aggregates every reading from both rounds, which is
    # enough to describe a spread but NOT enough to decide an admission: a rule
    # of the form "candidate >= baseline at every paired repeat index in every
    # round" cannot be evaluated from a min/max envelope. Emitting the paired
    # values makes the decision rule computable from the report alone, and
    # auditable afterwards by someone who was not there.
    paired_metrics: dict[str, Any] = {}
    for round_number, pair in sorted(by_round.items()):
        per_round: dict[str, Any] = {}
        for name in sorted(metric_names):
            arm_values = {
                arm: [float(value) for value in
                      _mapping(_mapping(pair[arm]["metrics"], "metrics")[name],
                               f"metrics.{name}")["values"]]
                for arm in ("baseline", "candidate")
            }
            if len(arm_values["baseline"]) != len(arm_values["candidate"]):
                _refuse(f"round {round_number} metric {name} repeat counts differ: "
                        f"{len(arm_values['baseline'])} vs {len(arm_values['candidate'])}")
            deltas = [round(c - b, 12) for b, c in
                      zip(arm_values["baseline"], arm_values["candidate"], strict=True)]
            per_round[name] = {"baseline": arm_values["baseline"],
                               "candidate": arm_values["candidate"],
                               "delta": deltas,
                               "min_delta": min(deltas) if deltas else None}
        paired_metrics[str(round_number)] = per_round

    paired_runs = []
    for index, (round_number, pair) in enumerate(sorted(by_round.items())):
        baseline_times = _stability_timestamps(pair["baseline"])
        candidate_times = _stability_timestamps(pair["candidate"])
        if len(baseline_times) != len(candidate_times):
            _refuse(f"round {round_number} stability timestamp counts differ")
        skews = [round(abs(candidate - baseline), 6)
                 for baseline, candidate in zip(baseline_times, candidate_times, strict=True)]
        paired_runs.append({"round": round_number, "process_order": list(process_orders[index]),
                            "stability_timestamp_skew_s": skews,
                            "max_stability_timestamp_skew_s": max(skews),
                            "mean_stability_timestamp_skew_s": round(statistics.mean(skews), 6)})

    return {
        "comparator": COMPARATOR_VERSION, "gate": GATE_NAME, "gate_version": GATE_VERSION,
        "rounds": len(by_round), "fixtures": dict(_mapping(first["fixtures"], "fixtures")),
        "seed_sha256": _mapping(first["seed"], "seed")["sha256"],
        "config": dict(_mapping(first["config"], "config")), "arms": arm_metadata,
        "artifact_identity": {
            "same_distribution_version": same_version,
            "same_native_extension_bytes": same_native_bytes,
            "warning": artifact_warning,
        },
        "ordering_signatures": {
            "baseline_only": partition(baseline_keys - candidate_keys),
            "candidate_only": partition(candidate_keys - baseline_keys),
            "shared": partition(baseline_keys & candidate_keys, shared=True)},
        "paired_runs": paired_runs, "metric_ranges": metric_ranges,
        "paired_metrics": paired_metrics,
    }


def _run_gate(python_executable: str, gate_path: Path, db_path: Path,
              config: GateConfig) -> Mapping[str, Any]:
    command = [python_executable, str(gate_path), "--db", str(db_path),
               "--repeats", str(config.metric_repeats),
               "--determinism-runs", str(config.stability_runs),
               "--drift-probe-seconds", str(config.drift_probe_seconds)]
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        _refuse(f"gate under {python_executable!r} did not emit one JSON object: {error}")
    return _mapping(report, f"gate output from {python_executable}")


def _assert_seed(path: Path, expected: str, moment: str, hasher: Hasher) -> None:
    actual = hasher(path)
    if actual != expected:
        _refuse(f"seed changed {moment}: expected fingerprint {expected}, got {actual}")


def run_comparison(*, baseline_python: str, candidate_python: str, db_path: Path,
                   gate_path: Path, rounds: int, config: GateConfig,
                   runner: Runner = _run_gate,
                   hasher: Hasher = _seed_fingerprint) -> dict[str, Any]:
    if rounds < 2:
        _refuse("--rounds must be at least 2")
    if not db_path.is_file():
        _refuse(f"--db must name an existing preseeded file: {db_path}")
    if config.metric_repeats < 1 or config.stability_runs < 2:
        _refuse("repeats must be positive and determinism-runs must be at least 2")
    if config.drift_probe_seconds < 0:
        _refuse("drift-probe-seconds must not be negative")

    fingerprint = hasher(db_path)
    executables = {"baseline": baseline_python, "candidate": candidate_python}
    runs, process_orders = [], []
    for round_index in range(rounds):
        round_number = round_index + 1
        order = ["baseline", "candidate"] if round_index % 2 == 0 else ["candidate", "baseline"]
        process_orders.append(order)
        for position, arm in enumerate(order):
            _assert_seed(db_path, fingerprint, f"before round {round_number} {arm}", hasher)
            report = runner(executables[arm], gate_path, db_path, config)
            _assert_seed(db_path, fingerprint, f"after round {round_number} {arm}", hasher)
            report_seed = _mapping(report.get("seed"), f"{arm} round {round_number}.seed")
            if report_seed.get("sha256") != fingerprint:
                _refuse(
                    f"{arm} round {round_number} reported seed SHA "
                    f"{report_seed.get('sha256')!r}, but the pre-launch bytes hash to {fingerprint}"
                )
            runs.append(ArmRun(arm, round_number, report))
    _assert_seed(db_path, fingerprint, "after all processes", hasher)
    result = compare_reports(runs, expected_config=config, process_orders=process_orders)
    result["arms"]["baseline"]["python_executable"] = baseline_python
    result["arms"]["candidate"]["python_executable"] = candidate_python
    result["local_seed_fingerprint_sha256"] = fingerprint
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-python", required=True)
    parser.add_argument("--candidate-python", required=True)
    parser.add_argument("--db", type=Path, required=True, help="preseeded gate database")
    parser.add_argument("--gate", type=Path, default=Path(__file__).with_name("gate_4k.py"))
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--determinism-runs", type=int, default=6)
    parser.add_argument("--drift-probe-seconds", type=int, default=8)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = GateConfig(args.repeats, args.determinism_runs, args.drift_probe_seconds)
    try:
        result = run_comparison(
            baseline_python=args.baseline_python, candidate_python=args.candidate_python,
            db_path=args.db.resolve(), gate_path=args.gate.resolve(), rounds=args.rounds,
            config=config)
    except ComparisonRefusal as error:
        print(f"comparison refused: {error}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()
        print(f"comparison refused: {error}", file=sys.stderr)
        if detail:
            print(detail, file=sys.stderr)
        return 2
    except OSError as error:
        print(f"comparison refused: could not launch gate process: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
