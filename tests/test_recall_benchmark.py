"""Regression guard for recall quality + self-tuning lift.

Runs the real recall benchmark against an embedded YantrikDB and asserts a
conservative floor so a ranking regression (or a broken self-tuning re-rank)
fails CI. Skips cleanly when the native engine wheel isn't installed — CI's
unit lane mocks the engine, so this only runs where a real ``yantrikdb``
wheel is present (local dev, the e2e lane).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

# The benchmark needs the REAL native engine, not the mocked one the rest of
# the suite uses. Skip when it's absent or when `import yantrikdb` would
# resolve to the plugin dir (no native submodule) instead of the wheel.
_REPO = Path(__file__).resolve().parent.parent
_BENCH = _REPO / "benchmarks"


def _engine_available() -> bool:
    # Look for the installed wheel's native submodule on the import path,
    # ignoring the plugin dir of the same name at the repo root.
    saved = list(sys.path)
    try:
        for entry in ("", str(_REPO)):
            while entry in sys.path:
                sys.path.remove(entry)
        return importlib.util.find_spec("yantrikdb._yantrikdb_rust") is not None
    except (ImportError, ValueError):
        return False
    finally:
        sys.path[:] = saved


pytestmark = pytest.mark.skipif(
    not _engine_available(),
    reason="native yantrikdb engine wheel not installed",
)


@pytest.fixture(scope="module")
def bench():
    sys.path.insert(0, str(_BENCH))
    import _bootstrap  # noqa: E402
    import run_recall_bench as runner  # noqa: E402

    dataset = json.loads((_BENCH / "dataset.json").read_text(encoding="utf-8"))
    return _bootstrap, runner, dataset


def test_baseline_recall_floor(bench):
    _bootstrap, runner, dataset = bench
    provider = _bootstrap.make_provider(
        env={"YANTRIKDB_SELF_TUNING_RECALL": "false"},
    )
    id_to_rid = runner._ingest(provider, dataset["corpus"])
    assert len(id_to_rid) == len(dataset["corpus"])  # every memory stored
    report = runner.evaluate(provider, dataset, id_to_rid)

    # Conservative floors — current run is recall@3≈1.0, MRR≈0.93. A drop
    # below these means a real ranking regression, not noise.
    assert report["recall_at_k"][3] >= 0.85
    assert report["recall_at_k"][5] >= 0.90
    assert report["mrr"] >= 0.75


def test_self_tuning_lift_is_nonnegative(bench):
    _bootstrap, runner, dataset = bench
    provider = _bootstrap.make_provider(
        env={"YANTRIKDB_SELF_TUNING_RECALL": "true"},
    )
    id_to_rid = runner._ingest(provider, dataset["corpus"])
    before = runner.evaluate(provider, dataset, id_to_rid, reinforce_after=True)
    after = runner.evaluate(provider, dataset, id_to_rid)

    # Reinforcing the gold memory of each query must never HURT ranking, and
    # should not lower recall@1. (We assert non-negativity rather than a
    # fixed lift so the test is robust to embedder/engine version drift.)
    assert after["mrr"] >= before["mrr"] - 1e-9
    assert after["recall_at_k"][1] >= before["recall_at_k"][1] - 1e-9
