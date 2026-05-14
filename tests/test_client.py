"""Tests for the YantrikDB HTTP client (client.py).

Drive the client against a mocked ``requests.Session`` so tests never
touch the network. Each test asserts one of: config loading, request
formation (URL / method / headers / body), or error-to-exception mapping.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
import requests


def _make_response(
    status: int = 200,
    body: dict | None = None,
    text: str | None = None,
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    if body is not None:
        payload = json.dumps(body)
        resp.json.return_value = body
        resp.content = payload.encode()
        resp.text = payload
    elif text is not None:
        resp.json.side_effect = ValueError("not json")
        resp.content = text.encode()
        resp.text = text
    else:
        resp.content = b""
        resp.text = ""
    return resp


@pytest.fixture
def mock_session() -> MagicMock:
    return MagicMock(spec=requests.Session)


@pytest.fixture
def client(client_module, mock_session):
    cfg = client_module.YantrikDBConfig(
        url="http://test:7438",
        token="ydb_test",
        namespace="hermes",
        top_k=10,
    )
    return client_module.YantrikDBClient(cfg, session=mock_session)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class TestConfigFromEnv:
    def test_defaults_when_env_empty(self, client_module):
        cfg = client_module.YantrikDBConfig.from_env()
        assert cfg.url == "http://localhost:7438"
        assert cfg.token == ""
        assert cfg.namespace == "hermes"
        assert cfg.top_k == 10

    def test_reads_env_vars(self, client_module, monkeypatch):
        monkeypatch.setenv("YANTRIKDB_URL", "http://remote:7438/")
        monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_abc")
        monkeypatch.setenv("YANTRIKDB_NAMESPACE", "myns")
        monkeypatch.setenv("YANTRIKDB_TOP_K", "25")
        cfg = client_module.YantrikDBConfig.from_env()
        assert cfg.url == "http://remote:7438"  # trailing slash stripped
        assert cfg.token == "ydb_abc"
        assert cfg.namespace == "myns"
        assert cfg.top_k == 25

    def test_bad_top_k_falls_back_to_default(self, client_module, monkeypatch):
        monkeypatch.setenv("YANTRIKDB_TOP_K", "not-a-number")
        cfg = client_module.YantrikDBConfig.from_env()
        assert cfg.top_k == 10

    def test_reads_timeout_and_retry_env(self, client_module, monkeypatch):
        monkeypatch.setenv("YANTRIKDB_CONNECT_TIMEOUT", "2.5")
        monkeypatch.setenv("YANTRIKDB_READ_TIMEOUT", "45")
        monkeypatch.setenv("YANTRIKDB_RETRY_TOTAL", "7")
        monkeypatch.setenv("YANTRIKDB_MAX_TEXT_LEN", "5000")
        cfg = client_module.YantrikDBConfig.from_env()
        assert cfg.connect_timeout == 2.5
        assert cfg.read_timeout == 45.0
        assert cfg.retry_total == 7
        assert cfg.max_text_len == 5000

    def test_bad_timeout_falls_back_to_default(self, client_module, monkeypatch):
        monkeypatch.setenv("YANTRIKDB_READ_TIMEOUT", "slow")
        cfg = client_module.YantrikDBConfig.from_env()
        assert cfg.read_timeout == 15.0


class TestConfigLoad:
    def test_json_overrides_env(self, client_module, monkeypatch, tmp_path):
        monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_env")
        (tmp_path / "yantrikdb.json").write_text(json.dumps({
            "token": "ydb_json",
            "namespace": "override",
            "top_k": 33,
        }))
        cfg = client_module.YantrikDBConfig.load(tmp_path)
        assert cfg.token == "ydb_json"
        assert cfg.namespace == "override"
        assert cfg.top_k == 33

    def test_partial_json_keeps_env_for_missing_keys(
        self, client_module, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_env")
        (tmp_path / "yantrikdb.json").write_text(json.dumps({"namespace": "only"}))
        cfg = client_module.YantrikDBConfig.load(tmp_path)
        assert cfg.token == "ydb_env"
        assert cfg.namespace == "only"

    def test_missing_file_uses_env(self, client_module, monkeypatch, tmp_path):
        monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_env")
        cfg = client_module.YantrikDBConfig.load(tmp_path)
        assert cfg.token == "ydb_env"

    def test_corrupt_json_falls_back_to_env(
        self, client_module, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_env")
        (tmp_path / "yantrikdb.json").write_text("not json {{{")
        cfg = client_module.YantrikDBConfig.load(tmp_path)
        assert cfg.token == "ydb_env"

    def test_empty_values_in_json_ignored(
        self, client_module, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_env")
        (tmp_path / "yantrikdb.json").write_text(json.dumps({
            "token": "",
            "namespace": None,
        }))
        cfg = client_module.YantrikDBConfig.load(tmp_path)
        assert cfg.token == "ydb_env"
        assert cfg.namespace == "hermes"

    def test_json_coerces_numeric_fields(
        self, client_module, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_env")
        (tmp_path / "yantrikdb.json").write_text(json.dumps({
            "top_k": "25",
            "retry_total": "9",
            "read_timeout": "30.5",
        }))
        cfg = client_module.YantrikDBConfig.load(tmp_path)
        assert cfg.top_k == 25
        assert cfg.retry_total == 9
        assert cfg.read_timeout == 30.5


# ---------------------------------------------------------------------------
# Request formation
# ---------------------------------------------------------------------------

class TestRequestFormation:
    def test_remember_url_and_body(self, client, mock_session):
        mock_session.request.return_value = _make_response(200, {"rid": "r1"})
        result = client.remember("hello", importance=0.7, domain="work")
        assert mock_session.request.call_args.args == (
            "POST", "http://test:7438/v1/remember",
        )
        body = mock_session.request.call_args.kwargs["json"]
        assert body["text"] == "hello"
        assert body["importance"] == 0.7
        assert body["domain"] == "work"
        assert body["namespace"] == "hermes"
        headers = mock_session.request.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer ydb_test"
        assert headers["Content-Type"] == "application/json"
        assert result == {"rid": "r1"}

    def test_remember_includes_metadata(self, client, mock_session):
        mock_session.request.return_value = _make_response(200, {"rid": "r1"})
        client.remember("x", metadata={"session_id": "s1"})
        body = mock_session.request.call_args.kwargs["json"]
        assert body["metadata"] == {"session_id": "s1"}

    def test_remember_skips_optional_fields_when_absent(
        self, client, mock_session,
    ):
        mock_session.request.return_value = _make_response(200, {"rid": "r"})
        client.remember("x")
        body = mock_session.request.call_args.kwargs["json"]
        assert "domain" not in body
        assert "memory_type" not in body
        assert "metadata" not in body

    def test_recall_body(self, client, mock_session):
        mock_session.request.return_value = _make_response(200, {"results": []})
        client.recall("what about X?", top_k=5)
        body = mock_session.request.call_args.kwargs["json"]
        assert body == {
            "query": "what about X?",
            "namespace": "hermes",
            "top_k": 5,
        }

    def test_forget(self, client, mock_session):
        mock_session.request.return_value = _make_response(
            200, {"rid": "r1", "found": True},
        )
        client.forget("r1")
        assert mock_session.request.call_args.args == (
            "POST", "http://test:7438/v1/forget",
        )
        assert mock_session.request.call_args.kwargs["json"] == {"rid": "r1"}

    def test_think_default_flags(self, client, mock_session):
        mock_session.request.return_value = _make_response(
            200, {"consolidation_count": 3},
        )
        client.think()
        body = mock_session.request.call_args.kwargs["json"]
        assert body["run_consolidation"] is True
        assert body["run_conflict_scan"] is True
        assert body["run_pattern_mining"] is False
        assert body["run_personality"] is False
        assert "consolidation_limit" not in body

    def test_think_with_pattern_mining(self, client, mock_session):
        mock_session.request.return_value = _make_response(200, {})
        client.think(run_pattern_mining=True, consolidation_limit=100)
        body = mock_session.request.call_args.kwargs["json"]
        assert body["run_pattern_mining"] is True
        assert body["consolidation_limit"] == 100

    def test_conflicts_is_get_with_no_body(self, client, mock_session):
        mock_session.request.return_value = _make_response(200, {"conflicts": []})
        client.conflicts()
        assert mock_session.request.call_args.args == (
            "GET", "http://test:7438/v1/conflicts",
        )
        assert mock_session.request.call_args.kwargs["json"] is None

    def test_relate(self, client, mock_session):
        mock_session.request.return_value = _make_response(200, {"edge_id": "e1"})
        client.relate("Alice", "Acme", "works_at", weight=0.9)
        body = mock_session.request.call_args.kwargs["json"]
        assert body == {
            "entity": "Alice",
            "target": "Acme",
            "relationship": "works_at",
            "weight": 0.9,
        }

    def test_health(self, client, mock_session):
        mock_session.request.return_value = _make_response(200, {"status": "ok"})
        result = client.health()
        assert mock_session.request.call_args.args == (
            "GET", "http://test:7438/v1/health",
        )
        assert result == {"status": "ok"}

    def test_stats(self, client, mock_session):
        mock_session.request.return_value = _make_response(
            200, {"active_memories": 42, "open_conflicts": 1},
        )
        result = client.stats()
        assert mock_session.request.call_args.args == (
            "GET", "http://test:7438/v1/stats",
        )
        assert mock_session.request.call_args.kwargs["json"] is None
        assert mock_session.request.call_args.kwargs["params"] is None
        assert result == {"active_memories": 42, "open_conflicts": 1}

    def test_stats_with_namespace(self, client, mock_session):
        mock_session.request.return_value = _make_response(
            200, {"active_memories": 42, "open_conflicts": 1},
        )
        result = client.stats(namespace="hermes:workspace:coder")
        assert mock_session.request.call_args.args == (
            "GET", "http://test:7438/v1/stats",
        )
        assert mock_session.request.call_args.kwargs["json"] is None
        assert mock_session.request.call_args.kwargs["params"] == {
            "namespace": "hermes:workspace:coder",
        }
        assert result == {"active_memories": 42, "open_conflicts": 1}

    def test_resolve_conflict_keep_winner(self, client, mock_session):
        mock_session.request.return_value = _make_response(
            200, {"conflict_id": "c1", "strategy": "keep_winner"},
        )
        client.resolve_conflict("c1", strategy="keep_winner", winner_rid="r2")
        assert mock_session.request.call_args.args == (
            "POST", "http://test:7438/v1/conflicts/c1/resolve",
        )
        body = mock_session.request.call_args.kwargs["json"]
        assert body == {"strategy": "keep_winner", "winner_rid": "r2"}

    def test_resolve_conflict_merge(self, client, mock_session):
        mock_session.request.return_value = _make_response(200, {})
        client.resolve_conflict(
            "c1",
            strategy="merge",
            new_text="Unified fact",
            resolution_note="merged contradictory claims",
        )
        body = mock_session.request.call_args.kwargs["json"]
        assert body == {
            "strategy": "merge",
            "new_text": "Unified fact",
            "resolution_note": "merged contradictory claims",
        }

    def test_remember_truncates_oversize_text(
        self, client_module, mock_session,
    ):
        cfg = client_module.YantrikDBConfig(
            url="http://test:7438",
            token="ydb_test",
            max_text_len=50,
        )
        c = client_module.YantrikDBClient(cfg, session=mock_session)
        mock_session.request.return_value = _make_response(200, {"rid": "r1"})
        long_text = "word " * 200
        c.remember(long_text)
        sent = mock_session.request.call_args.kwargs["json"]["text"]
        assert len(sent) <= 50
        assert "…[truncated]" in sent


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------

class TestErrorMapping:
    def test_401(self, client, client_module, mock_session):
        mock_session.request.return_value = _make_response(
            401, {"error": "invalid token"},
        )
        with pytest.raises(client_module.YantrikDBAuthError):
            client.remember("x")

    def test_403(self, client, client_module, mock_session):
        mock_session.request.return_value = _make_response(
            403, {"error": "forbidden"},
        )
        with pytest.raises(client_module.YantrikDBAuthError):
            client.remember("x")

    def test_400(self, client, client_module, mock_session):
        mock_session.request.return_value = _make_response(
            400, {"error": "bad text"},
        )
        with pytest.raises(client_module.YantrikDBClientError):
            client.remember("")

    def test_404(self, client, client_module, mock_session):
        mock_session.request.return_value = _make_response(
            404, {"error": "not found"},
        )
        with pytest.raises(client_module.YantrikDBClientError):
            client.forget("missing")

    def test_429_is_transient(self, client, client_module, mock_session):
        mock_session.request.return_value = _make_response(
            429, {"error": "rate limit"},
        )
        with pytest.raises(client_module.YantrikDBTransientError):
            client.recall("q")

    def test_503_is_transient(self, client, client_module, mock_session):
        mock_session.request.return_value = _make_response(
            503, {"error": "load shed"},
        )
        with pytest.raises(client_module.YantrikDBTransientError):
            client.recall("q")

    def test_500_is_server_error(self, client, client_module, mock_session):
        mock_session.request.return_value = _make_response(500, {"error": "boom"})
        with pytest.raises(client_module.YantrikDBServerError):
            client.recall("q")

    def test_timeout_is_transient(self, client, client_module, mock_session):
        mock_session.request.side_effect = requests.Timeout("slow")
        with pytest.raises(client_module.YantrikDBTransientError):
            client.remember("x")

    def test_connection_error_is_transient(
        self, client, client_module, mock_session,
    ):
        mock_session.request.side_effect = requests.ConnectionError("refused")
        with pytest.raises(client_module.YantrikDBTransientError):
            client.remember("x")

    def test_empty_body_returns_empty_dict(self, client, mock_session):
        mock_session.request.return_value = _make_response(200)
        assert client.health() == {}

    def test_non_json_body_wrapped_in_raw(self, client, mock_session):
        mock_session.request.return_value = _make_response(200, text="pong")
        assert client.health() == {"raw": "pong"}

    def test_error_without_json_body(self, client, client_module, mock_session):
        resp = MagicMock()
        resp.status_code = 500
        resp.json.side_effect = ValueError
        resp.text = "internal explosion"
        resp.content = b"internal explosion"
        mock_session.request.return_value = resp
        with pytest.raises(client_module.YantrikDBServerError) as exc:
            client.health()
        assert "internal explosion" in str(exc.value)


# ---------------------------------------------------------------------------
# truncate_text helper
# ---------------------------------------------------------------------------

class TestTruncateText:
    def test_short_text_unchanged(self, client_module):
        assert client_module.truncate_text("hello", 100) == "hello"

    def test_exact_length_unchanged(self, client_module):
        text = "x" * 50
        assert client_module.truncate_text(text, 50) == text

    def test_truncates_with_marker(self, client_module):
        text = "word " * 100  # 500 chars
        result = client_module.truncate_text(text, 100)
        assert len(result) <= 100
        assert result.endswith("…[truncated]")

    def test_truncates_at_word_boundary(self, client_module):
        text = "alpha beta gamma delta epsilon zeta eta theta iota"
        result = client_module.truncate_text(text, 25)
        # Should cut cleanly, not mid-word
        before_marker = result.rsplit(" …[truncated]", 1)[0]
        assert " " in before_marker or before_marker in text

    def test_zero_max_len_unchanged(self, client_module):
        assert client_module.truncate_text("hello", 0) == "hello"
