"""Build the local installer payload (plan §8, §8.1, §8.3 F6). Stdlib only, besides PyInstaller.

One command, from the project root, builds the payload and then the per-user installer
``dist\\StuffDownloader-Setup-<version>.exe`` from packaging\\installer.iss (needs Inno Setup 6)::

    .venv\\Scripts\\python.exe packaging\\build_installer.py installer

``payload [--out dist\\payload]`` builds only the payload.

Layout of the payload (the folder is rebuilt from scratch every time)::

    StuffDownloader\\                   GUI onedir; the spec bundles only pin-verified tools
    runtime-setup\\python\\             CPython 3.11 (python-build-standalone, SHA-256 pinned)
    runtime-setup\\src\\stuff_downloader_worker\\
    runtime-setup\\packaging\\          build_runtime.py, this file, engine-requirements\\*.txt
    runtime-setup\\wheels\\             every pinned engine wheel, downloaded with --require-hashes
    licences\\                          LICENSE, THIRD_PARTY_LICENSES.txt, tool and package licences
    PAYLOAD.txt                        version, inputs and their hashes

Engine environments are not built here. A venv hard-codes its own path, and the app looks for
them under ``%LOCALAPPDATA%\\StuffDownloader\\runtime`` (build_runtime.default_root), so they are
created on the target machine instead, offline, from the staged wheels::

    runtime-setup\\python\\python.exe runtime-setup\\packaging\\build_installer.py setup-runtime

That runs build_runtime's own base + install for each engine, so every env still has its hashes
checked by pip, its imports checked and the worker self-test passed before it becomes active.

Personal use only (THIRD_PARTY_LICENSES.txt F2): the payload contains the spotDL environment's
wheels, so it and any installer built from it must not be published or shared.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from importlib import metadata
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
REQS = HERE / "engine-requirements"
DEFAULT_OUT = PROJECT / "dist" / "payload"
CACHE = PROJECT / "build" / "payload-cache"
SPEC = HERE / "StuffDownloader.spec"
ISS = HERE / "installer.iss"

# (engine, requirements file) in install order: the previous yt-dlp first, so that installing the
# current one leaves it active with the previous one kept for rollback.
ENGINE_INSTALLS: tuple[tuple[str, str], ...] = (
    ("ytdlp", "ytdlp-previous.txt"),
    ("ytdlp", "ytdlp.txt"),
    ("gallerydl", "gallerydl.txt"),
    ("spotdl", "spotdl.txt"),
)

# Engine packages that must never be frozen into the GUI (they run in the runtime's envs).
ENGINE_PACKAGES = ("stuff_downloader_worker", "yt_dlp", "gallery_dl", "spotdl", "curl_cffi")

LICENCE_NAME = re.compile(r"^(LICEN[CS]E|COPYING|NOTICE|AUTHORS)([.\-_].*)?$", re.IGNORECASE)
PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==([^\s;\\]+)")
HASH = re.compile(r"--hash=sha256:([0-9a-f]{64})")


class PayloadError(Exception):
    """An input is missing or failed verification; the payload was not produced."""


def _load(name: str):
    """Import a sibling packaging script by path (they are scripts, not a package)."""
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def pins(requirements: Path) -> dict[str, str]:
    """``{normalised name: version}`` for every exact pin in a requirements/lock file."""
    found = {}
    for line in requirements.read_text(encoding="utf-8").splitlines():
        match = PIN.match(line.strip())
        if match:
            found[_normalise(match.group(1))] = match.group(2)
    return found


def pinned_hashes(requirements: list[Path]) -> set[str]:
    return {h for path in requirements for h in HASH.findall(path.read_text(encoding="utf-8"))}


def _safe_child(root: Path, *parts: str) -> Path:
    target = root.joinpath(*parts).resolve()
    if not target.is_relative_to(root.resolve()):
        raise PayloadError(f"path escapes {root}: {'/'.join(parts)}")
    return target


# --- inputs -------------------------------------------------------------------------------------


def check_inputs(tools_dir: Path) -> None:
    """Everything the payload is built from must be present and verified before anything runs."""
    for name in ("LICENSE", "THIRD_PARTY_LICENSES.txt", "requirements.lock"):
        if not (PROJECT / name).is_file():
            raise PayloadError(f"{name} is missing")
    if not SPEC.is_file():
        raise PayloadError(f"{SPEC} is missing")
    fetch_tools = _load("fetch_tools")
    try:
        fetch_tools.verify_staged(tools_dir)
    except fetch_tools.ToolError as exc:
        raise PayloadError(str(exc)) from None
    build_runtime = _load("build_runtime")
    for _engine, name in ENGINE_INSTALLS:
        path = REQS / name
        if not path.is_file():
            raise PayloadError(f"{path} is missing")
        try:
            build_runtime._check_hashes(path)
        except build_runtime.RuntimeBuildError as exc:
            raise PayloadError(str(exc)) from None


# --- GUI ----------------------------------------------------------------------------------------


def check_onedir(app_dir: Path, tools_dir_in_app: Path) -> None:
    """The frozen GUI must hold the verified tools and none of the engine packages."""
    if not (app_dir / "StuffDownloader.exe").is_file():
        raise PayloadError(f"PyInstaller produced no {app_dir / 'StuffDownloader.exe'}")
    fetch_tools = _load("fetch_tools")
    try:
        fetch_tools.verify_staged(tools_dir_in_app)
    except fetch_tools.ToolError as exc:
        raise PayloadError(f"the frozen GUI's tools: {exc}") from None
    leaked = sorted(
        p.relative_to(app_dir).as_posix()
        for p in app_dir.rglob("*")
        if p.name in ENGINE_PACKAGES and p.is_dir()
    )
    if leaked:
        raise PayloadError(f"engine packages were frozen into the GUI: {', '.join(leaked)}")


def build_gui(dest: Path) -> Path:
    work = PROJECT / "build" / "payload-pyinstaller"
    subprocess.run(
        [
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
            "--distpath", str(dest), "--workpath", str(work), str(SPEC),
        ],
        check=True,
        cwd=PROJECT,
    )  # fmt: skip
    app_dir = dest / "StuffDownloader"
    check_onedir(app_dir, _find_tools_dir(app_dir))
    check_gui_import(app_dir)
    return app_dir


def check_gui_import(app_dir: Path) -> None:
    """Catch DLL conflicts in the frozen GUI before making an installer."""
    exe = app_dir / "StuffDownloader.exe"
    try:
        result = subprocess.run(
            [str(exe), "--check-gui-import"],
            capture_output=True,
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PayloadError("frozen GUI import check timed out") from exc
    if result.returncode:
        raise PayloadError(f"frozen GUI import check failed (exit {result.returncode})")


def _find_tools_dir(app_dir: Path) -> Path:
    # PyInstaller 6 puts datas under _internal; older layouts put them beside the exe.
    for candidate in (app_dir / "_internal" / "tools", app_dir / "tools"):
        if candidate.is_dir():
            return candidate
    raise PayloadError(f"no tools folder inside {app_dir}")


# --- runtime setup ------------------------------------------------------------------------------


def stage_python(setup: Path) -> None:
    """The pinned CPython, downloaded once into the cache and checked on every use."""
    build_runtime = _load("build_runtime")
    CACHE.mkdir(parents=True, exist_ok=True)
    archive = CACHE / "cpython.tar.gz"
    if not archive.is_file() or sha256_file(archive) != build_runtime.PYTHON_SHA256:
        archive.unlink(missing_ok=True)
        build_runtime.download_verified(
            build_runtime.PYTHON_URL, build_runtime.PYTHON_SHA256, archive
        )
    with tempfile.TemporaryDirectory(dir=setup) as tmp:
        build_runtime._safe_extract(archive, Path(tmp))
        shutil.move(str(Path(tmp) / "python"), str(setup / "python"))
    if not (setup / "python" / "python.exe").is_file():
        raise PayloadError("the CPython archive held no python\\python.exe")


def download_wheels(wheels: Path) -> list[Path]:
    """Every pinned engine wheel for CPython 3.11 on win_amd64, hash-checked by pip."""
    cache = CACHE / "wheels"
    cache.mkdir(parents=True, exist_ok=True)
    for _engine, name in ENGINE_INSTALLS:
        subprocess.run(
            [
                sys.executable, "-m", "pip", "download", "--require-hashes", "--no-deps",
                "--only-binary", ":all:", "--platform", "win_amd64", "--python-version", "3.11",
                "--implementation", "cp", "--no-input", "--disable-pip-version-check",
                "-d", str(cache), "-r", str(REQS / name),
            ],
            check=True,
        )  # fmt: skip
    return copy_pinned_wheels(cache, wheels, [REQS / name for _e, name in ENGINE_INSTALLS])


def copy_pinned_wheels(cache: Path, wheels: Path, requirements: list[Path]) -> list[Path]:
    """Copy only wheels whose SHA-256 one of the requirements files pins; then check all pins."""
    allowed = pinned_hashes(requirements)
    wheels.mkdir(parents=True, exist_ok=True)
    copied = []
    for wheel in sorted(cache.glob("*.whl")):
        if sha256_file(wheel) in allowed:
            copied.append(Path(shutil.copy2(wheel, wheels / wheel.name)))
    have = {(_normalise(w.name.split("-")[0]), w.name.split("-")[1]) for w in copied}
    needed = {pin for path in requirements for pin in pins(path).items()}
    if missing := sorted(needed - have):
        raise PayloadError(f"no pinned wheel for: {', '.join(f'{n}=={v}' for n, v in missing)}")
    return copied


def stage_runtime_setup(setup: Path) -> None:
    (setup / "packaging" / "engine-requirements").mkdir(parents=True)
    for name in ("build_runtime.py", "build_installer.py"):
        shutil.copy2(HERE / name, setup / "packaging" / name)
    for _engine, name in ENGINE_INSTALLS:
        shutil.copy2(REQS / name, setup / "packaging" / "engine-requirements" / name)
    shutil.copytree(
        PROJECT / "src" / "stuff_downloader_worker",
        setup / "src" / "stuff_downloader_worker",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )


def setup_runtime(root: Path | None = None) -> dict[str, str]:
    """On the target machine: build every engine env offline from the staged wheels.

    Runs from ``runtime-setup\\packaging``; build_runtime (loaded from beside this file) then
    treats ``runtime-setup`` as its project, so it copies the worker from there too.
    """
    build_runtime = _load("build_runtime")
    root = (root or build_runtime.default_root()).resolve()
    setup = PROJECT
    wheels = setup / "wheels"
    if not wheels.is_dir() or not (setup / "python" / "python.exe").is_file():
        raise PayloadError(f"{setup} is not a staged runtime-setup folder")
    if not build_runtime.base_python(root).is_file():
        root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(setup / "python", root / "python")
    build_runtime.build_base(root)
    # pip reads these: no index, only the staged wheels. --require-hashes still applies.
    # A file: URL, because pip splits PIP_FIND_LINKS on whitespace and install paths have spaces.
    offline = {"PIP_NO_INDEX": "1", "PIP_FIND_LINKS": wheels.resolve().as_uri()}
    saved = {key: os.environ.get(key) for key in offline}
    os.environ.update(offline)
    try:
        return {
            f"{engine}:{name}": build_runtime.install_engine(root, engine, REQS / name)
            for engine, name in ENGINE_INSTALLS
        }
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# --- licences -----------------------------------------------------------------------------------


def _is_licence_member(parts: tuple[str, ...]) -> bool:
    """A wheel member inside ``*.dist-info`` that is a licence text."""
    if len(parts) < 2 or not parts[0].endswith(".dist-info"):
        return False
    return parts[1] in ("licenses", "licences") or LICENCE_NAME.match(parts[-1]) is not None


def wheel_licences(wheel: Path, dest_root: Path) -> list[str]:
    """Copy the licence files a wheel ships in its dist-info into ``dest_root/<name>-<version>``."""
    name, version = wheel.name.split("-")[:2]
    dest = dest_root / f"{name}-{version}"
    written = []
    with zipfile.ZipFile(wheel) as zf:
        for member in zf.namelist():
            parts = PurePosixPath(member).parts
            if member.endswith("/") or not _is_licence_member(parts) or ".." in parts:
                continue
            target = _safe_child(dest, *parts[1:])
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
            written.append(target.relative_to(dest_root).as_posix())
    return written


def installed_licences(name: str, version: str, dest_root: Path) -> list[str]:
    """Licence files of a GUI dependency, from its installed dist-info in this environment."""
    try:
        dist = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        raise PayloadError(f"{name}=={version} is pinned but not installed here") from None
    if dist.version != version:
        raise PayloadError(f"{name} is {dist.version} here, but requirements.lock pins {version}")
    dest = dest_root / f"{_normalise(name)}-{version}"
    written = []
    for file in dist.files or []:
        parts = PurePosixPath(str(file)).parts
        if not _is_licence_member(parts) or ".." in parts:
            continue
        target = _safe_child(dest, *parts[1:])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file.locate(), target)
        written.append(target.relative_to(dest_root).as_posix())
    return written


def stage_licences(licences: Path, wheels: list[Path], tools_dir: Path) -> None:
    licences.mkdir(parents=True)
    for name in ("LICENSE", "THIRD_PARTY_LICENSES.txt"):
        shutil.copy2(PROJECT / name, licences / name)
    shutil.copytree(tools_dir / "licenses", licences / "tools")
    index = ["Licence files shipped with this build. See THIRD_PARTY_LICENSES.txt.", ""]
    index.append("[GUI: requirements.lock]")
    for name, version in sorted(pins(PROJECT / "requirements.lock").items()):
        index.append(_index_line(f"{name}=={version}", installed_licences(name, version, licences)))
    index += ["", "[engines: runtime-setup/wheels]"]
    for wheel in sorted(wheels):
        name, version = wheel.name.split("-")[:2]
        index.append(_index_line(f"{name}=={version}", wheel_licences(wheel, licences)))
    index += ["", "[tools]"] + [f"  tools/{p.name}" for p in sorted((licences / "tools").iterdir())]
    (licences / "INDEX.txt").write_text("\n".join(index) + "\n", encoding="utf-8")


def _index_line(label: str, files: list[str]) -> str:
    return f"{label:40} " + (
        ", ".join(files) if files else "(the distribution ships no licence file)"
    )


# --- the command --------------------------------------------------------------------------------


def build_payload(out: Path, tools_dir: Path | None = None) -> Path:
    """Build the payload in ``<out>.building`` and swap it in only once everything passed."""
    tools_dir = tools_dir or PROJECT / "tools"
    check_inputs(tools_dir)
    work = out.with_name(out.name + ".building")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        build_gui(work)
        setup = work / "runtime-setup"
        setup.mkdir()
        stage_python(setup)
        wheels = download_wheels(setup / "wheels")
        stage_runtime_setup(setup)
        stage_licences(work / "licences", wheels, tools_dir)
        (work / "PAYLOAD.txt").write_text(payload_manifest(work), encoding="utf-8")
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    # Swap by renames, so a failure never leaves half a payload, and the old one comes back if
    # the new one cannot be moved in. Renames are retried: right after a build, antivirus
    # scanning the fresh .exe files holds them open for a moment.
    old = out.with_name(out.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    try:
        if out.exists():
            _rename(out, old)
        try:
            _rename(work, out)
        except OSError:
            if old.exists():
                _rename(old, out)
            raise
    except OSError as exc:
        shutil.rmtree(work, ignore_errors=True)
        raise PayloadError(f"cannot replace {out}; is something in it open? ({exc})") from None
    shutil.rmtree(old, ignore_errors=True)
    return out


def _rename(src: Path, dst: Path, attempts: int = 10) -> None:
    for attempt in range(attempts):
        try:
            src.rename(dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.2 * (attempt + 1))


def payload_manifest(root: Path) -> str:
    lines = [
        f"Stuff Downloader {_release_version()} installer payload. PERSONAL USE ONLY:",
        "contains the spotDL environment (THIRD_PARTY_LICENSES.txt F2); do not publish or share.",
        "",
        "Inputs:",
    ]
    lines += [f"  {p.relative_to(PROJECT).as_posix()}  {sha256_file(p)}" for p in _inputs()]
    lines += ["", "Wheels:"]
    lines += [
        f"  {w.name}  {sha256_file(w)}"
        for w in sorted((root / "runtime-setup" / "wheels").iterdir())
    ]
    return "\n".join(lines) + "\n"


# --- installer --------------------------------------------------------------------------------


def find_iscc() -> Path:
    """Inno Setup 6's compiler: %ISCC%, then PATH, then the per-machine and per-user installs."""
    candidates = [os.environ.get("ISCC"), shutil.which("ISCC")]
    for base in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        root = os.environ.get(base)
        if root:
            sub = ("Programs", "Inno Setup 6") if base == "LOCALAPPDATA" else ("Inno Setup 6",)
            candidates.append(str(Path(root, *sub, "ISCC.exe")))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise PayloadError("Inno Setup 6 (ISCC.exe) not found; install it or set ISCC to its path")


def _release_version() -> str:
    sys.path.insert(0, str(PROJECT / "src"))
    try:
        from stuff_downloader import release_version
    finally:
        sys.path.pop(0)
    return release_version()


def iscc_command(iscc: Path, payload: Path, out_dir: Path, version: str) -> list[str]:
    return [
        str(iscc),
        "/Q",
        f"/DAppVersion={version}",
        f"/DPayloadDir={payload}",
        f"/DOutputDir={out_dir}",
        str(ISS),
    ]


def build_setup(payload: Path, out_dir: Path, rebuild_payload: bool = True) -> Path:
    """Build the payload (unless told not to), then compile StuffDownloader-Setup-<ver>.exe."""
    iscc = find_iscc()  # before the long payload build, so a missing compiler fails fast
    if rebuild_payload:
        build_payload(payload)
    for part in ("StuffDownloader/StuffDownloader.exe", "runtime-setup/python/python.exe",
                 "licences/LICENSE", "PAYLOAD.txt"):  # fmt: skip
        if not (payload / part).is_file():
            raise PayloadError(f"{payload} is not a complete payload: {part} is missing")
    version = _release_version()
    out_dir.mkdir(parents=True, exist_ok=True)
    setup = out_dir / f"StuffDownloader-Setup-{version}.exe"
    setup.unlink(missing_ok=True)
    subprocess.run(iscc_command(iscc, payload, out_dir, version), check=True)
    if not setup.is_file():
        raise PayloadError(f"ISCC finished but {setup} was not produced")
    return setup


def _inputs() -> list[Path]:
    names = ["LICENSE", "THIRD_PARTY_LICENSES.txt", "requirements.lock"]
    return [PROJECT / n for n in names] + [REQS / name for _e, name in ENGINE_INSTALLS]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build_installer")
    sub = parser.add_subparsers(dest="command", required=True)
    pay = sub.add_parser("payload", help="build the installer payload (on the build machine)")
    pay.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ins = sub.add_parser("installer", help="build the payload, then the Inno Setup installer")
    ins.add_argument("--payload", type=Path, default=DEFAULT_OUT)
    ins.add_argument("--out", type=Path, default=PROJECT / "dist")
    ins.add_argument("--skip-payload", action="store_true", help="reuse an existing payload")
    run = sub.add_parser("setup-runtime", help="build the engine envs (on the target machine)")
    run.add_argument("--root", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        if args.command == "payload":
            print(build_payload(args.out.resolve()))
        elif args.command == "installer":
            print(build_setup(args.payload.resolve(), args.out.resolve(), not args.skip_payload))
        else:
            for label, env_id in setup_runtime(args.root).items():
                print(f"ok  {label} -> {env_id}")
    except subprocess.CalledProcessError as exc:
        print(f"error: {exc.cmd[2] if len(exc.cmd) > 2 else exc.cmd} failed ({exc.returncode})",
              file=sys.stderr)  # fmt: skip
        return 1
    except PayloadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # RuntimeBuildError from build_runtime, OSError
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
