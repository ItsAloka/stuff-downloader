"""Social posts without a login (plan §6, R4): Instagram, TikTok, X and Reddit photo posts.

Runs in the yt-dlp env, which already ships ``curl_cffi``: Instagram answers a plain Python TLS
client with its login page, and a browser-like handshake is the difference. Without
``curl_cffi`` the stdlib client in ``http`` is used instead.

Every request, including each redirect hop, goes through ``http.check_url`` (https only, public
host) and is pinned to the one address ``http._resolve`` vetted, so a site cannot point us at
this machine or the LAN through DNS or a redirect.

These are unofficial endpoints and will break sometimes. A post this engine cannot read ends
with ``unsupported`` so the router's chain moves on (yt-dlp, then gallery-dl). A post that is a
single video also ends that way on purpose: yt-dlp gives it real Video/Audio rows. Only a post
the site says needs an account ends with "login required", which offers the site login.

Options: ``mode`` "analyze" (default) or "download". Download takes either a Result-card row
request (one photo) or ``preset`` = "gallery_original" with ``items`` (1-based positions from
analyze) and optional ``image_format``. Item URLs never leave the worker: analyze returns
rebuilt rows and previews as bytes, and a download re-reads the post and fetches by position.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import math
import os
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit

from .. import presets
from ..protocol import JobSpec, media_result
from .base import Emit, EngineError
from .gallerydl import MAX_ITEMS, PRESET_ID, parse_items, rename_single, single_item_fields
from .http import (
    MAX_REDIRECTS,
    _resolve,
    check_url,
    move_into_place,
    open_url,
    page_image_url,
    part_names,
    safe_file_name,
)

TIMEOUT = 30
MAX_PAGE_BYTES = 8 * 1024 * 1024  # an Instagram or TikTok page is a few MB of inline JSON
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_PREVIEW_BYTES = 2_000_000
MAX_PREVIEWS = 60
MAX_MEDIA_BYTES = 2 * 1024 * 1024 * 1024
MAX_TEXT = 300
REQUEST_DELAY = 1.0  # seconds between item downloads from one site (plan §6.1)
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
    " Chrome/140.0.0.0 Safari/537.36"
)
LOGIN_REQUIRED = "login required: this post is only visible to signed-in accounts"
_EXT = re.compile(r"[a-z0-9]{1,5}")

IG_APP_ID = "936619743392459"
IG_DOC_ID = "8845758582119845"  # PolarisPostActionLoadPostQueryQuery (cobalt, Sep 2026)
_IG_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com"}
_TIKTOK_HOSTS = {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}
_TIKTOK_SHORT = {"vt.tiktok.com", "vm.tiktok.com"}
_X_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
_REDDIT_HOSTS = {"reddit.com", "www.reddit.com", "old.reddit.com", "new.reddit.com"}
_REDDIT_SHORT = {"redd.it"}
_SHORTCODE = re.compile(r"[A-Za-z0-9_-]{5,40}")
_NUMERIC_ID = re.compile(r"\d{5,25}")
_REDDIT_ID = re.compile(r"[a-z0-9]{3,12}")


# ── the post as this engine sees it ────────────────────────────────────────────────────────
@dataclass
class Media:
    kind: str  # "image" | "video" | "audio"
    url: str
    ext: str = ""
    width: int | None = None
    height: int | None = None
    preview_url: str = ""  # a smaller picture for the grid; "" means the item itself (images)


@dataclass
class Post:
    site: str
    title: str = ""
    uploader: str = ""
    items: list[Media] = field(default_factory=list)


def _short(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:MAX_TEXT]


def _dim(value: Any) -> int | None:
    ok = isinstance(value, int) and not isinstance(value, bool) and 0 < value < 100_000
    return value if ok else None


def _ext_of(url: str, default: str) -> str:
    tail = urlsplit(url).path.rsplit("/", 1)[-1]
    ext = tail.rsplit(".", 1)[1].lower() if "." in tail else ""
    if ext == "jpeg":
        return "jpg"
    return ext if _EXT.fullmatch(ext) and ext != "heic" else default


def _https(url: Any) -> str:
    """``url`` if it is an absolute https link, else "". The fetch re-checks the host."""
    if not isinstance(url, str):
        return ""
    url = html.unescape(url.strip())
    return url if urlsplit(url).scheme == "https" else ""


# ── fetching: checked, pinned, every hop ───────────────────────────────────────────────────
@dataclass
class Response:
    status: int
    url: str
    body: bytes
    headers: dict[str, str]

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except ValueError:
            return None


class Client:
    """One site conversation: cookies kept between requests, nothing else carried over."""

    def __init__(self) -> None:
        self.cookies: dict[str, str] = {}
        try:
            from curl_cffi import CurlOpt
            from curl_cffi import requests as curl_requests
        except ImportError:
            self._curl = None
        else:
            self._curl = (curl_requests, CurlOpt)

    def fetch(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        limit: int = MAX_JSON_BYTES,
    ) -> Response:
        """GET (or POST with ``data``) one URL, following checked redirects; body ≤ ``limit``."""
        body_data = urlencode(data).encode() if data is not None else None
        for _ in range(MAX_REDIRECTS + 1):
            status, headers_out, body = self._one(url, headers or {}, body_data, limit)
            if status in (301, 302, 303, 307, 308):
                location = headers_out.get("location", "")
                if not location:
                    raise EngineError("unsupported", "the site sent a redirect with no target")
                url = urljoin(url, location)
                if status == 303:
                    body_data = None
                continue
            return Response(status, url, body, headers_out)
        raise EngineError("unsupported", "too many redirects")

    def stream(self, url: str, dest: Path, emit: Emit, headers: dict[str, str]) -> int:
        """Save one media URL to ``dest``; returns the byte count. Refuses anything not 200."""
        for _ in range(MAX_REDIRECTS + 1):
            with self._open(url, headers, None, stream=True) as (status, hdrs, chunks):
                if status in (301, 302, 303, 307, 308):
                    location = hdrs.get("location", "")
                    if not location:
                        raise EngineError("download_error", "the site sent an empty redirect")
                    url = urljoin(url, location)
                    continue
                if status != 200:
                    raise EngineError("download_error", f"http error {status}")
                total = int(hdrs.get("content-length") or 0) or None
                done = 0
                with open(dest, "wb") as out:
                    for chunk in chunks:
                        done += len(chunk)
                        if done > MAX_MEDIA_BYTES:
                            raise EngineError("download_error", "the file is too large")
                        out.write(chunk)
                        if total:
                            emit(
                                "progress",
                                {
                                    "downloaded_bytes": done,
                                    "total_bytes": total,
                                    "percent": round(min(100.0, 100 * done / total), 1),
                                    "speed": None,
                                    "eta": None,
                                },
                            )
                return done
        raise EngineError("download_error", "too many redirects")

    # ── transport ───────────────────────────────────────────────────────────────────────
    def _one(
        self, url: str, headers: dict[str, str], data: bytes | None, limit: int
    ) -> tuple[int, dict[str, str], bytes]:
        with self._open(url, headers, data, stream=True) as (status, hdrs, chunks):
            parts, size = [], 0
            for chunk in chunks:
                size += len(chunk)
                if size > limit:
                    raise EngineError("unsupported", "the site sent more than expected")
                parts.append(chunk)
        return status, hdrs, b"".join(parts)

    def _open(self, url: str, headers: dict[str, str], data: bytes | None, stream: bool) -> Any:
        target = check_url(url, https_only=True)
        sent = {"User-Agent": BROWSER_UA, **headers}
        if self.cookies:
            sent["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if self._curl is not None:
            return _CurlCall(self, target.host, target.port, url, sent, data)
        return _StdlibCall(self, url, sent, data)

    def _keep_cookies(self, set_cookies: list[str]) -> None:
        for line in set_cookies[:50]:
            pair = line.split(";", 1)[0]
            name, _, value = pair.partition("=")
            name = name.strip()
            if name and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name) and len(value) < 4096:
                self.cookies[name] = value.strip()


class _CurlCall:
    """A curl_cffi request pinned to the vetted address (CURLOPT_RESOLVE), no auto-redirect."""

    def __init__(
        self, client: Client, host: str, port: int, url: str, headers: dict, data: bytes | None
    ) -> None:
        self.client, self.url, self.headers, self.data = client, url, headers, data
        self.host, self.port = host, port
        self.session: Any = None
        self.resp: Any = None

    def __enter__(self) -> tuple[int, dict[str, str], Any]:
        requests, curl_opt = self.client._curl  # type: ignore[misc]
        address = _resolve(self.host, self.port)
        pinned = f"[{address}]" if ":" in address else address
        self.session = requests.Session(
            impersonate="chrome",
            curl_options={curl_opt.RESOLVE: [f"{self.host}:{self.port}:{pinned}"]},
        )
        # The impersonated browser sends its own User-Agent; ours would contradict its TLS.
        headers = {k: v for k, v in self.headers.items() if k.lower() != "user-agent"}
        try:
            self.resp = self.session.request(
                "POST" if self.data is not None else "GET",
                self.url,
                headers=headers,
                data=self.data,
                timeout=TIMEOUT,
                allow_redirects=False,
                stream=True,
            )
        except Exception as exc:  # curl_cffi raises its own RequestException family
            self.session.close()
            raise EngineError("download_error", "could not reach the site") from exc
        hdrs = {k.lower(): v for k, v in self.resp.headers.items()}
        try:
            cookies = self.resp.headers.get_list("set-cookie")
        except AttributeError:
            cookies = [hdrs["set-cookie"]] if "set-cookie" in hdrs else []
        self.client._keep_cookies(cookies)
        return self.resp.status_code, hdrs, self.resp.iter_content(chunk_size=256 * 1024)

    def __exit__(self, *exc: Any) -> None:
        if self.resp is not None:
            self.resp.close()
        if self.session is not None:
            self.session.close()


class _StdlibCall:
    """The same contract over ``http.open_url``'s one hop (it re-checks and pins, too)."""

    def __init__(self, client: Client, url: str, headers: dict, data: bytes | None) -> None:
        self.client, self.url, self.headers, self.data = client, url, headers, data
        self.conn: Any = None

    def __enter__(self) -> tuple[int, dict[str, str], Any]:
        if self.data is not None:
            # open_url sends no request body; the POST fallbacks only exist with curl_cffi.
            raise EngineError("unsupported", "this lookup needs the browser-like client")
        # open_url follows redirects itself, re-checking and pinning each hop.
        self.conn, resp, _ = open_url(self.url, self.headers, https_only=True)
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        self.client._keep_cookies(resp.headers.get_all("Set-Cookie") or [])

        def chunks() -> Any:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    return
                yield chunk

        return resp.status, hdrs, chunks()

    def __exit__(self, *exc: Any) -> None:
        if self.conn is not None:
            self.conn.close()


# ── Instagram ──────────────────────────────────────────────────────────────────────────────
def instagram_code(path: str) -> str:
    segments = [s for s in path.split("/") if s]
    if len(segments) >= 2 and segments[0] in ("p", "reel", "reels", "tv"):
        code = segments[1]
    elif len(segments) >= 3 and segments[1] in ("p", "reel", "tv"):
        code = segments[2]  # /<user>/p/<code>/
    else:
        return ""
    return code if _SHORTCODE.fullmatch(code) else ""


def _ig_best_image(node: dict[str, Any]) -> tuple[str, int | None, int | None]:
    resources = node.get("display_resources")
    if isinstance(resources, list):
        best = max(
            (r for r in resources if isinstance(r, dict) and _https(r.get("src"))),
            key=lambda r: _dim(r.get("config_width")) or 0,
            default=None,
        )
        if best:
            return (
                _https(best["src"]),
                _dim(best.get("config_width")),
                _dim(best.get("config_height")),
            )
    dims = node.get("dimensions") if isinstance(node.get("dimensions"), dict) else {}
    return _https(node.get("display_url")), _dim(dims.get("width")), _dim(dims.get("height"))


def _ig_node(node: dict[str, Any]) -> Media | None:
    url, width, height = _ig_best_image(node)
    if node.get("is_video"):
        video = _https(node.get("video_url"))
        return Media("video", video, "mp4", width, height, url) if video else None
    return Media("image", url, _ext_of(url, "jpg"), width, height) if url else None


def instagram_post(media: Any) -> Post | None:
    """A GraphQL ``shortcode_media`` (embed contextJSON or the post query) as a Post."""
    if not isinstance(media, dict):
        return None
    owner = media.get("owner") if isinstance(media.get("owner"), dict) else {}
    post = Post("Instagram", uploader=_short(owner.get("username")))
    edges = (media.get("edge_media_to_caption") or {}).get("edges") or []
    if edges and isinstance(edges[0], dict):
        post.title = _short((edges[0].get("node") or {}).get("text"))
    children = (media.get("edge_sidecar_to_children") or {}).get("edges")
    nodes = [e.get("node") for e in children if isinstance(e, dict)] if children else [media]
    for node in nodes[:MAX_ITEMS]:
        item = _ig_node(node) if isinstance(node, dict) else None
        if item:
            post.items.append(item)
    return post


_IG_INIT = re.compile(r'"init",\[\],\[(\{.*?\})\]\]')
_IG_IMAGE = re.compile(r'<img[^>]*class="EmbeddedMediaImage"[^>]*>', re.IGNORECASE)
_SRCSET = re.compile(r'srcset="([^"]+)"')


def instagram_embed(page: str) -> tuple[Post | None, bool]:
    """(post, is_sidecar) from ``/p/<code>/embed/captioned/``.

    The page carries the full post in ``contextJSON`` when Instagram feels like it; otherwise
    only the first picture as an ``<img srcset>``, which is the whole post only if it is not a
    carousel (``isSidecar`` false).
    """
    sidecar = '"isSidecar":true' in page
    for match in _IG_INIT.finditer(page):
        try:
            init = json.loads(match.group(1))
        except ValueError:
            continue
        context = init.get("contextJSON") if isinstance(init, dict) else None
        if isinstance(context, str):
            try:
                data = json.loads(context)
            except ValueError:
                continue
            post = instagram_post((data.get("gql_data") or {}).get("shortcode_media"))
            if post and post.items:
                return post, sidecar
    if sidecar or "EmbeddedMediaVideo" in page:
        return None, sidecar
    tag = _IG_IMAGE.search(page)
    srcset = _SRCSET.search(tag.group(0)) if tag else None
    if not srcset:
        return None, sidecar
    best, best_w = "", 0
    for entry in html.unescape(srcset.group(1)).split(","):
        bits = entry.strip().rsplit(" ", 1)
        width = int(bits[1][:-1]) if len(bits) == 2 and re.fullmatch(r"\d{1,5}w", bits[1]) else 0
        if _https(bits[0]) and width >= best_w:
            best, best_w = _https(bits[0]), width
    if not best:
        return None, sidecar
    user = re.search(r'class="UsernameText"[^>]*>([A-Za-z0-9._]{1,30})<', page) or re.search(
        r"shared by (?:@|&#064;)([A-Za-z0-9._]{1,30})", page
    )
    caption = re.search(
        r'class="CaptionUsername".*?</a>(.*?)<div class="CaptionComments"', page, re.S
    )
    text = re.sub(r"<[^>]+>", " ", caption.group(1)) if caption else ""
    post = Post(
        "Instagram", title=_short(html.unescape(text)), uploader=user.group(1) if user else ""
    )
    post.items.append(Media("image", best, "jpg", best_w or None, None))
    return post, sidecar


def _read_instagram(client: Client, url: str) -> Post:
    if [s for s in urlsplit(url).path.split("/") if s][:1] == ["stories"]:
        # Stories and highlights are never shown to a logged-out viewer.
        raise EngineError("download_error", LOGIN_REQUIRED)
    code = instagram_code(urlsplit(url).path)
    if not code:
        raise EngineError("unsupported", "unsupported url: not an Instagram post")
    base = f"https://www.instagram.com/p/{code}/"
    embed = client.fetch(base + "embed/captioned/", limit=MAX_PAGE_BYTES)
    if embed.status == 200:
        post, _ = instagram_embed(embed.text())
        if post and post.items:
            return post
    # The post query, as the logged-out web page makes it.
    page = client.fetch(base, limit=MAX_PAGE_BYTES)
    lsd = re.search(r'"LSD",\[\],\{"token":"([^"]+)"', page.text()) if page.status == 200 else None
    token = lsd.group(1) if lsd else "AVqbxe3J_YA"
    variables = {"shortcode": code, "fetch_tagged_user_count": None, "hoisted_comment_id": None}
    resp = client.fetch(
        "https://www.instagram.com/graphql/query",
        headers={
            "X-FB-Friendly-Name": "PolarisPostActionLoadPostQueryQuery",
            "X-FB-LSD": token,
            "X-IG-App-ID": IG_APP_ID,
            "X-CSRFToken": client.cookies.get("csrftoken", ""),
            "Referer": base,
            "Origin": "https://www.instagram.com",
        },
        data={
            "lsd": token,
            "doc_id": IG_DOC_ID,
            "fb_api_req_friendly_name": "PolarisPostActionLoadPostQueryQuery",
            "variables": json.dumps(variables),
            "server_timestamps": "true",
        },
    )
    data = resp.json()
    media = (
        ((data or {}).get("data") or {}).get("xdt_shortcode_media")
        if isinstance(data, dict)
        else None
    )
    post = instagram_post(media)
    if post and post.items:
        return post
    if isinstance(data, dict) and data.get("data") and media is None:
        # The query ran and the post is not there for a logged-out viewer: private or gone.
        raise EngineError("download_error", LOGIN_REQUIRED)
    raise EngineError("unsupported", "no media information found in that post")


# ── TikTok ─────────────────────────────────────────────────────────────────────────────────
_TT_DATA = re.compile(
    r'<script[^>]*id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', re.DOTALL
)


def tiktok_id(path: str) -> str:
    segments = [s for s in path.split("/") if s]
    for i, seg in enumerate(segments[:-1]):
        if seg in ("video", "photo") and _NUMERIC_ID.fullmatch(segments[i + 1]):
            return segments[i + 1]
    return ""


def tiktok_post(page: str) -> tuple[Post | None, int | None]:
    """(post, statusCode) from a video/photo page's rehydration JSON."""
    match = _TT_DATA.search(page)
    if not match:
        return None, None
    try:
        data = json.loads(match.group(1))
    except ValueError:
        return None, None
    detail = (data.get("__DEFAULT_SCOPE__") or {}).get("webapp.video-detail") or {}
    status = detail.get("statusCode") if isinstance(detail.get("statusCode"), int) else None
    item = ((detail.get("itemInfo") or {}).get("itemStruct")) or {}
    if not isinstance(item, dict) or not item:
        return None, status
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    post = Post("TikTok", title=_short(item.get("desc")), uploader=_short(author.get("uniqueId")))
    images = ((item.get("imagePost") or {}).get("images")) or []
    for image in images[:MAX_ITEMS] if isinstance(images, list) else []:
        urls = ((image or {}).get("imageURL") or {}).get("urlList") or []
        urls = [_https(u) for u in urls if _https(u)]
        if not urls:
            continue
        chosen = next((u for u in urls if ".jpeg" in u or ".jpg" in u), urls[0])
        post.items.append(
            Media("image", chosen, "jpg", _dim(image.get("imageWidth")),
                  _dim(image.get("imageHeight")))
        )  # fmt: skip
    if post.items:
        music = item.get("music") if isinstance(item.get("music"), dict) else {}
        play = _https(music.get("playUrl"))
        if play:
            post.items.append(Media("audio", play, _ext_of(play, "mp3"), None, None,
                                    _https(music.get("coverLarge"))))  # fmt: skip
    return post, status


# TikTok's bot wall ("SlardarWAF") answers a first visit with a small page holding a puzzle:
# find the number whose SHA-256, appended to a given prefix, matches a given digest. The page's
# script then sets the answer as a cookie and reloads. This does the same, as yt-dlp does.
MAX_CHALLENGE_TRIES = 1_000_001
_CLASS = re.compile(r"""\bclass=["']([^"']{0,8000})["']""")


def _class_of(page: str, element_id: str) -> str | None:
    """The class attribute of the element with ``element_id``: where the puzzle hides its data."""
    opening = r"<[a-zA-Z]+\s[^<>]{0,8000}?\bid=[\"']" + re.escape(element_id)
    tag = re.search(opening + r"[\"'][^<>]{0,8000}>", page)
    found = _CLASS.search(tag.group(0)) if tag else None
    return found.group(1) if found else None


def solve_tiktok_challenge(page: str) -> dict[str, str]:
    """The cookies that answer TikTok's bot-wall puzzle in ``page``, or {} if there is none."""
    try:
        data = json.loads(base64.b64decode(_class_of(page, "cs") + "==="))
        expected = base64.b64decode(data["v"]["c"])
        base = hashlib.sha256(base64.b64decode(data["v"]["a"]))
    except Exception:  # no puzzle on this page, or not one we understand
        return {}
    for number in range(MAX_CHALLENGE_TRIES):
        attempt = base.copy()
        attempt.update(str(number).encode())
        if attempt.digest() == expected:
            data["d"] = base64.b64encode(str(number).encode()).decode()
            break
    else:
        return {}
    name = _class_of(page, "wci")
    if not name:
        return {}
    answer = {name: base64.b64encode(json.dumps(data, separators=(",", ":")).encode()).decode()}
    extra_name, extra_value = _class_of(page, "rci"), _class_of(page, "rs")
    if extra_name and extra_value:
        answer[extra_name] = extra_value
    return answer


def _fetch_tiktok_page(client: Client, url: str) -> Any:
    """A TikTok post page, past the bot wall when it puts one up."""
    page = client.fetch(url, limit=MAX_PAGE_BYTES)
    if page.status != 200 or _TT_DATA.search(page.text()):
        return page
    answer = solve_tiktok_challenge(page.text())
    if not answer:
        return page
    client.cookies.update(answer)
    try:
        return client.fetch(url, limit=MAX_PAGE_BYTES)
    finally:
        for name in answer:  # the page's own script lets these expire at once
            client.cookies.pop(name, None)


def _read_tiktok(client: Client, url: str) -> Post:
    parts = urlsplit(url)
    if (parts.hostname or "").lower() in _TIKTOK_SHORT:
        resolved = client.fetch(url, limit=MAX_PAGE_BYTES)
        url = resolved.url
    post_id = tiktok_id(urlsplit(url).path)
    if not post_id:
        raise EngineError("unsupported", "unsupported url: not a TikTok post")
    page = _fetch_tiktok_page(client, f"https://www.tiktok.com/@i/video/{post_id}")
    post, status = tiktok_post(page.text()) if page.status == 200 else (None, None)
    if post and post.items:
        return post
    if status in (10216, 10222):  # "private account" / "only friends can see this"
        raise EngineError("download_error", LOGIN_REQUIRED)
    raise EngineError("unsupported", "no photos found in that TikTok post")


# ── X / Twitter ────────────────────────────────────────────────────────────────────────────
_DIGITS36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def syndication_token(tweet_id: str) -> str:
    """``((id / 1e15) * π).toString(36)`` with zeros and the dot removed, as X's embed does."""
    value = (int(tweet_id) / 1e15) * math.pi
    whole, frac = int(value), value - int(value)
    head = ""
    while whole:
        whole, digit = divmod(whole, 36)
        head = _DIGITS36[digit] + head
    tail = ""
    for _ in range(11):  # JS prints about 11 base-36 fraction digits for a double this size
        frac *= 36
        digit = int(frac)
        tail += _DIGITS36[digit]
        frac -= digit
        if frac <= 0:
            break
    return (head + tail).replace("0", "")


def x_id(path: str) -> str:
    segments = [s for s in path.split("/") if s]
    for i, seg in enumerate(segments[:-1]):
        if seg == "status" and _NUMERIC_ID.fullmatch(segments[i + 1]):
            return segments[i + 1]
    return ""


def x_post(data: Any) -> Post | None:
    if not isinstance(data, dict) or data.get("__typename") == "TweetTombstone":
        return None
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    text = (
        re.sub(r"https://t\.co/\S+", "", data.get("text") or "")
        if isinstance(data.get("text"), str)
        else ""
    )
    post = Post("X", title=_short(text), uploader=_short(user.get("screen_name")))
    for media in (data.get("mediaDetails") or [])[:MAX_ITEMS]:
        if not isinstance(media, dict):
            continue
        picture = _https(media.get("media_url_https"))
        info = media.get("original_info") if isinstance(media.get("original_info"), dict) else {}
        width, height = _dim(info.get("width")), _dim(info.get("height"))
        if media.get("type") == "photo" and picture:
            ext = _ext_of(picture, "jpg")
            base = picture.rsplit(".", 1)[0]
            post.items.append(Media("image", f"{base}?format={ext}&name=orig", ext, width,
                                    height, f"{base}?format={ext}&name=small"))  # fmt: skip
        elif media.get("type") in ("video", "animated_gif"):
            variants = (media.get("video_info") or {}).get("variants") or []
            mp4 = [
                v
                for v in variants
                if isinstance(v, dict)
                and v.get("content_type") == "video/mp4"
                and _https(v.get("url"))
            ]
            if mp4:
                best = max(mp4, key=lambda v: v.get("bitrate") or 0)
                post.items.append(Media("video", _https(best["url"]), "mp4", width, height,
                                        picture))  # fmt: skip
    return post


def _read_x(client: Client, url: str) -> Post:
    tweet_id = x_id(urlsplit(url).path)
    if not tweet_id:
        raise EngineError("unsupported", "unsupported url: not an X post")
    query = urlencode({"id": tweet_id, "token": syndication_token(tweet_id), "lang": "en"})
    resp = client.fetch(f"https://cdn.syndication.twimg.com/tweet-result?{query}")
    post = x_post(resp.json()) if resp.status == 200 else None
    if post and post.items:
        return post
    if (
        resp.status == 200
        and isinstance(resp.json(), dict)
        and (resp.json() or {}).get("__typename") == "TweetTombstone"
    ):
        raise EngineError("download_error", LOGIN_REQUIRED)  # protected or age-gated
    raise EngineError("unsupported", "no media information found in that post")


# ── Reddit ─────────────────────────────────────────────────────────────────────────────────
def reddit_id(host: str, path: str) -> str:
    segments = [s for s in path.split("/") if s]
    if host in _REDDIT_SHORT:
        candidate = segments[0] if segments else ""
    elif "comments" in segments[:-1]:
        candidate = segments[segments.index("comments") + 1]
    elif segments[:1] == ["gallery"] and len(segments) >= 2:
        candidate = segments[1]
    else:
        return ""
    return candidate.lower() if _REDDIT_ID.fullmatch(candidate.lower()) else ""


def reddit_post(data: Any) -> Post | None:
    try:
        post_data = data[0]["data"]["children"][0]["data"]
    except (KeyError, IndexError, TypeError):
        return None
    if not isinstance(post_data, dict):
        return None
    post = Post("Reddit", title=_short(post_data.get("title")),
                uploader=_short(post_data.get("author")))  # fmt: skip
    meta = post_data.get("media_metadata")
    meta = meta if isinstance(meta, dict) else {}
    gallery = ((post_data.get("gallery_data") or {}).get("items")) or []
    for entry in gallery[:MAX_ITEMS] if isinstance(gallery, list) else []:
        info = meta.get((entry or {}).get("media_id")) if isinstance(entry, dict) else None
        if not isinstance(info, dict) or info.get("status") != "valid":
            continue
        source = info.get("s") if isinstance(info.get("s"), dict) else {}
        width, height = _dim(source.get("x")), _dim(source.get("y"))
        if info.get("e") == "AnimatedImage" and _https(source.get("mp4")):
            post.items.append(Media("video", _https(source["mp4"]), "mp4", width, height))
        elif _https(source.get("u")) or _https(source.get("gif")):
            image = _https(source.get("u")) or _https(source.get("gif"))
            post.items.append(Media("image", image, _ext_of(image, "jpg"), width, height))
    if not post.items and post_data.get("post_hint") == "image":
        image = _https(post_data.get("url_overridden_by_dest") or post_data.get("url"))
        if image:
            post.items.append(Media("image", image, _ext_of(image, "jpg")))
    return post


def _read_reddit(client: Client, url: str) -> Post:
    parts = urlsplit(url)
    segments = [s for s in parts.path.split("/") if s]
    if "s" in segments[:-1]:  # /r/<sub>/s/<share id>: the redirect names the post
        url = client.fetch(url, limit=MAX_PAGE_BYTES).url
        parts = urlsplit(url)
    post_id = reddit_id((parts.hostname or "").lower(), parts.path)
    if not post_id:
        raise EngineError("unsupported", "unsupported url: not a Reddit post")
    resp = client.fetch(
        f"https://www.reddit.com/comments/{post_id}/.json?raw_json=1",
        headers={"User-Agent": "StuffDownloader/1.0 (desktop downloader)"},
    )
    if resp.status == 403 and b"private" in resp.body.lower():
        raise EngineError("download_error", LOGIN_REQUIRED)
    post = reddit_post(resp.json()) if resp.status == 200 else None
    if post and post.items:
        return post
    raise EngineError("unsupported", "no photos found in that Reddit post")


# ── any other page: its own picture ────────────────────────────────────────────────────────
def _read_page(client: Client, url: str) -> Post:
    resp = client.fetch(url, limit=MAX_PAGE_BYTES)
    kind = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    if resp.status != 200 or kind not in ("text/html", "application/xhtml+xml"):
        raise EngineError("unsupported", "unsupported url: no picture found on that page")
    image = page_image_url(resp.text(), resp.url)
    if not image:
        raise EngineError("unsupported", "unsupported url: no picture found on that page")
    title = re.search(r"<title[^>]*>(.*?)</title>", resp.text(), re.IGNORECASE | re.DOTALL)
    post = Post(urlsplit(resp.url).hostname or "Page",
                title=_short(html.unescape(title.group(1))) if title else "")  # fmt: skip
    post.items.append(Media("image", image, _ext_of(image, "jpg")))
    return post


def reader_for(url: str) -> Any:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        host = ""
    if host in _IG_HOSTS:
        return _read_instagram
    if host in _TIKTOK_HOSTS or host in _TIKTOK_SHORT:
        return _read_tiktok
    if host in _X_HOSTS:
        return _read_x
    if host in _REDDIT_HOSTS or host in _REDDIT_SHORT:
        return _read_reddit
    return _read_page


def read_post(client: Client, url: str) -> Post:
    """The post behind ``url``. A single video goes back to yt-dlp as "unsupported"."""
    post = reader_for(url)(client, url)
    visual = [m for m in post.items if m.kind != "audio"]
    if len(visual) == 1 and visual[0].kind == "video" and len(post.items) == 1:
        raise EngineError("unsupported", "a single video: the video engine reads it better")
    return post


def _already_saved(folder: Path, base: str, media: Media, fmt: str) -> bool:
    """Whether this item's file is already in the folder, as saved or as converted."""
    exts = {media.ext or "bin"}
    if media.kind == "image" and fmt != "original":
        exts.add(fmt)
    return any((folder / f"{base}.{ext}").is_file() for ext in exts)


# ── rows the GUI may see ───────────────────────────────────────────────────────────────────
def item_rows(post: Post) -> list[dict[str, Any]]:
    """Rebuilt rows: position, kind, extension, size. Never a URL."""
    rows = []
    for position, media in enumerate(post.items, 1):
        row: dict[str, Any] = {"index": position, "kind": media.kind, "ext": media.ext}
        if media.width:
            row["width"] = media.width
        if media.height:
            row["height"] = media.height
        rows.append(row)
    return rows


def _title(post: Post) -> str:
    if post.title:
        return post.title
    return f"{post.site} post by @{post.uploader}" if post.uploader else f"{post.site} post"


class SocialEngine:
    name = "social"

    def __init__(self, client: Client | None = None) -> None:
        self._client = client

    def download(self, job: JobSpec, emit: Emit) -> dict[str, Any]:
        opts = dict(job.options)
        login = opts.pop("site_login", None)  # never used: this engine is the no-login path
        mode = opts.get("mode", "analyze")
        client = self._client or Client()
        if mode == "analyze":
            if login is not None:
                # The owner set a login for this site, so the engines that can use it
                # (yt-dlp, then gallery-dl) read the post instead.
                raise EngineError("unsupported", "a site login is set; the login engines read it")
            if set(opts) - {"mode"}:
                raise EngineError("bad_options", "analyze takes no options")
            return self._analyze(client, job.url, emit)
        if mode != "download":
            raise EngineError("bad_options", f"unknown mode {mode!r}")
        from ..image_convert import IMAGE_OPTION_KEYS, parse_image_options

        edited_title: str | None = None
        if presets.is_row_request(opts):
            request = presets.parse_row_request(opts, original_only=True)
            edited_title = request.edited_title
            opts = {"mode": "download", "preset": PRESET_ID, "items": [1]}
            if request.tab == "image" and request.container != "original":
                opts["image_format"] = request.container
            job = replace(job, options=opts)
        if (
            set(opts) - ({"mode", "preset", "items", "archive"} | IMAGE_OPTION_KEYS)
            or opts.get("preset") != PRESET_ID
        ):
            raise EngineError("bad_options", "social download takes preset=gallery_original")
        if not isinstance(opts.get("archive", False), bool):
            raise EngineError("bad_options", "'archive' must be true or false")
        parse_image_options(opts)
        items = parse_items(opts)
        result = self._download(client, job, items, emit)
        return rename_single(result, edited_title)

    # ── analyze ─────────────────────────────────────────────────────────────────────────
    def _analyze(self, client: Client, url: str, emit: Emit) -> dict[str, Any]:
        emit("stage", {"stage": "analyzing"})
        post = read_post(client, url)
        rows = item_rows(post)
        self._attach_previews(client, post, rows, emit)
        emit("stage", {"stage": "completed"})
        visual = [r for r in rows if r["kind"] != "audio"]
        if len(rows) == 1 and visual and visual[0]["kind"] == "image":
            kind, tabs = "image", ["image"]
        else:
            kind, tabs = "gallery", ["gallery"]
        return media_result(
            kind,
            tabs,
            _title(post),
            url,
            **single_item_fields(kind, rows),
            uploader=post.uploader or None,
            site=post.site,
            extractor=post.site,
            items=rows,
            truncated=False,
        )

    @staticmethod
    def _attach_previews(client: Client, post: Post, rows: list[dict], emit: Emit) -> None:
        for media, row in list(zip(post.items, rows, strict=True))[:MAX_PREVIEWS]:
            source = media.preview_url or (media.url if media.kind == "image" else "")
            if not source:
                continue
            try:
                resp = client.fetch(source, limit=MAX_PREVIEW_BYTES)
            except EngineError:
                emit("log", {"level": "warning", "message": "a post preview could not load"})
                continue
            kind = resp.headers.get("content-type", "")
            if resp.status == 200 and resp.body and kind.startswith("image/"):
                row["preview"] = {"data": base64.b64encode(resp.body).decode("ascii")}

    # ── download ────────────────────────────────────────────────────────────────────────
    def _download(
        self, client: Client, job: JobSpec, items: list[int], emit: Emit
    ) -> dict[str, Any]:
        folder = Path(job.output_dir)
        if not folder.is_dir():
            raise EngineError("download_error", "the download folder does not exist")
        emit("stage", {"stage": "analyzing"})
        post = read_post(client, job.url)
        chosen = [p for p in items if 1 <= p <= len(post.items)]
        if not chosen:
            raise EngineError("bad_options", "none of those items are in the post any more")
        from ..image_convert import finish_images, parse_image_options

        fmt, background = parse_image_options(job.options)
        emit("stage", {"stage": "downloading"})
        stem = safe_file_name(_title(post).replace(".", " "))[:80].strip() or "post"
        files: list[str] = []
        for position in chosen:
            media = post.items[position - 1]
            # Items of a many-item post are numbered by position, so a re-run names them alike.
            base = f"{stem} {position}" if len(post.items) > 1 else stem
            name = f"{base}.{media.ext or 'bin'}"
            if job.options.get("archive") and _already_saved(folder, base, media, fmt):
                continue  # "Skip items already downloaded to this folder"
            if files:
                time.sleep(REQUEST_DELAY)
            part_name, _ = part_names(name, job.job_id)
            part = folder / part_name
            try:
                client.stream(media.url, part, emit, {"Referer": job.url})
                final = move_into_place(part, folder, name)
            except BaseException:
                part.unlink(missing_ok=True)
                if files:
                    emit("log", {"level": "warning",
                                 "message": "some post items could not be downloaded"})  # fmt: skip
                    break
                raise
            files.append(str(final))
        if not files:
            emit("stage", {"stage": "completed"})
            return {
                "title": stem,
                "preset": PRESET_ID,
                "files": [],
                "total_bytes": 0,
                "skipped": True,
                "skipped_reason": "Already downloaded",
            }
        files, notes = finish_images(files, fmt, background, emit)
        emit("stage", {"stage": "completed"})
        result: dict[str, Any] = {
            "title": Path(files[0]).stem,
            "preset": PRESET_ID,
            "files": files,
            "total_bytes": sum(os.path.getsize(p) for p in files),
            "item_count": len(files),
        }
        if notes:
            result["notes"] = notes
        return result
