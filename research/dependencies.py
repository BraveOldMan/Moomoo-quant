"""Optional dependency loading for research-only modules."""

from __future__ import annotations

import importlib
from types import ModuleType


def optional_import(module_name: str) -> ModuleType | None:
    """Import an optional research dependency, returning None when unavailable."""

    try:
        return importlib.import_module(module_name)
    except ImportError:
        return None


def require_optional(module_name: str) -> ModuleType:
    """Import a research dependency or fail closed with an actionable message."""

    module = optional_import(module_name)
    if module is None:
        raise RuntimeError(
            f"Research step requires optional dependency '{module_name}'. "
            "Install it explicitly before running this step."
        )
    return module

