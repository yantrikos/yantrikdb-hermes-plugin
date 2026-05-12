"""Adapter — instantiates *this* plugin's ``MemoryProvider`` for the probe.

Per-provider adapters are the only place where provider-specific config
lives. The probe sees only the ABC surface.

Run via::

    python -m tests.comparison.providers.yantrikdb.adapter

Produces ``findings.yaml`` next to this file plus a transcript and raw
response dump for evidence.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

# Make the parent test-comparison package importable when run as a script.
_THIS = Path(__file__).resolve()
_COMPARISON_DIR = _THIS.parent.parent.parent  # tests/comparison/
_REPO_ROOT = _COMPARISON_DIR.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "tests"))  # for the conftest stubs

# Bootstrap the Hermes stubs the same way the unit-test conftest does, so
# this adapter can run without a Hermes checkout.
from conftest import _ensure_hermes_stubs, _load_plugin  # type: ignore  # noqa: E402
from tests.comparison.probe import (  # noqa: E402
    FindingsRow,
    findings_to_yaml,
    probe_provider,
)


def make_provider():
    """Build a fully-initialised yantrikdb ``MemoryProvider`` in embedded mode."""
    _ensure_hermes_stubs()
    provider_mod, _client_mod = _load_plugin()

    # Embedded mode against a throwaway DB path. The bundled potion-2M
    # embedder is auto-attached by YantrikDB.with_default() — no env
    # config required.
    db_path = Path(tempfile.mkdtemp(prefix="yhp-comparison-")) / "mem.db"
    os.environ["YANTRIKDB_MODE"] = "embedded"
    os.environ["YANTRIKDB_DB_PATH"] = str(db_path)
    os.environ["YANTRIKDB_NAMESPACE"] = "comparison-probe"
    # No embedder env vars → default with_default() path (bundled potion-2M).

    # The plugin exposes its MemoryProvider via the standard register()
    # pattern. Use the same collector the test conftest uses.
    collector_cls = type(
        "_Collector", (),
        {
            "provider": None,
            "register_memory_provider": lambda self, p: setattr(self, "provider", p),
            "register_tool": lambda self, *a, **kw: None,
            "register_hook": lambda self, *a, **kw: None,
            "register_cli_command": lambda self, *a, **kw: None,
        },
    )
    collector = collector_cls()
    provider_mod.register(collector)
    if collector.provider is None:
        raise RuntimeError("yantrikdb plugin.register() did not register a provider")
    return collector.provider


def main() -> int:
    provider_dir = _THIS.parent
    transcript_out = provider_dir / "transcript.md"
    raw_out = provider_dir / "raw"
    findings_out = provider_dir / "findings.yaml"

    try:
        provider = make_provider()
    except Exception as e:
        # Couldn't even instantiate — emit a findings row with the explicit
        # reason. This is exactly the path cloud-only providers will take
        # when their accounts aren't available.
        row = FindingsRow(
            provider="yantrikdb",
            couldnt_verify_reason=f"instantiation failed: {e!r}",
        )
        findings_out.write_text(findings_to_yaml(row), encoding="utf-8")
        print(f"FAIL: {e!r}", file=sys.stderr)
        return 1

    row = probe_provider(
        provider, "yantrikdb",
        raw_responses_out=raw_out,
        transcript_out=transcript_out,
    )

    # Populate the metadata fields the probe doesn't know about.
    row.version_under_test = _resolve_plugin_version()
    row.verified_at = time.strftime("%Y-%m-%d", time.gmtime())
    row.verified_against = "local (Hermes stubs); embedded backend, bundled potion-2M"
    row.backend_hosting = "embedded"
    row.backend_requires_account = "false"
    row.backend_requires_separate_server = "false"

    findings_out.write_text(findings_to_yaml(row), encoding="utf-8")
    print(f"wrote {findings_out}")
    print(f"transcript: {transcript_out}")
    print(f"raw: {raw_out}")
    return 0


def _resolve_plugin_version() -> str:
    """Read version from pyproject.toml so findings record what we actually tested."""
    pyproj = _REPO_ROOT / "pyproject.toml"
    try:
        for line in pyproj.read_text(encoding="utf-8").splitlines():
            if line.startswith("version"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return "unknown"


if __name__ == "__main__":
    sys.exit(main())
