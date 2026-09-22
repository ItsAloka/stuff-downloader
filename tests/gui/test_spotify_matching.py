"""Spotify match review (item 9): our own score, uncertain badges and the Change… dialog."""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QDialogButtonBox
from test_spotify_page import (  # noqa: F401  (fixtures)
    T1,
    T2,
    T3,
    VID,
    VID2,
    _analyzed,
    _match_runs,
    match_result,
    page,
    runs,
)

from stuff_downloader.core import spotify
from stuff_downloader.gui.widgets import MatchDialog, SpotifyCard

VID3 = "abcdefghijk"
TRACK = spotify.SpotifyTrack(T1, 1, "Global Warming", ("Pitbull", "Sensato"), duration=85.0)


# ── scoring (core) ───────────────────────────────────────────────────────────────────────
def test_the_right_recording_scores_high_and_is_certain():
    score = spotify.match_score(TRACK, "Global Warming", "Pitbull", 86.0)
    assert score >= 95
    m = spotify.Match(T1, VID, duration_diff=1.0, score=score)
    assert not spotify.is_uncertain(m)


@pytest.mark.parametrize(
    ("title", "channel", "duration"),
    [
        ("Global Warming (Sped Up)", "Nightcore Hub", 70.0),  # other artist, other length
        ("Timber", "Pitbull", 204.0),  # the artist's other song
        ("Global Warming", "Pitbull", 140.0),  # right name, very different length
    ],
)
def test_wrong_recordings_are_uncertain(title, channel, duration):
    score = spotify.match_score(TRACK, title, channel, duration)
    diff = spotify.duration_diff(duration, TRACK.duration)
    assert spotify.is_uncertain(spotify.Match(T1, VID, duration_diff=diff, score=score))


def test_the_threshold_edges_are_exactly_as_stated():
    at = spotify.Match(T1, VID, duration_diff=spotify.UNCERTAIN_DIFF, score=spotify.UNCERTAIN_SCORE)
    assert not spotify.is_uncertain(at)  # "under 70" and "more than 10 s"
    over = spotify.Match(T1, VID, duration_diff=spotify.UNCERTAIN_DIFF + 0.1, score=99.0)
    under = spotify.Match(T1, VID, duration_diff=0.0, score=spotify.UNCERTAIN_SCORE - 0.1)
    assert spotify.is_uncertain(over) and spotify.is_uncertain(under)
    assert "70%" in spotify.UNCERTAIN_RULE and "10 s" in spotify.UNCERTAIN_RULE


def test_the_owners_own_choice_is_never_flagged():
    chosen = spotify.Match(T1, VID, duration_diff=200.0, score=5.0, manual=True)
    assert not spotify.is_uncertain(chosen)


def test_a_feat_part_and_official_audio_do_not_lower_the_title_score():
    plain = spotify.match_score(TRACK, "Global Warming", "Pitbull", 85.0)
    dressed = spotify.match_score(TRACK, "Global Warming (feat. Sensato) [Official Audio]",
                                  "Pitbull - Topic", 85.0)
    assert dressed >= spotify.UNCERTAIN_SCORE and plain >= dressed


def test_candidates_are_validated_and_capped():
    rows = [
        {"video_id": VID2, "title": "A", "channel": "B", "duration": 90},
        {"video_id": VID2, "title": "duplicate"},
        {"video_id": "../../etc", "title": "bad id"},
        {"video_id": VID3, "title": "x" * 5000, "duration": -5},
        "not a row",
        *({"video_id": f"{i:011d}"} for i in range(20)),
    ]
    got = spotify.parse_candidates({"candidates": rows}, TRACK)
    assert len(got) == spotify.MAX_CANDIDATES
    assert [c.video_id for c in got[:2]] == [VID2, VID3]
    assert len(got[1].title) <= spotify.MAX_TEXT and got[1].duration is None
    assert spotify.parse_candidates({"candidates": "nope"}, TRACK) == ()


def test_a_parsed_match_is_scored_here_never_from_the_payload():
    data = match_result(T1, duration=85.0) | {"score": 100, "title": "Timber", "channel": "X"}
    m = spotify.parse_match(data, TRACK)
    assert m.score < spotify.UNCERTAIN_SCORE and spotify.is_uncertain(m)


# ── the card ─────────────────────────────────────────────────────────────────────────────
def _with_matches(page, runs, qtbot, *results):  # noqa: F811
    _analyzed(page, runs, qtbot)
    page.check_spotify_matches()
    for result in results:
        pending = [r for r in _match_runs(runs) if r.started and not getattr(r, "done", False)]
        run = next(r for r in pending if r.spec.url.endswith(result["track_id"]))
        run.done = True
        run.emit("result", **result)
        qtbot.waitUntil(lambda tid=result["track_id"]: tid in page._spotify_matches)


def _wrong(track_id):
    return match_result(track_id, video_id=VID2, duration=300.0) | {
        "title": "Something Else",
        "channel": "Nobody",
        "candidates": [
            {"video_id": VID3, "title": "Don't Stop the Party", "channel": "Pitbull, TJR",
             "duration": 206.0},
        ],
    }


def test_uncertain_rows_get_a_badge_and_the_header_counts_them_before_download(
    page, runs, qtbot  # noqa: F811
):
    card = page.spotify_card
    assert card.uncertain_label.isHidden()
    _with_matches(page, runs, qtbot, match_result(T1, duration=85.0), _wrong(T2))
    assert not card.cell_text(0, SpotifyCard.SCORE_COLUMN).startswith("⚠")
    assert card.cell_text(1, SpotifyCard.SCORE_COLUMN).startswith("⚠")
    assert "Uncertain" in card.table.item(1, SpotifyCard.SCORE_COLUMN).toolTip()
    assert not card.uncertain_label.isHidden()
    assert card.uncertain_label.text().startswith("⚠  1 uncertain match — ")
    assert spotify.UNCERTAIN_RULE in card.uncertain_label.text()
    assert page.jobs == {}  # all of this happens before anything downloads


def test_picking_a_candidate_fixes_the_row_and_the_download_uses_it(
    page, runs, qtbot, monkeypatch  # noqa: F811
):
    _with_matches(page, runs, qtbot, _wrong(T2))
    seen = {}

    def pick(track, candidates):
        seen["candidates"] = candidates
        return next(c for c in candidates if c.video_id == VID3)

    monkeypatch.setattr(page, "_ask_match", pick)
    match = page.change_spotify_match(1)
    # The automatic pick is offered first, then the looked-up alternatives.
    assert [c.video_id for c in seen["candidates"]] == [VID2, VID3]
    assert match.video_id == VID3 and match.manual
    card = page.spotify_card
    assert "(your choice)" in card.cell_text(1, SpotifyCard.MATCH_COLUMN)
    assert not card.cell_text(1, SpotifyCard.SCORE_COLUMN).startswith("⚠")
    assert card.uncertain_label.isHidden()

    card.set_all_checked(False)
    card.checkbox(1).setChecked(True)
    (job,) = page.start_spotify_download()
    assert job.spec.url.endswith(T2)
    assert job.spec.options == spotify.download_options(VID3, archive=True)

    # A second Change… still offers the same results to go back to.
    monkeypatch.setattr(page, "_ask_match", lambda t, c: seen.update(again=c))
    page.change_spotify_match(1)
    assert [c.video_id for c in seen["again"]] == [VID2, VID3]


def test_the_existing_warning_colour_is_kept(page, runs, qtbot):  # noqa: F811
    _with_matches(page, runs, qtbot, _wrong(T2))
    item = page.spotify_card.table.item(1, SpotifyCard.DIFF_COLUMN)
    assert item.foreground().color().name() == "#e0a040"


# ── the dialog ───────────────────────────────────────────────────────────────────────────
def _dialog(qtbot, candidates):
    track = spotify.SpotifyTrack(T3, 3, "Feel This Moment", ("Pitbull",), duration=229.0)
    dialog = MatchDialog(track, candidates)
    qtbot.addWidget(dialog)
    return dialog


CANDS = (
    spotify.Candidate(VID2, "Feel This Moment", "Pitbull", 230.0),
    spotify.Candidate(VID3, "<b>Feel This Moment (Live)</b>", "Fan", 300.0),
)


def test_the_dialog_lists_candidates_with_length_diff_and_score(qtbot):
    dialog = _dialog(qtbot, CANDS)
    table = dialog.table
    assert [table.item(0, c).text() for c in range(5)] == [
        "Feel This Moment", "Pitbull", "3:50", "+1s", table.item(0, 4).text()
    ]
    assert not table.item(0, 4).text().startswith("⚠")
    assert table.item(1, 4).text().startswith("⚠")
    assert "&lt;b&gt;" in table.item(1, 0).toolTip()  # site text stays text
    assert dialog.choice() is None  # nothing picked yet


def test_the_dialog_returns_the_selected_candidate_or_a_pasted_link(qtbot):
    dialog = _dialog(qtbot, CANDS)
    dialog.table.selectRow(1)
    assert dialog.choice() == CANDS[1]
    dialog.link_edit.setText(f"  https://youtu.be/{VID}  ")
    assert dialog.choice() == f"https://youtu.be/{VID}"  # a pasted link wins


def test_the_dialog_without_candidates_still_takes_a_link(qtbot):
    dialog = _dialog(qtbot, ())
    assert dialog.table.isHidden()
    ok = dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok)
    assert ok.text() == "Use this recording"
