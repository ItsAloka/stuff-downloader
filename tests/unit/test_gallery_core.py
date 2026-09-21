"""M4 core: gallery routing, the per-site queue limit, the gallery preset and listing parsing."""

from __future__ import annotations

import base64

import pytest

from stuff_downloader.core import gallery, presets, router
from stuff_downloader.core.protocol import JobSpec
from stuff_downloader.core.scheduler import Scheduler


# ── routing (plan §5.3 step 3) ───────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "url",
    [
        "https://www.instagram.com/p/C0ffee123/",
        "https://instagram.com/p/C0ffee123/?img_index=2",
        "https://www.instagram.com/stories/some.user/3141592653/",
        "https://www.instagram.com/stories/highlights/17900000000000000/",
        "https://www.instagram.com/some.user/",
        "https://www.tiktok.com/@someone/photo/7300000000000000000",
        "https://www.facebook.com/photo/?fbid=10150000000000000",
        "https://www.facebook.com/photo.php?fbid=1",
        "https://www.facebook.com/media/set/?set=a.10150000000000000",
        "https://www.facebook.com/someone/photos",
        "https://x.com/someone/media",
        "https://twitter.com/someone/status/1700000000000000000/photo/1",
    ],
)
def test_photo_links_on_social_sites_go_to_gallery_dl(url):
    route = router.route(url)
    assert route.kind == "gallery" and route.is_gallery and route.ok
    assert route.engine == "gallerydl"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.instagram.com/reel/C0ffee123/",
        "https://www.instagram.com/explore/",
        "https://www.tiktok.com/@someone/video/7300000000000000000",
        "https://x.com/someone/status/1700000000000000000",
        "https://www.facebook.com/watch/?v=1",
        "https://vimeo.com/123456789",
    ],
)
def test_video_pages_stay_with_ytdlp(url):
    assert router.route(url).engine == "ytdlp"


def test_youtube_and_direct_files_are_unchanged():
    assert router.route("https://youtu.be/dQw4w9WgXcQ").kind == "youtube"
    assert router.route("https://cdn.example.com/a.jpg").kind == "file"


def test_gallery_links_keep_the_url_validation():
    assert not router.route("https://user:pw@www.instagram.com/p/x/").ok
    assert router.route("https://www.instagram.com/p/x/#frag").url == (
        "https://www.instagram.com/p/x/"
    )


@pytest.mark.parametrize(
    ("url", "group"),
    [
        ("https://www.instagram.com/p/x/", "instagram"),
        ("https://m.tiktok.com/@a/photo/1", "tiktok"),
        ("https://twitter.com/a/media", "x"),
        ("https://x.com/a/status/1", "x"),
        ("https://www.facebook.com/photo/?fbid=1", "facebook"),
        ("https://vimeo.com/1", ""),
        ("not a url", ""),
    ],
)
def test_social_group(url, group):
    assert router.social_group(url) == group


# ── the gallery preset ───────────────────────────────────────────────────────────────────
def test_gallery_options_are_exact_sorted_positions():
    assert presets.gallery_download_options([3, 1, 3]) == {
        "mode": "download",
        "preset": "gallery_original",
        "items": [1, 3],
    }
    assert presets.gallery_download_options([1], archive=True)["archive"] is True
    assert presets.get("gallery_original") is presets.GALLERY_PRESET
    assert presets.GALLERY_PRESET not in presets.PRESETS


@pytest.mark.parametrize("items", [[], [0], [501], [True]])
def test_gallery_options_refuse_bad_positions(items):
    with pytest.raises(ValueError):
        presets.gallery_download_options(items)


# ── per-site queue limit (plan §6.1) ─────────────────────────────────────────────────────
def _spec(job_id, url):
    return JobSpec(job_id, "gallerydl", url, ".", {})


def test_one_job_per_social_site_while_others_keep_flowing():
    started = []
    sched = Scheduler(
        lambda spec: started.append(spec.job_id),
        max_concurrent=3,
        group_of=lambda spec: router.social_group(spec.url),
    )
    sched.submit_all(
        [
            _spec("ig1", "https://www.instagram.com/p/1/"),
            _spec("ig2", "https://www.instagram.com/p/2/"),
            _spec("x1", "https://x.com/a/media"),
            _spec("yt", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
        ]
    )
    assert started == ["ig1", "x1", "yt"]  # ig2 waits, but does not block the rest
    assert sched.queued_ids() == ["ig2"]
    sched.finished("x1")
    assert started == ["ig1", "x1", "yt"]  # a free slot is not enough while ig1 runs
    sched.finished("ig1")
    assert started[-1] == "ig2"


def test_without_a_group_function_nothing_changes():
    started = []
    sched = Scheduler(lambda spec: started.append(spec.job_id), max_concurrent=2)
    sched.submit_all(
        [_spec("a", "https://www.instagram.com/p/1/"), _spec("b", "https://www.instagram.com/p/2/")]
    )
    assert started == ["a", "b"]


def test_a_broken_group_function_does_not_stall_the_queue():
    started = []

    def broken(spec):
        raise RuntimeError("oops")

    sched = Scheduler(lambda spec: started.append(spec.job_id), max_concurrent=2, group_of=broken)
    sched.submit_all([_spec("a", "https://x.com/a/media"), _spec("b", "https://x.com/b/media")])
    assert started == ["a", "b"]


def test_a_cancelled_social_job_frees_its_site():
    started = []
    sched = Scheduler(
        lambda spec: started.append(spec.job_id),
        max_concurrent=3,
        group_of=lambda spec: router.social_group(spec.url),
    )
    sched.submit_all([_spec("a", "https://x.com/a/media"), _spec("b", "https://x.com/b/media")])
    assert sched.cancel("a") is True
    sched.finished("a")
    assert started == ["a", "b"]


# ── the GUI's own parse of a gallery listing ─────────────────────────────────────────────
def test_parse_keeps_only_well_formed_rows():
    jpeg = base64.b64encode(b"\xff\xd8img").decode()
    listing = gallery.parse(
        {
            "title": "  A   day\nout ",
            "uploader": "someone",
            "extractor": "Instagram",
            "truncated": True,
            "items": [
                {"index": 2, "kind": "video", "ext": "mp4"},
                {"index": 1, "kind": "image", "ext": "jpg", "width": 10, "height": 20,
                 "thumbnail": {"data": jpeg}},
                {"index": 1, "kind": "image"},  # duplicate position
                {"index": 0, "kind": "image"},
                {"index": "3", "kind": "image"},
                {"index": 4, "kind": "script", "ext": "../x", "url": "https://evil.example/"},
                {"index": 5, "thumbnail": {"data": "not base64!!"}},
                "junk",
            ],
        }
    )  # fmt: skip
    assert listing.title == "A day out" and listing.truncated
    assert [i.index for i in listing.items] == [1, 2, 4, 5]
    first = listing.items[0]
    assert first.preview == b"\xff\xd8img" and first.label == "#1  JPG  10×20"
    assert listing.items[2].kind == "file" and listing.items[2].ext == ""
    assert listing.items[3].preview == b""
    assert not hasattr(listing.items[2], "url")


def test_parse_tolerates_junk():
    listing = gallery.parse({"items": "nope"})
    assert listing.items == () and listing.title == "Gallery"
