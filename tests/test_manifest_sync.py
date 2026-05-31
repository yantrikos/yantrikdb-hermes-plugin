"""The version field must agree across all four surfaces.

Belt-and-suspenders with the CI `version-sync` job: if a contributor runs
`pytest` locally before pushing, this test catches the drift before CI.

The four surfaces and what reads each:

- ``pyproject.toml``         — PyPI artifact metadata; pip / dependency
  resolvers read this.
- ``yantrikdb/plugin.yaml``  — bundled inside the wheel; the
  ``yantrikdb-hermes install <hermes>`` CLI copies this into the user's
  Hermes plugins dir, so it's what ``hermes plugins list`` reports for
  pip-installed users.
- ``plugin.yaml`` (root)     — read directly by Hermes when a user runs
  ``hermes plugins install yantrikos/yantrikdb-hermes-plugin`` against
  the GitHub repo (no pip involved). Drifted from 0.4.12 → 0.4.17 across
  five releases before community member [@wysie] caught it in PR #26 via
  a dashboard mismatch warning.
- ``yantrikdb/CHANGELOG.md`` — first ``## [X.Y.Z]`` header. The release
  notes the project's `gh release create` step references.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _read_pyproject_version() -> str:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version = "([^"]+)"', text, re.MULTILINE)
    assert m, "pyproject.toml missing [project] version line"
    return m.group(1)


def _read_yaml_version(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    m = re.search(r"^version:\s*([0-9.]+)", text, re.MULTILINE)
    assert m, f"{path} missing top-level 'version:' line"
    return m.group(1)


def _read_changelog_head_version() -> str:
    text = (REPO_ROOT / "yantrikdb" / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(r"^## \[([0-9.]+)\]", text, re.MULTILINE)
    assert m, "CHANGELOG.md missing first '## [X.Y.Z]' header"
    return m.group(1)


def test_all_four_version_surfaces_agree():
    pyproject = _read_pyproject_version()
    plugin_yaml_pkg = _read_yaml_version(REPO_ROOT / "yantrikdb" / "plugin.yaml")
    plugin_yaml_root = _read_yaml_version(REPO_ROOT / "plugin.yaml")
    changelog = _read_changelog_head_version()

    surfaces = {
        "pyproject.toml": pyproject,
        "yantrikdb/plugin.yaml": plugin_yaml_pkg,
        "plugin.yaml (root)": plugin_yaml_root,
        "CHANGELOG.md head": changelog,
    }
    distinct = set(surfaces.values())
    assert len(distinct) == 1, (
        f"Version drift across surfaces: {surfaces!r}. "
        "All four must agree or `hermes plugins list` and PyPI display "
        "the wrong version on at least one install path. Use "
        "`bump-my-version bump patch` to update all four together."
    )
