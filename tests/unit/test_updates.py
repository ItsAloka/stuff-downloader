import json
import urllib.error
from pathlib import Path

import pytest
from packaging.version import Version

from stuff_downloader.core import updates
from stuff_downloader.core.settings import Settings

DAY = 24 * 60 * 60


def _release(*, yanked=False):
    return [{"filename": "x.whl", "yanked": yanked}]


def pypi(*versions, yanked=(), empty=()):
    releases = {v: _release(yanked=v in yanked) for v in versions}
    releases.update({v: [] for v in empty})
    return {"info": {}, "releases": releases}


def make_runtime(root: Path, envs: dict[str, dict[str, str]]) -> None:
    """A runtime with an active env per engine holding the given dist-info folders."""
    pointer = {}
    for engine, dists in envs.items():
        site = root / "envs" / engine / "1-abc" / "Lib" / "site-packages"
        site.mkdir(parents=True)
        for name, version in dists.items():
            info = site / f"{name.replace('-', '_')}-{version}.dist-info"
            info.mkdir()
            (info / "METADATA").write_text(
                f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8"
            )
        pointer[engine] = {"active": "1-abc", "previous": None}
    (root / "active.json").write_text(json.dumps(pointer), encoding="utf-8")


def make_reqs(folder: Path, files: dict[str, str]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for engine, text in files.items():
        (folder / f"{engine}.in").write_text(text, encoding="utf-8")
    return folder


# ── versions ──────────────────────────────────────────────────────────────────────────────────
def test_parse_final_accepts_only_final_releases():
    assert updates.parse_final("2026.8.19") == Version("2026.8.19")
    assert updates.parse_final("2026.08.19") == updates.parse_final("2026.8.19")
    assert updates.parse_final("1.0") == updates.parse_final("1.0.0")
    assert updates.parse_final("1.0.post2") == Version("1.0.post2")
    assert updates.parse_final("1!2.0") == Version("1!2.0")
    for text in (
        "1.0a1",
        "1.0b2",
        "1.0rc1",
        "1.0.dev3",
        "1.0.post1.dev1",
        "1.0+local",
        "",
        "abc",
        None,
    ):
        assert updates.parse_final(text) is None
    assert updates.parse_final("1.10") > updates.parse_final("1.9")
    assert updates.parse_final("1.0.post1") > updates.parse_final("1.0")


def test_release_versions_drop_yanked_prerelease_and_empty():
    data = pypi("1.8.1", "1.8.2", "1.8.3", "1.9.0rc1", yanked={"1.8.3"}, empty={"1.8.4"})
    assert sorted(updates.release_versions(data).values()) == ["1.8.1", "1.8.2"]
    # One yanked file among good ones does not yank the release.
    data["releases"]["1.8.3"].append({"filename": "y.tar.gz", "yanked": False})
    assert "1.8.3" in updates.release_versions(data).values()
    # Malformed file entries never make a release a candidate.
    for files in ([None], [None, {"yanked": False}], ["x"], [{}], [{"yanked": "no"}], [[]]):
        assert updates.release_versions({"releases": {"9.0": files}}) == {}
    for bad in (None, [], {"releases": []}, {"releases": {"1.0": "x"}}):
        assert updates.release_versions(bad) == {}


# ── suitability rules ─────────────────────────────────────────────────────────────────────────
def test_ytdlp_and_gallerydl_always_get_the_latest():
    rel = updates.release_versions(pypi("2026.8.19", "2026.9.20", "2027.1.1", "2027.2.1.dev1"))
    assert updates.choose("yt-dlp", "2026.8.19", rel) == ("2027.1.1", None)
    rel = updates.release_versions(pypi("1.32.13", "2.0.0"))
    assert updates.choose("gallery_dl", "1.32.13", rel) == ("2.0.0", None)


def test_others_update_within_major_and_show_newer_major():
    rel = updates.release_versions(pypi("1.8.1", "1.8.3", "1.9.0", "2.0.0", "2.1.0", "3.0.0b1"))
    assert updates.choose("ytmusicapi", "1.8.1", rel) == ("1.9.0", "2.1.0")
    assert updates.choose("ytmusicapi", "1.9.0", rel) == (None, "2.1.0")
    assert updates.choose("ytmusicapi", "2.1.0", rel) == (None, None)
    rel = updates.release_versions(pypi("1.8.1", "1.8.3"))
    assert updates.choose("ytmusicapi", "1.8.1", rel) == ("1.8.3", None)


def test_skipped_versions_are_not_offered_but_newer_ones_are():
    rel = updates.release_versions(pypi("1.8.1", "1.8.3", "2.0.0"))
    assert updates.choose("mutagen", "1.8.1", rel, ["1.8.3"]) == (None, "2.0.0")
    assert updates.choose("mutagen", "1.8.1", rel, ["1.8.3", "2.0.0"]) == (None, None)
    rel = updates.release_versions(pypi("1.8.1", "1.8.3", "1.8.4"))
    assert updates.choose("mutagen", "1.8.1", rel, ["1.8.3"]) == ("1.8.4", None)
    rel = updates.release_versions(pypi("2026.8.19", "2026.9.20"))
    assert updates.choose("yt-dlp", "2026.8.19", rel, ["2026.09.20"]) == (None, None)


def test_choose_with_no_data_or_unparseable_install():
    assert updates.choose("yt-dlp", "2026.8.19", {}) == (None, None)
    rel = updates.release_versions(pypi("2.0"))
    assert updates.choose("requests", "1.0.dev1", rel) == (None, None)
    assert updates.choose("requests", "3.0", rel) == (None, None)  # installed newer than PyPI


# ── tracked packages and installed versions ───────────────────────────────────────────────────
def test_parse_requirements_takes_top_level_names():
    text = (
        "# comment\n-c constraints.txt\n\n"
        "yt-dlp[default,curl-cffi]==2026.8.19\nYtMusicAPI==1.12.2  # inline\n"
        "mutagen>=1.48\nrequests==2.34.2; python_version>'3'\nmutagen==1\n"
    )
    assert updates.parse_requirements(text) == ["yt-dlp", "ytmusicapi", "mutagen", "requests"]


def test_tracked_packages_from_the_real_requirements():
    tracked = updates.tracked_packages()
    assert tracked["ytdlp"] == ["yt-dlp"]
    assert tracked["gallerydl"] == ["gallery-dl"]
    assert tracked["spotdl"] == ["spotdl"]
    assert tracked["music"] == ["yt-dlp", "ytmusicapi", "mutagen", "requests"]
    assert "ytdlp-previous" not in tracked


def test_tracked_packages_tolerates_missing_files(tmp_path):
    reqs = make_reqs(tmp_path / "reqs", {"ytdlp": "yt-dlp==1\n"})
    assert updates.tracked_packages(reqs) == {"ytdlp": ["yt-dlp"]}
    (reqs / "music.in").write_bytes(b"\xff\xfe\x00bad")
    assert updates.tracked_packages(reqs) == {"ytdlp": ["yt-dlp"]}


def test_installed_versions_read_env_metadata(tmp_path):
    make_runtime(tmp_path, {"music": {"yt-dlp": "2026.8.19", "ytmusicapi": "1.12.2"}})
    site = updates.env_site_packages("music", tmp_path)
    assert updates.installed_versions(site) == {"yt-dlp": "2026.8.19", "ytmusicapi": "1.12.2"}
    assert updates.env_site_packages("spotdl", tmp_path) is None
    (tmp_path / "active.json").write_text('{"music": {"active": ".."}}')
    assert updates.env_site_packages("music", tmp_path) is None


# ── the check ─────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def world(tmp_path):
    root = tmp_path / "runtime"
    make_runtime(
        root,
        {
            "ytdlp": {"yt-dlp": "2026.8.19"},
            "music": {"yt-dlp": "2026.8.19", "ytmusicapi": "1.8.1", "mutagen": "1.48.1"},
        },
    )
    reqs = make_reqs(
        tmp_path / "reqs",
        {"ytdlp": "yt-dlp[default]==2026.8.19\n", "music": "yt-dlp\nytmusicapi\nmutagen\n"},
    )
    answers = {
        "yt-dlp": pypi("2026.8.19", "2026.9.20"),
        "ytmusicapi": pypi("1.8.1", "1.8.3", "2.0.0"),
        "mutagen": pypi("1.48.1"),
    }
    calls = []

    def fetch(name):
        calls.append(name)
        return answers.get(name)

    return {"root": root, "reqs": reqs, "fetch": fetch, "calls": calls, "answers": answers}


def _check(world, settings, **kw):
    return updates.run_check(
        settings, fetch=world["fetch"], root=world["root"], req_dir=world["reqs"], **kw
    )


def test_check_offers_per_engine_and_asks_pypi_once(world):
    s = Settings()
    result = _check(world, s, now=1_000_000.0)
    assert sorted(world["calls"]) == ["mutagen", "yt-dlp", "ytmusicapi"]
    offers = {(o.engine, o.name): o for o in result.offers}
    assert set(offers) == {("ytdlp", "yt-dlp"), ("music", "yt-dlp"), ("music", "ytmusicapi")}
    assert (
        offers["ytdlp", "yt-dlp"].version == "2026.9.20" and offers["ytdlp", "yt-dlp"].recommended
    )
    ytm = offers["music", "ytmusicapi"]
    assert (ytm.installed, ytm.version, ytm.newer_major, ytm.recommended) == (
        "1.8.1",
        "1.8.3",
        "2.0.0",
        False,
    )
    assert ytm.selectable
    assert result.installed["music"]["mutagen"] == "1.48.1"
    assert s.update_last_check == 1_000_000.0


def test_newer_major_only_is_not_selectable(world):
    world["answers"]["ytmusicapi"] = pypi("1.8.1", "2.0.0")
    result = _check(world, Settings(), force=True)
    ytm = next(o for o in result.offers if o.name == "ytmusicapi")
    assert ytm.version is None and ytm.newer_major == "2.0.0" and not ytm.selectable


def test_skip_version_persists_and_hides_offer(world, tmp_path):
    from stuff_downloader.core import settings as settings_mod

    s = Settings()
    updates.skip_version(s, "YT_DLP", "2026.9.20")
    updates.skip_version(s, "yt-dlp", "2026.9.20")
    assert s.update_skips == {"yt-dlp": ["2026.9.20"]}
    settings_mod.save(s, tmp_path / "s.json")
    s = settings_mod.load(tmp_path / "s.json")
    result = _check(world, s, force=True)
    assert [o.name for o in result.offers] == ["ytmusicapi"]
    world["answers"]["yt-dlp"] = pypi("2026.8.19", "2026.9.20", "2026.9.25")
    result = _check(world, s, force=True)
    assert {o.version for o in result.offers if o.name == "yt-dlp"} == {"2026.9.25"}


def test_throttle_runs_at_most_once_a_day(world):
    s = Settings()
    assert _check(world, s, now=10 * DAY) is not None
    assert _check(world, s, now=10 * DAY + DAY - 1) is None
    assert _check(world, s, now=10 * DAY + DAY) is not None
    s.update_last_check = 20 * DAY  # clock moved backwards
    assert _check(world, s, now=5 * DAY) is not None


def test_force_ignores_throttle_and_the_startup_switch(world):
    s = Settings(update_check_on_start=False, update_last_check=100.0)
    assert not updates.is_due(s, now=100.0 + 5 * DAY)
    assert _check(world, s, now=200.0) is None
    result = _check(world, s, force=True, now=200.0)
    assert result.offers and s.update_last_check == 200.0


def test_offline_gives_no_offers_and_is_not_recorded(world):
    world["answers"].clear()
    s = Settings()
    result = _check(world, s, now=5 * DAY)
    assert result.offers == [] and not result.reached_pypi
    assert s.update_last_check == 0.0  # retried at the next start


def test_malformed_pypi_answers_give_no_offer(world):
    world["answers"]["yt-dlp"] = {"releases": "nope"}
    world["answers"]["ytmusicapi"] = {"releases": {"banana": _release(), "2.0.0rc1": _release()}}
    result = _check(world, Settings(), force=True)
    assert result.offers == []


def test_no_runtime_or_requirements_gives_nothing(tmp_path):
    result = updates.run_check(
        Settings(), force=True, fetch=lambda n: pytest.fail("no fetch expected"),
        root=tmp_path / "none", req_dir=tmp_path / "none",
    )  # fmt: skip
    assert result.offers == [] and result.installed == {}


# ── PyPI fetch boundary ───────────────────────────────────────────────────────────────────────
def test_pypi_url_is_https_pypi_only():
    assert updates.pypi_url("YT_DLP") == "https://pypi.org/pypi/yt-dlp/json"
    for bad in ("../x", "a/b", "evil.com/x?", "", "-x", "x@y"):
        with pytest.raises(ValueError):
            updates.pypi_url(bad)


class _Resp:
    def __init__(self, body, status=200):
        self.body, self.status = body, status

    def read(self, n=-1):
        return self.body[:n] if n >= 0 else self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Opener:
    def __init__(self, outcome):
        self.outcome, self.requests = outcome, []

    def open(self, request, timeout):
        self.requests.append((request.full_url, timeout))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


@pytest.fixture
def opener(monkeypatch):
    box = {}

    def build(*handlers):
        assert any(isinstance(h, updates._NoRedirect) for h in handlers)
        return box["opener"]

    monkeypatch.setattr(updates.urllib.request, "build_opener", build)
    return box


def test_fetch_pypi_success(opener):
    opener["opener"] = _Opener(_Resp(json.dumps(pypi("1.0")).encode()))
    assert updates.fetch_pypi("Requests") == pypi("1.0")
    assert opener["opener"].requests == [("https://pypi.org/pypi/requests/json", updates.TIMEOUT)]


@pytest.mark.parametrize(
    "outcome",
    [
        urllib.error.URLError("offline"),
        TimeoutError("slow"),
        OSError("reset"),
        urllib.error.HTTPError("u", 404, "nf", {}, None),
        urllib.error.HTTPError("u", 301, "moved", {}, None),
        RuntimeError("odd"),
        _Resp(b"{not json"),
        _Resp(b"\xff\xfe"),
        _Resp(b"[1, 2]"),
        _Resp(b"{}", status=204),
    ],
)
def test_fetch_pypi_failures_return_none(opener, outcome):
    opener["opener"] = _Opener(outcome)
    assert updates.fetch_pypi("yt-dlp") is None


def test_fetch_pypi_refuses_oversized_body(opener, monkeypatch):
    monkeypatch.setattr(updates, "MAX_RESPONSE_BYTES", 10)
    opener["opener"] = _Opener(_Resp(b'{"releases": {}}'))
    assert updates.fetch_pypi("yt-dlp") is None


def test_fetch_pypi_refuses_bad_names_without_network(opener):
    opener["opener"] = _Opener(AssertionError("must not be called"))
    assert updates.fetch_pypi("../../evil") is None


def test_no_redirect_handler_refuses_redirects():
    assert updates._NoRedirect().redirect_request(None, None, 302, "x", {}, "https://e.com") is None


# ── frozen app ────────────────────────────────────────────────────────────────────────────────
def test_frozen_app_reads_bundled_requirements(tmp_path, monkeypatch):
    make_reqs(tmp_path / "engine-requirements", {"ytdlp": "yt-dlp==1\n"})
    monkeypatch.setattr(updates.sys, "_MEIPASS", str(tmp_path), raising=False)
    assert updates.requirements_dir() == tmp_path / "engine-requirements"
    assert updates.tracked_packages() == {"ytdlp": ["yt-dlp"]}


def test_spec_bundles_the_in_files_where_the_app_looks():
    spec = (Path(__file__).resolve().parents[2] / "packaging" / "StuffDownloader.spec").read_text(
        encoding="utf-8"
    )
    assert '"engine-requirements").glob("*.in")' in spec
    assert '(str(path), "engine-requirements") for path in REQ_INS' in spec


def test_packaging_is_a_pinned_gui_dependency():
    import tomllib

    root = Path(__file__).resolve().parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert "packaging==26.3" in project["dependencies"]
    assert "packaging==26.3 \\" in (root / "requirements.lock").read_text(encoding="utf-8")
