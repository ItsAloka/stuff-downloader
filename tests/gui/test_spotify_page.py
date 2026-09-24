"""The Spotify listing, match review, pasted overrides and the MP3 batch (plan §7)."""

from __future__ import annotations

import pytest

from stuff_downloader.core import protocol, settings, spotify, tools
from stuff_downloader.core.protocol import Event
from stuff_downloader.gui import pages
from stuff_downloader.gui.main_window import MainWindow
from stuff_downloader.gui.widgets import SpotifyCard, TrackTable

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
    """The spotdl engine's MediaResult: a playlist whose entries are the tracks."""
    fields = {
        "spotify_kind": "album",
        "spotify_id": ALBUM,
        "owner": "Pitbull",
        "site": "Spotify",
        "tracks": [
            track(T1, "Global Warming", ["Pitbull", "Sensato"], 85.0, explicit=True),
            track(T2, "Don't Stop the Party", ["Pitbull", "TJR"], 206.0),
            track(T3, "Feel This Moment", ["Pitbull", "Christina Aguilera"], 229.0),
        ],
        "skipped": 0,
        "truncated": False,
        **extra,
    }
    fields["entries"] = fields["tracks"]
    title = fields.pop("title", "Global Warming")
    return protocol.media_result("playlist", ["tracks"], title, SHARE_URL, **fields)


NAMES = {T2: "Don't Stop the Party", T3: "Feel This Moment"}


def match_result(track_id, video_id=VID, duration=88.0, confidence=96.0, method="song"):
    return {
        "kind": "spotify_match",
        "track_id": track_id,
        "video_id": video_id,
        "title": f"Pitbull - {NAMES.get(track_id, 'Global Warming')}",
        "channel": "PitbullVEVO",
        "duration": duration,
        "confidence": confidence,
        "method": method,
        "album": "Global Warming",
    }


def _analyzed(page, runs, qtbot, url=SHARE_URL, result=None):
    page.url_edit.setText(url)
    page.analyze()
    run = runs[-1]
    run.emit("result", **(result or album_result()))
    qtbot.waitUntil(lambda: not page.spotify_card.isHidden())
    return run


def _match_runs(runs):
    return [r for r in runs if r.spec.options.get("mode") == "match"]


def _all_matched(page, video_id=VID2):
    """Paste the owner's own link for every row: certain, so each downloads at once."""
    page._ask_match = lambda t, c: f"https://youtu.be/{video_id}"
    for row in range(page.spotify_card.table.rowCount()):
        page.change_spotify_match(row)


def album_options(video_id, index, **extra):
    """An album link's download: its own album name and the track's position on it."""
    return spotify.download_options(
        video_id, archive=True, album="Global Warming", album_track=index, **extra
    )


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
    assert card.cell_text(0, TrackTable.TITLE) == "Global Warming  🅴"
    assert card.cell_text(1, TrackTable.ARTIST) == "Pitbull, TJR"
    assert card.cell_text(1, TrackTable.LENGTH) == "3:26"
    assert card.cell_text(0, SpotifyCard.MATCH_COLUMN) == SpotifyCard.NOT_CHECKED
    # The disclosure is not optional small print: it is in the card, in plain words.
    text = card.disclosure_label.text()
    assert "never downloaded" in text and "YouTube Music" in text
    assert page.result_card.isHidden() and page.playlist_card.isHidden()
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
    # An album link's own name lets the worker look on that album first (plan §7 item 3).
    assert all(r.spec.options == {"mode": "match", "album": "Global Warming"} for r in lookups)
    card = page.spotify_card
    assert card.cell_text(2, SpotifyCard.MATCH_COLUMN) == "Waiting to check…"
    assert "checking 3 matches" in card.selection_label.text()

    lookups[0].emit("result", **match_result(T1))
    qtbot.waitUntil(lambda: len(_match_runs(runs)) == 3)  # a slot freed, the third started
    assert card.cell_text(0, SpotifyCard.MATCH_COLUMN) == "Pitbull - Global Warming  ·  PitbullVEVO"
    assert card.cell_text(0, SpotifyCard.DIFF_COLUMN) == "+3s"
    # Our own score (title, artist, length 3 s off); how it was found is in the tooltip.
    assert card.cell_text(0, SpotifyCard.SCORE_COLUMN) == "92%"
    assert "as a song" in card.table.item(0, SpotifyCard.SCORE_COLUMN).toolTip()
    # Match lookups are not downloads: no queue rows, nothing in history.
    assert page.jobs == {}


def test_matches_are_not_looked_up_twice(page, runs, qtbot):
    _analyzed(page, runs, qtbot)
    page.spotify_card.set_all_checked(False)
    page.spotify_card.table.set_checked(0, True)
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
    page.spotify_card.table.set_checked(0, True)
    page.check_spotify_matches()
    _match_runs(runs)[0].emit(event, **data)
    qtbot.waitUntil(lambda: not page._match_runs)
    assert expected in page.spotify_card.cell_text(0, SpotifyCard.MATCH_COLUMN)
    assert T1 not in page._spotify_matches


def test_a_suspicious_match_is_flagged(page, runs, qtbot):
    _analyzed(page, runs, qtbot)
    page.spotify_card.set_all_checked(False)
    page.spotify_card.table.set_checked(0, True)
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
        page, "_ask_match", lambda t, c: f"https://music.youtube.com/watch?v={VID2}&si=x"
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
    monkeypatch.setattr(page, "_ask_match", lambda t, c: text)
    assert page.change_spotify_match(0) is None
    assert T1 not in page._spotify_matches
    assert "YouTube" in page.message_label.text()


def test_a_cancelled_or_empty_override_changes_nothing(page, runs, qtbot, monkeypatch):
    _analyzed(page, runs, qtbot)
    for answer in (None, "   "):
        monkeypatch.setattr(page, "_ask_match", lambda t, c, a=answer: a)
        assert page.change_spotify_match(0) is None
    assert page._spotify_matches == {} and page.message_label.isHidden()


def test_a_pasted_link_wins_over_a_lookup_still_running(page, runs, qtbot, monkeypatch):
    _analyzed(page, runs, qtbot)
    page.check_spotify_matches()
    monkeypatch.setattr(page, "_ask_match", lambda t, c: f"https://youtu.be/{VID2}")
    page.change_spotify_match(0)
    _match_runs(runs)[0].emit("result", **match_result(T1))
    qtbot.waitUntil(lambda: len(_match_runs(runs)) == 3)
    assert page._spotify_matches[T1].video_id == VID2


# ── download (plan §7 item 4) ─────────────────────────────────────────────────────────────
def test_ticked_songs_queue_as_one_group_carrying_their_reviewed_matches(
    page, runs, qtbot, monkeypatch
):
    _analyzed(page, runs, qtbot)
    page.spotify_card.table.set_checked(1, False)
    page.check_spotify_matches()
    _match_runs(runs)[0].emit("result", **match_result(T1))
    qtbot.waitUntil(lambda: T1 in page._spotify_matches)
    monkeypatch.setattr(page, "_ask_match", lambda t, c: f"https://youtu.be/{VID2}")
    page.change_spotify_match(2)

    jobs = page.start_spotify_download()
    specs = [j.spec for j in jobs]
    assert [s.url for s in specs] == [
        f"https://open.spotify.com/track/{T1}",
        f"https://open.spotify.com/track/{T3}",
    ]
    assert all(s.engine == "spotdl" for s in specs)
    assert specs[0].options == album_options(VID, 1)
    assert specs[1].options == album_options(VID2, 3)
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


def test_an_unchecked_song_is_looked_up_first_then_downloads_when_certain(page, runs, qtbot):
    _analyzed(page, runs, qtbot)
    page.spotify_card.set_all_checked(False)
    page.spotify_card.table.set_checked(1, True)
    page.spotify_card.archive_check.setChecked(False)
    assert page.start_spotify_download() == []  # nothing yet: its match is being looked up
    (lookup,) = _match_runs(runs)
    assert lookup.spec.url.endswith(T2)
    lookup.emit("result", **match_result(T2, duration=207.0))
    qtbot.waitUntil(lambda: bool(page.jobs))
    (job,) = page.jobs.values()
    assert job.spec.options == spotify.download_options(
        VID, archive=False, album="Global Warming", album_track=2
    )
    assert job.group_id == ""  # one song is one job, not a group of one
    assert page.start_spotify_download() == []  # already queued, never twice


def test_certain_songs_download_while_uncertain_ones_wait_for_review(page, runs, qtbot):
    """Plan §7 item 4, in the order a real batch goes: nothing blocks the certain songs."""
    _analyzed(page, runs, qtbot)
    assert page.start_spotify_download() == []
    group_id = next(iter(page._groups))
    assert page._groups[group_id].total == 3
    first, second = _match_runs(runs)
    first.emit("result", **match_result(T1, duration=86.0))
    qtbot.waitUntil(lambda: len(page.jobs) == 1)  # certain: queued the moment it landed
    # Found only as a video: flagged, held back, and the batch counts one song fewer.
    second.emit("result", **match_result(T2, duration=206.0, method="video"))
    qtbot.waitUntil(lambda: T2 in page._spotify_held)
    assert page._groups[group_id].total == 2
    third = _match_runs(runs)[2]
    third.emit("result", **match_result(T3, duration=229.5))
    qtbot.waitUntil(lambda: len(page.jobs) == 2)
    assert {j.group_id for j in page.jobs.values()} == {group_id}
    assert {j.spec.url.rsplit("/", 1)[1] for j in page.jobs.values()} == {T1, T3}

    card = page.spotify_card
    assert card.cell_text(1, SpotifyCard.SCORE_COLUMN).startswith("⚠")
    assert card.change_button(1).text() == "⚠ Review…"
    assert card.change_button(0).text() == "Change…"
    assert not card.uncertain_button.isHidden()
    assert card.uncertain_button.text() == "⬇  Download 1 uncertain anyway"
    assert "1 uncertain match waits for review" in card.uncertain_label.text()

    (held,) = page.download_uncertain_anyway()
    assert held.spec.url.endswith(T2) and held.spec.options["video_id"] == VID
    assert held.spec.options["album"] == "Global Warming"
    assert card.uncertain_button.isHidden() and card.uncertain_label.isHidden()
    assert page.download_uncertain_anyway() == []


def test_reviewing_a_held_song_downloads_the_owners_choice(page, runs, qtbot, monkeypatch):
    _analyzed(page, runs, qtbot)
    page.spotify_card.set_all_checked(False)
    page.spotify_card.table.set_checked(0, True)
    page.start_spotify_download()
    _match_runs(runs)[0].emit("result", **match_result(T1, method="video"))
    qtbot.waitUntil(lambda: T1 in page._spotify_held)
    assert page.jobs == {}
    monkeypatch.setattr(page, "_ask_match", lambda t, c: f"https://youtu.be/{VID2}")
    page.spotify_card.change_button(0).click()
    (job,) = page.jobs.values()
    assert job.spec.options["video_id"] == VID2
    assert page.spotify_card.uncertain_label.isHidden()


def test_an_uncertain_match_known_before_download_waits_at_once(page, runs, qtbot):
    _analyzed(page, runs, qtbot)
    page.check_spotify_matches()
    lookups = _match_runs(runs)
    lookups[0].emit("result", **match_result(T1, duration=86.0))
    lookups[1].emit("result", **match_result(T2, duration=400.0))  # far too long
    qtbot.waitUntil(lambda: T2 in page._spotify_matches)
    _match_runs(runs)[2].emit("result", **match_result(T3, duration=229.0))
    qtbot.waitUntil(lambda: T3 in page._spotify_matches)
    jobs = page.start_spotify_download()
    assert [j.spec.url.rsplit("/", 1)[1] for j in jobs] == [T1, T3]
    assert "1 uncertain song waits for review" in page.message_label.text()
    assert T2 in page._spotify_held


def test_a_lookup_that_finds_nothing_leaves_the_batch_without_it(page, runs, qtbot):
    _analyzed(page, runs, qtbot)
    page.spotify_card.table.set_checked(2, False)
    page.start_spotify_download()
    group_id = next(iter(page._groups))
    first, second = _match_runs(runs)
    first.emit("error", code="no_match", message="No matching song")
    second.emit("result", **match_result(T2, duration=206.0))
    qtbot.waitUntil(lambda: len(page.jobs) == 1)
    assert page._groups[group_id].total == 1
    assert "Change…" in page.spotify_card.cell_text(0, SpotifyCard.MATCH_COLUMN)


def test_a_new_link_drops_the_songs_still_waiting_for_a_match(page, runs, qtbot):
    _analyzed(page, runs, qtbot)
    page.start_spotify_download()
    assert len(page._groups) == 1
    page.url_edit.setText(f"https://open.spotify.com/track/{T3}")
    page.analyze()
    # The queue group had no song yet: it goes with the listing it belonged to.
    assert page._groups == {} and page._spotify_pending == {}


def test_nothing_ticked_means_no_download(page, runs, qtbot):
    _analyzed(page, runs, qtbot)
    page._set_spotify_selection(False)
    assert not page.spotify_card.download_button.isEnabled()
    assert not page.spotify_card.match_button.isEnabled()
    assert page.start_spotify_download() == []


def test_a_spotify_job_restores_after_a_restart_and_downloads_again(page, runs, qtbot):
    _analyzed(page, runs, qtbot)
    _all_matched(page)
    page.spotify_card.set_all_checked(False)
    page.spotify_card.table.set_checked(0, True)
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

    tip = page.spotify_card.table.item(0, TrackTable.TITLE).toolTip()
    doc = QTextDocument()
    doc.setHtml(tip)
    assert doc.toPlainText().splitlines()[0] == hostile  # rendered as the characters it is
    assert "<img" not in tip and "<a " not in tip


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("ERROR: unable to download video data: HTTP Error 403: Forbidden", True),
        ("ERROR: [youtube] abc: HTTP Error 403: Forbidden (private video)", False),
        ("ERROR: [youtube] abc: Unable to download webpage: HTTP Error 403: Forbidden", False),
        ("ERROR: fragment 3: HTTP Error 403: Forbidden", True),
        ("ERROR: [generic] page#fragment: Unable to download webpage: HTTP Error 403", False),
        ("ERROR: HTTP Error 403: Forbidden (fragment of a private page)", False),
        ("ERROR: fragment 12 not found, unable to continue", True),
        ("ERROR: Did not get any data blocks", True),
        ("ERROR: unable to download webpage: HTTP Error 503", True),
        ("http error 404: that track was not found", False),
    ],
)
def test_a_youtube_stream_403_is_retried_but_a_page_403_is_not(message, expected):
    """Seen live: one playlist song failed with the first, then downloaded on a retry."""
    assert pages.is_retryable(message) is expected


# ── R5 §5.5: a Spotify title is edited in place and names the file only ─────────────────────
def test_every_spotify_title_is_editable_in_place(page, runs, qtbot):
    from PyQt6.QtCore import Qt

    from stuff_downloader.gui.widgets import TrackTable

    _analyzed(page, runs, qtbot)
    table = page.spotify_card.table
    header = table.horizontalHeaderItem(TrackTable.TITLE).text()
    assert header == "Title (click to edit)"
    for row in range(table.rowCount()):
        assert table.item(row, TrackTable.TITLE).flags() & Qt.ItemFlag.ItemIsEditable


def test_an_edited_spotify_title_reaches_its_job_and_nothing_else(page, runs, qtbot):
    from stuff_downloader.gui.widgets import TrackTable

    _analyzed(page, runs, qtbot)
    _all_matched(page)
    table = page.spotify_card.table
    table.item(0, TrackTable.TITLE).setText("My name  🅴")  # the explicit badge is not a name
    table.item(1, TrackTable.TITLE).setText("  ")
    table.item(2, TrackTable.TITLE).setText("a:b")
    assert table.edited_title(0) == "My name"
    specs = [j.spec for j in page.start_spotify_download()]
    assert specs[0].options["edited_title"] == "My name"
    assert "edited_title" not in specs[1].options
    assert specs[2].options["edited_title"] == "a_b"
    assert specs[1].options == album_options(VID2, 2)


def test_the_spotify_worker_names_the_file_from_the_edit_but_validates_it():
    from stuff_downloader_worker.engines import spotdl
    from stuff_downloader_worker.engines.base import EngineError

    opts = spotify.download_options(archive=False, edited_title="My name")
    assert spotdl.parse_download_options(opts) == (None, False)
    assert spotdl.edited_stem(opts) == "My name"
    assert spotdl.edited_stem(spotify.download_options()) is None
    with pytest.raises(EngineError, match="edited_title"):
        spotdl.parse_download_options({**opts, "edited_title": "x" * 301})


# ── Spotify's own art (plan §5.6a, §7 item 2; R5 carry-over) ─────────────────────────────
ART = "https://i.scdn.co/image/ab67616d00001e02" + "c" * 24
COVER = "https://i.scdn.co/image/ab67706f00000002" + "d" * 24


class _Fetch:
    """The page loader's fetcher: records every URL, answers with a small green picture."""

    def __init__(self):
        self.calls = []

    def __call__(self, url):
        from PyQt6.QtCore import QBuffer, QByteArray, QIODevice
        from PyQt6.QtGui import QColor, QImage

        self.calls.append(url)
        image = QImage(64, 64, QImage.Format.Format_RGB32)
        image.fill(QColor("#00ff00"))
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        return bytes(data)


def _shown(page, qtbot):
    window = page.window()
    window.resize(1000, 700)
    window.show()
    qtbot.waitExposed(window)
    return window


def test_album_rows_and_header_show_spotifys_cover_before_any_match_check(page, runs, qtbot):
    fetch = _Fetch()
    page.thumbs.fetcher = fetch
    _shown(page, qtbot)
    tracks = [dict(t, art=ART) for t in album_result()["tracks"]]
    _analyzed(page, runs, qtbot, result=album_result(tracks=tracks, cover=ART))
    card = page.spotify_card
    qtbot.waitUntil(lambda: all(card.table.has_art(r) for r in range(3)), timeout=3000)
    assert card.header.has_cover
    assert set(fetch.calls) == {ART}  # one album cover, shared by every row
    assert _match_runs(runs) == []  # before any match was looked up


def test_playlist_rows_look_up_their_own_art_lazily_through_oembed(page, runs, qtbot):
    fetch = _Fetch()
    page.thumbs.fetcher = fetch
    _shown(page, qtbot)
    tracks = [track(f"{i:022d}", f"Song {i}", ["A"], 200.0, album="") for i in range(40)]
    playlist = album_result(spotify_kind="playlist", tracks=tracks, cover=COVER)
    _analyzed(page, runs, qtbot, result=playlist)
    qtbot.waitUntil(lambda: len(fetch.calls) > 1, timeout=3000)
    qtbot.wait(100)
    rows = [u for u in fetch.calls if u != COVER]
    assert COVER in fetch.calls and page.spotify_card.header.has_cover
    assert 0 < len(set(rows)) < 40  # only the rows on screen
    assert all(u.startswith("https://open.spotify.com/oembed?url=") for u in rows)
    assert not any("ytimg" in u for u in fetch.calls)


def test_the_art_stays_after_matching_and_follows_into_the_queue_and_history(
    page, runs, qtbot
):
    from PyQt6.QtGui import QColor

    fetch = _Fetch()
    page.thumbs.fetcher = fetch
    _shown(page, qtbot)
    tracks = [dict(t, art=ART) for t in album_result()["tracks"]]
    _analyzed(page, runs, qtbot, result=album_result(tracks=tracks, cover=ART))
    card = page.spotify_card
    qtbot.waitUntil(lambda: card.table.has_art(0), timeout=3000)
    card.set_all_checked(False)
    card.table.set_checked(0, True)
    page.check_spotify_matches()
    _match_runs(runs)[0].emit("result", **match_result(T1))
    qtbot.waitUntil(lambda: T1 in page._spotify_matches)
    assert card.art(0).pixelColor(10, 10) == QColor("#00ff00")  # still Spotify's picture
    (job,) = page.start_spotify_download()
    thumb = job.card.thumb.pixmap().toImage()
    assert thumb.pixelColor(thumb.width() // 2, thumb.height() // 2) == QColor("#00ff00")
    # History keeps Spotify's picture too, never the YouTube match's.
    assert page.store.get(job.spec.job_id).thumb_url == ART
    assert not any("ytimg" in u for u in fetch.calls)


def test_a_playlist_song_takes_its_oembed_lookup_into_history(page, runs, qtbot):
    _analyzed(page, runs, qtbot)
    listing = spotify.parse_listing(album_result(spotify_kind="playlist"))
    page._spotify = listing
    page._spotify_art_urls = [t.art_url for t in listing.tracks]
    _all_matched(page)
    page.spotify_card.set_all_checked(False)
    page.spotify_card.table.set_checked(1, True)
    (job,) = page.start_spotify_download()
    assert page.store.get(job.spec.job_id).thumb_url == spotify.oembed_url(T2)
