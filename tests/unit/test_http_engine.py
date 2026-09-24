"""Direct HTTP engine against a local server. No internet access.

The engine refuses loopback hosts, so the tests use public-looking names and patch only name
resolution to point them at the local server. Everything else — URL vetting, redirects, Range,
If-Range, naming, collisions, progress — is the real code path.
"""

from __future__ import annotations

import base64
import io
import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from stuff_downloader_worker import __main__ as worker_main
from stuff_downloader_worker.engines import get_engine
from stuff_downloader_worker.engines import http as http_engine
from stuff_downloader_worker.engines.base import EngineError
from stuff_downloader_worker.protocol import Event, JobSpec

BODY = bytes(range(256)) * 400  # 102,400 bytes
# Partial files carry the job id (see http_engine.part_names); the tests' jobs are all "j1".
PART, META = http_engine.part_names("clip.mp4", "j1")
ETAG = '"v1"'


class Handler(BaseHTTPRequestHandler):
    routes: dict = {}
    seen: list = []

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802 - stdlib name
        Handler.seen.append((self.path, dict(self.headers)))
        route = Handler.routes.get(self.path.split("?")[0])
        if route is None:
            self.send_response(404, "Not Found")
            self.end_headers()
            return
        route(self)


def serve_file(
    body=BODY,
    ctype="video/mp4",
    etag=ETAG,
    ranges=True,
    disposition=None,
    truncate_at=None,
):
    def handler(req):
        start = 0
        rng = req.headers.get("Range")
        if_range = req.headers.get("If-Range")
        partial = ranges and rng and (if_range is None or if_range == etag)
        if partial:
            start = int(rng.split("=")[1].split("-")[0])
            end = int(rng.split("-")[1]) if rng.split("-")[1] else len(body) - 1
            if start >= len(body):
                req.send_response(416)
                req.send_header("Content-Range", f"bytes */{len(body)}")
                req.end_headers()
                return
            chunk = body[start : end + 1]
            req.send_response(206)
            req.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
        else:
            chunk = body
            req.send_response(200)
        req.send_header("Content-Type", ctype)
        req.send_header("Content-Length", str(len(chunk)))
        if etag:
            req.send_header("ETag", etag)
        if disposition:
            req.send_header("Content-Disposition", disposition)
        req.end_headers()
        if truncate_at is not None and not partial:
            req.wfile.write(chunk[:truncate_at])
            req.wfile.flush()
            req.connection.shutdown(socket.SHUT_RDWR)
            return
        req.wfile.write(chunk)

    return handler


def redirect(location, status=302):
    def handler(req):
        req.send_response(status)
        req.send_header("Location", location)
        req.end_headers()

    return handler


@pytest.fixture
def server(monkeypatch):
    Handler.routes = {}
    Handler.seen = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    monkeypatch.setattr(http_engine, "_resolve", lambda host, p: "127.0.0.1")
    yield f"http://files.example.com:{port}"
    httpd.shutdown()
    httpd.server_close()


def _collect():
    events = []
    return events, lambda kind, data: events.append((kind, data))


def _download(base, path, out, **options):
    spec = JobSpec("j1", "http", base + path, str(out), {"mode": "download", **options})
    events, emit = _collect()
    result = get_engine("http").download(spec, emit)
    return result, events


def _analyze(base, path):
    events, emit = _collect()
    spec = JobSpec("j1", "http", base + path, ".", {"mode": "analyze"})
    return get_engine("http").download(spec, emit), events


# ── analyze ──────────────────────────────────────────────────────────────────────────────
def test_analyze_reports_the_file_without_downloading_it(server):
    Handler.routes["/media/clip.mp4"] = serve_file()
    info, events = _analyze(server, "/media/clip.mp4?sig=SECRET")
    assert info["kind"] == "video" and info["title"] == "clip" and info["ext"] == "mp4"
    assert info["filesize"] == len(BODY) and info["resumable"] is True
    # Only the durable link leaves: no query (the signature), so nothing replayable.
    assert info["webpage"] == server + "/media/clip.mp4"
    rest = {k: v for k, v in info.items() if k != "webpage"}
    assert "SECRET" not in json.dumps(info) and "example.com" not in json.dumps(rest)
    assert Handler.seen[0][1]["Range"] == "bytes=0-0"
    assert [d["stage"] for k, d in events if k == "stage"] == ["analyzing", "completed"]


def test_analyze_refuses_a_page_that_only_looks_like_a_file(server):
    Handler.routes["/fake.mp4"] = serve_file(body=b"<html>hi</html>", ctype="text/html")
    with pytest.raises(EngineError) as info:
        _analyze(server, "/fake.mp4")
    assert info.value.code == "unsupported" and "unsupported url" in info.value.message


def test_analyze_accepts_octet_stream_only_with_a_media_name(server):
    Handler.routes["/a.webm"] = serve_file(ctype="application/octet-stream")
    Handler.routes["/a.exe"] = serve_file(ctype="application/octet-stream")
    assert _analyze(server, "/a.webm")[0]["ext"] == "webm"
    with pytest.raises(EngineError, match="not a media file"):
        _analyze(server, "/a.exe")


def test_http_errors_use_status_text_and_never_the_url(server):
    with pytest.raises(EngineError) as info:
        _analyze(server, "/missing.mp4?token=SECRET")
    assert info.value.message == "HTTP Error 404: Not Found"
    assert info.value.code == "unsupported"


# ── download ─────────────────────────────────────────────────────────────────────────────
def test_download_streams_the_file_with_progress(server, tmp_path):
    Handler.routes["/v/clip.mp4"] = serve_file()
    result, events = _download(server, "/v/clip.mp4", tmp_path, preset="original_file")
    target = tmp_path / "clip.mp4"
    assert target.read_bytes() == BODY
    assert result["files"] == [str(target)] and result["total_bytes"] == len(BODY)
    assert not (tmp_path / PART).exists()
    assert not (tmp_path / META).exists()
    progress = [d for k, d in events if k == "progress"]
    assert progress[-1]["downloaded_bytes"] == len(BODY) and progress[-1]["percent"] == 100.0
    assert set(progress[-1]) == {"downloaded_bytes", "total_bytes", "percent", "speed", "eta"}
    stages = [d["stage"] for k, d in events if k == "stage"]
    assert stages == ["analyzing", "downloading", "completed"]


def test_download_never_overwrites_an_existing_file(server, tmp_path):
    Handler.routes["/clip.mp4"] = serve_file()
    (tmp_path / "clip.mp4").write_bytes(b"mine")
    (tmp_path / "clip (2).mp4").write_bytes(b"mine too")
    result, _ = _download(server, "/clip.mp4", tmp_path, preset="original_file")
    assert result["files"] == [str(tmp_path / "clip (3).mp4")]
    assert (tmp_path / "clip.mp4").read_bytes() == b"mine"


def test_download_resumes_a_part_file_with_range_and_if_range(server, tmp_path):
    Handler.routes["/clip.mp4"] = serve_file()
    half = len(BODY) // 2
    (tmp_path / PART).write_bytes(BODY[:half])
    (tmp_path / META).write_text(json.dumps({"validator": ETAG}))
    result, events = _download(server, "/clip.mp4", tmp_path, preset="original_file")
    assert (tmp_path / "clip.mp4").read_bytes() == BODY
    headers = Handler.seen[-1][1]
    assert headers["Range"] == f"bytes={half}-" and headers["If-Range"] == ETAG
    first = next(d for k, d in events if k == "progress")
    assert first["downloaded_bytes"] > half  # progress counts the resumed part


def test_download_restarts_when_the_file_changed_on_the_server(server, tmp_path):
    Handler.routes["/clip.mp4"] = serve_file(etag='"v2"')
    (tmp_path / PART).write_bytes(b"x" * 1000)
    (tmp_path / META).write_text(json.dumps({"validator": ETAG}))
    _download(server, "/clip.mp4", tmp_path, preset="original_file")
    assert (tmp_path / "clip.mp4").read_bytes() == BODY  # the stale part was discarded


def test_download_without_a_validator_starts_over(server, tmp_path):
    Handler.routes["/clip.mp4"] = serve_file()
    (tmp_path / PART).write_bytes(b"x" * 1000)
    _download(server, "/clip.mp4", tmp_path, preset="original_file")
    assert "If-Range" not in Handler.seen[-1][1]
    assert (tmp_path / "clip.mp4").read_bytes() == BODY


def test_a_complete_part_is_finished_without_refetching(server, tmp_path):
    Handler.routes["/clip.mp4"] = serve_file()
    (tmp_path / PART).write_bytes(BODY)
    (tmp_path / META).write_text(json.dumps({"validator": ETAG}))
    result, _ = _download(server, "/clip.mp4", tmp_path, preset="original_file")
    assert result["total_bytes"] == len(BODY)
    assert (tmp_path / "clip.mp4").read_bytes() == BODY


def test_an_interrupted_download_keeps_the_part_for_resume(server, tmp_path):
    Handler.routes["/clip.mp4"] = serve_file(truncate_at=5000)
    with pytest.raises(EngineError) as info:
        _download(server, "/clip.mp4", tmp_path, preset="original_file")
    assert "incomplete" in info.value.message or "reset" in info.value.message
    assert (tmp_path / PART).stat().st_size == 5000
    assert not (tmp_path / "clip.mp4").exists()
    # The next run picks up where this one stopped.
    Handler.routes["/clip.mp4"] = serve_file()
    _download(server, "/clip.mp4", tmp_path, preset="original_file")
    assert Handler.seen[-1][1]["Range"] == "bytes=5000-"
    assert (tmp_path / "clip.mp4").read_bytes() == BODY


@pytest.mark.parametrize(
    ("disposition", "expected"),
    [
        ('attachment; filename="..\\..\\evil.mp4"', "evil.mp4"),
        ('attachment; filename="../../x/CON.mp4"', "download.mp4"),
        ("attachment; filename*=UTF-8''caf%C3%A9%20%3Cnight%3E.mp3", "café _night_.mp3"),
    ],
)
def test_header_file_names_are_sanitized(server, tmp_path, disposition, expected):
    Handler.routes["/get"] = serve_file(ctype="audio/mpeg", disposition=disposition)
    result, _ = _download(server, "/get", tmp_path, preset="original_file")
    assert result["files"] == [str(tmp_path / expected)]


def test_a_missing_extension_comes_from_the_content_type(server, tmp_path):
    Handler.routes["/stream"] = serve_file(ctype="audio/mpeg")
    result, _ = _download(server, "/stream", tmp_path, preset="original_file")
    assert result["files"] == [str(tmp_path / "stream.mp3")]


# ── redirects and URL vetting ────────────────────────────────────────────────────────────
def test_redirects_to_a_public_name_are_followed(server, tmp_path):
    Handler.routes["/go"] = redirect("/real/clip.mp4")
    Handler.routes["/real/clip.mp4"] = serve_file()
    result, _ = _download(server, "/go", tmp_path, preset="original_file")
    assert result["files"] == [str(tmp_path / "clip.mp4")]


@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1/clip.mp4",
        "http://localhost/clip.mp4",
        "http://0x7f.1/clip.mp4",
        "http://[::1]/clip.mp4",
        "http://router.lan/clip.mp4",
        "file:///C:/Windows/win.ini",
        "http://user:pw@cdn.example.com/clip.mp4",
    ],
)
def test_redirects_to_private_or_odd_targets_are_refused(server, tmp_path, location):
    Handler.routes["/go"] = redirect(location)
    with pytest.raises(EngineError) as info:
        _download(server, "/go", tmp_path, preset="original_file")
    assert info.value.code == "unsupported"
    assert "127.0.0.1" not in info.value.message and "localhost" not in info.value.message


def test_redirect_loops_stop(server, tmp_path):
    Handler.routes["/loop"] = redirect("/loop")
    with pytest.raises(EngineError, match="too many redirects"):
        _download(server, "/loop", tmp_path, preset="original_file")


def test_resolve_refuses_a_name_with_any_private_address(monkeypatch):
    def fake_getaddrinfo(host, port, type=0):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(EngineError) as info:
        http_engine._resolve("rebind.example.com", 443)
    assert info.value.code == "unsupported"


def test_resolve_returns_a_global_address(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, type=0: [(socket.AF_INET, 1, 6, "", ("93.184.216.34", port))],
    )
    assert http_engine._resolve("cdn.example.com", 443) == "93.184.216.34"


def test_connection_goes_to_the_checked_address_only(monkeypatch):
    target = http_engine.check_url("https://cdn.example.com/a.mp4")
    monkeypatch.setattr(http_engine, "_resolve", lambda host, port: "93.184.216.34")
    calls = []
    monkeypatch.setattr(
        socket, "create_connection", lambda addr, *a, **k: calls.append(addr) or 1 / 0
    )
    conn = http_engine._connect(target)
    assert conn.host == "cdn.example.com"  # TLS still verifies the real name
    with pytest.raises(ZeroDivisionError):
        conn._create_connection(("cdn.example.com", 443), 5)
    assert calls == [("93.184.216.34", 443)]


@pytest.mark.parametrize(
    "url",
    ["ftp://cdn.example.com/a.mp4", "http://10.0.0.1/a.mp4", "http://intranet/a.mp4",
     "http://cdn.example.com:bad/a.mp4", "http://a_b.example.com/a.mp4"],
)  # fmt: skip
def test_check_url_refuses(url):
    with pytest.raises(EngineError):
        http_engine.check_url(url)


# ── options and protocol ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "options",
    [
        {"mode": "download"},
        {"mode": "download", "preset": "video_best"},
        {"mode": "download", "preset": "original_file", "output": "C:/x"},
        {"mode": "analyze", "site_login": {"source": "browser", "browser": "firefox"}},
        {"mode": "delete"},
    ],
)
def test_bad_options_are_refused_before_any_request(options, monkeypatch):
    monkeypatch.setattr(http_engine, "_resolve", lambda *a: pytest.fail("no request expected"))
    with pytest.raises(EngineError) as info:
        get_engine("http").download(
            JobSpec("j", "http", "https://a.example.com/x.mp4", ".", options), None
        )
    assert info.value.code == "bad_options"


def test_progress_body_counts_only_this_runs_bytes_for_speed():
    body = http_engine.progress(done=600, total=1000, offset=400, elapsed=2.0)
    assert body == {
        "downloaded_bytes": 600,
        "total_bytes": 1000,
        "percent": 60.0,
        "speed": 100.0,
        "eta": 4.0,
    }
    assert http_engine.progress(10, None, 0, 1.0)["percent"] is None


def test_worker_speaks_json_lines_for_the_http_engine(server, tmp_path, monkeypatch):
    Handler.routes["/clip.mp4"] = serve_file()
    spec = JobSpec(
        "job-7",
        "http",
        server + "/clip.mp4",
        str(tmp_path),
        {"mode": "download", "preset": "original_file"},
    )
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert worker_main.run_job("http", spec.to_json()) == 0
    events = [Event.from_line(line) for line in out.getvalue().splitlines()]
    assert all(e.job_id == "job-7" for e in events)
    assert events[-1].type == "result" and events[-1].data["files"] == [str(tmp_path / "clip.mp4")]
    assert any(e.type == "progress" for e in events)


def test_worker_reports_http_failures_as_one_error_event(server, tmp_path, monkeypatch):
    spec = JobSpec(
        "job-8", "http", server + "/nope.mp4?sig=SECRET", str(tmp_path), {"mode": "analyze"}
    )
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert worker_main.run_job("http", spec.to_json()) == 1
    (event,) = [Event.from_line(line) for line in out.getvalue().splitlines() if '"error"' in line]
    assert event.data["code"] == "unsupported" and "SECRET" not in out.getvalue()


# ── partial files are owned by one job ───────────────────────────────────────────────────
def test_part_names_are_per_job_and_sanitized():
    assert http_engine.part_names("a.mp4", "abc123")[1] == "a.mp4.abc123.part.json"
    assert http_engine.part_names("a.mp4", r"..\x/y") == ("a.mp4.xy.part", "a.mp4.xy.part.json")
    assert http_engine.part_names("a.mp4", "...")[0] == "a.mp4.job.part"


def test_another_jobs_part_is_never_resumed_or_touched(server, tmp_path):
    Handler.routes["/clip.mp4"] = serve_file()
    other_part, other_meta = http_engine.part_names("clip.mp4", "someone-else")
    (tmp_path / other_part).write_bytes(b"x" * 1000)
    (tmp_path / other_meta).write_text(json.dumps({"validator": ETAG}))
    _download(server, "/clip.mp4", tmp_path, preset="original_file")
    assert "Range" not in Handler.seen[-1][1] or Handler.seen[-1][1]["Range"] == "bytes=0-0"
    assert (tmp_path / "clip.mp4").read_bytes() == BODY
    assert (tmp_path / other_part).read_bytes() == b"x" * 1000  # left for its own job


def test_two_concurrent_jobs_for_the_same_name_both_finish_intact(server, tmp_path):
    Handler.routes["/clip.mp4"] = serve_file()
    results, errors = {}, []

    def run(job_id):
        spec = JobSpec(
            job_id,
            "http",
            server + "/clip.mp4",
            str(tmp_path),
            {"mode": "download", "preset": "original_file"},
        )
        try:
            results[job_id] = get_engine("http").download(spec, lambda *a: None)
        except Exception as exc:  # surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(f"job{i}",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert errors == []
    files = sorted(r["files"][0] for r in results.values())
    assert len(set(files)) == 4  # four distinct outputs, none overwritten
    for path in files:
        assert open(path, "rb").read() == BODY
    assert not list(tmp_path.glob("*.part")) and not list(tmp_path.glob("*.part.json"))


def test_move_into_place_never_replaces_a_file_that_appears_late(tmp_path, monkeypatch):
    part = tmp_path / "p.part"
    part.write_bytes(b"new")
    (tmp_path / "clip.mp4").write_bytes(b"theirs")  # e.g. another job won the race
    final = http_engine.move_into_place(part, tmp_path, "clip.mp4")
    assert final == tmp_path / "clip (2).mp4" and final.read_bytes() == b"new"
    assert (tmp_path / "clip.mp4").read_bytes() == b"theirs" and not part.exists()


def test_move_into_place_without_hard_links_still_never_overwrites(tmp_path, monkeypatch):
    def no_links(*args):
        raise OSError("hard links not supported")

    monkeypatch.setattr(http_engine.os, "link", no_links)
    part = tmp_path / "p.part"
    part.write_bytes(b"new")
    (tmp_path / "clip.mp4").write_bytes(b"theirs")
    final = http_engine.move_into_place(part, tmp_path, "clip.mp4")
    assert final.name == "clip (2).mp4" and (tmp_path / "clip.mp4").read_bytes() == b"theirs"


@pytest.mark.parametrize(
    ("chosen", "expected"),
    [
        ("holiday", "holiday.mp4"),
        (r"..\..\evil", "____evil.mp4"),
        ("%(title)s", "%(title)s.mp4"),
        ("CON .txt", "clip.mp4"),  # a device name keeps the server's name, never "download"
        ("   ", "clip.mp4"),
    ],
)
def test_a_chosen_name_is_sanitized_and_blank_keeps_the_file_name(
    server, tmp_path, chosen, expected
):
    Handler.routes["/v/clip.mp4"] = serve_file()
    result, _ = _download(
        server, "/v/clip.mp4", tmp_path, preset="original_file", output_name=chosen
    )
    assert result["files"] == [str(tmp_path / expected)]


# ── direct image preview (item 4) ─────────────────────────────────────────────────────────
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 500


def test_analyzing_an_image_returns_the_image_as_its_preview(server):
    Handler.routes["/p/photo.png"] = serve_file(body=PNG, ctype="image/png")
    info, _ = _analyze(server, "/p/photo.png")
    assert base64.b64decode(info["preview"]["data"]) == PNG


def test_a_video_gets_no_image_preview(server):
    Handler.routes["/v/clip.mp4"] = serve_file()
    info, _ = _analyze(server, "/v/clip.mp4")
    assert info["preview"] is None


def test_an_image_over_five_megabytes_gets_no_preview(server, monkeypatch):
    monkeypatch.setattr(http_engine, "MAX_PREVIEW_BYTES", 400)
    Handler.routes["/p/big.png"] = serve_file(body=PNG, ctype="image/png")
    info, _ = _analyze(server, "/p/big.png")
    assert info["preview"] is None and info["filesize"] == len(PNG)


def test_a_failed_preview_still_analyzes_the_image(server, monkeypatch):
    Handler.routes["/p/photo.png"] = serve_file(body=PNG, ctype="image/png")
    calls = []
    real = http_engine.open_url

    def second_call_fails(url, headers=None, method="GET", https_only=False):
        calls.append(url)
        if len(calls) > 1:
            raise OSError("connection reset")
        return real(url, headers, method, https_only)

    monkeypatch.setattr(http_engine, "open_url", second_call_fails)
    info, events = _analyze(server, "/p/photo.png")
    assert info["kind"] == "image" and info["preview"] is None
    assert any(k == "log" and "preview failed" in d["message"] for k, d in events)


# ── media kind by MIME (plan §5.3, R1 acceptance) ────────────────────────────────────────
JPEG = b"\xff\xd8\xff\xe0" + b"j" * 300


@pytest.mark.parametrize(
    ("path", "ctype", "body", "kind", "tabs", "ext"),
    [
        ("/d/clip.mp4", "video/mp4", BODY, "video", ["video"], "mp4"),
        ("/d/song.mp3", "audio/mpeg", BODY, "audio", ["audio"], "mp3"),
        ("/d/photo.jpg", "image/jpeg", JPEG, "image", ["image"], "jpg"),
        # The image is judged by its type, not its URL: no extension at all here.
        ("/media/Gx1AbC?format=jpg&name=large", "image/jpeg", JPEG, "image", ["image"], "jpg"),
        # A generic type falls back to the extension.
        ("/d/tune.flac", "application/octet-stream", BODY, "audio", ["audio"], "flac"),
    ],
    ids=["mp4", "mp3", "jpg", "format-jpg", "octet-flac"],
)
def test_direct_files_report_their_kind_and_tabs(server, path, ctype, body, kind, tabs, ext):
    Handler.routes[path.split("?")[0]] = serve_file(body=body, ctype=ctype)
    info, _ = _analyze(server, path)
    assert (info["kind"], info["tabs"], info["ext"]) == (kind, tabs, ext)
    assert [row["id"] for row in info[f"{kind}_rows"]] == [f"{kind[0]}:orig"]
    assert (info["preview"] is not None) == (kind == "image")
    protocol_check(info)


def protocol_check(info):
    from stuff_downloader.core.protocol import validate_media_result

    assert validate_media_result(json.loads(json.dumps(info))) is not None


def test_an_extensionless_image_is_saved_under_its_real_type(server, tmp_path):
    Handler.routes["/media/Gx1AbC"] = serve_file(body=JPEG, ctype="image/jpeg")
    path = "/media/Gx1AbC?format=jpg&name=large"
    result, _ = _download(server, path, tmp_path, preset="original_file")
    assert Path(result["files"][0]).name == "Gx1AbC.jpg"


def test_an_image_is_named_by_its_type_not_its_path(server, tmp_path):
    Handler.routes["/p/photo.jpg"] = serve_file(body=PNG, ctype="image/png")
    result, _ = _download(server, "/p/photo.jpg", tmp_path, preset="original_file")
    assert Path(result["files"][0]).name == "photo.png"


def test_an_unlisted_image_type_is_refused(server):
    Handler.routes["/p/logo.svg"] = serve_file(body=b"<svg/>", ctype="image/svg+xml")
    with pytest.raises(EngineError, match="not a media file"):
        _analyze(server, "/p/logo.svg")


def test_an_image_whose_type_changes_mid_download_is_refused(server, tmp_path):
    calls = []

    def flip(req):
        calls.append(1)
        serve_file(body=JPEG, ctype="image/jpeg" if len(calls) == 1 else "image/png")(req)

    Handler.routes["/p/shot"] = flip
    with pytest.raises(EngineError, match="image type changed"):
        _download(server, "/p/shot", tmp_path, preset="original_file")


# ── image format (item 6B) ────────────────────────────────────────────────────────────────
FFMPEG = Path(__file__).resolve().parents[2] / "tools" / "ffmpeg.exe"
needs_ffmpeg = pytest.mark.skipif(not FFMPEG.is_file(), reason="bundled ffmpeg not fetched")


def _png(width=4, height=3) -> bytes:
    import struct
    import zlib

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(b"\x00" + b"\xff\x00\x00\x80" * width for _ in range(height))  # RGBA
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


@needs_ffmpeg
def test_a_direct_png_is_converted_and_the_jpg_is_reported(server, tmp_path, monkeypatch):
    monkeypatch.setenv("STUFF_DOWNLOADER_TOOLS_DIR", str(FFMPEG.parent))
    Handler.routes["/p/photo.png"] = serve_file(body=_png(), ctype="image/png")
    result, events = _download(
        server, "/p/photo.png", tmp_path, preset="original_file", image_format="jpg"
    )
    final = tmp_path / "photo.jpg"
    assert result["files"] == [str(final)]
    assert final.read_bytes()[:3] == b"\xff\xd8\xff"
    assert not (tmp_path / "photo.png").exists()
    assert result["total_bytes"] == final.stat().st_size
    assert "converting" in [d["stage"] for k, d in events if k == "stage"]


def test_a_video_ignores_the_image_format(server, tmp_path):
    Handler.routes["/v/clip.mp4"] = serve_file()
    result, _ = _download(server, "/v/clip.mp4", tmp_path, preset="original_file",
                          image_format="png")
    assert result["files"] == [str(tmp_path / "clip.mp4")] and "notes" not in result


def test_without_ffmpeg_the_original_image_is_kept_with_a_note(server, tmp_path, monkeypatch):
    monkeypatch.delenv("STUFF_DOWNLOADER_TOOLS_DIR", raising=False)
    Handler.routes["/p/photo.png"] = serve_file(body=_png(), ctype="image/png")
    result, _ = _download(
        server, "/p/photo.png", tmp_path, preset="original_file", image_format="jpg"
    )
    assert result["files"] == [str(tmp_path / "photo.png")]
    assert result["notes"] == ["Could not convert to JPG; kept the original."]


@pytest.mark.parametrize(
    "extra", [{"image_format": "tiff"}, {"image_background": "red"}, {"image_format": 1}]
)
def test_bad_image_options_are_refused_before_any_request(server, tmp_path, extra):
    Handler.seen.clear()
    with pytest.raises(EngineError) as info:
        _download(server, "/p/photo.png", tmp_path, preset="original_file", **extra)
    assert info.value.code == "bad_options" and Handler.seen == []


# ── previews for direct video and audio (plan §5.6, R3: P6) ──────────────────────────────
TOOLS = Path(__file__).resolve().parents[2] / "tools"
HAS_TOOLS = (TOOLS / "ffmpeg.exe").is_file() and (TOOLS / "ffprobe.exe").is_file()
needs_tools = pytest.mark.skipif(not HAS_TOOLS, reason="the bundled ffmpeg/ffprobe are not here")


def _make_media(tmp_path, name, *args):
    import subprocess

    out = tmp_path / name
    cmd = [str(TOOLS / "ffmpeg.exe"), "-hide_banner", "-loglevel", "error", "-y", *args, str(out)]
    subprocess.run(cmd, check=True, capture_output=True, timeout=60)
    return out.read_bytes()


@needs_tools
def test_a_direct_video_gets_its_details_and_a_frame(server, tmp_path, monkeypatch):
    monkeypatch.setenv("STUFF_DOWNLOADER_TOOLS_DIR", str(TOOLS))
    body = _make_media(
        tmp_path, "clip.mp4",
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=4",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
    )  # fmt: skip
    Handler.routes["/d/clip.mp4"] = serve_file(body=body, ctype="video/mp4")
    info, _ = _analyze(server, "/d/clip.mp4")
    assert (info["width"], info["height"], info["vcodec"]) == (320, 240, "h264")
    assert 3.5 < info["duration"] < 4.5 and info["acodec"] == "aac"
    assert info["source_audio"]["codec"] == "AAC"
    assert info["video_rows"][0]["height"] == 240
    assert base64.b64decode(info["preview"]["data"])[:2] == b"\xff\xd8"  # a JPEG frame
    protocol_check(info)


@needs_tools
def test_a_direct_mp3_gets_duration_bitrate_and_its_embedded_cover(server, tmp_path, monkeypatch):
    monkeypatch.setenv("STUFF_DOWNLOADER_TOOLS_DIR", str(TOOLS))
    cover = tmp_path / "cover.png"
    cover.write_bytes(_png())
    body = _make_media(
        tmp_path, "song.mp3",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-i", str(cover),
        "-map", "0:a", "-map", "1:v", "-c:a", "libmp3lame", "-b:a", "192k",
        "-c:v", "png", "-disposition:v", "attached_pic", "-id3v2_version", "3",
    )  # fmt: skip
    Handler.routes["/d/song.mp3"] = serve_file(body=body, ctype="audio/mpeg")
    info, _ = _analyze(server, "/d/song.mp3")
    assert info["kind"] == "audio" and 2.5 < info["duration"] < 3.5
    assert info["source_audio"] == {"codec": "MP3", "abr_kbps": 192}
    assert "vcodec" not in info and "has_cover" not in info  # the cover is not a video
    assert base64.b64decode(info["preview"]["data"])[:2] == b"\xff\xd8"
    protocol_check(info)


@needs_tools
def test_a_direct_mp3_without_a_cover_has_details_and_no_preview(server, tmp_path, monkeypatch):
    monkeypatch.setenv("STUFF_DOWNLOADER_TOOLS_DIR", str(TOOLS))
    body = _make_media(
        tmp_path, "plain.mp3", "-f", "lavfi", "-i", "sine=duration=2", "-b:a", "128k"
    )
    Handler.routes["/d/plain.mp3"] = serve_file(body=body, ctype="audio/mpeg")
    info, _ = _analyze(server, "/d/plain.mp3")
    assert info["source_audio"]["abr_kbps"] == 128 and info["preview"] is None


def test_without_ffprobe_a_direct_video_still_analyzes(server, monkeypatch):
    monkeypatch.delenv("STUFF_DOWNLOADER_TOOLS_DIR", raising=False)
    Handler.routes["/d/clip.mp4"] = serve_file()
    info, events = _analyze(server, "/d/clip.mp4")
    assert info["kind"] == "video" and info["preview"] is None and "duration" not in info
    assert any(k == "log" and "ffprobe is missing" in d["message"] for k, d in events)


def _fake_tools(tmp_path, monkeypatch):
    for exe in ("ffmpeg.exe", "ffprobe.exe"):
        (tmp_path / exe).write_bytes(b"")
    monkeypatch.setenv("STUFF_DOWNLOADER_TOOLS_DIR", str(tmp_path))


def test_the_tools_never_see_the_remote_link(server, tmp_path, monkeypatch):
    _fake_tools(tmp_path, monkeypatch)
    calls = []
    probe = {
        "format": {"duration": "100.0", "bit_rate": "900000"},
        "streams": [{"codec_type": "video", "codec_name": "h264", "width": 640, "height": 360}],
    }

    def fake_run(args, limit):
        calls.append(args)
        return json.dumps(probe).encode() if "-show_format" in args else b"\xff\xd8frame"

    monkeypatch.setattr(http_engine, "_run_tool", fake_run)
    Handler.routes["/d/clip.mp4"] = serve_file()
    info, _ = _analyze(server, "/d/clip.mp4?sig=SECRET")
    assert len(calls) == 2
    for args in calls:
        joined = " ".join(args)
        assert "files.example.com" not in joined and "SECRET" not in joined
        assert args[args.index("-protocol_whitelist") + 1] == "http,tcp"
        local = [a for a in args if a.startswith("http://")]
        assert len(local) == 1 and local[0].startswith("http://127.0.0.1:")
    frame = calls[1]
    assert frame[frame.index("-ss") + 1] == "10.000"  # 10% of 100 s
    assert frame[frame.index("-frames:v") + 1] == "1"
    assert info["preview"] is not None and info["tbr"] == 900


def test_a_failing_tool_costs_only_the_preview(server, tmp_path, monkeypatch):
    _fake_tools(tmp_path, monkeypatch)
    monkeypatch.setattr(http_engine, "_run_tool", lambda args, limit: None)
    Handler.routes["/d/clip.mp4"] = serve_file()
    info, events = _analyze(server, "/d/clip.mp4")
    assert info["kind"] == "video" and info["preview"] is None
    assert any(k == "log" and "ffprobe could not read" in d["message"] for k, d in events)


def _relay_get(relay_url, headers=None):
    import urllib.error
    import urllib.request

    req = urllib.request.Request(relay_url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def test_the_relay_serves_ranges_of_the_checked_link_only(server):
    Handler.routes["/d/clip.mp4"] = serve_file()
    with http_engine.Relay(server + "/d/clip.mp4") as relay:
        status, body = _relay_get(relay.local_url, {"Range": "bytes=10-19"})
        assert (status, body) == (206, BODY[10:20])
        assert _relay_get(relay.local_url.rsplit("/", 1)[0] + "/guess")[0] == 404


@pytest.mark.parametrize(
    "target", ["http://localhost/x.mp4", "http://10.0.0.7/x.mp4", "file:///C:/x.mp4"]
)
def test_the_relay_refuses_a_redirect_to_a_private_target(server, target):
    Handler.routes["/d/clip.mp4"] = redirect(target)
    with http_engine.Relay(server + "/d/clip.mp4") as relay:
        assert _relay_get(relay.local_url)[0] == 502


def test_the_relay_stops_at_its_byte_cap(server, monkeypatch):
    monkeypatch.setattr(http_engine, "RELAY_BYTE_CAP", 1000)
    Handler.routes["/d/clip.mp4"] = serve_file()
    with http_engine.Relay(server + "/d/clip.mp4") as relay:
        try:
            _, body = _relay_get(relay.local_url)
        except Exception:  # a short body is a broken response to urllib, which is the point
            body = b""
        assert len(body) < len(BODY)


@pytest.mark.parametrize(
    ("raw", "facts"),
    [
        (b"not json", {}),
        (b"[]", {}),
        (
            json.dumps({"format": {"duration": "nan", "bit_rate": "-5"}, "streams": "x"}).encode(),
            {},
        ),
        (
            json.dumps({"streams": [
                {"codec_type": "audio", "codec_name": "mp3<script>", "bit_rate": "320000"},
                {"codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}},
            ]}).encode(),
            {"acodec": "mp3script", "abr": 320, "has_cover": True},
        ),
        (
            json.dumps({"streams": [
                {"codec_type": "video", "codec_name": "vp9", "width": True, "height": 99999},
            ]}).encode(),
            {"vcodec": "vp9"},
        ),
    ],
    ids=["garbage", "list", "bad-numbers", "audio-cover", "bad-sizes"],
)  # fmt: skip
def test_probe_output_is_sanitized(raw, facts):
    assert http_engine.parse_probe(raw) == facts


# ── page images when yt-dlp has none (plan §5.6) ─────────────────────────────────────────
@pytest.mark.parametrize(
    ("page", "found"),
    [
        ('<meta property="og:image" content="https://cdn.example.com/og.jpg">',
         "https://cdn.example.com/og.jpg"),
        ("<meta content='/img/tw.jpg' name='twitter:image'>",
         "https://news.example.com/img/tw.jpg"),
        ('<meta property="og:image" content="https://a.example.com/x.jpg?a=1&amp;b=2">',
         "https://a.example.com/x.jpg?a=1&b=2"),
        ('<script type="application/ld+json">{"@graph":[{"@type":"VideoObject",'
         '"thumbnailUrl":["https://cdn.example.com/ld.jpg"]}]}</script>',
         "https://cdn.example.com/ld.jpg"),
        ('<meta property="og:image" content="http://cdn.example.com/plain.jpg">', None),
        ('<meta property="og:image" content="javascript:alert(1)">', None),
        ('<script type="application/ld+json">{broken</script>', None),
        ("<html>nothing</html>", None),
    ],
    ids=["og", "twitter-relative", "entities", "json-ld", "plain-http", "js", "bad-json", "none"],
)  # fmt: skip
def test_page_image_url(page, found):
    assert http_engine.page_image_url(page, "https://news.example.com/story/1") == found


def test_page_preview_fetches_the_page_then_its_image(server, monkeypatch):
    Handler.routes["/story"] = serve_file(
        body=b'<meta property="og:image" content="https://img.example.com/og.png">',
        ctype="text/html; charset=utf-8",
    )
    fetched = []
    real = http_engine.fetch_bytes

    def spy(url, limit, seconds=15.0, https_only=False, accept=()):
        fetched.append((url, https_only))
        if url.startswith("https://img.example.com"):
            return PNG
        return real(url, limit, seconds, https_only, accept)

    monkeypatch.setattr(http_engine, "fetch_bytes", spy)
    assert http_engine.page_preview(server + "/story", lambda *_: None) == PNG
    assert fetched == [(server + "/story", False), ("https://img.example.com/og.png", True)]


def test_an_https_only_fetch_refuses_plain_http(server):
    with pytest.raises(EngineError, match="only http and https"):
        http_engine.fetch_bytes(server + "/x.png", 1000, https_only=True)


def test_page_preview_ignores_a_page_that_is_not_html(server):
    Handler.routes["/d/clip.mp4"] = serve_file()
    assert http_engine.page_preview(server + "/d/clip.mp4", lambda *_: None) is None


# ── a direct video's audio and frame rows (plan §5.3, R3) ────────────────────────────────
def _clip(tmp_path, audio=True):
    sound = ["-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-c:a", "aac", "-b:a", "128k",
             "-shortest"] if audio else []  # fmt: skip
    return _make_media(
        tmp_path, "src.mp4",
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=4", *sound,
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
    )  # fmt: skip


def _probe(path):
    import subprocess

    out = subprocess.run(
        [str(TOOLS / "ffprobe.exe"), "-v", "error", "-print_format", "json", "-show_format",
         "-show_streams", str(path)],
        check=True, capture_output=True, timeout=30,
    ).stdout  # fmt: skip
    return json.loads(out)


def _row_download(server, path, out, tab, row_id, container=None, **extra):
    options = {"tab": tab, "row_id": row_id, **extra}
    if container is not None:
        options["container"] = container
    return _download(server, path, out, **options)


@needs_tools
def test_a_direct_video_offers_its_audio_and_a_frame(server, tmp_path, monkeypatch):
    monkeypatch.setenv("STUFF_DOWNLOADER_TOOLS_DIR", str(TOOLS))
    Handler.routes["/d/clip.mp4"] = serve_file(body=_clip(tmp_path), ctype="video/mp4")
    info, _ = _analyze(server, "/d/clip.mp4")
    assert info["tabs"] == ["video", "audio", "image"]
    ids = [r["id"] for r in info["audio_rows"]]
    assert ids[:5] == ["a:mp3:320", "a:mp3:256", "a:mp3:192", "a:mp3:128", "a:mp3:64"]
    assert {"a:m4a", "a:flac", "a:wav"} <= set(ids) and "a:opus" not in ids  # AAC source
    m4a = next(r for r in info["audio_rows"] if r["id"] == "a:m4a")
    assert m4a["copy"] is True  # the AAC stream is copied, not re-encoded
    assert info["image_rows"] == [
        {"id": "i:frame", "frame": True, "ext": "jpg", "default": True, "width": 320,
         "height": 240}
    ]
    protocol_check(info)


@needs_tools
def test_a_silent_direct_video_offers_only_a_frame(server, tmp_path, monkeypatch):
    monkeypatch.setenv("STUFF_DOWNLOADER_TOOLS_DIR", str(TOOLS))
    Handler.routes["/d/clip.mp4"] = serve_file(body=_clip(tmp_path, audio=False))
    info, _ = _analyze(server, "/d/clip.mp4")
    assert info["tabs"] == ["video", "image"] and info["audio_rows"] == []


def test_without_ffmpeg_a_direct_video_has_only_its_video_row(server, monkeypatch):
    monkeypatch.delenv("STUFF_DOWNLOADER_TOOLS_DIR", raising=False)
    Handler.routes["/d/clip.mp4"] = serve_file()
    info, _ = _analyze(server, "/d/clip.mp4")
    assert info["tabs"] == ["video"] and info["image_rows"] == []


@needs_tools
@pytest.mark.parametrize(
    ("row_id", "ext", "codec"),
    [
        ("a:mp3:192", "mp3", "mp3"),
        ("a:m4a", "m4a", "aac"),
        ("a:flac", "flac", "flac"),
        ("a:wav", "wav", "pcm_s16le"),
        ("a:opus", "opus", "opus"),  # not the source codec: encoded
    ],
)
def test_a_direct_video_row_saves_only_its_audio(
    server, tmp_path, monkeypatch, row_id, ext, codec
):
    monkeypatch.setenv("STUFF_DOWNLOADER_TOOLS_DIR", str(TOOLS))
    src = tmp_path / "src"
    src.mkdir()
    Handler.routes["/d/clip.mp4"] = serve_file(body=_clip(src), ctype="video/mp4")
    out = tmp_path / "out"
    out.mkdir()
    result, events = _row_download(server, "/d/clip.mp4", out, "audio", row_id,
                                   edited_title="My clip")  # fmt: skip
    saved = Path(result["files"][0])
    assert saved == out / f"My clip.{ext}"
    assert sorted(p.name for p in out.iterdir()) == [saved.name]  # no video, no temp files
    streams = _probe(saved)["streams"]
    assert [s["codec_type"] for s in streams] == ["audio"] and streams[0]["codec_name"] == codec
    if row_id == "a:mp3:192":
        assert 180_000 <= int(_probe(saved)["format"]["bit_rate"]) <= 200_000
    assert "converting" in [d["stage"] for k, d in events if k == "stage"]


@needs_tools
@pytest.mark.parametrize(
    ("container", "ext", "magic"),
    [("original", "jpg", b"\xff\xd8"), ("png", "png", b"\x89PNG"), ("webp", "webp", b"RIFF")],
)
def test_a_direct_video_frame_row_saves_one_image(
    server, tmp_path, monkeypatch, container, ext, magic
):
    monkeypatch.setenv("STUFF_DOWNLOADER_TOOLS_DIR", str(TOOLS))
    src = tmp_path / "src"
    src.mkdir()
    Handler.routes["/d/clip.mp4"] = serve_file(body=_clip(src), ctype="video/mp4")
    out = tmp_path / "out"
    out.mkdir()
    result, _ = _row_download(server, "/d/clip.mp4", out, "image", "i:frame", container)
    saved = Path(result["files"][0])
    assert saved == out / f"clip.{ext}" and saved.read_bytes().startswith(magic)
    assert sorted(p.name for p in out.iterdir()) == [saved.name]


def test_a_frame_row_without_ffmpeg_fails_plainly(server, tmp_path, monkeypatch):
    monkeypatch.delenv("STUFF_DOWNLOADER_TOOLS_DIR", raising=False)
    Handler.routes["/d/clip.mp4"] = serve_file()
    with pytest.raises(EngineError) as info:
        _row_download(server, "/d/clip.mp4", tmp_path, "image", "i:frame", "png")
    assert info.value.code == "convert_error" and "FFmpeg is needed" in str(info.value)


def test_a_broken_ffmpeg_is_reported_and_leaves_no_temp(server, tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    _fake_tools(tools, monkeypatch)  # empty files: starting ffmpeg fails
    out = tmp_path / "out"
    out.mkdir()
    Handler.routes["/d/clip.mp4"] = serve_file()  # not a real video: ffmpeg fails on it
    with pytest.raises(EngineError) as info:
        _row_download(server, "/d/clip.mp4", out, "audio", "a:mp3:320")
    assert info.value.code == "convert_error"
    assert not any(p.name.startswith(".") for p in out.iterdir())


@pytest.mark.parametrize(
    "extra",
    [
        {"extract": "mp3:999"},
        {"extract": "frame; rm"},
        {"extract": ["mp3:320"]},
        {"frame_format": "tiff"},
    ],
)
def test_bad_extract_options_are_refused_before_any_request(server, tmp_path, extra):
    Handler.seen.clear()
    with pytest.raises(EngineError) as info:
        _download(server, "/d/clip.mp4", tmp_path, preset="original_file", **extra)
    assert info.value.code == "bad_options" and Handler.seen == []


def test_row_requests_map_to_extract_options():
    parse = http_engine.presets.parse_row_request

    def opts(tab, row_id, container=None, title=None):
        options = {"tab": tab, "row_id": row_id, "container": container, "edited_title": title}
        return http_engine.row_options(parse(options, original_only=True))

    assert opts("audio", "a:mp3:256")["extract"] == "mp3:256"
    assert opts("audio", "a:wav", title="X") == {
        "mode": "download", "preset": "original_file", "output_name": "X", "extract": "wav"
    }
    frame = opts("image", "i:frame", "png")
    assert (frame["extract"], frame["frame_format"]) == ("frame", "png")
    assert "image_format" not in frame
    assert "extract" not in opts("video", "v:orig", "mkv")
    assert "extract" not in opts("audio", "a:orig")


def test_the_relay_honours_a_range_the_site_ignores(server):
    Handler.routes["/d/clip.mp4"] = serve_file(ranges=False)  # always 200, whole file
    with http_engine.Relay(server + "/d/clip.mp4") as relay:
        status, body = _relay_get(relay.local_url, {"Range": "bytes=100-"})
        assert (status, body) == (206, BODY[100:])
        status, body = _relay_get(relay.local_url, {"Range": "bytes=10-19"})
        assert (status, body) == (206, BODY[10:20])
        assert _relay_get(relay.local_url, {"Range": f"bytes={len(BODY)}-"})[0] == 416
        assert _relay_get(relay.local_url) == (200, BODY)
