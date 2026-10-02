"""Yantrik mode: the plugin over a Yantrik machine's shared memory server.

The server here is a small fake that speaks the real transport — MCP streamable
HTTP with SSE replies, session ids and a bearer token — so these tests exercise
the client's actual wire handling (session setup, stream parsing, reopening a
session after the memory changes owner) rather than a mocked session object.
Its tools mirror the shapes the real server (yantrik-mind's mind-memory-mcp)
returns, and it authenticates the way that server does: the machine token
(older machines) is served everything, a per-mind ``mem-`` credential only the
tools its grants cover, refused with the server's own wording.
"""

from __future__ import annotations

import contextlib
import inspect
import itertools
import json
import os
import socket
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

TOKEN = "a" * 64
CRED = "mem-" + "1f" * 32
CRED_2 = "mem-" + "2e" * 32
ALL_GRANTS = frozenset({"recall_ordinary", "remember", "believe"})
# The grant each tool needs, exactly as mind-memory-mcp's server.rs asks for it.
TOOL_GRANT = {
    "remember": "remember",
    "recall": "recall_ordinary",
    "beliefs": "recall_ordinary",
    "explain": "recall_ordinary",
    "conflicts": "recall_ordinary",
    "reflect": "recall_ordinary",
    "believe": "believe",
    "relate": "believe",
}


class FakeMemory:
    """The memory server's state, and its tools."""

    def __init__(self) -> None:
        # credential -> its grants. The desktop's answer, as the server would get it.
        self.credentials: dict[str, frozenset[str]] = {}
        self.bearers: list[str] = []  # every bearer presented, in order
        self.sessions: set[str] = set()
        self.session_ids = itertools.count(1)
        self.memories: list[dict[str, Any]] = []
        self.beliefs: list[dict[str, Any]] = [
            {"id": "b1", "statement": "The person lives in Bentonville", "confidence": 0.82,
             "evidence_count": 3, "score": 0.71, "why": ["semantic match"]},
        ]
        self.forgotten: list[tuple[str, str]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.initializations = 0
        self.token = TOKEN
        self.socket_only = False  # the person's socket: the machine token is not accepted there

    def grants_for(self, bearer: str) -> frozenset[str] | None:
        """What this bearer may do, or None when it is refused outright (401)."""
        if bearer == self.token and not self.socket_only:
            return ALL_GRANTS
        if bearer.startswith("mem-"):
            return self.credentials.get(bearer)
        return None

    def calls_to(self, name: str) -> list[dict[str, Any]]:
        return [args for tool, args in self.calls if tool == name]

    def tool(self, name: str, args: dict[str, Any], grants: frozenset[str] = ALL_GRANTS) -> dict[str, Any]:
        self.calls.append((name, args))
        need = TOOL_GRANT.get(name)
        if need and need not in grants:
            raise ValueError(
                f"refused: this mind has no `{need}` grant -- the person can give it in Settings -> Memory"
            )
        if name == "believe":
            if "BEGIN RSA PRIVATE KEY" in args["statement"]:
                raise ValueError("believe refused: denied: memory write-gate: private key")
            found = next((b for b in self.beliefs if b["statement"] == args["statement"]), None)
            if found is None:
                found = {"id": f"b{len(self.beliefs) + 1}", "statement": args["statement"],
                         "confidence": 0.6, "evidence_count": 0, "score": 0.7, "why": ["semantic match"]}
                self.beliefs.append(found)
            found["evidence_count"] += 1
            return {"kind": "belief", "id": found["id"], "statement": found["statement"],
                    "confidence": found["confidence"], "evidence_count": found["evidence_count"],
                    "provenance": args.get("provenance", "told"), "status": "active", "updated_ms": 0}
        if name == "remember":
            if "BEGIN RSA PRIVATE KEY" in args["text"]:
                raise ValueError("invalid_params: remember refused: denied: memory write-gate: private key")
            rid = f"m{len(self.memories) + 1}"
            meta = dict(args.get("metadata") or {})
            meta.setdefault("source", args.get("source", "mcp"))
            self.memories.append({
                "kind": "memory", "rid": rid, "text": args["text"], "score": 0.66,
                "memory_type": args.get("memory_type", "semantic"),
                "namespace": args.get("namespace", "default"),
                "domain": args.get("domain", "general"), "source": args.get("source", "mcp"),
                "created_at": time.time(), "importance": args.get("importance", 0.5),
                "metadata": meta, "why_retrieved": ["semantic match"],
            })
            return {"rid": rid, "status": "stored"}
        if name == "recall":
            include = args.get("include", "all")
            beliefs = [
                {"kind": "belief", **{k: v for k, v in b.items() if k != "statement"},
                 "statement": b["statement"]}
                for b in self.beliefs
            ] if include in ("all", "beliefs") else []
            memories = [
                m for m in self.memories
                if args.get("namespace") in (None, m["namespace"])
            ] if include in ("all", "memories") else []
            results: list[dict[str, Any]] = []
            for pair in itertools.zip_longest(beliefs, memories):
                results.extend(r for r in pair if r is not None)
            return {"query": args["query"], "count": len(results), "results": results}
        if name == "forget":
            self.forgotten.append((args["kind"], args["target"]))
            return {"forgotten": True, "kind": args["kind"], "target": args["target"]}
        if name == "conflicts":
            return {"count": 1, "conflicts": [
                {"id": "c1", "belief_a": "The person drinks coffee",
                 "belief_b": "The person never drinks coffee", "severity": 0.9, "status": "open"},
            ]}
        if name == "relate":
            return {"related": True, "src": args["src"], "rel": args["rel"], "dst": args["dst"]}
        raise KeyError(name)


def _handler(memory: FakeMemory):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:  # quiet
            pass

        def _send(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/health":
                body = json.dumps({"ok": True, "server": "yantrik-memory", "served_by": "yantrik-mind"})
                self._send(200, body.encode(), {"Content-Type": "application/json"})
            else:
                self._send(404)

        def do_DELETE(self) -> None:
            memory.sessions.discard(self.headers.get("Mcp-Session-Id", ""))
            self._send(202)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            msg = json.loads(self.rfile.read(length))
            presented = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
            memory.bearers.append(presented)
            grants = memory.grants_for(presented)
            if grants is None:
                self._send(401, b"a valid bearer token is required")
                return
            if msg.get("method") == "initialize":
                memory.initializations += 1
                sid = f"session-{next(memory.session_ids)}"
                memory.sessions.add(sid)
                reply = {"jsonrpc": "2.0", "id": msg["id"], "result": {
                    "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                    "serverInfo": {"name": "yantrik-memory", "version": "test"}}}
                self._sse(reply, {"Mcp-Session-Id": sid})
                return
            if self.headers.get("Mcp-Session-Id") not in memory.sessions:
                self._send(404, b"session not found")
                return
            if "id" not in msg:
                self._send(202)
                return
            params = msg.get("params") or {}
            if params.get("name") == "echo_bearer":
                # A server that is careless with what it was sent: the client must not repeat it.
                self._sse({"jsonrpc": "2.0", "id": msg["id"], "error": {
                    "code": -32603, "message": f"internal error handling Bearer {presented}"}})
                return
            try:
                data = memory.tool(params["name"], params.get("arguments") or {}, grants)
                reply = {"jsonrpc": "2.0", "id": msg["id"], "result": {
                    "content": [{"type": "text", "text": json.dumps(data, indent=2, ensure_ascii=False)}],
                    "isError": False}}
            except ValueError as e:
                reply = {"jsonrpc": "2.0", "id": msg["id"],
                         "error": {"code": -32602, "message": str(e)}}
            except KeyError as e:
                reply = {"jsonrpc": "2.0", "id": msg["id"],
                         "error": {"code": -32603, "message": f"no tool {e}"}}
            self._sse(reply)

        def _sse(self, reply: dict[str, Any], headers: dict[str, str] | None = None) -> None:
            # A priming event with no JSON first, as resumable servers send, then the reply.
            # Raw UTF-8 and no charset, exactly as the real server sends it.
            body = ("id: 0\nretry: 3000\ndata: \n\n"
                    f"id: 1\ndata: {json.dumps(reply, ensure_ascii=False)}\n\n").encode()
            self._send(200, body, {"Content-Type": "text/event-stream", **(headers or {})})

    return Handler


@pytest.fixture
def memory_server():
    memory = FakeMemory()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(memory))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    memory.url = f"http://127.0.0.1:{server.server_address[1]}/mcp"
    yield memory
    server.shutdown()
    server.server_close()


# The Hermes gateway session key of the desktop's chat: what Hermes hands memory providers as
# `gateway_session_key`, and what the Yantrik platform adapter registers the credential under.
SK = "agent:main:yantrik:dm:desk-1"
OTHER_SK = "agent:main:telegram:dm:42"


@pytest.fixture(autouse=True)
def fresh_registry():
    """Every test starts on a machine with no desktop harness: the registry is process-global."""
    sys.modules.pop("yantrik_desktop_memory_grants", None)
    yield
    sys.modules.pop("yantrik_desktop_memory_grants", None)


@pytest.fixture
def yantrik_env(memory_server, tmp_path, monkeypatch):
    token_file = tmp_path / "yantrik-memory.token"
    token_file.write_text(TOKEN, encoding="utf-8")
    monkeypatch.setenv("YANTRIKDB_MODE", "yantrik")
    monkeypatch.setenv("YANTRIK_MEMORY_URL", memory_server.url)
    monkeypatch.setenv("YANTRIK_MEMORY_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("YANTRIK_MEMORY_CREDENTIAL", raising=False)
    # The defaults these tests rely on, pinned: benchmarks/_bootstrap.py turns extraction off in
    # os.environ for the rest of the process.
    monkeypatch.setenv("YANTRIKDB_EXTRACTION_ENABLED", "true")
    monkeypatch.setenv("YANTRIKDB_SYNC_USER_MESSAGES", "true")
    return token_file


@pytest.fixture
def ym(plugin, yantrik_env):
    """The yantrik_memory module, imported under the test package."""
    import importlib
    return importlib.import_module(f"{plugin[0].__name__}.yantrik_memory")


@pytest.fixture
def granted(memory_server, ym):
    """The desktop has granted Hermes its memory, for the desktop session SK, in memory."""
    memory_server.credentials[CRED] = ALL_GRANTS
    ym.set_desktop_credential(SK, CRED, memory_server.url)
    return memory_server


@pytest.fixture
def client(ym, client_module):
    """A client that Hermes said nothing about: an older machine's, on the token file."""
    c = ym.YantrikMemoryClient(client_module.YantrikDBConfig.from_env())
    yield c
    c.close()


@pytest.fixture
def desk(ym, client_module):
    """The client of Hermes's Yantrik desktop session SK."""
    c = ym.YantrikMemoryClient(
        client_module.YantrikDBConfig.from_env(), platform="yantrik", session_keys=(SK,),
    )
    yield c
    c.close()


@pytest.fixture
def provider(provider_module, yantrik_env, ym):
    p = provider_module.YantrikDBMemoryProvider()
    p.initialize("sess-1", agent_workspace="home", agent_identity="hermes", platform="cli")
    yield p
    p.shutdown()


@pytest.fixture
def desk_provider(provider_module, yantrik_env, ym):
    """The provider exactly as Hermes's gateway initializes it for a Yantrik desktop turn."""
    p = provider_module.YantrikDBMemoryProvider()
    p.initialize(
        "20261002_desk", agent_workspace="hermes", agent_identity="default",
        platform="yantrik", gateway_session_key=SK, agent_context="primary",
    )
    yield p
    p.shutdown()


def _env_has_no_credential() -> bool:
    return not any(CRED in v or CRED_2 in v for v in os.environ.values())


def _minimal_args(schema: dict[str, Any]) -> dict[str, Any]:
    """The least a tool call needs to reach the client: every required field, filled plainly."""
    params = schema.get("parameters") or {}
    props = params.get("properties") or {}
    filler = {"string": "x", "number": 0.5, "integer": 1, "boolean": False, "array": ["x"], "object": {}}
    args: dict[str, Any] = {}
    for name in params.get("required") or []:
        spec = props.get(name) or {}
        args[name] = (spec.get("enum") or [filler.get(spec.get("type"), "x")])[0]
    return args


def _wait(t, timeout: float = 5.0) -> None:
    if t is not None and t.is_alive():
        t.join(timeout=timeout)


# ---------------------------------------------------------------------------
# The client over the wire
# ---------------------------------------------------------------------------

class TestWire:
    def test_health_names_the_owner_and_proves_the_token(self, client, memory_server):
        info = client.health()
        assert info["served_by"] == "yantrik-mind"
        assert memory_server.initializations == 1

    def test_an_own_note_is_flat_in_hermes_namespace_and_comes_back_with_its_metadata(
        self, client, memory_server
    ):
        rid = client.remember(
            "Delegated the garage inventory; the subagent found the bike",
            namespace="hermes:home:hermes", importance=0.5, memory_type="episodic",
            metadata={"source": "hermes_delegation", "session_id": "s1"},
        )["rid"]
        name, sent = memory_server.calls[-1]
        assert name == "remember"
        assert sent["source"] == "hermes"
        assert sent["namespace"] == "hermes:home:hermes"
        assert sent["memory_type"] == "episodic"
        assert "written_by" not in sent and "written_by" not in sent.get("metadata", {})

        hits = client.recall("bike", namespace="hermes:home:hermes", top_k=10)["results"]
        mine = next(h for h in hits if h["rid"] == rid)
        assert mine["metadata"]["source"] == "hermes_delegation"
        assert mine["importance"] == 0.5

    def test_recall_reads_the_whole_machine_not_this_agents_namespace(self, client, memory_server):
        memory_server.tool("remember", {"text": "Written by Yantrik Mind", "namespace": "default",
                                        "source": "yantrik-mind"})
        hits = client.recall("anything", namespace="hermes:home:hermes")["results"]
        assert "namespace" not in memory_server.calls[-1][1]
        assert any(h["text"] == "Written by Yantrik Mind" for h in hits)

    def test_text_that_is_not_ascii_survives_the_stream(self, client):
        rid = client.remember("Tonight’s dinner is at Zoë’s café")["rid"]
        hits = client.recall("dinner")["results"]
        assert next(h for h in hits if h["rid"] == rid)["text"] == "Tonight’s dinner is at Zoë’s café"

    def test_beliefs_come_back_marked_as_beliefs(self, client):
        hits = client.recall("where do they live")["results"]
        belief = next(h for h in hits if h["memory_type"] == "belief")
        assert belief["text"] == "The person lives in Bentonville"
        assert belief["rid"] == "belief:b1"
        assert belief["metadata"] == {"kind": "belief", "confidence": 0.82, "evidence_count": 3}

    def test_a_domain_filter_cannot_match_a_belief(self, client, memory_server):
        memory_server.tool("remember", {"text": "Standup is at nine", "domain": "work"})
        hits = client.recall("standup", domain="work")["results"]
        assert [h["text"] for h in hits] == ["Standup is at nine"]

    def test_a_lost_session_is_reopened_once_transparently(self, client, memory_server):
        client.health()
        memory_server.sessions.clear()  # the memory changed owner; the new one never saw this session
        assert client.remember("After the handover")["rid"]
        assert memory_server.initializations == 2

    def test_a_wrong_token_is_an_auth_error(self, client, client_module, memory_server):
        memory_server.token = "b" * 64
        with pytest.raises(client_module.YantrikDBAuthError):
            client.health()

    def test_the_token_is_reread_when_refused(self, client, memory_server, yantrik_env):
        memory_server.token = "c" * 64
        yantrik_env.write_text("c" * 64, encoding="utf-8")
        assert client.health()["status"] == "ok"

    def test_a_refused_write_is_the_callers_error(self, client, client_module):
        with pytest.raises(client_module.YantrikDBClientError) as e:
            client.remember("-----BEGIN RSA PRIVATE KEY-----\nMIIEvg==")
        assert not isinstance(e.value, client_module.YantrikDBServerError)

    def test_a_server_failure_is_a_server_error(self, client, client_module):
        with pytest.raises(client_module.YantrikDBServerError):
            client._call("no_such_tool", {})

    def test_an_unreachable_server_is_transient(self, ym, client_module, monkeypatch):
        monkeypatch.setenv("YANTRIK_MEMORY_URL", "http://127.0.0.1:9/mcp")
        c = ym.YantrikMemoryClient(client_module.YantrikDBConfig.from_env())
        with pytest.raises(client_module.YantrikDBTransientError):
            c.remember("nobody home")

    def test_forgetting_a_memory_goes_by_rid_and_a_belief_by_statement(self, client, memory_server, client_module):
        with pytest.raises(client_module.YantrikDBClientError):
            client.forget("belief:b1")  # not recalled yet: no statement to forget by
        rid = client.remember("Temporary", memory_type="episodic")["rid"]
        client.recall("where do they live")
        assert client.forget(rid)["found"] is True
        assert client.forget("belief:b1")["found"] is True
        assert memory_server.forgotten == [("memory", rid), ("belief", "The person lives in Bentonville")]

    def test_conflicts_take_the_plugins_shape(self, client):
        c = client.conflicts()["conflicts"][0]
        assert (c["conflict_id"], c["text_a"], c["text_b"]) == (
            "c1", "The person drinks coffee", "The person never drinks coffee")

    def test_an_idempotency_key_is_refused_not_dropped(self, client, ym):
        with pytest.raises(ym.YantrikMemoryUnsupported):
            client.remember("x", idempotency_key="k1")

    def test_the_conversation_buffer_is_bounded_and_per_namespace(self, client):
        for i in range(5):
            client.record_turn("user", f"turn {i}", namespace="a", max_turns=3)
        client.record_turn("user", "elsewhere", namespace="b")
        assert [t["content"] for t in client.recent_turns(namespace="a")["turns"]] == [
            "turn 2", "turn 3", "turn 4"]
        client.clear_turns(namespace="a")
        assert client.recent_turns(namespace="a")["turns"] == []
        assert len(client.recent_turns(namespace="b")["turns"]) == 1

    @pytest.mark.parametrize("call", [
        lambda c: c.think(),
        lambda c: c.stats(),
        lambda c: c.pending_triggers(),
        lambda c: c.task_list(),
        lambda c: c.knowledge_gaps(),
        lambda c: c.list_records(),
        lambda c: c.skill_search("x"),
        lambda c: c.pack_action("mount", path="p"),
        lambda c: c.resolve_conflict("c1", strategy="keep_both"),
    ])
    def test_what_the_server_does_not_offer_is_refused_as_a_client_error(self, client, ym, client_module, call):
        with pytest.raises(ym.YantrikMemoryUnsupported) as e:
            call(client)
        assert isinstance(e.value, client_module.YantrikDBClientError)


class TestParity:
    """Every kwarg the provider can pass to the HTTP client must be accepted here too."""

    METHODS = [
        "remember", "recall", "forget", "think", "conflicts", "relate", "stats",
        "record_turn", "recent_turns", "clear_turns", "list_records", "knowledge_gaps",
        "task_add", "task_list", "task_get", "task_update", "task_delete",
        "pending_triggers", "acknowledge_trigger", "dismiss_trigger", "act_on_trigger",
        "resolve_conflict", "pack_action", "pack_context", "pack_namespaces",
        "skill_search", "skill_define", "skill_outcome", "health", "close",
    ]

    @pytest.mark.parametrize("method", METHODS)
    def test_signature_covers_the_http_clients(self, ym, client_module, method):
        http = inspect.signature(getattr(client_module.YantrikDBClient, method))
        mine = inspect.signature(getattr(ym.YantrikMemoryClient, method))
        assert set(http.parameters) <= set(mine.parameters), method


# ---------------------------------------------------------------------------
# The provider in yantrik mode
# ---------------------------------------------------------------------------

class TestProvider:
    def test_available_before_the_desktop_has_handed_over_a_grant(
        self, provider_module, yantrik_env, monkeypatch
    ):
        # The credential arrives with each turn. A provider Hermes drops at startup stays dropped
        # for the session, so availability cannot wait for it: each call answers instead.
        assert provider_module.YantrikDBMemoryProvider().is_available() is True
        yantrik_env.unlink()
        monkeypatch.delenv("YANTRIK_MEMORY_URL")
        assert provider_module.YantrikDBMemoryProvider().is_available() is True

    def test_initialize_connects(self, provider, ym):
        assert isinstance(provider._client, ym.YantrikMemoryClient)
        assert provider._init_error in (None, "")

    def test_owner_scoping_is_refused_at_initialize(self, provider_module, yantrik_env, ym, monkeypatch):
        monkeypatch.setenv("YANTRIKDB_OWNER_SCOPING", "true")
        p = provider_module.YantrikDBMemoryProvider()
        p.initialize("sess-1", agent_workspace="home", agent_identity="hermes", platform="cli")
        assert p._client is None
        assert "owner_scoping" in (p._init_error or "")

    def test_a_turn_is_saved_to_the_shared_memory(self, provider, memory_server):
        provider.sync_turn("My sister Asha is visiting next week from Pune", "Noted.")
        _wait(provider._sync_thread)
        texts = [m["text"] for m in memory_server.memories]
        assert "My sister Asha is visiting next week from Pune" in texts
        assert all(m["source"] == "hermes" for m in memory_server.memories)

    def test_prefetch_brings_back_what_yantrik_mind_believes(self, provider):
        provider.queue_prefetch("where does the person live", session_id="sess-1")
        _wait(provider._prefetch_thread)
        block = provider.prefetch("where does the person live", session_id="sess-1")
        assert "The person lives in Bentonville" in block

    def test_the_recall_tool_says_which_results_are_beliefs(self, provider):
        out = json.loads(provider.handle_tool_call("yantrikdb_recall", {"query": "where do they live"}))
        belief = next(r for r in out["results"] if r["text"] == "The person lives in Bentonville")
        assert (belief["kind"], belief["confidence"]) == ("belief", 0.82)

    def test_prefetch_marks_a_belief_with_its_confidence(self, provider):
        provider.queue_prefetch("where does the person live", session_id="sess-1")
        _wait(provider._prefetch_thread)
        block = provider.prefetch("where does the person live", session_id="sess-1")
        assert "The person lives in Bentonville _(score 0.71)_ _(belief, confidence 0.82)_" in block

    def test_recall_tool_hides_extracted_candidates(self, provider, memory_server):
        memory_server.tool("remember", {"text": "Maybe likes jazz", "metadata": {"source": "extracted"}})
        memory_server.tool("remember", {"text": "Likes jazz, said so", "source": "yantrik-mind"})
        out = json.loads(provider.handle_tool_call("yantrikdb_recall", {"query": "jazz"}))
        texts = [r["text"] for r in out["results"]]
        assert "Likes jazz, said so" in texts
        assert "Maybe likes jazz" not in texts

    def test_unsupported_tools_do_not_trip_the_breaker(self, provider):
        for _ in range(10):
            out = provider.handle_tool_call("yantrikdb_think", {})
            assert "error" in out.lower()
        assert provider._failure_count == 0
        assert not provider._breaker_open()

    def test_the_model_is_offered_exactly_what_the_server_does_not_refuse(
        self, provider_module, yantrik_env, ym, monkeypatch
    ):
        # Every surface switched on, so nothing is hidden for a reason other than the server.
        for flag, value in {
            "YANTRIKDB_TOOL_PROFILE": "full",
            "YANTRIKDB_SKILLS_ENABLED": "true",
            "YANTRIKDB_PACKS_ENABLED": "true",
            "YANTRIKDB_FLEET_VIEW": "true",
        }.items():
            monkeypatch.setenv(flag, value)
        p = provider_module.YantrikDBMemoryProvider()
        p.initialize("sess-1", agent_workspace="home", agent_identity="hermes", platform="cli")
        try:
            refused = set()
            for schema in provider_module.ALL_TOOL_SCHEMAS:
                out = p.handle_tool_call(schema["name"], _minimal_args(schema))
                if "not offered by the Yantrik memory server" in out:
                    refused.add(schema["name"])
            offered = {s["name"] for s in p.get_tool_schemas()}
        finally:
            p.shutdown()
        assert refused == set(ym.UNOFFERED_TOOLS)
        assert not offered & refused
        assert offered == {s["name"] for s in provider_module.ALL_TOOL_SCHEMAS} - refused

    def test_remember_is_offered_without_the_idempotency_key_the_server_refuses(self, provider):
        out = provider.handle_tool_call(
            "yantrikdb_remember", {"text": "The spare key is in the blue pot", "idempotency_key": "msg-1"}
        )
        assert "not offered by the Yantrik memory server" in out
        remember = next(s for s in provider.get_tool_schemas() if s["name"] == "yantrikdb_remember")
        assert "idempotency_key" not in remember["parameters"]["properties"]
        assert "text" in remember["parameters"]["properties"]

    def test_embedded_mode_still_offers_the_idempotency_key(self, provider_module, monkeypatch):
        monkeypatch.setenv("YANTRIKDB_MODE", "embedded")
        remember = next(
            s for s in provider_module.YantrikDBMemoryProvider().get_tool_schemas()
            if s["name"] == "yantrikdb_remember"
        )
        assert "idempotency_key" in remember["parameters"]["properties"]

    def test_session_end_does_not_count_missing_consolidation_as_failure(self, provider):
        provider.on_session_end([{"role": "user", "content": "bye"}])
        assert provider._failure_count == 0

    def test_system_prompt_block_renders_without_the_optional_features(self, provider):
        block = provider.system_prompt_block()
        assert isinstance(block, str)
        assert provider._failure_count == 0


# ---------------------------------------------------------------------------
# The desktop's credential, handed over in memory
# ---------------------------------------------------------------------------

class TestDesktopRegistry:
    def test_the_desktop_session_presents_its_own_credential(self, granted, desk):
        desk.recall("anything")
        assert granted.bearers and set(granted.bearers) == {CRED}

    def test_the_credential_never_touches_the_environment(self, granted, desk, desk_provider):
        desk.recall("anything")
        desk_provider.handle_tool_call("yantrikdb_recall", {"query": "where do they live"})
        assert _env_has_no_credential()
        assert "YANTRIK_MEMORY_CREDENTIAL" not in os.environ

    def test_another_platform_never_gets_the_credential(
        self, granted, ym, client_module, provider_module, yantrik_env
    ):
        # The same gateway process, a Telegram user's turn, even one that knows the desktop's key.
        for keys in ((OTHER_SK,), (SK,)):
            c = ym.YantrikMemoryClient(
                client_module.YantrikDBConfig.from_env(), platform="telegram", session_keys=keys,
            )
            with pytest.raises(ym.YantrikMemoryNotGranted) as e:
                c.recall("what does the person like")
            assert "not granted Hermes its memory" in str(e.value) and "telegram" in str(e.value)
            c.close()
        p = provider_module.YantrikDBMemoryProvider()
        p.initialize("tg-1", platform="telegram", gateway_session_key=SK, agent_context="primary")
        try:
            out = p.handle_tool_call("yantrikdb_recall", {"query": "where do they live"})
            assert "has not granted Hermes its memory yet" in out
        finally:
            p.shutdown()
        assert granted.bearers == []  # nothing at all was sent: no credential, no machine token

    def test_another_session_on_the_yantrik_platform_does_not_borrow_this_ones(
        self, granted, ym, client_module
    ):
        c = ym.YantrikMemoryClient(
            client_module.YantrikDBConfig.from_env(), platform="yantrik",
            session_keys=("agent:main:yantrik:dm:someone-else",),
        )
        with pytest.raises(ym.YantrikMemoryNotGranted):
            c.recall("anything")
        c.close()
        assert granted.bearers == []

    def test_cleared_means_refused(self, granted, desk, ym):
        desk.recall("first turn")
        sent = len(granted.bearers)
        ym.set_desktop_credential(SK, None, None)  # Hermes was detached
        for _ in range(3):
            with pytest.raises(ym.YantrikMemoryNotGranted) as e:
                desk.recall("after detaching")
            assert str(e.value) == ym.NOT_GRANTED_MESSAGE
        assert len(granted.bearers) == sent  # refused before anything goes on the wire

    def test_clear_all_takes_back_every_session(self, granted, desk, ym):
        ym.clear_desktop_credentials()
        with pytest.raises(ym.YantrikMemoryNotGranted):
            desk.recall("anything")

    def test_the_credential_is_read_again_on_every_request(self, granted, desk, ym):
        desk.recall("first turn")
        first = len(granted.bearers)
        granted.credentials[CRED_2] = ALL_GRANTS
        ym.set_desktop_credential(SK, CRED_2, granted.url)  # the next turn's credential
        desk.recall("second turn")
        assert set(granted.bearers[:first]) == {CRED}
        assert set(granted.bearers[first:]) == {CRED_2}
        assert granted.initializations == 1  # the server authenticates each request; same session

    def test_a_credential_that_changes_after_a_refusal_is_tried_once(self, granted, desk, ym):
        desk.recall("first turn")
        del granted.credentials[CRED]
        granted.credentials[CRED_2] = ALL_GRANTS
        ym.set_desktop_credential(SK, CRED_2, granted.url)
        assert desk.recall("second turn")["results"] is not None

    def test_a_revoked_credential_is_refused_once_never_in_a_loop(self, granted, desk, ym):
        desk.recall("first turn")
        del granted.credentials[CRED]  # the desktop stopped vouching for it
        before = len(granted.bearers)
        for _ in range(3):
            with pytest.raises(ym.YantrikMemoryNotGranted) as e:
                desk.recall("after revoking")
            assert CRED not in str(e.value) and "Settings › Minds" in str(e.value)
        assert len(granted.bearers) - before == 3  # one request per call, no retry

    def test_the_session_id_is_accepted_as_the_key_too(self, memory_server, ym, client_module):
        memory_server.credentials[CRED] = ALL_GRANTS
        ym.set_desktop_credential("20261002_desk", CRED, memory_server.url)
        c = ym.YantrikMemoryClient(
            client_module.YantrikDBConfig.from_env(), platform="yantrik",
            session_keys=(SK, "20261002_desk"),
        )
        c.recall("anything")
        c.close()
        assert set(memory_server.bearers) == {CRED}

    def test_the_provider_serves_its_own_desktop_session(self, granted, desk_provider):
        out = json.loads(desk_provider.handle_tool_call("yantrikdb_recall", {"query": "where do they live"}))
        assert any(r["text"] == "The person lives in Bentonville" for r in out["results"])
        assert set(granted.bearers) == {CRED}

    @pytest.mark.parametrize("malformed", [
        "mem-" + "1f" * 31,          # too short
        "mem-" + "1f" * 32 + "00",   # too long
        "mem-" + "zz" * 32,          # not hex
        "1f" * 32,                   # no prefix
        "Bearer " + "mem-" + "1f" * 32,
    ])
    def test_a_malformed_credential_is_never_sent_and_clears_the_old_one(
        self, granted, desk, ym, malformed
    ):
        desk.recall("first turn")
        sent = len(granted.bearers)
        with pytest.raises(ValueError) as e:
            ym.set_desktop_credential(SK, malformed, granted.url)
        assert malformed not in str(e.value) and CRED not in str(e.value)
        with pytest.raises(ym.YantrikMemoryNotGranted):
            desk.recall("after a bad handover")
        assert len(granted.bearers) == sent

    def test_a_session_key_is_required(self, ym):
        for bad in ("", "   ", None):
            with pytest.raises(ValueError):
                ym.set_desktop_credential(bad, CRED, None)  # type: ignore[arg-type]

    def test_one_registry_however_many_times_the_plugin_is_loaded(
        self, ym, memory_server, client_module, monkeypatch
    ):
        # Hermes may load a copied plugin under its own module name while the adapter imports the
        # pip package: two module objects. The grant must reach both.
        import importlib.util
        name = f"{ym.__package__}.yantrik_memory_second_copy"
        spec = importlib.util.spec_from_file_location(name, ym.__file__)
        second = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, second)
        spec.loader.exec_module(second)
        assert second is not ym
        memory_server.credentials[CRED] = ALL_GRANTS
        second.set_desktop_credential(SK, CRED, memory_server.url)
        assert ym.desktop_grant(SK) is not None
        c = ym.YantrikMemoryClient(
            client_module.YantrikDBConfig.from_env(), platform="yantrik", session_keys=(SK,),
        )
        c.recall("anything")
        c.close()
        assert set(memory_server.bearers) == {CRED}

    def test_a_credential_in_the_environment_is_ignored(self, memory_server, desk, ym, monkeypatch):
        memory_server.credentials[CRED] = ALL_GRANTS
        monkeypatch.setenv("YANTRIK_MEMORY_CREDENTIAL", CRED)
        with pytest.raises(ym.YantrikMemoryNotGranted):
            desk.recall("anything")
        assert memory_server.bearers == []


class TestNoMachineTokenUnderTheDesktop:
    def test_an_older_machine_still_uses_the_token_file(self, client, memory_server):
        client.recall("anything")
        assert set(memory_server.bearers) == {TOKEN}

    def test_once_the_registry_is_used_the_token_file_is_never_used(self, client, memory_server, ym):
        ym.set_desktop_credential(SK, None, None)  # the harness exists, even with nothing granted
        with pytest.raises(ym.YantrikMemoryNotGranted):
            client.recall("anything")
        assert memory_server.bearers == []

    def test_a_revoked_grant_means_no_access_not_the_token(self, granted, desk, ym, memory_server):
        desk.recall("first turn")
        ym.set_desktop_credential(SK, None, None)
        with pytest.raises(ym.YantrikMemoryNotGranted):
            desk.recall("after revoking")
        assert TOKEN not in memory_server.bearers

    def test_a_yantrik_session_marks_the_harness(self, ym, client_module, client, memory_server):
        ym.YantrikMemoryClient(
            client_module.YantrikDBConfig.from_env(), platform="yantrik", session_keys=(SK,),
        ).close()
        with pytest.raises(ym.YantrikMemoryNotGranted):
            client.recall("anything")
        assert memory_server.bearers == []

    def test_a_gateway_that_knows_the_yantrik_platform_marks_the_harness(
        self, ym, client, memory_server, monkeypatch
    ):
        import types as _types
        entry = _types.SimpleNamespace(name="yantrik")
        registry = _types.SimpleNamespace(plugin_entries=lambda: [entry])
        fake = _types.ModuleType("gateway.platform_registry")
        fake.platform_registry = registry
        monkeypatch.setitem(sys.modules, "gateway.platform_registry", fake)
        assert ym.desktop_harness_present()
        with pytest.raises(ym.YantrikMemoryNotGranted):
            client.recall("anything")
        assert memory_server.bearers == []

    def test_with_neither_a_grant_nor_a_token_nothing_is_sent(self, client, memory_server, yantrik_env, ym):
        yantrik_env.unlink()
        with pytest.raises(ym.YantrikMemoryNotGranted) as e:
            client.recall("anything")
        assert str(e.value) == (
            "this Yantrik machine has not granted Hermes its memory yet: "
            "grant it in Settings › Minds"
        )
        assert isinstance(e.value, ym.YantrikMemoryUnsupported)
        assert memory_server.bearers == []

    def test_not_granted_yet_is_a_plain_tool_error_and_spares_the_breaker(self, desk_provider):
        for _ in range(10):
            out = desk_provider.handle_tool_call("yantrikdb_recall", {"query": "where do they live"})
            assert "has not granted Hermes its memory yet" in out
        assert desk_provider._failure_count == 0
        assert not desk_provider._breaker_open()


class TestSecrecy:
    def test_the_credential_is_redacted_in_repr(self, granted, desk, ym):
        assert CRED not in repr(desk)
        assert "mem-<redacted>" in repr(desk)
        grant = ym.desktop_grant(SK)
        assert CRED not in repr(grant) and CRED not in str(grant) and CRED not in f"{grant}"
        bearer = ym.Bearer(CRED, "credential")
        assert CRED not in repr(bearer) and CRED not in str(bearer)

    def test_the_credential_is_redacted_in_errors(self, granted, desk, client_module):
        with pytest.raises(client_module.YantrikDBError) as e:
            desk._call("echo_bearer", {})
        assert granted.bearers[-1] == CRED  # it was sent, and the server said it back
        assert CRED not in str(e.value)
        assert "mem-<redacted>" in str(e.value)


class TestMemoryAddress:
    @pytest.mark.parametrize("url", [
        "http://127.0.0.1:7440/mcp",
        "http://127.0.0.1:7440",
        "http://127.8.9.10:7440/mcp",
        "http://[::1]:7440/mcp",
        "http://localhost:7440/mcp",
        "unix:/run/yantrik-mind/1000/memory.sock",
        "unix:///run/yantrik-mind/1000/memory.sock",
    ])
    def test_loopback_and_the_socket_are_accepted(self, ym, url):
        ym.set_desktop_credential(SK, CRED, url)
        assert ym.desktop_grant(SK) is not None

    @pytest.mark.parametrize("url", [
        "http://192.168.4.65:7440/mcp",
        "http://yantrik.local:7440/mcp",
        "https://127.0.0.1:7440/mcp",
        "https://example.com/mcp",
        "http://127.0.0.1.evil.example/mcp",
        "http://user:pw@127.0.0.1:7440/mcp",
        "ftp://127.0.0.1/mcp",
        "unix:relative/memory.sock",
        "127.0.0.1:7440",
        "http://127.0.0.1:notaport/mcp",
    ])
    def test_anything_else_is_refused_and_clears_the_grant(self, granted, desk, ym, url):
        with pytest.raises(ValueError):
            ym.set_desktop_credential(SK, CRED, url)
        assert ym.desktop_grant(SK) is None
        with pytest.raises(ym.YantrikMemoryNotGranted):
            desk.recall("anything")

    def test_a_configured_address_off_this_machine_is_refused(self, ym, client_module, monkeypatch):
        monkeypatch.setenv("YANTRIK_MEMORY_URL", "http://192.168.4.65:7440/mcp")
        c = ym.YantrikMemoryClient(client_module.YantrikDBConfig.from_env())
        with pytest.raises(ym.YantrikMemoryUnsupported):
            c.recall("anything")


class TestGrants:
    def _grant(self, memory: FakeMemory, ym, grants: set[str]) -> None:
        memory.credentials[CRED] = frozenset(grants)
        ym.set_desktop_credential(SK, CRED, memory.url)

    def test_a_missing_grant_names_the_grant_and_is_not_fatal(self, desk, memory_server, ym):
        self._grant(memory_server, ym, {"recall_ordinary"})
        with pytest.raises(ym.YantrikMemoryNotGranted) as e:
            desk.remember("The person prefers tea")
        assert (e.value.tool, e.value.grant) == ("believe", "believe")
        assert "has not granted Hermes `believe`" in str(e.value)
        # The session survives: what is granted still works.
        assert desk.recall("tea")["results"] is not None

    @pytest.mark.parametrize("grants, call, grant", [
        ({"believe", "remember"}, lambda c: c.recall("x"), "recall_ordinary"),
        ({"believe", "remember"}, lambda c: c.conflicts(), "recall_ordinary"),
        ({"recall_ordinary", "remember"}, lambda c: c.relate("a", "b", "knows"), "believe"),
        ({"recall_ordinary", "believe"}, lambda c: c.remember("note", memory_type="episodic"), "remember"),
    ])
    def test_each_tool_is_refused_for_its_own_grant(self, desk, memory_server, ym, grants, call, grant):
        self._grant(memory_server, ym, grants)
        with pytest.raises(ym.YantrikMemoryNotGranted) as e:
            call(desk)
        assert e.value.grant == grant

    def test_a_missing_grant_is_a_clear_tool_error_that_spares_the_breaker(
        self, desk_provider, memory_server, ym
    ):
        self._grant(memory_server, ym, {"recall_ordinary"})
        for _ in range(10):
            out = desk_provider.handle_tool_call("yantrikdb_remember", {"text": "The person prefers tea"})
            assert "has not granted Hermes `believe`" in out
        assert desk_provider._failure_count == 0
        assert not desk_provider._breaker_open()
        out = json.loads(desk_provider.handle_tool_call("yantrikdb_recall", {"query": "where do they live"}))
        assert any(r["text"] == "The person lives in Bentonville" for r in out["results"])

    def test_background_writes_refused_for_a_grant_spare_the_breaker(
        self, desk_provider, memory_server, ym
    ):
        self._grant(memory_server, ym, {"recall_ordinary"})
        for i in range(8):
            desk_provider.sync_turn(f"I prefer tea number {i}.", "Noted.")
            _wait(desk_provider._sync_thread)
            desk_provider.on_memory_write("add", "user", f"Prefers tea {i}")
        time.sleep(0.3)  # on_memory_write's thread is not kept
        assert desk_provider._failure_count == 0
        assert not desk_provider._breaker_open()


# ---------------------------------------------------------------------------
# Reading and writing the shared memory
# ---------------------------------------------------------------------------

class TestSharedMemoryPaths:
    def test_recall_asks_for_everything_with_no_namespace(self, granted, desk):
        for memory_type in (None, "semantic", "episodic", "belief"):
            desk.recall("anything", namespace="hermes:home:hermes", memory_type=memory_type)
        sent = granted.calls_to("recall")
        assert len(sent) == 4
        assert all(a["include"] == "all" and "namespace" not in a for a in sent)

    def test_prefetch_is_one_recall_of_everything(self, granted, desk_provider):
        granted.calls.clear()
        desk_provider.queue_prefetch("where does the person live", session_id="20261002_desk")
        _wait(desk_provider._prefetch_thread)
        sent = granted.calls_to("recall")
        assert len(sent) == 1
        assert sent[0]["include"] == "all" and "namespace" not in sent[0]
        block = desk_provider.prefetch("where does the person live", session_id="20261002_desk")
        assert "The person lives in Bentonville" in block

    def test_memory_type_narrows_what_comes_back(self, granted, desk):
        granted.tool("remember", {"text": "Ran the backup", "memory_type": "episodic"})
        kinds = lambda mt: {h["memory_type"] for h in desk.recall("x", memory_type=mt)["results"]}  # noqa: E731
        assert kinds("belief") == {"belief"}
        assert kinds("episodic") == {"episodic"}
        assert "belief" in kinds("semantic")

    def test_the_remember_tool_writes_a_fact_as_a_belief(self, granted, desk_provider):
        out = json.loads(desk_provider.handle_tool_call(
            "yantrikdb_remember", {"text": "The person's daughter is called Mira"}))
        believed = granted.calls_to("believe")
        assert [b["statement"] for b in believed] == ["The person's daughter is called Mira"]
        assert believed[0]["direction"] == "supports"
        assert believed[0]["provenance"] == "told"
        assert "namespace" not in believed[0] and "written_by" not in believed[0]
        assert granted.calls_to("remember") == []
        assert out["rid"].startswith("belief:")

    def test_a_fact_from_a_turn_is_a_belief_and_the_turn_itself_a_note(self, granted, desk_provider):
        desk_provider.sync_turn("I prefer tabs over spaces.", "Noted.")
        _wait(desk_provider._sync_thread)
        believed = granted.calls_to("believe")
        assert [b["statement"] for b in believed] == ["user prefers tabs"]
        assert believed[0]["provenance"] == "extracted"
        assert believed[0]["strength"] < 1.0
        notes = granted.calls_to("remember")
        assert [n["text"] for n in notes] == ["I prefer tabs over spaces."]
        assert notes[0]["memory_type"] == "episodic"
        assert notes[0]["namespace"] == desk_provider._namespace

    def test_hermes_user_profile_writes_are_beliefs(self, granted, desk_provider):
        desk_provider.on_memory_write("add", "user", "Pranab prefers dark mode")
        deadline = time.time() + 5
        while not granted.calls_to("believe") and time.time() < deadline:
            time.sleep(0.02)
        assert [b["statement"] for b in granted.calls_to("believe")] == ["Pranab prefers dark mode"]

    def test_a_delegation_result_stays_hermes_own_note(self, granted, desk_provider):
        desk_provider.on_delegation("Sort the photos", "Sorted 120 photos by year", child_session_id="c1")
        deadline = time.time() + 5
        while not granted.calls_to("remember") and time.time() < deadline:
            time.sleep(0.02)
        assert granted.calls_to("believe") == []
        note = granted.calls_to("remember")[0]
        assert note["memory_type"] == "episodic" and note["namespace"] == desk_provider._namespace

    def test_a_belief_hermes_wrote_can_be_forgotten_by_its_statement(self, granted, desk):
        rid = desk.remember("The person likes hiking")["rid"]
        assert desk.forget(rid)["found"] is True
        assert granted.forgotten == [("belief", "The person likes hiking")]


# ---------------------------------------------------------------------------
# The person's unix socket
# ---------------------------------------------------------------------------

class TestUnixSocket:
    SOCK = "/run/yantrik-mind/1000/memory.sock"

    def test_a_unix_url_builds_a_unix_socket_transport(self, granted, desk, ym):
        ym.set_desktop_credential(SK, CRED, f"unix:{self.SOCK}")
        ep, _ = desk._current()
        assert ep.socket_path == self.SOCK
        adapter = desk._http.get_adapter(ep.mcp_url)
        assert isinstance(adapter, ym.UnixSocketAdapter)
        assert adapter.socket_path == self.SOCK
        assert adapter.pool.conn_kw["socket_path"] == self.SOCK
        assert adapter.pool.ConnectionCls.__name__ == "_UnixHTTPConnection"
        # Nothing addressed to TCP goes through it.
        assert not isinstance(desk._http.get_adapter("http://127.0.0.1:7440/mcp"), ym.UnixSocketAdapter)

    @pytest.mark.parametrize("url", ["unix:/run/x/memory.sock", "unix:///run/x/memory.sock"])
    def test_both_spellings_of_a_socket_path(self, ym, url):
        assert ym.endpoint_for(url).socket_path == "/run/x/memory.sock"

    def test_the_machine_token_is_never_sent_on_the_socket(self, client, ym, monkeypatch):
        monkeypatch.setenv("YANTRIK_MEMORY_URL", f"unix:{self.SOCK}")
        with pytest.raises(ym.YantrikMemoryNotGranted):
            client.recall("anything")  # the token file is there; the socket does not take it

    def test_a_changed_address_drops_the_session(self, granted, desk, ym):
        desk.recall("first")
        assert desk._session_id
        ym.set_desktop_credential(SK, CRED, f"unix:{self.SOCK}")
        desk._current()
        assert desk._session_id is None

    @pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="no unix sockets on this platform")
    def test_mcp_over_a_real_unix_socket(self, granted, ym, desk):
        import tempfile
        sock_dir = tempfile.mkdtemp(prefix="ym-")  # short: AF_UNIX paths are capped near 108 bytes
        path = os.path.join(sock_dir, "memory.sock")
        granted.socket_only = True
        server = socketserver.ThreadingUnixStreamServer(path, _handler(granted))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        ym.set_desktop_credential(SK, CRED, f"unix:{path}")
        try:
            assert desk.health()["served_by"] == "yantrik-mind"
            assert desk.remember("The person’s café is Zoë’s")["rid"].startswith("belief:")
            hits = desk.recall("café")["results"]
            assert any(h["text"] == "The person’s café is Zoë’s" for h in hits)
            assert set(granted.bearers) == {CRED}
        finally:
            desk.close()
            server.shutdown()
            server.server_close()
            with contextlib.suppress(OSError):
                os.unlink(path)
                os.rmdir(sock_dir)
