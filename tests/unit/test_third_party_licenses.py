"""THIRD_PARTY_LICENSES.txt stays true to what is pinned, and the GUI stays engine-free.

The licence file is only useful if it describes the packages that actually ship. Each
`[<lock file>]` ... `[end]` section must list exactly the `name==version` pins of that lock file,
so adding, dropping or re-pinning a dependency without re-auditing its licence fails here.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LICENSES = ROOT / "THIRD_PARTY_LICENSES.txt"
LOCKS = [
    "requirements.lock",
    "packaging/engine-requirements/ytdlp.txt",
    "packaging/engine-requirements/ytdlp-previous.txt",
    "packaging/engine-requirements/gallerydl.txt",
    "packaging/engine-requirements/spotdl.txt",
]
PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\;]+)")
# Modules that run only in the separate engine processes (plan §8.3). If the GUI imported one,
# PyInstaller would freeze it into the GPLv3 PyQt6 binary and the boundary would be gone.
ENGINE_MODULES = (
    "yt_dlp",
    "gallery_dl",
    "spotdl",
    "mutagen",
    "curl_cffi",
    "stuff_downloader_worker",
)


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pins(text: str) -> dict[str, str]:
    pins = {}
    for line in text.splitlines():
        match = PIN.match(line.strip())
        if match:
            pins[_norm(match[1])] = match[2]
    return pins


def _section(lock: str) -> str:
    text = LICENSES.read_text(encoding="utf-8")
    match = re.search(rf"^\[{re.escape(lock)}\]\n(.*?)^\[end\]", text, re.M | re.S)
    assert match, f"THIRD_PARTY_LICENSES.txt has no [{lock}] section"
    return match[1]


@pytest.mark.parametrize("lock", LOCKS)
def test_every_pinned_package_is_audited_at_its_pinned_version(lock):
    pinned = _pins((ROOT / lock).read_text(encoding="utf-8"))
    audited = _pins(_section(lock))
    assert pinned, f"no pins parsed from {lock}"
    assert audited == pinned


@pytest.mark.parametrize("lock", LOCKS)
def test_every_audited_package_names_a_licence(lock):
    for line in _section(lock).splitlines():
        if PIN.match(line.strip()):
            name_version, _, licence = line.strip().partition(" ")
            assert licence.strip(), f"{name_version} has no licence recorded"


def test_the_gui_package_never_imports_an_engine():
    gui = ROOT / "src" / "stuff_downloader"
    found = []
    for path in gui.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                names = [node.module]
            found += [
                f"{path.relative_to(ROOT)}: {name}"
                for name in names
                if any(name == m or name.startswith(m + ".") for m in ENGINE_MODULES)
            ]
    assert found == []


def test_the_frozen_build_excludes_every_engine():
    spec = (ROOT / "packaging" / "StuffDownloader.spec").read_text(encoding="utf-8")
    for module in ENGINE_MODULES:
        assert f'"{module}"' in spec, f"{module} is not in the spec's ENGINE_EXCLUDES"


def test_the_project_licence_is_mit_everywhere_it_is_declared():
    import tomllib

    licence = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert licence.startswith("MIT License\n")
    assert re.search(r"^Copyright \(c\) \d{4} \S", licence, re.M)
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["license"] == {"text": "MIT"}
    assert "License :: OSI Approved :: MIT License" in project["classifiers"]
    audit = LICENSES.read_text(encoding="utf-8")
    assert "F1  RESOLVED" in audit and "MIT" in audit
    assert "F2  RESOLVED" in audit and "F2  BLOCKER" not in audit
    assert "UNKNOWN" not in audit
    assert re.search(r"^spotapi==1\.2\.8 +GPL-3\.0", audit, re.M)
    assert re.search(r"^spotipyfree==1\.9\.14 +MIT", audit, re.M)
