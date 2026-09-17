"""Yantrik mode: the plugin over a Yantrik machine's shared memory server.

The server here is a small fake that speaks the real transport — MCP streamable
HTTP with SSE replies, session ids and a bearer token — so these tests exercise
the client's actual wire handling (session setup, stream parsing, reopening a
session after the memory changes owner) rather than a mocked session object.
Its tools mirror the shapes the real server (yantrik-mind's mind-memory-mcp)
returns.
"""

from __future__ import annotations

import inspect
import itertools
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

TOKEN = "a" * 64


class FakeMemory:
    """The memory server's state, and its tools."""

    def __init__(self) -> None:
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

    def tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, args))
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
            if self.headers.get("Authorization") != f"Bearer {memory.token}":
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
            try:
                data = memory.tool(params["name"], params.get("arguments") or {})
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


@pytest.fixture
def yantrik_env(memory_server, tmp_path, monkeypatch):
    token_file = tmp_path / "yantrik-memory.token"
    token_file.write_text(TOKEN, encoding="utf-8")
    monkeypatch.setenv("YANTRIKDB_MODE", "yantrik")
    monkeypatch.setenv("YANTRIK_MEMORY_URL", memory_server.url)
    monkeypatch.setenv("YANTRIK_MEMORY_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return token_file


@pytest.fixture
def ym(plugin, yantrik_env):
    """The yantrik_memory module, imported under the test package."""
    import importlib
    return importlib.import_module(f"{plugin[0].__name__}.yantrik_memory")


@pytest.fixture
def client(ym, client_module):
    c = ym.YantrikMemoryClient(client_module.YantrikDBConfig.from_env())
    yield c
    c.close()


@pytest.fixture
def provider(provider_module, yantrik_env, ym):
    p = provider_module.YantrikDBMemoryProvider()
    p.initialize("sess-1", agent_workspace="home", agent_identity="hermes", platform="cli")
    yield p
    p.shutdown()


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

    def test_a_memory_written_comes_back_with_its_metadata(self, client, memory_server):
        rid = client.remember(
            "The person keeps their bike in the garage",
            namespace="hermes:home:hermes", importance=0.5,
            metadata={"source": "extracted", "session_id": "s1"},
        )["rid"]
        sent = memory_server.calls[-1][1]
        assert sent["source"] == "hermes"
        assert sent["namespace"] == "hermes:home:hermes"

        hits = client.recall("bike", namespace="hermes:home:hermes", top_k=10)["results"]
        mine = next(h for h in hits if h["rid"] == rid)
        assert mine["metadata"]["source"] == "extracted"
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
        rid = client.remember("Temporary")["rid"]
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
    def test_available_iff_the_token_file_is_readable(self, provider_module, yantrik_env):
        assert provider_module.YantrikDBMemoryProvider().is_available() is True
        yantrik_env.unlink()
        assert provider_module.YantrikDBMemoryProvider().is_available() is False

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

    def test_session_end_does_not_count_missing_consolidation_as_failure(self, provider):
        provider.on_session_end([{"role": "user", "content": "bye"}])
        assert provider._failure_count == 0

    def test_system_prompt_block_renders_without_the_optional_features(self, provider):
        block = provider.system_prompt_block()
        assert isinstance(block, str)
        assert provider._failure_count == 0
