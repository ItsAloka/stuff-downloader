"""R4 (plan §6): the no-login social extractor, against canned site responses. No network."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from stuff_downloader.core import errors
from stuff_downloader_worker.engines import ENGINES, get_engine, social
from stuff_downloader_worker.engines.base import EngineError
from stuff_downloader_worker.protocol import JobSpec, validate_media_result

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
IMG = (200, PNG, {"content-type": "image/png"})


class FakeClient:
    """Answers fetch/stream from a {url-prefix: (status, body, headers)} table, in order."""

    def __init__(self, table: dict[str, tuple[int, bytes, dict[str, str]]]):
        self.table = table
        self.cookies: dict[str, str] = {}
        self.fetched: list[str] = []
        self.streamed: list[str] = []

    def _answer(self, url: str):
        for prefix, answer in self.table.items():
            if url.startswith(prefix):
                return answer
        return 404, b"", {}

    def fetch(self, url, headers=None, data=None, limit=social.MAX_JSON_BYTES):
        self.fetched.append(url)
        status, body, hdrs = self._answer(url)
        return social.Response(status, url, body, hdrs)

    def stream(self, url, dest: Path, emit, headers):
        self.streamed.append(url)
        status, body, _ = self._answer(url)
        if status != 200:
            raise EngineError("download_error", f"http error {status}")
        dest.write_bytes(body)
        return len(body)


def _json(data) -> tuple[int, bytes, dict[str, str]]:
    return 200, json.dumps(data).encode(), {"content-type": "application/json"}


def _run(client, url, output_dir=".", /, **options):
    spec = JobSpec("job1", "social", url, output_dir, options or {"mode": "analyze"})
    return social.SocialEngine(client).download(spec, lambda t, d: None)


# ── X ───────────────────────────────────────────────────────────────────────────────────────
TWEET = "https://x.com/AnimeePost/status/2102612420057292860"


def _tweet(n_photos=4, video=False):
    media = [
        {
            "type": "photo",
            "media_url_https": f"https://pbs.twimg.com/media/P{i}.jpg",
            "original_info": {"width": 2048, "height": 1536},
        }
        for i in range(n_photos)
    ]
    if video:
        variants = [
            {"content_type": "application/x-mpegURL", "url": "https://video.twimg.com/a.m3u8"},
            {"content_type": "video/mp4", "bitrate": 832000, "url": "https://video.twimg.com/lo.mp4"},
            {"content_type": "video/mp4", "bitrate": 2176000, "url": "https://video.twimg.com/hi.mp4"},
        ]  # fmt: skip
        media.append(
            {
                "type": "video",
                "media_url_https": "https://pbs.twimg.com/ext_tw_video_thumb/1/V.jpg",
                "original_info": {"width": 1280, "height": 720},
                "video_info": {"variants": variants},
            }
        )
    return {"text": "four photos", "user": {"screen_name": "AnimeePost"}, "mediaDetails": media}


def test_syndication_token_has_the_embed_shape():
    # ((id / 1e15) * pi).toString(36) with "0" and "." removed, as X's own embed computes it.
    token = social.syndication_token("2102612420057292860")
    assert token and "0" not in token and "." not in token
    assert token.isalnum() and token == token.lower()


def test_an_x_post_with_four_photos_is_a_gallery_at_original_size():
    client = FakeClient(
        {"https://cdn.syndication.twimg.com/": _json(_tweet()), "https://pbs.twimg.com/": IMG}
    )
    result = validate_media_result(_run(client, TWEET))
    assert result["kind"] == "gallery" and result["tabs"] == ["gallery"]
    assert [i["index"] for i in result["items"]] == [1, 2, 3, 4]
    assert all(i["kind"] == "image" and i["width"] == 2048 for i in result["items"])
    assert result["uploader"] == "AnimeePost" and result["site"] == "X"
    assert "http" not in json.dumps(result["items"])  # previews are bytes, never links
    assert result["webpage"] == TWEET
    # The grid previews ask for the small rendition; the download asks for name=orig.
    assert all("name=small" in u for u in client.fetched if u.startswith("https://pbs"))


def test_a_mixed_x_post_keeps_both_kinds_and_downloads_by_position(tmp_path):
    client = FakeClient(
        {
            "https://cdn.syndication.twimg.com/": _json(_tweet(2, video=True)),
            "https://pbs.twimg.com/": IMG,
            "https://video.twimg.com/": (200, b"mp4", {}),
        }
    )
    result = _run(client, TWEET)
    assert [i["kind"] for i in result["items"]] == ["image", "image", "video"]
    done = _run(
        client, TWEET, str(tmp_path), mode="download", preset="gallery_original", items=[1, 3]
    )
    assert client.streamed == [
        "https://pbs.twimg.com/media/P0?format=jpg&name=orig",
        "https://video.twimg.com/hi.mp4",  # the highest-bitrate mp4, never the playlist
    ]
    assert [Path(f).suffix for f in done["files"]] == [".jpg", ".mp4"]
    assert all(Path(f).parent == tmp_path for f in done["files"])


def test_the_skip_already_downloaded_box_skips_items_by_their_position_name(tmp_path):
    client = FakeClient(
        {"https://cdn.syndication.twimg.com/": _json(_tweet(3)), "https://pbs.twimg.com/": IMG}
    )
    opts = {"mode": "download", "preset": "gallery_original", "archive": True}
    first = _run(client, TWEET, str(tmp_path), items=[1, 2], **opts)
    assert sorted(Path(f).name for f in first["files"]) == [
        "four photos 1.jpg",
        "four photos 2.jpg",
    ]
    client.streamed.clear()
    again = _run(client, TWEET, str(tmp_path), items=[1, 2, 3], **opts)
    assert client.streamed == ["https://pbs.twimg.com/media/P2?format=jpg&name=orig"]
    assert [Path(f).name for f in again["files"]] == ["four photos 3.jpg"]
    client.streamed.clear()
    done = _run(client, TWEET, str(tmp_path), items=[1, 2, 3], **opts)
    assert done["skipped"] is True and client.streamed == []


def test_a_single_video_post_steps_aside_for_ytdlp():
    client = FakeClient({"https://cdn.syndication.twimg.com/": _json(_tweet(0, video=True))})
    with pytest.raises(EngineError) as info:
        _run(client, TWEET)
    assert info.value.code == "unsupported"  # the router's chain moves on to yt-dlp


def test_a_protected_tweet_says_login_required_without_saying_video():
    client = FakeClient(
        {"https://cdn.syndication.twimg.com/": _json({"__typename": "TweetTombstone"})}
    )
    with pytest.raises(EngineError) as info:
        _run(client, TWEET)
    assert errors.needs_site_login(info.value.code, info.value.message)
    friendly = errors.friendly_message(info.value.code, info.value.message)
    assert "video" not in friendly.lower() and "not public" in friendly


# ── Instagram ───────────────────────────────────────────────────────────────────────────────
IG = "https://www.instagram.com/p/DdHWXPMyCL0/"


def _embed_srcset() -> bytes:
    img = (
        '<img class="EmbeddedMediaImage" alt="Instagram post shared by @8garak" '
        'src="https://scontent.cdninstagram.com/a_640.jpg" srcset="'
        "https://scontent.cdninstagram.com/a_640.jpg?x=1&amp;y=2 640w,"
        'https://scontent.cdninstagram.com/a_1440.jpg?x=1&amp;y=2 1440w">'
    )
    return ('{"isSidecar":false,"contextJSON":null}' + img).encode()


def test_the_owners_instagram_photo_is_one_image_at_the_largest_size(tmp_path):
    client = FakeClient(
        {IG + "embed/captioned/": (200, _embed_srcset(), {}), "https://scontent.": IMG}
    )
    result = validate_media_result(_run(client, IG))
    assert result["kind"] == "image" and result["tabs"] == ["image"]
    assert result["image_rows"][0]["id"] == "i:orig" and result["uploader"] == "8garak"
    assert result["items"][0]["width"] == 1440 and result["preview"]
    done = _run(
        client, IG, str(tmp_path), mode="download", tab="image", row_id="i:orig",
        container="original", edited_title="My photo",
    )  # fmt: skip
    assert client.streamed == ["https://scontent.cdninstagram.com/a_1440.jpg?x=1&y=2"]
    assert Path(done["files"][0]).name == "My photo.jpg"


def _sidecar():
    def node(i, video=False):
        n = {
            "is_video": video,
            "display_url": f"https://scontent.cdninstagram.com/{i}.jpg",
            "display_resources": [
                {"src": f"https://scontent.cdninstagram.com/{i}_s.jpg", "config_width": 640},
                {"src": f"https://scontent.cdninstagram.com/{i}_l.jpg", "config_width": 1080},
            ],
        }
        if video:
            n["video_url"] = f"https://scontent.cdninstagram.com/{i}.mp4"
        return {"node": n}

    return {
        "__typename": "XDTGraphSidecar",
        "owner": {"username": "someone"},
        "edge_media_to_caption": {"edges": [{"node": {"text": "Beach day"}}]},
        "edge_sidecar_to_children": {"edges": [node(1), node(2, video=True), node(3)]},
    }


def test_a_mixed_instagram_carousel_from_the_embed_context_json(tmp_path):
    context = json.dumps({"gql_data": {"shortcode_media": _sidecar()}})
    page = '"init",[],[' + json.dumps({"isSidecar": True, "contextJSON": context}) + "]],"
    client = FakeClient(
        {IG + "embed/captioned/": (200, page.encode(), {}), "https://scontent.": IMG}
    )
    result = _run(client, IG)
    assert result["kind"] == "gallery" and result["title"] == "Beach day"
    assert [i["kind"] for i in result["items"]] == ["image", "video", "image"]
    assert [i["width"] for i in result["items"]] == [1080, 1080, 1080]
    assert all("preview" in i for i in result["items"])  # the video's poster frame too
    _run(client, IG, str(tmp_path), mode="download", preset="gallery_original", items=[2, 3])
    assert client.streamed == [
        "https://scontent.cdninstagram.com/2.mp4",
        "https://scontent.cdninstagram.com/3_l.jpg",
    ]


def test_a_carousel_the_embed_hides_falls_back_to_the_post_query():
    client = FakeClient(
        {
            IG + "embed/captioned/": (200, b'{"isSidecar":true,"contextJSON":null}', {}),
            "https://www.instagram.com/graphql/query": _json(
                {"data": {"xdt_shortcode_media": _sidecar()}}
            ),
            IG: (200, b'"LSD",[],{"token":"tok123"}', {}),
            "https://scontent.": IMG,
        }
    )
    assert len(_run(client, IG)["items"]) == 3


def test_a_post_the_query_cannot_see_needs_a_login():
    client = FakeClient(
        {
            IG + "embed/captioned/": (200, b"<html>Login</html>", {}),
            "https://www.instagram.com/graphql/query": _json(
                {"data": {"xdt_shortcode_media": None}}
            ),
            IG: (200, b"", {}),
        }
    )
    with pytest.raises(EngineError) as info:
        _run(client, IG)
    assert errors.needs_site_login(info.value.code, info.value.message)


def test_a_rate_limited_instagram_moves_on_rather_than_asking_for_a_login():
    client = FakeClient(
        {
            IG + "embed/captioned/": (200, b"<html></html>", {}),
            "https://www.instagram.com/graphql/query": (401, b'{"require_login":true}', {}),
        }
    )
    with pytest.raises(EngineError) as info:
        _run(client, IG)
    assert info.value.code == "unsupported"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.instagram.com/stories/some.user/3141592653/",
        "https://www.instagram.com/stories/highlights/17900000000000000/",
    ],
)
def test_a_story_or_highlight_needs_a_login_and_says_so_plainly(url):
    client = FakeClient({})
    with pytest.raises(EngineError) as info:
        _run(client, url)
    assert errors.needs_site_login(info.value.code, info.value.message)
    friendly = errors.friendly_message(info.value.code, info.value.message)
    assert "signed-in account" in friendly and "video" not in friendly.lower()
    assert client.fetched == []  # nothing is asked of the site


def test_an_owner_login_makes_social_step_aside_for_the_login_engines():
    client = FakeClient({})
    with pytest.raises(EngineError) as info:
        _run(client, IG, mode="analyze", site_login={"kind": "browser", "browser": "firefox"})
    assert info.value.code == "unsupported" and client.fetched == []


# ── TikTok ──────────────────────────────────────────────────────────────────────────────────
def _tiktok_page(images=3, status=0):
    item = {
        "desc": "Trip photos",
        "author": {"uniqueId": "someone"},
        "imagePost": {
            "images": [
                {
                    "imageURL": {
                        "urlList": [
                            f"https://p16.tiktokcdn.com/{i}.webp",
                            f"https://p16.tiktokcdn.com/{i}.jpeg",
                        ]
                    },
                    "imageWidth": 1080,
                    "imageHeight": 1440,
                }
                for i in range(images)
            ]
        },
        "music": {"playUrl": "https://sf16.tiktokcdn.com/m.mp3"},
    }
    detail = {"statusCode": status, "itemInfo": {"itemStruct": item} if images else {}}
    data = {"__DEFAULT_SCOPE__": {"webapp.video-detail": detail}}
    page = (
        '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
        + json.dumps(data)
        + "</script>"
    )
    return 200, page.encode(), {}


def test_a_tiktok_slideshow_is_its_photos_plus_the_sound_as_an_audio_row(tmp_path):
    client = FakeClient(
        {
            "https://www.tiktok.com/@i/video/7300000000000000000": _tiktok_page(),
            "https://p16.": IMG,
            "https://sf16.": (200, b"mp3", {}),
        }
    )
    url = "https://www.tiktok.com/@someone/photo/7300000000000000000"
    result = _run(client, url)
    assert result["kind"] == "gallery" and result["title"] == "Trip photos"
    assert [i["kind"] for i in result["items"]] == ["image", "image", "image", "audio"]
    assert result["items"][-1]["ext"] == "mp3"
    _run(client, url, str(tmp_path), mode="download", preset="gallery_original", items=[1, 4])
    assert client.streamed == [
        "https://p16.tiktokcdn.com/0.jpeg",
        "https://sf16.tiktokcdn.com/m.mp3",
    ]


def test_a_tiktok_short_link_is_resolved_first():
    client = FakeClient({})

    def fetch(url, headers=None, data=None, limit=0):
        client.fetched.append(url)
        if url.startswith("https://vt.tiktok.com/"):
            final = "https://www.tiktok.com/@a/photo/7300000000000000000"
            return social.Response(200, final, b"", {})
        if url.startswith("https://www.tiktok.com/@i/video/7300000000000000000"):
            return social.Response(200, url, _tiktok_page()[1], {})
        return social.Response(200, url, PNG, {"content-type": "image/png"})

    client.fetch = fetch
    assert len(_run(client, "https://vt.tiktok.com/ZSabc123/")["items"]) == 4


def test_a_private_tiktok_needs_a_login():
    client = FakeClient({"https://www.tiktok.com/@i/video/": _tiktok_page(images=0, status=10222)})
    with pytest.raises(EngineError) as info:
        _run(client, "https://www.tiktok.com/@a/video/7300000000000000000")
    assert errors.needs_site_login(info.value.code, info.value.message)


# ── Reddit ──────────────────────────────────────────────────────────────────────────────────
def _reddit_gallery():
    meta = {
        f"m{i}": {
            "status": "valid",
            "e": "Image",
            "s": {"u": f"https://preview.redd.it/m{i}.jpg?w=3000&amp;s=abc", "x": 3000, "y": 2000},
        }
        for i in range(2)
    }  # fmt: skip
    post = {
        "title": "My gallery",
        "author": "someone",
        "gallery_data": {"items": [{"media_id": "m1"}, {"media_id": "m0"}]},
        "media_metadata": meta,
    }
    return [{"data": {"children": [{"data": post}]}}]


def test_a_reddit_gallery_keeps_the_posts_order_and_unescapes_links(tmp_path):
    client = FakeClient(
        {
            "https://www.reddit.com/comments/abc123/.json": _json(_reddit_gallery()),
            "https://preview.redd.it/": IMG,
        }
    )
    url = "https://www.reddit.com/r/pics/comments/abc123/my_gallery/"
    result = _run(client, url)
    assert result["kind"] == "gallery" and result["title"] == "My gallery"
    assert [i["width"] for i in result["items"]] == [3000, 3000]
    _run(client, url, str(tmp_path), mode="download", preset="gallery_original", items=[1])
    assert client.streamed == ["https://preview.redd.it/m1.jpg?w=3000&s=abc"]


def test_a_reddit_block_page_moves_on_quietly():
    client = FakeClient({"https://www.reddit.com/": (403, b"<html>blocked</html>", {})})
    with pytest.raises(EngineError) as info:
        _run(client, "https://redd.it/abc123")
    assert info.value.code == "unsupported"


# ── any other page ──────────────────────────────────────────────────────────────────────────
def test_another_page_gives_its_og_image_as_one_photo():
    page = (
        b"<html><head><title>News</title>"
        b'<meta property="og:image" content="https://cdn.example.com/p.jpg"></head></html>'
    )
    client = FakeClient(
        {
            "https://www.example.com/story": (200, page, {"content-type": "text/html"}),
            "https://cdn.example.com/": IMG,
        }
    )
    result = _run(client, "https://www.example.com/story")
    assert result["kind"] == "image" and result["title"] == "News"


def test_a_page_with_no_picture_ends_the_chain_with_neutral_wording():
    empty = (200, b"<html></html>", {"content-type": "text/html"})
    client = FakeClient({"https://www.example.com/": empty})
    with pytest.raises(EngineError) as info:
        _run(client, "https://www.example.com/story")
    friendly = errors.friendly_message(info.value.code, info.value.message)
    assert info.value.code == "unsupported" and "video, photo or file" in friendly


# ── options and safety ──────────────────────────────────────────────────────────────────────
def test_the_engine_is_registered():
    assert ENGINES["social"] is social.SocialEngine
    assert isinstance(get_engine("social"), social.SocialEngine)


@pytest.mark.parametrize(
    "options",
    [
        {"mode": "analyze", "extra": 1},
        {"mode": "download", "preset": "gallery_original", "items": ["https://x/1.jpg"]},
        {"mode": "download", "preset": "gallery_original", "items": [0]},
        {"mode": "download", "preset": "best", "items": [1]},
        {"mode": "download", "preset": "gallery_original", "items": [1], "url": "https://x/"},
        {"mode": "download", "preset": "gallery_original", "items": [1], "image_format": "exe"},
        {"mode": "explode"},
    ],
)
def test_bad_options_are_refused_before_any_request(options, tmp_path):
    client = FakeClient({})
    with pytest.raises(EngineError) as info:
        _run(client, TWEET, str(tmp_path), **options)
    assert info.value.code == "bad_options" and client.fetched == []


def test_positions_the_post_no_longer_has_are_refused(tmp_path):
    client = FakeClient({"https://cdn.syndication.twimg.com/": _json(_tweet(2))})
    with pytest.raises(EngineError) as info:
        _run(client, TWEET, str(tmp_path), mode="download", preset="gallery_original", items=[9])
    assert info.value.code == "bad_options" and client.streamed == []


@pytest.mark.parametrize(
    "url",
    [
        "http://pbs.twimg.com/media/a.jpg",  # plain http
        "https://127.0.0.1/a.jpg",
        "https://localhost/a.jpg",
        "https://router.lan/a.jpg",
        "https://user:pw@pbs.twimg.com/a.jpg",
    ],
)
def test_the_real_client_refuses_non_public_or_plain_http_links_before_connecting(url, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not connect")

    monkeypatch.setattr(social, "_resolve", boom)
    monkeypatch.setattr(social, "open_url", boom)
    with pytest.raises(EngineError):
        social.Client().fetch(url)


class _FakeResp:
    def __init__(self, status, headers, body=b""):
        self.status_code, self.headers, self.body = status, headers, body

    def iter_content(self, chunk_size):
        yield self.body

    def close(self):
        pass


def _fake_curl(monkeypatch, answers, seen):
    class FakeSession:
        def __init__(self, impersonate, curl_options):
            seen.append({"impersonate": impersonate, "curl_options": curl_options})

        def request(self, method, url, **kwargs):
            seen[-1].update(url=url, allow_redirects=kwargs["allow_redirects"])
            return answers.pop(0)

        def close(self):
            pass

    class FakeRequests:
        Session = FakeSession

    class FakeOpt:
        RESOLVE = "RESOLVE"

    client = social.Client()
    client._curl = (FakeRequests, FakeOpt)
    return client


def test_curl_requests_are_pinned_to_the_vetted_address(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(social, "_resolve", lambda host, port: "93.184.216.34")
    client = _fake_curl(monkeypatch, [_FakeResp(200, {"content-type": "text/plain"}, b"ok")], seen)
    assert client.fetch("https://www.example.com/x").body == b"ok"
    assert seen == [
        {
            "impersonate": "chrome",
            "curl_options": {"RESOLVE": ["www.example.com:443:93.184.216.34"]},
            "url": "https://www.example.com/x",
            "allow_redirects": False,  # every hop comes back to check_url
        }
    ]


def test_a_redirect_into_the_lan_is_refused_on_that_hop(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(social, "_resolve", lambda host, port: "93.184.216.34")
    answers = [_FakeResp(302, {"location": "https://192.168.1.1/admin"})]
    client = _fake_curl(monkeypatch, answers, seen)
    with pytest.raises(EngineError):
        client.fetch("https://t.co/abc")
    assert [s["url"] for s in seen] == ["https://t.co/abc"]  # the LAN hop never connected


def test_an_oversized_body_is_refused_not_truncated(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(social, "_resolve", lambda host, port: "93.184.216.34")
    client = _fake_curl(monkeypatch, [_FakeResp(200, {}, b"x" * 100)], seen)
    with pytest.raises(EngineError):
        client.fetch("https://www.example.com/x", limit=10)


# ── TikTok's bot wall ─────────────────────────────────────────────────────────────────────
def _wall_page(answer: int, *, extra: bool = True) -> str:
    """A page shaped like TikTok's "Please wait..." puzzle, whose answer is ``answer``."""
    import hashlib

    prefix = b"prefix-bytes"
    digest = hashlib.sha256(prefix + str(answer).encode()).digest()
    data = {"v": {"a": base64.b64encode(prefix).decode(), "c": base64.b64encode(digest).decode()}}
    cs = base64.b64encode(json.dumps(data).encode()).decode().rstrip("=")
    rci = '<p id="rci" class="waforiginalreid"></p><p id="rs" class="abc"></p>' if extra else ""
    return (
        "<html><script>if(a<b){x()}</script>Please wait..."
        f'<p id="wci" class="_wafchallengeid"></p><p id="cs" class="{cs}"></p>{rci}</html>'
    )


def test_the_bot_wall_puzzle_is_answered_with_its_cookies():
    answer = social.solve_tiktok_challenge(_wall_page(93))
    assert set(answer) == {"_wafchallengeid", "waforiginalreid"}
    assert answer["waforiginalreid"] == "abc"
    solved = json.loads(base64.b64decode(answer["_wafchallengeid"]))
    assert base64.b64decode(solved["d"]) == b"93"


def test_a_page_without_a_puzzle_gets_no_cookies():
    assert social.solve_tiktok_challenge("<html><p id='cs' class='not base64!'></p></html>") == {}
    assert social.solve_tiktok_challenge("<html>a normal page</html>") == {}
    assert set(social.solve_tiktok_challenge(_wall_page(5, extra=False))) == {"_wafchallengeid"}


def test_a_puzzle_with_no_answer_in_range_gives_up(monkeypatch):
    monkeypatch.setattr(social, "MAX_CHALLENGE_TRIES", 10)
    assert social.solve_tiktok_challenge(_wall_page(500)) == {}


def test_a_post_blocked_for_the_network_says_so_plainly():
    message = "ERROR: [TikTok] 1: Your IP address is blocked from accessing this post"
    assert "country or network" in errors.friendly_message("download_error", message)
