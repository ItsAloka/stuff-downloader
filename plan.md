# Stuff Downloader — Change Plan

## Planned feature: Converters tab

- Add a **Converters** tab to the sidebar.
- Provide three local conversion tools: **Video**, **Image**, and **Audio**.
- **Video:** select a local video file, choose MP4, MKV, AVI, MOV, WebM, or MPEG/MPG, then convert. Example: MPEG/MPG → MP4.
- **Image:** select a local image, choose JPEG, PNG, WebP, GIF, or BMP, then convert with options appropriate to the format.
- **Audio:** select a local audio file, choose MP3, M4A, AAC, Opus, WAV, or FLAC, then convert.
- Show source file, output format, destination, progress, clear completion/error state, and an **Open folder** action for every conversion.
- Use the bundled FFmpeg tool for video/audio conversions and the image conversion pipeline for image conversions.

## YouTube music output options

- Offer **Original audio / Opus** as the recommended default: preserve YouTube's source audio without re-encoding and keep files small.
- Offer **M4A/AAC** for broad compatibility and **MP3** for maximum device compatibility.
- Offer **FLAC** and **WAV** only as format-conversion options, with an explicit note that YouTube's source is normally lossy, so these formats do not increase sound quality and create larger files.

## Confirmed working

- YouTube playlists, single songs, and videos are working correctly.
- Direct image links are recognized, previewed, and downloaded correctly.
- Spotify-to-YouTube matching works well for most playlist songs (about 90%); less well-known tracks can still be mismatched.

## UI issues and improvements

### Playlist item selection indicator is clipped

- **Observed:** In the playlist results table, the blue selection tick at the far left of each row is cut off, so only part of the icon is visible.
- **Expected:** Display the entire tick/icon cleanly at every supported window size and DPI scale.
- **Fix direction:** Give the selection column and its delegate enough minimum width and horizontal padding; verify at normal and maximized window sizes.

### Playlist titles cannot be copied

- **Observed:** A user can drag across a playlist title but cannot copy the selected text.
- **Expected:** Titles (and other useful table text such as artist and file name) can be selected and copied with `Ctrl+C`; expose a right-click **Copy** option as well.
- **Fix direction:** Use copy-enabled text cells/delegates or implement the table's copy-to-clipboard action while preserving row selection and inline editing.

### History removal ignores multi-selection

- **Observed:** When several history rows are selected, **Remove from history** removes only the last selected row.
- **Expected:** Remove every selected history entry in one action. The downloaded media files must remain untouched, as the screen states.
- **Fix direction:** Pass the full set of selected row IDs to the history deletion action, then refresh the table while keeping the selection state consistent.

### Most non-YouTube social-media video previews are missing

- **Observed:** Downloads work for social-media links, but most analyzed video results outside YouTube (for example, Instagram Reels) show an empty/blank thumbnail area.
- **Expected:** Show the source-provided thumbnail or preview image for every supported social-media video source before download.
- **Fix direction:** Carry the extractor thumbnail URL through the analysis result, download it safely for display, and show a clear fallback image/message only when the source does not provide one.

### Direct image links are classified as videos

- **Observed:** A direct image URL (for example, a `pbs.twimg.com` URL with `format=jpg`) is detected as a generic video and is shown video presets and conversion options.
- **Expected:** Detect direct image links by their URL and response content type, show an image preview, and download the original image without video processing.
- **Fix direction:** Classify image MIME types (`image/jpeg`, `image/png`, `image/webp`, `image/gif`, etc.) before creating the result card; route them to an image-specific UI and direct download handler.

### Incomplete image links get a misleading error

- **Observed:** Pasting an incomplete image-link fragment (without `https://` and a hostname) produces the message “That link is not a video page we can read.”
- **Expected:** Validate the pasted value first and explain that a complete direct image URL or a supported social-media post URL is required.
- **Fix direction:** Parse and validate URLs before analysis; replace video-specific failure wording with actionable, media-neutral guidance.

### Social-media image downloads fail while video downloads work

- **Observed:** Video downloads from social-media post links work, but image posts from those sites cannot be downloaded reliably.
- **Expected:** Support image-only posts, single images, and multi-image/carousel posts from the same supported social-media sites, with selectable image items and original-quality downloads.
- **Fix direction:** Ensure the site extractor's image entries are not discarded by the video-only analysis/download path; route them to the existing image result and download flow, and test each supported social-media extractor with image-only posts.

### Spotify tracks have no visual preview

- **Observed:** Spotify playlist rows have no album-cover preview while selecting tracks or checking matches, and queued/downloading Spotify tracks show a generic download icon instead of the cover.
- **Expected:** Show Spotify's album artwork beside each track in the selection/match table and beside its queue item throughout download progress.
- **Fix direction:** Carry each track's safe Spotify cover URL through the listing, match, and job models; load it once with a lightweight cache and use a clear fallback only if the artwork is unavailable.

### Spotify track names need simple inline editing

- **Observed:** There is no convenient way to change an output name before downloading a Spotify track.
- **Expected:** Clicking a track's visible name opens a compact inline editor so the user can add or remove text, without replacing the normal row with a large full-width field.
- **Fix direction:** Make the title cell editable on click/F2 (or use a small popover editor), persist the chosen output name in that track's queued job, and preserve original Spotify metadata tags.

### Obscure Spotify tracks can be matched to the wrong YouTube song

- **Observed:** Most Spotify-to-YouTube matches are correct, but less-popular songs sometimes resolve to a different recording.
- **Expected:** Low-confidence matches must be visibly flagged and require the user to confirm or choose another result before download; high-confidence matches may continue automatically.
- **Fix direction:** Strengthen ranking with exact artist/title/duration checks and verified/ISRC matches where available; reject mismatched versions (remix, live, sped-up, instrumental, etc.), expose alternatives in **Change…**, and block automatic download below a strict confidence threshold.

### Playlist queue cannot be stopped and cleaned up easily

- **Observed:** Pausing a playlist leaves many paused items in the queue. Cancelling is only available one item at a time, and **Clear finished** cannot remove paused or cancelled entries.
- **Expected:** Provide clear bulk controls: **Cancel all remaining**, **Remove selected**, and **Clear non-active items**. A user can stop a playlist midway and return to an empty usable queue in a few clicks.
- **Partial files:** Before removal, offer a simple choice to keep resumable partial files or discard them. Never remove completed downloads unless the user explicitly asks.
- **Fix direction:** Let bulk actions operate on the full selected/filtered set, terminate the active worker safely, remove paused/cancelled job records, and refresh queue totals and controls immediately.

### Queue layout does not adapt to window size or long text

- **Observed:** At narrower window widths, the queue uses a horizontal scrollbar and parts of the cards/actions are cut off. Long song titles make the fixed layout worse.
- **Expected:** The page and each queue card reflow or shrink gracefully at supported window sizes; primary controls remain visible and usable without horizontal scrolling.
- **Fix direction:** Replace rigid minimum widths with responsive layouts, let the title area stretch and elide/wrap long text, keep status/actions compact, and test normal, narrow, maximized, and high-DPI window sizes.
