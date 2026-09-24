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
    assert audio["format"] == "bestaudio/best"
    assert [pp["key"] for pp in audio["postprocessors"]] == ["FFmpegMetadata"]


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



# ── Result-card rows (plan §8 R2) ──────────────────────────────────────────────────────────
def _row(**options):
    return presets.parse_row_request({"mode": "download", **options})


def _keys(opts):
    return [pp["key"] for pp in opts.get("postprocessors", [])]


@pytest.mark.parametrize("kbps", [320, 256, 192, 128, 64])
def test_an_mp3_row_extracts_at_its_bitrate_with_tags_and_a_square_cover(kbps, tmp_path):
    opts = presets.build_row_opts(_row(tab="audio", row_id=f"a:mp3:{kbps}"), str(tmp_path))
    extract = next(pp for pp in opts["postprocessors"] if pp["key"] == "FFmpegExtractAudio")
    assert extract == {
        "key": "FFmpegExtractAudio",
        "preferredcodec": "mp3",
        "preferredquality": str(kbps),
    }
    assert _keys(opts) == [
        "FFmpegThumbnailsConvertor",
        "FFmpegExtractAudio",
        "FFmpegMetadata",
        "EmbedThumbnail",
    ]
    assert opts["writethumbnail"] is True
    assert opts["postprocessor_args"] == {
        "thumbnailsconvertor+ffmpeg_o": presets.SQUARE_CROP_ARGS
    }
    assert opts["outtmpl"] == presets.MUSIC_OUTTMPL


@pytest.mark.parametrize(
    ("row_id", "selector", "codec"),
    [
        ("a:m4a", "bestaudio[acodec^=mp4a]/bestaudio/best", "m4a"),
        ("a:opus", "bestaudio[acodec=opus]/bestaudio/best", "opus"),
        ("a:flac", "bestaudio/best", "flac"),
    ],
)
def test_every_other_audio_row_keeps_tags_and_a_cover(row_id, selector, codec):
    opts = presets.build_row_opts(_row(tab="audio", row_id=row_id), ".")
    assert opts["format"] == selector
    assert _keys(opts) == [
        "FFmpegThumbnailsConvertor",
        "FFmpegExtractAudio",
        "FFmpegMetadata",
        "EmbedThumbnail",
    ]
    extract = opts["postprocessors"][1]
    assert extract["preferredcodec"] == codec
    # M4A from AAC and Opus from Opus are copied by yt-dlp; only a non-AAC source is encoded.
    assert extract.get("preferredquality") == ("256" if codec == "m4a" else None)


def test_a_wav_row_is_tagged_but_gets_no_embedded_cover():
    opts = presets.build_row_opts(_row(tab="audio", row_id="a:wav"), ".")
    assert _keys(opts) == ["FFmpegExtractAudio", "FFmpegMetadata"]
    assert opts["writethumbnail"] is False and "postprocessor_args" not in opts


@pytest.mark.parametrize(
    ("container", "merge", "key"),
    [
        ("mp4", "mp4", "FFmpegVideoRemuxer"),
        ("mkv", "mkv", "FFmpegVideoRemuxer"),
        ("webm", "webm/mkv", "FFmpegVideoConvertor"),
        ("mov", "mkv", "FFmpegVideoConvertor"),
        ("avi", "mkv", "FFmpegVideoConvertor"),
    ],
)
def test_a_video_row_maps_its_container_to_a_merge_or_a_convert(container, merge, key):
    opts = presets.build_row_opts(
        _row(tab="video", row_id="v:1080:mp4", container=container), "."
    )
    assert opts["merge_output_format"] == merge
    assert opts["postprocessors"] == [{"key": key, "preferedformat": container}]
    first, second = opts["format"].split("/")[:2]
    # The row shows H.264 for the height, so H.264 is asked for first (not yt-dlp's AV1).
    assert first.startswith("bv*[height=1080][ext=mp4][vcodec^=avc1]+ba")
    assert second.startswith("bv*[height=1080][ext=mp4]+ba")
    assert opts["outtmpl"] == presets.VIDEO_ROW_OUTTMPL


def test_reencodes_are_only_mov_avi_and_webm_from_another_codec():
    assert presets.needs_reencode("mov", "mp4") and presets.needs_reencode("avi", "webm")
    assert presets.needs_reencode("webm", "mp4") and not presets.needs_reencode("webm", "webm")
    assert not presets.needs_reencode("mp4", "webm") and not presets.needs_reencode("mkv", "mp4")


@pytest.mark.parametrize("fmt", ["jpg", "png", "webp"])
def test_an_image_row_converts_the_chosen_thumbnail(fmt):
    request = _row(tab="image", row_id="i:1280x720", container=fmt)
    assert request.image_size == (1280, 720) and request.preset == "thumbnail"
    opts = presets.build_row_opts(request, ".")
    assert opts["skip_download"] is True and opts["writethumbnail"] is True
    assert opts["postprocessors"] == [
        {"key": "FFmpegThumbnailsConvertor", "format": fmt, "when": "before_dl"}
    ]


def test_an_original_image_row_is_saved_as_it_is():
    opts = presets.build_row_opts(_row(tab="image", row_id="i:best", container="original"), ".")
    assert opts["postprocessors"] == []


def test_the_edited_title_is_the_literal_file_name():
    request = _row(tab="audio", row_id="a:mp3:320", edited_title="50% off: my/mix")
    assert request.edited_title == "50% off_ my_mix"
    opts = presets.build_row_opts(request, ".")
    assert opts["outtmpl"] == "50%% off_ my_mix.%(ext)s"
    # Tags are untouched: FFmpegMetadata reads the source's title, not the file name.
    assert {"key": "FFmpegMetadata", "add_metadata": True} in opts["postprocessors"]


@pytest.mark.parametrize(
    "options",
    [
        {"tab": "video", "row_id": "137", "container": "mp4"},  # a site format id
        {"tab": "video", "row_id": "v:1080:mp4"},  # no container
        {"tab": "video", "row_id": "v:1080:flv", "container": "mp4"},
        {"tab": "video", "row_id": "v:99999:mp4", "container": "mp4"},
        {"tab": "video", "row_id": "a:mp3:320", "container": "mp4"},  # row from another tab
        {"tab": "audio", "row_id": "a:mp3:999"},
        {"tab": "audio", "row_id": "a:mp3:320", "container": "mp4"},
        {"tab": "image", "row_id": "i:1280x720", "container": "tiff"},
        {"tab": "image", "row_id": "i:../../x", "container": "png"},
        {"tab": "subtitles", "row_id": "s:en"},
        {"tab": "audio", "row_id": "a:wav", "edited_title": 5},
        {"tab": "audio", "row_id": "a:wav", "edited_title": "x" * 301},
        {"tab": "audio", "row_id": "a:wav", "format": "bestaudio"},  # an engine option
        {"tab": "audio", "row_id": "a:wav", "postprocessors": []},
    ],
)
def test_row_requests_refuse_anything_outside_the_catalog(options):
    with pytest.raises(EngineError) as info:
        _row(**options)
    assert info.value.code == "bad_options"


def test_the_direct_engines_take_only_their_original_rows():
    assert presets.parse_row_request(
        {"tab": "video", "row_id": "v:orig", "container": "mkv"}, original_only=True
    ).container == "mkv"
    with pytest.raises(EngineError):
        presets.parse_row_request(
            {"tab": "video", "row_id": "v:1080:mp4", "container": "mkv"}, original_only=True
        )
    with pytest.raises(EngineError):
        _row(tab="video", row_id="v:orig", container="mp4")


def test_a_webm_row_does_not_ask_for_h264():
    selector = presets.row_video_format(2160, "webm", "webm")
    assert "avc1" not in selector and selector.startswith("bv*[height=2160][ext=webm]+ba[ext=webm]")


@pytest.mark.parametrize(
    ("tab", "row_id", "container"),
    [
        ("audio", "a:mp3:320", None),
        ("audio", "a:mp3:64", None),
        ("audio", "a:m4a", None),
        ("audio", "a:opus", None),
        ("audio", "a:flac", None),
        ("audio", "a:wav", None),
        ("audio", "a:orig", None),
        ("image", "i:frame", "png"),
        ("image", "i:frame", "original"),
    ],
)
def test_a_direct_video_also_takes_its_audio_and_frame_rows(tab, row_id, container):
    request = presets.parse_row_request(
        {"tab": tab, "row_id": row_id, "container": container}, original_only=True
    )
    assert (request.tab, request.row_id, request.container) == (tab, row_id, container)


@pytest.mark.parametrize(
    ("tab", "row_id", "container"),
    [
        ("audio", "a:mp3:999", None),
        ("audio", "a:mp3:320", "mp4"),  # an audio row takes no container
        ("image", "i:1280x720", "png"),  # thumbnail sizes are yt-dlp rows, not file rows
        ("image", "i:best", "png"),
        ("image", "i:frame", "tiff"),
        ("image", "i:frame:2", "png"),
        ("video", "v:frame", "mp4"),
        ("audio", "a:frame", None),
    ],
)
def test_the_direct_engines_refuse_other_rows(tab, row_id, container):
    with pytest.raises(EngineError):
        presets.parse_row_request(
            {"tab": tab, "row_id": row_id, "container": container}, original_only=True
        )


def test_the_frame_row_is_not_a_yt_dlp_row():
    with pytest.raises(EngineError):
        _row(tab="image", row_id="i:frame", container="png")
