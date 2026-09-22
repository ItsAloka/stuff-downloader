import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def isolated_app_dirs(tmp_path, monkeypatch):
    """Never touch the real %APPDATA% / %LOCALAPPDATA% during tests."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    return tmp_path


@pytest.fixture(autouse=True)
def no_thumbnail_network(monkeypatch):
    """GUI tests never fetch real thumbnails; tests that need images pass a fake fetcher."""
    try:
        from stuff_downloader.gui import thumbs
    except ImportError:  # the worker-only test runs have no PyQt
        return

    def refuse(url):
        raise AssertionError(f"a test tried to fetch {url}")

    monkeypatch.setattr(thumbs, "fetch", refuse)
