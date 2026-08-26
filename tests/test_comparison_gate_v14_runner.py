"""Fast contract tests for the v1.4 comparison-gate evidence envelope."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

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
    assert gate.GATE_VERSION == "1.5.0"
    assert set(gate.FIXTURE_HASHES) == {
        "corpus_sha256",
        "queries_sha256",
        "probes_sha256",
    }
    facts, queries, probes = gate._load()
    assert facts and queries and probes


def test_seed_snapshot_hashes_the_checkpointed_main_file_and_detects_mutation(tmp_path):
    gate = _runner_module()
    base = tmp_path / "gate.db"
    con = sqlite3.connect(base)
    con.execute("CREATE TABLE memories (created_at REAL)")
    con.executemany("INSERT INTO memories VALUES (?)", [(1.0,), (2.0,)])
    con.commit()
    con.close()
    before = gate._seed_snapshot(base)
    assert before["bytes"] == base.stat().st_size
    assert before["record_count"] == 2
    assert before["created_at_min"] == 1.0
    assert before["created_at_max"] == 2.0
    assert [f["name"] for f in before["files"]] == ["gate.db"]

    con = sqlite3.connect(base)
    con.execute("INSERT INTO memories VALUES (3.0)")
    con.commit()
    con.close()
    after = gate._seed_snapshot(base)
    assert after["sha256"] != before["sha256"]


def test_seed_snapshot_refuses_authoritative_wal_bytes(tmp_path):
    gate = _runner_module()
    base = tmp_path / "gate.db"
    con = sqlite3.connect(base)
    assert con.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    con.execute("CREATE TABLE memories (created_at REAL)")
    con.execute("INSERT INTO memories VALUES (1.0)")
    con.commit()
    wal = Path(f"{base}-wal")
    assert wal.stat().st_size > 0
    with pytest.raises(SystemExit, match="checkpoint it or use VACUUM INTO"):
        gate._seed_snapshot(base)
    con.close()


def test_self_seed_checkpoint_retries_busy_and_requires_success(monkeypatch, tmp_path):
    gate = _runner_module()
    rows = iter([(1, 3, 2), (0, 0, 0)])
    sleeps = []

    class Connection:
        def __init__(self, row):
            self.row = row

        def execute(self, statement):
            assert statement == "PRAGMA wal_checkpoint(TRUNCATE)"
            return self

        def fetchone(self):
            return self.row

        def close(self):
            pass

    monkeypatch.setattr(gate.sqlite3, "connect", lambda _path: Connection(next(rows)))
    monkeypatch.setattr(gate.time, "sleep", sleeps.append)
    gate._checkpoint_seed(tmp_path / "gate.db", retries=2)
    assert sleeps == [0.05]


def test_self_seed_checkpoint_refuses_permanent_busy(monkeypatch, tmp_path):
    gate = _runner_module()

    class BusyConnection:
        def execute(self, _statement):
            return self

        def fetchone(self):
            return (1, 3, 0)

        def close(self):
            pass

    monkeypatch.setattr(gate.sqlite3, "connect", lambda _path: BusyConnection())
    monkeypatch.setattr(gate.time, "sleep", lambda _seconds: None)
    with pytest.raises(RuntimeError, match="SQLite busy=1"):
        gate._checkpoint_seed(tmp_path / "gate.db", retries=2)


def test_import_metadata_must_describe_the_imported_module(monkeypatch, tmp_path):
    gate = _runner_module()
    imported = tmp_path / "imported" / "yantrikdb"
    imported.mkdir(parents=True)
    module_file = imported / "__init__.py"
    extension_file = imported / "_yantrikdb_rust.pyd"
    module_file.write_text("", encoding="utf-8")
    extension_file.write_bytes(b"native")

    module = ModuleType("yantrikdb")
    module.__file__ = str(module_file)
    module._yantrikdb_rust = SimpleNamespace(__file__=str(extension_file))
    monkeypatch.setitem(sys.modules, "yantrikdb", module)

    class WrongDistribution:
        version = "0.17.1"

        def read_text(self, _name):
            return None

        def locate_file(self, _name):
            return tmp_path / "different-install"

    monkeypatch.setattr(
        gate.importlib_metadata, "distribution", lambda _name: WrongDistribution())
    with pytest.raises(SystemExit, match="imported yantrikdb module is outside"):
        gate._import_metadata(module)


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


def test_stability_identity_includes_rids_not_only_duplicate_text():
    gate = _runner_module()
    observations = [
        {"ordering_rids": ["r1"], "ordering_texts": ["same text"],
         "observed_at_unix": 1.0},
        {"ordering_rids": ["r2"], "ordering_texts": ["same text"],
         "observed_at_unix": 2.0},
    ]
    grouped = gate._group_stability_observations(observations)
    assert len(grouped) == 2
    assert {tuple(row["ordering_rids"]) for row in grouped} == {("r1",), ("r2",)}


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
