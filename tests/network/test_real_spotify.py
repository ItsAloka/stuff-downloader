r"""The Spotify branch (plan §6.3, §M5) against the real Spotify web player and YouTube Music.

Excluded from the default run by pyproject's addopts (-m 'not network'). Run it with:

    .venv\Scripts\python.exe -m pytest -m network -q

One short track (Pitbull, "Global Warming", 85 s) through all three worker modes, exactly as the
app drives them: analyze lists its album, match picks a YouTube recording, and a download of that
match produces an MP3 whose tags and cover are Spotify's -- read back from the file with mutagen
in the spotdl runtime, not taken from the worker's report. A repeat is skipped by the archive.

spotDL reads Spotify by scraping its web player (the official API's shared credentials are over
quota; see engines/spotdl.py), so this is the test that notices when that path breaks.
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
def track() -> spotify.SpotifyTrack:
    data = run_job(f"https://open.spotify.com/track/{TRACK_ID}", spotify.analyze_options())
    listing = spotify.parse_listing(data)
    assert listing.kind == "track" and len(listing.tracks) == 1
    return listing.tracks[0]


def test_an_album_lists_all_its_tracks_in_one_pass():
    data = run_job(f"https://open.spotify.com/album/{ALBUM_ID}", spotify.analyze_options())
    listing = spotify.parse_listing(data)
    assert listing.kind == "album" and listing.title == "Global Warming"
    assert listing.owner == "Pitbull"
    assert len(listing.tracks) >= 10
    assert TRACK_ID in {t.track_id for t in listing.tracks}


def test_the_match_is_close_to_spotifys_duration(track):
    data = run_job(track.url, spotify.match_options())
    match = spotify.parse_match(data, track)
    assert match is not None, data
    # A wrong recording (a remix, a live cut, a one-hour loop) shows up here first.
    assert match.duration_diff is not None and abs(match.duration_diff) <= 15, match
    assert match.confidence is None or match.confidence >= 50, match


def test_a_download_is_tagged_with_spotifys_metadata_and_a_repeat_is_skipped(track, tmp_path):
    data = run_job(track.url, spotify.download_options(), out=str(tmp_path))
    (path,) = [Path(p) for p in data["files"]]
    assert path.parent == tmp_path and path.suffix == ".mp3"
    assert path.name == "Pitbull, Sensato - Global Warming (feat. Sensato).mp3"

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
    tags = json.loads(read.stdout)
    assert tags["TIT2"] == ["Global Warming (feat. Sensato)"]
    assert tags["TPE1"] == ["Pitbull, Sensato"]
    assert tags["TALB"] == ["Global Warming"]
    assert tags["TRCK"][0].startswith("1/")
    assert len(tags["covers"]) == 1 and tags["covers"][0] > 10_000  # Spotify's, and only it

    again = run_job(track.url, spotify.download_options(), out=str(tmp_path))
    assert again.get("skipped") is True, again
    assert sorted(p.name for p in tmp_path.glob("*.mp3")) == [path.name]
