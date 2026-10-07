<h1 align="center">
  <img src="src/stuff_downloader/resources/app.png" width="72" alt=""><br>
  Stuff Downloader
</h1>

<p align="center">
  A clean desktop app for downloading videos, music and image galleries from public links.<br>
  Paste a link, press <b>Analyze</b>, pick a format and you're done.
</p>

<p align="center">
  <a href="https://github.com/AlokaWarnakula/stuff-downloader/releases/latest"><img alt="Latest release" src="https://img.shields.io/github/v/release/AlokaWarnakula/stuff-downloader"></a>
  <img alt="Platform" src="https://img.shields.io/badge/platform-Windows%2010%2F11%20%7C%20macOS%20(soon)-blue">
  <img alt="Python" src="https://img.shields.io/badge/python-3.11-3776AB">
  <img alt="Qt" src="https://img.shields.io/badge/GUI-PyQt6-41CD52">
  <img alt="Tests" src="https://img.shields.io/badge/tests-1757%20passing-brightgreen">
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/license-MIT-green"></a>
</p>

<p align="center">
  <img src="docs/screenshots/analyze-video.png" width="860" alt="Analyzing a YouTube video: every available quality with a one-click download">
</p>

---

## Contents

- [Features](#features)
- [Screenshots](#screenshots)
- [Download and install](#download-and-install)
- [How to use it](#how-to-use-it)
- [Supported links](#supported-links)
- [Signing in (optional)](#signing-in-optional)
- [Architecture](#architecture)
- [Security and reliability](#security-and-reliability)
- [Build from source](#build-from-source)
- [Project layout](#project-layout)
- [Roadmap](#roadmap)
- [Licenses and third-party source](#licenses-and-third-party-source)
- [Disclaimer](#disclaimer)

## Features

- **One box for every link.** Paste YouTube, YouTube Music, Spotify, Apple Music, Deezer, Instagram,
  TikTok, X/Twitter, Facebook, Reddit or a direct file link. The app detects what the link is and
  shows the right options.
- **Playlist and album preview.** See every track with its cover, artist and length. Tick the ones
  you want, filter by title, rename files before you download, and skip songs you already have.
- **Presets instead of settings screens.** For example: *MP3 music (square cover + tags)*, *Best
  video*, *Up to 1080p*, *Up to 720p (small)*, *Original audio (no re-encode)* and *Thumbnail only*.
- **Music with proper metadata.** Tracks from Spotify, Apple Music and Deezer are matched to
  YouTube Music (via `ytmusicapi`), then tagged with title, artist, album and square cover art.
  When a match isn't certain, the app asks you instead of guessing. You don't need a Spotify
  account or API keys: a share link is enough.
- **Galleries.** Download photo and video posts and carousels (through `gallery-dl`) with a
  grid preview.
- **A real download queue.** Run several downloads at the same time, pause, resume, cancel and retry,
  follow live speed and progress, and use *Open* / *Show in folder* when a download finishes.
- **History.** A searchable list of everything you've downloaded, with multi-select removal.
- **Self-updating engines.** When the app starts, it checks PyPI for newer `yt-dlp`, `gallery-dl`,
  `ytmusicapi`, the sign-in browser (`PyQt6-WebEngine`) and so on, and offers them in an
  *Updates available* dialog, a bit like `apt list --upgradable`. Each update is installed into
  a new environment and self-tested before it goes live, and you can roll it back with one click.
- **Dark, responsive UI.** The layout works at small, medium and maximized window sizes.

## Screenshots

These are real screenshots taken while downloading Blender's open movie *Big Buck Bunny* (CC-BY).

| Pick an audio format (MP3 with tags and cover) | A live queue: parallel downloads, speed, ETA, pause and resume |
|---|---|
| <img src="docs/screenshots/analyze-audio.png" alt="Audio formats"> | <img src="docs/screenshots/queue-live.png" alt="Live download queue"> |
| **Playlist preview: choose tracks, rename, skip duplicates** | **Gallery preview** |
| <img src="docs/screenshots/responsive-playlist-medium.png" alt="Playlist preview"> | <img src="docs/screenshots/gallery-format-medium.png" alt="Gallery preview"> |

## Download and install

### Windows 10 / 11 (64-bit)

1. Go to the [**latest release**](https://github.com/AlokaWarnakula/stuff-downloader/releases/latest).
2. Download `StuffDownloader-Setup-1.4.0.exe`.
3. Run it. The installer isn't code-signed, so Windows SmartScreen may show a warning. Click
   **More info → Run anyway**.
4. Choose whether to install the optional **spotDL** engine (it's ticked by default).

Everything the app needs comes with it: FFmpeg, FFprobe, Deno and the Python engines. You don't
have to install Python or anything else.

### macOS

A macOS build is being worked on by a contributor and will be attached to a future release.

## How to use it

1. Copy a link from your browser or a share button.
2. Click **Paste** (or press `Ctrl+V`), then **Analyze**.
3. Pick a **preset**. For playlists and albums, tick the items you want.
4. Click **Download**. Files are saved to the folder shown under the link box. You can change
   it in **Settings**.

The **Tools** dialog shows the health of each bundled engine and has a **Check for updates** button.

## Supported links

| Source | What you get | Engine |
|---|---|---|
| YouTube / YouTube Music: videos, Shorts, playlists | Video (best / 1080p / 720p), MP3, original audio, thumbnail | yt-dlp |
| Spotify: track, album, playlist (share link) | Tagged MP3 with cover art | ytmusicapi + yt-dlp, optional spotDL |
| Apple Music, Deezer: track, album, playlist | Tagged MP3 with cover art | ytmusicapi + yt-dlp |
| Instagram, TikTok, X/Twitter, Facebook, Reddit | Videos, photos, carousels, TikTok slideshows | yt-dlp / gallery-dl / built-in |
| Vimeo, Dailymotion, SoundCloud, Bandcamp, Twitch, Pinterest, Imgur, Flickr, Archive.org | Video or audio, best quality available | yt-dlp |
| Other public video pages | Whatever yt-dlp supports for that site | yt-dlp |
| Direct file links | The file itself | built-in |

Public links work with no account. A few posts are only shown to signed-in people; for those,
see **Signing in** below. The app never asks for, sees or stores your passwords.

## Signing in (optional)

Private accounts, age-restricted videos and some sites only show posts to people who are
signed in. When a download fails for that reason, the app shows a **Sign in to *site*…**
button. You can also sign in ahead of time from **Settings → Site logins**.

1. Click **Sign in…**. The site's own sign-in page opens inside Stuff Downloader.
2. Sign in the way you normally would.
3. When you can see you are signed in, click **Done**.

Your password goes only to the site. Stuff Downloader keeps the site's "signed in" cookie in
`%LOCALAPPDATA%\StuffDownloader\signins\` and uses it only for that site's links.
**Settings → Site logins → Sign out** deletes it.

You don't need Chrome, Edge or Brave for this. On Windows they lock and encrypt their cookies,
so no other app can read them. If you already use Firefox or a `cookies.txt` export, the sign-in
dialog has these under **Other ways**.

## Architecture

```text
┌───────────────────────────── StuffDownloader.exe (PyQt6) ─────────────────────────────┐
│  gui/        pages, queue widgets, theme            ← the only app code that imports Qt │
│  core/       router · presets · scheduler · runner · history · settings · updates      │
└──────────────┬──────────────────────────────────────────────────────────────────────────┘
               │ spawns one worker process per job, JSON-lines protocol over stdio
               ▼
   ┌─────────────────┐  ┌──────────────────┐  ┌───────────────────────┐  ┌──────────────────┐
   │ yt-dlp env       │  │ gallery-dl env    │  │ music env / spotDL env │  │ sign-in env       │
   │ (video + audio)  │  │ (galleries)       │  │ (catalog matching)     │  │ (PyQt6-WebEngine) │
   └─────────────────┘  └──────────────────┘  └───────────────────────┘  └──────────────────┘
          each engine runs in its own versioned, hash-pinned Python environment
```

A few design decisions:

- **A worker process per job.** A crash or hang in a download engine can never freeze the UI.
  Cancelling a job kills its process tree cleanly.
- **Separate environments per engine.** yt-dlp, gallery-dl and spotDL have conflicting
  dependencies and different licences, so each one lives in its own environment under
  `envs/<engine>/<id>`. An `active.json` pointer selects the live one.
- **Safe engine updates.** An update builds a *new* environment, runs the worker self-test in it,
  and only then switches `active.json`. The previous environment is kept for rollback, and the app
  switches back automatically if the first jobs after an update all fail to start.
- **The sign-in browser is an engine too.** The **Sign in** window runs as its own process, from
  its own environment (`PyQt6-WebEngine`), so it gets the same update offers, self-test and undo
  as the download engines, and it never shares a process with gallery-dl.
- **`core/` has no Qt imports.** The download logic can be tested without a GUI and reused by
  another front end (such as the macOS port).
- **Every URL is validated** (HTTPS only for network fetches, host allow-lists per site) before
  anything uses it.

## Security and reliability

- All dependencies are locked with **SHA-256 hashes** (`requirements.lock`, `requirements-dev.lock`,
  `packaging/engine-requirements/*.txt`) and installed with `--require-hashes`.
- The bundled FFmpeg and Deno binaries are **pinned by SHA-256** in `packaging/fetch_tools.py`.
- Engine updates only come from PyPI over HTTPS, and yanked and pre-release versions are ignored.
  Updates to non-yt-dlp libraries stay within the same major version.
- **1800+ automated tests**: unit tests, GUI tests with `pytest-qt` (including screenshot tests of
  the layout at three window sizes), and opt-in network tests. The code is linted with `ruff`.

## Build from source

Requires **Windows** and **Python 3.11**.

```powershell
git clone https://github.com/AlokaWarnakula/stuff-downloader.git
cd stuff-downloader
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-dev.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
```

Run the app and its checks:

```powershell
.\.venv\Scripts\python.exe -m stuff_downloader              # start the GUI
.\.venv\Scripts\python.exe -m stuff_downloader --self-test  # quick health check
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m pytest
```

After you change `pyproject.toml`, regenerate the hash-locked requirements:

```powershell
uv pip compile pyproject.toml --generate-hashes --python-version 3.11 --python-platform windows -o requirements.lock
uv pip compile pyproject.toml --extra dev --generate-hashes --python-version 3.11 --python-platform windows -o requirements-dev.lock
```

### Build the installer

You need [Inno Setup 6](https://jrsoftware.org/isinfo.php). `ISCC.exe` must be on your `PATH`, in its
default folder, or named by the `ISCC` environment variable.

```powershell
.\.venv\Scripts\python.exe packaging\fetch_tools.py fetch          # once: ffmpeg, ffprobe, deno (SHA-256 pinned)
.\.venv\Scripts\python.exe packaging\build_installer.py installer  # → dist\StuffDownloader-Setup-<version>.exe
```

The app is frozen with PyInstaller (`packaging/StuffDownloader.spec`), and the engine wheels are
staged into an offline payload that the installer sets up on first run.

## Project layout

```text
src/stuff_downloader/
  core/        URL router, presets, scheduler, worker runner, history, settings, engine updates
  gui/         main window, pages (Downloads, History, Tools, Settings), widgets, theme
  resources/   icons
packaging/     PyInstaller spec, Inno Setup script, payload and runtime builders, pinned engine requirements
tests/         unit/, gui/ (pytest-qt + screenshots), convert/, network/ (opt-in)
```

## Roadmap

- [x] YouTube video/audio, playlists, presets, queue, history
- [x] Galleries (Instagram, X, Reddit…) via gallery-dl
- [x] Spotify, Apple Music and Deezer links with no account needed
- [x] In-app engine updates with self-test and rollback (**1.2.0**)
- [x] Self-repairing history database; errors logged instead of closing the app (**1.3.0**)
- [x] Built-in **Sign in** window for posts that need an account, updated like the engines;
      Vimeo, TikTok slideshows and heightless video files (Facebook, Flickr, Imgur) work again
      (**1.4.0**)
- [ ] macOS build
- [ ] Faster failure on sites that block the connection (e.g. Reddit timeouts)

## Licenses and third-party source

Stuff Downloader's own source code is released under the **[MIT License](LICENSE)**.

The Windows installer also contains third-party software under its own licences. The full list,
with every package, version and licence, is in [`THIRD_PARTY_LICENSES.txt`](THIRD_PARTY_LICENSES.txt),
and the licence texts are installed into the `licences\` folder next to the app. Some bundled
components are licensed under the **GNU GPL**. As required, their complete corresponding source
code is available at these links:

| Component | Version | Licence | Source |
|---|---|---|---|
| FFmpeg / FFprobe (gyan.dev "essentials" build) | 9.0.1 | GPL-3.0 | [FFmpeg source](https://ffmpeg.org/releases/ffmpeg-9.0.1.tar.xz) · [build](https://github.com/GyanD/codexffmpeg/releases/tag/9.0.1) |
| gallery-dl | 1.32.13 | GPL-2.0-only | [PyPI sdist](https://pypi.org/project/gallery-dl/1.32.13/#files) · [GitHub](https://github.com/mikf/gallery-dl/tree/v1.32.13) |
| spotapi (optional spotDL engine) | 1.2.8 | GPL-3.0 | [PyPI sdist](https://pypi.org/project/spotapi/1.2.8/#files) |
| pykakasi, mutagen, unidecode | see list | GPL | source on PyPI at the pinned versions in `packaging/engine-requirements/` |
| Deno | 2.9.6 | MIT | [GitHub](https://github.com/denoland/deno/tree/v2.9.6) |

If any of these links stop working, open an issue and I'll provide the source directly.

## Disclaimer

Stuff Downloader is a personal project made for learning. Only download content that you have the
right to download, and respect each platform's terms of service and your local copyright law. This
project isn't affiliated with YouTube, Spotify, Apple, Deezer, Meta, TikTok, X or Reddit.

---

<p align="center">Made by <a href="https://github.com/AlokaWarnakula">Aloka Warnakula</a></p>
