"""Pytest fixtures + module shims.

When running tests in isolation (before the plugin lands inside Hermes),
we stub out the two Hermes-provided imports and load the plugin files as
a package, mirroring how Hermes' plugins_memory/__init__.py does it.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parent.parent / "yantrikdb"
_PKG = "yantrikdb_plugin_under_test"


def _ensure_hermes_stubs() -> None:
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
            self, user_content: str, assistant_content: str, *, session_id: str = "",
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

        def on_turn_start(self, turn_number: int, message: str, **kwargs: Any) -> None:
            return None

        def on_session_end(self, messages: list[dict[str, Any]]) -> None:
            return None

        def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:
            return ""

        def on_delegation(
            self, task: str, result: str, *, child_session_id: str = "", **kwargs: Any,
        ) -> None:
            return None

        def get_config_schema(self) -> list[dict[str, Any]]:
            return []

        def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
            return None

        def on_memory_write(self, action: str, target: str, content: str) -> None:
            return None

    mp_mod.MemoryProvider = MemoryProvider
    sys.modules["agent.memory_provider"] = mp_mod

    sys.modules["tools"] = types.ModuleType("tools")
    registry_mod = types.ModuleType("tools.registry")

    def tool_error(message: str) -> str:
        return json.dumps({"error": message})

    registry_mod.tool_error = tool_error
    sys.modules["tools.registry"] = registry_mod


def _load_plugin() -> tuple[types.ModuleType, types.ModuleType]:
    _ensure_hermes_stubs()

    if _PKG in sys.modules and hasattr(sys.modules[_PKG], "YantrikDBMemoryProvider"):
        return sys.modules[_PKG], sys.modules[f"{_PKG}.client"]

    pkg_mod = types.ModuleType(_PKG)
    pkg_mod.__path__ = [str(_ROOT)]
    sys.modules[_PKG] = pkg_mod

    client_spec = importlib.util.spec_from_file_location(
        f"{_PKG}.client", str(_ROOT / "client.py"),
    )
    assert client_spec and client_spec.loader
    client_mod = importlib.util.module_from_spec(client_spec)
    sys.modules[f"{_PKG}.client"] = client_mod
    client_spec.loader.exec_module(client_mod)

    init_spec = importlib.util.spec_from_file_location(
        _PKG, str(_ROOT / "__init__.py"),
        submodule_search_locations=[str(_ROOT)],
    )
    assert init_spec and init_spec.loader
    provider_mod = importlib.util.module_from_spec(init_spec)
    sys.modules[_PKG] = provider_mod
    init_spec.loader.exec_module(provider_mod)

    return provider_mod, client_mod


@pytest.fixture(scope="session")
def plugin() -> tuple[types.ModuleType, types.ModuleType]:
    return _load_plugin()


@pytest.fixture
def provider_module(plugin):
    return plugin[0]


@pytest.fixture
def client_module(plugin):
    return plugin[1]


@pytest.fixture(autouse=True)
def _clean_yantrikdb_env(monkeypatch):
    for var in (
        "YANTRIKDB_URL",
        "YANTRIKDB_TOKEN",
        "YANTRIKDB_NAMESPACE",
        "YANTRIKDB_TOP_K",
        "YANTRIKDB_READ_TIMEOUT",
        "YANTRIKDB_CONNECT_TIMEOUT",
        "YANTRIKDB_RETRY_TOTAL",
        "YANTRIKDB_MAX_TEXT_LEN",
        "YANTRIKDB_MODE",
        "YANTRIKDB_DB_PATH",
        "YANTRIKDB_EMBEDDER",
        "YANTRIKDB_SKILLS_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)
    yield
