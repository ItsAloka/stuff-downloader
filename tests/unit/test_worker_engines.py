"""Probe and yt-dlp spike engines, without network access."""

from __future__ import annotations

import sys

import pytest

from stuff_downloader_worker.engines import ENGINES, get_engine
from stuff_downloader_worker.engines.base import EngineError
from stuff_downloader_worker.protocol import JobSpec


def _spec(engine, **options):
    return JobSpec("j1", engine, "https://example.invalid/", ".", options)


def test_registry_has_spike_engines():
    assert {"fake", "probe", "ytdlp"} <= set(ENGINES)


def test_probe_reports_interpreter_and_modules():
    result = get_engine("probe").download(_spec("probe", modules=["json", "no_such_mod_xyz"]), None)
    assert result["executable"] == sys.executable
    assert result["modules"]["no_such_mod_xyz"] is None
    assert result["modules"]["json"] is not None


@pytest.mark.parametrize("modules", ["json", ["os.path"], ["x; import os"], [1], ["a"] * 21])
def test_probe_rejects_bad_module_lists(modules):
    with pytest.raises(EngineError):
        get_engine("probe").download(_spec("probe", modules=modules), None)


def test_ytdlp_rejects_bad_mode_before_network(monkeypatch):
    pytest.importorskip("yt_dlp")
    with pytest.raises(EngineError, match="mode"):
        get_engine("ytdlp").download(_spec("ytdlp", mode="delete"), lambda *_: None)


def test_trusted_tool_only_uses_fixed_names_in_runner_dir(monkeypatch, tmp_path):
    from stuff_downloader_worker.engines import ytdlp

    (tmp_path / "deno.exe").write_bytes(b"")
    monkeypatch.delenv(ytdlp.TOOLS_DIR_ENV_VAR, raising=False)
    assert ytdlp.trusted_tool("deno") is None
    monkeypatch.setenv(ytdlp.TOOLS_DIR_ENV_VAR, str(tmp_path))
    assert ytdlp.trusted_tool("deno") == tmp_path / "deno.exe"
    assert ytdlp.trusted_tool("ffmpeg") is None  # not present
    assert ytdlp.trusted_tool("calc") is None  # not an allowed tool


def test_ytdlp_ignores_job_supplied_executable_paths(monkeypatch, tmp_path):
    from stuff_downloader_worker.engines import ytdlp

    captured = {}

    class FakeYDL:
        def __init__(self, opts):
            captured.update(opts)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            return {"title": "t", "id": "x", "formats": []}

    fake = type(sys)("yt_dlp")
    fake.YoutubeDL = FakeYDL
    fake.version = type(sys)("yt_dlp.version")
    fake.version.__version__ = "0"
    fake.utils = type(sys)("yt_dlp.utils")
    fake.utils.DownloadError = RuntimeError
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)
    monkeypatch.delenv(ytdlp.TOOLS_DIR_ENV_VAR, raising=False)
    evil = tmp_path / "evil.exe"
    evil.write_bytes(b"")
    spec = _spec("ytdlp", deno_path=str(evil), ffmpeg_location=str(evil))
    get_engine("ytdlp").download(spec, lambda *_: None)
    assert "js_runtimes" not in captured and "ffmpeg_location" not in captured


def test_ytdlp_missing_engine_is_a_clean_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "yt_dlp", None)  # import now raises ImportError
    with pytest.raises(EngineError) as info:
        get_engine("ytdlp").download(_spec("ytdlp"), lambda *_: None)
    assert info.value.code == "engine_missing"
