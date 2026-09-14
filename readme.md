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

## Layout

- `src/stuff_downloader/core` — no Qt, no engine imports.
- `src/stuff_downloader/gui` — PyQt6 shell.
- `src/stuff_downloader_worker` — separate worker program; no Qt. Speaks JSON lines on stdout.
