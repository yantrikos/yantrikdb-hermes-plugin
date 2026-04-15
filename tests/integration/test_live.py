"""Live integration test against a real yantrikdb-server.

Skipped by default. To run, export a token for a running server:

    YANTRIKDB_INTEGRATION_URL=http://192.168.4.140:7438 \\
    YANTRIKDB_INTEGRATION_TOKEN=ydb_live... \\
    python -m pytest tests/integration/ -v

The test walks the end-to-end flow the way a Hermes session would use the
plugin: initialize, remember, recall (with why_retrieved), relate, think,
conflicts, stats, forget. Each step asserts the structured response shape
we depend on in the provider.

This is the `test against a live server` checkpoint in HANDOFF §8 — PRs
should not go out until this passes at least once.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

_INTEGRATION_URL = os.environ.get("YANTRIKDB_INTEGRATION_URL", "")
_INTEGRATION_TOKEN = os.environ.get("YANTRIKDB_INTEGRATION_TOKEN", "")

pytestmark = pytest.mark.skipif(
    not (_INTEGRATION_URL and _INTEGRATION_TOKEN),
    reason="YANTRIKDB_INTEGRATION_URL and YANTRIKDB_INTEGRATION_TOKEN required",
)


@pytest.fixture
def live_client(client_module):
    cfg = client_module.YantrikDBConfig(
        url=_INTEGRATION_URL.rstrip("/"),
        token=_INTEGRATION_TOKEN,
        namespace=f"hermes-plugin-it-{uuid.uuid4().hex[:8]}",
        top_k=10,
    )
    client = client_module.YantrikDBClient(cfg)
    yield client
    client.close()


class TestLiveRoundtrip:
    def test_health_returns_ok(self, live_client):
        resp = live_client.health()
        assert resp.get("status") == "ok"

    def test_end_to_end_flow(self, live_client):
        # Remember three related facts
        r1 = live_client.remember(
            "Alice is the engineering lead at Acme",
            importance=0.9, domain="people",
        )
        r2 = live_client.remember(
            "Acme uses PostgreSQL in production",
            importance=0.8, domain="architecture",
        )
        r3 = live_client.remember(
            "Alice prefers to pair on Mondays",
            importance=0.6, domain="preference",
        )
        for r in (r1, r2, r3):
            assert r.get("rid"), f"remember returned no rid: {r}"

        # Relate (knowledge graph edge)
        edge = live_client.relate("Alice", "Acme", "works_at")
        assert edge.get("edge_id")

        # Give the server a beat to index
        time.sleep(0.5)

        # Recall with explainable reasons
        recall = live_client.recall("Who is Alice?", top_k=5)
        results = recall.get("results", [])
        assert results, f"expected at least one recall hit, got: {recall}"
        first = results[0]
        assert "rid" in first
        assert "text" in first
        assert "score" in first
        # why_retrieved is the headline differentiator — verify it exists
        assert "why_retrieved" in first, (
            f"server should return why_retrieved; got: {first}"
        )

        # Introduce a contradiction for think() + conflicts to pick up
        live_client.remember(
            "Alice is the engineering lead at Beta Corp",
            importance=0.9, domain="people",
        )
        time.sleep(0.5)

        # Maintenance pass
        think = live_client.think(
            run_consolidation=True, run_conflict_scan=True,
        )
        assert "consolidation_count" in think
        assert "conflicts_found" in think
        assert "duration_ms" in think

        # Conflict listing
        conflicts = live_client.conflicts()
        assert "conflicts" in conflicts
        # Can't assert count > 0 — server may or may not detect depending on config

        # Stats
        stats = live_client.stats()
        assert "active_memories" in stats
        assert stats["active_memories"] >= 4

        # Cleanup
        for r in (r1, r2, r3):
            resp = live_client.forget(r["rid"])
            assert resp.get("rid") == r["rid"]
