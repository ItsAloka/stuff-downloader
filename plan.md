# Stuff Downloader — Rebuild Plan v2

> **Written:** 2026-09-24 by Claude, after reviewing the Sep 22 stable build, the Sep 23 Codex
> build, the `problmes/` screenshots and notes, and similar open-source downloaders on GitHub.
>
> **Earlier plans are still in git:** the original M0–M7 architecture plan is `git show 1887f92:plan.md`,
> and the Sep 23 Codex change plan is `git show f98cb6c:plan.md`. The architecture in the original
> plan still applies: worker process per job, engines in their own runtime, `core/` has no Qt
> imports, and every URL is checked before use. This plan replaces its **product and UI** sections.

---

## 0. What this app is for

**Paste a link and get the video, audio or image you want, in the format you pick.** That is the
whole job.

1. The app works out **what the link is** (video, audio, image, gallery or playlist) without
   being told.
2. It shows a **proper preview** (thumbnail/cover, title, uploader, duration, site) for every site,
   not only YouTube.
3. It lists **every format it can deliver**, grouped under **Video / Audio / Image** tabs, with
   quality and size, and a Download button on each row. This is the 9xbuddy / ytmp3 layout
   the owner shared.
4. **Every video can also be saved as audio only** (the Audio tab always appears for videos).
5. Format conversion happens **while downloading**. No separate Converters tab.
6. **Click the title to rename.** No separate "File name" field anywhere.
7. Music files always get cover art and tags. That is the default behaviour, not a preset name
   or a checkbox.
8. The UI can look plain, but it must be **completely functional**: nothing clipped, nothing
   hidden, and no huge buttons.

---

## 1. Baseline decision

| Version | Commit | Verdict |
|---|---|---|
| **Sep 22 stable** (installer `StuffDownloader-Setup-1.0.0.exe`, built 2026-09-22 21:50) | `2171346` (committed 2026-09-23 00:11) | **Rebuild from here.** |
| Sep 23 morning (Codex) | `8969ee1` | Converters (drop) and multi-select history removal (keep). |
| Sep 23 night (Codex) | `f98cb6c` | Broke the UI and presets. Keep only the parts listed in §1.2. |

### 1.1 Reset steps (milestone R0)
1. Create branch `rebuild` from `2171346`. Do not rewrite `main`'s history for the code itself.
2. Check that `2171346` really is what the 1.0.0 installer shipped. Build it and compare the
   About version and behaviour with the installed 1.0.0. The installer was built 2h20m before
   the commit.
3. Bring back the **keepers** from §1.2 by hand-picking them, not by reverting whole commits.
4. Delete the Converters tab and its worker engines completely (see §1.3).
5. **Remove the four copyrighted MP3s in `.devteam-smoke/`**. `f98cb6c` committed them and they are
   now on the **public** GitHub repo. Add `.devteam-smoke/` and `problmes/*.png` to `.gitignore`.
   Rewriting history with a force push to remove them completely is the owner's decision (§12 Q1).
6. Run the full test suite and ruff. Record the baseline pass count in this file.

### 1.2 Keep from the Sep 23 work (re-apply by hand, with tests)
| What | Where it was | Why keep |
|---|---|---|
| Remove several selected History rows at once (files left alone) | `8969ee1` `core/history.py`, `HistoryPage` | The owner confirmed it works. |
| Public-host thumbnail fetch with HTTPS-only, DNS-pinned redirects (`_public_thumbnail_url`, `_open_thumbnail`) | `f98cb6c` `engines/ytdlp.py` | This is the correct fix for missing non-YouTube previews (P5). |
| Image MIME map (`IMAGE_TYPES`), and naming files by their real type | `f98cb6c` `engines/http.py` | Fixes `format=jpg` style links saved with the wrong extension. |
| Spotify `cover_url` passed through worker → core → GUI, and GUI allow-list for `*.spotifycdn.com` / `*.scdn.co` | `f98cb6c` | Needed for P9, but the cover source changes (§7). |
| Bulk queue logic: cancel remaining, clear non-active, keep or discard partial files | `f98cb6c` `pages.py` `_remove_jobs` | The logic is fine. The layout is not (P11). |
| Ctrl+C / right-click Copy in tables | `f98cb6c` `_PlaylistTable` | Something the owner asked for. |
| Version-marker idea (remix / live / sped-up / lyric) for Spotify matching | `f98cb6c` `core/spotify.py` | Keep only as a *warning signal*. It must not block downloads (§7). |

### 1.3 Drop
- `gui/converters.py`, `worker/audio_convert.py`, `worker/video_convert.py`,
  `engines/audioconvert.py`, `engines/imageconvert.py`, `engines/videoconvert.py`,
  `tests/convert/test_local_*`, `tests/gui/test_converters.py`, and the sidebar entry.
  (`worker/image_convert.py` from the stable build **stays**: download-time image conversion uses it.)
- Every **"File name"** field and column (`PreviewCard.name_edit`, `PlaylistCard` column 6,
  `SpotifyCard.NAME_COLUMN`).
- The **"Crop cover to a square"** checkbox and the `"MP3 — Music (square cover + tags)"` label.
- The 2×N grid of full-width buttons on `JobCard`.
- The Spotify "every selected song must be checked before download" block, and the automatic
  "No match found" when the title variant differs.
- `audio_original` with no metadata, and the M4A/FLAC/WAV presets that do not tag.
- Probing every non-social link as a direct file first (it adds a network round trip and a worker
  spawn to every paste).
- Default audio preset changed to Opus.

---

## 2. What went wrong on Sep 23

### 2.1 Process failures
1. **Tasks were accepted with no independent review.** The Codex plan itself says "DevTeam
   completed a read-only self-review because only one agent was present." No other agent and
   no owner looked at the UI before it was marked `[x]`.
2. **Requests were implemented backwards.** The owner asked to *edit the title directly and remove
   the File name field*. Codex **kept** the File name field and made a title click *jump to it*
   (`PreviewCard.eventFilter`, `_focus_name_on_title_click`, and F2 in `_SpotifyTable`).
3. **Scope drift.** A Converters tab (about 1,400 lines) was built before the core problems
   (image posts, previews, media detection) were fixed. It moved the app away from its purpose.
4. **Checkboxes marked done that were not done.** The plan said "Direct image links …
   downloaded correctly" and "confirmation safeguard implemented" while the screenshots show
   otherwise.
5. **Private test media committed to a public repo** (`.devteam-smoke/*.mp3`).

### 2.2 Code regressions introduced on Sep 23
| # | Regression | Evidence |
|---|---|---|
| R-1 | Queue cards became tall 2-column grids with full-width Pause/Cancel buttons | `problmes/3.png`, `JobCard` `controls = QGridLayout()` |
| R-2 | "Original audio" lost `FFmpegMetadata`, so files have no tags. M4A/FLAC/WAV extract only, with no tags or cover | `worker/presets.py` `audio_original`, `audio_m4a/flac/wav` branches |
| R-3 | Default playlist and music preset switched from MP3 to Opus | `pages.py` `findData("audio_original")` |
| R-4 | Spotify download refused until *every* selected track was matched. Uncertain ones needed a modal Yes/No | `start_spotify_download` (`missing` / `uncertain` blocks), `core/spotify.batch_specs` raises |
| R-5 | Any title "variant" mismatch turned a match into "No match found" | `spotdl.py` `if title and _variants(title) != …: return None`, `problmes/3.png` row 6 |
| R-6 | Checkbox column got `margin-left:10px` inside a fixed 44 px column, so the tick is clipped even more | `PlaylistCard.set_entries`, `problmes/1.png`, `2.png` |
| R-7 | A File name column was added to Spotify, and the playlist File name column was kept | `problmes/2.png`, `5.png` |
| R-8 | Every non-social link probed as a direct file before yt-dlp | `pages.analyze` `probe_file` |

---

## 3. All current problems (P-list)

Each problem notes whether it is from Sep 22 (**S**), a Codex regression (**C**), or both. Each fix is in
the milestone named.

| ID | Problem | Root cause (checked in code) | Fix in |
|---|---|---|---|
| **P1** | The app does not know if a link is video, audio or image. Every yt-dlp result gets the same video presets, including SoundCloud and other audio-only pages. Direct audio/video files get only "Original file". | `DownloadsPage._fill_presets(file=…)` picks presets only by route kind. No field in the analyze result says "audio-only" or "image". **S** | R1, R2 |
| **P2** | No format list like 9xbuddy/ytmp3. Options are hidden in Preset + Quality dropdowns. The "All formats" table is read-only. | Plan decision in M3 ("no per-row download"). **S** | R2 |
| **P3** | No format conversion at download time (MP4/MKV/WebM, MP3 bitrates, FLAC/WAV, PNG/WebP). The Converters tab was the wrong fix. | Presets are a fixed list of 6. **S + C** | R2 |
| **P4** | The "File name" field or column appears everywhere. The owner wants to click the title and edit it. | `PreviewCard.name_edit`, `PlaylistCard` col 6, Spotify col 9. **S + C** | R2, R5 |
| **P5** | Most non-YouTube videos (Instagram, TikTok, X, Vimeo…) show a blank preview. | `ytdlp._thumbnail_url` only accepts `*.ytimg.com`, `*.ggpht.com`, `*.googleusercontent.com` (`_THUMB_HOST_SUFFIXES`). Everything else is dropped. **S** | R3 |
| **P6** | Direct video/audio file links show no preview and no info (duration, resolution, bitrate). | `HttpEngine` analyze builds a preview only for images. **S** | R3 |
| **P7** | **Social-media images are blocked while videos work** (Instagram photos/carousels, TikTok photo slideshows, Facebook photos). | They go to gallery-dl, which now needs a login for Instagram ("HTTP redirect to login page", gallery-dl issue #9564, June 2026). TikTok returns 403. yt-dlp ignores photos ("no video in this post"). Our knowledge notes confirm: *"Instagram redirects to login, TikTok profiles 403 (Sept 2026)"*. **S** | R4 |
| **P8** | The blue tick in playlist and Spotify tables is clipped. | `QCheckBox` placed as a *cell widget* in a narrow column with the theme's padding, in compact 30–36 px rows (plus R-6). **S + C** | R5 |
| **P9** | Spotify album art does not load in the table or queue. | spotDL's free client returns **no cover for playlist rows** (header note in `engines/spotdl.py`). The table only showed the *YouTube match* thumbnail after "Check matches". **S** | R6 |
| **P10** | **Album metadata replaced by the playlist name.** | `tagging.verify_mp3(album=request.playlist_title)` *always overwrites* `TALB` with the YouTube playlist title. `TRCK` is set to the playlist position. **S** | R5 |
| **P11** | Downloads/queue UI got worse: huge buttons, tall cards, two rows of bulk buttons. | R-1, and the queue header split into two `QHBoxLayout`s. **C** | R5 |
| **P12** | Spotify → YouTube "mirror" picks the wrong recording for less-popular songs. A lyric upload scored 94 as certain (Chinese sample). A Sinhala match was 4.8 s short. | Matching uses a title/artist/duration score only. It does not prefer official "song" (Topic/Art Track) results or album context. **S** | R6 |
| **P13** | The Spotify pipeline is slow and fragile: about 10 s per spotDL free-client call and about 30 s per YTM search per track. The spotDL env also **cannot be redistributed** (spotapi / spotipyfree have no licence). | spotDL used as the metadata layer. **S** | R6 |
| **P14** | A pasted fragment (e.g. `pbs.twimg.com/media/…` with no `https://`) gets a video-only error. | The router rejects it with video wording. It could add `https://` itself when the host is valid. **S** | R1 |
| **P15** | Uncertain Spotify matches block the whole batch, or silently become "No match". | R-4, R-5. **C** | R6 |
| **P16** | Titles in tables cannot be copied. | Table text is not selectable. **S** (Codex fix kept) | R5 |
| **P17** | Converters tab is clutter. | `8969ee1`. **C** | R0 |
| **P18** | Queue layout does not fit narrow windows with long titles. | Fixed minimum widths. **S** | R5 |
| **P19** | Playlist row pictures are tiny (48×27) and exist only for YouTube. Spotify rows are blank until matched, and then show the *YouTube* image. There is no playlist cover in the header. | `ROW_THUMB = QSize(48, 27)`. Row URLs are built only from YouTube video ids. `_request_spotify_thumb` uses the match's video id. **S** | R5, R6 |

**Confirmed working on Sep 22 (keep working):** YouTube single videos, songs and playlists. Direct
image links (preview and download). Most Spotify matches (about 90%). History. Tray and toasts.
Installer with bundled FFmpeg/ffprobe/Deno and the engine runtime.

---

## 4. How other open-source projects solve this (research, Sep 2026)

| Project | What it does well | What we take |
|---|---|---|
| **[9xbuddy](https://github.com/divyanshusahu/9xbuddy)** (the linked repo) | It is only a browser extension that sends the page URL to 9xbuddy.in. No extraction code. | **UX only:** Format / Quality / Size / Action rows and All / Video / Audio / Images tabs (owner screenshot 3). |
| **[cobalt](https://github.com/imputnet/cobalt)** (`api/src/processing/services/*.js`) | Public posts with **no login** on Instagram, TikTok, X, Reddit and others. Returns a **"picker"** for multi-photo posts. | Our social image extractor (R4) follows its per-site approach. See the details below. |
| **[yt-dlp](https://github.com/yt-dlp/yt-dlp)** | 1,800+ video/audio sites. The generic extractor reads OpenGraph/JSON-LD (`og:image`) thumbnails. It does **not** return photos. | Stays the video/audio engine. We use *all* its thumbnails (not just YouTube hosts) and its full format list to build rows. |
| **[gallery-dl](https://github.com/mikf/gallery-dl)** | Galleries on hundreds of sites (Reddit, Pinterest, Tumblr, Imgur, Flickr, X…). Instagram now needs cookies (issue #9564). | Stays as the *second* image engine, after our social extractor. |
| **[spotDL](https://github.com/spotDL/spotify-downloader)** | Spotify metadata → YouTube Music match → tagged file. Its 2026 non-API client is slow. | Keep only as a fallback (R6). |
| **[Downtify](https://github.com/henriquesebastiao/downtify)** | Reads Spotify from the public **`open.spotify.com/embed/...`** pages (no credentials) and matches with **ytmusicapi** by duration. MP3/FLAC/M4A/OGG/Opus output with mutagen tags. | **Embed-page listing + ytmusicapi matching** (R6). |
| **[jackson29382938/spotify-downloader](https://github.com/jackson29382938/spotify-downloader)** | Strict title + artist + duration matching. Handles the embed page's first-page limit. Retries with a different search route. | Strict matching rules and the pagination fallback (R6). |
| **[Seal](https://github.com/JunkFood02/Seal)**, **[Parabolic](https://github.com/NickvisionApps/Parabolic)** | yt-dlp GUIs. Audio-only extraction with embedded metadata and thumbnail. Output formats mp4/webm/mp3/opus/flac/wav. | Confirms the format set and "tags + cover always on" for audio. |

**Checked live today (2026-09-24, no credentials):**
- `https://open.spotify.com/embed/playlist/<id>`: `__NEXT_DATA__ → props.pageProps.state.data.entity.trackList[]`
  gives `uri, title, subtitle (artists), duration (ms), isExplicit` in **one fast request**, plus the
  playlist `coverArt`. There are no per-track covers. The row cap still needs checking; about 100 is commonly reported.
- `https://open.spotify.com/embed/track/<id>` gives `title, artists[], duration, releaseDate` and
  `visualIdentity.image[]` (64 / 300 / 640 px album art on `image-cdn-ak.spotifycdn.com`).
- `https://open.spotify.com/oembed?url=https://open.spotify.com/track/<id>` gives `thumbnail_url` (300 px album art).

**Cobalt's no-login methods (to reimplement in Python, R4):**
- **Instagram:** `i.instagram.com/api/v1/oembed/?url=…` → `media_id` → `i.instagram.com/api/v1/media/<id>/info/`
  (mobile UA). Fallback: `www.instagram.com/p/<code>/embed/captioned/` (JSON in page). Fallback: `graphql/query`
  with `doc_id` for `PolarisPostActionLoadPostQueryQuery`, using `lsd` / `csrftoken` scraped from the page and
  `x-ig-app-id: 936619743392459`. Carousels come from `carousel_media[]` → `image_versions2.candidates[0]` /
  `video_versions[]`.
- **TikTok:** `www.tiktok.com/@i/video/<id>` → `<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">` →
  `__DEFAULT_SCOPE__["webapp.video-detail"].itemInfo.itemStruct`. Photos are in `imagePost.images[].imageURL.urlList`
  (take the `.jpeg` one) and the sound is in `music.playUrl`. `vt.tiktok.com` short links are resolved by redirect.
- **X / Twitter:** `cdn.syndication.twimg.com/tweet-result?id=<id>&token=<t>` where
  `t = ((id / 1e15) * π).toString(36)` with zeros and dots removed. Photos use `?name=orig` (or `4096x4096`),
  and videos use the highest-bitrate `video/mp4` variant. Fallback: guest-token GraphQL `TweetDetail`.
- These are **unofficial endpoints and will break sometimes.** That is why they live in the worker (updatable
  with "Update engines"), use the yt-dlp env's `curl_cffi` for browser-like TLS, fall back to gallery-dl, and
  finally offer the optional site login (plan §6.4).

---

## 5. Target design

### 5.1 One flow for every link
```
paste ─► router (URL shape) ─► analyze worker ─► MediaResult ─► Result card
                                                 │               ├ Video tab  (rows)
                                                 │               ├ Audio tab  (rows)
                                                 │               └ Image tab  (rows / grid)
                                                 └ playlist ───► Track table (batch format)
click a row's [Download] ─► job spec {tab, format row id, edited title} ─► worker ─► file
```

### 5.2 `MediaResult`: one analyze shape for every engine (R1)
Every engine's analyze returns the same sanitized structure, and the GUI draws only from it.
It never branches on the engine or route kind.
```text
MediaResult
  kind:        "video" | "audio" | "image" | "gallery" | "playlist"
  title, uploader/artist, album?, duration?, site, webpage (durable URL)
  preview:     {bytes, width, height} | null      # always fetched by the worker
  source_audio:{codec, abr_kbps}?                  # "Source audio: Opus 160 kbps"
  video_rows:  [{id, height, fps, hdr, vcodec, container, size, size_is_estimate}]
  audio_rows:  [{id, label, codec, bitrate, size, size_is_estimate, lossless_note?}]
  image_rows:  [{id, width, height, ext, size}]    # thumbnail sizes, or the image itself
  items:       [{pos, kind:image|video, preview, width, height}]   # gallery / carousel only
  entries:     [...]                                               # playlist only
```
Row ids are **ours** (for example `v:1080:mp4`, `a:mp3:320`, `i:orig`). The worker turns them back into
format selectors and post-processors, so no site-supplied format id reaches the engine. This follows the
original plan's rule.

### 5.3 Detecting the media type (R1)
| Signal | `kind` | Tabs shown (default first) |
|---|---|---|
| yt-dlp: any format with `vcodec != none` | video | **Video**, Audio, Image (thumbnail) |
| yt-dlp: `music.youtube.com`, or YouTube with `track`/`artist` fields (Topic / Art Track) | audio | **Audio**, Video, Image (cover) |
| yt-dlp: every format has `vcodec == none` (SoundCloud, Bandcamp, Mixcloud, Audiomack…) | audio | **Audio**, Image (cover) |
| HTTP engine: `Content-Type: video/*` (or a generic type plus a video extension) | video | **Video** (original + convert), Audio (extract), Image (frame) |
| HTTP engine: `audio/*` | audio | **Audio** (original + convert), Image (embedded cover, if any) |
| HTTP engine: `image/*` (checked by MIME, not by extension) | image | **Image** (original + JPG/PNG/WebP) |
| Social extractor / gallery-dl with more than one item | gallery | **Gallery grid** (photos and videos, checkboxes) |
| Social extractor with one photo | image | **Image** |
| YouTube / YTM / Spotify list | playlist | Track table |

**P14 fix:** when the pasted text has no scheme but starts with a valid public hostname
(`pbs.twimg.com/…`), add `https://` in front and show a note. Error text is neutral about media
type, e.g. "That isn't a complete link. Copy the full address starting with https://".

### 5.4 The format catalog (R2)
**Video tab.** One row per height that **actually exists**, highest first:

| Format | Quality | Size | |
|---|---|---|---|
| MP4 | 2160p60 · AV1 (not supported by older players) | ~412 MB | ⬇ |
| MP4 | 1080p60 · H.264 · **plays everywhere** ★ | ~142 MB | ⬇ |
| MP4 | 720p · H.264 | ~61 MB | ⬇ |
| … | 480p / 360p / 240p / 144p | … | ⬇ |

- **"Save video as"** selector above the rows: **MP4** (default), **MKV**, **WebM**, **MOV**, **AVI**.
  MP4/MKV/WebM are a remux, so they are fast and lossless when the codecs allow it. MOV/AVI and incompatible
  codec pairs need a re-encode; the row then says "(re-encodes, slower)". This replaces the Converters tab.
- Size is video-stream size plus best-audio size (`filesize` or `filesize_approx`, shown as "~").
  It shows "unknown" if neither exists.
- Direct video file: one "Original file (as served)" row, plus the same container choices.
- ★ marks the default: the highest H.264 height up to 1080p, as in Settings.

**Audio tab** (shown for **every video** and every audio source):

| Format | Quality | Size | |
|---|---|---|---|
| MP3 | 320 kbps ★ | ~9.3 MB | ⬇ |
| MP3 | 256 / 192 / 128 / 64 kbps | … | ⬇ |
| M4A (AAC) | original stream, no re-encode *(if the source is AAC)*, otherwise 256 kbps | … | ⬇ |
| Opus | original stream, no re-encode *(if the source is Opus)* | … | ⬇ |
| FLAC | lossless container: same sound, bigger file | … | ⬇ |
| WAV | uncompressed: same sound, much bigger | … | ⬇ |

- A header line states the honest source quality, e.g. "Source audio: Opus ~160 kbps. Higher MP3
  bitrates do not add quality." The tab does not repeat this per row.
- **Always**, with no checkbox: tags (title, artist, album, year from the source) and an embedded cover.
  The cover is a native square if the site has one, otherwise a centre crop. WAV gets no embedded cover
  (the format does not support it reliably), and its row says so.
- MP3 sizes are estimated as duration × bitrate ÷ 8, marked "~".

**Image tab:**
- For a video or audio source: the thumbnail/cover sizes that exist (e.g. 1280×720 JPG), each savable
  as **Original / JPG / PNG / WebP**.
- For an image link: **Original** (real type from MIME) ★, **JPG**, **PNG**, **WebP**, using
  `worker/image_convert.py`, which already handles transparency → JPG flattening.

**Gallery (carousel, slideshow, album):** a grid of tiles (checkbox, preview, 🎞 badge for
videos, size). "Download selected as [Original ▾]" for photos. Video items are always saved as the
original MP4.

### 5.5 Title editing: one rule everywhere (R2, R5)
- The title is a label with a small ✎. **One click turns it into a text box in the same place.** Enter
  or clicking away saves, Esc cancels, and "↺" restores the original.
- Tables (playlist / YTM / Spotify): **clicking the Title cell edits it in place**
  (`QTableWidgetItem` with `ItemIsEditable`, edit triggers `SelectedClicked | DoubleClicked | EditKeyPressed`).
- **The edited title is used only for the file name.** Tags keep the source's real title, artist
  and album. Default names: video `Title.ext`, music `Artist - Title.ext`. If the title was edited:
  `EditedTitle.ext` exactly, sanitised for Windows by the existing `safe_output_name`.
- No "File name" field or column anywhere. A test fails if one appears (§9).

### 5.6 Previews for everything (R3)
| Source | Preview source |
|---|---|
| yt-dlp (any site) | Largest `thumbnails[]` / `thumbnail` on **any public HTTPS host**, fetched by the worker with the Sep 23 `_open_thumbnail` checks (kept). Prefer square for music. |
| yt-dlp found no thumbnail | Page `og:image` / `twitter:image` / JSON-LD `thumbnailUrl` (one extra GET in the worker). |
| Direct video file | FFmpeg frame grab at about 10% of duration (`-ss`, `-frames:v 1`) with `-protocol_whitelist https,tls,tcp` on the already-checked URL. ffprobe gives resolution, duration and codecs. |
| Direct audio file | ffprobe gives duration, bitrate and codec. The embedded cover is extracted if present. Otherwise a plain 🎵 placeholder. |
| Image / gallery / carousel | The image itself (byte and pixel caps already exist in `thumbs.decode_image`). |
| Spotify | Track embed `visualIdentity.image` or oEmbed `thumbnail_url` (R6). |

The preview is bigger than today: **about 480×270** for video and **300×300** for music. The duration is
drawn over the preview's corner. The placeholder text is shown only when the site really has no image.

### 5.6a Playlist previews: every row has a picture (R5, R6), fixes P19
The owner's request (2026-09-24): *"if we are downloading a playlist we can still show a preview image,
even small. The playlist can be videos or songs, but the preview must show, like the screenshots."*

| Playlist type | Header picture | Row picture | Row size |
|---|---|---|---|
| YouTube **video** playlist / channel uploads | First video's thumbnail | `i.ytimg.com/vi/<id>/mqdefault.jpg` (16:9) | **96×54** |
| YouTube **Music** playlist / album | Album or playlist cover (square) | The same ytimg thumbnail, **centre-cropped square**, so it shows the song's cover | **56×56** |
| **Spotify** playlist / album | `coverArt` from the embed page | That track's album art from oEmbed `thumbnail_url` (§7), shown **before** any match check | **56×56** |
| Other yt-dlp playlists (SoundCloud sets, Vimeo showcases…, R7) | Playlist thumbnail | Entry `thumbnails[]`, fetched by the worker (public-host checks) | 96×54 video / 56×56 audio |

- The table picks video or song layout from the playlist type: a 16:9 picture for video lists,
  a square picture for music lists. Row height follows the picture (about 60–64 px), so titles still fit.
- The playlist card header shows the playlist's own cover at about **160×160** (or 16:9 for video lists),
  with the title, owner and count, like the single-item Result card.
- Loading stays **lazy**: only rows on screen are fetched (as `_request_visible_playlist_thumbs` does
  today), cached, and capped. A grey placeholder with 🎞 or 🎵 shows until the picture arrives, so
  rows never jump.
- The **same picture** follows the song or video into its **queue card** and into **History**.
- A Spotify row keeps its **Spotify album art** even after "Check matches". The YouTube match's
  thumbnail appears only as a small image inside the Change… dialog, so the owner can compare.
- Hovering over a row picture shows it at about 240 px, to check a cover without opening anything.

### 5.7 Metadata rules (R5), fixes P10
- **Album (`TALB`) comes only from the track's real album** (YTM `album`, Spotify album). It is never the
  playlist title. If the album is unknown, it is left empty.
- **Track number (`TRCK`)** is written only when downloading an **album** (source album position), never
  the position in an arbitrary playlist.
- The playlist title is used only as the **subfolder name**. Keep that setting.
- Artist: YTM `artist(s)`. For plain YouTube, the uploader with ` - Topic` / `VEVO` removed.
- The user's edited title never changes `TIT2`.
- The same tag writer (mutagen) handles MP3, M4A, Opus, FLAC and WAV, so every audio format gets the same
  tags. This fixes R-2.

### 5.8 UI spec: plain, native, functional (R2, R5)
- Plain Qt Fusion style with the existing dark theme colours. Standard widgets only: tables for
  lists, tabs for Video/Audio/Image. No custom-painted cards for tables.
- Sidebar: **Downloads · History · Tools · Settings** (Converters removed).
- Must work at **1280×720, 1920×1080, maximised, and 125%/150% DPI** with **no horizontal scrollbar**.
  Long titles are elided with a tooltip.
- Checkboxes in tables are **model check states** (`Qt.ItemIsUserCheckable`), not `QCheckBox` cell
  widgets. The style draws them in full, so they cannot be clipped (P8).

```
┌ Result ───────────────────────────────────────────────────────────────────────────┐
│ ┌──────────────────────┐  Midnight City ✎                                         │
│ │  preview 480×270     │  M83 · 4:04 · YouTube Music                              │
│ │                 4:04 │  Source audio: Opus ~160 kbps                            │
│ └──────────────────────┘  Save to: C:\Users\…\Downloads            [Change…]      │
│  [ Video 7 ] [ Audio 7 ] [ Image 2 ]                    Save video as: [MP4 ▾]    │
│  ┌────────┬──────────────────────────────────┬──────────┬───────────────┐         │
│  │ MP3    │ 320 kbps ★                       │ ~9.3 MB  │ [ ⬇ Download ]│         │
│  │ MP3    │ 256 kbps                         │ ~7.5 MB  │ [ ⬇ Download ]│         │
│  │ M4A    │ AAC original (no re-encode)      │  3.8 MB  │ [ ⬇ Download ]│         │
│  │ FLAC   │ lossless container, same sound   │ ~26 MB   │ [ ⬇ Download ]│         │
│  └────────┴──────────────────────────────────┴──────────┴───────────────┘         │
└───────────────────────────────────────────────────────────────────────────────────┘

┌ Playlist ─────────────────────────────────────────────────────────────────────────┐
│ ┌──────────┐  Aloka' Temp Songs                                                   │
│ │ playlist │  KernelKhaos · 5 songs · 16:47                                       │
│ │ cover    │                                                                      │
│ │ 160×160  │                                                                      │
│ └──────────┘                                                                      │
│ ☑  #  cover     Title (click to edit)        Artist          Length   Status      │
│ ☑  1  ┌──────┐  Collide (Solo Version)       Justine Skye    4:23                 │
│       │56×56 │                                                                    │
│       └──────┘                                                                    │
│ ☑  2  ┌──────┐  i'm yours                    Isabel LaRosa   2:26     ✓ in folder │
│       └──────┘                                                                    │
│   (a video playlist uses 96×54 16:9 pictures in the same column)                  │
│ [Select all] [Select none] [Filter…]   Download selected as: [MP3 320 kbps ▾]     │
│ ☑ Skip songs already in this folder                     [ ⬇ Download 5 selected ] │
└───────────────────────────────────────────────────────────────────────────────────┘

┌ Queue ─ 1 active · 4 queued · 1 done   [Pause all] [Cancel remaining] [Clear done ▾]┐
│ [art] Takanori Nishikawa - Ignis              Downloading  44%          [⏸] [✕]    │
│       MP3 320 kbps · 4.1 / 9.3 MB · 2.3 MB/s · 0:03 left                           │
│       ██████████████░░░░░░░░░░░░░░░░░                                              │
│ [art] 須田景凪 - veil                          ✔ Done · 7.9 MB        [Open] [📂]    │
└────────────────────────────────────────────────────────────────────────────────────┘
```
- The queue card is **one row plus a thin progress bar**, as on Sep 22. Buttons are small icons,
  placed on the right.
- Bulk actions sit in the queue header. "Clear done ▾" is a menu: *Finished*, *Cancelled & failed*,
  *Everything not running*. The keep/discard question for partial files is kept from §1.2.
- Per-row selection checkboxes on queue cards are removed. Selection is not needed once the bulk menu
  exists, which keeps cards compact.

---

## 6. Social images, no login (R4), fixes P7
New worker module `stuff_downloader_worker/engines/social.py`, running in the **yt-dlp env**
(stdlib + `curl_cffi` already installed there, both licence-clean):

| Site | Method (in order) | Output |
|---|---|---|
| Instagram `/p/`, `/reel/`, `/tv/` | mobile oEmbed → media info → `embed/captioned` JSON → GraphQL `doc_id` | 1 image → `image`, several → `gallery` (photos and videos mixed) |
| TikTok `/photo/`, photo posts under `/video/`, `vt.`/`vm.` short links | `__UNIVERSAL_DATA_FOR_REHYDRATION__` → `imagePost.images` (+ `music.playUrl` as an Audio row) | gallery + audio |
| X / Twitter status | syndication `tweet-result` + token → `mediaDetails` (`?name=orig`) → guest GraphQL fallback | image / gallery / video |
| Reddit post | `<post>.json` → `gallery_data` + `media_metadata`, `i.redd.it` | image / gallery |
| Any other page | yt-dlp first. If there is no video, gallery-dl. If that fails, `og:image` as a single image. | — |

- The router sends Instagram, TikTok, X and Reddit **post** links to `social` first, then yt-dlp
  (videos), then gallery-dl, and finally the optional site-login offer (plan §6.4). Stories, highlights
  and private profiles still need a login. Those get a clear message, not a generic error.
- Item URLs stay in the worker, as with gallery-dl today. Downloads go by **position**. Every media URL
  passes the existing public-host check with pinned DNS (`http.check_url`).
- Keep per-site rate limits (one job at a time per site, delays), already in `SOCIAL_HOST_GROUPS`.
- Live check before each release, because these endpoints change: one public post of each type per
  site, from the owner's link list (§9).

---

## 7. Spotify v2 (R6), fixes P9, P12, P13, P15
1. **Listing:** the embed page (`/embed/{track|album|playlist}/<id>`, one request, no credentials).
   If the playlist is longer than the embed's page cap, fall back to the current spotDL `get_metadata`
   for the remaining tracks. Album name for playlist rows comes from the matched YTM song. For album
   links, it is the album itself.
2. **Covers:** per track, from oEmbed `thumbnail_url` (300 px, fetched lazily for visible rows). For
   tagging, the 640 px variant from the track embed's `visualIdentity.image` (**P9**).
3. **Matching (ytmusicapi directly, no spotDL search):**
   1. *Album-first:* for album links, or when the album is known, search YTM **albums** by album +
      artist, `get_album`, then pick the track by title and duration (±2 s). This finds the official
      audio.
   2. *Songs search* (`filter="songs"`, i.e. Topic/Art Track uploads): the artist must match, the
      duration must be within ±3 s, and version markers (remix / live / sped-up / lyric / cover /
      instrumental) must be the same on both sides.
   3. *Videos search:* used only as a fallback, and **always marked uncertain**.
4. **Uncertain matches never block the batch.** Certain matches download right away. Uncertain ones
   wait in the table with a ⚠ and a "Review" button (the existing Change… dialog with alternatives).
   One button, "Download uncertain anyway", sends them all.
5. **Licensing:** when steps 1–3 work, the spotDL env becomes optional. That also fixes the spotDL
   redistribution blocker recorded in `knowledge/`.
6. Tagging: Spotify title, artists, album, year and cover. The file name follows §5.5.
7. Re-run the four-language live sample (JP / CN / EN / SI in `.devteam-smoke/run.py`, stored **outside
   the repo**). Target: the Chinese lyric-video case is flagged, and the Sinhala case matches within
   ±3 s or is flagged.

**Other music sites** (R7): SoundCloud, Bandcamp, Mixcloud and Audiomack already work through yt-dlp
as real audio and appear as `audio` results. Apple Music and Deezer links get the Spotify treatment:
metadata from their free public lookups (iTunes Lookup API, Deezer API, no keys) and the same YTM
matching. Tidal and Amazon Music are refused by name (DRM, no public metadata).

---

## 8. Milestones
Each milestone ends with: tests green, ruff clean, **screenshots at 1280×720 / maximised / 150% DPI
sent to the owner**, **owner acceptance**, then one commit. **Claude implements. Codex reviews and
plans only** (owner decision, 2026-09-24).

### R0 — Reset (half a day)
§1.1 steps 1–6. The Converters tab is gone. The copyrighted MP3s are gone from the tree. The baseline
test count is recorded.
**Accept:** the app looks and behaves like installer 1.0.0, plus multi-select History removal.

> **R0 status (2026-09-24):** branch `rebuild` from `2171346`. The History multi-select removal and
> its tests are re-applied from `8969ee1`. Converters are absent (they never existed at `2171346`).
> `.devteam-smoke/` and `problmes/` are git-ignored. The MP3s were removed from `main` in a normal
> commit (owner chose option (a), no history rewrite). **Baseline: 1153 passed, 1 skipped,
> 20 deselected (network), ruff clean.** The other §1.2 keepers are tied to the broken Sep 23 UI,
> so each is ported in its own milestone instead: thumbnail fetcher → R3, `IMAGE_TYPES` → R1,
> bulk queue logic and Copy → R5, Spotify `cover_url` → R6.
> Still open: build and side-by-side check against installed 1.0.0 (§1.1 step 2), then Codex review.

### R1 — Media model and detection
`MediaResult` schema in `core/protocol.py` and `worker/protocol.py` (kept identical by the existing test).
Each engine's analyze produces it. Detection per §5.3. Router `https://` prefix fix (P14).
Neutral error text. One analyze fallback chain decided in core, with no direct-file probe on every paste
(drops R-8).
**Accept:** fixture tests for YouTube video, YTM song, SoundCloud (audio-only), a direct mp4 / mp3 / jpg,
a `format=jpg` link with no extension, and an X photo post each return the correct `kind` and tabs.

### R2 — Result card, format tabs, title editing
The Result card per §5.8. Video / Audio / Image rows per §5.4 with a Download button on each row.
"Save video as" container choice. Worker: new request fields `{tab, row_id, container, edited_title}`,
validated like `presets.parse_request`, mapped to yt-dlp options:
- MP3: `FFmpegExtractAudio(mp3, 320|256|192|128|64)` + `FFmpegMetadata` + `EmbedThumbnail`
- M4A: `bestaudio[ext=m4a]`, copied (no re-encode)
- Opus: `bestaudio[acodec=opus]`, copied
- FLAC / WAV: extracted and converted
- Video: `merge_output_format`, or `FFmpegVideoConvertor` for MOV/AVI

Tags always on. Inline title editing. File name fields removed. Crop checkbox removed.
**Accept:** from one YouTube video, download 1080p MP4, 720p MKV, MP3 320, M4A and the PNG thumbnail.
Every audio file has tags and a cover, checked with mutagen. The edited title becomes the file name and
`TIT2` stays the original.

### R3 — Previews everywhere
§5.6. Kept thumbnail fetcher, `og:image` fallback, FFmpeg frame grab, ffprobe info for direct files.
**Accept:** Instagram reel, TikTok video, X video, Vimeo, Reddit video, direct mp4 and a generic news
page video all show a preview. A direct mp3 shows duration and bitrate.

### R4 — Social images
§6. Gallery grid and the image row flow.
**Accept (no login):** an Instagram single photo, an Instagram mixed carousel, a TikTok photo
slideshow, an X post with 4 photos and a Reddit gallery all download at original quality.
Stories and private accounts give a clear "needs a login" message with the optional login offer.

### R5 — Playlists, metadata, queue
One shared track-table widget for YouTube, YTM and Spotify, with model checkboxes (P8). Title cell
editing (P4). Copy (P16). Batch "Download selected as" drop-down using the §5.4 formats.
Metadata rules §5.7 (P10). Queue card and header per §5.8 (P11, P18).
Playlist previews per §5.6a (P19): header cover, 96×54 video / 56×56 song row pictures, lazy
loading with placeholders, and the same picture on queue cards and History.
**Accept:** a YouTube *video* playlist and a YTM *song* playlist both show a picture on every row
(16:9 and square respectively) and a header cover. The pictures follow into the queue.
YTM playlist → MP3 → each file's album is the real album, not the playlist name.
Ticks are fully visible at all sizes and DPIs. The queue has no horizontal scrollbar at 1280×720.

### R6 — Spotify v2
§7 items 1–7.
**Accept:** a 7-song playlist lists in about 2 s with **Spotify album art in every row before any
match check**, and the playlist cover in the header. The art stays after matching and follows into the queue. Certain matches start
immediately. Uncertain ones are flagged, not blocked. The four-language sample passes.

### R7 — Other music sites and release 1.1.0
SoundCloud / Bandcamp checked. Apple Music / Deezer link matching. Installer
`StuffDownloader-Setup-1.1.0.exe` with self-test. Full manual matrix (§9) on the installed build.
THIRD_PARTY_LICENSES updated (ytmusicapi, curl_cffi already there, and spotDL if still shipped).

---

## 9. Test matrix (manual, on the installed build, per release)
The owner provides one public link per row. Links are kept in a local file outside the repo.

| Category | Must show | Must download |
|---|---|---|
| YouTube video / Short | Preview, Video + Audio + Image tabs, real heights | 1080p MP4, MP3 320, thumbnail PNG |
| YouTube video playlist | Header cover, a 16:9 picture on every row | Batch MP4 and batch MP3 |
| YouTube Music song / album / playlist | Square cover, Audio tab first, a square picture on every playlist row | MP3 with real album tag, M4A original |
| Spotify track / album / playlist (EN/JP/CN/SI) | Cover per row, matches | Tagged MP3, uncertain ones flagged |
| SoundCloud / Bandcamp track | Audio-only result | MP3, original |
| Instagram reel / photo / carousel | Preview or grid | Video MP4, photos at original quality |
| TikTok video / photo slideshow | Preview or grid | MP4 (no watermark if available), photos, audio |
| X video / X 4-photo post | Preview or grid | MP4, photos `name=orig` |
| Facebook public video | Preview | MP4 |
| Reddit video / gallery, Vimeo, Dailymotion | Preview | Correct files |
| Direct `.mp4`, `.mp3`, `.jpg`, extensionless `format=jpg` | Preview and info | Original, plus a converted format |
| HLS `.m3u8` test stream, generic news page video | Preview | MP4 |

Automated guards added in R2/R5: no `QLineEdit` named or labelled "File name" anywhere in the GUI.
No `QCheckBox` cell widgets in tables. Every audio preset's post-processor list contains metadata and
thumbnail embedding (except the WAV cover). `TALB` is never set from `playlist_title`.

---

## 10. Working rules (lessons from Sep 23)
1. **Claude implements. Codex only reviews and plans.** Every change is reviewed by an agent that did
   not write it. Self-review does not count as review.
2. **UI changes need the owner to see screenshots** before they are marked done. Acceptance criteria
   quote the owner's own words.
3. Only one milestone at a time. Nothing outside this plan unless the owner adds it here first.
4. `[x]` only after the owner has seen it working. Put the evidence (test names, screenshots) next to it.
5. Before every commit, scan for secrets **and copyrighted media**. `.devteam-smoke/`, downloads and
   screenshots stay outside git. The repo is public.

---

## 11. Out of scope (unchanged from the original plan)
DRM circumvention (Netflix, Spotify's own audio, Apple Music audio), paywall or login bypass, scraping
y2mate / 9xbuddy / spotisaver servers, and local-file conversion tools. The Sniffer (original M7)
stays for later.

## 12. Open questions for the owner
1. **The MP3s in the public repo:** delete them going forward only (simple), or also rewrite GitHub
   history with a force push so they are gone completely?
2. **"Also download the audio version":** this plan reads it as *every video offers an Audio tab
   (audio-only download)*. Do you also want a one-click **"Video + MP3 (both files)"** row in the
   Video tab?
3. **Spotify:** is it OK to keep spotDL only as a fallback for playlists longer than the embed page
   shows, and drop it completely later if the embed + ytmusicapi path proves reliable?
