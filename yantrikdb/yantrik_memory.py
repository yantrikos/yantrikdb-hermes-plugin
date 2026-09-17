"""Yantrik mode: the machine's shared memory, reached through its memory server.

On a Yantrik machine one memory file is shared by whichever mind is active —
Yantrik Mind or Hermes — and exactly one process may hold it with a live
engine: a second engine on the same file does not see the first one's writes.
So this backend never opens the file. It talks MCP (streamable HTTP) to the
memory server, which is served by whoever owns the file at the moment: Yantrik
Mind while it runs, the standalone ``yantrik-memory`` service otherwise. Same
address, same token file, same tools either way.

What changes from the other modes, on purpose:

- **Recall covers the whole machine's memory.** The plugin's per-agent
  namespace is kept on writes, as a record of who wrote what, but not used to
  narrow recall — reading one memory together is the point of this mode.
  Owner scoping is refused for the same reason: it separates *people* inside
  one database, and a server that serves everything to the machine's owner
  cannot honour that.
- **Recall returns beliefs as well as memories.** Yantrik Mind keeps beliefs —
  statements held with a confidence and evidence behind them. They come back
  with ``memory_type`` ``"belief"`` and the confidence in ``metadata``.
- **Features the server does not offer are refused, not faked** —
  consolidation (``think``), stats, triggers, tasks, skills, knowledge gaps,
  record scans and packs. Each raises :class:`YantrikMemoryUnsupported`, a
  :class:`YantrikDBClientError`, which the provider treats as "not available"
  without counting it against the circuit breaker.
- **The conversation buffer is kept in this process.** The server has none; the
  last few turns are working memory for this agent, not shared memory.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import logging
import os
import threading
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any

import requests

from .client import (
    YantrikDBAuthError,
    YantrikDBClientError,
    YantrikDBConfig,
    YantrikDBError,
    YantrikDBServerError,
    YantrikDBTransientError,
    truncate_text,
)

logger = logging.getLogger(__name__)

DEFAULT_MEMORY_URL = "http://127.0.0.1:7440/mcp"
DEFAULT_TOKEN_FILE = "~/.local/share/yantrik-mind/yantrik-memory.token"
MCP_PROTOCOL_VERSION = "2025-06-18"
# Written into every record's source column, so the machine can tell which mind wrote what.
WRITER = "hermes"
# Beliefs are forgotten by statement; remember the statements behind recently recalled belief ids.
_BELIEF_CACHE_MAX = 512
_JSONRPC_INVALID_PARAMS = -32602

_PLUGIN_VERSION = "0.25.0"


class YantrikMemoryUnsupported(YantrikDBClientError):
    """The memory server does not offer this. Not a failure of the memory."""


def _unsupported(what: str) -> YantrikMemoryUnsupported:
    return YantrikMemoryUnsupported(
        f"{what} is not offered by the Yantrik memory server (yantrik mode)"
    )


def _is_owner_scoped(config: YantrikDBConfig) -> bool:
    return bool(getattr(config, "owner_scoping", False))


def resolve_memory_url(config: YantrikDBConfig) -> str:
    url = (getattr(config, "memory_server_url", "") or DEFAULT_MEMORY_URL).rstrip("/")
    return url if url.endswith("/mcp") else f"{url}/mcp"


def resolve_token_file(config: YantrikDBConfig) -> Path:
    raw = getattr(config, "memory_server_token_file", "") or DEFAULT_TOKEN_FILE
    return Path(os.path.expanduser(raw))


def read_token(config: YantrikDBConfig) -> str:
    """The token beside the memory file. Empty when it cannot be read."""
    try:
        return resolve_token_file(config).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


class YantrikMemoryClient:
    """The plugin's backend surface over the Yantrik memory server's MCP tools."""

    def __init__(self, config: YantrikDBConfig) -> None:
        if _is_owner_scoped(config):
            raise YantrikDBError(
                "owner_scoping cannot be used in yantrik mode: it separates people "
                "inside one database, and the Yantrik memory server serves the whole "
                "memory to the machine's owner. Turn owner_scoping off, or use "
                "embedded mode for a multi-person agent."
            )
        self.config = config
        self._url = resolve_memory_url(config)
        self._health_url = self._url[: -len("/mcp")] + "/health"
        self._token = read_token(config)
        self._http = requests.Session()
        self._session_id: str | None = None
        self._session_lock = threading.Lock()
        self._ids = itertools.count(1)
        self._ids_lock = threading.Lock()
        self._beliefs: OrderedDict[str, str] = OrderedDict()
        self._beliefs_lock = threading.Lock()
        self._turns: dict[str, deque[dict[str, Any]]] = {}
        self._turns_lock = threading.Lock()

    # -- transport ------------------------------------------------------

    def _next_id(self) -> int:
        with self._ids_lock:
            return next(self._ids)

    def _headers(self, *, with_session: bool = True) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": f"hermes-yantrikdb-plugin/{_PLUGIN_VERSION}",
        }
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        if with_session and self._session_id:
            h["Mcp-Session-Id"] = self._session_id
            h["MCP-Protocol-Version"] = MCP_PROTOCOL_VERSION
        return h

    def _post(self, message: dict[str, Any], *, with_session: bool = True) -> requests.Response:
        try:
            return self._http.post(
                self._url,
                data=json.dumps(message),
                headers=self._headers(with_session=with_session),
                timeout=(self.config.connect_timeout, self.config.read_timeout),
                stream=True,
            )
        except requests.Timeout as e:
            raise YantrikDBTransientError(f"timeout reaching the memory server: {e}") from e
        except requests.ConnectionError as e:
            raise YantrikDBTransientError(
                f"the memory server at {self._url} is not reachable: {e}"
            ) from e
        except requests.RequestException as e:
            raise YantrikDBError(f"memory server request failed: {e}") from e

    @staticmethod
    def _read_reply(resp: requests.Response, want_id: int) -> dict[str, Any]:
        """The JSON-RPC reply with ``want_id``, from a JSON body or an SSE stream.

        Decoded as UTF-8 by hand. An event stream arrives with no charset, and ``requests`` then
        falls back to ISO-8859-1 for any ``text/*`` type — which turns every curly quote and
        accented name in memory into mojibake on the way to the agent.
        """
        try:
            ctype = resp.headers.get("Content-Type", "")
            if "text/event-stream" not in ctype:
                return json.loads(resp.content.decode("utf-8"))
            data: list[str] = []
            for raw in resp.iter_lines():
                line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                if line.startswith("data:"):
                    data.append(line[5:].lstrip())
                    continue
                if line == "" and data:
                    payload, data = "\n".join(data), []
                    try:
                        msg = json.loads(payload)
                    except ValueError:
                        continue
                    if isinstance(msg, dict) and msg.get("id") == want_id:
                        return msg
            if data:
                msg = json.loads("\n".join(data))
                if isinstance(msg, dict) and msg.get("id") == want_id:
                    return msg
            raise YantrikDBServerError("the memory server closed the stream without replying")
        except ValueError as e:
            raise YantrikDBServerError(f"unreadable reply from the memory server: {e}") from e
        finally:
            resp.close()

    def _check_status(self, resp: requests.Response) -> None:
        status = resp.status_code
        if status < 400:
            return
        body = (resp.text or "")[:300]
        resp.close()
        if status in (401, 403):
            raise YantrikDBAuthError(
                f"{status}: the memory server refused the token from "
                f"{resolve_token_file(self.config)}"
            )
        if status in (429, 503):
            raise YantrikDBTransientError(f"{status}: {body}")
        if status >= 500:
            raise YantrikDBServerError(f"{status}: {body}")
        raise YantrikDBClientError(f"{status}: {body}")

    def _open_session(self) -> None:
        init_id = self._next_id()
        initialize = {
            "jsonrpc": "2.0",
            "id": init_id,
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "hermes-yantrikdb-plugin", "version": _PLUGIN_VERSION},
            },
        }
        resp = self._post(initialize, with_session=False)
        if resp.status_code in (401, 403):
            # Re-read the token once: it may not have existed when this client started, since the
            # memory's first owner creates it.
            resp.close()
            self._token = read_token(self.config)
            resp = self._post(initialize, with_session=False)
        self._check_status(resp)
        session_id = resp.headers.get("Mcp-Session-Id")
        reply = self._read_reply(resp, init_id)
        if "error" in reply:
            raise YantrikDBServerError(f"the memory server refused the session: {reply['error']}")
        self._session_id = session_id
        note = self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._check_status(note)
        note.close()

    def _ensure_session(self) -> None:
        if self._session_id:
            return
        with self._session_lock:
            if not self._session_id:
                self._open_session()

    def _call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call one tool and return its JSON result.

        A lost session is reopened once. That is the normal shape of a mind switch: the owner of
        the memory file changes, the new owner has never heard of this session, and says 404.
        """
        for attempt in (1, 2):
            self._ensure_session()
            stale = self._session_id
            call_id = self._next_id()
            resp = self._post(
                {
                    "jsonrpc": "2.0",
                    "id": call_id,
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments},
                }
            )
            if resp.status_code == 404 and attempt == 1:
                resp.close()
                with self._session_lock:
                    if self._session_id == stale:
                        self._session_id = None
                continue
            self._check_status(resp)
            reply = self._read_reply(resp, call_id)
            break
        if "error" in reply:
            err = reply["error"] or {}
            message = str(err.get("message") or err)[:500]
            if err.get("code") == _JSONRPC_INVALID_PARAMS:
                raise YantrikDBClientError(f"{tool}: {message}")
            raise YantrikDBServerError(f"{tool}: {message}")
        result = reply.get("result") or {}
        text = "".join(
            c.get("text", "") for c in result.get("content") or [] if c.get("type") == "text"
        )
        if result.get("isError"):
            raise YantrikDBClientError(f"{tool}: {text[:500]}")
        if result.get("structuredContent") is not None:
            return result["structuredContent"]
        try:
            parsed = json.loads(text) if text.strip() else {}
        except ValueError:
            return {"text": text}
        return parsed if isinstance(parsed, dict) else {"data": parsed}

    # -- core ----------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Who is serving the memory, and whether this client may use it."""
        try:
            resp = self._http.get(
                self._health_url,
                timeout=(self.config.connect_timeout, self.config.read_timeout),
            )
        except requests.RequestException as e:
            raise YantrikDBTransientError(
                f"the memory server at {self._health_url} is not reachable: {e}"
            ) from e
        self._check_status(resp)
        try:
            info = resp.json()
        except ValueError:
            info = {}
        # Health is open; the token is not. Opening the session proves the token works.
        self._ensure_session()
        return {"status": "ok", "mode": "yantrik", **(info if isinstance(info, dict) else {})}

    def remember(
        self,
        text: str,
        *,
        namespace: str | None = None,
        importance: float = 0.6,
        domain: str | None = None,
        memory_type: str | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if idempotency_key:
            raise _unsupported("an idempotency key")
        args: dict[str, Any] = {
            "text": truncate_text(text, self.config.max_text_len),
            "importance": max(0.0, min(1.0, float(importance))),
            "namespace": namespace or self.config.namespace,
            "source": WRITER,
        }
        if domain:
            args["domain"] = domain
        if memory_type:
            args["memory_type"] = memory_type
        if metadata:
            args["metadata"] = metadata
        resp = self._call("remember", args)
        return {"rid": resp.get("rid"), "stored": True}

    def recall(
        self,
        query: str,
        *,
        namespace: str | None = None,
        top_k: int | None = None,
        memory_type: str | None = None,
        domain: str | None = None,
    ) -> dict[str, Any]:
        # `namespace` is accepted and deliberately not used: see the module docstring.
        k = int(top_k or self.config.top_k)
        include = "all"
        if memory_type == "belief":
            include = "beliefs"
        elif memory_type:
            include = "memories"
        resp = self._call("recall", {"query": query, "top_k": k, "include": include})
        results: list[dict[str, Any]] = []
        for row in resp.get("results") or []:
            if row.get("kind") == "belief":
                if domain:
                    continue  # beliefs carry no domain, so none can match one
                results.append(self._belief_result(row))
                continue
            if memory_type and memory_type != "belief" and row.get("memory_type") != memory_type:
                continue
            if domain and row.get("domain") != domain:
                continue
            results.append(self._memory_result(row))
        results = results[:k]
        return {"results": results, "total": len(results)}

    def _belief_result(self, row: dict[str, Any]) -> dict[str, Any]:
        rid = f"belief:{row.get('id')}"
        statement = row.get("statement") or ""
        with self._beliefs_lock:
            self._beliefs[rid] = statement
            self._beliefs.move_to_end(rid)
            while len(self._beliefs) > _BELIEF_CACHE_MAX:
                self._beliefs.popitem(last=False)
        why = row.get("why") or []
        return {
            "rid": rid,
            "text": statement,
            "score": row.get("score"),
            "memory_type": "belief",
            "namespace": "",
            "domain": "",
            "importance": row.get("confidence"),
            "created_at": None,
            "metadata": {
                "kind": "belief",
                "confidence": row.get("confidence"),
                "evidence_count": row.get("evidence_count"),
            },
            "why_retrieved": list(why) if isinstance(why, list) else [str(why)],
        }

    @staticmethod
    def _memory_result(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "rid": row.get("rid"),
            "text": row.get("text") or "",
            "score": row.get("score"),
            "memory_type": row.get("memory_type"),
            "namespace": row.get("namespace"),
            "domain": row.get("domain"),
            "importance": row.get("importance"),
            "created_at": row.get("created_at"),
            "metadata": row.get("metadata") or {"source": row.get("source")},
            "why_retrieved": list(row.get("why_retrieved") or []),
        }

    def forget(self, rid: str) -> dict[str, Any]:
        if rid.startswith("belief:"):
            with self._beliefs_lock:
                statement = self._beliefs.get(rid)
            if not statement:
                raise YantrikDBClientError(
                    f"{rid} is a belief this session has not recalled; beliefs are "
                    "forgotten by statement, so recall it first"
                )
            resp = self._call(
                "forget",
                {"kind": "belief", "target": statement, "reason": "forgotten by Hermes"},
            )
        else:
            resp = self._call("forget", {"kind": "memory", "target": rid})
        return {"rid": rid, "found": bool(resp.get("forgotten"))}

    def conflicts(self, *, namespace: str | None = None) -> dict[str, Any]:
        resp = self._call("conflicts", {})
        out = [
            {
                "conflict_id": c.get("id"),
                "text_a": c.get("belief_a"),
                "text_b": c.get("belief_b"),
                "severity": c.get("severity"),
                "status": c.get("status"),
            }
            for c in resp.get("conflicts") or []
        ]
        return {"conflicts": out, "count": len(out)}

    def relate(
        self,
        entity: str,
        target: str,
        relationship: str,
        *,
        weight: float | None = None,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        return self._call(
            "relate",
            {
                "src": entity,
                "dst": target,
                "rel": relationship,
                "weight": 1.0 if weight is None else float(weight),
            },
        )

    # -- not offered by the memory server --------------------------------

    def think(
        self,
        *,
        run_consolidation: bool = True,
        run_conflict_scan: bool = True,
        run_pattern_mining: bool = False,
        run_personality: bool = False,
        consolidation_limit: int | None = None,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        raise _unsupported("consolidation (think)")

    def stats(self, *, namespace: str | None = None) -> dict[str, Any]:
        raise _unsupported("stats")

    def resolve_conflict(
        self,
        conflict_id: str,
        *,
        strategy: str,
        winner_rid: str | None = None,
        new_text: str | None = None,
        resolution_note: str | None = None,
    ) -> dict[str, Any]:
        raise _unsupported("conflict resolution")

    def pending_triggers(self, *, limit: int = 10) -> dict[str, Any]:
        raise _unsupported("triggers")

    def acknowledge_trigger(self, trigger_id: str) -> dict[str, Any]:
        raise _unsupported("triggers")

    def dismiss_trigger(self, trigger_id: str) -> dict[str, Any]:
        raise _unsupported("triggers")

    def act_on_trigger(self, trigger_id: str) -> dict[str, Any]:
        raise _unsupported("triggers")

    def list_records(
        self,
        *,
        namespace: str | None = None,
        limit: int = 50,
        order: str = "asc",
        domain: str | None = None,
        since_rid: str | None = None,
    ) -> dict[str, Any]:
        raise _unsupported("record listing")

    def knowledge_gaps(
        self,
        *,
        min_count: int = 3,
        max_avg_top_score: float = 0.4,
        limit: int = 20,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        raise _unsupported("knowledge gaps")

    def pack_action(
        self,
        action: str,
        *,
        path: str | None = None,
        pack_id: str | None = None,
        allow_unverified_embedder: bool = False,
    ) -> dict[str, Any]:
        raise _unsupported("knowledge packs")

    def pack_context(self) -> dict[str, Any]:
        return {"context": None}

    def pack_namespaces(self) -> list[str]:
        return []

    def task_add(
        self,
        title: str,
        *,
        namespace: str | None = None,
        priority: str = "medium",
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        raise _unsupported("tasks")

    def task_list(
        self, *, namespace: str | None = None, status: str | None = None,
    ) -> dict[str, Any]:
        raise _unsupported("tasks")

    def task_get(self, task_id: str) -> dict[str, Any]:
        raise _unsupported("tasks")

    def task_update(
        self, task_id: str, *, status: str | None = None, priority: str | None = None,
    ) -> dict[str, Any]:
        raise _unsupported("tasks")

    def task_delete(self, task_id: str) -> dict[str, Any]:
        raise _unsupported("tasks")

    def skill_search(
        self, query: str, *, top_k: int | None = None, applies_to: str | None = None,
    ) -> dict[str, Any]:
        raise _unsupported("skills")

    def skill_define(
        self,
        skill_id: str,
        body: str,
        skill_type: str,
        applies_to: list[str],
        *,
        triggers: list[str] | None = None,
        on_conflict: str = "reject",
        version: str | None = None,
        supersedes_skill_id: str | None = None,
    ) -> dict[str, Any]:
        raise _unsupported("skills")

    def skill_outcome(
        self, skill_id: str, succeeded: bool, *, note: str | None = None,
    ) -> dict[str, Any]:
        raise _unsupported("skills")

    # -- conversation buffer: this process's working memory ----------------

    def record_turn(
        self,
        role: str,
        content: str,
        *,
        namespace: str | None = None,
        max_turns: int = 10,
    ) -> dict[str, Any]:
        key = namespace or self.config.namespace
        with self._turns_lock:
            turns = self._turns.get(key)
            if turns is None or turns.maxlen != max(1, int(max_turns)):
                turns = deque(turns or (), maxlen=max(1, int(max_turns)))
                self._turns[key] = turns
            turns.append({"role": role, "content": content})
        return {"recorded": True, "role": role}

    def recent_turns(self, *, namespace: str | None = None, limit: int = 10) -> dict[str, Any]:
        key = namespace or self.config.namespace
        with self._turns_lock:
            turns = list(self._turns.get(key) or ())
        return {"turns": turns[-max(0, int(limit)):] if limit else []}

    def clear_turns(self, *, namespace: str | None = None) -> dict[str, Any]:
        with self._turns_lock:
            self._turns.pop(namespace or self.config.namespace, None)
        return {"cleared": True}

    def close(self) -> None:
        session_id, self._session_id = self._session_id, None
        if session_id:
            with contextlib.suppress(requests.RequestException):
                self._http.delete(
                    self._url,
                    headers={**self._headers(with_session=False), "Mcp-Session-Id": session_id},
                    timeout=(self.config.connect_timeout, 2.0),
                )
        self._http.close()


__all__ = [
    "DEFAULT_MEMORY_URL",
    "DEFAULT_TOKEN_FILE",
    "YantrikMemoryClient",
    "YantrikMemoryUnsupported",
    "read_token",
    "resolve_memory_url",
    "resolve_token_file",
]
