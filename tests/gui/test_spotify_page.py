"""M5 in the GUI: the Spotify listing, match review, pasted overrides and the MP3 batch."""

from __future__ import annotations

import pytest

from stuff_downloader.core import settings, spotify, tools
from stuff_downloader.core.protocol import Event
from stuff_downloader.gui import pages
from stuff_downloader.gui.main_window import MainWindow
from stuff_downloader.gui.widgets import SpotifyCard

T1, T2, T3 = "6OmhkSOpvYBokMKQxpIGx2", "2iblMMIgSznA464mNov7A8", "4yOn1TEcfsKHUJCL2h1r8I"
ALBUM = "4aawyAB9vmqN3uQ7FjRGTy"
SHARE_URL = f"https://open.spotify.com/album/{ALBUM}?si=a1b2c3d4e5f6"
VID, VID2 = "q7xBoh0emqo", "dQw4w9WgXcQ"


class FakeRun:
    instances: list = []

    def __init__(self, spec, on_event):
        self.spec = spec
        self.on_event = on_event
        self.started = False
        self.cancelled = False
        FakeRun.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True
        self.emit("error", code="cancelled", message="Cancelled")

    def emit(self, event_type, /, **data):
        self.on_event(Event(event_type, self.spec.job_id, data))


@pytest.fixture
def runs(monkeypatch):
    FakeRun.instances = []
    monkeypatch.setattr(pages, "JobRun", FakeRun)
    return FakeRun.instances


@pytest.fixture
def page(qtbot, monkeypatch, runs):
    monkeypatch.setattr(tools, "check_all", lambda configured=None: [])
    window = MainWindow(settings.Settings())
    qtbot.addWidget(window)
    yield window.downloads_page


def track(track_id, title, artists, duration, **extra):
    return {
        "id": track_id,
        "title": title,
        "artists": artists,
        "album": "Global Warming",
        "duration": duration,
        "explicit": False,
        **extra,
    }


def album_result(**extra):
    return {
        "kind": "spotify",
        "spotify_kind": "album",
        "spotify_id": ALBUM,
        "title": "Global Warming",
        "owner": "Pitbull",
        "tracks": [
            track(T1, "Global Warming", ["Pitbull", "Sensato"], 85.0, explicit=True),
            track(T2, "Don't Stop the Party", ["Pitbull", "TJR"], 206.0),
            track(T3, "Feel This Moment", ["Pitbull", "Christina Aguilera"], 229.0),
        ],
        "skipped": 0,
        "truncated": False,
        **extra,
    }


def match_result(track_id, video_id=VID, duration=88.0, confidence=96.0):
    return {
        "kind": "spotify_match",
        "track_id": track_id,
        "video_id": video_id,
        "title": "Pitbull - Global Warming",
        "channel": "PitbullVEVO",
        "duration": duration,
        "confidence": confidence,
    }


def _analyzed(page, runs, qtbot, url=SHARE_URL, result=None):
    page.url_edit.setText(url)
    page.analyze()
    run = runs[-1]
    run.emit("result", **(result or album_result()))
    qtbot.waitUntil(lambda: not page.spotify_card.isHidden())
    return run


def _match_runs(runs):
    return [r for r in runs if r.spec.options == {"mode": "match"}]


# ── analyze ───────────────────────────────────────────────────────────────────────────────
def test_a_share_link_is_analyzed_by_the_spotdl_engine_without_its_tracking_query(
    page, runs, qtbot
):
    run = _analyzed(page, runs, qtbot)
    assert run.spec.engine == "spotdl"
    assert run.spec.url == f"https://open.spotify.com/album/{ALBUM}"  # ?si= never travels
    assert run.spec.options == {"mode": "analyze"}
    assert "site_login" not in run.spec.options


def test_the_listing_shows_every_track_ticked_and_says_the_audio_is_matched(page, runs, qtbot):
    _analyzed(page, runs, qtbot)
    card = page.spotify_card
    assert card.title_label.text() == "Global Warming"
    assert "Album" in card.meta_label.text() and "3 songs" in card.meta_label.text()
    assert card.table.rowCount() == 3 and card.selected_rows() == [0, 1, 2]
    assert card.cell_text(0, 2) == "Global Warming  🅴"
    assert card.cell_text(1, 3) == "Pitbull, TJR" and card.cell_text(1, 4) == "3:26"
    assert card.cell_text(0, SpotifyCard.MATCH_COLUMN) == SpotifyCard.NOT_CHECKED
    # The disclosure is not optional small print: it is in the card, in plain words.
    text = card.disclosure_label.text()
    assert "never downloaded" in text and "YouTube Music" in text
    assert page.preview.isHidden() and page.playlist_card.isHidden()
    assert card.selection_label.text() == "3 selected"


def test_markup_in_spotify_text_is_shown_as_text(page, runs, qtbot):
    _analyzed(page, runs, qtbot, result=album_result(title="<b>bold</b><img src=x>"))
    assert page.spotify_card.title_label.text() == "<b>bold</b><img src=x>"
    from PyQt6.QtCore import Qt

    assert page.spotify_card.title_label.textFormat() == Qt.TextFormat.PlainText


def test_an_empty_listing_says_so_instead_of_an_empty_table(page, runs, qtbot):
    page.url_edit.setText(SHARE_URL)
    page.analyze()
    runs[-1].emit("result", **album_result(tracks=[]))
    qtbot.waitUntil(lambda: "No downloadable songs" in page.message_label.text())
    assert page.spotify_card.isHidden()


def test_a_spotify_failure_never_offers_a_site_login(page, runs, qtbot):
    page.url_edit.setText(SHARE_URL)
    page.analyze()
    runs[-1].emit("error", code="download_error", message="login required: private playlist")
    qtbot.waitUntil(lambda: not page.message_label.isHidden() and page._analyze_run is None)
    assert page.login_button.isHidden()


# ── matches ───────────────────────────────────────────────────────────────────────────────
def test_checking_matches_runs_a_few_lookups_at_a_time_and_fills_rows_as_they_land(
    page, runs, qtbot
):
    _analyzed(page, runs, qtbot)
    assert page.check_spotify_matches() == 3
    lookups = _match_runs(runs)
    assert len(lookups) == pages.MATCH_CONCURRENCY  # not all three at once
    assert [r.spec.url for r in lookups] == [
        f"https://open.spotify.com/track/{T1}",
        f"https://open.spotify.com/track/{T2}",
    ]
    assert all(r.spec.engine == "spotdl" and r.started for r in lookups)
    card = page.spotify_card
    assert card.cell_text(2, SpotifyCard.MATCH_COLUMN) == "Waiting to check…"
    assert "checking 3 matches" in card.selection_label.text()

    lookups[0].emit("result", **match_result(T1))
    qtbot.waitUntil(lambda: len(_match_runs(runs)) == 3)  # a slot freed, the third started
    assert card.cell_text(0, SpotifyCard.MATCH_COLUMN) == "Pitbull - Global Warming  ·  PitbullVEVO"
    assert card.cell_text(0, SpotifyCard.DIFF_COLUMN) == "+3s"
    assert card.cell_text(0, SpotifyCard.SCORE_COLUMN) == "96%"
    # Match lookups are not downloads: no queue rows, nothing in history.
    assert page.jobs == {}


def test_matches_are_not_looked_up_twice(page, runs, qtbot):
    _analyzed(page, runs, qtbot)
    page.spotify_card.set_all_checked(False)
    page.spotify_card.checkbox(0).setChecked(True)
    assert page.check_spotify_matches() == 1
    assert page.check_spotify_matches() == 0  # already running
    _match_runs(runs)[0].emit("result", **match_result(T1))
    qtbot.waitUntil(lambda: T1 in page._spotify_matches)
    assert page.check_spotify_matches() == 0  # already known
    assert len(_match_runs(runs)) == 1


@pytest.mark.parametrize(
    ("event", "data", "expected"),
    [
        ("error", {"code": "no_match", "message": "No matching song"}, "Change…"),
        ("result", match_result(T2), "No usable match"),  # an answer for another row
        ("result", {**match_result(T1), "video_id": "javascript:x"}, "No usable match"),
    ],
)
def test_a_failed_or_unusable_lookup_says_so_on_its_row(page, runs, qtbot, event, data, expected):
    _analyzed(page, runs, qtbot)
    page.spotify_card.set_all_checked(False)
    page.spotify_card.checkbox(0).setChecked(True)
    page.check_spotify_matches()
    _match_runs(runs)[0].emit(event, **data)
    qtbot.waitUntil(lambda: not page._match_runs)
    assert expected in page.spotify_card.cell_text(0, SpotifyCard.MATCH_COLUMN)
    assert T1 not in page._spotify_matches


def test_a_suspicious_match_is_flagged(page, runs, qtbot):
    _analyzed(page, runs, qtbot)
    page.spotify_card.set_all_checked(False)
    page.spotify_card.checkbox(0).setChecked(True)
    page.check_spotify_matches()
    _match_runs(runs)[0].emit("result", **match_result(T1, duration=3600.0, confidence=40))
    qtbot.waitUntil(lambda: T1 in page._spotify_matches)
    item = page.spotify_card.table.item(0, SpotifyCard.DIFF_COLUMN)
    assert page.spotify_card.cell_text(0, SpotifyCard.DIFF_COLUMN) == "+3515s"
    assert item.foreground().color().name() == "#e0a040"


def test_a_new_link_cancels_lookups_and_late_answers_land_nowhere(page, runs, qtbot):
    _analyzed(page, runs, qtbot)
    page.check_spotify_matches()
    first = _match_runs(runs)[0]
    page.url_edit.setText(f"https://open.spotify.com/track/{T3}")
    page.analyze()
    assert first.cancelled
    qtbot.waitUntil(lambda: not page._match_runs)
    first.emit("result", **match_result(T1))  # a straggler for the old listing
    runs[-1].emit(
        "result",
        **album_result(spotify_kind="track", spotify_id=T3, tracks=[album_result()["tracks"][2]]),
    )
    qtbot.waitUntil(lambda: not page.spotify_card.isHidden())
    assert page._spotify_matches == {}
    assert page.spotify_card.cell_text(0, SpotifyCard.MATCH_COLUMN) == SpotifyCard.NOT_CHECKED


# ── overrides ─────────────────────────────────────────────────────────────────────────────
def test_a_pasted_youtube_link_replaces_the_match(page, runs, qtbot, monkeypatch):
    _analyzed(page, runs, qtbot)
    monkeypatch.setattr(
        page, "_ask_match_link", lambda t: f"https://music.youtube.com/watch?v={VID2}&si=x"
    )
    page.spotify_card.change_button(1).click()
    assert page._spotify_matches[T2].video_id == VID2 and page._spotify_matches[T2].manual
    assert page.spotify_card.cell_text(1, SpotifyCard.MATCH_COLUMN) == "Your link"


@pytest.mark.parametrize(
    "text",
    [
        "https://www.youtube.com/playlist?list=PL0123456789abcdef",
        "https://evil.example/watch?v=dQw4w9WgXcQ",
        f"https://open.spotify.com/track/{T1}",
        "file:///C:/Windows/notepad.exe",
    ],
)
def test_anything_but_a_single_youtube_video_is_refused(page, runs, qtbot, monkeypatch, text):
    _analyzed(page, runs, qtbot)
    monkeypatch.setattr(page, "_ask_match_link", lambda t: text)
    assert page.change_spotify_match(0) is None
    assert T1 not in page._spotify_matches
    assert "YouTube" in page.message_label.text()


def test_a_cancelled_or_empty_override_changes_nothing(page, runs, qtbot, monkeypatch):
    _analyzed(page, runs, qtbot)
    for answer in (None, "   "):
        monkeypatch.setattr(page, "_ask_match_link", lambda t, a=answer: a)
        assert page.change_spotify_match(0) is None
    assert page._spotify_matches == {} and page.message_label.isHidden()


def test_a_pasted_link_wins_over_a_lookup_still_running(page, runs, qtbot, monkeypatch):
    _analyzed(page, runs, qtbot)
    page.check_spotify_matches()
    monkeypatch.setattr(page, "_ask_match_link", lambda t: f"https://youtu.be/{VID2}")
    page.change_spotify_match(0)
    _match_runs(runs)[0].emit("result", **match_result(T1))
    qtbot.waitUntil(lambda: len(_match_runs(runs)) == 3)
    assert page._spotify_matches[T1].video_id == VID2


# ── download ──────────────────────────────────────────────────────────────────────────────
def test_ticked_songs_queue_as_one_group_carrying_their_reviewed_matches(
    page, runs, qtbot, monkeypatch
):
    _analyzed(page, runs, qtbot)
    page.spotify_card.checkbox(1).setChecked(False)
    page.check_spotify_matches()
    _match_runs(runs)[0].emit("result", **match_result(T1))
    qtbot.waitUntil(lambda: T1 in page._spotify_matches)
    monkeypatch.setattr(page, "_ask_match_link", lambda t: f"https://youtu.be/{VID2}")
    page.change_spotify_match(2)

    jobs = page.start_spotify_download()
    specs = [j.spec for j in jobs]
    assert [s.url for s in specs] == [
        f"https://open.spotify.com/track/{T1}",
        f"https://open.spotify.com/track/{T3}",
    ]
    assert all(s.engine == "spotdl" for s in specs)
    assert specs[0].options == spotify.download_options(VID, archive=True)
    assert specs[1].options == spotify.download_options(VID2, archive=True)
    assert [j.title for j in jobs] == [
        "Pitbull, Sensato - Global Warming",
        "Pitbull, Christina Aguilera - Feel This Moment",
    ]
    assert len({j.group_id for j in jobs}) == 1 and jobs[0].group_id
    assert jobs[0].card.details_label.text() == pages.SPOTIFY_PRESET.label

    for spec in specs:
        record = page.store.get(spec.job_id)
        assert record.engine == "spotdl" and record.url == spec.url
        assert "site_login" not in record.options


def test_an_unreviewed_song_is_queued_for_the_worker_to_match(page, runs, qtbot):
    _analyzed(page, runs, qtbot)
    page.spotify_card.set_all_checked(False)
    page.spotify_card.checkbox(1).setChecked(True)
    page.spotify_card.archive_check.setChecked(False)
    (job,) = page.start_spotify_download()
    assert job.spec.options == {"mode": "download", "preset": "spotify_mp3", "archive": False}
    assert job.group_id == ""  # one song is one job, not a group of one


def test_nothing_ticked_means_no_download(page, runs, qtbot):
    _analyzed(page, runs, qtbot)
    page._set_spotify_selection(False)
    assert not page.spotify_card.download_button.isEnabled()
    assert not page.spotify_card.match_button.isEnabled()
    assert page.start_spotify_download() == []


def test_a_spotify_job_restores_after_a_restart_and_downloads_again(page, runs, qtbot):
    _analyzed(page, runs, qtbot)
    page.spotify_card.set_all_checked(False)
    page.spotify_card.checkbox(0).setChecked(True)
    (job,) = page.start_spotify_download()
    # What the next start-up does: unfinished jobs come back paused, and a Spotify one must not
    # be dropped for having a preset the video presets do not know.
    restored = page.restore_unfinished()
    assert [j.spec.job_id for j in restored] == [job.spec.job_id]
    assert restored[0].spec.options == job.spec.options

    record = page.store.get(job.spec.job_id)
    again = page.download_again(record)
    assert again is not None and again.spec.engine == "spotdl"
    assert again.spec.options == job.spec.options
    assert pages.HistoryPage._record_type(record) == "audio"


def test_tooltips_show_uploader_text_literally(page, runs, qtbot):
    hostile = '<img src="file:///C:/x.png"><a href="https://evil.example">click</a>'
    _analyzed(
        page,
        runs,
        qtbot,
        result=album_result(tracks=[track(T1, hostile, ["<b>A</b>"], 85.0)]),
    )
    from PyQt6.QtGui import QTextDocument

    tip = page.spotify_card.table.item(0, 2).toolTip()
    doc = QTextDocument()
    doc.setHtml(tip)
    assert doc.toPlainText() == hostile  # rendered as the characters it is
    assert "<img" not in tip and "<a " not in tip


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("ERROR: unable to download video data: HTTP Error 403: Forbidden", True),
        ("ERROR: [youtube] abc: HTTP Error 403: Forbidden (private video)", False),
        ("http error 404: that track was not found", False),
    ],
)
def test_a_youtube_stream_403_is_retried_but_a_page_403_is_not(message, expected):
    """Seen live: one playlist song failed with the first, then downloaded on a retry."""
    assert pages.is_retryable(message) is expected
