# Stuff Downloader — Project Plan

> A private, local Windows desktop app (Python + PyQt6, shipped as a `.exe`) that downloads
> videos, playlists, MP3s and images from the web. No ads, no third-party servers, nothing to pay.

Status: **planning** · Written 2026-09-14 by the DevTeam (Claude: plan, Codex: research + review).
Source inputs: the owner's task brief and room messages, `Youtube_Playlist_downloader.txt`,
`Youtube_Single_Songs.txt`, and Codex's feasibility research
(`knowledge/workflows/work-5711-…toolchain-for-stuff-downloader.md`).

---

## 1. Goals

0. **No login, no account, no subscription.** Install the `.exe`, paste a public link, and it
   downloads. First run asks for nothing except (optionally) where to save. Signing in to the
   *source* site is never required for public content (see §6.4 for the optional advanced case).
1. **One paste box for everything.** Paste a link → the app detects what it is → shows a preview →
   you pick the quality → it downloads.
2. **Replace the three sites the owner uses today** (feature parity targets, *UX references only*):
   | Site today | What we replicate locally |
   |---|---|
   | y2mate | YouTube link → pick MP4 resolution or MP3 → download |
   | 9xbuddy | Link from almost any site → list every available quality/format → download |
   | spotisaver | Spotify track/album/playlist → matched audio → tagged MP3 with Spotify metadata and cover |
3. **Turn the two ChatGPT yt-dlp recipes into buttons.** MP3 with embedded square cover art,
   metadata, `Artist - Title.mp3` names, single songs *and* whole playlists.
4. **Resolution selector.** Show only the resolutions that really exist for that link.
5. **A good-looking download UI.** Live progress bars per item (speed, ETA, size, stage), queue,
   pause/cancel/retry, history.
6. **Save location is selectable, default = the Windows Downloads folder.**
7. **Ship as a Windows `.exe`** with an installer; no Python or manual ffmpeg install needed.
8. **Trustworthy.** Everything runs on the owner's PC. The only network calls go to the site
   being downloaded from, and to the official update sources when the owner clicks "Update engines".

### Out of scope (deliberately)

- **No DRM circumvention.** Widevine/PlayReady/FairPlay streams (Netflix, Disney+, Spotify's own
  audio stream, most paid-course platforms) are detected and reported as *unsupported*.
- **No paywall or login bypass.** Cookies only let the app see what the owner's own account can
  already see. Content that has to be paid for stays paid for.
- **No calling or scraping y2mate / 9xbuddy / spotisaver.** That would leak links and cookies to
  them and add ads/availability risk. We use the same open-source engines they are largely built on.
- **Personal use.** Respect each site's terms and copyright; the owner decides what they download.

---

## 2. How "download anything" works

Online video almost never arrives as one file. It comes as **HLS (`.m3u8`) or DASH (`.mpd`)
segments**, often with **separate video and audio tracks**. The app does what the owner described
("collect the packets and assemble them"), but uses proven engines instead of rewriting them:

```
            ┌───────────── URL router ─────────────┐
 paste ───► │ which engine handles this link?      │
            └──┬─────────────┬──────────────┬──────┘
               ▼             ▼              ▼
          yt-dlp        gallery-dl       spotDL adapter
     (video, audio,   (images, photo    (Spotify metadata →
      playlists,       posts, albums,    YouTube Music match →
      HLS/DASH,        carousels,        yt-dlp download →
      ~1,800 sites +   slideshows)       mutagen tags)
      generic pages)
               │             │              │
               └──────► ffmpeg / ffprobe ◄──┘   merge segments, mux video+audio,
                               │                convert to MP3, crop cover, embed tags
                               ▼
                    Downloads\… final file (atomic move from temp)
```

Plus a small **direct HTTP engine** for plain file links (`.mp4`, `.jpg`, `.zip`…) with resume.
Later, a **Sniffer** (M7) opens the page in a built-in browser and lists every media request it
makes, for sites no extractor knows.

---

## 3. Platform support matrix

**Everything in the "Works without login?" column works straight after install.** The "Needs cookies
for" column lists *optional, advanced* cases: media the source site itself hides from logged-out
visitors. "Cookies" means the owner's own browser session on that site (§6.4). It is off by default,
never part of setup, and grants no access the owner doesn't already have.

| Platform | Engine | Works without login? | Needs cookies for | Known breakage / limits |
|---|---|---|---|---|
| **YouTube** (videos, Shorts, playlists, channels) | yt-dlp + yt-dlp-ejs + Deno | Yes, public content | Private, age-restricted, members-only, some "confirm you're not a bot" checks | JS challenges (needs EJS + Deno), rolling PO-token enforcement → HTTP 403, extractor changes. PO-token provider = advanced fallback only. Heavy logged-in bulk downloading can get an account throttled. |
| **YouTube Music** (songs, albums, playlists) | yt-dlp (same as above) | Yes | Premium-only higher-bitrate audio | Same as YouTube. Source audio is typically ~128–160 kbps Opus/AAC; converting to MP3 cannot add quality. |
| **Spotify** (track, album, playlist, artist) | spotDL 4.5.2 adapter → yt-dlp | Yes (metadata is public) | — | **Spotify audio is DRM-protected and is never captured.** Audio is *matched* from YouTube/YT Music, so a wrong recording can be picked; UI shows "matched from YouTube" + confidence + manual fix. spotDL's shared Spotify API credentials can be rate-limited (§11). |
| **Instagram** — reels/single videos | yt-dlp | Sometimes | Consistent access | Login walls, GraphQL changes, rate limits, short-lived CDN URLs |
| **Instagram** — photos, carousels, stories, highlights, profiles | gallery-dl | Rarely | Practically always; required for stories/private/saved/profiles | As above. Bulk uses 6–12 s delays between requests. |
| **Facebook** — videos, reels | yt-dlp | Public videos sometimes | Private/friends/age/region-limited | Checkpoint/login pages, expiring CDN signatures, frequent player URL changes |
| **Facebook** — photos, albums | gallery-dl | No | Yes | Same as above |
| **TikTok** — videos | yt-dlp | Public singles usually | Private, region-restricted | Anti-bot/signature changes, region blocks, rate limits; no-watermark availability varies |
| **TikTok** — photo slideshows, user feeds | gallery-dl | Sometimes | Profiles, bulk, saved | Slideshow schema changes |
| **X / Twitter** — single tweet video | yt-dlp | Sometimes | Protected/sensitive media | Login and API changes, rate limits |
| **X / Twitter** — images, media timelines | gallery-dl | No | Yes (required) | Same |
| **Other video sites** (Vimeo, Dailymotion, Twitch VODs/clips, Reddit, SoundCloud, Bilibili, news sites…) | yt-dlp site extractors | Mostly | Site-dependent | Per-site extractor breakage; fixed by engine updates |
| **Generic pages** with `.mp4` / `.webm` / `.m3u8` / `.mpd` | yt-dlp generic extractor | If the page is public | Site-dependent | May need Referer/User-Agent/cookies; signed URLs expire; JS-only players may hide the URL (→ Sniffer). Plain AES-128 HLS is fine; DRM is not. |
| **Direct file / image links** | built-in HTTP engine | Yes | Rare | Resume only if the server supports Range |
| **Arbitrary pages → "all images on this page"** | Sniffer (M7) | — | — | gallery-dl's generic mode is off by default and is **not** advertised before M7 |
| **DRM streams** (Netflix, Disney+, Prime, Spotify audio, most paid courses) | — | — | — | **Unsupported by design.** Detected where possible and reported clearly. |

---

## 4. Tech stack (pinned per research on 2026-09-14; re-verify at M0)

| Area | Choice | Notes |
|---|---|---|
| Language | **Python 3.11.9** (already installed) | Same interpreter for dev and the frozen build |
| GUI | **PyQt6 6.11.0** | GPL. Fine for personal/open-source use. If the app might ever be distributed closed-source, switch to PySide6 (LGPL). See open question Q1. |
| Video/audio engine | **yt-dlp[default,curl-cffi] 2026.8.19** + **yt-dlp-ejs** | Used as a Python library inside worker processes. Updatable at runtime (§8). |
| JS runtime for YouTube | **Deno** | Required by yt-dlp EJS for full YouTube support |
| Media tools | **FFmpeg + ffprobe** (static Windows build) | Mandatory for merge/convert/crop/tag. Do **not** install the unrelated PyPI package `ffmpeg`. |
| Images/galleries | **gallery-dl 1.32.11** | |
| Spotify | **spotDL 4.5.2**, isolated (§6.3) | Its internals are async-first and not a stable GUI API, so it runs behind a subprocess boundary |
| Tagging | **mutagen 1.48.1** | Controlled ID3 fields, cover art, verification |
| Process control | **psutil** | Kill a job's whole process tree (ffmpeg children) on cancel |
| Storage | `sqlite3` (stdlib) + JSON settings | |
| Tests | pytest, pytest-qt | |
| Lint/format | ruff | |
| Env/lock | venv + **uv** (or pip-tools) lockfile | |
| Packaging (GUI) | **PyInstaller `--onedir`** + **Inno Setup** installer | onedir: faster start, fewer antivirus false positives, and works better with Qt. No UPX. The GUI binary does **not** import yt-dlp, gallery-dl or spotDL (§5.2, §8). |
| Engine runtime | **App-managed CPython 3.11 runtime** running the engine workers (preferred), proven by the M0 spike | Keeps engines updatable (curl_cffi native wheels, spotDL's dependency tree) and outside the GUI binary (licensing, §8). Fallback: official standalone engine executables. |
| Sniffer (M7 only) | **PyQt6-WebEngine** (matching PyQt6 version) | Adds roughly 150–200 MB (Chromium). Only installed/bundled once M7 starts. |

---

## 5. Architecture

### 5.1 Layers

```
┌──────────────────────────── gui (PyQt6) ────────────────────────────┐
│ MainWindow · UrlBar · PreviewCard · FormatPicker · PlaylistTable     │
│ QueueView (JobRowDelegate) · HistoryPage · SettingsPage · ToolsPage  │
│ bridge.py: core events ──queued Qt signals──► widgets (main thread)  │
└───────────────────────────────▲──────────────────────────────────────┘
                                │ plain Python callbacks / event objects
┌───────────────────────────── core (NO Qt imports) ───────────────────┐
│ router · analyzer · presets · queue/scheduler · runner · history     │
│ settings · tools (ffmpeg/ffprobe/deno discovery) · paths · updater   │
│ (never imports yt-dlp / gallery-dl / spotDL)                         │
└───────────────────────────────┬──────────────────────────────────────┘
                                │ spawn per job: JSON job spec on stdin
                                ▼
┌──── worker process (one per active job, in the ENGINE RUNTIME) ───────┐
│ separate program from the GUI exe; imports exactly one engine;        │
│ emits JSON-lines events on stdout:                                    │
│ {"type":"stage"|"progress"|"log"|"result"|"error", ...}               │
└───────────────────────────────────────────────────────────────────────┘
```

**Rule:** `core/` never imports Qt. That keeps it unit-testable, gives it a CLI later for free,
and makes the GUI swappable.

### 5.2 Decision: one worker subprocess per job (not QThreads)

| | QThread/QThreadPool running yt-dlp in-process | **Worker subprocess per job** ✅ |
|---|---|---|
| Cancel during ffmpeg post-processing | Unreliable (a hook exception can't stop ffmpeg) | Kill the process tree, always works |
| A crashing extractor or native lib | Takes down the GUI | Only that job fails |
| GIL contention with UI | Possible stutter | None |
| Hot-swap an updated yt-dlp | Needs an app restart | The next job simply uses the new engine |
| spotDL's async internals | Awkward | Natural fit |
| Cost | Lower | ~0.3–0.5 s spawn per job. Fine for downloads. |

- In development the worker runs as `python -m stuff_downloader_worker` from the venv. In the
  installed app it runs from the **engine runtime** (§8), e.g.
  `%LOCALAPPDATA%\StuffDownloader\runtime\python.exe -m stuff_downloader_worker`. It is a separate
  program from `StuffDownloader.exe`, never the frozen GUI binary re-entered with a flag.
- The worker package (`src/stuff_downloader_worker/`) imports **no Qt** and nothing from `gui/`.
  The GUI side (`core/runner.py`) talks to it only through the JSON-lines protocol, never by
  importing an engine.
- **Analyze** (`extract_info(download=False)`) also runs in a short-lived worker, so a hanging
  extractor can't freeze the UI. It has a timeout and a Cancel button.
- **Pause** = stop the worker and keep the `.part` file. **Resume** = start a new worker with
  continue-download on. yt-dlp resumes partial files; HLS resumes at segment level.
- **Stages** are modelled explicitly: `queued → analyzing → downloading video → downloading audio →
  merging → converting → tagging → completed | failed | cancelled | paused`.

### 5.3 URL router

1. Normalize: trim, strip tracking params, and expand `youtu.be`/`music.youtube.com`
   (`watch?v=…&list=…` asks the owner "just this song or the whole playlist?", mirroring the
   `--no-playlist` / `--yes-playlist` distinction in the guides).
2. `open.spotify.com/*` → spotDL engine.
3. gallery-dl-first hosts/paths (Instagram `/p/` with images, stories, highlights, profiles;
   Facebook photos/albums; TikTok `/photo/`; X media timelines) → gallery-dl.
4. Everything else → yt-dlp (site extractors, then generic).
5. If yt-dlp says "unsupported URL" and the URL looks like a file (Content-Type from a HEAD
   request) → HTTP engine.
6. Otherwise show a clear "Not supported — try Sniffer (M7)" message.
7. Only `http`/`https` are accepted. `file://` and other schemes are rejected.

### 5.4 Presets (the ChatGPT recipes, as data)

Presets are plain dataclasses that core converts into yt-dlp options. Users never type raw
yt-dlp/ffmpeg arguments, so nothing from user input reaches a post-processor or an exec call.

**MP3 — Music (square cover + tags)** reproduces both `.txt` guides:

| Guide flag | yt-dlp library option |
|---|---|
| `-x --audio-format mp3 --audio-quality 0` | `format: "bestaudio/best"`, postprocessor `FFmpegExtractAudio(preferredcodec="mp3", preferredquality="0")` |
| `--embed-metadata` | postprocessor `FFmpegMetadata(add_metadata=True)` |
| `--embed-thumbnail` + `--convert-thumbnails jpg` | `writethumbnail: True`, postprocessors `FFmpegThumbnailsConvertor(format="jpg")` + `EmbedThumbnail` |
| `--ppa "ThumbnailsConvertor+ffmpeg_o:-qmin 1 -q:v 1 -vf crop=…"` | `postprocessor_args: {"thumbnailsconvertor+ffmpeg_o": ["-qmin","1","-q:v","1","-vf","crop='if(gt(ih,iw),iw,ih)':'if(gt(iw,ih),ih,iw)'"]}` |
| `--windows-filenames` | `windowsfilenames: True` |
| `-o ".../%(artist,uploader)s - %(track,title)s.%(ext)s"` | `outtmpl: "%(artist,uploader)s - %(track,title)s.%(ext)s"` under the chosen folder |
| `--no-playlist` / `--yes-playlist` | `noplaylist: True/False`, decided by the router prompt |

Improvements over the guides (from research):
- **Prefer a native square thumbnail** when one exists (common on "Topic" art tracks). Centre-crop
  is the fallback. The cover is previewed before download, with a "Don't crop cover" toggle,
  because centre-crop can cut meaningful content on normal video thumbnails.
- **mutagen pass after yt-dlp**: sets and verifies title/artist/album/track number/year/cover. For
  playlists it also writes the album/playlist name and track number from the playlist index.
- **Honest quality labels**: "MP3 (VBR ~V0, best transcode)" rather than "320 kbps". Source audio
  on YouTube is usually ~128–160 kbps. Also offer **Original audio (Opus/M4A, no re-encode)** and
  **M4A (AAC)**.
- **Download archive** (`download_archive`) per library, so re-running a playlist fetches only new
  songs. The owner can override it per job ("re-download anyway").

Other built-in presets: **Video — Best**, **Video — up to 1080p (compatible MP4)**,
**Video — up to 720p (small)**, **Audio — Original**, **Thumbnail only** (the guide's cover-test
recipe), **Subtitles** (when available), **Images — Original**.

### 5.5 Resolution / format selector

1. Analyze → sanitized `info_dict` → `FormatOption` rows.
2. Group by height into friendly choices that **only list what exists**: 4320p/2160p/1440p/1080p/
   720p/480p/360p/240p/144p. Show fps (60) and HDR badges, codec, container, and size
   (`filesize` or `filesize_approx`, marked "~"; "unknown" for some manifests).
3. **Auto/Best** is always offered. A default ("Best up to 1080p") lives in Settings.
4. **Compatibility toggle:** "Most compatible (H.264/AAC MP4, plays everywhere)" vs "Best quality
   (VP9/AV1, may be MKV/WebM)".
5. The selected height becomes a format selector such as `bv*[height<=H]+ba/b[height<=H]`.
   yt-dlp merges video-only + best audio with ffmpeg (`merge_output_format` mp4 or mkv).
6. **Formats are re-resolved at download time** because URLs expire. If the exact format has
   vanished, fall back to best ≤ the selected height, and say so in the job log.
7. An **"All formats" advanced table** (9xbuddy parity) lists every video, audio-only, thumbnail
   and subtitle track.

### 5.6 Paths & files

- Default folder: `QStandardPaths.DownloadLocation` (the Windows **Downloads** folder), resolved in
  the GUI and passed to core as a plain path.
- Per-job "Save to…" override. Optional per-type subfolders (Q4).
- Partial files go to `%LOCALAPPDATA%\StuffDownloader\temp\<job-id>\`, then an **atomic move** to
  the final location on success.
- Windows-safe names (reserved names, trailing dots, illegal characters). A long-path guard trims
  titles so full paths stay under 260 characters unless long paths are enabled.
- **Collision policy:** skip if identical (archive), else append ` (2)`. Playlists can add the index
  and/or media id to the template.
- Output folders are validated to be real, writable directories.

---

## 6. Features in detail

### 6.1 Queue
- Concurrency setting 1–5 (default 3). Per-host limit of 1 for Instagram/TikTok/X/Facebook bulk,
  with delays.
- Pause / resume / cancel / retry (with backoff), per item and "all".
- Drag to reorder queued items.
- Global speed limit (optional).
- **Persistence:** the queue is stored in SQLite. On restart, unfinished jobs come back as *paused*.

### 6.2 Playlists (YouTube, YT Music, Spotify, channels)
- Expand into a table: checkbox · # · thumbnail · title · artist · duration · "already downloaded" badge.
- Select all / none / range. Filter box. One preset for the batch, with a per-item override.
- The whole batch becomes one **group** in the queue (collapsible), with an aggregate progress bar.
- Unavailable/private/deleted entries are listed with the reason, not silently dropped.

### 6.3 Spotify (spotisaver parity)
- spotDL runs **in its own isolated engine environment**, so its yt-dlp pin never conflicts with ours.
  Communication is via the same worker JSON-lines protocol (a thin adapter translates its output).
- Flow: Spotify URL → track list (title, artist, album, duration, ISRC, cover) → match on YouTube
  Music → **match review table** (Spotify track vs chosen YouTube result, duration diff, confidence)
  → owner can swap a match by pasting a different link → download → tag with **Spotify's**
  metadata and cover via mutagen.
- spotDL's sync/delete behaviour is **never** used.

### 6.4 Advanced (optional): restricted media via source-site cookies
- **Not part of setup and not needed for public content.** The app works fully with this left
  untouched. It is only offered, in context, when a download fails because the *source site*
  requires its viewers to be logged in (private/friends-only posts, stories, age-restricted video),
  and even then that media can't be promised to work.
- Per site: **No login** (default) · **Use browser session** (pick browser + profile) ·
  **cookies.txt file** (Netscape format, user-selected).
- Store only the *choice* (browser name/profile or file path), never copied cookie contents.
  Cookies, auth headers and tokens are **never logged**.
- Windows gotchas, explained in the Tools page:
  - Chromium browsers (Chrome/Edge/Brave) use app-bound cookie encryption and lock the cookie DB
    while running, so reading their cookies is unreliable.
  - **Firefox** works best for "use browser session".
  - Otherwise export a `cookies.txt` for just that site.
- Warn that a cookies file is a secret: anyone with it can act as the owner on that site.

### 6.5 History
- SQLite: every finished/failed job with source URL, title, thumbnail, preset, output path(s),
  size, duration, finished time and error.
- Search, filter by type/site/status, "Open file", "Show in folder", "Download again",
  "Remove from history" (removes the entry only; deleting the file is a separate, confirmed action).

### 6.6 Tools & health page
- Checks FFmpeg, ffprobe and Deno and shows the versions found, plus yt-dlp, gallery-dl and spotDL
  engine versions.
- Tool discovery order: **configured path → app tools dir → PATH**.
- "Update engines" button (§8). Log viewer. "Copy diagnostics" (redacted).

### 6.7 Sniffer (M7, experimental / best-effort)
- A built-in browser tab (**PyQt6-WebEngine**, roughly +150–200 MB) with a
  `QWebEngineUrlRequestInterceptor`.
- **What it can and can't see:**
  - The interceptor sees *request URLs, methods and resource types*. It does **not** get response
    bodies, and it doesn't reliably get response headers, sizes or resolutions.
  - Media assembled in page JavaScript (MSE `blob:` sources) may never expose a downloadable URL.
  - So the Sniffer lists only **observable manifest or direct-media URLs** (`.m3u8`, `.mpd`, `.mp4`,
    `.webm`, `.mp3`, `.m4a`, image URLs), classified by URL/extension and resource type.
- Size and resolution are filled in **afterwards, if possible**, by probing the chosen URL from the
  worker (HTTP HEAD / Range, or ffprobe on a manifest) with the page's Referer. Otherwise they show
  as unknown.
- Selected rows are handed to the generic/HTTP engine with the page's Referer (and cookies only if
  the owner enabled §6.4 for that site).
- Clear expectations in the UI: "Experimental — works on many simple players, not all sites."
  When EME/DRM license requests are observed the page is marked "DRM protected — unsupported",
  and no attempt is made on it.

---

## 7. UI / UX spec

Look: modern Windows-11 feel with a custom QSS theme (dark by default, light option, follows the
Windows setting). Rounded cards, a single accent colour, icon set bundled locally. See Q8 for an
optional Fluent widget library.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ ⬇ Stuff Downloader                                              ─  ▢  ✕    │
├────────┬────────────────────────────────────────────────────────────────────┤
│ ⬇ Down-│ ┌────────────────────────────────────────────────┐ [Paste] [Analyze]│
│  loads │ │ 🔗 Paste a link from YouTube, Spotify, TikTok…  │                  │
│ 🕘 Hist│ └────────────────────────────────────────────────┘                  │
│  ory   │ ┌─ Preview ──────────────────────────────────────────────────────┐ │
│ 🧭 Sni-│ │ [thumbnail]  Coldplay – Yellow            ▶ YouTube Music      │ │
│  ffer  │ │              Coldplay · 4:29 · 2000                            │ │
│ 🛠 Tools│ │ ( Video )( Audio )( Images )( Subtitles )( All formats )       │ │
│ ⚙ Sett-│ │ Format: [MP3 — Music (square cover + tags) ▾]  Quality:[V0 ▾]  │ │
│  ings  │ │ Resolution: [1080p60 · ~142 MB ▾]   ☑ Most compatible (MP4)    │ │
│        │ │ Save to: [C:\Users\Aloka\Downloads        ] [Change…]           │ │
│        │ │ [cover preview ▣]  ☐ Don't crop cover        [ ⬇ Download ]    │ │
│        │ └────────────────────────────────────────────────────────────────┘ │
│        │ Queue  ▸ 3 active · 5 queued · 12 done        [Pause all][Clear done]│
│        │ ┌────────────────────────────────────────────────────────────────┐ │
│        │ │[▣] Night Changes.mp3            Downloading audio              │ │
│        │ │    ████████████████░░░░░░  67%  4.1/6.2 MB · 2.3 MB/s · 0:02  ⏸✕│ │
│        │ │[▣] Trip Vlog 4K.mp4             Merging video + audio          │ │
│        │ │    ███████████████████████▒ 98%  ▮video ▮audio ▯merge         ⏸✕│ │
│        │ │[▾] Playlist: Chill Mix (24)     11/24 done · 1 failed          │ │
│        │ │    ██████████░░░░░░░░░░░░░ 46%                                 │ │
│        │ │[▣] Reel_8812.mp4                ✔ Completed · 18 MB   📂 ▶      │ │
│        │ │[▣] Private video                ✖ Private on the site  ⓘ ↻     │ │
│        │ └────────────────────────────────────────────────────────────────┘ │
├────────┴────────────────────────────────────────────────────────────────────┤
│ ↓ 6.8 MB/s total · FFmpeg ✔ Deno ✔ · Engines up to date                     │
└─────────────────────────────────────────────────────────────────────────────┘
```

Download-progress style:
- One **row card** per job: thumbnail, title, stage chip, progress bar, `%`, `downloaded / total`,
  speed, ETA, action buttons. Rendered by a custom `QStyledItemDelegate` over a
  `QAbstractListModel` so hundreds of rows stay smooth.
- **Stage-segmented bar** for multi-step jobs (video → audio → merge/convert → tag). Indeterminate
  shimmer when the total size is unknown (live HLS or manifests).
- Colour per state: active = accent, paused = amber, done = green with a ✔, failed = red with a
  **plain-language reason** ("Private on the site", "Removed by uploader", "Region blocked",
  "DRM protected — unsupported", "Engine out of date — update engines").
- Progress events are throttled to ~10 UI updates/s per job.
- Completed rows: **Open**, **Show in folder**, **Download again**.
- A **group row** per playlist with an aggregate bar; expanding it shows the child rows.
- Footer: total speed, active/queued/done counts, tool health.
- Windows taskbar progress on the app icon (optional), a tray icon, and a toast when a job or batch
  finishes.

Interaction:
- **Ctrl+V anywhere** pastes and analyzes. Links can also be dragged in.
- Optional **clipboard watcher** ("Link detected — analyze?"), off by default.
- A playlist-style link opens the playlist table. A `watch?v=…&list=…` link asks
  "This song / Whole playlist".
- Keyboard: Enter = Download with the current preset, Del = cancel the selected job, Space = pause/resume.
- Settings are one scrolling page with sections.

---

## 7a. Settings

Stored as JSON at `%APPDATA%\StuffDownloader\settings.json` with a `schema_version` for migrations.

| Setting | Default |
|---|---|
| Download folder | Windows **Downloads** |
| Subfolders by type (`Videos\`, `Music\`, `Images\`) | Off (Q4) |
| Filename templates (video / music / playlist / images) | `%(title)s.%(ext)s` · `%(artist,uploader)s - %(track,title)s.%(ext)s` · `%(playlist)s\%(playlist_index)02d - %(artist,uploader)s - %(track,title)s.%(ext)s` · gallery-dl default per site |
| Default video quality | Best up to 1080p, most compatible MP4 |
| Default audio preset | MP3 — Music (square cover + tags) |
| Use download archive | On |
| Max concurrent downloads | 3 |
| Speed limit | Off |
| Cookies per site | No login |
| Proxy | None |
| Tool paths (ffmpeg/ffprobe/deno) | Auto-detect |
| Engine update channel | Stable (Nightly optional) |
| Theme | Follow Windows |
| Clipboard watcher / notifications / tray | Off / On / On |

Data locations: `%LOCALAPPDATA%\StuffDownloader\` → `library.db` (queue + history), `archive.txt`,
`temp\`, `runtime\` (engine runtime + versioned engine envs, §8.1), `tools\`, `logs\` (rotating, redacted).

---

## 8. Packaging, updates & release

- **Build:** `PyInstaller --onedir` from a `.spec` → `dist\StuffDownloader\StuffDownloader.exe`.
  No UPX. App icon and version resources included.
- **Installer:** Inno Setup → `StuffDownloader-Setup-x.y.z.exe`: per-user install (no admin),
  Start-menu and optional desktop shortcut, uninstaller that leaves the owner's downloads alone.
- **Tools:** FFmpeg/ffprobe and Deno are fetched by `packaging/fetch_tools.py` from official release
  URLs **pinned with SHA-256** at build time and bundled into `tools\` (Q2: bundle vs first-run download).
### 8.1 Engines live outside the GUI binary

Unpacking wheels into a folder and prepending `sys.path` inside a PyInstaller-frozen process is
**not** a safe update mechanism:
- `curl_cffi` ships native binaries tied to the Python ABI.
- spotDL has a large dependency tree.
- Frozen importers can shadow or refuse externally added packages.

So engines run in their own runtime, chosen by the **M0 packaging spike**:

| Option | How | Updates | Status |
|---|---|---|---|
| **A. App-managed CPython runtime** (preferred) | Installer ships the official CPython 3.11 embeddable/runtime for Windows + pip. Each engine gets its own environment: `runtime\envs\ytdlp\`, `envs\gallerydl\`, `envs\spotdl\` (spotDL isolated so its yt-dlp pin can't clash). Worker = `runtime\python.exe -m stuff_downloader_worker --engine ytdlp`. | "Update engines" runs pip against **hash-pinned requirements** (`--require-hashes`) into a *new* versioned env dir, runs the self-test, then flips `active.json`. The previous env is kept for one-click rollback; a failed self-test rolls back automatically. Native wheels (curl_cffi) install correctly because it's a real CPython of the matching version. | Must be proven in the M0 spike |
| **B. Official standalone engine executables** (fallback) | Use `yt-dlp.exe` and `gallery-dl.exe` release binaries (and a spotDL equivalent or Option A for spotDL only). The worker drives them via CLI with machine-readable progress (`--progress-template` JSON, `--print-json`/`--dump-json`). | Download a new versioned release binary, verify the published SHA-256, self-test, flip pointer, keep the previous one for rollback. | Used if A fails the spike |
| C. Engines frozen into the app | Bundled at build time | **Only by rebuilding and reinstalling the app.** Hot updates are limited to nothing. | Last resort; documented so we never pretend otherwise |

**M0 spike acceptance** (on a clean Windows user profile, built installer, no dev venv on PATH):
1. The GUI exe launches a worker from the engine runtime.
2. The worker imports yt-dlp with `curl_cffi` and completes one analyze + one short download.
3. An engine version is upgraded and then rolled back without reinstalling the app.
4. spotDL imports from its isolated env.

The result is recorded as a decision note before M1 starts.

### 8.2 Self-test
`StuffDownloader.exe --self-test` (GUI side) plus `stuff_downloader_worker --self-test` (engine
side) check tools, engine imports, a write/move in temp, and the worker round-trip. They run after
every build and after every engine update.

### 8.3 Licensing boundary
- **Personal use is unaffected.** The following matters only if the app is ever distributed.
- PyQt6 is **GPLv3**; gallery-dl is **GPLv2** (GPLv2-only terms may be incompatible with
  combining into one GPLv3 program).
- The design therefore keeps gallery-dl (and all engines) as **separately invoked programs in the
  engine runtime** that communicate only over the JSON-lines/CLI boundary. They are never imported
  into or frozen inside the PyQt6 GUI binary.
- **Before any distribution, a licence audit is a required M6 gate.** Its output is
  `THIRD_PARTY_LICENSES.txt` covering PyQt6 GPLv3, the FFmpeg build (GPL or LGPL as chosen),
  Deno MIT, yt-dlp Unlicense, gallery-dl GPLv2, spotDL MIT, mutagen GPLv2+, curl_cffi and CPython
  PSF. The audit re-checks the separate-process boundary, or picks compatible components (e.g.
  PySide6/LGPL).

---

## 9. Project layout

```
Stuff Downloader/
├─ plan.md
├─ README.md
├─ pyproject.toml            # deps, ruff, pytest config
├─ uv.lock                   # (or requirements.lock)
├─ src/stuff_downloader/
│  ├─ __main__.py            # python -m stuff_downloader  (--self-test flag)
│  ├─ app.py                 # QApplication bootstrap
│  ├─ core/                  # ── NO Qt imports ──
│  │  ├─ models.py           # Job, MediaInfo, FormatOption, Preset, Event, Stage
│  │  ├─ router.py
│  │  ├─ analyzer.py
│  │  ├─ presets.py          # preset → engine options (MP3 square-cover lives here)
│  │  ├─ formats.py          # resolution grouping, format selectors
│  │  ├─ scheduler.py        # queue, concurrency, per-host limits, retry
│  │  ├─ runner.py           # spawns workers, parses JSON-lines, kills process trees
│  │  ├─ history.py          # SQLite (queue + history)
│  │  ├─ settings.py         # JSON + migrations
│  │  ├─ tools.py            # ffmpeg/ffprobe/deno discovery + health
│  │  ├─ paths.py            # sanitize, long paths, collisions, atomic move
│  │  ├─ cookies.py          # optional cookie source selection (no contents stored)
│  │  ├─ protocol.py         # JSON-lines event/job schema (copied verbatim into worker; test keeps them identical)
│  │  └─ updater.py          # engine runtime envs/binaries, hashes, active pointer, rollback
│  └─ gui/
│     ├─ main_window.py
│     ├─ bridge.py           # core events → Qt signals (queued connections)
│     ├─ models/queue_model.py
│     ├─ widgets/            # url_bar, preview_card, format_picker, playlist_table,
│     │                      # job_row_delegate, match_review_table, cover_preview
│     ├─ pages/              # downloads, history, sniffer, tools, settings
│     └─ theme/              # dark.qss, light.qss, icons/
├─ src/stuff_downloader_worker/   # runs in the ENGINE RUNTIME; no Qt, no gui imports
│  ├─ __main__.py            # --engine X; reads job spec, runs engine, writes events; --self-test
│  ├─ protocol.py            # same schema as core/protocol.py
│  ├─ tagging.py             # mutagen
│  └─ engines/
│     ├─ base.py             # Engine protocol: analyze(), download(job, emit)
│     ├─ ytdlp_engine.py
│     ├─ gallerydl_engine.py
│     ├─ spotdl_engine.py
│     └─ http_engine.py
├─ tests/
│  ├─ unit/                  # router, presets, formats, paths, settings, history, protocol, updater
│  ├─ gui/                   # pytest-qt: queue model/delegate, format picker, playlist table
│  ├─ integration/           # @pytest.mark.network — real URLs, run manually
│  └─ fixtures/              # recorded, sanitized info_dict JSON (YT video, YTM playlist, TikTok, IG…)
├─ packaging/
│  ├─ StuffDownloader.spec   # GUI binary only
│  ├─ installer.iss
│  ├─ build_runtime.py       # engine runtime (Option A) or engine binaries (Option B), hash-pinned
│  ├─ engine-requirements/   # ytdlp.txt, gallerydl.txt, spotdl.txt with --require-hashes
│  ├─ fetch_tools.py         # ffmpeg/ffprobe/deno: pinned URLs + SHA-256
│  └─ THIRD_PARTY_LICENSES.txt
└─ tools/                    # (git-ignored) local ffmpeg/ffprobe/deno for dev
```

---

## 10. Milestones

Each milestone ends with a **working app the owner can run** (from source *and* as a `.exe`
build), independent review, and tests passing. Later milestones add to it; none depend on a
later one.

### M0 — Foundations
- Repo scaffold per §9, `pyproject.toml`, lockfile, ruff, pytest. Git init (Q5).
- Install FFmpeg + Deno for dev (`winget install Gyan.FFmpeg`, `winget install DenoLand.Deno`),
  plus pinned Python deps in a venv.
- `core/settings.py` (default folder = Downloads), `core/tools.py` health check, logging with redaction.
- Worker JSON-lines protocol + runner with a **fake engine** (proves spawn, progress, cancel/kill).
- GUI shell: sidebar, empty Downloads page, Tools page showing health, Settings page with folder picker.
- PyInstaller onedir smoke build of the GUI + `--self-test`.
- **Packaging spike (§8.1), a gate for M1:** prove Option A (app-managed CPython engine runtime)
  against the four acceptance points, or fall back to Option B. Record the outcome as a decision note.
- **Ships:** an app window that launches as `.exe` (no login, no setup beyond an optional folder
  choice), shows tool health, saves the chosen folder, and runs a fake job through the chosen
  engine runtime.

### M1 — YouTube single video & song (y2mate parity)
- Router (YouTube/YT Music), analyzer in worker, PreviewCard.
- **Resolution selector** (§5.5) with sizes, the compatibility toggle and Auto/Best.
- Presets: Video Best / ≤1080p / ≤720p, **MP3 — Music (square cover + tags)** exactly per §5.4,
  Original audio, Thumbnail only.
- Cover preview + "don't crop"; mutagen tagging and verification.
- Queue view with row delegate: progress, speed, ETA, stages, cancel, retry, open/show in folder.
- `watch?v=…&list=…` → "this song / whole playlist" prompt (playlist action disabled until M2).
- **Ships:** paste a YouTube link → pick 1080p MP4 or MP3 → get the file in Downloads.

### M2 — Playlists, queue power & history
- Playlist expansion table with checkboxes (YouTube, YT Music, channels).
- **YouTube / YT Music playlist → MP3 batch** with playlist template, track numbers and album tag.
- Download archive (skip already-downloaded).
- Concurrency setting, pause/resume (`.part` continue), reorder, pause all, retry with backoff.
- SQLite queue persistence (restore as paused) + History page (search, filter, open, download again).
- Toasts + tray icon.
- **Ships:** the owner's ChatGPT playlist workflow, fully in the app, re-runnable.

### M3 — Any-site video (9xbuddy parity) + optional restricted-media support
- yt-dlp for all other site extractors + generic pages (m3u8/mpd/mp4). HTTP engine for direct files.
- "All formats" advanced table (video, audio-only, thumbnails, subtitles).
- Facebook, TikTok, Instagram reels, X single-tweet videos via yt-dlp.
- Plain-language error mapping ("Private on the site", "Region blocked", "DRM protected").
- *Advanced, off by default:* per-site cookies (§6.4), offered only from a "private on the site"
  error, with the Firefox/cookies.txt guidance.
- **Ships:** paste a public video link from most sites → choose a format → download, with no login.

### M4 — Images & social galleries
- gallery-dl engine: Instagram posts/carousels/stories/highlights, TikTok photo slideshows,
  Facebook photos/albums, X media, plus the other gallery-dl sites.
- Image grid preview with select/deselect; original-quality files; per-host delays and limits.
- **Ships:** paste an Instagram carousel or TikTok slideshow → pick images → download originals.

### M5 — Spotify (spotisaver parity)
- Isolated spotDL engine env + adapter.
- Track/album/playlist expansion, **match review table** with manual override, Spotify metadata
  and cover tagging, archive.
- **Ships:** paste a Spotify playlist → review matches → tagged MP3s.

### M6 — Packaging & release polish
- Inno Setup installer with the GUI, the engine runtime (§8.1) and bundled pinned tools.
- **Update engines** with hash verification, self-test and rollback.
- First-run welcome: choose folder (optional), tool check. **No account, no sign-in.**
- App icon, version/About.
- **Licence audit gate (§8.3)** before the build is shared with anyone, producing
  `THIRD_PARTY_LICENSES.txt`.
- Antivirus sanity check of the build (onedir, no UPX, signed if a certificate is ever obtained).
- **Ships:** `StuffDownloader-Setup-1.0.0.exe` that installs and runs on a clean Windows 11 machine.

### M7 — Sniffer (experimental, best-effort)
- Add the PyQt6-WebEngine dependency (+150–200 MB).
- Build the WebEngine tab + request interceptor → list of *observable* manifest/direct-media URLs
  → optional probe for size/resolution → hand-off to the generic/HTTP engine with Referer.
- DRM detection message. Labelled "Experimental" in the UI.
- **Ships:** grab media from many pages no extractor supports, when the page exposes a non-DRM
  manifest or direct media URL. `blob:`/MSE-only players and DRM pages are reported, not promised.

---

## 11. Testing strategy

| Level | What | How |
|---|---|---|
| Unit (core) | router rules, preset → yt-dlp options (**asserts the MP3 preset's square-crop postprocessor args, metadata/thumbnail postprocessors, outtmpl, noplaylist**), resolution grouping from fixture `info_dict`s, format-selector fallback, path sanitizing / long paths / collisions, settings migration, history DB, updater hash check + rollback, cookie redaction in logs | pytest, no network, fast (runs every change) |
| Protocol | worker ⇄ runner: events parsed, malformed lines tolerated, cancel kills the child process tree, pause/resume state; `core/protocol.py` and worker `protocol.py` schemas identical; import-boundary test: `core/` and `gui/` never import yt_dlp/gallery_dl/spotdl, worker never imports PyQt6 | pytest with the fake engine |
| GUI | queue model updates, delegate renders all states, format picker lists only existing heights, playlist selection | pytest-qt (offscreen) |
| Integration | a small fixed list of stable public URLs (short Creative-Commons YouTube video, one YTM playlist of 2–3 tracks, one public TikTok/IG/FB sample, a direct mp4, an HLS test stream). Verify output exists, duration via ffprobe, MP3 has square cover + tags via mutagen. | `pytest -m network`, run manually per milestone (sites change; not a blocker for unit CI) |
| Packaged | GUI and worker `--self-test`, one real download from the installed build on a clean user profile, and (from M0 spike / M6) an engine upgrade + rollback without reinstalling | Per milestone build |
| Manual UX | checklist per milestone: paste → preview → select → progress → open file; error states | Tester role |

Every milestone in DevTeam runs: **implementer(s) → reviewer → tester → owner acceptance.**
Core/engine work and GUI work get separate write scopes (`src/stuff_downloader/core/**` vs
`src/stuff_downloader/gui/**`) so two agents can build in parallel.

---

## 12. Risks & mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Site extractors break (YouTube, IG, TikTok, FB change often) | Downloads fail | Engine hot-update (stable/nightly) + rollback; clear "update engines" error hint |
| YouTube JS challenges / PO tokens / 403 / bot checks | YouTube failures | Bundle Deno + yt-dlp-ejs; cookies option; PO-token provider as advanced fallback only |
| Account throttling/bans from logged-in bulk downloads | Owner's account at risk | Cookies off by default; per-host concurrency 1 + delays for social sites; warning text |
| Chromium cookie encryption / DB lock on Windows | "Use browser session" fails | Recommend Firefox or cookies.txt; explain in the Tools page |
| Wrong Spotify matches | Wrong song in the library | Match review table, confidence, manual override, duration check |
| spotDL dependency conflicts / shared API credential rate limits | Spotify feature flaky | Isolated engine env; Q7 about using the owner's own Spotify API app credentials |
| Antivirus false positives on PyInstaller builds | App quarantined | onedir, no UPX, version resources; submit false-positive reports if needed |
| Bundle size (Qt + ffmpeg + ffprobe + Deno ≈ 250–350 MB) | Large installer | Accept for personal use, or first-run tool download with pinned SHA-256 (Q2) |
| Engine hot-update doesn't work in the packaged app (native wheels, frozen imports, spotDL deps) | Engines can only be fixed by rebuilding | Engines never frozen into the GUI; M0 spike proves Option A (managed CPython runtime) or falls back to Option B (standalone engine exes); Option C documented as rebuild-only (§8.1) |
| Licensing: PyQt6 GPLv3 + gallery-dl GPLv2 (+ FFmpeg build licence) | Only matters if distributed | Engines are separate processes in their own runtime, not linked into the GUI binary; mandatory licence audit gate in M6 before sharing; PySide6 if needed (Q1) |
| Sniffer can't see everything (no response bodies, `blob:`/MSE players) | Some pages yield nothing | M7 labelled experimental; lists only observable URLs; honest "not found / DRM" messages |
| Windows paths: long names, emoji/Unicode titles, reserved names | Save failures | `paths.py` sanitizer + tests; temp-then-atomic-move |
| Cookie/secret leakage via logs or diagnostics | Account compromise | Never store cookie contents; redact logs; "copy diagnostics" is redacted |
| Expectation gap on audio quality | Disappointment with "320 kbps" | Honest labels; "Original audio" option |

---

## 13. Open questions for the owner

1. **PyQt6 or PySide6?** They are nearly identical to code. PyQt6 is GPL (fine for personal use);
   PySide6 is LGPL (safer if you ever share or sell the app). *Recommendation: PyQt6 as you asked,
   unless you think you'll distribute it.*
2. **Bundle FFmpeg + Deno in the installer** (bigger, ~300 MB, works offline) **or download them on
   first run** (small installer, pinned and hash-checked)? *Recommendation: bundle.*
3. *(Optional, later, not needed to use the app.)* If you ever want **restricted media** (private
   or friends-only posts, stories) from sites you already use, which browser would you use? Firefox
   is easiest on Windows; Chrome/Edge usually need a `cookies.txt` export. Public downloads work
   with no answer here.
4. **Folder organisation:** save everything straight into `Downloads`, or
   `Downloads\Stuff Downloader\{Videos, Music, Images}`?
5. **Git:** can we `git init` this folder (and optionally a private GitHub repo) at M0? It's not a
   repository yet.
6. **Default music format:** MP3 (best compatibility, re-encoded) or keep original audio
   (Opus/M4A, no quality loss)? *Recommendation: MP3 default, as in your guides.*
7. **Spotify:** OK to rely on spotDL's built-in Spotify access, or will you create your own free
   Spotify developer app (client ID/secret) for more reliable playlist reading?
8. **Look:** plain custom dark theme, or a Fluent-style widget library (e.g. PyQt-Fluent-Widgets:
   nicer Windows-11 look, but another dependency with GPL/commercial terms)?
9. **Priority check:** is the order M1 YouTube → M2 playlists → M3 other video sites →
   M4 images → M5 Spotify right, or should Spotify come earlier?
10. **YouTube Premium:** do you have it? If so, cookies can unlock higher-bitrate YT Music audio.
