"""Memory Integrity V1.1 — correction chains, supersedes links, historical recall.

Covers the tool contract Hermes actually calls::

    YantrikDBMemoryProvider.initialize()
      -> get_tool_schemas()
      -> handle_tool_call("yantrikdb_remember" / "yantrikdb_recall" / "yantrikdb_links")
      -> embedded client
      -> engine

Each test runs against a throw-away embedded database in ``tmp_path``; nothing
touches a user's store. The engine is not mocked here on purpose: the contract
under test is how real ``supersedes`` edges and ``as_of`` cut-offs behave, and a
mock would only re-assert our own assumptions.

Known boundary (asserted explicitly at the end): the `as_of` cut-off resolves
against the `supersedes` **edge** timestamp, which the engine writes a few
hundred microseconds *after* the successor record. ``as_of ==
successor.created_at`` therefore shows the state immediately before the
correction took effect — the predecessor is still current, even though the
successor already exists. That is the correct reading of "what was true at T",
not a rounding artefact: at T the correction had not been written yet.
"""

from __future__ import annotations

import datetime as dt
import json
import time

import pytest

TEXTS = ("MI1-A", "MI1-B", "MI1-C")
LOCAL_TZ = dt.timezone(dt.timedelta(hours=2))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_env(monkeypatch, tmp_path):
    """Point an embedded provider at a fresh store inside tmp_path."""
    monkeypatch.setenv("YANTRIKDB_MODE", "embedded")
    monkeypatch.setenv("YANTRIKDB_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("YANTRIKDB_NAMESPACE", "test:memory-integrity")
    monkeypatch.setenv("YANTRIKDB_SHARED_BRAIN_NAMESPACE", "")
    return tmp_path / "memory.db"


def _open(provider_module, session: str = "mi1-test"):
    provider = provider_module.YantrikDBMemoryProvider()
    provider.initialize(session, agent_workspace="ws", agent_identity="mi1")
    return provider


@pytest.fixture
def provider(provider_module, db_env):
    instance = _open(provider_module)
    try:
        yield instance
    finally:
        instance.shutdown()


def _call(provider, tool: str, args: dict) -> dict:
    return json.loads(provider.handle_tool_call(tool, args))


def _rid(envelope: dict) -> str:
    return envelope.get("data", envelope)["rid"]


def _texts(envelope: dict) -> list[str]:
    return [r["text"] for r in envelope.get("results", [])]


def _write_chain(provider) -> dict:
    """Write A -> B -> C and return rids plus stored created_at values."""
    a = _rid(_call(provider, "yantrikdb_remember", {"text": TEXTS[0], "importance": 0.9}))
    time.sleep(0.4)
    b = _rid(_call(provider, "yantrikdb_remember",
                   {"text": TEXTS[1], "importance": 0.9, "supersedes_rid": a}))
    time.sleep(0.4)
    c = _rid(_call(provider, "yantrikdb_remember",
                   {"text": TEXTS[2], "importance": 0.9, "supersedes_rid": b}))
    time.sleep(0.1)

    everything = _call(provider, "yantrikdb_recall",
                       {"query": "MI1", "top_k": 10, "include_superseded": True})
    stamps = {r["text"]: float(r["created_at"]) for r in everything["results"]
              if r.get("text") in TEXTS}
    assert set(stamps) == set(TEXTS), f"chain incomplete: {stamps}"
    return {"rids": {"A": a, "B": b, "C": c}, "stamps": stamps}


@pytest.fixture
def chain(provider):
    return _write_chain(provider)


# ---------------------------------------------------------------------------
# Contract surface
# ---------------------------------------------------------------------------

def test_schemas_expose_the_new_fields(provider):
    schemas = {s["name"]: s for s in provider.get_tool_schemas()}
    assert "yantrikdb_links" in schemas

    remember = json.dumps(schemas["yantrikdb_remember"])
    assert "supersedes_rid" in remember

    recall = json.dumps(schemas["yantrikdb_recall"])
    assert "include_superseded" in recall
    assert "as_of" in recall


def test_default_recall_returns_only_the_tail(chain, provider):
    result = _call(provider, "yantrikdb_recall", {"query": "MI1", "top_k": 10})
    assert _texts(result) == [TEXTS[2]]


def test_include_superseded_returns_the_whole_chain(chain, provider):
    result = _call(provider, "yantrikdb_recall",
                   {"query": "MI1", "top_k": 10, "include_superseded": True})
    assert set(_texts(result)) == set(TEXTS)


def test_links_are_directional(chain, provider):
    out = _call(provider, "yantrikdb_links",
                {"rid": chain["rids"]["C"], "direction": "outbound"})
    assert {link["rid"] for link in out.get("links", [])} == {chain["rids"]["B"]}

    inbound = _call(provider, "yantrikdb_links",
                    {"rid": chain["rids"]["A"], "direction": "inbound"})
    assert {link["rid"] for link in inbound.get("links", [])} == {chain["rids"]["B"]}

    both = _call(provider, "yantrikdb_links",
                 {"rid": chain["rids"]["B"], "direction": "both"})
    assert {link["rid"] for link in both.get("links", [])} == {
        chain["rids"]["A"], chain["rids"]["C"],
    }


def test_link_type_filter(chain, provider):
    filtered = _call(provider, "yantrikdb_links",
                     {"rid": chain["rids"]["C"], "link_type": "supersedes"})
    assert {link["rid"] for link in filtered.get("links", [])} == {chain["rids"]["B"]}

    empty = _call(provider, "yantrikdb_links",
                  {"rid": chain["rids"]["C"], "link_type": "contradicts"})
    assert empty.get("links", []) == []


# ---------------------------------------------------------------------------
# Historical recall: three unambiguous points, three input formats
# ---------------------------------------------------------------------------

EXPECTED = {"before-B": TEXTS[0], "between-B-and-C": TEXTS[1], "after-C": TEXTS[2]}


def _points(stamps: dict) -> dict[str, float]:
    return {
        "before-B": (stamps[TEXTS[0]] + stamps[TEXTS[1]]) / 2,
        "between-B-and-C": (stamps[TEXTS[1]] + stamps[TEXTS[2]]) / 2,
        "after-C": stamps[TEXTS[2]] + 0.2,
    }


def _formats(ts: float) -> dict[str, object]:
    return {
        "unix": ts,
        "iso-offset": dt.datetime.fromtimestamp(ts, LOCAL_TZ).isoformat(timespec="milliseconds"),
        "iso-local": dt.datetime.fromtimestamp(ts).isoformat(timespec="milliseconds"),
    }


@pytest.mark.parametrize("point", list(EXPECTED))
@pytest.mark.parametrize("fmt", ["unix", "iso-offset", "iso-local"])
def test_historical_recall_at_point_in_all_formats(chain, provider, point, fmt):
    timestamp = _points(chain["stamps"])[point]
    as_of = _formats(timestamp)[fmt]
    result = _call(provider, "yantrikdb_recall",
                   {"query": "MI1", "top_k": 10, "as_of": as_of})
    assert _texts(result) == [EXPECTED[point]], f"{point} as {fmt} ({as_of!r})"


def test_bare_date_is_local_midnight_and_z_is_utc(provider_module):
    """A bare date means local midnight; an explicit ``Z`` means UTC.

    On a UTC host the two coincide, so the local-midnight half is skipped
    there rather than asserted vacuously.
    """
    utc_midnight = dt.datetime(2026, 10, 17, tzinfo=dt.timezone.utc).timestamp()
    assert provider_module._parse_as_of("2026-10-17T00:00:00Z") == pytest.approx(utc_midnight)

    if dt.datetime.now().astimezone().utcoffset() != dt.timedelta(0):
        local_midnight = dt.datetime(2026, 10, 17).timestamp()
        assert provider_module._parse_as_of("2026-10-17") == pytest.approx(local_midnight)
        assert provider_module._parse_as_of("2026-10-17") != pytest.approx(utc_midnight)


def test_invalid_as_of_is_rejected_not_ignored(chain, provider):
    result = _call(provider, "yantrikdb_recall",
                   {"query": "MI1", "as_of": "vorgestern"})
    assert result.get("ok") is False, "unparseable as_of must fail, never fall back to now"


def test_supersedes_and_idempotency_key_are_mutually_exclusive(chain, provider):
    result = _call(provider, "yantrikdb_remember", {
        "text": "MI1-conflict",
        "supersedes_rid": chain["rids"]["A"],
        "idempotency_key": "fixed-key",
    })
    assert result.get("ok") is False


def test_backward_compatible_without_new_fields(provider):
    remembered = _call(provider, "yantrikdb_remember", {"text": "MI1-legacy", "importance": 0.9})
    assert _rid(remembered)
    recalled = _call(provider, "yantrikdb_recall", {"query": "MI1-legacy", "top_k": 5})
    assert "MI1-legacy" in _texts(recalled)


def test_chain_persists_across_reopen(provider_module, db_env):
    first = _open(provider_module, "mi1-first")
    try:
        chain = _write_chain(first)
    finally:
        first.shutdown()

    second = _open(provider_module, "mi1-second")
    try:
        result = _call(second, "yantrikdb_recall", {"query": "MI1", "top_k": 10})
        assert _texts(result) == [TEXTS[2]]
        links = _call(second, "yantrikdb_links",
                      {"rid": chain["rids"]["C"], "direction": "outbound"})
        assert {link["rid"] for link in links.get("links", [])} == {chain["rids"]["B"]}
    finally:
        second.shutdown()


def test_exact_successor_timestamp_is_not_yet_superseded(chain, provider):
    """Documented boundary, not a bug.

    ``as_of == C.created_at`` describes the instant the record was written,
    which is *before* the ``supersedes`` edge existed. The predecessor is
    therefore still current and C already visible — exactly what was true at
    that moment. Callers who mean "after the correction landed" query at the
    edge timestamp (see the next test) or later.
    """
    result = _call(provider, "yantrikdb_recall",
                   {"query": "MI1", "top_k": 10, "as_of": chain["stamps"][TEXTS[2]]})
    got = _texts(result)
    assert TEXTS[2] in got
    assert TEXTS[0] not in got


def test_cutoff_is_the_edge_timestamp_not_the_record(chain, provider):
    """The takeover instant is the edge's timestamp, written just after C."""
    links = _call(provider, "yantrikdb_links",
                  {"rid": chain["rids"]["C"], "direction": "outbound"})
    edges = [l for l in links.get("links", []) if l.get("link_type") == "supersedes"]
    assert edges, "expected a supersedes edge on C"
    edge_ts = edges[0]["created_at"]

    c_created = chain["stamps"][TEXTS[2]]
    assert edge_ts > c_created, "the edge is written after the record it links"

    before = _texts(_call(provider, "yantrikdb_recall",
                          {"query": "MI1", "top_k": 10, "as_of": c_created}))
    at_edge = _texts(_call(provider, "yantrikdb_recall",
                           {"query": "MI1", "top_k": 10, "as_of": edge_ts}))

    # Immediately before the edge: B still current, C already stored.
    assert set(before) == {TEXTS[1], TEXTS[2]}
    # At the edge: the successor takes over, the predecessor is retired.
    assert set(at_edge) == {TEXTS[2]}
