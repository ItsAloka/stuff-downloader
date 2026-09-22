"""The release version is declared twice; both copies must name the same 1.0.0 release."""

from __future__ import annotations

import tomllib
from pathlib import Path

import stuff_downloader

ROOT = Path(__file__).resolve().parents[2]


def test_pyproject_and_fallback_constant_agree_on_1_0_0():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["version"] == "1.0.0"
    assert stuff_downloader.__version__ == "1.0.0"
    assert stuff_downloader._pyproject_version(ROOT / "pyproject.toml") == "1.0.0"


def test_release_version_resolves_1_0_0_from_source(monkeypatch):
    from importlib import metadata

    def missing(name):
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(stuff_downloader.metadata, "version", missing)
    assert stuff_downloader.release_version() == "1.0.0"
    monkeypatch.setattr(stuff_downloader.sys, "frozen", True, raising=False)
    assert stuff_downloader.release_version() == "1.0.0"
