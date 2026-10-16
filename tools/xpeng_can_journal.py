#!/usr/bin/env python3
"""Resolve the capture journal format, which now lives in the integration.

The format moved to ``custom_components/xpeng/can_journal.py`` when the
integration gained the ability to capture from the MQTT connection it already
holds: an add-on or a tool cannot import that file as a package member, and
having two implementations of one on-disk format is how a capture becomes
unreadable, so the tools load the single implementation by path - the same thing
``mqtt_credential_check.py`` does for ``xpeng_api.py``.

This module keeps the tools' import surface (``import xpeng_can_journal``) and
re-exports every public name from the component file.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

COMPONENT_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "xpeng"
JOURNAL_PATH = COMPONENT_DIR / "can_journal.py"


def _load():
    spec = importlib.util.spec_from_file_location("xpeng_can_journal_impl", JOURNAL_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - unreadable source
        raise ImportError(f"cannot load the capture journal from {JOURNAL_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_impl = _load()

# Re-export the public surface, so callers are unaffected by where it lives.
for _name in getattr(_impl, "__all__", ()):
    globals()[_name] = getattr(_impl, _name)

__all__ = list(getattr(_impl, "__all__", ()))
