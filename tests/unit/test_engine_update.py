import json
from pathlib import Path

import pytest

from stuff_downloader.core import engine_update as eu
from stuff_downloader.core import updates

SHA = "a" * 64
ROOT_PROJECT = Path(__file__).resolve().parents[2]


def wheel(name, version, url=None, digest=SHA):
    return {
        "metadata": {"name": name, "version": version},
        "download_info": {
            "url": url
            or f"https://files.pythonhosted.org/packages/x/{name}-{version}-py3-none-any.whl",
            "archive_info": {"hashes": {"sha256": digest}},
        },
    }


def make_env(root: Path, engine: str, env_id: str, dists: dict[str, str]) -> None:
    site = root / "envs" / engine / env_id / "Lib" / "site-packages"
    site.mkdir(parents=True)
    (root / "envs" / engine / env_id / "Scripts").mkdir()
    (root / "envs" / engine / env_id / "Scripts" / "python.exe").write_text("")
    for name, version in dists.items():
        info = site / f"{name.replace('-', '_')}-{version}.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")


def pointer(root: Path) -> dict:
    return json.loads((root / "active.json").read_text())


def set_pointer(root: Path, data: dict) -> None:
    (root / "active.json").write_text(json.dumps(data))


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "runtime"
    (root / "python").mkdir(parents=True)
    (root / "python" / "python.exe").write_text("")
    make_env(root, "music", "1.12.2-old", {"yt-dlp": "2026.8.19", "ytmusicapi": "1.12.2",
                                           "mutagen": "1.48.1", "requests": "2.34.2"})  # fmt: skip
    set_pointer(root, {"music": {"active": "1.12.2-old", "previous": None}})
    reqs = tmp_path / "reqs"
    reqs.mkdir()
    (reqs / "music.in").write_text(
        "# comment\nyt-dlp[default,curl-cffi]==2026.8.19\nytmusicapi==1.12.2\n"
        "mutagen==1.48.1\nrequests==2.34.2\n"
    )
    return root, reqs


class FakeRuntime:
    """Plays pip (dry-run report) and build_runtime.py (install / rollback)."""

    def __init__(self, root, report=None, fail=None):
        self.root, self.fail, self.calls = root, fail, []
        self.report = report or {"install": [wheel("ytmusicapi", "1.12.3"), wheel("idna", "3.20")]}
        self.snapshot = None

    def __call__(self, args, timeout):
        self.calls.append(args)
        if "pip" in args:
            if self.fail == "resolve":
                raise eu.UpdateError("pip: no matching distribution")
            report = Path(args[args.index("--report") + 1])
            report.write_text(json.dumps(self.report))
            return ""
        command = args[args.index("--root") + 2]
        if command == "install":
            self.snapshot = Path(args[-1]).read_text()
            if self.fail == "install":
                raise eu.UpdateError("worker self-test did not pass")
            make_env(self.root, "music", "1.12.3-new", {"ytmusicapi": "1.12.3"})
            old = pointer(self.root)["music"]["active"]
            if self.fail != "no-switch":
                set_pointer(self.root, {"music": {"active": "1.12.3-new", "previous": old}})
            return "1.12.3-new\n"
        if command == "rollback":
            if self.fail == "rollback":
                raise eu.UpdateError("no previous music env to roll back to")
            entry = pointer(self.root)["music"]
            set_pointer(self.root, {"music": {"active": entry["previous"],
                                              "previous": entry["active"]}})  # fmt: skip
            return entry["previous"] + "\n"
        raise AssertionError(args)


# ── top-level pins ────────────────────────────────────────────────────────────────────────────
def test_top_level_pins_keep_extras_and_pin_the_rest_to_installed(world):
    root, reqs = world
    installed = {"yt-dlp": "2026.8.19", "ytmusicapi": "1.12.2", "mutagen": "1.48.1",
                 "requests": "2.34.2"}  # fmt: skip
    pins = eu.top_level_pins("music", {"YTMusicAPI": "1.12.3"}, installed, reqs)
    assert pins == [
        "yt-dlp[default,curl-cffi]==2026.8.19",
        "ytmusicapi==1.12.3",
        "mutagen==1.48.1",
        "requests==2.34.2",
    ]


@pytest.mark.parametrize(
    "selections, message",
    [
        ({"numpy": "2.0"}, "not a library of the music engine"),
        ({"ytmusicapi": "1.13.0rc1"}, "not a stable release"),
        ({"ytmusicapi": "1.12.3 --index-url http://evil"}, "not a stable release"),
    ],
)
def test_top_level_pins_refuse_bad_selections(world, selections, message):
    _, reqs = world
    installed = {"yt-dlp": "1", "ytmusicapi": "1", "mutagen": "1", "requests": "1"}
    with pytest.raises(eu.UpdateError, match=message):
        eu.top_level_pins("music", selections, installed, reqs)


def test_top_level_pins_need_every_library_installed(world):
    _, reqs = world
    with pytest.raises(eu.UpdateError, match="mutagen is not installed"):
        eu.top_level_pins("music", {}, {"yt-dlp": "1", "ytmusicapi": "1"}, reqs)
    with pytest.raises(eu.UpdateError, match="unknown engine"):
        eu.top_level_pins("fake", {}, {}, reqs)


# ── snapshot from pip's report ────────────────────────────────────────────────────────────────
def test_snapshot_is_exact_hash_pinned_and_accepted_by_build_runtime(tmp_path):
    lines = eu.snapshot_from_report(
        {"install": [wheel("YTMusicAPI", "1.12.3"), wheel("charset_normalizer", "3.5.1")]}
    )
    assert lines == [
        f"charset-normalizer==3.5.1 --hash=sha256:{SHA}",
        f"ytmusicapi==1.12.3 --hash=sha256:{SHA}",
    ]
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "br_check", ROOT_PROJECT / "packaging" / "build_runtime.py"
    )
    br = importlib.util.module_from_spec(spec)
    sys.modules["br_check"] = br
    spec.loader.exec_module(br)
    req = tmp_path / "music.txt"
    req.write_text("\n".join(lines) + "\n")
    br._check_hashes(req)  # the installer's own gate accepts what we write
    assert br._pinned_version(req, "ytmusicapi") == "1.12.3"


@pytest.mark.parametrize(
    "item",
    [
        wheel("x", "1.0", url="http://files.pythonhosted.org/x-1.0-py3-none-any.whl"),
        wheel("x", "1.0", url="https://evil.example/x-1.0-py3-none-any.whl"),
        wheel("x", "1.0", url="https://files.pythonhosted.org:8443/x-1.0-py3-none-any.whl"),
        wheel("x", "1.0", url="https://files.pythonhosted.org/x-1.0.tar.gz"),
        wheel("x", "1.0", digest="nothex"),
        wheel("x", "1.0rc1"),
        wheel("x y", "1.0"),
        {"metadata": {"name": "x", "version": "1.0"}, "download_info": {"url": "https://a"}},
        {"metadata": None},
        None,
    ],
)
def test_snapshot_refuses_anything_unsafe(item):
    with pytest.raises(eu.UpdateError):
        eu.snapshot_from_report({"install": [wheel("ok", "1.0"), item]})


def test_snapshot_refuses_empty_or_malformed_reports():
    for report in (None, [], {}, {"install": []}, {"install": "x"}):
        with pytest.raises(eu.UpdateError):
            eu.snapshot_from_report(report)


# ── the update ────────────────────────────────────────────────────────────────────────────────
def test_update_success_switches_prunes_and_arms_watchdog(world):
    root, reqs = world
    make_env(root, "music", "1.11.0-older", {"ytmusicapi": "1.11.0"})
    set_pointer(root, {"music": {"active": "1.12.2-old", "previous": "1.11.0-older"}})
    fake = FakeRuntime(root)
    stages = []
    env_id = eu.update_engine(
        "music", {"ytmusicapi": "1.12.3"}, root=root, req_dir=reqs, run=fake,
        progress=lambda stage, msg: stages.append(stage),
    )  # fmt: skip
    assert env_id == "1.12.3-new"
    assert pointer(root)["music"] == {"active": "1.12.3-new", "previous": "1.12.2-old"}
    assert sorted(p.name for p in (root / "envs" / "music").iterdir()) == [
        "1.12.2-old", "1.12.3-new"
    ]  # fmt: skip
    assert stages == ["resolve", "install", "prune", "done"]
    assert f"ytmusicapi==1.12.3 --hash=sha256:{SHA}" in fake.snapshot
    pip_args = fake.calls[0]
    assert (
        "--only-binary" in pip_args and "--dry-run" in pip_args and "ytmusicapi==1.12.3" in pip_args
    )
    assert all(isinstance(a, str) for call in fake.calls for a in call)  # argument lists only
    assert eu.Watchdog(root).watching("music") == "1.12.3-new"


@pytest.mark.parametrize("stage", ["resolve", "install", "no-switch"])
def test_failed_update_leaves_pointer_and_envs_untouched(world, stage):
    root, reqs = world
    before = (root / "active.json").read_bytes()
    make_env(root, "music", "1.11.0-older", {"ytmusicapi": "1.11.0"})
    with pytest.raises(eu.UpdateError):
        eu.update_engine("music", {"ytmusicapi": "1.12.3"}, root=root, req_dir=reqs,
                         run=FakeRuntime(root, fail=stage))  # fmt: skip
    if stage != "no-switch":
        assert (root / "active.json").read_bytes() == before
    assert (root / "envs" / "music" / "1.12.2-old").is_dir()
    assert (root / "envs" / "music" / "1.11.0-older").is_dir()  # no pruning after a failure
    assert eu.Watchdog(root).watching("music") is None


def test_update_refuses_unsafe_pip_report_before_installing(world):
    root, reqs = world
    fake = FakeRuntime(
        root, report={"install": [wheel("ytmusicapi", "1.12.3", url="http://x/y.whl")]}
    )
    with pytest.raises(eu.UpdateError, match="HTTPS"):
        eu.update_engine("music", {"ytmusicapi": "1.12.3"}, root=root, req_dir=reqs, run=fake)
    assert len(fake.calls) == 1  # build_runtime install never ran


def test_update_needs_an_installed_engine_and_a_selection(world, tmp_path):
    root, reqs = world
    with pytest.raises(eu.UpdateError, match="nothing selected"):
        eu.update_engine("music", {}, root=root, req_dir=reqs, run=FakeRuntime(root))
    with pytest.raises(eu.UpdateError, match="not installed"):
        eu.update_engine("gallerydl", {"gallery-dl": "2"}, root=root, req_dir=reqs,
                         run=FakeRuntime(root))  # fmt: skip
    (root / "python" / "python.exe").unlink()
    with pytest.raises(eu.UpdateError, match="runtime is not installed"):
        eu.update_engine("music", {"ytmusicapi": "1.12.3"}, root=root, req_dir=reqs,
                         run=FakeRuntime(root))  # fmt: skip


def test_only_one_update_at_a_time(world):
    root, reqs = world
    assert eu._LOCK.acquire(blocking=False)
    try:
        with pytest.raises(eu.UpdateError, match="already running"):
            eu.update_engine("music", {"ytmusicapi": "1.12.3"}, root=root, req_dir=reqs,
                             run=FakeRuntime(root))  # fmt: skip
        with pytest.raises(eu.UpdateError, match="update is running"):
            eu.undo("music", root=root, run=FakeRuntime(root))
    finally:
        eu._LOCK.release()


def test_run_command_bounds_errors_and_cleans_env(monkeypatch):
    seen = {}

    class Proc:
        returncode, stdout, stderr = 1, "", "x" * 5000

    def fake_run(args, **kwargs):
        seen.update(kwargs, args=args)
        return Proc()

    monkeypatch.setenv("PYTHONPATH", "C:/evil")
    monkeypatch.setattr(eu.subprocess, "run", fake_run)
    with pytest.raises(eu.UpdateError) as info:
        eu.run_command(["python.exe", "-V"], 5)
    assert len(str(info.value)) <= eu.ERROR_TAIL + 1
    assert "PYTHONPATH" not in seen["env"] and "shell" not in seen and seen["timeout"] == 5


def test_run_command_timeout_and_missing_program(monkeypatch):
    def timeout(args, **kwargs):
        raise eu.subprocess.TimeoutExpired(args, 5)

    monkeypatch.setattr(eu.subprocess, "run", timeout)
    with pytest.raises(eu.UpdateError, match="longer than 5 s"):
        eu.run_command(["python.exe"], 5)
    monkeypatch.setattr(eu.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    with pytest.raises(eu.UpdateError, match="could not start"):
        eu.run_command(["python.exe"], 5)


# ── prune ─────────────────────────────────────────────────────────────────────────────────────
def test_prune_keeps_active_previous_and_recovery_folders(world):
    root, _ = world
    for env_id in ("1.10-a", "1.11-b", "1.12.3-new"):
        make_env(root, "music", env_id, {})
    (root / "envs" / "music" / "1.10-a.bak").mkdir()
    set_pointer(root, {"music": {"active": "1.12.3-new", "previous": "1.12.2-old"}})
    assert sorted(eu.prune_envs(root, "music")) == ["1.10-a", "1.11-b"]
    assert sorted(p.name for p in (root / "envs" / "music").iterdir()) == [
        "1.10-a.bak", "1.12.2-old", "1.12.3-new"
    ]  # fmt: skip


def test_prune_does_nothing_without_a_readable_pointer(world):
    root, _ = world
    make_env(root, "music", "1.10-a", {})
    (root / "active.json").write_text("{broken")
    assert eu.prune_envs(root, "music") == []
    set_pointer(root, {"music": {"active": None}})
    assert eu.prune_envs(root, "music") == []
    assert (root / "envs" / "music" / "1.10-a").is_dir()


# ── undo ──────────────────────────────────────────────────────────────────────────────────────
def test_undo_switches_back_and_disarms(world):
    root, reqs = world
    fake = FakeRuntime(root)
    eu.update_engine("music", {"ytmusicapi": "1.12.3"}, root=root, req_dir=reqs, run=fake)
    assert eu.can_undo("music", root)
    assert eu.undo("music", root=root, run=fake) == "1.12.2-old"
    assert pointer(root)["music"] == {"active": "1.12.2-old", "previous": "1.12.3-new"}
    assert eu.Watchdog(root).watching("music") is None
    assert fake.calls[-1][-2:] == ["rollback", "music"]


def test_undo_failure_and_missing_previous(world):
    root, _ = world
    assert not eu.can_undo("music", root)
    with pytest.raises(eu.UpdateError, match="no previous"):
        eu.undo("music", root=root, run=FakeRuntime(root, fail="rollback"))
    set_pointer(root, {"music": {"active": "1.12.2-old", "previous": "..\\evil"}})
    assert not eu.can_undo("music", root)
    set_pointer(root, {"music": {"active": "1.12.2-old", "previous": "gone-1"}})
    assert not eu.can_undo("music", root)
    with pytest.raises(eu.UpdateError, match="unknown engine"):
        eu.undo("nope", root=root, run=FakeRuntime(root))


# ── watchdog ──────────────────────────────────────────────────────────────────────────────────
def _watch(tmp_path):
    rolled = []
    dog = eu.Watchdog(tmp_path, rollback=lambda engine: rolled.append(engine) or "old-1")
    return dog, rolled


def test_three_start_failures_on_new_env_roll_back(tmp_path):
    dog, rolled = _watch(tmp_path)
    dog.arm("ytdlp", "new-1")
    assert dog.record("ytdlp", "new-1", started=False) is None
    assert dog.record("ytdlp", "new-1", started=False) is None
    assert eu.Watchdog(tmp_path).watching("ytdlp") == "new-1"  # survives a restart
    assert dog.record("ytdlp", "new-1", started=False) == "old-1"
    assert rolled == ["ytdlp"] and dog.watching("ytdlp") is None
    assert dog.record("ytdlp", "new-1", started=False) is None  # never loops


def test_a_successful_start_ends_the_watch(tmp_path):
    dog, rolled = _watch(tmp_path)
    dog.arm("ytdlp", "new-1")
    dog.record("ytdlp", "new-1", started=False)
    dog.record("ytdlp", "new-1", started=False)
    assert dog.record("ytdlp", "new-1", started=True) is None
    assert dog.watching("ytdlp") is None
    dog.record("ytdlp", "new-1", started=False)
    assert rolled == []


def test_other_envs_and_engines_are_not_counted(tmp_path):
    dog, rolled = _watch(tmp_path)
    dog.arm("ytdlp", "new-1")
    for _ in range(5):
        dog.record("ytdlp", "old-1", started=False)  # a job still running on the old env
        dog.record("music", "new-1", started=False)
        dog.record("ytdlp", None, started=False)
    assert rolled == [] and dog.watching("ytdlp") == "new-1"


def test_failed_automatic_rollback_is_logged_not_raised(tmp_path):
    def boom(engine):
        raise eu.UpdateError("previous env failed its self-test")

    dog = eu.Watchdog(tmp_path, rollback=boom)
    dog.arm("ytdlp", "new-1")
    for _ in range(2):
        dog.record("ytdlp", "new-1", started=False)
    assert dog.record("ytdlp", "new-1", started=False) is None
    assert dog.watching("ytdlp") is None


def test_corrupt_watch_file_is_ignored(tmp_path):
    (tmp_path / eu.WATCH_FILE).write_text("{nope")
    dog, _ = _watch(tmp_path)
    assert dog.watching("ytdlp") is None
    (tmp_path / eu.WATCH_FILE).write_text(json.dumps({"ytdlp": {"env": 1, "failures": "x"}}))
    assert dog.watching("ytdlp") is None
    dog.arm("ytdlp", "new-1")
    assert dog.watching("ytdlp") == "new-1"


# ── packaged app ──────────────────────────────────────────────────────────────────────────────
def test_frozen_app_uses_bundled_build_runtime(tmp_path, monkeypatch):
    (tmp_path / "runtime-tools").mkdir()
    (tmp_path / "runtime-tools" / "build_runtime.py").write_text("")
    monkeypatch.setattr(eu.sys, "_MEIPASS", str(tmp_path), raising=False)
    assert eu.build_runtime_script() == tmp_path / "runtime-tools" / "build_runtime.py"


def test_spec_bundles_build_runtime_and_engines_match():
    spec = (ROOT_PROJECT / "packaging" / "StuffDownloader.spec").read_text(encoding="utf-8")
    assert '"build_runtime.py"), "runtime-tools")' in spec
    text = (ROOT_PROJECT / "packaging" / "build_runtime.py").read_text(encoding="utf-8")
    for engine in updates.ENGINES:
        assert f'"{engine}": {{' in text


def test_licence_note_records_publication_and_source_offer():
    text = (ROOT_PROJECT / "THIRD_PARTY_LICENSES.txt").read_text(encoding="utf-8")
    assert "can update the engine" in text and "PUBLICATION" in text
    assert "ffmpeg-9.0.1.tar.xz" in text
