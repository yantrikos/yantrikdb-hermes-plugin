"""v0.15.0 — admit engine 0.13.x, and stop shipping a signal that can't tell.

Two changes, one theme: a number that doesn't disclose its own preconditions.

RAISING THE CEILING. The engine's 0.13.0 brought BM25 lexical fusion. Our pin
capped at <0.13.0, so nobody installing this plugin got it. The ceiling moves
only after the suite runs green against the new minor — that rule is the whole
reason `test_dependency_pins.py` exists, and this file records that it happened.

RETIRING THE GAP SIGNAL. `gap_max_avg_top_score` thresholds the COMPOSITE
recall score, which folds in importance, recency and decay — terms describing
how to weight a result once you've decided to return it, not whether memory
holds anything near the question. Measured on engine 0.13.0 over 4,353 records:
0/20 queries flagged, and 3 of 4 deliberate-nonsense queries evaded the
threshold ("zzqx wobble frangible" scored 0.4810). The detector isn't
conservative, it's uninformative — and an operator reads its silence as
"nothing missing".

v0.12.2 already recalibrated this value once. Recalibrating again would move
the same defect to a new constant and expire it at the next scoring change, so
the default goes off until the signal itself discriminates. The knob stays:
off-by-default is a default, not a removal.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def repo_root() -> Path:
    return _ROOT


@pytest.fixture
def mock_client(client_module) -> MagicMock:
    c = MagicMock(spec=client_module.YantrikDBClient)
    c.health.return_value = {"status": "ok"}
    c.knowledge_gaps.return_value = {"gaps": [{"query": "unanswered thing"}]}
    c.task_list.return_value = {"tasks": []}
    return c


def _provider(provider_module, mock_client, monkeypatch, **env):
    monkeypatch.setenv("YANTRIKDB_MODE", "http")
    monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_test")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    p = provider_module.YantrikDBMemoryProvider()
    with patch.object(provider_module, "make_backend", return_value=mock_client):
        p.initialize("s1", agent_workspace="ws", agent_identity="coder",
                     platform="cli")
    return p


def _engine_spec(repo_root):
    """The raw engine specifier string from pyproject."""
    text = (repo_root / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'"(yantrikdb>=[^"]+)"', text)
    assert m, "engine pin is missing or no longer parseable"
    return m.group(1)


def _engine_bounds(repo_root):
    """(floor, ceiling) of the engine pin as (major, minor, patch) tuples.

    Parsed rather than string-matched: this file asserts that 0.13.x stays
    admitted, which is a claim about the *range*. Pinning the literal ceiling
    made a later release fail a test whose own docstring says the bound moves.

    Tolerates `!=` exclusions between the bounds. A pin may need to admit a
    range while excluding named releases inside it — excluding a known-bad
    version is not the same as being unbounded, and the parser must not
    conflate them. See `_engine_exclusions` for the other half of the claim.
    """
    spec = _engine_spec(repo_root)
    floor = re.search(r">=([\d.]+)", spec)
    ceiling = re.search(r"<([\d.]+)", spec)
    assert floor and ceiling, f"engine pin is missing a bound: {spec}"

    def parts(v):
        return tuple(int(x) for x in v.split("."))

    return parts(floor.group(1)), parts(ceiling.group(1))


def _engine_exclusions(repo_root):
    """Versions the pin explicitly refuses, as (major, minor, patch) tuples."""
    return {
        tuple(int(x) for x in v.split("."))
        for v in re.findall(r"!=([\d.]+)", _engine_spec(repo_root))
    }


class TestEngineCeiling:
    def test_admits_0_13_x(self, repo_root):
        """The whole point of the release: users get BM25 fusion."""
        floor, ceiling = _engine_bounds(repo_root)
        assert floor <= (0, 13, 0), f"floor {floor} excludes 0.13.x"
        assert ceiling > (0, 13, 0), f"ceiling {ceiling} excludes 0.13.x"

    def test_known_bad_releases_stay_excluded(self, repo_root):
        """Engine 0.15.0-0.15.2 changed the score SCALE (priors moved into one
        bounded budget) without changing ranking, and shipped three ranking
        defects the engine fixed in 0.15.3. They sit inside the admitted range,
        so they are excluded by name. Measured, embedder held fixed at 64 dims:
        composite/similarity fell 1.0644 -> 0.7788 across that boundary while
        similarity itself was byte-identical.

        If a later release widens the range, these exclusions must survive or
        be replaced by a floor above them — dropping them silently re-admits
        the defects."""
        floor, ceiling = _engine_bounds(repo_root)
        excluded = _engine_exclusions(repo_root)
        for bad in [(0, 15, 0), (0, 15, 1), (0, 15, 2)]:
            if floor <= bad < ceiling:
                assert bad in excluded, (
                    f"{bad} is inside the admitted range {floor}..{ceiling} "
                    f"and is not excluded; it carries known ranking defects"
                )

    def test_still_bounded(self, repo_root):
        """Raising a ceiling is not removing it. An unbounded pin is how
        yantrikdb-mcp lost a published release when a dependency shipped a
        new major; the bound moves, it never disappears."""
        floor, ceiling = _engine_bounds(repo_root)
        assert floor == (0, 12, 1), f"engine floor moved to {floor} unremarked"
        text = (repo_root / "pyproject.toml").read_text(encoding="utf-8")
        for line in text.splitlines():
            s = line.strip()
            if s.startswith('"yantrikdb>=') or s.startswith('"requests>='):
                assert "<" in s, f"dependency lost its upper bound: {s}"


class TestGapSignalIsOffByDefault:
    def test_no_gap_call_at_session_end(
        self, provider_module, mock_client, monkeypatch,
    ):
        """Not merely 'creates no tasks' — the engine is never asked. A
        detector that cannot discriminate should not be consulted at all."""
        p = _provider(provider_module, mock_client, monkeypatch)
        p.on_session_end([])
        mock_client.knowledge_gaps.assert_not_called()
        mock_client.task_add.assert_not_called()

    def test_agenda_block_does_not_consult_gaps(
        self, provider_module, mock_client, monkeypatch,
    ):
        p = _provider(provider_module, mock_client, monkeypatch,
                      YANTRIKDB_SURFACE_AGENDA="true")
        p._format_agenda_block()
        mock_client.knowledge_gaps.assert_not_called()

    def test_agenda_still_shows_real_tasks(
        self, provider_module, mock_client, monkeypatch,
    ):
        """Retiring the gap signal must not take the agenda with it — open
        tasks are user-authored facts, not an inferred signal."""
        mock_client.task_list.return_value = {"tasks": [
            {"id": "t1", "title": "Ship the migration", "priority": "high"},
        ]}
        p = _provider(provider_module, mock_client, monkeypatch,
                      YANTRIKDB_SURFACE_AGENDA="true")
        block = p._format_agenda_block()
        assert "Ship the migration" in block

    def test_opt_in_restores_both_surfaces(
        self, provider_module, mock_client, monkeypatch,
    ):
        p = _provider(provider_module, mock_client, monkeypatch,
                      YANTRIKDB_GAP_DETECTION="true",
                      YANTRIKDB_SURFACE_AGENDA="true")
        p._format_agenda_block()
        mock_client.knowledge_gaps.assert_called()
        p.on_session_end([])
        mock_client.task_add.assert_called()


class TestTheOperatorIsToldWhy:
    def test_setup_surfaces_the_switch(self, provider_module):
        """A capability that silently stopped running is indistinguishable
        from one that is broken. It appears in `hermes memory setup`."""
        keys = {e["key"] for e in provider_module.YantrikDBMemoryProvider()
                .get_config_schema()}
        assert "gap_detection" in keys

    def test_description_warns_rather_than_just_naming(self, provider_module):
        """The description has to carry the risk, not the mechanism — an
        operator flipping this on deserves to know it may fill their agenda
        with work that isn't real."""
        entry = next(e for e in provider_module.YantrikDBMemoryProvider()
                     .get_config_schema() if e["key"] == "gap_detection")
        d = entry["description"].lower()
        assert entry["default"] == "false"
        assert "off" in d
        assert "gibberish" in d or "noise" in d

    def test_the_measurement_is_recorded_next_to_the_constant(self, repo_root):
        """The threshold that caused this must carry its own evidence. A bare
        'deprecated' comment would leave the next maintainer free to just
        retune it — which is precisely the mistake being corrected."""
        src = (repo_root / "yantrikdb" / "client.py").read_text(encoding="utf-8")
        i = src.index("gap_max_avg_top_score: float")
        context = src[max(0, i - 3000):i]
        assert "0 / 20" in context or "0/20" in context
        assert "nonsense" in context.lower()
        assert "gap_floor_check" in context


class TestTheDiagnosticShips:
    def test_gap_floor_check_is_present_and_runnable(self, repo_root):
        """Anyone who calibrated a threshold against the composite has the
        same latent bug. Shipping the check is how they find out."""
        p = repo_root / "benchmarks" / "gap_floor_check.py"
        assert p.exists()
        src = p.read_text(encoding="utf-8")
        compile(src, str(p), "exec")
        # it must refuse to repeat the derivation that was retracted
        assert "do NOT sum" in src or "does not sum" in src
        assert "read_this_before_switching_to_similarity" in src
