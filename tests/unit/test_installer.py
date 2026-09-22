"""packaging/installer.iss and the `build_installer.py installer` command."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ISS = ROOT / "packaging" / "installer.iss"


@pytest.fixture(scope="module")
def bi():
    spec = importlib.util.spec_from_file_location(
        "build_installer", ROOT / "packaging" / "build_installer.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_installer"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def iss() -> str:
    return ISS.read_text(encoding="utf-8")


def _section(text: str, name: str) -> str:
    match = re.search(rf"^\[{name}\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S)
    assert match, f"[{name}] missing"
    return match.group(1)


def _setup(text: str) -> dict[str, str]:
    pairs = (line.split("=", 1) for line in _section(text, "Setup").splitlines() if "=" in line)
    return {k.strip(): v.strip() for k, v in pairs}


def test_installs_per_user_without_admin(iss):
    setup = _setup(iss)
    assert setup["PrivilegesRequired"] == "lowest"
    assert setup["DefaultDirName"].startswith("{autopf}")
    assert setup["OutputBaseFilename"] == "StuffDownloader-Setup-{#AppVersion}"
    assert setup["AppVersion"] == "{#AppVersion}"


def test_ships_gui_runtime_setup_and_licences(iss):
    files = _section(iss, "Files")
    for source, dest in [
        (r"{#PayloadDir}\StuffDownloader\*", '"{app}"'),
        (r"{#PayloadDir}\runtime-setup\*", r'"{app}\runtime-setup"'),
        (r"{#PayloadDir}\licences\*", r'"{app}\licences"'),
    ]:
        assert f'Source: "{source}"; DestDir: {dest}' in files


def test_shortcuts_start_menu_and_optional_desktop(iss):
    icons = _section(iss, "Icons")
    assert r'Name: "{group}\{#AppName}"' in icons
    assert "{autodesktop}" in icons and "Tasks: desktopicon" in icons
    assert "Flags: unchecked" in _section(iss, "Tasks")


def test_runs_setup_runtime_after_copying_and_reports_failure(iss):
    code = _section(iss, "Code")
    assert "CurStep = ssPostInstall" in code
    assert r"{app}\runtime-setup\python\python.exe" in code
    assert r"{app}\runtime-setup\packaging\build_installer.py" in code
    assert "setup-runtime" in code
    assert "ResultCode = 0" in code and "MsgBox" in code
    # the app is only offered for launch when its engines are ready
    assert "Check: RuntimeReady" in _section(iss, "Run")


def test_code_brace_comments_do_not_nest(iss):
    # Pascal { } comments end at the first "}", so a {constant} inside one breaks the compile.
    code = re.sub(r"'[^']*'|//[^\n]*", "", _section(iss, "Code"))
    for comment in re.findall(r"\{[^}]*\}", code):
        assert "{" not in comment[1:], f"nested brace in [Code] comment: {comment!r}"


def test_uninstall_never_touches_user_data(iss):
    # Everything Setup installs or deletes is declared before [Code], which only runs setup.
    declarative = iss.split("\n[Code]", 1)[0]
    body = "\n".join(
        line for line in declarative.splitlines() if line.strip() and not line.startswith(";")
    )
    assert "{localappdata}" not in body.lower()
    assert "StuffDownloader\\runtime" not in body
    for line in _section(iss, "UninstallDelete").splitlines():
        if line.strip() and not line.startswith(";"):
            assert 'Name: "{app}\\' in line


def test_no_signing_or_publishing(iss):
    assert "SignTool" not in iss and "SignedUninstaller" not in iss


def test_find_iscc_prefers_env_and_fails_loudly(bi, monkeypatch, tmp_path):
    fake = tmp_path / "ISCC.exe"
    fake.write_bytes(b"")
    monkeypatch.setenv("ISCC", str(fake))
    assert bi.find_iscc() == fake
    monkeypatch.delenv("ISCC")
    monkeypatch.setattr(bi.shutil, "which", lambda name: None)
    for var in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        monkeypatch.setenv(var, str(tmp_path / "nowhere"))
    with pytest.raises(bi.PayloadError, match="ISCC"):
        bi.find_iscc()


def _payload(root: Path) -> Path:
    for part in ("StuffDownloader/StuffDownloader.exe", "runtime-setup/python/python.exe",
                 "licences/LICENSE", "PAYLOAD.txt"):  # fmt: skip
        (root / part).parent.mkdir(parents=True, exist_ok=True)
        (root / part).write_bytes(b"x")
    return root


def test_build_setup_runs_iscc_with_version_and_paths(bi, monkeypatch, tmp_path):
    payload = _payload(tmp_path / "payload")
    out = tmp_path / "out"
    calls = []

    def run(cmd, check):
        calls.append(cmd)
        (out / "StuffDownloader-Setup-1.0.0.exe").write_bytes(b"MZ")

    monkeypatch.setattr(bi, "find_iscc", lambda: Path("C:/iscc/ISCC.exe"))
    monkeypatch.setattr(bi, "_release_version", lambda: "1.0.0")
    monkeypatch.setattr(bi.subprocess, "run", run)
    monkeypatch.setattr(bi, "build_payload", lambda p: pytest.fail("payload rebuilt"))
    setup = bi.build_setup(payload, out, rebuild_payload=False)
    assert setup == out / "StuffDownloader-Setup-1.0.0.exe"
    (cmd,) = calls
    assert "/DAppVersion=1.0.0" in cmd and f"/DPayloadDir={payload}" in cmd
    assert f"/DOutputDir={out}" in cmd and cmd[-1] == str(bi.ISS)


def test_build_setup_refuses_incomplete_payload_and_missing_output(bi, monkeypatch, tmp_path):
    monkeypatch.setattr(bi, "find_iscc", lambda: Path("ISCC.exe"))
    monkeypatch.setattr(bi, "_release_version", lambda: "1.0.0")
    monkeypatch.setattr(bi.subprocess, "run", lambda cmd, check: None)
    (tmp_path / "empty").mkdir()
    with pytest.raises(bi.PayloadError, match="not a complete payload"):
        bi.build_setup(tmp_path / "empty", tmp_path / "out", rebuild_payload=False)
    with pytest.raises(bi.PayloadError, match="was not produced"):
        bi.build_setup(_payload(tmp_path / "p"), tmp_path / "out", rebuild_payload=False)


def test_iscc_failure_is_reported_by_main(bi, monkeypatch, capsys):
    def boom(*a, **k):
        raise subprocess.CalledProcessError(2, ["ISCC.exe", "/Q", "/DAppVersion=1.0.0"])

    monkeypatch.setattr(bi, "build_setup", boom)
    assert bi.main(["installer", "--skip-payload"]) == 1
    assert "error:" in capsys.readouterr().err


def test_release_version_is_1_0_0(bi):
    assert bi._release_version() == "1.0.0"
