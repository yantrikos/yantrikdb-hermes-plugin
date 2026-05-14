"""Top-level Hermes plugin entry point — for ``hermes plugins install``.

When users install via ``hermes plugins install yantrikos/yantrikdb-hermes-plugin``,
Hermes' user-plugin loader clones this repo into ``~/.hermes/plugins/yantrikdb/``
(target name comes from ``plugin.yaml.name`` at the repo root). The loader then
imports ``__init__.py`` from that directory and looks for ``register`` or a
``MemoryProvider`` subclass. This file is that entry point — it loads the real
plugin module from the ``yantrikdb/`` subfolder and re-exports both.

The two install paths coexist intentionally:

* ``pip install yantrikdb-hermes-plugin`` + ``yantrikdb-hermes install <hermes>``
  copies the contents of the ``yantrikdb/`` subfolder into
  ``<hermes>/plugins/memory/yantrikdb/`` (bundled-discovery path). This file
  is NOT used on that path — the inner ``yantrikdb/__init__.py`` is the entry.

* ``hermes plugins install yantrikos/yantrikdb-hermes-plugin`` clones the whole
  repo into ``~/.hermes/plugins/yantrikdb/`` (user-discovery path). This file
  IS used; it dynamic-loads the inner subfolder so the actual provider code is
  shared between both paths instead of duplicated.

We use absolute file-path loading (``importlib.util.spec_from_file_location``)
rather than a relative import (``from .yantrikdb import ...``) because Hermes'
user-installed-plugin loader doesn't register a parent package for
``_hermes_user_memory.yantrikdb``, so relative imports inside this file fail
silently and the provider is discovered as "loaded but no instance found".
Absolute file-path loading sidesteps that.

If you opened this file looking for the implementation, see ``yantrikdb/``.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

# This file is the entry point for the ``hermes plugins install`` path ONLY.
# It is intentionally a no-op when loaded in any other context (pytest
# collection, direct ``import``, etc.) so it doesn't create a second copy of
# the inner package and break ``isinstance`` checks across test fixtures.
#
# Hermes' user-installed-plugin loader loads this file under a module name
# starting with ``_hermes_user_memory.`` — that's the discriminator we use to
# decide whether to do the inner load. Bundled discovery (which fires after
# ``yantrikdb-hermes install`` copies the inner ``yantrikdb/`` contents into
# ``<hermes>/plugins/memory/yantrikdb/``) doesn't go through this file at all.
if __name__.startswith("_hermes_user_memory"):

    # Workaround for a Hermes bug in user-installed plugin discovery: the loader
    # registers the module under a dotted name (e.g. ``_hermes_user_memory.yantrikdb``)
    # but never registers the parent package (``_hermes_user_memory``). Python's
    # import machinery then fails when our ``__init__.py`` tries to register a
    # child module under our own dotted name. Pre-register a synthetic parent
    # if it's missing.
    if "." in __name__:
        _parent = __name__.split(".", 1)[0]
        if _parent not in sys.modules:
            sys.modules[_parent] = types.ModuleType(_parent)

    _INNER_DIR = Path(__file__).parent / "yantrikdb"
    _INNER_INIT = _INNER_DIR / "__init__.py"
    _INNER_MOD_NAME = f"{__name__}._yantrikdb_inner"

    _spec = importlib.util.spec_from_file_location(
        _INNER_MOD_NAME,
        str(_INNER_INIT),
        submodule_search_locations=[str(_INNER_DIR)],
    )
    if _spec is None or _spec.loader is None:  # pragma: no cover — defensive
        raise ImportError(
            f"yantrikdb-hermes-plugin: could not locate inner package at {_INNER_DIR}"
        )
    _inner = importlib.util.module_from_spec(_spec)
    sys.modules[_INNER_MOD_NAME] = _inner
    _spec.loader.exec_module(_inner)

    # Re-export the names Hermes' user-plugin loader looks for.
    register = _inner.register
    YantrikDBMemoryProvider = _inner.YantrikDBMemoryProvider

    __all__ = ["YantrikDBMemoryProvider", "register"]
