"""Fast, engine-free tests for the canonical gate v1.4 comparator."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_PATH = _ROOT / "tests" / "comparison" / "compare_gate_4k.py"
_SPEC = importlib.util.spec_from_file_location("compare_gate_4k_under_test", _PATH)
assert _SPEC and _SPEC.loader
comparator = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = comparator
_SPEC.loader.exec_module(comparator)


def _config():
    return comparator.GateConfig(3, 2, 0)


def _signature(name, timestamps):
    return {
        "ordering_rids": [f"{name}-rid"],
        "ordering_texts": [f"{name} text"],
        "count": len(timestamps),
        "observed_at_unix": timestamps,
    }


def _report(*, arm, seed_sha, signatures, timestamp_offset=0.0):
    config = _config()
    shifted = [{**signature, "observed_at_unix": [value + timestamp_offset
                                                   for value in signature["observed_at_unix"]]}
               for signature in signatures]
    return {
        "gate": comparator.GATE_NAME,
        "gate_version": comparator.GATE_VERSION,
        "engine": {
            "distribution_version": arm,
            "module_file": f"/{arm}/yantrikdb/__init__.py",
            "import_source": "wheel",
            "wheel_sha256": arm * 8,
            "module_sha256": arm * 16,
            "extension_file": f"/{arm}/yantrikdb/_yantrikdb_rust.so",
            "extension_sha256": arm * 32,
        },
        "host": {"platform": "test", "python": f"python-{arm}"},
        "fixtures": {
            "corpus_sha256": "c" * 64,
            "queries_sha256": "q" * 64,
            "probes_sha256": "p" * 64,
        },
        "seed": {
            "path": "/seed.db",
            "sha256": seed_sha,
            "bytes": 4,
            "mtime_unix": 900.0,
            "created_at_min": 1.0,
            "created_at_max": 2.0,
            "record_count": 4353,
        },
        "config": config.as_dict(),
        "observed": {
            "started_unix": 999.0 + timestamp_offset,
            "finished_unix": 1005.0 + timestamp_offset,
            "seed_age_s_at_start": 99.0 + timestamp_offset,
        },
        "metrics": {
            "precision": {
                "values": [0.1, 0.2, 0.3],
                "mean": 0.2,
                "stdev": 0.1,
                "spread": 0.2,
                "window": {"t_start_unix": 1000.0, "t_end_unix": 1001.0,
                           "seed_age_s": 100.0},
                "burst_span_s": 1.0,
                "drift_probe": {"before": 0.2, "after": 0.2, "moved": 0.0,
                                "seconds": 0.0},
            }
        },
        "stability": {
            "query": config.stability_query,
            "runs": config.stability_runs,
            "distinct": len(shifted),
            "signatures": shifted,
        },
        "lexical_degeneracy": {"term": "taylor"},
    }


def _paired(seed_sha):
    baseline = _report(
        arm="baseline", seed_sha=seed_sha,
        signatures=[_signature("shared", [1000.0]), _signature("base", [1001.0])])
    candidate = _report(
        arm="candidate", seed_sha=seed_sha,
        signatures=[_signature("shared", [1000.0]), _signature("candidate", [1001.0])],
        timestamp_offset=2.5)
    return [
        comparator.ArmRun("baseline", 1, baseline),
        comparator.ArmRun("candidate", 1, candidate),
        comparator.ArmRun("candidate", 2, copy.deepcopy(candidate)),
        comparator.ArmRun("baseline", 2, copy.deepcopy(baseline)),
    ]


def _compare(runs):
    return comparator.compare_reports(
        runs, expected_config=_config(),
        process_orders=(("baseline", "candidate"), ("candidate", "baseline")))


def test_reports_signature_counts_timestamp_skew_and_metric_ranges():
    result = _compare(_paired("a" * 64))
    signatures = result["ordering_signatures"]
    assert signatures["baseline_only"][0]["baseline_count"] == 2
    assert signatures["candidate_only"][0]["candidate_count"] == 2
    assert signatures["shared"][0]["baseline_count"] == 2
    assert signatures["shared"][0]["candidate_count"] == 2
    assert [pair["process_order"] for pair in result["paired_runs"]] == [
        ["baseline", "candidate"], ["candidate", "baseline"]]
    assert result["paired_runs"][0]["stability_timestamp_skew_s"] == [2.5, 2.5]
    assert result["metric_ranges"]["precision"] == {
        "baseline": {"count": 6, "min": 0.1, "max": 0.3,
                     "range": pytest.approx(0.2)},
        "candidate": {"count": 6, "min": 0.1, "max": 0.3,
                      "range": pytest.approx(0.2)},
    }


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda report: report.update(gate_version="1.3.0"), "gate version mismatch"),
        (lambda report: report["fixtures"].update(corpus_sha256="different"),
         "fixtures mismatch"),
        (lambda report: report["seed"].update(sha256="different"),
         "seed.sha256 mismatch"),
        (lambda report: report["config"].update(top_k=99), "config mismatch"),
    ],
)
def test_refuses_nonidentical_cross_arm_contract_fields(mutation, message):
    runs = _paired("a" * 64)
    mutation(runs[1].report)
    with pytest.raises(comparator.ComparisonRefusal, match=message):
        _compare(runs)


def test_refuses_engine_or_host_instability_within_an_arm():
    runs = _paired("a" * 64)
    runs[-1].report["engine"] = {**runs[-1].report["engine"],
                                  "module_file": "/changed/yantrikdb/__init__.py"}
    with pytest.raises(comparator.ComparisonRefusal,
                       match="baseline engine/host metadata changed"):
        _compare(runs)


def test_flags_same_version_with_different_native_bytes_without_refusing():
    runs = _paired("a" * 64)
    for run in runs:
        run.report["engine"]["distribution_version"] = "0.17.1"
    result = _compare(runs)
    assert result["artifact_identity"] == {
        "same_distribution_version": True,
        "same_native_extension_bytes": False,
        "warning": "SAME_VERSION_DIFFERENT_NATIVE_BYTES",
    }


def test_refuses_bad_signature_counts_and_metric_value_counts():
    runs = _paired("a" * 64)
    runs[0].report["stability"]["signatures"][0]["count"] = 2
    with pytest.raises(comparator.ComparisonRefusal, match="count does not match"):
        _compare(runs)

    runs = _paired("a" * 64)
    runs[0].report["metrics"]["precision"]["values"].pop()
    with pytest.raises(comparator.ComparisonRefusal, match="values count mismatch"):
        _compare(runs)


def test_orchestration_alternates_and_guards_the_same_seed(tmp_path):
    db = tmp_path / "seed.db"
    db.write_bytes(b"seed")
    seed_sha = comparator._seed_fingerprint(db)
    calls = []

    def runner(executable, gate_path, db_path, config):
        calls.append(executable)
        arm = "baseline" if executable == "base-python" else "candidate"
        return _report(arm=arm, seed_sha=seed_sha,
                       signatures=[_signature("one", [1000.0, 1001.0])])

    result = comparator.run_comparison(
        baseline_python="base-python", candidate_python="candidate-python",
        db_path=db, gate_path=tmp_path / "gate.py", rounds=2,
        config=_config(), runner=runner)
    assert calls == ["base-python", "candidate-python", "candidate-python", "base-python"]
    assert result["local_seed_fingerprint_sha256"]


def test_orchestration_refuses_seed_sidecar_mutation(tmp_path):
    db = tmp_path / "seed.db"
    db.write_bytes(b"seed")
    seed_sha = comparator._seed_fingerprint(db)

    def runner(executable, gate_path, db_path, config):
        Path(f"{db_path}-wal").write_bytes(b"new WAL")
        return _report(arm=executable, seed_sha=seed_sha,
                       signatures=[_signature("one", [1000.0, 1001.0])])

    with pytest.raises(comparator.ComparisonRefusal,
                       match="seed changed after round 1 baseline"):
        comparator.run_comparison(
            baseline_python="baseline", candidate_python="candidate", db_path=db,
            gate_path=tmp_path / "gate.py", rounds=2, config=_config(), runner=runner)


def test_orchestration_refuses_a_report_not_bound_to_the_prelaunch_seed(tmp_path):
    db = tmp_path / "seed.db"
    db.write_bytes(b"seed")

    def runner(executable, gate_path, db_path, config):
        return _report(arm=executable, seed_sha="0" * 64,
                       signatures=[_signature("one", [1000.0, 1001.0])])

    with pytest.raises(comparator.ComparisonRefusal, match="reported seed SHA"):
        comparator.run_comparison(
            baseline_python="baseline", candidate_python="candidate", db_path=db,
            gate_path=tmp_path / "gate.py", rounds=2, config=_config(), runner=runner)


def test_subprocess_command_forwards_config(monkeypatch, tmp_path):
    captured = {}

    def fake_run(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        return SimpleNamespace(stdout=json.dumps({"gate": comparator.GATE_NAME}))

    monkeypatch.setattr(comparator.subprocess, "run", fake_run)
    gate, db = tmp_path / "gate.py", tmp_path / "seed.db"
    comparator._run_gate("candidate-python", gate, db, _config())
    assert captured["command"] == [
        "candidate-python", str(gate), "--db", str(db), "--repeats", "3",
        "--determinism-runs", "2", "--drift-probe-seconds", "0"]
    assert captured["kwargs"] == {"check": True, "capture_output": True, "text": True}


def test_at_least_two_rounds_are_required(tmp_path):
    db = tmp_path / "seed.db"
    db.write_bytes(b"seed")
    with pytest.raises(comparator.ComparisonRefusal, match="at least 2"):
        comparator.run_comparison(
            baseline_python="baseline", candidate_python="candidate", db_path=db,
            gate_path=tmp_path / "gate.py", rounds=1, config=_config(),
            runner=lambda *args: {})
