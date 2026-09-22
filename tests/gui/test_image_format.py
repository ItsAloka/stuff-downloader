"""GUI: the Original/JPG/PNG choice for direct images and galleries (item 6B)."""

from __future__ import annotations

import pytest
from test_main_window import _analyzed
from test_playlist_queue import runs, window  # noqa: F401  (fixtures)

from stuff_downloader.core import presets
from stuff_downloader.core.protocol import Event


def _analyze_file(window, runs, qtbot, url, ext):  # noqa: F811
    page = window.downloads_page
    page.url_edit.setText(url)
    page.analyze()
    run = runs[-1]
    payload = {
        "kind": "file",
        "title": "thing",
        "extractor": "Direct file",
        "ext": ext,
        "filesize": 100,
        "formats": [],
    }
    run.on_event(Event("result", run.spec.job_id, payload))
    qtbot.waitUntil(lambda: not page.preview.isHidden())
    return page


def test_a_direct_image_offers_the_format_and_sends_it(window, runs, qtbot):  # noqa: F811
    page = _analyze_file(window, runs, qtbot, "https://cdn.example.com/p/photo.webp", "webp")
    combo = page.preview.image_format_combo
    assert not combo.isHidden() and combo.currentData() == "original"
    assert "image_format" not in page.start_download().spec.options  # Original is the default
    combo.setCurrentIndex(combo.findData("jpg"))
    assert page.start_download().spec.options["image_format"] == "jpg"


def test_a_direct_video_has_no_format_choice(window, runs, qtbot):  # noqa: F811
    page = _analyze_file(window, runs, qtbot, "https://cdn.example.com/p/photo.png", "png")
    page.preview.image_format_combo.setCurrentIndex(2)
    page = _analyze_file(window, runs, qtbot, "https://cdn.example.com/v/clip.mp4", "mp4")
    assert page.preview.image_format_combo.isHidden()
    assert "image_format" not in page.start_download().spec.options


def test_each_new_image_link_starts_as_original(window, runs, qtbot):  # noqa: F811
    page = _analyze_file(window, runs, qtbot, "https://cdn.example.com/p/a.png", "png")
    page.preview.image_format_combo.setCurrentIndex(2)
    page = _analyze_file(window, runs, qtbot, "https://cdn.example.com/p/b.png", "png")
    assert page.preview.image_format_combo.currentData() == "original"


def test_a_gallery_sends_its_image_format(window, runs, qtbot):  # noqa: F811
    page = window.downloads_page
    page.url_edit.setText("https://www.instagram.com/p/ABC123/")
    page.analyze()
    run = runs[-1]
    run.on_event(
        Event(
            "result",
            run.spec.job_id,
            {
                "kind": "gallery",
                "title": "Post",
                "site": "Instagram",
                "items": [{"index": 1, "kind": "image", "ext": "webp"}],
            },
        )
    )
    qtbot.waitUntil(lambda: not page.gallery_card.isHidden())
    combo = page.gallery_card.image_format_combo
    assert page.gallery_card.download_button.text().endswith("(original quality)")
    combo.setCurrentIndex(combo.findData("png"))
    assert page.gallery_card.download_button.text().endswith("(images as PNG)")
    job = page.start_gallery_download()
    assert job.spec.options["image_format"] == "png"


def test_option_builders_refuse_unknown_formats():
    with pytest.raises(ValueError):
        presets.file_download_options(image_format="tiff")
    with pytest.raises(ValueError):
        presets.gallery_download_options([1], image_format="bmp")
    assert presets.gallery_download_options([1], image_format="original") == {
        "mode": "download",
        "preset": "gallery_original",
        "items": [1],
    }


def test_a_worker_note_shows_in_the_job_status(window, runs, qtbot, tmp_path):  # noqa: F811
    window.settings_page.set_folder(str(tmp_path))
    page = _analyzed(window, runs, qtbot)
    job = page.start_download()
    out = tmp_path / "anim.jpg"
    out.write_bytes(b"x")
    runs[-1].emit(
        "result",
        files=[str(out)],
        total_bytes=1,
        notes=[
            "Animated image: only the first frame was kept.",
            "Could not convert to PNG; kept the original.",
            "see http://evil.example/<b>x</b>",
            5,
        ],
    )
    details = job.card.details_label.text()
    assert "only the first frame was kept" in details
    assert "kept the original" in details
    assert "evil" not in details  # only the worker's known sentences are shown
    assert job.card.details_label.textFormat().name == "PlainText"
    assert page._final_file(job.spec.job_id)[0] == out.resolve()  # the converted file opens


def test_the_gui_shows_every_note_the_worker_can_send():
    from stuff_downloader.gui.pages import KNOWN_NOTE
    from stuff_downloader_worker import image_convert

    assert KNOWN_NOTE.fullmatch(image_convert.FIRST_FRAME_NOTE)
    for fmt in ("jpg", "png"):
        assert KNOWN_NOTE.fullmatch(f"Could not convert to {fmt.upper()}; kept the original.")


def test_only_galleries_and_direct_images_offer_a_format():
    from stuff_downloader.gui.widgets import PlaylistCard, SpotifyCard

    assert not hasattr(SpotifyCard(), "image_format_combo")
    assert not hasattr(PlaylistCard(), "image_format_combo")
