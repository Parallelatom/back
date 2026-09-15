"""The code version stamped onto every row.

Every recorded row says which build wrote it, so that a Hit Rate computed over a span of
weeks can still be attributed to the rules that were actually in force at the time.
"""
from __future__ import annotations

import os
import subprocess
from functools import lru_cache

_UNKNOWN = "unknown"


@lru_cache(maxsize=1)
def code_version() -> str:
    override = os.environ.get("STRATEGY_LAB_VERSION")
    if override:
        return override
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        head = subprocess.run(
            ["git", "-C", root, "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if head.returncode != 0:
            return _UNKNOWN
        version = head.stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", root, "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if dirty.returncode == 0 and dirty.stdout.strip():
            version += "-dirty"
        return version or _UNKNOWN
    except (OSError, subprocess.SubprocessError):
        return _UNKNOWN
