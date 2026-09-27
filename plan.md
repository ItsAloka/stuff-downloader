# Stuff Downloader — Plan v3

> **Written:** 2026-09-25 by Claude. The completed work (R0–R7) was removed from this file.
>
> **Earlier plans are still in git:** Rebuild Plan v2 (R0–R7, the P-list, research and target design) is
> `git show cbee88f:plan.md`. The original M0–M7 architecture plan is `git show 1887f92:plan.md`.
> The architecture still applies: a worker process per job, engines in their own runtime, `core/` has no
> Qt imports, and every URL is checked before use.

---

## 1. Still open from R7

- [ ] The §4 manual matrix on the installed 1.1.0 build (needs the owner's link list).
- [ ] An Instagram carousel that **mixes** photos and a video (no example found yet).
- [ ] A Reddit gallery: Reddit blocks this machine ("blocked by network security"), and yt-dlp hangs
      over 120 s on `/gallery/hrrh23` instead of failing fast. Add a timeout so it fails fast.
- [ ] The M4A "(2)" file-name issue.
- [ ] Owner review of the R7 screenshots.
- [ ] Push `rebuild` (R7 commits `b2027c1`, `cbee88f` are not pushed).

---

## 2. R8 — Library updates on app start — DONE (owner, 2026-09-25)

**Why:** downloads stop working when a site changes and yt-dlp falls behind (owner, 2026-09-25: "when I
was at home my downloading did not work"). The owner wants the app to find better versions of its
libraries and offer to upgrade them, like `apt list --upgradable` on Linux.

### 2.1 What the owner sees
When the app opens and updates exist, it shows:

```
Updates available
☑ yt-dlp        2026.08.01 → 2026.09.20   (recommended)
☑ ytmusicapi    1.8.1 → 1.8.3
[Update selected]   [Later]   [Skip this version]
```

- **Update selected:** installs the ticked updates with a progress bar, then says whether it worked.
- **Later:** asks again at the next check (the next day).
- **Skip this version:** never asks about that exact version again. A newer one is offered as usual.
- **Settings:** shows the installed version of each library, a **Check for updates** button, and an
  **On app start: check for updates** switch (on by default).
- **Welcome / Tools dialog:** a **Check for updates** button next to **Re-check** (owner, 2026-09-25).
  It runs the same check right away (ignoring the 24 h limit). Each engine line shows its state,
  e.g. `✔ yt-dlp engine — env 2026.8.19 · update 2026.09.20 available`, or `· up to date`. If updates
  exist, it opens the **Updates available** popup above.
- The Tools list must also show the **music engine** line (it is missing in the 1.1.0 dialog).

### 2.2 How it works
1. **When:** in the background after the window opens, at most once every 24 h. Startup never waits for
   it. No network → nothing is shown.
2. **Which libraries:** the top-level packages in each engine env's `packaging/engine-requirements/*.in`
   (yt-dlp, gallery-dl, ytmusicapi, spotDL, curl_cffi, mutagen, requests…). Their dependencies follow
   pip's resolver. ffmpeg, ffprobe and Deno are outside R8 (they rarely need updating and ship with the
   installer).
3. **Where the versions come from:** installed = the env's own metadata. Latest = PyPI JSON
   (`https://pypi.org/pypi/<name>/json`, HTTPS only, through the existing URL checks). Yanked and
   pre-release versions are ignored.
4. **"Suitable" version rules:**
   - **yt-dlp** (and gallery-dl): always the latest release, marked **(recommended)**. Keeping it
     current is what keeps downloads working.
   - **Everything else:** only updates inside the same major version (1.8.1 → 1.8.3 yes, 1.x → 2.0
     no). A new major version is listed greyed out: "needs an app update".
   - A version the owner skipped is not offered.
5. **Installing safely:** the runtime already keeps versioned envs and an `active.json` pointer
   (`core/runner.py`). An update:
   1. builds a **new env id** next to the current one (`envs/<engine>/<new id>`) with the new versions,
   2. runs the worker **self-test** in the new env,
   3. only if it passes, switches `active.json` to the new env,
   4. keeps the previous env as a rollback and deletes older ones.
   A failed install or self-test leaves the current env untouched and shows the error.
   Jobs that are already running finish on the old env.
6. **Rollback:** Settings → **Undo last update** switches `active.json` back to the previous env.
   It also switches back automatically if the first three jobs after an update all fail at the
   engine-start level.

### 2.3 Acceptance
- [x] Unit tests: version rules (latest vs. same-major, skip, yanked, pre-release), the 24 h throttle,
      the `active.json` switch and rollback, and PyPI errors or offline → no popup and no crash.
      Evidence: `tests/unit/test_updates.py`, `tests/unit/test_engine_update.py`,
      `tests/gui/test_updates_ui.py` (35 GUI tests), `tests/gui/test_m6_welcome_about.py` (music line).
      Full suite 1757 passed, 4 skipped; Ruff clean. Reviewed and approved by Codex (discovery,
      installer, UI).
- [ ] Live test on the installed build: pin an older yt-dlp in one env → the popup offers it as
      recommended → the update installs, the self-test passes, and a YouTube download works → **Undo
      last update** brings back the old version. **Carried forward:** `StuffDownloader-Setup-1.2.0.exe`
      is built (bundle checked: `packaging.version`, `core.updates`, `core.engine_update`,
      `engine-requirements/*.in`, `runtime-tools/build_runtime.py`) but not installed yet. PyPI's newest
      yt-dlp equals the bundled 2026.8.19, so the test first moves the ytdlp env to 2026.7.4.
- [x] Screenshots of the popup (also while installing) and the Settings section, 1280×720 and maximised,
      100% and 150% DPI, in `%TEMP%\stuff-downloader-screenshots8\` (outside git). Checked by Codex;
      owner marked R8 done.
- [x] THIRD_PARTY_LICENSES note: libraries can now be updated after install. It stays PERSONAL USE ONLY.

Owner decisions (2026-09-25): updates install only after **Update selected** (no silent installs).
FFmpeg, ffprobe and Deno are not updated by the app; they ship with a new installer, checked every few
months or when downloads break after a yt-dlp update (Deno is the likeliest cause for YouTube).

---

## 3. Working rules
1. **Claude implements. Codex only reviews and plans.** Every change is reviewed by an agent that did
   not write it. Self-review does not count as review.
2. **UI changes need the owner to see screenshots** before they are marked done.
3. Only one milestone at a time. Nothing outside this plan unless the owner adds it here first.
4. `[x]` only after the owner has seen it working. Put the evidence (test names, screenshots) next to it.
5. Before every commit, scan for secrets **and copyrighted media**. `.devteam-smoke/`, downloads and
   screenshots stay outside git. The repo is public.
6. Each milestone ends with: tests green, ruff clean, screenshots, owner acceptance, then one commit.

---

## 4. Test matrix (manual, on the installed build, per release)
The owner provides one public link per row. Links are kept in a local file outside the repo.

| Category | Must show | Must download |
|---|---|---|
| YouTube video / Short | Preview, Video + Audio + Image tabs, real heights | 1080p MP4, MP3 320, thumbnail PNG |
| YouTube video playlist | Header cover, a 16:9 picture on every row | Batch MP4 and batch MP3 |
| YouTube Music song / album / playlist | Square cover, Audio tab first, a square picture on every playlist row | MP3 with real album tag, M4A original |
| Spotify track / album / playlist (EN/JP/CN/SI) | Cover per row, matches | Tagged MP3, uncertain ones flagged |
| Apple Music / Deezer album | Cover per row, matches | Tagged MP3 |
| SoundCloud / Bandcamp track | Audio-only result | MP3, original |
| Instagram reel / photo / carousel | Preview or grid | Video MP4, photos at original quality |
| TikTok video / photo slideshow | Preview or grid | MP4 (no watermark if available), photos, audio |
| X video / X 4-photo post | Preview or grid | MP4, photos `name=orig` |
| Facebook public video | Preview | MP4 |
| Reddit video / gallery, Vimeo, Dailymotion | Preview | Correct files |
| Direct `.mp4`, `.mp3`, `.jpg`, extensionless `format=jpg` | Preview and info | Original, plus a converted format |
| HLS `.m3u8` test stream, generic news page video | Preview | MP4 |

Automated guards: no `QLineEdit` named or labelled "File name" anywhere in the GUI. No `QCheckBox`
cell widgets in tables. Every audio preset's post-processor list contains metadata and thumbnail
embedding (except the WAV cover). `TALB` is never set from `playlist_title`.

---

## 5. Out of scope
DRM circumvention (Netflix, Spotify's own audio, Apple Music audio), paywall or login bypass, scraping
y2mate / 9xbuddy / spotisaver servers, and local-file conversion tools. The Sniffer (original M7)
stays for later.

## 6. Open questions for the owner
1. **The MP3s in the public repo:** delete them going forward only (simple), or also rewrite GitHub
   history with a force push so they are gone completely?
2. **"Also download the audio version":** every video already has an Audio tab. Do you also want a
   one-click **"Video + MP3 (both files)"** row in the Video tab?
3. **R8:** should updates install only when you click **Update selected** (as planned), or should
   yt-dlp update by itself without asking?
