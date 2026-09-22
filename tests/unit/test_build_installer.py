"""packaging/build_installer.py: input checks, wheel pinning, licence staging, offline setup."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
import zipfile
from pathlib import Path

import pytest

PACKAGING = Path(__file__).resolve().parents[2] / "packaging"


@pytest.fixture(scope="module")
def bi():
    spec = importlib.util.spec_from_file_location(
        "build_installer", PACKAGING / "build_installer.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_installer"] = module
    spec.loader.exec_module(module)
    return module


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_every_engine_requirements_file_is_installed_previous_ytdlp_first(bi):
    names = [name for _engine, name in bi.ENGINE_INSTALLS]
    assert sorted(names) == sorted(
        p.name for p in (PACKAGING / "engine-requirements").glob("*.txt")
    )
    assert names.index("ytdlp-previous.txt") < names.index("ytdlp.txt")
    assert {engine for engine, _ in bi.ENGINE_INSTALLS} == {"ytdlp", "gallerydl", "spotdl"}


def test_pins_reads_exact_pins_with_extras_and_markers(bi, tmp_path):
    reqs = tmp_path / "r.txt"
    reqs.write_text(
        "# comment\n"
        "Yt_DLP[default]==2026.8.19 \\\n    --hash=sha256:" + "a" * 64 + "\n"
        "colorama==0.4.6 ; sys_platform == 'win32' \\\n    --hash=sha256:" + "b" * 64 + "\n",
        encoding="utf-8",
    )
    assert bi.pins(reqs) == {"yt-dlp": "2026.8.19", "colorama": "0.4.6"}
    assert bi.pinned_hashes([reqs]) == {"a" * 64, "b" * 64}


def test_inputs_fail_loudly_when_tools_are_not_verified(bi, tmp_path):
    with pytest.raises(bi.PayloadError, match="does not hold the pinned tools"):
        bi.check_inputs(tmp_path)


def test_a_failed_build_leaves_no_payload_behind(bi, monkeypatch, tmp_path):
    out = tmp_path / "payload"
    out.mkdir()
    (out / "old.txt").write_text("previous payload")
    monkeypatch.setattr(bi, "check_inputs", lambda tools: None)

    def boom(dest):
        (dest / "half-built").mkdir()
        raise bi.PayloadError("PyInstaller failed")

    monkeypatch.setattr(bi, "build_gui", boom)
    with pytest.raises(bi.PayloadError):
        bi.build_payload(out, tools_dir=tmp_path)
    assert (out / "old.txt").read_text() == "previous payload"  # the old payload is untouched
    assert not (tmp_path / "payload.building").exists()


def _wheel(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def test_only_wheels_with_a_pinned_hash_are_copied(bi, tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    good = _wheel(cache / "yt_dlp-2026.8.19-py3-none-any.whl", {"a": b"1"})
    _wheel(cache / "evil-1.0-py3-none-any.whl", {"a": b"2"})
    reqs = tmp_path / "r.txt"
    reqs.write_text(
        f"yt-dlp==2026.8.19 \\\n    --hash=sha256:{bi.sha256_file(good)}\n", encoding="utf-8"
    )
    copied = bi.copy_pinned_wheels(cache, tmp_path / "wheels", [reqs])
    assert [w.name for w in copied] == [good.name]
    assert sorted(p.name for p in (tmp_path / "wheels").iterdir()) == [good.name]


def test_a_pinned_package_without_a_wheel_fails(bi, tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    old = _wheel(cache / "yt_dlp-2026.7.4-py3-none-any.whl", {"a": b"1"})
    reqs = tmp_path / "r.txt"
    reqs.write_text(
        f"yt-dlp==2026.8.19 \\\n    --hash=sha256:{bi.sha256_file(old)}\n", encoding="utf-8"
    )
    with pytest.raises(bi.PayloadError, match=r"no pinned wheel for: yt-dlp==2026\.8\.19"):
        bi.copy_pinned_wheels(cache, tmp_path / "wheels", [reqs])


def test_wheel_licences_copies_only_dist_info_licence_files(bi, tmp_path):
    wheel = _wheel(
        tmp_path / "pkg-1.0-py3-none-any.whl",
        {
            "pkg/__init__.py": b"",
            "pkg/LICENSE": b"not the dist-info one",
            "pkg-1.0.dist-info/METADATA": b"meta",
            "pkg-1.0.dist-info/LICENSE.txt": b"mit",
            "pkg-1.0.dist-info/licenses/NOTICE": b"notice",
            "pkg-1.0.dist-info/licenses/../../../evil": b"x",
        },
    )
    written = bi.wheel_licences(wheel, tmp_path / "lic")
    assert sorted(written) == ["pkg-1.0/LICENSE.txt", "pkg-1.0/licenses/NOTICE"]
    assert (tmp_path / "lic" / "pkg-1.0" / "LICENSE.txt").read_bytes() == b"mit"
    assert not (tmp_path / "evil").exists()


def test_installed_licences_refuses_a_version_other_than_the_pin(bi, tmp_path):
    with pytest.raises(bi.PayloadError, match="requirements.lock pins 0.0.0"):
        bi.installed_licences("pytest", "0.0.0", tmp_path)
    with pytest.raises(bi.PayloadError, match="not installed"):
        bi.installed_licences("no-such-package-xyz", "1.0", tmp_path)


def test_the_onedir_check_refuses_frozen_engines_and_missing_tools(bi, monkeypatch, tmp_path):
    app = tmp_path / "StuffDownloader"
    (app / "_internal" / "yt_dlp").mkdir(parents=True)
    (app / "StuffDownloader.exe").write_bytes(b"exe")
    tools = app / "_internal" / "tools"
    tools.mkdir()
    with pytest.raises(bi.PayloadError, match="tools"):
        bi.check_onedir(app, tools)
    fetch_tools = bi._load("fetch_tools")
    monkeypatch.setattr(fetch_tools, "verify_staged", lambda d: [])
    monkeypatch.setattr(bi, "_load", lambda name: fetch_tools)
    with pytest.raises(bi.PayloadError, match="_internal/yt_dlp"):
        bi.check_onedir(app, tools)


def test_setup_runtime_installs_offline_in_order_and_restores_env(bi, monkeypatch, tmp_path):
    setup = tmp_path / "runtime setup"  # a space, as in real install paths
    (setup / "wheels").mkdir(parents=True)
    (setup / "python").mkdir()
    (setup / "python" / "python.exe").write_bytes(b"py")
    calls = []

    class FakeRuntime:
        RuntimeBuildError = RuntimeError

        @staticmethod
        def default_root():
            return tmp_path / "root"

        @staticmethod
        def base_python(root):
            return root / "python" / "python.exe"

        @staticmethod
        def build_base(root):
            calls.append(("base", root))

        @staticmethod
        def install_engine(root, engine, requirements):
            calls.append((engine, requirements.name, os.environ.get("PIP_NO_INDEX"),
                          os.environ.get("PIP_FIND_LINKS")))  # fmt: skip
            return f"{engine}-id"

    monkeypatch.setattr(bi, "PROJECT", setup)
    monkeypatch.setattr(bi, "_load", lambda name: FakeRuntime)
    monkeypatch.delenv("PIP_NO_INDEX", raising=False)
    monkeypatch.setenv("PIP_FIND_LINKS", "before")
    result = bi.setup_runtime()
    root = (tmp_path / "root").resolve()
    assert (root / "python" / "python.exe").read_bytes() == b"py"  # copied into place
    assert calls[0] == ("base", root)
    assert [c[:2] for c in calls[1:]] == list(bi.ENGINE_INSTALLS)
    wheels_url = (setup / "wheels").resolve().as_uri()
    assert " " not in wheels_url  # pip splits PIP_FIND_LINKS on whitespace
    assert all(c[2] == "1" and c[3] == wheels_url for c in calls[1:])
    assert len(result) == 4
    assert "PIP_NO_INDEX" not in os.environ
    assert os.environ["PIP_FIND_LINKS"] == "before"


def test_setup_runtime_refuses_a_folder_that_is_not_staged(bi, monkeypatch, tmp_path):
    monkeypatch.setattr(bi, "PROJECT", tmp_path)
    with pytest.raises(bi.PayloadError, match="not a staged runtime-setup folder"):
        bi.setup_runtime(tmp_path / "root")


def test_main_reports_a_missing_input_without_a_traceback(bi, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(bi, "PROJECT", tmp_path)
    assert bi.main(["payload", "--out", str(tmp_path / "out")]) == 1
    assert "error: LICENSE is missing" in capsys.readouterr().err


def test_a_successful_build_replaces_the_old_payload(bi, monkeypatch, tmp_path):
    out = tmp_path / "payload"
    out.mkdir()
    (out / "old.txt").write_text("previous payload")
    monkeypatch.setattr(bi, "check_inputs", lambda tools: None)
    monkeypatch.setattr(bi, "build_gui", lambda dest: (dest / "StuffDownloader").mkdir())
    for step in ("stage_python", "stage_runtime_setup"):
        monkeypatch.setattr(bi, step, lambda setup: None)
    monkeypatch.setattr(bi, "download_wheels", lambda wheels: [])
    monkeypatch.setattr(bi, "stage_licences", lambda lic, wheels, tools: lic.mkdir())
    monkeypatch.setattr(bi, "payload_manifest", lambda root: "manifest")
    assert bi.build_payload(out, tools_dir=tmp_path) == out
    assert sorted(p.name for p in out.iterdir()) == ["PAYLOAD.txt", "StuffDownloader", "licences",
                                                     "runtime-setup"]  # fmt: skip
    assert sorted(p.name for p in tmp_path.iterdir()) == ["payload"]


def test_an_old_payload_that_cannot_be_moved_fails_loudly(bi, monkeypatch, tmp_path):
    out = tmp_path / "payload"
    out.mkdir()
    monkeypatch.setattr(bi, "check_inputs", lambda tools: None)
    monkeypatch.setattr(bi, "build_gui", lambda dest: None)
    for step in ("stage_python", "stage_runtime_setup"):
        monkeypatch.setattr(bi, step, lambda setup: None)
    monkeypatch.setattr(bi, "download_wheels", lambda wheels: [])
    monkeypatch.setattr(bi, "stage_licences", lambda lic, wheels, tools: None)
    monkeypatch.setattr(bi, "payload_manifest", lambda root: "")
    monkeypatch.setattr(bi.time, "sleep", lambda s: None)
    real_rename = Path.rename

    def locked(self, target):
        if self == out:
            raise PermissionError("in use")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", locked)
    with pytest.raises(bi.PayloadError, match="is something in it open"):
        bi.build_payload(out, tools_dir=tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["payload"]


def _stub_build(bi, monkeypatch):
    monkeypatch.setattr(bi, "check_inputs", lambda tools: None)
    monkeypatch.setattr(bi, "build_gui", lambda dest: (dest / "new.txt").write_text("new"))
    for step in ("stage_python", "stage_runtime_setup"):
        monkeypatch.setattr(bi, step, lambda setup: None)
    monkeypatch.setattr(bi, "download_wheels", lambda wheels: [])
    monkeypatch.setattr(bi, "stage_licences", lambda lic, wheels, tools: None)
    monkeypatch.setattr(bi, "payload_manifest", lambda root: "")
    monkeypatch.setattr(bi.time, "sleep", lambda s: None)


def test_a_briefly_locked_payload_is_retried(bi, monkeypatch, tmp_path):
    out = tmp_path / "payload"
    out.mkdir()
    _stub_build(bi, monkeypatch)
    real_rename = Path.rename
    failures = {"left": 3}

    def flaky(self, target):
        if self.name == "payload.building" and failures["left"]:
            failures["left"] -= 1
            raise PermissionError("scanning")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky)
    bi.build_payload(out, tools_dir=tmp_path)
    assert (out / "new.txt").read_text() == "new"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["payload"]


def test_the_old_payload_comes_back_if_the_new_one_cannot_move_in(bi, monkeypatch, tmp_path):
    out = tmp_path / "payload"
    out.mkdir()
    (out / "old.txt").write_text("previous payload")
    _stub_build(bi, monkeypatch)
    real_rename = Path.rename

    def stuck(self, target):
        if self.name == "payload.building":
            raise PermissionError("in use")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", stuck)
    with pytest.raises(bi.PayloadError, match="is something in it open"):
        bi.build_payload(out, tools_dir=tmp_path)
    assert (out / "old.txt").read_text() == "previous payload"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["payload"]
