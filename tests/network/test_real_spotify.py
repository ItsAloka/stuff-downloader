r"""The Spotify branch (plan §7) against Spotify's real embed pages and YouTube Music.

Excluded from the default run by pyproject's addopts (-m 'not network'). Run it with:

    .venv\Scripts\python.exe -m pytest -m network -q

One short track (Pitbull, "Global Warming", 85 s) through all three worker modes, exactly as the
app drives them from the album link: analyze lists the album with its cover, match finds the
recording on the YouTube Music album, and a download of that match produces an MP3 whose tags and
cover are Spotify's -- read back from the file with mutagen in the spotdl runtime, not taken from
the worker's report. A repeat is skipped by the archive, and an edited title names the file only.

The listing reads Spotify's public embed page and the art comes from oEmbed; neither needs an
account, so this is the test that notices when either changes shape.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import netjob
import pytest

from stuff_downloader.core import spotify
from stuff_downloader.core.protocol import JobSpec
from stuff_downloader.core.runner import default_worker_command

pytestmark = pytest.mark.network

# Resolved at import time, before conftest redirects LOCALAPPDATA (see netjob.worker_command).
WORKER_COMMAND = default_worker_command("spotdl")
if Path(WORKER_COMMAND[0]).resolve() == Path(sys.executable).resolve():
    pytest.skip(
        "the spotDL engine runtime is not installed; build it before running network tests",
        allow_module_level=True,
    )

TRACK_ID = "6OmhkSOpvYBokMKQxpIGx2"
ALBUM_ID = "4aawyAB9vmqN3uQ7FjRGTy"
JOB_TIMEOUT = 300.0


def run_job(url: str, options: dict, out: str = "."):
    spec = JobSpec(job_id="netspot", engine="spotdl", url=url, output_dir=out, options=options)
    terminal, _ = netjob.run_job(spec, WORKER_COMMAND, JOB_TIMEOUT)
    return terminal.data


@pytest.fixture(scope="module")
def album() -> spotify.SpotifyListing:
    data = run_job(f"https://open.spotify.com/album/{ALBUM_ID}", spotify.analyze_options())
    return spotify.parse_listing(data)


@pytest.fixture(scope="module")
def track(album) -> spotify.SpotifyTrack:
    return next(t for t in album.tracks if t.track_id == TRACK_ID)


def test_an_album_lists_all_its_tracks_with_spotifys_cover(album):
    assert album.kind == "album" and album.title == "Global Warming"
    assert album.owner == "Pitbull"
    assert len(album.tracks) >= 10
    assert album.cover.startswith("https://i.scdn.co/image/")
    assert {t.art for t in album.tracks} == {album.cover}
    assert {t.album for t in album.tracks} == {"Global Warming"}


def test_a_single_track_lists_with_its_own_art():
    data = run_job(f"https://open.spotify.com/track/{TRACK_ID}", spotify.analyze_options())
    listing = spotify.parse_listing(data)
    assert listing.kind == "track" and len(listing.tracks) == 1
    assert listing.tracks[0].art.startswith("https://i.scdn.co/image/")


def test_a_rows_oembed_lookup_resolves_to_a_real_picture(qtbot, monkeypatch):
    from stuff_downloader.gui import thumbs

    monkeypatch.undo()  # conftest's no-network guard on thumbs.fetch

    data = thumbs.fetch(spotify.oembed_url(TRACK_ID))
    image = thumbs.decode_image(data)
    assert image is not None and image.width() >= 64


def test_the_match_is_found_on_the_album_and_is_certain(track):
    data = run_job(track.url, spotify.match_spec(track).options)
    match = spotify.parse_match(data, track)
    assert match is not None, data
    # A wrong recording (a remix, a live cut, a one-hour loop) shows up here first.
    assert match.duration_diff is not None and abs(match.duration_diff) <= 3, match
    assert match.method in ("album", "song") and not spotify.is_uncertain(match), match


def _tags(path: Path) -> dict:
    read = subprocess.run(  # noqa: S603 - fixed interpreter and script
        [
            WORKER_COMMAND[0],
            "-s",
            "-c",
            "import json,sys; from mutagen.id3 import ID3; t=ID3(sys.argv[1]);"
            "print(json.dumps({k: [str(x) for x in t[k].text] for k in "
            "('TIT2','TPE1','TALB','TRCK') if k in t} | "
            "{'covers': [len(a.data) for a in t.getall('APIC')]}))",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env={"SYSTEMROOT": os.environ.get("SYSTEMROOT", "")},
    )
    assert read.returncode == 0, read.stderr
    return json.loads(read.stdout)


def _download(track, out: Path, **extra):
    options = spotify.download_options(
        archive=extra.pop("archive", True), album=track.album, album_track=track.index, **extra
    )
    return run_job(track.url, options, out=str(out))


def test_a_download_is_tagged_with_spotifys_metadata_and_a_repeat_is_skipped(track, tmp_path):
    data = _download(track, tmp_path)
    (path,) = [Path(p) for p in data["files"]]
    assert path.parent == tmp_path and path.suffix == ".mp3"
    assert path.name == "Pitbull, Sensato - Global Warming (feat. Sensato).mp3"

    tags = _tags(path)
    assert tags["TIT2"] == ["Global Warming (feat. Sensato)"]
    assert tags["TPE1"] == ["Pitbull, Sensato"]
    assert tags["TALB"] == ["Global Warming"]
    assert tags["TRCK"] == [str(track.index)]
    assert len(tags["covers"]) == 1 and tags["covers"][0] > 10_000  # Spotify's, and only it

    again = _download(track, tmp_path)
    assert again.get("skipped") is True, again
    assert sorted(p.name for p in tmp_path.glob("*.mp3")) == [path.name]


def test_an_edited_title_names_the_file_and_the_tags_stay_spotifys(track, tmp_path):
    """The R5 carry-over, live: the owner's edit is the file name, never the tags (§5.5)."""
    data = _download(track, tmp_path, archive=False, edited_title="My edited name")
    (path,) = [Path(p) for p in data["files"]]
    assert path.name == "My edited name.mp3"
    tags = _tags(path)
    assert tags["TIT2"] == ["Global Warming (feat. Sensato)"]
    assert tags["TPE1"] == ["Pitbull, Sensato"] and tags["TALB"] == ["Global Warming"]
    assert len(tags["covers"]) == 1 and tags["covers"][0] > 10_000
