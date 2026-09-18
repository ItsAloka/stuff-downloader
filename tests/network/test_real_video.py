r"""The real-network verification the video branch never had.

``video_1080`` is ``DEFAULT_PRESET_ID``, and until this file existed every video download the
app can produce was unproven: resolution selection, the compatible-MP4 vs MKV choice and the
ffmpeg merge had only ever run against stubs. All six earlier network tests are the MP3 path.

Excluded from the default run by pyproject's addopts (-m 'not network'). Run it with:

    .venv\Scripts\python.exe -m pytest -m network -q

One short Creative Commons video (Blender's "Caminandes 3: Llamigos", CC-BY, 2m30s) is
downloaded three times -- capped compatible, capped incompatible, and uncapped -- because those
three are the distinct code paths, not because the bytes are interesting. Everything runs
through the app's own path (core.presets.download_options and core.runner.JobRun driving the
real worker process), so what passes here is what the GUI does.

Every assertion about the produced media is made against ffprobe reading the finished file.
None of them trusts yt-dlp's own reporting: the question these tests exist to answer is what
actually landed on disk.

Nothing about a network test is guaranteed: YouTube can rate-limit, change an extractor's
output or take the video down. A failure here is information, not necessarily a regression in
this project; read the reason before believing it.
"""

from __future__ import annotations

from pathlib import Path

import netjob
import pytest
from netjob import ffprobe

from stuff_downloader.core import formats, presets
from stuff_downloader.core.protocol import JobSpec

pytestmark = pytest.mark.network

# Resolved at import time, on purpose -- see the note in netjob.worker_command.
WORKER_COMMAND = netjob.require_engine_runtime()

# Blender Foundation, "Caminandes 3: Llamigos", Creative Commons BY. Chosen because it is
# short (150s) and still carries a full resolution ladder up to 1080p -- a video that only
# offered one height could not tell a working cap from a broken one.
VIDEO_URL = "https://www.youtube.com/watch?v=SkVqJ1SGeL0"
CAP = 720
JOB_TIMEOUT = 600.0

# Blender's "Big Buck Bunny 60fps 4K", CC-BY. Only ever analyzed here, never downloaded -- it is
# 10 minutes long and the point is its format ladder, not its bytes. It is the counter-example
# the short video cannot be: it publishes 2160p, but no avc1 above 1080p.
FOURK_URL = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"


def run_job(spec: JobSpec):
    """One real job, with transient provider refusals retried then skipped. See netjob."""
    return netjob.run_job(spec, WORKER_COMMAND, JOB_TIMEOUT)


def streams(probe: dict, kind: str) -> list[dict]:
    return [s for s in probe["streams"] if s.get("codec_type") == kind]


def download(out: Path, preset_id: str, compatible: bool) -> tuple[Path, dict]:
    """One real video download through the app's own path. Returns (file, ffprobe output)."""
    spec = JobSpec(
        job_id=f"netvid-{preset_id}-{'mp4' if compatible else 'mkv'}",
        engine="ytdlp",
        url=VIDEO_URL,
        output_dir=str(out),
        options=presets.download_options(preset_id, compatible=compatible),
    )
    terminal, _ = run_job(spec)
    produced = [p for p in out.rglob("*") if p.is_file()]
    # (d) is partly this: the merge must leave ONE file behind, not a video and an audio file.
    assert len(produced) == 1, [p.name for p in produced]
    return produced[0], ffprobe(produced[0])


@pytest.fixture(scope="module")
def available_heights() -> list[int]:
    """The resolution ladder this video really offers, read the way the app reads it.

    The cap assertions are written against this rather than against a hard-coded 720, so the
    test states what it actually proved even if YouTube changes what it offers.
    """
    spec = JobSpec(
        job_id="netvid-analyze",
        engine="ytdlp",
        url=VIDEO_URL,
        output_dir=".",
        options=presets.analyze_options(),
    )
    terminal, _ = run_job(spec)
    choices = formats.resolution_choices(terminal.data.get("formats") or [])
    heights = sorted({c.height for c in choices}, reverse=True)
    assert heights, "analyze returned no video formats"
    return heights


@pytest.fixture(scope="module")
def capped_mp4(tmp_path_factory) -> tuple[Path, dict]:
    """video_720, compatible=True -- the capped MP4 path."""
    return download(tmp_path_factory.mktemp("capped_mp4"), "video_720", compatible=True)


@pytest.fixture(scope="module")
def capped_mkv(tmp_path_factory) -> tuple[Path, dict]:
    """video_720, compatible=False -- the same cap, the other container."""
    return download(tmp_path_factory.mktemp("capped_mkv"), "video_720", compatible=False)


@pytest.fixture(scope="module")
def uncapped(tmp_path_factory) -> tuple[Path, dict]:
    """video_best -- no cap at all."""
    return download(tmp_path_factory.mktemp("uncapped"), "video_best", compatible=True)


def test_the_test_video_can_tell_a_working_cap_from_a_broken_one(available_heights):
    """Guard on the fixture itself: without a ladder around the cap, (a) proves nothing.

    If this video ever stops offering both a height at/below the cap and a higher one, the
    cap assertions below would pass on a single available format and mean nothing. Fail here
    with a clear reason instead of passing vacuously there.
    """
    at_or_below = [h for h in available_heights if h <= CAP]
    above = [h for h in available_heights if h > CAP]
    assert len(at_or_below) > 1, f"need >1 height at or below {CAP}p, got {available_heights}"
    assert above, f"need a height above {CAP}p to prove the cap does something: {available_heights}"


def test_a_capped_preset_picks_the_highest_bucket_under_the_cap(capped_mp4, available_heights):
    """(a) Not merely "something small" -- the best height the cap allows.

    A cap that silently fell back to 144p, or that ignored the ladder and took whatever came
    first, passes `height <= 720` and fails this.
    """
    path, probe = capped_mp4
    expected = max(h for h in available_heights if h <= CAP)
    (video,) = streams(probe, "video")
    height = int(video["height"])

    assert height <= CAP, f"{height}p exceeds the {CAP}p cap ({path.name})"
    assert height == expected, (
        f"got {height}p but {expected}p was available at or under the {CAP}p cap; "
        f"ladder was {available_heights}"
    )


def test_compatible_true_produces_a_real_mp4_of_avc1_and_mp4a(capped_mp4):
    """(b) The container and both codecs, from the file -- not from the format selector string."""
    path, probe = capped_mp4
    (video,) = streams(probe, "video")
    (audio,) = streams(probe, "audio")

    assert path.suffix == ".mp4", path.name
    assert "mp4" in probe["format"]["format_name"], probe["format"]["format_name"]
    assert video["codec_name"] == "h264", video["codec_name"]
    assert video.get("codec_tag_string", "").startswith("avc1"), video
    assert audio["codec_name"] == "aac", audio["codec_name"]
    assert audio.get("codec_tag_string", "").startswith("mp4a"), audio


def test_compatible_false_produces_an_mkv(capped_mkv):
    """(c) The other branch of merge_output_format."""
    path, probe = capped_mkv
    assert path.suffix == ".mkv", path.name
    assert "matroska" in probe["format"]["format_name"], probe["format"]["format_name"]


@pytest.mark.parametrize("fixture_name", ["capped_mp4", "capped_mkv", "uncapped"])
def test_the_merge_produced_one_file_with_both_a_video_and_an_audio_stream(fixture_name, request):
    """(d) The ffmpeg merge path, on every variant.

    A silent video is the failure this catches: it has a plausible size, plays, and is wrong.
    Asserting on the audio stream's existence is the only thing that notices.
    """
    path, probe = request.getfixturevalue(fixture_name)
    video = streams(probe, "video")
    audio = streams(probe, "audio")

    assert len(video) == 1, f"{len(video)} video streams in {path.name}"
    assert len(audio) == 1, f"{path.name} has no audio stream -- the merge dropped it"
    assert float(audio[0].get("duration") or probe["format"]["duration"]) > 0
    assert int(probe["format"]["size"]) > 0


@pytest.fixture(scope="module")
def fourk_formats() -> list[dict]:
    """The 4K video's format list. Analyze only -- nothing is downloaded."""
    spec = JobSpec(
        job_id="netvid-analyze-4k",
        engine="ytdlp",
        url=FOURK_URL,
        output_dir=".",
        options=presets.analyze_options(),
    )
    terminal, _ = run_job(spec)
    fmts = terminal.data.get("formats") or []
    assert fmts, "analyze returned no formats"
    return fmts


def test_best_is_bounded_by_the_compatibility_toggle(fourk_formats):
    """The false promise "Best" used to make, pinned so it cannot go quiet again.

    video_best has no height cap, so it reads as "whatever the site has". It is not: with
    compatible=True (the default) the selector is
    ``bv*[vcodec^=avc1]+ba[acodec^=mp4a]/...``, and YouTube publishes no avc1 above 1080p. So
    "Best" silently stops at 1080p on a 4K video unless the user turns compatibility off.

    Verified live on this video at the time of writing: compatible=True picked format 299
    (1080p avc1); compatible=False picked 401 (2160p AV1).

    This asserts the condition that makes the trap possible rather than re-downloading 4K. If
    YouTube ever ships avc1 above 1080p this fails, which is the correct moment to revisit both
    the preset description and whether the toggle should still bound "Best".
    """
    heights = [f.get("height") for f in fourk_formats if f.get("height")]
    avc1 = [
        f.get("height")
        for f in fourk_formats
        if f.get("height") and str(f.get("vcodec") or "").startswith("avc1")
    ]
    assert heights and avc1

    assert max(heights) > 1080, f"expected a 4K ladder, got max {max(heights)}p"
    assert max(avc1) == 1080, (
        f"avc1 now reaches {max(avc1)}p; the compatibility ceiling has moved and "
        "presets.video_best's description needs revisiting"
    )
    # Which is to say: with compatibility on, "Best" cannot exceed this, well below what exists.
    assert max(avc1) < max(heights)


def test_video_best_is_not_capped(uncapped, available_heights, capped_mp4):
    """(e) No cap means the top of the ladder, and visibly more than the capped run.

    Scope, stated plainly: this video tops out at 1080p, which is also the avc1 ceiling, so it
    proves video_best applies no height cap of its own -- not that video_best reaches 4K. It
    does not, with compatibility on; that is
    test_best_is_bounded_by_the_compatibility_toggle's job.
    """
    path, probe = uncapped
    (video,) = streams(probe, "video")
    height = int(video["height"])
    _, capped_probe = capped_mp4

    assert height == available_heights[0], (
        f"video_best gave {height}p but {available_heights[0]}p was available "
        f"(ladder {available_heights})"
    )
    assert height > CAP, f"video_best gave {height}p, no better than the {CAP}p cap"
    assert height > int(streams(capped_probe, "video")[0]["height"])
