"""Fast contract tests for the v1.4 comparison-gate evidence envelope."""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_RUNNER = _ROOT / "tests" / "comparison" / "gate_4k.py"


def _runner_module():
    spec = importlib.util.spec_from_file_location("gate_4k_v14", _RUNNER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_v14_pins_and_reports_all_three_fixture_hashes():
    gate = _runner_module()
    assert gate.GATE_NAME == "hermes-plugin/competing-distractors"
    assert gate.GATE_VERSION == "1.4.0"
    assert set(gate.FIXTURE_HASHES) == {
        "corpus_sha256",
        "queries_sha256",
        "probes_sha256",
    }
    facts, queries, probes = gate._load()
    assert facts and queries and probes


def test_seed_snapshot_covers_every_sidecar_and_detects_mutation(tmp_path):
    gate = _runner_module()
    base = tmp_path / "gate.db"
    con = sqlite3.connect(base)
    con.execute("CREATE TABLE memories (created_at REAL)")
    con.executemany("INSERT INTO memories VALUES (?)", [(1.0,), (2.0,)])
    con.commit()
    con.close()
    (tmp_path / "gate.db-extra").write_bytes(b"aux")

    before = gate._seed_snapshot(base)
    assert before["bytes"] == base.stat().st_size + 3
    assert before["record_count"] == 2
    assert before["created_at_min"] == 1.0
    assert before["created_at_max"] == 2.0
    assert {f["name"] for f in before["files"]} == {"gate.db", "gate.db-extra"}

    (tmp_path / "gate.db-extra").write_bytes(b"changed")
    after = gate._seed_snapshot(base)
    assert after["sha256"] != before["sha256"]


def test_metric_observation_carries_raw_ordering_and_seed_age():
    gate = _runner_module()

    def metric(_db, _unique, _ambiguous, _probes, trace):
        trace.extend([
            {
                "query": "q",
                "top_k": 5,
                "count": 2,
                "ordering_signature": "a" * 64,
                "observed_at_unix": 1,
            },
            {
                "query": "q2",
                "top_k": 3,
                "count": 1,
                "ordering_signature": "b" * 64,
                "observed_at_unix": 2,
            },
        ])
        return 0.5

    value, observation = gate._metric_observation(metric, None, [], [], [], 0)
    assert value == 0.5
    assert observation["query_count"] == 2
    assert observation["result_count"] == 3
    assert len(observation["ordering_signature"]) == 64
    assert observation["observed_at_unix"] <= observation["finished_at_unix"]
    assert observation["seed_age_start_s"] <= observation["seed_age_end_s"]


def test_metric_report_contains_the_canonical_v14_fields():
    gate = _runner_module()

    class FakeDB:
        def recall_text(self, _query, top_k, namespace):
            assert namespace == "g"
            return [{"rid": "r1", "text": "Taylor reports to Carol."}][:top_k]

    unique = [{"query": "Taylor", "target_text": "Taylor reports to Carol."}]
    probes = [{
        "entity": "Taylor",
        "query_possessive": "What is Taylor's role?",
        "query_plain": "What is Taylor s role?",
        "query_subject": "Taylor as subject",
        "query_object": "Taylor as object",
    }]
    report = gate._measure_transposed(
        [FakeDB(), FakeDB()], unique, [], probes,
        drift_probe_seconds=0, seed_mtime_ns=0)
    for metric in report.values():
        assert len(metric["values"]) == 2
        assert set(metric["window"]) == {"t_start_unix", "t_end_unix", "seed_age_s"}
        assert {"before", "after", "moved", "seconds"} <= set(metric["drift_probe"])
        assert len(metric["raw_observations"]) == 2
        assert all(len(row["ordering_signature"]) == 64
                   for row in metric["raw_observations"])


def test_runner_exposes_reproducibility_knobs_and_drops_stale_v13_advice():
    src = _RUNNER.read_text(encoding="utf-8")
    assert "--repeats" in src
    assert "--stability-runs" in src
    assert "--determinism-runs" in src
    assert "--drift-probe-seconds" in src
    assert "raw_observations" in src
    assert "seed_age_start_s" in src
    assert '"gate_version": GATE_VERSION' in src
    assert '"config": {' in src
    assert '"stability": determinism' in src
    assert '"module_sha256"' in src
    assert '"extension_sha256"' in src
    assert '"executable"' in src
    assert "drift_sensitive=false" not in src
    assert "determinism_burst" not in src
