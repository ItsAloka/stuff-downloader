# PyInstaller spec: GUI-only onedir build of Stuff Downloader (plan §8, §8.1).
#
#   .venv\Scripts\pyinstaller.exe --noconfirm --clean packaging\StuffDownloader.spec
#
# Output: dist\StuffDownloader\StuffDownloader.exe. No UPX.
# Engines (yt-dlp, gallery-dl, spotDL) and the worker package are never frozen in: workers run in
# the separate engine runtime (core/runner.py), never by re-running this exe.

from pathlib import Path

ROOT = Path(SPECPATH).parent
SRC = ROOT / "src"

# A plain script entry so the package's relative imports keep working when frozen.
ENTRY = Path(workpath) / "stuff_downloader_entry.py"
ENTRY.parent.mkdir(parents=True, exist_ok=True)
ENTRY.write_text(
    "import sys\nfrom stuff_downloader.__main__ import main\nsys.exit(main())\n", encoding="utf-8"
)

# The GUI shells out to ffmpeg/ffprobe (merge, transcode, cover art) and resolves them through
# core.tools.app_tools_dir(), which is `<exe dir>\tools` when frozen. Without this the build
# produces an app that launches, self-tests and then cannot finish a single real download --
# verified: before this was here, a frozen --self-test reported all three tools "not found".
#
# This copies whatever is in the repo's git-ignored tools\ folder. Plan §8 wants them fetched
# from official URLs pinned by SHA-256 by packaging/fetch_tools.py, which does not exist yet;
# writing it, and the licence audit in §8.3, are M6 gates before anything is distributed.
TOOLS_DIR = ROOT / "tools"
TOOL_FILES = sorted(TOOLS_DIR.glob("*.exe")) if TOOLS_DIR.is_dir() else []
if not TOOL_FILES:
    # Loud, not silent: a toolless build is the failure mode this block exists to prevent.
    print("WARNING: no tools found in tools\\ -- the built app will not be able to convert media")

ENGINE_EXCLUDES = [
    "stuff_downloader_worker",
    "yt_dlp",
    "yt_dlp_ejs",
    "curl_cffi",
    "gallery_dl",
    "spotdl",
    "mutagen",
]

a = Analysis(
    [str(ENTRY)],
    pathex=[str(SRC)],
    binaries=[],
    datas=[(str(path), "tools") for path in TOOL_FILES],
    hiddenimports=["stuff_downloader.app"],
    hookspath=[],
    runtime_hooks=[],
    excludes=ENGINE_EXCLUDES + ["tkinter", "pytest", "pytestqt"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="StuffDownloader",
    console=False,
    debug=False,
    strip=False,
    upx=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="StuffDownloader",
)
