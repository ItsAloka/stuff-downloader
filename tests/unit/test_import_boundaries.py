"""core never imports Qt or engines; worker never imports Qt or the GUI package."""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
ENGINES = ("yt_dlp", "gallery_dl", "spotdl")
QT = ("PyQt6", "PySide6", "PyQt5")


def _imports(package_dir: Path):
    for path in package_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    yield path, alias.name
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:
                    # resolve relative import against the file's package
                    parts = list(path.relative_to(SRC).with_suffix("").parts[:-1])
                    parts = parts[: len(parts) - (node.level - 1)]
                    module = ".".join([*parts, module] if module else parts)
                yield path, module


def _violations(package_dir: Path, forbidden):
    return [
        f"{p.relative_to(SRC)}: {name}"
        for p, name in _imports(package_dir)
        if any(name == f or name.startswith(f + ".") for f in forbidden)
    ]


@pytest.mark.parametrize("package", ["stuff_downloader/core"])
def test_core_has_no_qt_or_engine_imports(package):
    assert _violations(SRC / package, QT + ENGINES + ("stuff_downloader.gui",)) == []


def test_gui_has_no_engine_imports():
    assert _violations(SRC / "stuff_downloader" / "gui", ENGINES) == []


def test_worker_has_no_qt_or_gui_imports():
    forbidden = QT + ("stuff_downloader",)
    assert _violations(SRC / "stuff_downloader_worker", forbidden) == []


def test_boundary_checker_detects_violations(monkeypatch):
    """Guard against a checker that silently passes everything."""
    names = {name for _, name in _imports(SRC / "stuff_downloader" / "gui")}
    assert any(n.startswith("PyQt6") for n in names)
    # relative "from ..core import x" inside gui resolves to the absolute package name
    assert "stuff_downloader.core" in names
    assert _violations(SRC / "stuff_downloader" / "gui", QT)
