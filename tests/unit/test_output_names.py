"""Output-name rules: the sanitizer, template escaping and never re-using a file (item 2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from stuff_downloader_worker import presets
from stuff_downloader_worker.engines import ytdlp
from stuff_downloader_worker.engines.ytdlp import YtDlpEngine
from stuff_downloader_worker.names import safe_output_name

SRC = Path(__file__).resolve().parents[2] / "src"


def test_the_gui_and_worker_sanitizers_are_identical():
    core = (SRC / "stuff_downloader" / "core" / "names.py").read_bytes()
    worker = (SRC / "stuff_downloader_worker" / "names.py").read_bytes()
    assert core == worker


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (r"..\..\x", "____x"),
        ("../../etc/passwd", "____etc_passwd"),
        ("a:b", "a_b"),
        ("a?b", "a_b"),
        ("name.", "name"),
        ("name . . ", "name"),
        ("  spaced   out  ", "spaced out"),
        ("tab\there", "tab_here"),
        ("50%", "50%"),
        ("CONSOLE", "CONSOLE"),
        ("con.txt.bak", None),
        ("CON.txt", None),
        ("CON .txt", None),
        ("nul ", None),
        ("Com1", None),
        ("LPT9.mp3", None),
        ("aux.", None),
        ("", None),
        ("   ", None),
        ("...", "_"),
        (". .", None),
        (None, None),
    ],
)
def test_sanitizer_rules(raw, expected):
    assert safe_output_name(raw) == expected


def test_a_long_name_is_cut_to_200_without_a_trailing_dot():
    assert safe_output_name("x" * 250) == "x" * 200
    assert safe_output_name("y" * 199 + ".z" * 30) == "y" * 199


def test_no_result_ever_contains_a_separator_or_parent_reference():
    for raw in [r"..\..\x", "a/../b", r"C:\Windows", r"\\server\share", "....//"]:
        out = safe_output_name(raw)
        assert out is None or not ({"/", "\\", ":"} & set(out) or ".." in out)


def _request(**options):
    return presets.parse_request({"mode": "download", **options})


@pytest.mark.parametrize("raw", ["", "  ", "CON"])
def test_a_blank_or_device_name_means_the_default_template(raw):
    request = _request(preset="mp3_music", output_name=raw)
    assert request.output_name is None
    assert presets.music_outtmpl(request) == presets.MUSIC_OUTTMPL


@pytest.mark.parametrize(
    ("raw", "outtmpl"),
    [
        ("%(uploader)s", "%%(uploader)s.%(ext)s"),
        ("50%%", "50%%%%.%(ext)s"),
        ("plain", "plain.%(ext)s"),
    ],
)
def test_a_chosen_name_can_never_inject_template_fields(raw, outtmpl, tmp_path):
    assert presets.music_outtmpl(_request(preset="mp3_music", output_name=raw)) == outtmpl
    video = presets.build_ydl_opts(_request(preset="video_best", output_name=raw), str(tmp_path))
    assert video["outtmpl"] == outtmpl


# ── the yt-dlp engine never reuses or re-tags an existing file ───────────────────────────
class FakeYDL:
    """Plans ``<home>/<stem>.webm`` and 'writes' ``<outtmpl stem>.mp3`` like yt-dlp would."""

    def __init__(self, home: Path, stem: str):
        self.home, self.stem = home, stem
        self.params = {"outtmpl": {"default": presets.MUSIC_OUTTMPL}}
        self.files: list[str] = []

    def prepare_filename(self, info):
        return str(self.home / f"{self.stem}.webm")

    def in_download_archive(self, info):
        return False

    def process_ie_result(self, info, download):
        template = self.params["outtmpl"]["default"]
        stem = self.stem if template == presets.MUSIC_OUTTMPL else template[: -len(".%(ext)s")]
        target = self.home / (stem.replace("%%", "%") + ".mp3")
        assert list(self.home.glob("*.sdclaim")), "the name is claimed while downloading"
        if not target.exists():
            target.write_bytes(b"new audio")
        self.files.append(str(target))  # yt-dlp reports an existing file too
        return {}


@pytest.fixture
def tagged(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(ytdlp.tagging, "verify_mp3", lambda path, *a, **k: calls.append(path))
    return calls


def _run(ydl, request):
    return YtDlpEngine._download(
        ydl, {}, request, {"title": "t"}, ydl.files, lambda s: None, lambda *a: None
    )


def test_an_existing_file_is_never_reused_or_retagged(tmp_path, tagged):
    old = tmp_path / "Artist - Song.mp3"
    old.write_bytes(b"someone's file")
    ydl = FakeYDL(tmp_path, "Artist - Song")
    result = _run(ydl, _request(preset="mp3_music"))
    new = tmp_path / "Artist - Song (2).mp3"
    assert result["files"] == [str(new)] and new.read_bytes() == b"new audio"
    assert old.read_bytes() == b"someone's file"
    assert tagged == [str(new)]
    assert not list(tmp_path.glob("*.sdclaim"))


def test_two_playlist_tracks_with_the_same_artist_and_title(tmp_path, tagged):
    first = _run(FakeYDL(tmp_path, "Artist - Title"), _request(preset="mp3_music"))
    second = _run(FakeYDL(tmp_path, "Artist - Title"), _request(preset="mp3_music"))
    assert first["files"] == [str(tmp_path / "Artist - Title.mp3")]
    assert second["files"] == [str(tmp_path / "Artist - Title (2).mp3")]
    assert tagged == first["files"] + second["files"]


def test_a_name_claimed_by_a_running_job_is_skipped(tmp_path, tagged):
    (tmp_path / "Artist - Title.sdclaim").write_bytes(b"")  # another job, mid-download
    result = _run(FakeYDL(tmp_path, "Artist - Title"), _request(preset="mp3_music"))
    assert result["files"] == [str(tmp_path / "Artist - Title (2).mp3")]
    assert (tmp_path / "Artist - Title.sdclaim").exists()  # not ours to remove


def test_a_percent_in_an_existing_name_stays_literal(tmp_path, tagged):
    (tmp_path / "100% Song.mp3").write_bytes(b"old")
    result = _run(FakeYDL(tmp_path, "100% Song"), _request(preset="mp3_music"))
    assert result["files"] == [str(tmp_path / "100% Song (2).mp3")]


def test_a_file_that_was_not_written_by_this_run_is_not_tagged(tmp_path, tagged):
    class Stubborn(FakeYDL):  # ignores the pinned name and reports an old file
        def process_ie_result(self, info, download):
            old = self.home / "Old.mp3"
            old.write_bytes(b"old")
            self.files.append(str(old))
            return {}

    _run(Stubborn(tmp_path, "Artist - Title"), _request(preset="mp3_music"))
    assert tagged == []


@pytest.mark.parametrize(
    "leftover",
    ["Artist - Title.webm.part", "Artist - Title.f251.webm", "Artist - Title.webm.ytdl"],
)
def test_a_resumed_job_keeps_its_name_despite_its_own_part_files(tmp_path, tagged, leftover):
    (tmp_path / leftover).write_bytes(b"half")
    result = _run(FakeYDL(tmp_path, "Artist - Title"), _request(preset="mp3_music"))
    assert result["files"] == [str(tmp_path / "Artist - Title.mp3")]
