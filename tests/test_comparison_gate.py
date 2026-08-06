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
