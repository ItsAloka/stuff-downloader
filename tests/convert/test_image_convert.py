import subprocess
from pathlib import Path

import pytest

from stuff_downloader_worker import image_convert
from stuff_downloader_worker.engines import ytdlp
from stuff_downloader_worker.engines.base import EngineError

FFMPEG = Path(__file__).resolve().parents[2] / "tools" / "ffmpeg.exe"
needs_ffmpeg = pytest.mark.skipif(not FFMPEG.is_file(), reason="bundled ffmpeg not fetched")


def make(path: Path, source: str, *extra: str) -> Path:
    """Generate a fixture image with ffmpeg's lavfi sources."""
    subprocess.run(
        [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", source,
         *extra, str(path)],
        check=True, stdin=subprocess.DEVNULL,
    )  # fmt: skip
    return path


def first_pixel(path: Path) -> tuple[int, ...]:
    raw = subprocess.run(
        [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-i", str(path),
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-frames:v", "1", "-"],
        check=True, capture_output=True, stdin=subprocess.DEVNULL,
    ).stdout  # fmt: skip
    return tuple(raw[:3])


def transparent_png(folder: Path) -> Path:
    return make(folder / "logo.png", "color=c=red@0.0:s=8x6,format=rgba", "-frames:v", "1")


def convert(source: Path, fmt: str, **kwargs):
    return image_convert.convert_image(source, fmt, ffmpeg=FFMPEG, **kwargs)


def leftovers(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir() if ".converting" in p.name)


# ── no-ops ───────────────────────────────────────────────────────────────────────────────────
def test_original_is_returned_untouched(tmp_path):
    source = tmp_path / "a.webp"
    source.write_bytes(b"not even an image")
    result = image_convert.convert_image(source, "original")
    assert result == image_convert.ConvertResult(source, converted=False)
    assert source.read_bytes() == b"not even an image"


@pytest.mark.parametrize(("name", "fmt"), [("a.JPEG", "jpg"), ("a.jpg", "jpg"), ("a.png", "png")])
def test_a_file_already_in_the_target_format_is_not_reencoded(tmp_path, name, fmt):
    source = tmp_path / name
    source.write_bytes(b"x")
    assert image_convert.convert_image(source, fmt).path == source


@pytest.mark.parametrize(
    ("fmt", "background"),
    [("gif", "#ffffff"), ("jpg", "white"), ("jpg", "#fff"), ("jpg", "#ff00zz")],
)
def test_bad_options_are_refused(tmp_path, fmt, background):
    source = tmp_path / "a.png"
    source.write_bytes(b"x")
    with pytest.raises(EngineError) as err:
        image_convert.convert_image(source, fmt, background=background)
    assert err.value.code == "bad_options"


# ── real conversions with the bundled ffmpeg ─────────────────────────────────────────────────
@needs_ffmpeg
def test_transparency_is_flattened_onto_the_background_not_black(tmp_path):
    source = transparent_png(tmp_path)
    result = convert(source, "jpg", background="#00ff00")
    assert result.converted and result.note is None
    assert result.path == tmp_path / "logo.jpg"
    r, g, b = first_pixel(result.path)
    assert r < 16 and g > 239 and b < 16  # pure green (JPEG rounding allowed), never black
    assert not source.exists()  # the owner asked for JPG instead of the PNG
    assert leftovers(tmp_path) == []


@needs_ffmpeg
def test_default_background_is_white(tmp_path):
    result = convert(transparent_png(tmp_path), "jpg")
    assert all(channel > 239 for channel in first_pixel(result.path))


@needs_ffmpeg
def test_opaque_image_keeps_its_colours(tmp_path):
    source = make(tmp_path / "blue.webp", "color=c=blue:s=8x8", "-frames:v", "1")
    result = convert(source, "jpg")
    r, g, b = first_pixel(result.path)
    assert b > 200 and r < 40 and g < 40


@needs_ffmpeg
def test_jpg_to_png(tmp_path):
    source = make(tmp_path / "photo.jpg", "color=c=blue:s=8x8", "-frames:v", "1")
    result = convert(source, "png")
    assert result.path == tmp_path / "photo.png"
    assert result.path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


@needs_ffmpeg
def test_keep_original_leaves_the_download_in_place(tmp_path):
    source = transparent_png(tmp_path)
    result = convert(source, "jpg", keep_original=True)
    assert source.is_file() and result.path.is_file()


@needs_ffmpeg
@pytest.mark.parametrize(
    ("name", "extra"),
    [("anim.gif", ["-loop", "0"]), ("anim.webp", ["-loop", "0", "-c:v", "libwebp_anim"])],
)
def test_animated_images_keep_the_first_frame_and_say_so(tmp_path, name, extra):
    source = make(tmp_path / name, "testsrc=s=16x16:r=5:d=1", *extra)
    assert image_convert.is_animated(source)
    result = convert(source, "png")
    assert result.note == image_convert.FIRST_FRAME_NOTE
    assert result.path.suffix == ".png" and result.path.stat().st_size > 0


@needs_ffmpeg
def test_a_still_webp_is_not_reported_as_animated(tmp_path):
    source = make(tmp_path / "still.webp", "color=c=blue:s=8x8", "-frames:v", "1")
    assert not image_convert.is_animated(source)
    assert convert(source, "png").note is None


@needs_ffmpeg
def test_an_existing_file_is_never_overwritten(tmp_path):
    source = transparent_png(tmp_path)
    taken = tmp_path / "logo.jpg"
    taken.write_bytes(b"someone else's photo")
    result = convert(source, "jpg")
    assert result.path == tmp_path / "logo (2).jpg"
    assert taken.read_bytes() == b"someone else's photo"


@needs_ffmpeg
def test_a_broken_image_keeps_the_original_and_leaves_no_partial(tmp_path):
    source = tmp_path / "broken.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\n this is not really a png")
    with pytest.raises(EngineError) as err:
        convert(source, "jpg")
    assert err.value.code == "convert_error"
    assert "broken" not in err.value.message and str(tmp_path) not in err.value.message
    assert source.read_bytes().startswith(b"\x89PNG")
    assert not (tmp_path / "broken.jpg").exists()
    assert leftovers(tmp_path) == []


# ── process handling (no real ffmpeg needed) ─────────────────────────────────────────────────
def test_only_the_trusted_tools_dir_ffmpeg_is_used(tmp_path, monkeypatch):
    source = tmp_path / "a.png"
    source.write_bytes(b"x")
    monkeypatch.delenv(ytdlp.TOOLS_DIR_ENV_VAR, raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))  # an ffmpeg on PATH must not be picked up
    (tmp_path / "ffmpeg.exe").write_bytes(b"")
    with pytest.raises(EngineError, match="FFmpeg is needed"):
        image_convert.convert_image(source, "jpg")


def test_ffmpeg_gets_an_argument_list_without_a_shell(tmp_path, monkeypatch):
    source = tmp_path / "a b&c.png"
    source.write_bytes(b"x")
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "ffmpeg.exe").write_bytes(b"")
    monkeypatch.setenv(ytdlp.TOOLS_DIR_ENV_VAR, str(tools))
    seen = {}

    def fake_run(args, **kwargs):
        seen.update(args=args, kwargs=kwargs)
        Path(args[-1]).write_bytes(b"jpeg")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(image_convert.subprocess, "run", fake_run)
    result = image_convert.convert_image(source, "jpg")
    assert isinstance(seen["args"], list) and "shell" not in seen["kwargs"]
    assert seen["args"][0] == str(tools / "ffmpeg.exe")
    assert "-nostdin" in seen["args"] and str(source) in seen["args"]
    assert seen["kwargs"]["stdin"] is subprocess.DEVNULL and seen["kwargs"]["timeout"] > 0
    assert result.path == tmp_path / "a b&c.jpg"


@pytest.mark.parametrize("leave", ["nothing", "empty"])
def test_a_zero_exit_without_output_is_a_convert_error(tmp_path, monkeypatch, leave):
    source = tmp_path / "a.png"
    source.write_bytes(b"x")
    fake = tmp_path / "ffmpeg.exe"
    fake.write_bytes(b"")

    def quiet(args, **kwargs):
        output = Path(args[-1])
        output.unlink()  # mkstemp's placeholder is gone, as if ffmpeg removed its output
        if leave == "empty":
            output.write_bytes(b"")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(image_convert.subprocess, "run", quiet)
    with pytest.raises(EngineError) as err:
        image_convert.convert_image(source, "jpg", ffmpeg=fake)
    assert err.value.code == "convert_error"
    assert source.read_bytes() == b"x"
    assert not (tmp_path / "a.jpg").exists()
    assert leftovers(tmp_path) == []


def test_a_timeout_cleans_up_and_keeps_the_original(tmp_path, monkeypatch):
    source = tmp_path / "a.png"
    source.write_bytes(b"x")
    fake = tmp_path / "ffmpeg.exe"
    fake.write_bytes(b"")

    def slow(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(image_convert.subprocess, "run", slow)
    with pytest.raises(EngineError, match="timed out"):
        image_convert.convert_image(source, "jpg", ffmpeg=fake, timeout=1)
    assert source.read_bytes() == b"x"
    assert leftovers(tmp_path) == []
