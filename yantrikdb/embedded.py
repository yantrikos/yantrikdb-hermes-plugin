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
