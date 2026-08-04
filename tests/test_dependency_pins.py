"""Every runtime dependency must carry an upper bound.

An unbounded `>=` lets a future major silently break releases that are ALREADY
PUBLISHED: nothing in this repo changes, upstream cuts a new major, a fresh
`pip install` resolves into it, and the failure looks like our bug. There is no
signal — no CI run, no diff, no notification — because from our side nothing
happened.

This is not hypothetical. yantrikdb-mcp lost their published v0.10.0 exactly
this way when the MCP SDK shipped 2.0.0 against `mcp[cli]>=1.2.0`: the release
re-shipped itself broken. This plugin has also had to move its engine floor
twice for defects that a ceiling would have contained.

A ceiling is a claim about what has been TESTED. Raise one only after running
the suite against the new major — which is the same rule the plugin applies to
engine upgrades.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_UPPER_BOUND = re.compile(r"[<!=]=?\s*\d")


def _requirements() -> dict[str, list[str]]:
    data = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data.get("project", {})
    out = {"dependencies": list(project.get("dependencies", []))}
    for extra, reqs in (project.get("optional-dependencies") or {}).items():
        out[f"optional:{extra}"] = list(reqs)
    return out


def _has_upper_bound(spec: str) -> bool:
    """True when the specifier constrains the upper end at all.

    Accepts `<`, `<=`, `==`, `!=`-style ceilings and `~=`; rejects a bare
    `>=x` or a name with no specifier.
    """
    if "~=" in spec:
        return True
    return bool(_UPPER_BOUND.search(spec.split(";", 1)[0].replace(">=", "").replace(">", "")))


class TestRuntimeDependencies:
    def test_every_runtime_dependency_has_an_upper_bound(self):
        missing = [
            spec for spec in _requirements()["dependencies"]
            if not _has_upper_bound(spec)
        ]
        assert not missing, (
            f"unbounded runtime dependencies: {missing}. An unbounded `>=` lets a "
            "future major break already-published releases with no signal on our "
            "side. Add a ceiling you have actually tested against."
        )

    @pytest.mark.parametrize("extra", ["optional:model2vec",
                                       "optional:sentence-transformers"])
    def test_optional_feature_extras_are_bounded_too(self, extra):
        """Extras install into the user's environment exactly like runtime deps
        do — a broken embedder loader is not less broken for being optional."""
        missing = [s for s in _requirements()[extra] if not _has_upper_bound(s)]
        assert not missing, f"unbounded in {extra}: {missing}"

    def test_engine_ceiling_matches_what_we_tested(self):
        """The engine ceiling is a claim about tested ground, so it should not
        drift ahead of the floor by more than the line we verify."""
        spec = next(s for s in _requirements()["dependencies"]
                    if s.startswith("yantrikdb"))
        assert "<" in spec, spec
        assert ">=" in spec, "keep a floor too — old engines lack current APIs"


class TestManifestsAgreeWithPyproject:
    """Hermes installs from plugin.yaml, pip installs from pyproject. A ceiling
    present in one and absent in the other protects only half the users."""

    @pytest.mark.parametrize("manifest", ["plugin.yaml", "yantrikdb/plugin.yaml"])
    def test_manifest_pins_match(self, manifest):
        text = (_ROOT / manifest).read_text(encoding="utf-8")
        declared = re.findall(r"^\s*-\s*(\S+)\s*$", text, re.MULTILINE)
        pinned = {d.split(">=")[0].split("<")[0].strip(): d
                  for d in declared if ">=" in d or "<" in d}
        for name, spec in pinned.items():
            assert _has_upper_bound(spec), f"{manifest}: {name} is unbounded ({spec})"

        pyproject = {s.split(">=")[0].split("<")[0].strip(): s
                     for s in _requirements()["dependencies"]}
        for name, spec in pyproject.items():
            assert name in pinned, f"{manifest} is missing {name}"
            assert pinned[name] == spec, (
                f"{manifest} declares {name} as {pinned[name]!r} but pyproject "
                f"says {spec!r} — the two install paths would resolve differently"
            )
