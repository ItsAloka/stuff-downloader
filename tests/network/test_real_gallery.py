r"""The gallery branch (plan §M4) against a real site, through the app's own path.

Excluded from the default run by pyproject's addopts (-m 'not network'). Run it with:

    .venv\Scripts\python.exe -m pytest -m network -q

One public NASA post on X with two photos. X is used because, at the time of writing, it is the
one gallery site gallery-dl can still read anonymously: Instagram redirects anonymous requests
to its login page and TikTok answers 403. Both were checked live; neither is a defect here.

What this proves, from the files on disk rather than from the worker's report: analyze lists
both photos, a download of only the second photo produces exactly that original, and a repeat
of the same selection with the archive on downloads nothing.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import netjob
import pytest

from stuff_downloader.core import gallery, presets
from stuff_downloader.core.protocol import JobSpec
from stuff_downloader.core.runner import default_worker_command

pytestmark = pytest.mark.network

# Resolved at import time, before conftest redirects LOCALAPPDATA (see netjob.worker_command).
WORKER_COMMAND = default_worker_command("gallerydl")
if Path(WORKER_COMMAND[0]).resolve() == Path(sys.executable).resolve():
    pytest.skip(
        "the gallery-dl engine runtime is not installed; build it before running network tests",
        allow_module_level=True,
    )

# NASA on X: two Artemis II photos, 4096x2731 JPEGs. A US government work.
POST_URL = "https://x.com/NASA/status/2042714976767349088"
JOB_TIMEOUT = 300.0


def run_job(spec: JobSpec):
    return netjob.run_job(spec, WORKER_COMMAND, JOB_TIMEOUT)


def jpeg_size(path: Path) -> tuple[int, int]:
    """(width, height) from the JPEG's own SOF marker, not from anyone's metadata."""
    data = path.read_bytes()
    assert data[:2] == b"\xff\xd8", f"{path.name} is not a JPEG"
    i = 2
    while i < len(data):
        marker, length = data[i + 1], struct.unpack(">H", data[i + 2 : i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2):
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return width, height
        i += 2 + length
    raise AssertionError(f"no SOF marker in {path.name}")


def download(out: Path, items: list[int]):
    spec = JobSpec(
        job_id="netgal-download",
        engine="gallerydl",
        url=POST_URL,
        output_dir=str(out),
        options=presets.gallery_download_options(items, archive=True),
    )
    terminal, _ = run_job(spec)
    return terminal.data


def media(out: Path) -> list[Path]:
    return sorted(p for p in out.iterdir() if p.is_file() and not p.name.startswith("."))


def test_analyze_lists_both_photos_with_previews():
    spec = JobSpec(
        job_id="netgal-analyze",
        engine="gallerydl",
        url=POST_URL,
        output_dir=".",
        options={"mode": "analyze"},
    )
    terminal, _ = run_job(spec)
    parsed = gallery.parse(terminal.data)
    assert [item.index for item in parsed.items] == [1, 2]
    assert all(item.kind == "image" for item in parsed.items)
    assert all(item.preview for item in parsed.items), "the grid would show blank tiles"


def test_only_the_ticked_original_downloads_and_a_repeat_is_skipped(tmp_path):
    first = download(tmp_path, [2])
    files = media(tmp_path)
    assert [p.name for p in files] == [Path(f).name for f in first["files"]]
    assert len(files) == 1 and files[0].stem.endswith("_2"), [p.name for p in files]
    # The original, not a preview-sized rendition.
    assert jpeg_size(files[0]) == (4096, 2731)

    again = download(tmp_path, [2])
    assert again.get("skipped") is True, again
    assert media(tmp_path) == files
