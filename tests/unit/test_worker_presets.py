import pytest

from stuff_downloader_worker import presets
from stuff_downloader_worker.engines.base import EngineError


def _req(**options):
    base = {"mode": "download", "preset": "video_best", "height": None, "compatible": True}
    return presets.parse_request({**base, **options})


def test_mp3_preset_matches_the_guide_recipe(tmp_path):
    opts = presets.build_ydl_opts(_req(preset="mp3_music", crop_cover=True), str(tmp_path))
    assert opts["format"] == "bestaudio/best"
    assert opts["noplaylist"] is True and opts["windowsfilenames"] is True
    assert opts["writethumbnail"] is True
    assert opts["outtmpl"] == "%(artist,uploader)s - %(track,title).150B.%(ext)s"
    assert opts["paths"] == {"home": str(tmp_path)}
    keys = [pp["key"] for pp in opts["postprocessors"]]
    assert keys == [
        "FFmpegThumbnailsConvertor",
        "FFmpegExtractAudio",
        "FFmpegMetadata",
        "EmbedThumbnail",
    ]
    extract = opts["postprocessors"][1]
    assert (extract["preferredcodec"], extract["preferredquality"]) == ("mp3", "0")
    assert opts["postprocessors"][0]["format"] == "jpg"
    assert opts["postprocessor_args"] == {
        "thumbnailsconvertor+ffmpeg_o": [
            "-qmin",
            "1",
            "-q:v",
            "1",
            "-vf",
            "crop='if(gt(ih,iw),iw,ih)':'if(gt(iw,ih),ih,iw)'",
        ]
    }


def test_crop_can_be_turned_off():
    opts = presets.build_ydl_opts(_req(preset="mp3_music", crop_cover=False), ".")
    assert "postprocessor_args" not in opts


def test_video_selectors_and_container():
    opts = presets.build_ydl_opts(_req(preset="video_1080", height=2160), ".")
    assert opts["format"].split("/")[0] == "bv*[height<=1080][vcodec^=avc1]+ba[acodec^=mp4a]"
    assert opts["format"].endswith("/bv*+ba/b")  # fall back rather than fail
    assert opts["merge_output_format"] == "mp4"
    assert "postprocessors" not in opts

    best = presets.build_ydl_opts(_req(compatible=False), ".")
    assert best["format"] == "bv*+ba/b" and best["merge_output_format"] == "mkv"


def test_thumbnail_and_original_audio_presets():
    thumb = presets.build_ydl_opts(_req(preset="thumbnail"), ".")
    assert thumb["skip_download"] and thumb["writethumbnail"]
    assert "thumbnailsconvertor+ffmpeg_o" in thumb["postprocessor_args"]
    audio = presets.build_ydl_opts(_req(preset="audio_original"), ".")
    assert audio["format"] == "bestaudio[acodec=opus]/bestaudio/best"
    assert "postprocessors" not in audio


@pytest.mark.parametrize(
    ("preset_id", "codec"),
    [("audio_m4a", "m4a"), ("audio_flac", "flac"), ("audio_wav", "wav")],
)
def test_audio_conversion_uses_fixed_ffmpeg_codec(preset_id, codec):
    opts = presets.build_ydl_opts(_req(preset=preset_id), ".")
    assert opts["format"] == "bestaudio/best"
    assert opts["postprocessors"] == [
        {"key": "FFmpegExtractAudio", "preferredcodec": codec}
    ]


@pytest.mark.parametrize(
    "options",
    [
        {"preset": "rm -rf"},
        {"preset": None},
        {"height": 1081},
        {"height": "1080"},
        {"height": True},
        {"compatible": "yes"},
        {"crop_cover": 1},
        {"postprocessor_args": {"x": ["-i", "evil"]}},
        {"outtmpl": "C:/Windows/%(id)s"},
        {"ffmpeg_location": "C:/evil.exe"},
        {"format": "b"},
    ],
)
def test_untrusted_options_are_rejected(options):
    with pytest.raises(EngineError) as info:
        _req(**options)
    assert info.value.code == "bad_options"


def test_height_is_dropped_for_non_video_presets():
    assert _req(preset="mp3_music", height=720).height is None
