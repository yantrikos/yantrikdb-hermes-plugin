"""Yantrik mode: the machine's shared memory, reached through its memory server.

On a Yantrik machine one memory file is shared by whichever mind is active —
Yantrik Mind or Hermes — and exactly one process may hold it with a live
engine: a second engine on the same file does not see the first one's writes.
So this backend never opens the file. It talks MCP (streamable HTTP) to the
memory server, which is served by whoever owns the file at the moment: Yantrik
Mind while it runs, the standalone ``yantrik-memory`` service otherwise. Same
address, same tools either way.

What changes from the other modes, on purpose:

- **Recall covers the whole machine's memory.** Every recall asks the server
  for ``include: "all"`` with no namespace: Yantrik Mind's beliefs, and flat
  memories from every namespace, each labelled by kind. The plugin's per-agent
  namespace is kept on Hermes's own flat writes, as a record of who wrote what,
  but not used to narrow recall — reading one memory together is the point of
  this mode. Owner scoping is refused for the same reason: it separates
  *people* inside one database, and a server that serves everything to the
  machine's owner cannot honour that.
- **Facts about the person are written as beliefs.** Yantrik Mind's turns read
  beliefs, not flat memories, so a fact stored with ``remember`` would never
  reach it. Facts, preferences and decisions — from the remember tool, from
  turn extraction, from Hermes's MEMORY.md / USER.md mirror — go through the
  server's ``believe`` tool as evidence *supporting* the statement. Beliefs
  have no namespace: there is one shared set. Only Hermes's own notes stay
  flat memories in its namespace: its record of what was said in a turn, a
  delegation's result, a how-to (``memory_type`` episodic or procedural). The
  server stamps every write with who wrote it; nothing here tries to.
- **Recall returns beliefs as well as memories.** They come back with
  ``memory_type`` ``"belief"`` and the confidence in ``metadata``.
- **Features the server does not offer are refused, not faked** —
  consolidation (``think``), stats, triggers, tasks, skills, knowledge gaps,
  record scans and packs. Each raises :class:`YantrikMemoryUnsupported`, a
  :class:`YantrikDBClientError`, which the provider treats as "not available"
  without counting it against the circuit breaker.
- **The conversation buffer is kept in this process.** The server has none; the
  last few turns are working memory for this agent, not shared memory.

Who Hermes is, to the server
----------------------------

A Yantrik desktop grants each mind its own memory credential (``mem-`` and 64
hex digits) and hands it over, with the memory's address, on every turn.
Hermes's platform adapter puts them in this process's environment:

- ``YANTRIK_MEMORY_CREDENTIAL`` — the credential;
- ``YANTRIK_MEMORY_URL`` — ``http://127.0.0.1:7440/mcp``, or
  ``unix:/run/yantrik-mind/<uid>/memory.sock`` for the person's own socket,
  where the server also checks the caller's uid and accepts only a credential.

Both are read on **every request**, never cached: the credential can change
between turns and is revoked when the mind detaches. The server asks the
desktop about each credential and refuses the tools its grants do not cover
(``recall_ordinary`` for recall, conflicts and the like; ``believe`` for
believe and relate; ``remember`` for remember; forget only on Hermes's own
writes). Each refusal is a :class:`YantrikMemoryNotGranted`, a client error
that says what is missing and never trips the breaker or retries.

On older machines, with no credential in the environment, the machine's token
file is used instead. With neither, every call is refused with "this Yantrik
machine has not granted Hermes its memory yet".

The credential is a secret. It is never logged, never put in an exception
message, and shown redacted by ``repr``.
"""

from __future__ import annotations

import contextlib
import ipaddress
import itertools
import json
import logging
import os
import re
import socket
import threading
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection
from urllib3.connectionpool import HTTPConnectionPool
from urllib3.exceptions import NewConnectionError

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

_PLUGIN_VERSION = "0.28.0"

# The per-mind credential the desktop hands over with each turn, and where the memory is.
CREDENTIAL_ENV = "YANTRIK_MEMORY_CREDENTIAL"
URL_ENV = "YANTRIK_MEMORY_URL"
_CREDENTIAL_RE = re.compile(r"mem-[0-9a-fA-F]{64}")
# Anything credential-shaped, for redaction: a truncated or malformed one is still a secret.
_CREDENTIAL_LIKE_RE = re.compile(r"mem-[0-9A-Za-z_\-]{8,}")
_REDACTED = "mem-<redacted>"
# The host requests are addressed to when the memory is on a unix socket. The socket, not this
# name, decides where they go.
_UNIX_HOST = "yantrik-memory.sock"
# The server's refusal for a grant this mind lacks: "refused: this mind has no `believe` grant".
_GRANT_REFUSAL_RE = re.compile(r"no `([A-Za-z0-9_]+)` grant")
NOT_GRANTED_MESSAGE = (
    "this Yantrik machine has not granted Hermes its memory yet: grant it in Settings › Minds"
)
# Hermes's own notes, kept flat in its namespace. Everything else it writes is a fact about the
# person, and goes to the beliefs Yantrik Mind reads.
_FLAT_MEMORY_TYPES = frozenset({"episodic", "procedural"})
# How hard one piece of evidence pushes a belief: an extracted guess weakly, a stated fact plainly.
_EXTRACTED_STRENGTH = 0.5
_TOLD_STRENGTH = 1.0


# Tools with nothing behind them on the memory server. Offered anyway, each would be refused on
# every call, and a model that sees a tool uses it: Hermes spent turns on yantrikdb_remember's
# idempotency key before retrying without it. So in yantrik mode the provider leaves these out of
# the tool list, and takes the refused fields off the tools it keeps.
UNOFFERED_TOOLS = frozenset({
    "yantrikdb_think",
    "yantrikdb_stats",
    "yantrikdb_resolve_conflict",
    "yantrikdb_pending_triggers",
    "yantrikdb_acknowledge_trigger",
    "yantrikdb_dismiss_trigger",
    "yantrikdb_act_on_trigger",
    "yantrikdb_knowledge_gaps",
    # These three read engine stats or scan every record, neither of which the server offers.
    "yantrikdb_observability",
    "yantrikdb_hygiene",
    "yantrikdb_fleet",
    "yantrikdb_packs",
    "yantrikdb_tasks",
    "yantrikdb_skill_search",
    "yantrikdb_skill_define",
    "yantrikdb_skill_outcome",
})
UNOFFERED_FIELDS = {
    "yantrikdb_remember": frozenset({"idempotency_key"}),
}


class YantrikMemoryUnsupported(YantrikDBClientError):
    """The memory server does not offer this. Not a failure of the memory."""


class YantrikMemoryNotGranted(YantrikMemoryUnsupported):
    """The machine has not granted Hermes this part of its memory, or any of it.

    A client error, so it never counts against the circuit breaker: the memory is fine, Hermes is
    just not let in. ``grant`` names the missing grant when the server said which.
    """

    def __init__(self, message: str, *, tool: str | None = None, grant: str | None = None) -> None:
        super().__init__(message)
        self.tool = tool
        self.grant = grant


def _unsupported(what: str) -> YantrikMemoryUnsupported:
    return YantrikMemoryUnsupported(
        f"{what} is not offered by the Yantrik memory server (yantrik mode)"
    )


def _is_owner_scoped(config: YantrikDBConfig) -> bool:
    return bool(getattr(config, "owner_scoping", False))


def redact(text: Any, *secrets: str) -> str:
    """``text`` with every credential-shaped string, and each of ``secrets``, taken out."""
    out = str(text)
    for secret in secrets:
        if secret:
            out = out.replace(secret, "<redacted>")
    return _CREDENTIAL_LIKE_RE.sub(_REDACTED, out)


_warned: set[str] = set()
_warned_lock = threading.Lock()


def _warn_once(key: str, message: str, *args: Any) -> None:
    with _warned_lock:
        if key in _warned:
            return
        _warned.add(key)
    logger.warning(message, *args)


@dataclass(frozen=True)
class Bearer:
    """What Hermes presents to the memory server, and where it came from.

    ``repr`` and ``str`` never show the value.
    """

    value: str
    source: str  # "credential" (this mind's, from the desktop) or "token file" (older machines)

    def __repr__(self) -> str:
        shown = _REDACTED if self.source == "credential" else "<redacted>"
        return f"Bearer(source={self.source!r}, value={shown!r})"

    __str__ = __repr__


def read_credential() -> str:
    """This mind's credential from the environment, read now. Empty unless well-formed.

    A malformed value is never sent: it could only be refused, and it may be part of something
    that should not leave this process.
    """
    raw = (os.environ.get(CREDENTIAL_ENV) or "").strip()
    if not raw:
        return ""
    if _CREDENTIAL_RE.fullmatch(raw):
        return raw
    _warn_once(
        "malformed-credential",
        "%s is set but is not a memory credential (mem- and 64 hex digits); it is not sent",
        CREDENTIAL_ENV,
    )
    return ""


def resolve_memory_url(config: YantrikDBConfig) -> str:
    """Where the memory is, read now: the environment, then the config, then the default."""
    raw = (os.environ.get(URL_ENV) or "").strip()
    if not raw:
        raw = (getattr(config, "memory_server_url", "") or "").strip() or DEFAULT_MEMORY_URL
    if raw.startswith("unix:"):
        return raw
    url = raw.rstrip("/")
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


def resolve_bearer(config: YantrikDBConfig, *, token_file_allowed: bool = True) -> Bearer | None:
    """This mind's credential when the desktop has handed one over, else the token file."""
    credential = read_credential()
    if credential:
        return Bearer(credential, "credential")
    if token_file_allowed:
        token = read_token(config)
        if token:
            return Bearer(token, "token file")
    return None


# -- the memory on the person's unix socket ---------------------------------------------------
#
# The socket speaks the same HTTP as the TCP port. requests has no unix-socket transport of its
# own, so this is the smallest one urllib3 allows: a connection whose socket is AF_UNIX, in a pool
# of its own, behind an adapter that hands every request to that pool.


class _UnixHTTPConnection(HTTPConnection):
    def __init__(self, *args: Any, socket_path: str, **kwargs: Any) -> None:
        self.socket_path = socket_path
        super().__init__(*args, **kwargs)

    def _new_conn(self) -> socket.socket:
        family = getattr(socket, "AF_UNIX", None)
        if family is None:
            raise NewConnectionError(self, "unix sockets are not available on this platform")
        sock = socket.socket(family, socket.SOCK_STREAM)
        if isinstance(self.timeout, (int, float)):
            sock.settimeout(self.timeout)
        try:
            sock.connect(self.socket_path)
        except OSError as e:
            sock.close()
            raise NewConnectionError(self, f"cannot connect to {self.socket_path}: {e}") from e
        return sock


class _UnixHTTPConnectionPool(HTTPConnectionPool):
    ConnectionCls = _UnixHTTPConnection


class UnixSocketAdapter(HTTPAdapter):
    """Sends every request it is given to the HTTP server listening on ``socket_path``."""

    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        super().__init__(max_retries=0)
        self.pool = _UnixHTTPConnectionPool("localhost", maxsize=4, socket_path=socket_path)

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        return self.pool

    def get_connection(self, url, proxies=None):  # requests before 2.32
        return self.pool

    def request_url(self, request, proxies):
        return request.path_url

    def close(self) -> None:
        self.pool.close()
        super().close()


@dataclass(frozen=True)
class Endpoint:
    """Where requests go: over TCP, or over the person's socket when ``socket_path`` is set."""

    display: str
    mcp_url: str
    health_url: str
    socket_path: str | None = None


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def resolve_endpoint(config: YantrikDBConfig) -> Endpoint:
    """The memory's address, read now, as the URLs requests are sent to."""
    url = resolve_memory_url(config)
    if url.startswith("unix:"):
        path = url[len("unix:"):]
        if path.startswith("//"):  # unix:///run/... as well as unix:/run/...
            path = path[2:]
        if not path.startswith("/"):
            raise YantrikMemoryUnsupported(
                f"{URL_ENV} must name an absolute socket path, like "
                "unix:/run/yantrik-mind/1000/memory.sock"
            )
        base = f"http://{_UNIX_HOST}"
        return Endpoint(url, f"{base}/mcp", f"{base}/health", socket_path=path)
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise YantrikMemoryUnsupported(f"the memory server address is not a URL: {url}")
    if parts.scheme == "http" and not _is_loopback(parts.hostname):
        # The bearer would cross the network in the clear; the server binds loopback only.
        raise YantrikMemoryUnsupported(
            f"refusing to send the memory credential over plain HTTP to {parts.hostname}: "
            "the Yantrik memory server is on this machine (127.0.0.1, or its unix socket)"
        )
    return Endpoint(url, url, url[: -len("/mcp")] + "/health")


def _discard(resp: requests.Response) -> None:
    """Read a short refusal's body and close it, so its connection can be used again."""
    with contextlib.suppress(Exception):
        resp.content  # noqa: B018 - reading is the point
    resp.close()


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
        self._http = requests.Session()
        # The memory is on this machine. A proxy from the environment would carry the bearer off
        # it, and a .netrc entry would replace the bearer with someone else's.
        self._http.trust_env = False
        self._endpoint: Endpoint | None = None
        self._unix_adapter: UnixSocketAdapter | None = None
        self._endpoint_lock = threading.Lock()
        self._session_id: str | None = None
        self._session_lock = threading.Lock()
        self._ids = itertools.count(1)
        self._ids_lock = threading.Lock()
        self._beliefs: OrderedDict[str, str] = OrderedDict()
        self._beliefs_lock = threading.Lock()
        self._turns: dict[str, deque[dict[str, Any]]] = {}
        self._turns_lock = threading.Lock()

    def __repr__(self) -> str:
        try:
            where = resolve_memory_url(self.config)
        except Exception:  # pragma: no cover - repr must not raise
            where = "?"
        bearer = resolve_bearer(self.config, token_file_allowed=not where.startswith("unix:"))
        return f"YantrikMemoryClient(url={redact(where)!r}, bearer={bearer!r})"

    # -- transport ------------------------------------------------------

    def _next_id(self) -> int:
        with self._ids_lock:
            return next(self._ids)

    def _current_endpoint(self) -> Endpoint:
        """The address as it is now. A changed address is a different server: the session goes."""
        ep = resolve_endpoint(self.config)
        with self._endpoint_lock:
            if ep != self._endpoint:
                if self._endpoint is not None:
                    self._session_id = None
                if ep.socket_path and (
                    self._unix_adapter is None or self._unix_adapter.socket_path != ep.socket_path
                ):
                    old, self._unix_adapter = self._unix_adapter, UnixSocketAdapter(ep.socket_path)
                    self._http.mount(f"http://{_UNIX_HOST}/", self._unix_adapter)
                    if old is not None:
                        old.close()
                self._endpoint = ep
        return ep

    def _current_bearer(self, ep: Endpoint) -> Bearer:
        """What to present, read now. The person's socket does not accept the machine token."""
        bearer = resolve_bearer(self.config, token_file_allowed=ep.socket_path is None)
        if bearer is None:
            raise YantrikMemoryNotGranted(NOT_GRANTED_MESSAGE)
        return bearer

    def _headers(self, bearer: Bearer | None, *, with_session: bool = True) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": f"hermes-yantrikdb-plugin/{_PLUGIN_VERSION}",
        }
        if bearer is not None:
            h["Authorization"] = f"Bearer {bearer.value}"
        if with_session and self._session_id:
            h["Mcp-Session-Id"] = self._session_id
            h["MCP-Protocol-Version"] = MCP_PROTOCOL_VERSION
        return h

    def _send(
        self, ep: Endpoint, bearer: Bearer, message: dict[str, Any], *, with_session: bool,
    ) -> requests.Response:
        try:
            return self._http.post(
                ep.mcp_url,
                data=json.dumps(message),
                headers=self._headers(bearer, with_session=with_session),
                timeout=(self.config.connect_timeout, self.config.read_timeout),
                stream=True,
            )
        except requests.Timeout as e:
            raise YantrikDBTransientError(
                f"timeout reaching the memory server at {ep.display}: {redact(e, bearer.value)}"
            ) from None
        except requests.ConnectionError as e:
            raise YantrikDBTransientError(
                f"the memory server at {ep.display} is not reachable: {redact(e, bearer.value)}"
            ) from None
        except requests.RequestException as e:
            raise YantrikDBError(
                f"memory server request failed: {redact(e, bearer.value)}"
            ) from None

    def _post(self, message: dict[str, Any], *, with_session: bool = True) -> requests.Response:
        """POST one MCP message with the bearer as it is now.

        A 401/403 is answered once, by reading the bearer again: the desktop may have handed over
        a new credential (or the memory's first owner just wrote the token) since this one was
        read. Only a bearer that has actually changed is tried, so this never loops.
        """
        ep = self._current_endpoint()
        bearer = self._current_bearer(ep)
        resp = self._send(ep, bearer, message, with_session=with_session)
        if resp.status_code in (401, 403):
            fresh = resolve_bearer(self.config, token_file_allowed=ep.socket_path is None)
            if fresh is not None and fresh.value != bearer.value:
                _discard(resp)
                bearer = fresh
                resp = self._send(ep, bearer, message, with_session=with_session)
        if resp.status_code in (401, 403):
            status = resp.status_code
            _discard(resp)
            raise self._refused(status, bearer)
        return resp

    def _refused(self, status: int, bearer: Bearer) -> YantrikDBError:
        if bearer.source == "credential":
            _warn_once(
                "credential-refused",
                "the Yantrik memory server refused Hermes's memory credential (%s)", status,
            )
            return YantrikMemoryNotGranted(
                f"{status}: this Yantrik machine refused Hermes's memory credential — the grant "
                "may have been withdrawn, or Hermes was detached: grant it in Settings › Minds"
            )
        return YantrikDBAuthError(
            f"{status}: the memory server refused the token from "
            f"{resolve_token_file(self.config)}"
        )

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
        body = redact((resp.text or "")[:300])
        resp.close()
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
        self._check_status(resp)
        session_id = resp.headers.get("Mcp-Session-Id")
        reply = self._read_reply(resp, init_id)
        if "error" in reply:
            raise YantrikDBServerError(
                f"the memory server refused the session: {redact(reply['error'])}"
            )
        self._session_id = session_id
        note = self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._check_status(note)
        note.close()

    def _ensure_session(self) -> None:
        self._current_endpoint()  # an address that changed since the session opened drops it
        if self._session_id:
            return
        with self._session_lock:
            if not self._session_id:
                self._open_session()

    @staticmethod
    def _refusal(tool: str, message: str) -> YantrikDBClientError | None:
        """The server's refusal of this call, as the error to raise — or None if it is not one."""
        m = _GRANT_REFUSAL_RE.search(message)
        if m:
            grant = m.group(1)
            _warn_once(
                f"grant:{grant}",
                "the Yantrik memory server refused %s: Hermes has no `%s` grant", tool, grant,
            )
            return YantrikMemoryNotGranted(
                f"{tool}: this Yantrik machine has not granted Hermes `{grant}` — {message}",
                tool=tool,
                grant=grant,
            )
        if message.lower().startswith("refused"):
            return YantrikDBClientError(f"{tool}: {message}")
        return None

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
                _discard(resp)
                with self._session_lock:
                    if self._session_id == stale:
                        self._session_id = None
                continue
            self._check_status(resp)
            reply = self._read_reply(resp, call_id)
            break
        if "error" in reply:
            err = reply["error"] or {}
            message = redact(str(err.get("message") or err)[:500])
            refusal = self._refusal(tool, message)
            if refusal is not None:
                raise refusal
            if err.get("code") == _JSONRPC_INVALID_PARAMS:
                raise YantrikDBClientError(f"{tool}: {message}")
            raise YantrikDBServerError(f"{tool}: {message}")
        result = reply.get("result") or {}
        text = "".join(
            c.get("text", "") for c in result.get("content") or [] if c.get("type") == "text"
        )
        if result.get("isError"):
            message = redact(text[:500])
            raise self._refusal(tool, message) or YantrikDBClientError(f"{tool}: {message}")
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
        ep = self._current_endpoint()
        try:
            resp = self._http.get(
                ep.health_url,
                timeout=(self.config.connect_timeout, self.config.read_timeout),
            )
        except requests.RequestException as e:
            raise YantrikDBTransientError(
                f"the memory server at {ep.display} is not reachable: {redact(e)}"
            ) from None
        self._check_status(resp)
        try:
            info = resp.json()
        except ValueError:
            info = {}
        # Health is open; the memory is not. Opening the session proves the bearer works.
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
        """Store what Hermes learned: a fact about the person as a belief, its own note flat.

        See the module docstring for why. A verbatim turn (``metadata.role`` set, not
        extracted) is Hermes's record of what was said — a note, not a fact.
        """
        if idempotency_key:
            raise _unsupported("an idempotency key")
        text = truncate_text(text, self.config.max_text_len)
        if self._is_own_note(memory_type, metadata):
            return self._remember_flat(
                text, namespace=namespace, importance=importance, domain=domain,
                memory_type=memory_type, metadata=metadata,
            )
        return self._believe(text, metadata=metadata)

    @staticmethod
    def _is_own_note(memory_type: str | None, metadata: dict[str, Any] | None) -> bool:
        if (memory_type or "").lower() in _FLAT_MEMORY_TYPES:
            return True
        meta = metadata or {}
        return bool(meta.get("role")) and meta.get("source") != "extracted"

    def _remember_flat(
        self,
        text: str,
        *,
        namespace: str | None,
        importance: float,
        domain: str | None,
        memory_type: str | None,
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "text": text,
            "importance": max(0.0, min(1.0, float(importance))),
            "namespace": namespace or self.config.namespace,
            "source": WRITER,
            "memory_type": memory_type or "episodic",
        }
        if domain:
            args["domain"] = domain
        if metadata:
            args["metadata"] = metadata
        resp = self._call("remember", args)
        return {"rid": resp.get("rid"), "stored": True}

    def _believe(self, statement: str, *, metadata: dict[str, Any] | None) -> dict[str, Any]:
        meta = metadata or {}
        extracted = meta.get("source") == "extracted"
        confirmed = bool(meta.get("confirmed_by_user"))
        origin = str(meta.get("source") or "remember tool")
        session = meta.get("session_id")
        args: dict[str, Any] = {
            "statement": statement,
            "direction": "supports",
            "strength": _EXTRACTED_STRENGTH if extracted and not confirmed else _TOLD_STRENGTH,
            "provenance": "extracted" if extracted else "told",
            "source": f"hermes ({origin}{f', session {session}' if session else ''})",
        }
        resp = self._call("believe", args)
        bid = resp.get("id")
        rid = f"belief:{bid}" if bid is not None else None
        if rid:
            self._cache_belief(rid, resp.get("statement") or statement)
        return {"rid": rid, "stored": True, "kind": "belief"}

    def recall(
        self,
        query: str,
        *,
        namespace: str | None = None,
        top_k: int | None = None,
        memory_type: str | None = None,
        domain: str | None = None,
    ) -> dict[str, Any]:
        """The whole machine's memory: always ``include: "all"``, never a namespace.

        ``namespace`` is accepted and deliberately not sent (see the module docstring).
        ``memory_type`` narrows what comes back here, not what is asked for: ``"belief"`` keeps
        only beliefs; episodic or procedural keeps only flat memories of that type; any other type
        keeps the beliefs — where facts about the person live — and flat memories of that type.
        """
        k = int(top_k or self.config.top_k)
        resp = self._call("recall", {"query": query, "top_k": k, "include": "all"})
        wanted = (memory_type or "").lower()
        results: list[dict[str, Any]] = []
        for row in resp.get("results") or []:
            if row.get("kind") == "belief":
                if domain or wanted in _FLAT_MEMORY_TYPES:
                    continue  # beliefs carry no domain or note type, so none can match one
                results.append(self._belief_result(row))
                continue
            if wanted == "belief":
                continue
            if wanted and row.get("memory_type") != memory_type:
                continue
            if domain and row.get("domain") != domain:
                continue
            results.append(self._memory_result(row))
        results = results[:k]
        return {"results": results, "total": len(results)}

    def _cache_belief(self, rid: str, statement: str) -> None:
        with self._beliefs_lock:
            self._beliefs[rid] = statement
            self._beliefs.move_to_end(rid)
            while len(self._beliefs) > _BELIEF_CACHE_MAX:
                self._beliefs.popitem(last=False)

    def _belief_result(self, row: dict[str, Any]) -> dict[str, Any]:
        rid = f"belief:{row.get('id')}"
        statement = row.get("statement") or ""
        self._cache_belief(rid, statement)
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
        """Forget by rid, or a belief by statement. The server allows only Hermes's own writes."""
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
        ep = self._endpoint
        if session_id and ep is not None:
            with contextlib.suppress(Exception):
                bearer = resolve_bearer(self.config, token_file_allowed=ep.socket_path is None)
                self._http.delete(
                    ep.mcp_url,
                    headers={**self._headers(bearer, with_session=False),
                             "Mcp-Session-Id": session_id},
                    timeout=(self.config.connect_timeout, 2.0),
                )
        self._http.close()


__all__ = [
    "CREDENTIAL_ENV",
    "DEFAULT_MEMORY_URL",
    "DEFAULT_TOKEN_FILE",
    "NOT_GRANTED_MESSAGE",
    "URL_ENV",
    "Bearer",
    "Endpoint",
    "UnixSocketAdapter",
    "YantrikMemoryClient",
    "YantrikMemoryNotGranted",
    "YantrikMemoryUnsupported",
    "read_credential",
    "read_token",
    "redact",
    "resolve_bearer",
    "resolve_endpoint",
    "resolve_memory_url",
    "resolve_token_file",
]
