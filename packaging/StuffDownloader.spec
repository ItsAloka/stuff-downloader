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
    datas=[],
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
