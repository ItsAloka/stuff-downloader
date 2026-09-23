from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")

from stuff_downloader.core.protocol import Event
from stuff_downloader.gui import converters


class FakeRun:
    runs = []

    def __init__(self, spec, callback):
        self.spec = spec
        self.callback = callback
        self.cancelled = False
        self.runs.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def wait(self, timeout):
        pass


def test_converter_ui_lifecycle(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(converters, "JobRun", FakeRun)
    source = tmp_path / "image.png"
    source.write_bytes(b"image")
    output = tmp_path / "output"
    output.mkdir()
    page = converters.ConvertersPage()
    qtbot.addWidget(page)
    page.source_edit.setText(str(source))
    page.destination_edit.setText(str(output))
    page.format_combo.setCurrentIndex(2)  # WebP
    page.start_conversion()
    run = FakeRun.runs[-1]
    assert run.spec.engine == "imageconvert"
    assert run.spec.options["format"] == "webp"
    assert page.progress.isVisible() is False  # parent page is not shown
    assert not page.convert_button.isEnabled()

    finished = output / "image.webp"
    page._on_event(Event("result", run.spec.job_id, {"files": [str(finished)]}))
    assert page.open_button.isEnabled()
    assert str(finished) in page.status_label.text()
    assert page.convert_button.isEnabled()
    page.source_edit.setText(str(tmp_path / "another.png"))
    assert not page.open_button.isEnabled()
    assert "Done:" not in page.status_label.text()


def test_cancel_cleans_reported_partial(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(converters, "JobRun", FakeRun)
    source = tmp_path / "image.png"
    source.write_bytes(b"image")
    page = converters.ConvertersPage()
    qtbot.addWidget(page)
    page.source_edit.setText(str(source))
    page.destination_edit.setText(str(tmp_path))
    page.start_conversion()
    run = FakeRun.runs[-1]
    partial_dir = tmp_path / f".image.{run.spec.job_id}.abc.converting"
    partial_dir.mkdir()
    (partial_dir / "image.png").write_bytes(b"partial")
    page._on_event(Event("stage", run.spec.job_id, {"temporary_path": str(partial_dir)}))
    page.cancel_conversion()
    qtbot.waitUntil(lambda: run.cancelled)
    page._on_event(Event("error", run.spec.job_id, {"message": "Cancelled"}))
    assert not partial_dir.exists()
    assert source.is_file()


def test_empty_result_is_reported_as_error(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(converters, "JobRun", FakeRun)
    source = tmp_path / "image.png"
    source.write_bytes(b"image")
    page = converters.ConvertersPage()
    qtbot.addWidget(page)
    page.source_edit.setText(str(source))
    page.destination_edit.setText(str(tmp_path))
    page.start_conversion()
    run = FakeRun.runs[-1]
    page._on_event(Event("result", run.spec.job_id, {"files": []}))
    assert "without an output" in page.status_label.text()
    assert not page.open_button.isEnabled()


def test_layout_stays_within_narrow_page(qtbot):
    page = converters.ConvertersPage()
    qtbot.addWidget(page)
    page.resize(550, 480)
    page.show()
    qtbot.wait(20)
    assert page.convert_button.isVisible()
    assert page.page_scroll.horizontalScrollBar().maximum() == 0, (
        page.width(), page.page_scroll.width(), page.page_content.width(),
        page.page_content.minimumSizeHint().width(),
    )


def test_video_converter_lifecycle_and_cancel_cleanup(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(converters, "JobRun", FakeRun)
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    page = converters.ConvertersPage()
    qtbot.addWidget(page)
    video = page.video
    video.source_edit.setText(str(source))
    video.destination_edit.setText(str(tmp_path))
    video.format_combo.setCurrentIndex(4)
    video.start_conversion()
    run = FakeRun.runs[-1]
    assert run.spec.engine == "videoconvert"
    assert run.spec.options["format"] == "webm"
    assert not video.convert_button.isEnabled()
    partial = tmp_path / f".clip.{run.spec.job_id}.abc.converting"
    partial.mkdir()
    (partial / "video.webm").write_bytes(b"partial")
    video._on_event(Event("stage", run.spec.job_id, {"temporary_path": str(partial)}))
    video.cancel_conversion()
    qtbot.waitUntil(lambda: run.cancelled)
    video._on_event(Event("error", run.spec.job_id, {"message": "Cancelled"}))
    assert not partial.exists()
    assert source.read_bytes() == b"video"
    assert video.convert_button.isEnabled()
    video.start_conversion()
    run = FakeRun.runs[-1]
    result = tmp_path / "clip.webm"
    video._on_event(Event("result", run.spec.job_id, {"files": [str(result)]}))
    assert video.open_button.isEnabled()
    assert str(result) in video.status_label.text()


def test_audio_converter_formats_and_lifecycle(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(converters, "JobRun", FakeRun)
    source = tmp_path / "clip.wav"
    source.write_bytes(b"audio")
    page = converters.ConvertersPage()
    qtbot.addWidget(page)
    audio = page.audio
    audio.source_edit.setText(str(source))
    audio.destination_edit.setText(str(tmp_path))
    assert [audio.format_combo.itemData(i) for i in range(audio.format_combo.count())] == [
        "mp3", "m4a", "aac", "opus", "wav", "flac"
    ]
    for i in range(audio.format_combo.count()):
        audio.format_combo.setCurrentIndex(i)
        audio.start_conversion()
        run = FakeRun.runs[-1]
        assert run.spec.engine == "audioconvert"
        assert run.spec.options["format"] == audio.format_combo.currentData()
        assert not audio.convert_button.isEnabled()
        result = tmp_path / f"clip.{run.spec.options['format']}"
        audio._on_event(Event("result", run.spec.job_id, {"files": [str(result)]}))
        assert audio.open_button.isEnabled()
        assert str(result) in audio.status_label.text()
        assert audio.convert_button.isEnabled()


def test_audio_cancel_and_error_cleanup(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(converters, "JobRun", FakeRun)
    source = tmp_path / "clip.wav"
    source.write_bytes(b"audio")
    page = converters.ConvertersPage()
    qtbot.addWidget(page)
    audio = page.audio
    audio.source_edit.setText(str(source))
    audio.destination_edit.setText(str(tmp_path))
    audio.start_conversion()
    run = FakeRun.runs[-1]
    partial = tmp_path / f".clip.{run.spec.job_id}.abc.converting"
    partial.mkdir()
    (partial / "audio.mp3").write_bytes(b"partial")
    audio._on_event(Event("stage", run.spec.job_id, {"temporary_path": str(partial)}))
    audio.cancel_conversion()
    qtbot.waitUntil(lambda: run.cancelled)
    audio._on_event(Event("error", run.spec.job_id, {"message": "Cancelled"}))
    assert not partial.exists()
    assert source.read_bytes() == b"audio"
    assert audio.convert_button.isEnabled()


def test_audio_file_picker_accepts_audio_extensions(qtbot, monkeypatch, tmp_path):
    source = tmp_path / "track.flac"
    source.write_bytes(b"audio")
    captured = {}

    def choose(_parent, _title, _directory, file_filter):
        captured["filter"] = file_filter
        return str(source), ""

    monkeypatch.setattr(converters.QFileDialog, "getOpenFileName", choose)
    page = converters.ConvertersPage()
    qtbot.addWidget(page)
    page.audio._choose_source()
    assert all(f"*.{fmt}" in captured["filter"] for fmt in
               ("mp3", "m4a", "aac", "opus", "wav", "flac"))
    assert page.audio.source_edit.text() == str(source)
    assert page.audio.destination_edit.text() == str(tmp_path)
