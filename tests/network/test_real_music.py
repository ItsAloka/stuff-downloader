r"""Apple Music and Deezer (plan §7 "Other music sites") against the real public lookups.

Excluded from the default run by pyproject's addopts (-m 'not network'). Run it with:

    .venv\Scripts\python.exe -m pytest -m network -q

The iTunes Lookup API and the Deezer API need no account and no key, so this is the test that
notices when either changes shape. Each link is analyzed and its first song matched on YouTube
Music, exactly as the app drives the music engine. Downloads are covered offline in
tests/unit/test_music_engine.py and live by the Spotify network test, which shares the code.
"""

from __future__ import annotations

import sys
from pathlib import Path

import netjob
import pytest

from stuff_downloader.core import router, spotify
from stuff_downloader.core.protocol import JobSpec
from stuff_downloader.core.runner import default_worker_command

pytestmark = pytest.mark.network

# Resolved at import time, before conftest redirects LOCALAPPDATA (see netjob.worker_command).
WORKER_COMMAND = default_worker_command("music")
if Path(WORKER_COMMAND[0]).resolve() == Path(sys.executable).resolve():
    pytest.skip(
        "no music (or spotDL) engine runtime is installed; build it before running network tests",
        allow_module_level=True,
    )

JOB_TIMEOUT = 300.0
APPLE_ALBUM = "https://music.apple.com/us/album/in-between-dreams/1440857781"
APPLE_SONG = "https://music.apple.com/us/album/better-together/1440857781?i=1440857786"
DEEZER_ALBUM = "https://www.deezer.com/fr/album/302127"
DEEZER_TRACK = "https://www.deezer.com/track/3135556"


def run_job(url: str, options: dict):
    spec = JobSpec(job_id="netmusic", engine="music", url=url, output_dir=".", options=options)
    terminal, _ = netjob.run_job(spec, WORKER_COMMAND, JOB_TIMEOUT)
    return terminal.data


def listing(link: str) -> spotify.SpotifyListing:
    route = router.route(link)
    assert route.kind == "catalog" and route.engine == "music"
    return spotify.parse_listing(run_job(route.url, spotify.analyze_options()))


@pytest.mark.parametrize(
    ("link", "service", "kind", "minimum"),
    [
        (APPLE_ALBUM, "apple", "album", 10),
        (APPLE_SONG, "apple", "track", 1),
        (DEEZER_ALBUM, "deezer", "album", 10),
        (DEEZER_TRACK, "deezer", "track", 1),
    ],
)
def test_a_link_lists_its_songs_with_the_services_own_art(link, service, kind, minimum):
    found = listing(link)
    assert (found.service, found.kind) == (service, kind)
    assert len(found.tracks) >= minimum
    assert all(t.art and spotify.image_url(t.art) == t.art for t in found.tracks)
    assert all(t.duration and t.artists for t in found.tracks)
    assert found.cover


@pytest.mark.parametrize("link", [APPLE_SONG, DEEZER_TRACK])
def test_a_well_known_song_matches_certainly_on_youtube_music(link):
    track = listing(link).tracks[0]
    spec = spotify.match_spec(track)
    match = spotify.parse_match(run_job(spec.url, spec.options), track)
    assert match is not None
    assert match.method in ("album", "song")
    assert not spotify.is_uncertain(match)
