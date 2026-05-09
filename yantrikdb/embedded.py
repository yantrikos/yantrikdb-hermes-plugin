"""In-process YantrikDB backend via the bundled embedded engine.

Mirrors ``YantrikDBClient``'s 8-method surface so the provider can use
either backend interchangeably. The embedded path uses ``yantrikdb >=
0.7.4`` which ships a default Rust-native embedder (potion-base-2M,
~8 MB, dim=64) — users get semantic recall via ``pip install`` alone:
no separate server, no token, no GPU, no network.

Lifecycle / threading model (per yantrikdb-core guidance):
- Construct ONCE at plugin init. ``YantrikDB.with_default(...)`` opens
  SQLite, spawns internal materializer + compactor threads, and lazy-
  loads the bundled model on first encode (~tens of ms cold).
- The pyo3 wrapper holds ``Arc<YantrikDB>`` internally; concurrent
  ``record_text`` / ``recall_text`` calls from any thread are safe.
- ``set_embedder_named()`` (v0.7.5+) requires exclusive Arc access —
  call it once right after construction, before any other code takes
  a ref to the handle.
- Don't call ``close()`` unless tearing down the plugin entirely;
  let the GC handle the last drop.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from .client import (
    YantrikDBClientError,
    YantrikDBConfig,
    YantrikDBError,
    YantrikDBServerError,
    truncate_text,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Skill schema validation (v0.3.0+)
#
# Reproduces yantrikdb-server's wrapper-layer checks client-side so the
# plugin can write skills in embedded mode (no server in front) without
# corrupting the shared `skill_substrate` convention. Rules per
# yantrikdb-server's RFC 022 / saga decision 2026-05-01:
#
#   skill_id     ^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$  length 4..200
#   body         length 50..5000
#   applies_to   non-empty list (<=10), each ^[a-z][a-z0-9_]*$
#   skill_type   {procedure, reference, lesson, pattern, rule}
#
# The applies_to entry regex is LOAD-BEARING per yantrikdb-server's
# May-9 review — anyone naturally writing "applies-to"-style hyphenated
# tags would corrupt the substrate convention. Hyphens MUST be rejected.
# ---------------------------------------------------------------------------

_SKILL_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")
_APPLIES_TO_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SKILL_TYPES = frozenset({"procedure", "reference", "lesson", "pattern", "rule"})

_SKILL_BODY_MIN = 50
_SKILL_BODY_MAX = 5000
_SKILL_ID_MIN = 4
_SKILL_ID_MAX = 200
_APPLIES_TO_MAX = 10

SKILL_NAMESPACE = "skill_substrate"
OUTCOME_NAMESPACE = "outcome_substrate"


def validate_skill_define_args(
    skill_id: str,
    body: str,
    skill_type: str,
    applies_to: list[str],
) -> None:
    """Raise ``YantrikDBClientError`` if any field violates the wrapper schema.

    Mirrors yantrikdb-server's `/v1/skills/define` validation. Errors are
    surfaced as 4xx-equivalents (don't trip the breaker; they're
    deterministic caller mistakes).
    """
    # skill_id
    if not isinstance(skill_id, str):
        raise YantrikDBClientError("skill_id must be a string")
    if not (_SKILL_ID_MIN <= len(skill_id) <= _SKILL_ID_MAX):
        raise YantrikDBClientError(
            f"skill_id length must be {_SKILL_ID_MIN}..{_SKILL_ID_MAX} chars; got {len(skill_id)}"
        )
    if not _SKILL_ID_RE.fullmatch(skill_id):
        raise YantrikDBClientError(
            f"skill_id {skill_id!r} must match {_SKILL_ID_RE.pattern} "
            "(lowercase, dot-separated segments; e.g. 'workflow.git.commit_clean')"
        )

    # body
    if not isinstance(body, str):
        raise YantrikDBClientError("body must be a string")
    if not (_SKILL_BODY_MIN <= len(body) <= _SKILL_BODY_MAX):
        raise YantrikDBClientError(
            f"body length must be {_SKILL_BODY_MIN}..{_SKILL_BODY_MAX} chars; got {len(body)}"
        )

    # skill_type
    if skill_type not in _SKILL_TYPES:
        raise YantrikDBClientError(
            f"skill_type {skill_type!r} not in {sorted(_SKILL_TYPES)}"
        )

    # applies_to — load-bearing regex (hyphen-vs-underscore drift)
    if not isinstance(applies_to, list) or not applies_to:
        raise YantrikDBClientError(
            "applies_to must be a non-empty list of identifiers"
        )
    if len(applies_to) > _APPLIES_TO_MAX:
        raise YantrikDBClientError(
            f"applies_to may contain at most {_APPLIES_TO_MAX} entries; got {len(applies_to)}"
        )
    for entry in applies_to:
        if not isinstance(entry, str) or not _APPLIES_TO_RE.fullmatch(entry):
            raise YantrikDBClientError(
                f"applies_to entry {entry!r} must match {_APPLIES_TO_RE.pattern} "
                "(lowercase + digits + underscores ONLY — no hyphens, no dots, no spaces)"
            )


def _default_db_path() -> str:
    """Resolve where to put the SQLite file when YANTRIKDB_DB_PATH is empty.

    Prefer ``$HERMES_HOME/yantrikdb-memory.db`` if Hermes is importable,
    fall back to ``~/.yantrikdb-hermes-memory.db`` for standalone use.
    """
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        return str(get_hermes_home() / "yantrikdb-memory.db")
    except ImportError:
        return str(Path.home() / ".yantrikdb-hermes-memory.db")


class EmbeddedYantrikDBClient:
    """Adapter wrapping ``yantrikdb._yantrikdb_rust.YantrikDB`` to the
    same surface as ``YantrikDBClient`` (HTTP).

    Translates engine return shapes (bare strings / lists / bools) into
    the dict envelopes the provider's dispatch code expects, so
    ``handle_tool_call`` works against either backend without branching.
    """

    def __init__(self, config: YantrikDBConfig) -> None:
        self.config = config

        try:
            from yantrikdb._yantrikdb_rust import YantrikDB
        except ImportError as e:
            raise YantrikDBError(
                "embedded mode requires `yantrikdb >= 0.7.4`. "
                "Install with: pip install --upgrade yantrikdb"
            ) from e

        db_path = config.db_path or _default_db_path()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        try:
            self._db = YantrikDB.with_default(db_path)
        except Exception as e:
            raise YantrikDBServerError(
                f"failed to open YantrikDB at {db_path}: {e}",
            ) from e

        # set_embedder_named requires exclusive Arc access — call BEFORE any
        # other state takes a reference. Fail soft: log and keep the bundled
        # potion-2M default if the named variant is unavailable.
        if config.embedder_name:
            try:
                self._db.set_embedder_named(config.embedder_name)
                logger.info(
                    "YantrikDB embedded: switched to embedder %s",
                    config.embedder_name,
                )
            except Exception as e:
                logger.warning(
                    "set_embedder_named(%r) failed; staying on bundled potion-2M: %s",
                    config.embedder_name, e,
                )

        if not self._db.has_embedder():
            raise YantrikDBError(
                "YantrikDB embedder not configured. The default `pip install "
                "yantrikdb` ships the bundled embedder; slim builds "
                "(--no-default-features) require an explicit embedder."
            )

        logger.info(
            "YantrikDB embedded backend ready: db=%s namespace=%s",
            db_path, config.namespace,
        )

    # -- Operational --------------------------------------------------

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "engine": "embedded",
            "embedder_attached": bool(self._db.has_embedder()),
        }

    # -- Memory ops ---------------------------------------------------

    def remember(
        self,
        text: str,
        *,
        namespace: str | None = None,
        importance: float = 0.6,
        domain: str | None = None,
        memory_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        safe_text = truncate_text(text, self.config.max_text_len)
        rid = self._db.record_text(
            safe_text,
            memory_type=memory_type or "semantic",
            importance=float(importance),
            namespace=namespace or self.config.namespace,
            domain=domain or "general",
            metadata=metadata,
        )
        return {"rid": rid}

    def recall(
        self,
        query: str,
        *,
        namespace: str | None = None,
        top_k: int | None = None,
        memory_type: str | None = None,
        domain: str | None = None,
    ) -> dict[str, Any]:
        results = self._db.recall(
            query=query,
            top_k=int(top_k or self.config.top_k),
            namespace=namespace or self.config.namespace,
            domain=domain,
            memory_type=memory_type,
        )
        items = list(results) if results else []
        return {"results": items, "total": len(items)}

    def forget(self, rid: str) -> dict[str, Any]:
        try:
            found = bool(self._db.forget(rid))
        except Exception as e:
            # Engine raises on bad rid format; map to client error so
            # provider's 4xx handling kicks in (no breaker trip).
            raise YantrikDBClientError(f"forget failed: {e}") from e
        return {"rid": rid, "found": found}

    # -- Maintenance --------------------------------------------------

    def think(
        self,
        *,
        run_consolidation: bool = True,
        run_conflict_scan: bool = True,
        run_pattern_mining: bool = False,
        run_personality: bool = False,
        consolidation_limit: int | None = None,
    ) -> dict[str, Any]:
        cfg: dict[str, Any] = {
            "run_consolidation": run_consolidation,
            "run_conflict_scan": run_conflict_scan,
            "run_pattern_mining": run_pattern_mining,
            "run_personality": run_personality,
        }
        if consolidation_limit is not None:
            cfg["consolidation_limit"] = int(consolidation_limit)
        out = self._db.think(cfg)
        return out if isinstance(out, dict) else {}

    def conflicts(self) -> dict[str, Any]:
        out = self._db.get_conflicts(namespace=self.config.namespace)
        items = list(out) if out else []
        return {"conflicts": items}

    def resolve_conflict(
        self,
        conflict_id: str,
        *,
        strategy: str,
        winner_rid: str | None = None,
        new_text: str | None = None,
        resolution_note: str | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "conflict_id": conflict_id,
            "strategy": strategy,
        }
        if winner_rid:
            kwargs["winner_rid"] = winner_rid
        if new_text:
            kwargs["new_text"] = new_text
        if resolution_note:
            kwargs["resolution_note"] = resolution_note
        out = self._db.resolve_conflict(**kwargs)
        if isinstance(out, dict):
            return out
        return {"conflict_id": conflict_id, "strategy": strategy}

    # -- Graph --------------------------------------------------------

    def relate(
        self,
        entity: str,
        target: str,
        relationship: str,
        *,
        weight: float | None = None,
    ) -> dict[str, Any]:
        edge_id = self._db.relate(
            entity, target,
            rel_type=relationship,
            weight=float(weight) if weight is not None else 1.0,
        )
        return {"edge_id": edge_id}

    # -- Stats --------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        out = self._db.stats(namespace=self.config.namespace)
        return out if isinstance(out, dict) else {}

    # -- Skills (v0.3.0+) ---------------------------------------------
    #
    # Skills live in the shared ``skill_substrate`` namespace alongside
    # other consumers (Lane B SDK, server handlers, WisePick). Hermes-
    # authored skills are tagged with ``metadata.source=hermes`` so any
    # downstream consumer can filter Hermes-authored skills in or out
    # of their searches. Outcomes go to ``outcome_substrate`` as an
    # append-only event log; consumers compute their own success metrics.

    def skill_search(
        self,
        query: str,
        *,
        top_k: int | None = None,
        applies_to: str | None = None,
    ) -> dict[str, Any]:
        # Prefer the keyword-only namespace filter on `recall_text` (engine
        # v0.7.7+); fall back to the lower-level `recall` for older wheels
        # so users who haven't upgraded yet still get a working read path.
        try:
            results = self._db.recall_text(
                query, top_k=int(top_k or self.config.top_k),
                namespace=SKILL_NAMESPACE,
            )
        except TypeError:
            # Pre-0.7.7 wheel: recall_text didn't accept `namespace`.
            results = self._db.recall(
                query=query,
                top_k=int(top_k or self.config.top_k),
                namespace=SKILL_NAMESPACE,
            )
        items: list[dict[str, Any]] = list(results) if results else []
        # Optional client-side post-filter on applies_to — server's
        # /v1/skills/search does the same as a post-filter pattern.
        if applies_to:
            items = [
                r for r in items
                if applies_to in (r.get("metadata", {}) or {}).get("applies_to", [])
            ]
        return {"skills": items, "total": len(items)}

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
        # Reproduce server-side schema validation client-side. These
        # raise YantrikDBClientError so the provider's 4xx handling
        # surfaces them without tripping the breaker.
        validate_skill_define_args(skill_id, body, skill_type, applies_to)

        # Best-effort uniqueness check. In embedded mode there's a
        # TOCTOU window between this lookup and the record_text below,
        # but single-agent embedded use is non-racy in practice. The
        # constraint difference (server: 409 transactional; embedded:
        # last-write-wins) is documented in the v0.3.0 changelog.
        if on_conflict == "reject":
            existing = self._db.recall(
                query=skill_id, top_k=5, namespace=SKILL_NAMESPACE,
            )
            for hit in (existing or []):
                meta = hit.get("metadata", {}) or {}
                if meta.get("skill_id") == skill_id:
                    raise YantrikDBClientError(
                        f"skill {skill_id!r} already exists "
                        "(on_conflict='reject'); use on_conflict='replace' or pick a new skill_id"
                    )

        metadata: dict[str, Any] = {
            "record_type": "skill",
            "skill_id": skill_id,
            "skill_type": skill_type,
            "applies_to": list(applies_to),
            "source": "hermes",
        }
        if triggers:
            metadata["triggers"] = list(triggers)
        if version:
            metadata["version"] = version
        if supersedes_skill_id:
            metadata["supersedes_skill_id"] = supersedes_skill_id

        rid = self._db.record_text(
            body,
            memory_type="procedural",
            namespace=SKILL_NAMESPACE,
            domain="skill",
            metadata=metadata,
        )
        return {"rid": rid, "skill_id": skill_id, "stored": True}

    def skill_outcome(
        self,
        skill_id: str,
        succeeded: bool,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        # Append-only event log. NO auto-rollup of success_count on the
        # parent skill record — agent-layer pedagogy decision per
        # yantrikdb-server's "schema not semantics" rule (matches the
        # WisePick pattern).
        body_parts = [
            f"outcome: skill={skill_id} succeeded={succeeded}",
        ]
        if note:
            body_parts.append(f"note: {note}")
        body = "\n".join(body_parts)

        metadata: dict[str, Any] = {
            "record_type": "skill_outcome",
            "skill_id": skill_id,
            "succeeded": bool(succeeded),
            "source": "hermes",
        }
        if note:
            metadata["note"] = note

        rid = self._db.record_text(
            body,
            memory_type="episodic",
            namespace=OUTCOME_NAMESPACE,
            domain="skill_outcome",
            metadata=metadata,
        )
        return {"rid": rid, "skill_id": skill_id, "recorded": True}

    # -- Lifecycle ----------------------------------------------------

    def close(self) -> None:
        # Per upstream guidance: don't close unless the plugin is being
        # torn down entirely; let GC handle the final drop. Calling
        # close() while concurrent threads still hold refs raises.
        logger.debug("EmbeddedYantrikDBClient.close() — no-op (GC manages handle)")


def make_backend(config: YantrikDBConfig) -> Any:
    """Factory: return either ``YantrikDBClient`` (HTTP) or
    ``EmbeddedYantrikDBClient`` based on ``config.mode``.

    Both expose the same 8-method surface so the provider's dispatch
    code stays unchanged.
    """
    mode = (config.mode or "embedded").strip().lower()
    if mode == "embedded":
        return EmbeddedYantrikDBClient(config)
    if mode == "http":
        from .client import YantrikDBClient
        return YantrikDBClient(config)
    raise YantrikDBError(
        f"unknown YANTRIKDB_MODE={mode!r}. Use 'embedded' or 'http'."
    )


__all__ = ["EmbeddedYantrikDBClient", "make_backend"]
