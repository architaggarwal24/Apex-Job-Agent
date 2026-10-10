# applier/adapters/__init__.py
"""
Site adapters: Greenhouse, Lever, ... each drives its own family of career
pages deterministically instead of through the generic LLM filler. The
Orchestrator asks adapter_for(site_type, url) and uses the adapter it gets,
else the universal filler (applier/universal.py).

TO ADD AN ADAPTER: write ONE file in this package with a class that extends
applier.adapters.base.Adapter (or the declarative form.FormAdapter) and add ONE
line to REGISTRY below ("module.path:ClassName"). Nothing else needs to know.

Entries are imported lazily and one at a time: an adapter that fails to import,
is not an Adapter, has no name or a name already taken, or names an unknown
site type is logged and skipped, and the rest keep working. The first adapter
(in REGISTRY order) whose can_handle() says yes wins.
"""

import importlib
import logging

from applier.adapters.base import Adapter, AdapterField, UnsupportedForm
from applier.site_type import SITE_TYPES

logger = logging.getLogger(__name__)

REGISTRY = (
    "applier.adapters.greenhouse:GreenhouseAdapter",
    "applier.adapters.lever:LeverAdapter",
)

_loaded: list[Adapter] | None = None


def _load_one(entry: str, taken: set[str]) -> Adapter | None:
    try:
        module_name, _, class_name = entry.partition(":")
        adapter = getattr(importlib.import_module(module_name), class_name)()
    except Exception:
        logger.exception(f"[Adapters] could not load {entry!r}; skipping it")
        return None
    problem = None
    if not isinstance(adapter, Adapter):
        problem = "is not an Adapter"
    elif not adapter.name:
        problem = "has no name"
    elif adapter.name in taken:
        problem = f"reuses the name {adapter.name!r}"
    elif not adapter.site_types or any(t not in SITE_TYPES for t in adapter.site_types):
        problem = "lists no site type, or one that does not exist"
    if problem:
        logger.error(f"[Adapters] {entry!r} {problem}; skipping it")
        return None
    taken.add(adapter.name)
    return adapter


def all_adapters() -> list[Adapter]:
    global _loaded
    if _loaded is None:
        taken: set[str] = set()
        _loaded = [a for a in (_load_one(e, taken) for e in REGISTRY) if a is not None]
    return list(_loaded)


def reload() -> None:
    """Forget what was loaded (after REGISTRY changes, e.g. in tests)."""
    global _loaded
    _loaded = None


def get(name: str) -> Adapter | None:
    return next((a for a in all_adapters() if a.name == name), None)


def adapter_for(site_type: str, url: str = "") -> Adapter | None:
    """The adapter that should run this application, or None (use the universal filler)."""
    for adapter in all_adapters():
        try:
            if adapter.can_handle(site_type, url):
                return adapter
        except Exception:
            logger.exception(f"[Adapters] {adapter.name}.can_handle failed; ignoring it")
    return None
