"""The gap threshold is a calibration, and calibrations expire silently.

`gap_max_avg_top_score` decides which queries the self-directing loop turns
into tasks — and it is an ABSOLUTE score, so it only means what it meant while
the engine composes scores the same way.

Engine 0.12.1 changed exactly that: recency used to ADD up to +0.5 to every
fresh record and now MULTIPLIES relevance, bounded to +12.5%. Retrieval got
*better* (benchmark MRR 0.928 -> 0.946) while absolute scores roughly halved
(avg-top3 median 1.138 -> 0.510 on the same corpus). Our 0.5 default went from
flagging 0/37 benchmark queries as gaps to flagging 17/37 — so the loop, on by
default since v0.10, would have minted tasks for questions the memory answers
correctly at rank 1.

Nothing crashed. No test failed. The whole suite stayed green, because every
test asserted behaviour and none asserted calibration.

This measures the default against a corpus of questions the memory demonstrably
CAN answer. If a future scoring change makes those look like gaps again, this
fails instead of quietly filling somebody's agenda with work that is already
done.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_DATASET = _ROOT / "benchmarks" / "dataset.json"


def _engine():
    """The real engine, or skip — this cannot be mocked.

    A mock returns whatever scores we tell it to, which would make this test
    assert our own assumption rather than the engine's behaviour.
    """
    sys.path.insert(0, str(_ROOT / "benchmarks"))
    try:
        from _bootstrap import _pin_engine_import
        _pin_engine_import()
        from yantrikdb._yantrikdb_rust import YantrikDB
    except Exception:  # noqa: BLE001 - no native wheel in this environment
        pytest.skip("native yantrikdb engine not installed")
    return YantrikDB


@pytest.fixture(scope="module")
def answered_scores():
    """avg-top-3 score for every benchmark query, all of which are answerable."""
    if not _DATASET.exists():
        pytest.skip("benchmark dataset missing")
    engine = _engine()
    data = json.loads(_DATASET.read_text(encoding="utf-8"))
    db = engine.with_default(os.path.join(tempfile.mkdtemp(), "calib.db"))
    for m in data["corpus"]:
        db.record_text(m["text"], memory_type="semantic", namespace="calib",
                       importance=m.get("importance", 0.6))
    out = []
    for q in data["queries"]:
        hits = db.recall_text(q["q"], top_k=5, namespace="calib")
        if hits:
            out.append(statistics.mean([(h.get("score") or 0.0) for h in hits[:3]]))
    if not out:
        pytest.skip("engine returned no hits for the benchmark corpus")
    return out


def _default_threshold(client_module) -> float:
    return client_module.YantrikDBConfig().gap_max_avg_top_score


class TestGapThresholdCalibration:
    def test_answerable_questions_are_not_called_gaps(
        self, answered_scores, client_module,
    ):
        """Every benchmark query has a gold answer in the corpus. If the
        default calls them gaps, the loop invents work that is already done —
        and the user sees an agenda full of questions their memory can answer.
        """
        threshold = _default_threshold(client_module)
        flagged = [s for s in answered_scores if s <= threshold]
        ratio = len(flagged) / len(answered_scores)
        assert ratio <= 0.10, (
            f"gap_max_avg_top_score={threshold} flags {len(flagged)}/"
            f"{len(answered_scores)} ANSWERABLE benchmark queries as knowledge "
            f"gaps (avg-top3 median {statistics.median(answered_scores):.3f}). "
            "The engine's score composition has probably changed — recalibrate "
            "the default against the current scale rather than shipping a loop "
            "that mints tasks for questions the memory answers correctly."
        )

    def test_threshold_still_has_headroom_below_answered_scores(
        self, answered_scores, client_module,
    ):
        """A threshold that sits just under the answered distribution will trip
        on the first corpus that is slightly harder than the benchmark. Keep
        real distance, not a hairline pass."""
        threshold = _default_threshold(client_module)
        worst_answered = min(answered_scores)
        assert threshold < worst_answered, (
            f"threshold {threshold} is at or above the WORST answered query "
            f"({worst_answered:.3f}) — no headroom at all"
        )
