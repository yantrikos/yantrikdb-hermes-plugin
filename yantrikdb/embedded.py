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

import glob
import logging
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any

from . import (
    embedders as _embedders_mod,  # noqa: F401 — register submodule for test patching; heavy deps inside the loader classes remain lazy
)
from .client import (
    YantrikDBClientError,
    YantrikDBConfig,
    YantrikDBError,
    YantrikDBServerError,
    YantrikDBTransientError,
    truncate_text,
)

logger = logging.getLogger(__name__)


# Engine 0.10+ typed exceptions, identified by CLASS NAME + engine module —
# never by message text (the ecosystem contract), and deliberately WITHOUT
# ``import yantrikdb`` (this plugin package is also named ``yantrikdb``; a
# stray import in a test/CI env with no installed engine would load a second
# copy of the plugin and duplicate the exception classes). We only ever see
# these instances via ``self._db`` calls against a real engine, so name+module
# identification is exact and side-effect-free.
_TRANSIENT_EXC_NAMES = frozenset({
    "Backpressure", "RecallContended",
    "CorrectionDeferredDuringReembed", "BatchDeferredDuringReembed",
})
_CALLER_EXC_NAMES = frozenset({
    "IdempotencyConflict", "InvalidIdempotencyKey", "ProvenanceInconsistent",
})
_RID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f-]{20,}")


def _is_engine_exc(exc: Exception, names: frozenset[str]) -> bool:
    cls = type(exc)
    return (
        cls.__name__ in names
        and cls.__module__.split(".")[0] == "yantrikdb"
    )


def _idempotency_conflict_rid(exc: Exception) -> str | None:
    """If ``exc`` is the engine's IdempotencyConflict, return the existing rid
    it carries (attribute, then message). ``None`` when not a conflict."""
    if not _is_engine_exc(exc, frozenset({"IdempotencyConflict"})):
        return None
    for attr in ("existing_rid", "rid", "original_rid"):
        val = getattr(exc, attr, None)
        if val:
            return str(val)
    m = _RID_RE.search(str(exc))
    return m.group(0) if m else ""


def _is_key_capability_refusal(exc: Exception) -> bool:
    """True when a key was rejected because this backend can't honor it: the
    engine's typed ``InvalidIdempotencyKey``, or a bare ``ValueError`` from the
    python-fallback-embedder wrapper (host-capability refusal)."""
    if _is_engine_exc(exc, frozenset({"InvalidIdempotencyKey"})):
        return True
    return isinstance(exc, ValueError)


def _map_engine_error(operation: str, exc: Exception) -> YantrikDBError:
    """Map embedded-engine exceptions into the plugin's error taxonomy."""
    if isinstance(exc, YantrikDBError):
        return exc
    # Engine 0.10+ typed exceptions — branch on type (name+module), not text.
    if _is_engine_exc(exc, _TRANSIENT_EXC_NAMES):
        return YantrikDBTransientError(f"{operation} failed: {exc}")
    if _is_engine_exc(exc, _CALLER_EXC_NAMES):
        return YantrikDBClientError(f"{operation} failed: {exc}")
    msg = str(exc)
    lowered = msg.lower()
    if (
        "queue full" in lowered
        or "retry after" in lowered
        or "database is locked" in lowered
        or "database locked" in lowered
        or "busy" in lowered
        or "timeout" in lowered
    ):
        return YantrikDBTransientError(f"{operation} failed transiently: {msg}")
    if "invalid" in lowered or "bad rid" in lowered or "not found" in lowered:
        return YantrikDBClientError(f"{operation} failed: {msg}")
    return YantrikDBServerError(f"{operation} failed: {msg}")


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


_ENGINE_EXT_SUFFIXES = (".so", ".pyd", ".dylib")


def find_engine_ext_path() -> str | None:
    """Absolute path to the engine's ``_yantrikdb_rust`` compiled extension,
    resolved WITHOUT importing the shadowed ``yantrikdb`` name (issue #50).

    ``None`` when the engine distribution is not installed. The engine's PyPI
    distribution is named ``yantrikdb``; this plugin's is
    ``yantrikdb-hermes-plugin`` — so ``importlib.metadata`` disambiguates them
    even though both expose a top-level package literally named ``yantrikdb``.
    """
    from importlib import metadata as _ilm

    # Preferred: read the engine distribution's own file list (no import, so
    # the plugin's shadowing ``yantrikdb`` package can't interfere).
    try:
        dist = _ilm.distribution("yantrikdb")
    except _ilm.PackageNotFoundError:
        dist = None
    if dist is not None:
        for f in (dist.files or []):
            name = os.path.basename(str(f))
            if name.startswith("_yantrikdb_rust") and name.endswith(
                _ENGINE_EXT_SUFFIXES,
            ):
                located = os.path.abspath(str(dist.locate_file(f)))
                if os.path.exists(located):
                    return located

    # Fallback: scan ``sys.path`` for a ``yantrikdb`` package dir that carries
    # the extension and is NOT this plugin's own directory (the shadow).
    our_dir = os.path.dirname(os.path.abspath(__file__))
    for entry in sys.path:
        try:
            cand = os.path.join(entry or ".", "yantrikdb")
            if not os.path.isdir(cand) or os.path.abspath(cand) == our_dir:
                continue
            for suf in _ENGINE_EXT_SUFFIXES:
                hits = glob.glob(os.path.join(cand, "_yantrikdb_rust*" + suf))
                if hits:
                    return os.path.abspath(hits[0])
        except OSError:
            continue
    return None


def load_engine_yantrikdb_class() -> Any:
    """Return the engine's ``YantrikDB`` class, resilient to the package-name
    collision of issue #50.

    This plugin package is itself named ``yantrikdb`` (to match ``plugin.yaml``
    ``name: yantrikdb`` and the ``hermes plugins install`` layout). When the
    plugin directory is ahead of the installed engine on ``sys.path`` — the
    ``hermes plugins install`` path, or any run from a source checkout — a plain
    ``from yantrikdb._yantrikdb_rust import YantrikDB`` resolves against THIS
    package (which has no ``_yantrikdb_rust``) and raises ``ImportError``, even
    though the real engine is installed and importable on its own. We recover by
    loading the engine's compiled extension directly from its installed
    distribution, and cache it so later plain imports resolve the engine too.
    """
    # Fast path — correct whenever the engine wins name resolution: the
    # pip-installed plugin imports as ``yantrikdb_hermes_plugin`` (no clash), or
    # the engine simply sits first on ``sys.path``.
    try:
        from yantrikdb._yantrikdb_rust import YantrikDB  # type: ignore
        return YantrikDB
    except ImportError:
        pass

    # Collision (or genuinely-missing) path.
    import importlib.util as _ilu

    ext_path = find_engine_ext_path()
    if ext_path is None:
        raise YantrikDBError(
            "embedded mode requires the `yantrikdb` engine, which is not "
            "installed in this environment. Install it with: "
            "pip install --upgrade yantrikdb",
        )

    try:
        spec = _ilu.spec_from_file_location("yantrikdb._yantrikdb_rust", ext_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"no import spec for {ext_path}")
        module = _ilu.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as e:  # noqa: BLE001 — re-raised as a truthful engine error
        raise YantrikDBError(
            "the `yantrikdb` engine is installed but this plugin package "
            f"(also named `yantrikdb`) is shadowing it on sys.path, and the "
            f"engine's native extension at {ext_path} could not be loaded "
            f"directly: {e}. Installing the plugin via "
            "`pip install yantrikdb-hermes-plugin` (which imports as "
            "`yantrikdb_hermes_plugin`) avoids the collision entirely. "
            "See https://github.com/yantrikos/yantrikdb-hermes-plugin/issues/50",
        ) from e

    # Cache the loaded extension so subsequent plain imports (e.g. the
    # availability probe) resolve the engine despite the shadowing package.
    try:
        sys.modules.setdefault("yantrikdb._yantrikdb_rust", module)
        shadow = sys.modules.get("yantrikdb")
        if shadow is not None and not hasattr(shadow, "_yantrikdb_rust"):
            shadow._yantrikdb_rust = module  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — caching is best-effort, never fatal
        pass

    try:
        return module.YantrikDB
    except AttributeError as e:
        raise YantrikDBError(
            "the loaded `yantrikdb` native extension exposes no `YantrikDB` "
            "class; the installed engine may be too old (need >= 0.7.4).",
        ) from e


# ---------------------------------------------------------------------------
# Process-global engine cache (v0.9.3)
#
# Hermes constructs a memory provider per agent/session, and every one of them
# resolves to the SAME database ($HERMES_HOME/yantrikdb-memory.db unless
# YANTRIKDB_DB_PATH says otherwise). Before this cache each provider opened its
# own engine over that same file, and each engine spawns its own materializer
# workers + compactor — so a host running N agents paid N times for background
# work on one database, and those workers poll whether or not anything is
# happening.
#
# Measured on a 32-logical-CPU box, 4,000 records, idle (no requests):
#   1 shared engine :  52 OS threads,  3.7% of the machine
#   6 engines       : 135 OS threads, 15.3% of the machine   (4.13x)
#
# Engines are keyed by database path + the full embedder selection, because the
# embedder determines vector dimensionality — two configs that disagree must
# never share one handle. Entries are intentionally never evicted: they mirror
# process lifetime, exactly like the pre-cache behaviour where each provider
# held its engine until interpreter shutdown.
# ---------------------------------------------------------------------------

_ENGINE_CACHE: dict[tuple[Any, ...], Any] = {}
_ENGINE_CACHE_LOCK = threading.Lock()


def _engine_cache_key(db_path: str, config: YantrikDBConfig) -> tuple[Any, ...]:
    """Identity of an engine handle: same key ⇒ safe to share."""
    return (
        os.path.abspath(db_path),
        (config.embedder_class or "").strip(),
        (config.embedder_model2vec or "").strip(),
        (config.embedder_huggingface or "").strip(),
        (config.embedder_name or "").strip(),
        int(config.embedding_dim or 0),
    )


def reset_engine_cache() -> None:
    """Drop all cached engines (tests; not part of the provider surface)."""
    with _ENGINE_CACHE_LOCK:
        _ENGINE_CACHE.clear()


class EmbeddedYantrikDBClient:
    """Adapter wrapping ``yantrikdb._yantrikdb_rust.YantrikDB`` to the
    same surface as ``YantrikDBClient`` (HTTP).

    Translates engine return shapes (bare strings / lists / bools) into
    the dict envelopes the provider's dispatch code expects, so
    ``handle_tool_call`` works against either backend without branching.
    """

    def __init__(self, config: YantrikDBConfig) -> None:
        self.config = config

        YantrikDB = load_engine_yantrikdb_class()

        db_path = config.db_path or _default_db_path()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        # Reuse an already-open engine for this database + embedder when one
        # exists in this process (see the cache notes above). Checked BEFORE
        # embedder materialisation so a hit also skips re-loading the model —
        # the expensive part of the model2vec / sentence-transformers paths.
        cache_key = _engine_cache_key(db_path, config)
        if config.share_engine:
            with _ENGINE_CACHE_LOCK:
                cached = _ENGINE_CACHE.get(cache_key)
            if cached is not None:
                self._db = cached
                logger.info(
                    "YantrikDB embedded backend ready (shared engine): "
                    "db=%s namespace=%s",
                    db_path, config.namespace,
                )
                return

        # Embedder selection — five paths, evaluated in order:
        #   1. YANTRIKDB_EMBEDDER_CLASS (most flexible escape hatch)
        #      → import dotted class, instantiate, set_embedder(instance).
        #        Requires YANTRIKDB_EMBEDDING_DIM (user knows their model).
        #   2. YANTRIKDB_EMBEDDER_MODEL2VEC (built-in model2vec loader, v0.4.2+)
        #      → Model2VecEmbedder(model_name); dim auto-probed.
        #        Install: pip install 'yantrikdb-hermes-plugin[model2vec]'.
        #   3. YANTRIKDB_EMBEDDER_HF (built-in sentence-transformers loader, v0.4.2+)
        #      → SentenceTransformerEmbedder(model_name); dim auto-probed.
        #        Install: pip install 'yantrikdb-hermes-plugin[sentence-transformers]'.
        #   4. YANTRIKDB_EMBEDDER (bundled-named download via engine)
        #      → set_embedder_named(name); requires YANTRIKDB_EMBEDDING_DIM.
        #   5. Default: with_default() → bundled potion-base-2M (dim=64).
        #
        # The precedence ordering is: more-specific user intent wins. A user
        # who set _CLASS clearly wants that exact class; a user who set
        # _MODEL2VEC/_HF picked a specific HF model; _EMBEDDER is the
        # bundled-download named variant; default is for users who set
        # nothing at all.
        #
        # set_embedder* requires exclusive Arc access on the engine — call
        # ONCE immediately after construction, before _db is shared.
        custom_class = (config.embedder_class or "").strip()
        model2vec_name = (config.embedder_model2vec or "").strip()
        hf_name = (config.embedder_huggingface or "").strip()
        named = (config.embedder_name or "").strip()

        # Resolve which path to take + materialise the embedder instance
        # (where applicable) BEFORE constructing YantrikDB, so the
        # builtin-loader paths can pass the probed dim into the ctor.
        # `instance` is the embedder object for paths 1/2/3; None for
        # path 4 (named, engine handles internally) and path 5 (default).
        instance: Any = None
        path_label: str = ""           # for log lines
        resolved_dim: int = 0          # 0 means "use with_default" or "trust user-set"

        if custom_class:
            # Path 1 — custom class via dotted import.
            if config.embedding_dim <= 0:
                raise YantrikDBError(
                    "YANTRIKDB_EMBEDDING_DIM must be a positive int when "
                    "YANTRIKDB_EMBEDDER_CLASS is set. The plugin can't probe "
                    "an arbitrary class's dim ahead of construction. Use the "
                    "embedder's output dim (e.g. 384 for all-MiniLM-L6-v2, "
                    "768 for all-mpnet-base-v2)."
                )
            try:
                import importlib
                mod_path, _, cls_name = custom_class.rpartition(".")
                if not mod_path or not cls_name:
                    raise YantrikDBError(
                        f"YANTRIKDB_EMBEDDER_CLASS={custom_class!r} must be a "
                        "dotted import path 'module.submodule.ClassName'."
                    )
                mod = importlib.import_module(mod_path)
                cls = getattr(mod, cls_name, None)
                if cls is None:
                    raise YantrikDBError(
                        f"class {cls_name!r} not found in module {mod_path!r}"
                    )
                instance = cls()
            except YantrikDBError:
                raise
            except Exception as e:
                raise YantrikDBError(
                    f"failed to import / instantiate YANTRIKDB_EMBEDDER_CLASS="
                    f"{custom_class!r}: {e}",
                ) from e
            if not callable(getattr(instance, "encode", None)):
                raise YantrikDBError(
                    f"YANTRIKDB_EMBEDDER_CLASS={custom_class!r}: instance has "
                    "no callable .encode() method. The engine expects an object "
                    "with `.encode(text: str) -> list[float]`."
                )
            resolved_dim = int(config.embedding_dim)
            path_label = f"class={custom_class}"
        elif model2vec_name:
            # Path 2 — built-in model2vec loader; dim auto-probed.
            from .embedders import Model2VecEmbedder
            instance = Model2VecEmbedder(model2vec_name)
            resolved_dim = instance.embedding_dim
            path_label = f"model2vec={model2vec_name}"
        elif hf_name:
            # Path 3 — built-in sentence-transformers loader; dim auto-probed.
            from .embedders import SentenceTransformerEmbedder
            instance = SentenceTransformerEmbedder(hf_name)
            resolved_dim = instance.embedding_dim
            path_label = f"hf={hf_name}"
        elif named:
            # Path 4 — bundled-named via engine; dim required.
            if config.embedding_dim <= 0:
                raise YantrikDBError(
                    "YANTRIKDB_EMBEDDING_DIM must be a positive int when "
                    "YANTRIKDB_EMBEDDER is set. Use the named embedder's "
                    "output dim (e.g. 256 for potion-base-8M, 512 for "
                    "potion-base-32M)."
                )
            resolved_dim = int(config.embedding_dim)
            path_label = f"named={named}"
        # else: path 5 — default, no explicit ctor

        # Construct the engine
        if not custom_class and not model2vec_name and not hf_name and not named:
            # Path 5 — default
            try:
                self._db = YantrikDB.with_default(db_path)
            except Exception as e:
                raise YantrikDBServerError(
                    f"failed to open YantrikDB at {db_path}: {e}",
                ) from e
        else:
            try:
                self._db = YantrikDB(db_path, embedding_dim=resolved_dim)
            except Exception as e:
                raise YantrikDBServerError(
                    f"failed to open YantrikDB at {db_path}: {e}",
                ) from e

        # Attach embedder (paths 1/2/3 share set_embedder; path 4 uses
        # set_embedder_named; path 5 already attached inside with_default).
        if instance is not None:
            try:
                self._db.set_embedder(instance)
                logger.info(
                    "YantrikDB embedded: attached embedder %s (dim=%d)",
                    path_label, resolved_dim,
                )
            except Exception as e:
                raise YantrikDBServerError(
                    f"set_embedder({path_label}) failed: {e}",
                ) from e
        elif named:
            try:
                self._db.set_embedder_named(named)
                logger.info(
                    "YantrikDB embedded: attached bundled embedder %s (dim=%d)",
                    named, resolved_dim,
                )
            except Exception as e:
                raise YantrikDBServerError(
                    f"set_embedder_named({named!r}) failed — most likely "
                    "the model name isn't a known bundled-download variant "
                    f"in this yantrikdb version, or the dim mismatches: {e}",
                ) from e

        if not self._db.has_embedder():
            raise YantrikDBError(
                "YantrikDB embedder not configured. The default `pip install "
                "yantrikdb` ships the bundled embedder; slim builds "
                "(--no-default-features) require an explicit embedder."
            )

        if config.share_engine:
            # Publish for reuse. Construction happens outside the lock (it can
            # load an embedding model), so two providers racing on a cold cache
            # may both build one — first writer wins and the loser adopts it,
            # so at most one engine per key survives to be used.
            with _ENGINE_CACHE_LOCK:
                winner = _ENGINE_CACHE.setdefault(cache_key, self._db)
            if winner is not self._db:
                self._db = winner

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
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        safe_text = truncate_text(text, self.config.max_text_len)
        ns = namespace or self.config.namespace
        mt = memory_type or "semantic"
        dom = domain or "general"
        if idempotency_key:
            return self._remember_idempotent(
                safe_text, key=idempotency_key, memory_type=mt,
                importance=float(importance), namespace=ns, domain=dom,
                metadata=metadata,
            )
        try:
            rid = self._db.record_text(
                safe_text, memory_type=mt, importance=float(importance),
                namespace=ns, domain=dom, metadata=metadata,
            )
        except Exception as e:
            raise _map_engine_error("remember", e) from e
        return {"rid": rid}

    def _remember_idempotent(
        self, text: str, *, key: str, memory_type: str, importance: float,
        namespace: str, domain: str, metadata: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Keyed write via ``record(idempotency_key=)`` (engine 0.10+).

        The wrapper routes this through the drift-safe
        ``record_text_with_idempotency`` (engine vector excluded from the
        digest). Same key + same payload is a silent HIT (original rid, zero
        writes); same key + divergent payload raises ``IdempotencyConflict``
        carrying the existing rid, which we surface for claim resolution. A
        python-fallback embedder can't produce the digest → clear refusal.
        """
        try:
            rid = self._db.record(
                text=text, memory_type=memory_type, importance=importance,
                namespace=namespace, domain=domain, metadata=metadata,
                idempotency_key=key,
            )
            return {"rid": rid, "idempotent": True}
        except AttributeError:
            # engine too old to expose record()/keys — honest refusal
            raise YantrikDBClientError(
                "idempotency keys need yantrikdb>=0.10.0 (embedded)."
            ) from None
        except Exception as e:
            existing = _idempotency_conflict_rid(e)
            if existing is not None:
                return {
                    "rid": existing or None,
                    "idempotency_conflict": True,
                    "detail": str(e),
                }
            if _is_key_capability_refusal(e):
                raise YantrikDBClientError(
                    "idempotency keys require the engine embedder (bundled "
                    "potion-2M in embedded mode). This install uses a "
                    "python-side embedder (YANTRIKDB_EMBEDDER_* / model2vec / "
                    "sentence-transformers), which can't produce the "
                    "drift-safe digest. Remove the key, or use the engine "
                    "embedder."
                ) from e
            raise _map_engine_error("remember", e) from e

    def recall(
        self,
        query: str,
        *,
        namespace: str | None = None,
        top_k: int | None = None,
        memory_type: str | None = None,
        domain: str | None = None,
    ) -> dict[str, Any]:
        try:
            results = self._db.recall(
                query=query,
                top_k=int(top_k or self.config.top_k),
                namespace=namespace or self.config.namespace,
                domain=domain,
                memory_type=memory_type,
            )
        except Exception as e:
            raise _map_engine_error("recall", e) from e
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
        namespace: str | None = None,
    ) -> dict[str, Any]:
        cfg: dict[str, Any] = {
            "run_consolidation": run_consolidation,
            "run_conflict_scan": run_conflict_scan,
            "run_pattern_mining": run_pattern_mining,
            "run_personality": run_personality,
        }
        # Only set namespace when provided so older engine versions that
        # don't read the key from cfg keep their existing behavior. Engine
        # pin is `yantrikdb >= 0.7.4`; current engines honor it.
        if namespace:
            cfg["namespace"] = namespace
        if consolidation_limit is not None:
            cfg["consolidation_limit"] = int(consolidation_limit)
        try:
            out = self._db.think(cfg)
        except Exception as e:
            raise _map_engine_error("think", e) from e
        return out if isinstance(out, dict) else {}

    def conflicts(self, *, namespace: str | None = None) -> dict[str, Any]:
        try:
            out = self._db.get_conflicts(namespace=namespace or self.config.namespace)
        except Exception as e:
            raise _map_engine_error("conflicts", e) from e
        items = list(out) if out else []
        return {"conflicts": items}

    def pending_triggers(self, *, limit: int = 10) -> dict[str, Any]:
        """Return triggers waiting for agent consumption.

        ``think()`` produces these as a side effect; without a consumer
        they accumulate. The agent decides whether to ``acknowledge``,
        ``dismiss``, or ``act_on`` each one.
        """
        try:
            out = self._db.get_pending_triggers(limit=int(limit))
        except Exception as e:
            raise _map_engine_error("pending_triggers", e) from e
        return {"triggers": list(out) if out else []}

    def acknowledge_trigger(self, trigger_id: str) -> dict[str, Any]:
        """Mark a trigger as seen by the agent. No action recorded.

        Auto-calls ``deliver_trigger`` first because the engine requires
        delivery before acknowledge succeeds — the deliver step is
        engine-internal bookkeeping the agent shouldn't have to know
        about.
        """
        try:
            self._db.deliver_trigger(trigger_id)
            ok = self._db.acknowledge_trigger(trigger_id)
        except Exception as e:
            raise _map_engine_error("acknowledge_trigger", e) from e
        if isinstance(ok, dict):
            return ok
        return {"trigger_id": trigger_id, "acknowledged": bool(ok)}

    def dismiss_trigger(self, trigger_id: str) -> dict[str, Any]:
        """Close a trigger without acting on it (agent declined)."""
        try:
            ok = self._db.dismiss_trigger(trigger_id)
        except Exception as e:
            raise _map_engine_error("dismiss_trigger", e) from e
        if isinstance(ok, dict):
            return ok
        return {"trigger_id": trigger_id, "dismissed": bool(ok)}

    def act_on_trigger(self, trigger_id: str) -> dict[str, Any]:
        """Record that the agent took action in response to a trigger.

        Auto-delivers first, matching the engine's lifecycle requirement.
        """
        try:
            self._db.deliver_trigger(trigger_id)
            ok = self._db.act_on_trigger(trigger_id)
        except Exception as e:
            raise _map_engine_error("act_on_trigger", e) from e
        if isinstance(ok, dict):
            return ok
        return {"trigger_id": trigger_id, "acted": bool(ok)}

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
        try:
            out = self._db.resolve_conflict(**kwargs)
        except Exception as e:
            raise _map_engine_error("resolve_conflict", e) from e
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
        namespace: str | None = None,
    ) -> dict[str, Any]:
        # Conditionally pass namespace so older engines that don't accept
        # the kwarg keep working. When the provider derives a workspace-
        # scoped namespace, this routes the edge into that namespace
        # rather than the engine's constructor-time default.
        rel_kwargs: dict[str, Any] = {
            "rel_type": relationship,
            "weight": float(weight) if weight is not None else 1.0,
        }
        # NOTE: namespace kwarg is intentionally NOT forwarded to the engine
        # until the engine adds namespace-scoped edge support.
        try:
            edge_id = self._db.relate(entity, target, **rel_kwargs)
        except Exception as e:
            raise _map_engine_error("relate", e) from e
        return {"edge_id": edge_id}

    # -- Stats --------------------------------------------------------

    def stats(self, *, namespace: str | None = None) -> dict[str, Any]:
        try:
            out = self._db.stats(namespace=namespace or self.config.namespace)
        except Exception as e:
            raise _map_engine_error("stats", e) from e
        return out if isinstance(out, dict) else {}

    # -- Record listing (engine 0.8+/0.9+) ----------------------------

    def list_records(
        self,
        *,
        namespace: str | None = None,
        limit: int = 50,
        order: str = "asc",
        domain: str | None = None,
        since_rid: str | None = None,
    ) -> dict[str, Any]:
        """Structured scan over a namespace (engine ``list_records``).

        Returns ``{"records": [...], "next_cursor": ...}``. Raises
        ``AttributeError`` on engines too old to expose ``list_records``;
        the provider catches that and falls back to its sidecar signal.
        """
        try:
            out = self._db.list_records(
                namespace=namespace or self.config.namespace,
                limit=int(limit),
                order=order,
                domain=domain,
                since_rid=since_rid,
            )
        except AttributeError:
            raise
        except Exception as e:
            raise _map_engine_error("list_records", e) from e
        if isinstance(out, dict):
            return out
        return {"records": list(out) if out else []}

    # -- Knowledge gaps (engine 0.9+) ---------------------------------

    # -- Packs (v0.11.0, engine 0.11.0+) ----------------------------------

    def pack_action(
        self,
        action: str,
        *,
        path: str | None = None,
        pack_id: str | None = None,
        allow_unverified_embedder: bool = False,
    ) -> dict[str, Any]:
        """Mount / install / unmount / inspect sealed knowledge packs.

        One method rather than nine because the provider dispatches a single
        action-based tool, and because these operations share one failure
        mode worth handling in exactly one place: an engine older than 0.11.0
        has none of them, which must read as "upgrade" and not as a crash.
        """
        db = self._db
        try:
            if action == "list":
                mounted = list(db.mounted_packs() or [])
                installed = list(db.installed_packs() or [])
                mounted_ids = {
                    (p.get("pack_id") if isinstance(p, dict) else str(p))
                    for p in mounted
                }
                # An installed pack missing from `mounted` failed to re-mount
                # this session — the engine documents this comparison as the
                # way to notice, so surface it rather than making callers
                # diff two lists themselves.
                failed = [
                    p for p in installed
                    if (p.get("pack_id") if isinstance(p, dict) else str(p))
                    not in mounted_ids
                ]
                return {
                    "mounted": mounted,
                    "installed": installed,
                    "failed_to_mount": failed,
                    "pack_dir": db.pack_dir(),
                }
            if action == "inspect":
                if not path:
                    raise YantrikDBClientError("pack inspect requires `path`")
                from yantrikdb._yantrikdb_rust import (  # noqa: PLC0415
                    YantrikDB as _Engine,
                )
                return {
                    "manifest": _Engine.read_pack_manifest(
                        self._resolve_pack_path(path),
                    ),
                }
            if action in ("mount", "install"):
                if not path:
                    raise YantrikDBClientError(f"pack {action} requires `path`")
                resolved = self._resolve_pack_path(path)
                if action == "install":
                    pid = db.install_pack(resolved)
                else:
                    pid = db.mount_pack(
                        resolved,
                        allow_unverified_embedder=allow_unverified_embedder,
                    )
                return {"pack_id": pid, "action": action, "path": resolved}
            if action in ("unmount", "uninstall"):
                if not pack_id:
                    raise YantrikDBClientError(f"pack {action} requires `pack_id`")
                fn = db.unmount_pack if action == "unmount" else db.uninstall_pack
                return {"pack_id": pack_id, "action": action, "ok": bool(fn(pack_id))}
            if action == "unmount_all":
                return {"unmounted": int(db.unmount_all_packs() or 0)}
            raise YantrikDBClientError(
                f"unknown pack action {action!r}; expected list, inspect, mount, "
                "install, unmount, uninstall, unmount_all",
            )
        except AttributeError as e:
            raise YantrikDBClientError(
                "packs need yantrikdb>=0.11.0 (embedded). "
                "Upgrade with: pip install --upgrade 'yantrikdb>=0.11.3'",
            ) from e
        except YantrikDBError:
            raise
        except Exception as e:  # noqa: BLE001 — engine typed excs vary by version
            # PackEmbedderMismatch is a REFUSAL, not a failure: the pack's
            # vectors are provably in a different embedding space, so mounting
            # it would return confidently wrong results. Keep the engine's own
            # wording, which explains why forcing it is the wrong instinct.
            if type(e).__name__ == "PackEmbedderMismatch":
                raise YantrikDBClientError(f"pack refused: {e}") from e
            raise _map_engine_error(f"pack {action}", e) from e

    def _resolve_pack_path(self, path: str) -> str:
        """Absolute path for a pack given a path or a bare filename.

        A bare name is resolved against the engine's pack directory so an
        agent can say `wordpress-expert-0.2.0.ydbpack` without knowing where
        the host keeps packs.
        """
        p = Path(path).expanduser()
        if p.is_absolute() or p.exists():
            return str(p)
        try:
            pack_dir = self._db.pack_dir()
        except AttributeError:
            pack_dir = None
        if pack_dir:
            candidate = Path(pack_dir) / path
            if candidate.exists():
                return str(candidate)
        return str(p)

    def pack_namespaces(self) -> list[str]:
        """Namespaces holding the records of currently-mounted packs.

        Mounting brings a pack's rules in via ``pack_context()``, but its
        *records* land in the namespace the author sealed them under — so an
        agent recalling within its own namespace gets the pack's constitution
        and none of its knowledge, which is the least useful half. Recall has
        to be widened to these namespaces for a mount to mean anything.

        ``mounted_packs()`` does not report the namespace, but it does report
        each pack's ``path``, and the manifest does — so resolve it there
        rather than recalling unscoped, which would also expose every other
        namespace in the database.
        """
        try:
            mounted = list(self._db.mounted_packs() or [])
        except AttributeError:
            return []
        except Exception:  # noqa: BLE001 — never break recall over a pack probe
            return []
        from yantrikdb._yantrikdb_rust import YantrikDB as _Engine  # noqa: PLC0415

        out: list[str] = []
        for entry in mounted:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            if not path:
                continue
            try:
                manifest = _Engine.read_pack_manifest(str(path)) or {}
            except Exception:  # noqa: BLE001
                continue
            ns = manifest.get("namespace")
            if ns and ns not in out:
                out.append(str(ns))
        return out

    def pack_context(self) -> dict[str, Any]:
        """The engine-assembled context block for all mounted packs.

        Assembled engine-side deliberately so every consumer injects the same
        text and a pack behaves identically wherever it is mounted; we pass it
        through verbatim rather than reformatting it.
        """
        try:
            return {"context": self._db.pack_context()}
        except AttributeError:
            return {"context": None}
        except Exception as e:  # noqa: BLE001
            raise _map_engine_error("pack_context", e) from e

    def knowledge_gaps(
        self,
        *,
        min_count: int = 3,
        max_avg_top_score: float = 0.4,
        limit: int = 20,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """The substrate's known unknowns (engine ``knowledge_gaps``).

        Namespace-scoped on engine 0.9.3+ (demand is recorded per namespace);
        pass ``namespace`` so gaps come from this agent's namespace. Engines
        0.9.0-0.9.2 have no ``namespace`` parameter (global demand) — we fall
        back to the unscoped call there. Raises ``AttributeError`` on engines
        without the method; the provider returns a clean "not available".
        """
        base: dict[str, Any] = {
            "min_count": int(min_count),
            "max_avg_top_score": float(max_avg_top_score),
            "limit": int(limit),
        }
        ns = namespace or self.config.namespace
        try:
            try:
                out = self._db.knowledge_gaps(namespace=ns, **base)
            except TypeError:
                # engine < 0.9.3: no namespace kwarg, demand is global.
                out = self._db.knowledge_gaps(**base)
        except AttributeError:
            raise
        except Exception as e:
            raise _map_engine_error("knowledge_gaps", e) from e
        if isinstance(out, dict):
            return out
        return {"gaps": list(out) if out else []}

    # -- Conversation buffer (engine 0.9+) ----------------------------

    def record_turn(
        self,
        role: str,
        content: str,
        *,
        namespace: str | None = None,
        max_turns: int = 10,
    ) -> dict[str, Any]:
        try:
            self._db.record_turn(
                namespace or self.config.namespace,
                role,
                content,
                max_turns=int(max_turns),
            )
        except AttributeError:
            raise
        except Exception as e:
            raise _map_engine_error("record_turn", e) from e
        return {"recorded": True, "role": role}

    def recent_turns(
        self, *, namespace: str | None = None, limit: int = 10,
    ) -> dict[str, Any]:
        try:
            out = self._db.recent_turns(
                namespace or self.config.namespace, limit=int(limit),
            )
        except AttributeError:
            raise
        except Exception as e:
            raise _map_engine_error("recent_turns", e) from e
        return {"turns": list(out) if out else []}

    def clear_turns(self, *, namespace: str | None = None) -> dict[str, Any]:
        try:
            self._db.clear_turns(namespace or self.config.namespace)
        except AttributeError:
            raise
        except Exception as e:
            raise _map_engine_error("clear_turns", e) from e
        return {"cleared": True}

    # -- Tasks (engine 0.9+) ------------------------------------------

    def task_add(
        self,
        title: str,
        *,
        namespace: str | None = None,
        priority: str = "medium",
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            tid = self._db.task_add(
                namespace or self.config.namespace,
                title,
                priority=priority,
                parent_id=parent_id,
            )
        except AttributeError:
            raise
        except Exception as e:
            raise _map_engine_error("task_add", e) from e
        return tid if isinstance(tid, dict) else {"id": tid}

    def task_list(
        self, *, namespace: str | None = None, status: str | None = None,
    ) -> dict[str, Any]:
        try:
            out = self._db.task_list(
                namespace or self.config.namespace, status=status,
            )
        except AttributeError:
            raise
        except Exception as e:
            raise _map_engine_error("task_list", e) from e
        return {"tasks": list(out) if out else []}

    def task_get(self, task_id: str) -> dict[str, Any]:
        try:
            out = self._db.task_get(task_id)
        except AttributeError:
            raise
        except Exception as e:
            raise _map_engine_error("task_get", e) from e
        return out if isinstance(out, dict) else {"task": out}

    def task_update(
        self, task_id: str, *, status: str | None = None,
        priority: str | None = None,
    ) -> dict[str, Any]:
        try:
            self._db.task_update(task_id, status=status, priority=priority)
        except AttributeError:
            raise
        except Exception as e:
            raise _map_engine_error("task_update", e) from e
        return {"id": task_id, "updated": True}

    def task_delete(self, task_id: str) -> dict[str, Any]:
        try:
            self._db.task_delete(task_id)
        except AttributeError:
            raise
        except Exception as e:
            raise _map_engine_error("task_delete", e) from e
        return {"id": task_id, "deleted": True}

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
