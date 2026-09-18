r"""The real-network verification M2 never did: a genuine YT Music playlist, end to end.

Excluded from the default run by pyproject's addopts (-m 'not network'). Run it with:

    .venv\Scripts\python.exe -m pytest -m network -q

These tests download from YouTube. They use a Creative Commons album (Kevin MacLeod,
CC-BY) and take two short tracks, deliberately: the point is to prove the pipeline, not to
move bytes. Everything runs through the app's own path — core.playlist.batch_specs and
core.runner.JobRun driving the real worker process — so what passes here is what the GUI does.

Nothing about a network test is guaranteed: YouTube can rate-limit, change an extractor's
output or take the album down. A failure here is information, not necessarily a regression in
this project; read the reason before believing it.
"""

from __future__ import annotations

from pathlib import Path

import netjob
import pytest
from netjob import ffprobe

from stuff_downloader.core import playlist, presets
from stuff_downloader.core.protocol import JobSpec

pytestmark = pytest.mark.network

# Resolved at import time, on purpose -- see the note in netjob.worker_command.
WORKER_COMMAND = netjob.require_engine_runtime()

# Kevin MacLeod — "25 Years, Vol. 1", Creative Commons BY. Real album, real track numbers,
# real cover art, and short tracks.
ALBUM_URL = "https://music.youtube.com/playlist?list=OLAK5uy_nNSGSaK1TYrKSRYWMF6yi7rGgOMFpRO0Y"
TRACKS = 2
JOB_TIMEOUT = 300.0


def run_job(spec: JobSpec):
    """One real job, with transient provider refusals retried then skipped. See netjob."""
    return netjob.run_job(spec, WORKER_COMMAND, JOB_TIMEOUT)


@pytest.fixture(scope="module")
def listing():
    """The real playlist, read the way the app reads it."""
    spec = JobSpec(
        job_id="netlist",
        engine="ytdlp",
        url=ALBUM_URL,
        output_dir=".",
        options=presets.playlist_options(),
    )
    terminal, _ = run_job(spec)
    assert terminal.type == "result", f"listing failed: {terminal.data}"
    result = playlist.parse_listing(terminal.data)
    assert result.entries, "the album returned no entries"
    return result


@pytest.fixture(scope="module")
def downloaded(listing, tmp_path_factory):
    """Two tracks, downloaded once, shared by the tests that inspect them."""
    out = tmp_path_factory.mktemp("album")
    entries = [e for e in listing.entries if not e.unavailable][:TRACKS]
    assert len(entries) == TRACKS, "not enough available entries to test with"
    specs = playlist.batch_specs(listing, entries, str(out), "mp3_music", archive=True)
    results = []
    for spec in specs:
        terminal, _ = run_job(spec)
        assert terminal.type == "result", f"download failed: {terminal.data}"
        assert not terminal.data.get("skipped"), "first run must not skip"
        results.append(terminal.data)
    return out, entries, specs, results


def test_the_playlist_reads_as_an_album(listing):
    assert listing.title
    assert len(listing.entries) > TRACKS
    for entry in listing.entries[:TRACKS]:
        assert entry.title and entry.url.startswith("https://")


def test_every_track_produced_one_mp3(downloaded):
    out, entries, _, results = downloaded
    mp3s = sorted(out.rglob("*.mp3"))
    assert len(mp3s) == TRACKS, [p.name for p in mp3s]
    for result in results:
        files = result.get("files") or []
        assert files and Path(files[0]).is_file()
        assert result.get("total_bytes", 0) > 0


def test_the_mp3s_carry_album_track_numbers_and_a_square_cover(downloaded, listing):
    out, entries, specs, _ = downloaded
    mp3s = sorted(out.rglob("*.mp3"))
    seen_tracks = set()
    for path in mp3s:
        probe = ffprobe(path)
        tags = {k.lower(): v for k, v in (probe["format"].get("tags") or {}).items()}

        assert tags.get("album") == listing.title, tags
        track = str(tags.get("track", "")).split("/")[0]
        assert track.isdigit(), f"no track number in {tags}"
        seen_tracks.add(int(track))
        assert tags.get("title"), tags

        covers = [s for s in probe["streams"] if s.get("codec_type") == "video"]
        assert covers, f"no embedded cover art in {path.name}"
        cover = covers[0]
        assert cover["width"] == cover["height"], (
            f"cover art is {cover['width']}x{cover['height']}, not square"
        )

    # The numbers are the playlist positions the app asked for, not whatever the site felt like.
    assert seen_tracks == {spec.options["playlist_index"] for spec in specs}


def test_the_archive_file_records_what_was_downloaded(downloaded):
    from stuff_downloader_worker.presets import ARCHIVE_FILENAME

    out, entries, _, _ = downloaded
    # The worker puts a playlist job in its own folder, so the archive sits beside the tracks.
    archives = list(out.rglob(ARCHIVE_FILENAME))
    assert len(archives) == 1, [str(a) for a in archives]
    archive = archives[0]
    assert archive.parent == sorted(out.rglob("*.mp3"))[0].parent
    recorded = archive.read_text(encoding="utf-8")
    for entry in entries:
        assert entry.video_id in recorded, recorded


def test_a_second_run_is_skipped_by_the_real_download_archive(downloaded):
    """The archive branch, proven against a real yt-dlp rather than a stub.

    This was an xfail(strict) plus a test pinning the wrong behaviour: extraction ran with
    download_archive set, so yt-dlp returned None for an archived id and the worker reported
    a failed download. Both went when the fix landed; this is what is left.
    """
    out, _, specs, _ = downloaded
    before = sorted(p.name for p in out.rglob("*.mp3"))
    terminal, _ = run_job(specs[0])

    assert terminal.type == "result", terminal.data
    assert terminal.data.get("skipped") is True, terminal.data
    assert terminal.data.get("skipped_reason") == "Already downloaded"
    assert terminal.data.get("files") == []
    assert terminal.data.get("total_bytes") == 0
    # It skipped instead of downloading again, rather than downloading and claiming a skip.
    assert sorted(p.name for p in out.rglob("*.mp3")) == before


def test_an_entry_not_in_the_archive_still_downloads(downloaded, listing, tmp_path):
    """The archive must skip what it has seen, not everything."""
    entries = [e for e in listing.entries if not e.unavailable][TRACKS : TRACKS + 1]
    assert entries, "no spare entry to test with"
    (spec,) = playlist.batch_specs(listing, entries, str(tmp_path), "mp3_music", archive=True)
    terminal, _ = run_job(spec)

    assert terminal.type == "result", terminal.data
    assert not terminal.data.get("skipped"), terminal.data
    assert list(tmp_path.rglob("*.mp3"))
