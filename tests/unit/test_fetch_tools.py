"""packaging/fetch_tools.py: pinned sources, verification before staging, and failure cleanup."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import sys
import zipfile
from pathlib import Path

import pytest

PACKAGING = Path(__file__).resolve().parents[2] / "packaging"


@pytest.fixture(scope="module")
def ft():
    spec = importlib.util.spec_from_file_location("fetch_tools", PACKAGING / "fetch_tools.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["fetch_tools"] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_every_source_is_https_and_fully_pinned(ft):
    for source in ft.SOURCES:
        assert source.url.startswith("https://"), source.url
        assert len(source.sha256) == 64 and int(source.sha256, 16) >= 0
        assert bool(source.members) != bool(source.target), source.label
    produced = [t for s in ft.SOURCES for t in (list(s.members.values()) or [s.target])]
    assert sorted(produced) == sorted(ft.STAGED)
    for relative, sha in ft.STAGED.items():
        assert len(sha) == 64 and int(sha, 16) >= 0, relative


def test_the_three_tools_and_their_licences_are_staged(ft):
    assert set(ft.EXECUTABLES) <= set(ft.STAGED)
    assert "licenses/FFmpeg-LICENSE.txt" in ft.STAGED
    assert "licenses/Deno-LICENSE.md" in ft.STAGED


def _fake_pins(ft, monkeypatch, files: dict[str, bytes]) -> None:
    monkeypatch.setattr(ft, "STAGED", {rel: _sha(data) for rel, data in files.items()})


def _write(root: Path, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)


FILES = {"ffmpeg.exe": b"ff", "licenses/L.txt": b"licence"}


def test_verify_accepts_matching_files(ft, monkeypatch, tmp_path):
    _fake_pins(ft, monkeypatch, FILES)
    _write(tmp_path, FILES)
    assert ft.verify_staged(tmp_path) == [(tmp_path / r).resolve() for r in FILES]


def test_verify_refuses_an_altered_file(ft, monkeypatch, tmp_path):
    _fake_pins(ft, monkeypatch, FILES)
    _write(tmp_path, {**FILES, "ffmpeg.exe": b"tampered"})
    with pytest.raises(ft.ToolError, match="mismatch for ffmpeg.exe"):
        ft.verify_staged(tmp_path)


def test_verify_refuses_a_missing_file(ft, monkeypatch, tmp_path):
    _fake_pins(ft, monkeypatch, FILES)
    _write(tmp_path, {"ffmpeg.exe": b"ff"})
    with pytest.raises(ft.ToolError, match="missing licenses/L.txt"):
        ft.verify_staged(tmp_path)


def test_staged_paths_cannot_escape_the_folder(ft, tmp_path):
    with pytest.raises(ft.ToolError, match="escapes"):
        ft._stage_path(tmp_path, "../evil.exe")


def _zip(tmp_path: Path, members: dict[str, bytes]) -> Path:
    path = tmp_path / "a.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def test_extract_reads_only_the_named_members(ft, tmp_path):
    archive = _zip(tmp_path, {"bin/ffmpeg.exe": b"ff", "../../evil.txt": b"x", "other": b"y"})
    out = tmp_path / "out"
    assert ft.extract_members(archive, {"bin/ffmpeg.exe": "ffmpeg.exe"}, out) == ["ffmpeg.exe"]
    assert sorted(p.name for p in out.rglob("*")) == ["ffmpeg.exe"]
    assert not (tmp_path.parent / "evil.txt").exists()


def test_extract_refuses_a_missing_member(ft, tmp_path):
    archive = _zip(tmp_path, {"other": b"y"})
    with pytest.raises(ft.ToolError, match="no member bin/ffmpeg.exe"):
        ft.extract_members(archive, {"bin/ffmpeg.exe": "ffmpeg.exe"}, tmp_path / "out")


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_download_refuses_plain_http(ft, tmp_path):
    with pytest.raises(ft.ToolError, match="non-HTTPS"):
        ft.download("http://example.com/x.zip", "0" * 64, tmp_path / "x")


def test_download_deletes_a_file_that_fails_its_hash(ft, monkeypatch, tmp_path):
    monkeypatch.setattr(ft.urllib.request, "urlopen", lambda url, timeout: _Resp(b"evil"))
    dest = tmp_path / "x"
    with pytest.raises(ft.ToolError, match="mismatch"):
        ft.download("https://example.com/x.zip", _sha(b"good"), dest)
    assert not dest.exists()


def _fake_world(ft, monkeypatch, served: dict[str, bytes]):
    """Point SOURCES/STAGED at a zip and a plain file served from memory."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("pkg/bin/tool.exe", b"tool")
        zf.writestr("pkg/LICENSE", b"gpl")
    archive = buf.getvalue()
    monkeypatch.setattr(
        ft,
        "SOURCES",
        (
            ft.Source(
                "zip",
                "https://x/a.zip",
                _sha(archive),
                members={"pkg/bin/tool.exe": "tool.exe", "pkg/LICENSE": "licenses/T.txt"},
            ),
            ft.Source("lic", "https://x/L.md", _sha(b"mit"), target="licenses/D.md"),
        ),
    )
    monkeypatch.setattr(
        ft,
        "STAGED",
        {"tool.exe": _sha(b"tool"), "licenses/T.txt": _sha(b"gpl"), "licenses/D.md": _sha(b"mit")},
    )
    content = {"https://x/a.zip": archive, "https://x/L.md": b"mit", **served}
    monkeypatch.setattr(ft.urllib.request, "urlopen", lambda url, timeout: _Resp(content[url]))


def test_fetch_stages_verified_files_and_writes_sources(ft, monkeypatch, tmp_path):
    _fake_world(ft, monkeypatch, {})
    dest = tmp_path / "tools"
    paths = ft.fetch(dest)
    assert (dest / "tool.exe").read_bytes() == b"tool"
    assert (dest / "licenses" / "T.txt").read_bytes() == b"gpl"
    assert (dest / "licenses" / "D.md").read_bytes() == b"mit"
    assert len(paths) == 3
    assert "https://x/a.zip" in (dest / "SOURCES.txt").read_text(encoding="utf-8")
    assert [p.name for p in tmp_path.iterdir()] == ["tools"]  # scratch folder cleaned up


def test_a_bad_download_leaves_the_existing_tools_untouched(ft, monkeypatch, tmp_path):
    _fake_world(ft, monkeypatch, {"https://x/L.md": b"tampered"})
    dest = tmp_path / "tools"
    dest.mkdir()
    (dest / "tool.exe").write_bytes(b"old")
    with pytest.raises(ft.ToolError, match="mismatch"):
        ft.fetch(dest)
    assert sorted(p.name for p in dest.iterdir()) == ["tool.exe"]
    assert (dest / "tool.exe").read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["tools"]


def test_main_verify_reports_failure(ft, tmp_path, capsys):
    assert ft.main(["--dest", str(tmp_path), "verify"]) == 1
    assert "fetch_tools.py fetch" in capsys.readouterr().err


def test_spec_builds_only_from_verified_tools():
    spec = (PACKAGING / "StuffDownloader.spec").read_text(encoding="utf-8")
    assert "fetch_tools.verify_staged(TOOLS_DIR)" in spec
    assert "for relative in fetch_tools.STAGED" in spec
    assert 'glob("*.exe")' not in spec
