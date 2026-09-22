"""Windows-safe output-name validation.

This file is kept byte-identical to ``stuff_downloader/core/names.py``; a test enforces it.
The GUI uses it to de-duplicate names; the worker uses it to decide the real file name.
"""

from __future__ import annotations

import re

MAX_STEM = 200

_UNSAFE = re.compile(
    "[" + "".join(chr(c) for c in range(32)) + re.escape('<>:"/|?*' + chr(92)) + "]"
)
_RESERVED = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)


def safe_output_name(name: str | None) -> str | None:
    """One safe file stem, or ``None`` when nothing usable is left (blank or a device name).

    Never returns a path, a parent reference, a trailing dot/space, or a Windows device name
    (Windows ignores trailing dots and spaces, so ``"CON .txt"`` is still the console).
    """
    clean = _UNSAFE.sub("_", name or "").replace("..", "_")
    clean = re.sub(r"\s+", " ", clean)[:MAX_STEM].strip().rstrip(". ")
    if not clean or clean.split(".")[0].rstrip(" .").upper() in _RESERVED:
        return None
    return clean

