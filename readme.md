# Stuff Downloader

Private, local Windows desktop downloader (Python 3.11 + PyQt6). See `plan.md` for the full plan.

Current state: **M0 foundation** — settings, tool health, JSON-lines worker protocol with a fake
engine, runner with cancel, and a minimal GUI shell (Downloads / Tools / Settings).
No real media engines yet.

## Dev setup

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-dev.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
```

Dependencies are locked with SHA-256 hashes: `requirements.lock` (runtime) and
`requirements-dev.lock` (runtime + dev tools). After changing `pyproject.toml`, regenerate both:

```powershell
uv pip compile pyproject.toml --generate-hashes --python-version 3.11 --python-platform windows -o requirements.lock
uv pip compile pyproject.toml --extra dev --generate-hashes --python-version 3.11 --python-platform windows -o requirements-dev.lock
```

## Run

```powershell
.\.venv\Scripts\python.exe -m stuff_downloader            # GUI
.\.venv\Scripts\python.exe -m stuff_downloader --self-test
.\.venv\Scripts\python.exe -m stuff_downloader_worker --self-test
```

## Check

```powershell
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m pytest
```

## Build the installer

Personal use only: the installer includes the spotDL environment (`THIRD_PARTY_LICENSES.txt`
F2, F5). Do not sign, publish or share what this builds.

Needs [Inno Setup 6](https://jrsoftware.org/isinfo.php) (`ISCC.exe` on PATH, in its default
install folder, or named by the `ISCC` environment variable).

```powershell
.\.venv\Scripts\python.exe packaging\fetch_tools.py fetch      # once: ffmpeg, ffprobe, deno (SHA-256 pinned)
.\.venv\Scripts\python.exe packaging\build_installer.py installer
```

The second command rebuilds `dist\payload\` from scratch, then compiles
`packaging\installer.iss` into `dist\StuffDownloader-Setup-<version>.exe`
(`--skip-payload` reuses an existing payload; `payload` alone builds only the payload).
The payload holds the frozen GUI (`StuffDownloader\`), the pinned CPython with every
hash-checked engine wheel (`runtime-setup\`), `licences\` and `PAYLOAD.txt`. It stops with an
error if any input is missing or fails its hash.

The installer needs no administrator rights. It installs to
`%LOCALAPPDATA%\Programs\Stuff Downloader`, adds a Start-menu shortcut (desktop shortcut
optional), then runs `runtime-setup\python\python.exe runtime-setup\packaging\build_installer.py
setup-runtime`, which builds the engine environments offline under
`%LOCALAPPDATA%\StuffDownloader\runtime` (a venv cannot be moved after it is created). If that
step fails, Setup says so and leaves its output in `setup-runtime.log` in the install folder;
running Setup again retries it. Uninstalling removes only the program files: settings, the
engine runtime and downloads stay.

## Layout

- `src/stuff_downloader/core` — no Qt, no engine imports.
- `src/stuff_downloader/gui` — PyQt6 shell.
- `src/stuff_downloader_worker` — separate worker program; no Qt. Speaks JSON lines on stdout.
