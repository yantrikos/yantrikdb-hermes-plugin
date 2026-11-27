"""Contract tests: what the dispatch passes must fit the client's signature.

One defect class reached a review three times in a row:

  * a schema field the dispatch never forwarded (`preconditions` became
    decorative — the schema advertised it, the client accepted it, and
    changing it produced `unchanged=true`),
  * a keyword the real client method does not declare (`include_history`
    raised TypeError on every HTTP `skill_search`, before a request existed),
  * a tool advertised without a dispatch branch.

`MagicMock(spec=SomeClass)` cannot catch the second: the spec is taken from
the class *after* the change, so a caller that passes one keyword too many
stays green. These tests use the **real client** and enforce its **real
signature** with `inspect.signature(...).bind(...)`, which rejects exactly
what Python would reject — and records what actually arrived.

No new framework: pytest, the existing fixtures, and the standard library.
"""
from __future__ import annotations

import inspect
import json

import pytest


def _spy(obj, method: str):
    """Replace `obj.method` with a recorder that enforces its real signature.

    `bind()` is the assertion: a keyword the method does not declare raises
    at the call site, so the defect surfaces where it is made instead of in
    a review three days later.
    """
    sig = inspect.signature(getattr(obj, method))
    calls: list[dict] = []

    def recorder(*args, **kwargs):
        try:
            sig.bind(*args, **kwargs)
        except TypeError as exc:
            raise AssertionError(
                f"{type(obj).__name__}.{method} called with parameters it does "
                f"not declare ({sig}): {exc}"
            ) from None
        calls.append({"args": args, "kwargs": kwargs})
        return {}

    setattr(obj, method, recorder)
    return calls, sig


@pytest.fixture
def prov(provider_module):
    """Dispatch seam without a host, a store or `initialize()`."""
    p = provider_module.YantrikDBMemoryProvider()
    cfg = provider_module.YantrikDBConfig(mode="embedded")
    cfg.skills_enabled = True
    cfg.packs_enabled = True
    cfg.fleet_view = True
    p._config = cfg
    return p


@pytest.fixture
def embedded_client(prov, provider_module):
    import importlib

    emb = importlib.import_module(provider_module.__name__ + ".embedded")
    client = emb.EmbeddedYantrikDBClient(emb.YantrikDBConfig(mode="embedded"))
    prov._client = client
    return client


@pytest.fixture
def http_client(prov, provider_module):
    client = provider_module.YantrikDBClient(
        provider_module.YantrikDBConfig(mode="http")
    )
    prov._client = client
    return client


# (tool, args, client method, {kwarg: expected value})
FORWARDING = [
    (
        "yantrikdb_skill_define",
        {
            "skill_id": "git.commit_clean",
            "body": "Always rebase before merge so the history stays linear.",
            "skill_type": "procedure",
            "applies_to": ["git"],
            "preconditions": ["a clean working tree"],
            "version": "2",
        },
        "skill_define",
        {"preconditions": ["a clean working tree"], "version": "2"},
    ),
    (
        "yantrikdb_skill_outcome",
        {
            "skill_id": "git.commit_clean",
            "succeeded": True,
            "note": "worked",
            "phase": "outcome",
            "task": "merge two branches",
            "evidence_ref": "run-7",
            "skill_version_rid": "v-rid",
            "corrects_rid": "c-rid",
            "observed_at": "2026-11-27T09:00:00Z",
        },
        "skill_outcome",
        {
            "phase": "outcome",
            "task": "merge two branches",
            "evidence_ref": "run-7",
            "skill_version_rid": "v-rid",
            "corrects_rid": "c-rid",
            "observed_at": "2026-11-27T09:00:00Z",
        },
    ),
    (
        "yantrikdb_skill_outcomes",
        {"query": "merge two branches", "limit": 5},
        "skill_outcomes",
        {"query": "merge two branches", "limit": 5},
    ),
    (
        "yantrikdb_skill_reify",
        {"skill_id": "git.commit_clean"},
        "skill_reify_revisions",
        {"skill_id": "git.commit_clean"},
    ),
    (
        "yantrikdb_skill_search",
        {"query": "git", "top_k": 3, "include_history": True},
        "skill_search",
        {"include_history": True, "top_k": 3},
    ),

    (
        "yantrikdb_skill_define",
        {
            "skill_id": "git.commit_clean",
            "body": "Always rebase before merge so the history stays linear.",
            "skill_type": "procedure",
            "applies_to": ["git", "workflow"],
            "triggers": ["rebase", "merge"],
            "on_conflict": "replace",
            "supersedes_skill_id": "git.commit_clean",
        },
        "skill_define",
        {
            "applies_to": ["git", "workflow"],
            "triggers": ["rebase", "merge"],
            "on_conflict": "replace",
            "supersedes_skill_id": "git.commit_clean",
        },
    ),
    (
        "yantrikdb_skill_outcome",
        {"skill_id": "git.commit_clean", "succeeded": False, "note": "conflict"},
        "skill_outcome",
        {"note": "conflict"},
    ),
    (
        "yantrikdb_skill_outcomes",
        {"skill_id": "git.commit_clean", "since": 1.0, "limit": 3},
        "skill_outcomes",
        {"skill_id": "git.commit_clean", "since": 1.0, "limit": 3},
    ),
]

IDS = [row[0] for row in FORWARDING]


@pytest.mark.parametrize("tool,args,method,expected", FORWARDING, ids=IDS)
def test_embedded_dispatch_fits_the_client_signature(
    embedded_client, prov, tool, args, method, expected
):
    """Every advertised field must arrive, and must be one the method takes."""
    calls, sig = _spy(embedded_client, method)
    prov.handle_tool_call(tool, args)

    assert calls, f"{tool}: {method} was never reached"
    call = calls[-1]
    params = list(sig.parameters)
    for key, value in expected.items():
        if key in call["kwargs"]:
            assert call["kwargs"][key] == value, (
                f"{tool}: '{key}' arrived as {call['kwargs'][key]!r}"
            )
            continue
        # A positional call is legitimate — but the field must still arrive.
        assert key in params, f"{tool}: '{key}' is not a parameter of {method}"
        pos = params.index(key)
        assert pos < len(call["args"]), (
            f"{tool}: '{key}' is in the schema and reached neither kwargs nor args"
        )
        assert call["args"][pos] == value, (
            f"{tool}: '{key}' arrived as {call['args'][pos]!r}"
        )


@pytest.mark.parametrize("tool,args,method,expected", FORWARDING, ids=IDS)
def test_http_dispatch_fits_the_client_signature(
    http_client, prov, tool, args, method, expected
):
    """The same dispatch runs on the other transport.

    This is the test that was missing when `include_history` was added: the
    embedded client took the keyword, the HTTP client did not, and every HTTP
    `skill_search` raised TypeError. Embedded-only tools answer with a typed
    tool error — never with an exception.
    """
    calls, _sig = _spy(http_client, method)
    out = prov.handle_tool_call(tool, args)

    parsed = json.loads(out)
    assert isinstance(parsed, dict)
    if not calls:
        assert parsed.get("error"), f"{tool}: no client call and no error"
        assert "embedded" in parsed["error"], parsed["error"]


def test_required_fields_are_declared(provider_module):
    """Every `required` name must exist in `properties`.

    Cheap and broad: a required field with no property cannot be supplied by
    any caller, so the tool would be unusable by construction.
    """
    problems = []
    for schema in provider_module.ALL_TOOL_SCHEMAS:
        params = schema.get("parameters") or {}
        props = params.get("properties") or {}
        missing = [f for f in (params.get("required") or []) if f not in props]
        if missing:
            problems.append(f"{schema['name']}: required but undeclared: {missing}")
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("transport", ["embedded", "http"])
def test_every_advertised_tool_has_a_dispatch_branch(
    provider_module, transport, request
):
    """S3: a tool in ALL_TOOL_SCHEMAS without a branch is a schema lie.

    Derived from the schema list itself, so a tool added later is covered
    without editing this test. A tool that is gated behind a feature flag
    answers with a tool error — that is a contract, not a missing branch;
    only "Unknown tool" means the dispatch never learned the name.
    """
    request.getfixturevalue(
        "embedded_client" if transport == "embedded" else "http_client"
    )
    prov = request.getfixturevalue("prov")
    failures = []
    for schema in provider_module.ALL_TOOL_SCHEMAS:
        name = schema["name"]
        out = prov.handle_tool_call(name, _minimal_args(schema))
        try:
            parsed = json.loads(out)
        except (TypeError, ValueError):  # pragma: no cover
            failures.append(f"{name}: dispatch returned non-JSON {out!r}")
            continue
        if isinstance(parsed, dict) and "Unknown tool" in str(parsed.get("error", "")):
            failures.append(f"{name}: advertised in ALL_TOOL_SCHEMAS, no branch")
    assert not failures, "\n".join(failures)


def _minimal_args(schema: dict) -> dict:
    """One value per REQUIRED field, typed from the schema itself."""
    params = schema.get("parameters") or {}
    props = params.get("properties") or {}
    args: dict = {}
    for name in params.get("required") or []:
        kind = (props.get(name) or {}).get("type")
        if kind == "string":
            args[name] = "x"
        elif kind in ("integer", "number"):
            args[name] = 1
        elif kind == "boolean":
            args[name] = True
        elif kind == "object":
            args[name] = {}
        elif kind == "array":
            args[name] = []
        else:
            args[name] = "x"
    return args
