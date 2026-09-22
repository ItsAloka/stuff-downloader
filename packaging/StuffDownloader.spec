# PyInstaller spec: GUI-only onedir build of Stuff Downloader (plan §8, §8.1).
#
#   .venv\Scripts\pyinstaller.exe --noconfirm --clean packaging\StuffDownloader.spec
#
# Output: dist\StuffDownloader\StuffDownloader.exe. No UPX.
# Engines (yt-dlp, gallery-dl, spotDL) and the worker package are never frozen in: workers run in
# the separate engine runtime (core/runner.py), never by re-running this exe.

import importlib.util
import sys
from pathlib import Path

from PyInstaller.utils.hooks import copy_metadata

ROOT = Path(SPECPATH).parent
SRC = ROOT / "src"

# A plain script entry so the package's relative imports keep working when frozen.
ENTRY = Path(workpath) / "stuff_downloader_entry.py"
ENTRY.parent.mkdir(parents=True, exist_ok=True)
ENTRY.write_text(
    "import sys\nfrom stuff_downloader.__main__ import main\nsys.exit(main())\n", encoding="utf-8"
)

# The GUI shells out to ffmpeg/ffprobe (merge, transcode, cover art) and resolves them through
# core.tools.app_tools_dir(), which is `<exe dir>\tools` when frozen. Without them the build
# produces an app that launches, self-tests and then cannot finish a single real download.
#
# The repo's git-ignored tools\ folder is filled by packaging/fetch_tools.py from official URLs
# pinned by SHA-256. The build takes only the pinned files, and only after every one of them
# matches its pin: a missing or altered tool stops the build instead of shipping it.
TOOLS_DIR = ROOT / "tools"
_fetch_spec = importlib.util.spec_from_file_location("fetch_tools", ROOT / "packaging" / "fetch_tools.py")
fetch_tools = importlib.util.module_from_spec(_fetch_spec)
sys.modules["fetch_tools"] = fetch_tools  # dataclasses look their module up here
_fetch_spec.loader.exec_module(fetch_tools)
try:
    fetch_tools.verify_staged(TOOLS_DIR)
except fetch_tools.ToolError as exc:
    raise SystemExit(f"ERROR: {exc}") from None
# The licence texts go with them (tools\licenses), as FFmpeg's GPLv3 requires (§8.3 F3).
TOOL_DATAS = [
    (str(TOOLS_DIR / relative), str(Path("tools", relative).parent))
    for relative in fetch_tools.STAGED
]

# The app icon, and the licence texts the About dialog opens (stuff_downloader.data_root()).
RESOURCES = SRC / "stuff_downloader" / "resources"
ICON = RESOURCES / "app.ico"
# The app icon plus the theme's PNGs (combo arrow, check mark), which gui/theme.py loads.
APP_DATAS = [
    (str(path), "resources")
    for path in sorted({*RESOURCES.glob("app.*"), *RESOURCES.glob("*.png")})
    if path.is_file()
]
for name in ("LICENSE", "THIRD_PARTY_LICENSES.txt"):
    if not (ROOT / name).is_file():
        raise SystemExit(f"{name} is missing: a build must ship its licence texts")
    APP_DATAS.append((str(ROOT / name), "."))
# dist-info, so the About dialog reads the release version from metadata rather than a fallback.
APP_DATAS += copy_metadata("stuff-downloader")

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
    datas=TOOL_DATAS + APP_DATAS,
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
    icon=str(ICON),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="StuffDownloader",
)
