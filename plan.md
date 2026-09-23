# Stuff Downloader — Change Plan

## Converters

- [x] Add a **Converters** tab to the sidebar.
- [x] **Image:** convert local images to JPEG, PNG, WebP, GIF, or BMP; preserve the source, report progress and completion/errors, and open the output folder. Animated inputs keep their first frame.
- [x] **Video:** select a local video file, choose MP4, MKV, AVI, MOV, WebM, or MPEG/MPG, then convert. Example: MPEG/MPG → MP4. The source is preserved, existing outputs are not overwritten, and temporary files are cleaned after errors or cancellation.
- [x] **Audio:** select a local audio file, choose MP3, M4A, AAC, Opus, WAV, or FLAC, then convert.
- [x] Apply the source, destination, progress, completion/error, cancellation, and **Open folder** workflow to video conversion.
- [x] Apply the same workflow to audio conversion.
- [x] Use the bundled FFmpeg tool for video conversion.
- [x] Use the bundled FFmpeg tool for audio conversion.

## Completed DevTeam work

- [x] **History:** select multiple rows and remove them in one action. Removal deletes only history records; downloaded media files remain untouched. Selection follows records still visible under the active filters, and the empty state appears when no records remain. Focused GUI and store tests pass (61 passed); independent DevTeam review is still pending.
- [x] **Local video conversion:** added MP4, MKV, AVI, MOV, WebM, and MPEG/MPG output to the Converters tab. Bundled-FFmpeg worker and GUI tests passed (45 converter regression tests); DevTeam completed a read-only self-review because only one agent was present.
- [x] **Local audio conversion:** added MP3, M4A, AAC, Opus, WAV, and FLAC output with source preservation, collision-safe names, and temporary cleanup. All 33 converter tests and Ruff pass; DevTeam self-review is complete and the task is accepted.

## Next fixes (items 1 and 2)

The next work session should focus on these two issues, reported from the current app screenshots:

- [ ] **1. Playlist selection blue tick:** Fix the clipped/misrendered blue selection checkmark shown in the Spotify playlist table. Verify it at supported window sizes and DPI scales.
- [ ] **2. Spotify artwork and title editing:** Make Spotify album art load beside each song in the listing and queue. Let the user edit the original title directly in the **Title** column and use that edited title for the output name; remove the separate **File name** field. Keep Spotify artist, album, and other metadata tags intact.

## YouTube music output options — implemented

- [x] **Original audio / Opus** is the recommended default; it preserves the downloaded stream without re-encoding.
- [x] **M4A/AAC**, **MP3**, **FLAC**, and **WAV** output options are available. The UI explains that converting the usually lossy YouTube source to FLAC/WAV makes a larger file without improving sound quality.

## Confirmed working

- YouTube playlists, single songs, and videos are working correctly.
- Direct image links are recognized, previewed, and downloaded correctly.
- Spotify-to-YouTube matching flags uncertain results for review. Metadata cannot prove that same-title, similar-length recordings are identical; accuracy work remains.

## UI issues and improvements

### Problem 1: Playlist selection blue tick is clipped or missing

- **Observed:** In the playlist results table, the blue selection tick at the far left of each row is cut off, so only part of the icon is visible.
- **Expected:** Display the entire tick/icon cleanly at every supported window size and DPI scale.
- **Fix direction:** Check the Spotify listing table's checkbox delegate, column width, padding, and stylesheet. Ensure the full blue tick is visible and aligned at normal, narrow, maximized, and high-DPI sizes. Add a focused GUI regression check.

### Playlist titles cannot be copied

- **Observed:** A user can drag across a playlist title but cannot copy the selected text.
- **Expected:** Titles (and other useful table text such as artist and file name) can be selected and copied with `Ctrl+C`; expose a right-click **Copy** option as well.
- **Fix direction:** Use copy-enabled text cells/delegates or implement the table's copy-to-clipboard action while preserving row selection and inline editing.

### History removal ignores multi-selection — completed

- **Done:** **Remove from history** now removes all selected visible rows. The store deletes history rows and file references without touching downloaded media. Selection is retained for still-visible records after refresh.
- **Verification:** Focused History GUI and store tests pass (61 passed); targeted Ruff checks pass. Independent DevTeam review remains pending.

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

### Problem 2: Spotify artwork does not load and title editing uses a separate file-name field

- **Observed:** Spotify song artwork is not appearing in the playlist view. Renaming also uses an extra **File name** field instead of editing the title in place.
- **Expected:** Load Spotify album artwork beside each song and in its queue card. Edit the original title directly in the **Title** column; use the edited title as the output filename and keep the Spotify metadata tags unchanged. Remove the separate **File name** field.
- **Fix direction:** Trace the cover URL from Spotify listing through the GUI and image loader, including failures and cache keys. Replace the extra name editor with direct Title-column editing, and verify the edited value reaches the output filename without changing artist/album tags.

### Spotify track names need direct title editing — revised workflow still needed

- **Observed:** There is no convenient way to change an output name before downloading a Spotify track.
- **Expected:** Edit the original title in the **Title** column itself, with no separate **File name** field. Use the edited title for the output name and preserve Spotify artist, album, and other metadata tags.
- **Fix direction:** Move the editor into the Title cell and remove the extra filename editor. Verify edits persist in the queued job and affect the output filename without changing Spotify tags.

### Obscure Spotify tracks can be matched to the wrong YouTube song — confirmation safeguard implemented; accuracy remains

- **Observed:** Most Spotify-to-YouTube matches are correct, but less-popular songs sometimes resolve to a different recording.
- **Expected:** Low-confidence matches are visibly flagged and require confirmation or another selection before the GUI queues a download; confident matches may continue automatically.
- **Done:** The GUI and batch job builder gate uncertain matches, and alternatives are available through **Change…**.
- **Still unresolved:** A wrong YouTube upload with the same title and similar duration can still look confident. The Spotify free client supplies no verified ISRC.
- **Next accuracy work:** Build a labeled set of obscure tracks and correct/incorrect recordings, measure false automatic matches, then tune the threshold and add same-title/same-length regression cases. Use verified identifiers only when trustworthy IDs are available on both sides.

### Playlist queue cannot be stopped and cleaned up easily

- **Observed:** Pausing a playlist leaves many paused items in the queue. Cancelling is only available one item at a time, and **Clear finished** cannot remove paused or cancelled entries.
- **Expected:** Provide clear bulk controls: **Cancel all remaining**, **Remove selected**, and **Clear non-active items**. A user can stop a playlist midway and return to an empty usable queue in a few clicks.
- **Partial files:** Before removal, offer a simple choice to keep resumable partial files or discard them. Never remove completed downloads unless the user explicitly asks.
- **Fix direction:** Let bulk actions operate on the full selected/filtered set, terminate the active worker safely, remove paused/cancelled job records, and refresh queue totals and controls immediately.

### Queue layout does not adapt to window size or long text

- **Observed:** At narrower window widths, the queue uses a horizontal scrollbar and parts of the cards/actions are cut off. Long song titles make the fixed layout worse.
- **Expected:** The page and each queue card reflow or shrink gracefully at supported window sizes; primary controls remain visible and usable without horizontal scrolling.
- **Fix direction:** Replace rigid minimum widths with responsive layouts, let the title area stretch and elide/wrap long text, keep status/actions compact, and test normal, narrow, maximized, and high-DPI window sizes.
