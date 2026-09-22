"""Fetch, verify and stage the bundled command-line tools (plan §8, §8.3 F3). Stdlib only.

Every input is an official HTTPS URL pinned by SHA-256, and every staged file is pinned too:

    ffmpeg.exe, ffprobe.exe, licenses/FFmpeg-LICENSE.txt   from the gyan.dev FFmpeg essentials zip
    deno.exe                                               from the Deno release zip
    licenses/Deno-LICENSE.md                               from the Deno repository at that tag

Commands::

    python packaging/fetch_tools.py [--dest DIR] fetch     download, verify, stage into tools/
    python packaging/fetch_tools.py [--dest DIR] verify    check an existing tools/ folder

``fetch`` downloads into a temporary directory, checks each archive's hash, extracts only the
named members (never a whole archive, so no member path is ever trusted), checks each extracted
file's hash, and only then moves the files into place: a failed download, hash or extraction
leaves ``--dest`` as it was. ``verify`` is what the PyInstaller spec calls: a build receives only
files that match their pins. Nothing here uploads or publishes anything.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_DEST = PROJECT / "tools"

FFMPEG_DIR = "ffmpeg-9.0.1-essentials_build"


@dataclass(frozen=True)
class Source:
    """One pinned download: archive ``members`` -> staged paths, or the whole file -> ``target``."""

    label: str
    url: str
    sha256: str
    members: dict[str, str] = field(default_factory=dict)
    target: str = ""


SOURCES: tuple[Source, ...] = (
    Source(
        label="FFmpeg 9.0.1 essentials (gyan.dev build)",
        url="https://github.com/GyanD/codexffmpeg/releases/download/9.0.1/ffmpeg-9.0.1-essentials_build.zip",
        sha256="fec81ae03971d9dd4be3ebe02e263bd2ec1d789483f931bdba5f5715e65da2e9",
        members={
            f"{FFMPEG_DIR}/bin/ffmpeg.exe": "ffmpeg.exe",
            f"{FFMPEG_DIR}/bin/ffprobe.exe": "ffprobe.exe",
            f"{FFMPEG_DIR}/LICENSE": "licenses/FFmpeg-LICENSE.txt",
        },
    ),
    Source(
        label="Deno v2.9.6 x86_64-pc-windows-msvc",
        url="https://github.com/denoland/deno/releases/download/v2.9.6/deno-x86_64-pc-windows-msvc.zip",
        sha256="15e5300b0ba3c3695a7621d90160a746ec9e710228cee639afa9d580f6e3cd11",
        members={"deno.exe": "deno.exe"},
    ),
    Source(
        label="Deno v2.9.6 licence",
        url="https://raw.githubusercontent.com/denoland/deno/v2.9.6/LICENSE.md",
        sha256="f62497fffecc0852960c8d3e6934b9db86d16396e9b604072e923892cae3a588",
        target="licenses/Deno-LICENSE.md",
    ),
)

# Every file a build may receive, and its SHA-256. Paths are relative to the tools folder.
STAGED: dict[str, str] = {
    "ffmpeg.exe": "72a489eccd008c2ec2c0a5856c5c75bc3d8bbfa90166c4566865c246445e6aa3",
    "ffprobe.exe": "19202b23c0043f15ad1b7bce2344f406fd52bd6efd8f995ce02e7392a1cec52f",
    "deno.exe": "2ff9493dfa356be2975f477025ea770088e9e9cb2c83d983236d13561b96b7a6",
    "licenses/FFmpeg-LICENSE.txt": "8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903",  # noqa: E501
    "licenses/Deno-LICENSE.md": "f62497fffecc0852960c8d3e6934b9db86d16396e9b604072e923892cae3a588",
}

EXECUTABLES = ("ffmpeg.exe", "ffprobe.exe", "deno.exe")


class ToolError(Exception):
    """A tool input is missing, unpinned or does not match its pin."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check(path: Path, expected: str, what: str) -> None:
    actual = sha256_file(path)
    if actual != expected.lower():
        raise ToolError(f"SHA-256 mismatch for {what}: {actual} != {expected}")


def _require_https(url: str) -> None:
    if not url.startswith("https://"):
        raise ToolError(f"refusing a non-HTTPS source: {url}")


def download(url: str, expected_sha256: str, dest: Path) -> Path:
    """Download ``url`` to ``dest`` and check its hash; a bad file is deleted, never kept."""
    _require_https(url)
    try:
        with urllib.request.urlopen(url, timeout=120) as resp, dest.open("wb") as out:  # noqa: S310
            shutil.copyfileobj(resp, out, 1 << 20)
        _check(dest, expected_sha256, url)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return dest


def _stage_path(root: Path, relative: str) -> Path:
    """``root / relative``, refusing anything that would land outside ``root``."""
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ToolError(f"staged path escapes the tools folder: {relative}")
    return target


def extract_members(archive: Path, members: dict[str, str], out_root: Path) -> list[str]:
    """Copy each named member out of ``archive``; only the names we pinned are ever read."""
    written = []
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        for member, relative in members.items():
            if member not in names:
                raise ToolError(f"{archive.name} has no member {member}")
            target = _stage_path(out_root, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
            written.append(relative)
    return written


def verify_staged(dest: Path) -> list[Path]:
    """Check every pinned file under ``dest``; return the paths, or raise ToolError."""
    problems = []
    for relative, expected in STAGED.items():
        path = _stage_path(dest, relative)
        if not path.is_file():
            problems.append(f"missing {relative}")
        elif sha256_file(path) != expected:
            problems.append(f"SHA-256 mismatch for {relative}")
    if problems:
        raise ToolError(
            f"{dest} does not hold the pinned tools ({'; '.join(problems)}). "
            "Run: python packaging/fetch_tools.py fetch"
        )
    return [_stage_path(dest, relative) for relative in STAGED]


def sources_text() -> str:
    lines = []
    for source in SOURCES:
        lines += [f"{source.label}, SHA-256 {source.sha256}", f"  {source.url}"]
    lines.append("Staged files:")
    lines += [f"  {relative}  {sha}" for relative, sha in STAGED.items()]
    return "\n".join(lines) + "\n"


def fetch(dest: Path) -> list[Path]:
    """Download and verify everything in a scratch folder, then move it into ``dest``."""
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fetch-tools-", dir=dest.parent) as tmp:
        work = Path(tmp)
        stage = work / "stage"
        stage.mkdir()
        staged: list[str] = []
        for index, source in enumerate(SOURCES):
            downloaded = download(source.url, source.sha256, work / f"download-{index}")
            if source.members:
                staged += extract_members(downloaded, source.members, stage)
            else:
                target = _stage_path(stage, source.target)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(downloaded, target)
                staged.append(source.target)
        if sorted(staged) != sorted(STAGED):
            raise ToolError(f"staged {sorted(staged)}, expected {sorted(STAGED)}")
        verify_staged(stage)
        for relative in STAGED:
            target = _stage_path(dest, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(_stage_path(stage, relative), target)
    (dest / "SOURCES.txt").write_text(sources_text(), encoding="utf-8")
    return verify_staged(dest)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    parser.add_argument("command", choices=["fetch", "verify"])
    args = parser.parse_args(argv)
    try:
        paths = fetch(args.dest) if args.command == "fetch" else verify_staged(args.dest)
    except (ToolError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for path in paths:
        print(f"ok  {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
