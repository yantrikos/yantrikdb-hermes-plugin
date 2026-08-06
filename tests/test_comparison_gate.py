"""The comparison gate is a cross-team contract, so CI defends its hashes.

`tests/comparison/gate_4k.py` is the benchmark the engine team runs candidate
builds against — they cannot reproduce it themselves, and both sides quote its
numbers at each other. That only works if "gate v1.1.0" means the same bytes on
both sides. A regenerated corpus that still *looked* right would silently make
every historical number incomparable, and nobody would get an error.

So the hashes are pinned here as well as inside the runner: the runner refuses
to execute on a mismatch, and CI refuses to merge one.

These tests deliberately do NOT run the gate — it needs a seeded engine and
~30s of drain. They check that the contract is intact and the runner is honest
about its own limits.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_FIX = _ROOT / "tests" / "comparison" / "fixtures"

# Sent to yantrikdb-core on 2026-08-06 as gate v1.1.0. Changing a value here
# without telling them makes their measurements silently incomparable to ours.
PINNED = {
    "corpus_4353_gate_v1.json":
        "2d2d039094644ce5b2a1d8de5047daa5b4a183e98919bdae3a57a67962df4fc9",
    "queries_1k.json":
        "a4b866a5bdeccfb103b25d2cf1a66a1ca50af9096ccb55bf73579f676f4b407f",
    "direction_probes_v1_1.json":
        "dfe0242e72fa575c5d45bf7afc8e2e153dde279fc45be2b049da15f6e0383a3d",
}


@pytest.mark.parametrize("name", sorted(PINNED))
def test_fixture_hash_is_unchanged(name):
    path = _FIX / name
    assert path.exists(), f"gate fixture missing: {name}"
    got = hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    assert got == PINNED[name], (
        f"{name} changed.\n  expected {PINNED[name]}\n  actual   {got}\n"
        "If this is intentional, bump the gate version, re-pin here AND in "
        "gate_4k.py, and send the new hashes to the engine team — otherwise "
        "their numbers and ours stop meaning the same thing."
    )


def test_runner_pins_the_same_hashes():
    """Two copies of a constant drift. This is the test that notices."""
    src = (_ROOT / "tests" / "comparison" / "gate_4k.py").read_text(encoding="utf-8")
    assert PINNED["corpus_4353_gate_v1.json"] in src
    assert PINNED["queries_1k.json"] in src


def test_runner_refuses_to_run_on_a_mismatch():
    """Reporting numbers from a drifted corpus is worse than reporting none."""
    src = (_ROOT / "tests" / "comparison" / "gate_4k.py").read_text(encoding="utf-8")
    assert "hash mismatch" in src
    assert "SystemExit" in src


def test_direction_probes_are_minimal_pairs():
    """The possessive axis measures ONE apostrophe. An earlier cut compared
    differently-worded questions, which conflated the tokenizer defect with
    rewording and produced a number that did not mean what its field name
    claimed. This asserts the pair is byte-identical apart from that one
    character, so the metric cannot quietly go back to measuring rewording."""
    probes = json.loads(
        (_FIX / "direction_probes_v1_1.json").read_text(encoding="utf-8"))["probes"]
    assert probes
    for p in probes:
        assert p["query_possessive"].replace("'", " ") == p["query_plain"], (
            f"not a minimal pair for {p['entity']}: "
            f"{p['query_possessive']!r} vs {p['query_plain']!r}"
        )


def test_direction_probes_use_each_entity_in_both_roles():
    """Role-share scoring is only meaningful if the entity actually appears as
    both subject and object; otherwise 'direction separation' measures noise."""
    probes = json.loads(
        (_FIX / "direction_probes_v1_1.json").read_text(encoding="utf-8"))["probes"]
    for p in probes:
        assert p["n_subject_records"] >= 5
        assert p["n_object_records"] >= 5


def test_corpus_declares_that_it_is_a_stress_fixture():
    """Whoever reads a number off this gate must be told it is pathological by
    construction, or they will quote it as representative field precision."""
    payload = json.loads(
        (_FIX / "corpus_4353_gate_v1.json").read_text(encoding="utf-8"))
    assert len(payload["facts"]) == 4353
    note = payload["note"].lower()
    assert "stress" in note
    assert "not a representative" in note or "representative" in note


# --- v1.2.0: the gate must refuse to measure what it cannot measure ---------

def test_ambiguous_queries_are_not_scored_by_record_identity():
    """The defect that made this version necessary.

    The `What is X's role?` queries have nineteen valid answers each. Scoring
    them by whether one designated record came back caps precision at 5/19 by
    construction AND penalises a mechanism that correctly promotes a different
    valid answer — the gate reported a real improvement as a regression. The
    partition is what stops that recurring.
    """
    src = (_ROOT / "tests" / "comparison" / "gate_4k.py").read_text(encoding="utf-8")
    assert "_partition_queries" in src
    assert "precision_at_5_unique_answers_only" in src, (
        "the precision metric must NAME its restriction, so nobody quotes it "
        "as overall precision"
    )
    assert "role_share_ambiguous_queries" in src


def test_repeats_run_against_isolated_copies():
    """`recall_text` has no skip_reinforce, so every query mutates access_count
    and repeat N would otherwise measure a db repeats 1..N-1 modified."""
    src = (_ROOT / "tests" / "comparison" / "gate_4k.py").read_text(encoding="utf-8")
    assert "_fresh_copy" in src
    assert "shutil.copy2" in src


def test_determinism_is_checked_before_any_metric_is_believed():
    """A build that answers differently on identical bytes cannot be measured.
    The gate must say so rather than reporting means over the top."""
    src = (_ROOT / "tests" / "comparison" / "gate_4k.py").read_text(encoding="utf-8")
    assert "_determinism" in src
    assert "is unreliable" in src
    assert '"deterministic"' in src


def test_every_metric_publishes_measured_drift_sensitivity():
    """v1.3.0 replaces an assumed noise floor with a measured one.

    v1.2.0 published `noise_floor = 2*stdev` from a repeat loop that ran
    instance-by-instance over ~30 seconds — so the "noise" was mostly recency
    decay, and two conclusions were drawn from it that had to be retracted.
    v1.3.0 transposes the loop and, for each metric, reads it twice on ONE
    fixed instance across a deliberate delay: any movement there is the clock,
    because nothing else changed. Declared immunity is not accepted — a
    rank-based metric is drift-immune only where the ranks it compares are
    separated, and this gate caught itself assuming otherwise.
    """
    src = (_ROOT / "tests" / "comparison" / "gate_4k.py").read_text(encoding="utf-8")
    assert "measured_drift_move" in src
    assert "drift_probe_seconds" in src
    assert "_measure_transposed" in src
    assert "burst_span_s" in src, "each metric must publish its own window"


def test_drift_sensitivity_is_not_hardcoded():
    """The shortcut this gate must never take again: labelling metrics
    drift-immune by category instead of measuring them on the corpus at hand."""
    src = (_ROOT / "tests" / "comparison" / "gate_4k.py").read_text(encoding="utf-8")
    block = src.split("METRICS = {")[1].split("}")[0]
    assert "True" not in block and "False" not in block, (
        "METRICS must map name -> fn only; a hardcoded drift flag is the "
        "assumption this version exists to remove"
    )
