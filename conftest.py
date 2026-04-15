"""Workspace-level conftest.

Pytest discovers tests under ``tests/`` but the workspace root is itself a
Python package (it contains ``__init__.py`` because the root IS the plugin
under test). When pytest imports the root package during collection it
hits ``from agent.memory_provider import MemoryProvider`` — which only
exists inside Hermes.

Installing the Hermes stubs here, at pytest's earliest import point,
lets the root ``__init__.py`` import cleanly. The real test-time fixtures
and plugin loader live in ``tests/conftest.py``.
"""

from __future__ import annotations

import json
import sys
import types
from abc import ABC, abstractmethod
from typing import Any, Dict, List


def _install_hermes_stubs() -> None:
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
        def get_tool_schemas(self) -> List[Dict[str, Any]]: ...

        def handle_tool_call(
            self, tool_name: str, args: Dict[str, Any], **kwargs: Any,
        ) -> str:
            raise NotImplementedError

        def shutdown(self) -> None:
            return None

        def on_turn_start(self, turn_number: int, message: str, **kwargs: Any) -> None:
            return None

        def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
            return None

        def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
            return ""

        def on_delegation(
            self, task: str, result: str, *, child_session_id: str = "", **kwargs: Any,
        ) -> None:
            return None

        def get_config_schema(self) -> List[Dict[str, Any]]:
            return []

        def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
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


_install_hermes_stubs()
