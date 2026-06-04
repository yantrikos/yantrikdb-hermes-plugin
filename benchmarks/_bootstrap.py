"""Shared harness bootstrap for the recall benchmark.

Loads the plugin as a package (the plugin dir is named ``yantrikdb`` and
shadows the engine package of the same name, so it must be loaded under an
alias — mirrors ``tests/conftest.py``), stubs the two Hermes-provided
imports, and builds a real provider backed by an embedded YantrikDB in a
temp directory. No HTTP, no mocks — this measures the real recall path.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

_PLUGIN_DIR = Path(__file__).resolve().parent.parent / "yantrikdb"
_REPO_ROOT = str(_PLUGIN_DIR.parent)
_PKG = "yantrikdb_plugin_bench"


def _pin_engine_import() -> None:
    """Ensure ``import yantrikdb`` resolves to the installed engine wheel.

    The plugin directory is named ``yantrikdb`` and sits at the repo root.
    If the repo root (or cwd ``''``) is on ``sys.path`` ahead of
    site-packages, ``import yantrikdb`` inside ``embedded.py`` would resolve
    to the *plugin* dir — which has no ``_yantrikdb_rust`` native module —
    and embedded mode would wrongly report "requires yantrikdb >= 0.7.4".
    Pytest sidesteps this with ``--import-mode=importlib``; the standalone
    harness purges the shadowing entries and pins the real engine.
    """
    import os

    for entry in ("", os.getcwd(), _REPO_ROOT):
        while entry in sys.path:
            sys.path.remove(entry)
    shadow = sys.modules.get("yantrikdb")
    if shadow is not None:
        f = getattr(shadow, "__file__", "") or ""
        if str(Path(f).resolve()).startswith(_REPO_ROOT):
            del sys.modules["yantrikdb"]
    import yantrikdb  # noqa: F401  — resolves to the installed engine now


def _ensure_hermes_stubs() -> None:
    """Install minimal stubs for ``agent.memory_provider`` + ``tools.registry``.

    Mirrors ``tests/conftest._ensure_hermes_stubs`` — the plugin imports
    these from the host Hermes runtime, which isn't present standalone.
    """
    if "agent.memory_provider" in sys.modules:
        return

    sys.modules["agent"] = types.ModuleType("agent")
    mp_mod = types.ModuleType("agent.memory_provider")

    class MemoryProvider(ABC):
        @property
        @abstractmethod
        def name(self) -> str: ...

        @abstractmethod
        def is_available(self) -> bool: ...

        @abstractmethod
        def initialize(self, session_id: str, **kwargs: Any) -> None: ...

        def system_prompt_block(self) -> str:
            return ""

        def prefetch(self, query: str, *, session_id: str = "") -> str:
            return ""

        def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
            return None

        def sync_turn(
            self, user_content: str, assistant_content: str,
            *, session_id: str = "",
        ) -> None:
            return None

        @abstractmethod
        def get_tool_schemas(self) -> list[dict[str, Any]]: ...

        def handle_tool_call(
            self, tool_name: str, args: dict[str, Any], **kwargs: Any,
        ) -> str:
            raise NotImplementedError

        def shutdown(self) -> None:
            return None

        def on_session_end(self, messages: list[dict[str, Any]]) -> None:
            return None

        def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:
            return ""

        def on_memory_write(self, action: str, target: str, content: str) -> None:
            return None

        def get_config_schema(self) -> list[dict[str, Any]]:
            return []

        def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
            return None

    mp_mod.MemoryProvider = MemoryProvider
    sys.modules["agent.memory_provider"] = mp_mod

    sys.modules["tools"] = types.ModuleType("tools")
    registry_mod = types.ModuleType("tools.registry")

    def tool_error(message: str) -> str:
        return json.dumps({"error": message})

    registry_mod.tool_error = tool_error
    sys.modules["tools.registry"] = registry_mod


def load_plugin() -> types.ModuleType:
    """Load the plugin package under an alias and return its provider module."""
    _pin_engine_import()
    _ensure_hermes_stubs()
    if _PKG in sys.modules and hasattr(sys.modules[_PKG], "YantrikDBMemoryProvider"):
        return sys.modules[_PKG]

    pkg = types.ModuleType(_PKG)
    pkg.__path__ = [str(_PLUGIN_DIR)]
    sys.modules[_PKG] = pkg

    for sub in ("client", "embedders", "extractor"):
        path = _PLUGIN_DIR / f"{sub}.py"
        if not path.exists():
            continue
        spec = importlib.util.spec_from_file_location(f"{_PKG}.{sub}", str(path))
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"{_PKG}.{sub}"] = mod
        spec.loader.exec_module(mod)

    init_spec = importlib.util.spec_from_file_location(
        _PKG, str(_PLUGIN_DIR / "__init__.py"),
        submodule_search_locations=[str(_PLUGIN_DIR)],
    )
    assert init_spec and init_spec.loader
    provider_mod = importlib.util.module_from_spec(init_spec)
    sys.modules[_PKG] = provider_mod
    init_spec.loader.exec_module(provider_mod)
    return provider_mod


def make_provider(
    *, env: dict[str, str] | None = None, home: Path | None = None,
) -> Any:
    """Build an initialized provider backed by an embedded engine in a temp dir.

    ``env`` values are applied to ``os.environ`` for the lifetime of the
    process (the caller owns cleanup if it cares). Returns the provider.
    """
    import os

    provider_mod = load_plugin()
    if home is None:
        home = Path(tempfile.mkdtemp(prefix="ydb-bench-"))
    base_env = {
        "YANTRIKDB_MODE": "embedded",
        "YANTRIKDB_DB_PATH": str(home / "memory.db"),
        "YANTRIKDB_AUTO_THINK_ON_SESSION_END": "false",
        "YANTRIKDB_EXTRACTION_ENABLED": "false",
    }
    base_env.update(env or {})
    for k, v in base_env.items():
        os.environ[k] = v

    provider = provider_mod.YantrikDBMemoryProvider()
    provider.initialize(
        "bench-session",
        agent_workspace="recall-benchmark",
        agent_identity="harness",
        platform="cli",
        hermes_home=str(home),
    )
    return provider
