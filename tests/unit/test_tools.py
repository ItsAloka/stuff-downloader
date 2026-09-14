import sys

from stuff_downloader.core import tools


def test_missing_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)
    status = tools.check_tool("ffmpeg", tools_dir=tmp_path)
    assert not status.ok and status.source == "missing" and status.error == "not found"


def test_discovery_order_configured_then_app_then_path(tmp_path, monkeypatch):
    monkeypatch.setattr(tools.shutil, "which", lambda name: "C:/on/path/" + name)
    exe = tools._exe_name("ffmpeg")
    assert tools.find_tool("ffmpeg", tools_dir=tmp_path) == ("C:/on/path/ffmpeg", "path")
    (tmp_path / exe).write_text("")
    assert tools.find_tool("ffmpeg", tools_dir=tmp_path)[1] == "app"
    configured = tmp_path / "custom.exe"
    configured.write_text("")
    assert tools.find_tool("ffmpeg", str(configured), tmp_path) == (str(configured), "configured")


def test_check_tool_reads_version_from_real_executable(tmp_path):
    # Use the Python interpreter as a stand-in tool that answers --version.
    status = tools.check_tool("deno", configured=sys.executable, tools_dir=tmp_path)
    assert status.ok and status.source == "configured"
    assert status.version and "Python" in status.version
