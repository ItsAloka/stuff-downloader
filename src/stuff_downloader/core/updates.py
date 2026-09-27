"""Library update discovery for the engine envs (plan §2, R8). No Qt imports.

Finds newer suitable releases of each engine env's top-level libraries, like
``apt list --upgradable``. Installing them is a separate step; this module only reads.

- Tracked libraries: the requirement names in ``packaging/engine-requirements/<engine>.in``.
- Installed version: the env's own ``*.dist-info`` metadata, read without running the env.
- Latest: ``https://pypi.org/pypi/<name>/json``, HTTPS to pypi.org only, no redirects.
- Every network, HTTP or parse failure means "no offer" for that library, never an exception.

Versions are PEP 440, parsed by ``packaging.version``. Only stable releases are candidates:
not a pre-release, not a dev release, no local label, and at least one file not yanked.
"""

from __future__ import annotations

import json
import logging
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from urllib.parse import urlsplit

from packaging.version import InvalidVersion, Version

from . import runner
from .settings import Settings

log = logging.getLogger(__name__)

ENGINES = ("ytdlp", "gallerydl", "music", "spotdl")
# Always offered at their newest release: keeping them current is what keeps downloads working.
ALWAYS_LATEST = frozenset({"yt-dlp", "gallery-dl"})
CHECK_INTERVAL = 24 * 60 * 60
PYPI_HOST = "pypi.org"
TIMEOUT = 10
MAX_RESPONSE_BYTES = 32 * 1024 * 1024  # yt-dlp's JSON, with every release, is a few MB

_NAME = re.compile(r"^\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")
_CANONICAL = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

Fetcher = Callable[[str], "dict | None"]


def normalize(name: str) -> str:
    """PEP 503 name: lower case, runs of ``-_.`` become one ``-``."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_final(text: str) -> Version | None:
    """A stable release as a comparable Version, or None for anything else."""
    if not isinstance(text, str):
        return None
    try:
        version = Version(text)
    except InvalidVersion:
        return None
    if version.is_prerelease or version.is_devrelease or version.local is not None:
        return None
    return version


def major(version: Version) -> tuple[int, int]:
    """(epoch, first release number): an epoch change counts as a new major."""
    return version.epoch, version.major


# ── tracked libraries ─────────────────────────────────────────────────────────────────────────
def requirements_dir() -> Path | None:
    """Where the ``*.in`` files are: bundled beside the frozen app, or the source checkout."""
    candidates = []
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates.append(Path(bundle) / "engine-requirements")
    candidates.append(Path(__file__).resolve().parents[3] / "packaging" / "engine-requirements")
    return next((path for path in candidates if path.is_dir()), None)


def parse_requirements(text: str) -> list[str]:
    """Normalized top-level names in one ``.in`` file, in order, without duplicates."""
    names: list[str] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue  # blank, comment, or a pip option such as -c / -r
        match = _NAME.match(line)
        if match and normalize(match.group(1)) not in names:
            names.append(normalize(match.group(1)))
    return names


def tracked_packages(req_dir: Path | None = None) -> dict[str, list[str]]:
    """engine -> top-level library names. A missing or unreadable file tracks nothing."""
    req_dir = req_dir or requirements_dir()
    tracked: dict[str, list[str]] = {}
    if req_dir is None:
        log.info("No engine requirements folder; update check tracks nothing")
        return tracked
    for engine in ENGINES:
        try:
            text = (req_dir / f"{engine}.in").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        names = parse_requirements(text)
        if names:
            tracked[engine] = names
    return tracked


# ── installed versions ────────────────────────────────────────────────────────────────────────
def env_site_packages(engine: str, root: Path | None = None) -> Path | None:
    """The active env's site-packages for ``engine``, or None when it has no active env."""
    root = root or runner.runtime_root()
    env_id = runner._active_env(root, engine)
    if not env_id:
        return None
    site = root / "envs" / engine / env_id / "Lib" / "site-packages"
    return site if site.is_dir() else None


def installed_versions(site_packages: Path) -> dict[str, str]:
    """Normalized name -> version for every distribution in one site-packages folder."""
    found: dict[str, str] = {}
    try:
        for dist in metadata.distributions(path=[str(site_packages)]):
            try:
                name, version = dist.metadata["Name"], dist.version
            except Exception:  # noqa: BLE001 - a broken dist-info must not stop the check
                continue
            if isinstance(name, str) and isinstance(version, str):
                found.setdefault(normalize(name), version)
    except OSError as exc:
        log.info("Could not read %s: %s", site_packages, exc)
    return found


# ── PyPI ──────────────────────────────────────────────────────────────────────────────────────
def pypi_url(name: str) -> str:
    name = normalize(name)
    if not _CANONICAL.fullmatch(name):
        raise ValueError(f"not a valid project name: {name!r}")
    url = f"https://{PYPI_HOST}/pypi/{name}/json"
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != PYPI_HOST or parts.port is not None:
        raise ValueError(f"refusing update URL {url!r}")
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return None  # a redirect becomes an HTTPError; nothing leaves pypi.org


def fetch_pypi(name: str) -> dict | None:
    """PyPI's JSON for one project, or None on any failure."""
    try:
        url = pypi_url(name)
        opener = urllib.request.build_opener(
            _NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context())
        )
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with opener.open(request, timeout=TIMEOUT) as response:
            if response.status != 200:
                return None
            body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            return None
        data = json.loads(body.decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError) as exc:
        log.info("Update check for %s failed: %s", name, exc)
        return None
    except Exception as exc:  # noqa: BLE001 - offline or odd server replies never crash the app
        log.warning("Update check for %s failed unexpectedly: %s", name, exc)
        return None
    return data if isinstance(data, dict) else None


def release_versions(data: dict | None) -> dict[Version, str]:
    """Final, not-yanked releases that have files: key -> version text as PyPI spells it."""
    releases = data.get("releases") if isinstance(data, dict) else None
    if not isinstance(releases, dict):
        return {}
    found: dict[Version, str] = {}
    for text, files in releases.items():
        key = parse_final(text)
        if key is None or not isinstance(files, list) or not files:
            continue
        # Malformed file entries reject the whole release; it needs one file explicitly not yanked.
        if not all(isinstance(f, dict) for f in files):
            continue
        if not any(f.get("yanked") is False for f in files):
            continue
        found.setdefault(key, text)
    return found


# ── suitability ───────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Offer:
    """One library line in the Updates available popup.

    ``version`` is the offered update (None when only a newer major exists). ``newer_major``
    is a newer major release, shown greyed out as "needs an app update"."""

    engine: str
    name: str
    installed: str
    version: str | None
    recommended: bool = False
    newer_major: str | None = None

    @property
    def selectable(self) -> bool:
        return self.version is not None


def choose(
    name: str, installed: str, releases: dict[Version, str], skipped: Iterable[str] = ()
) -> tuple[str | None, str | None]:
    """(offered version, newer major needing an app update) under the R8 rules."""
    current = parse_final(installed)
    if current is None or not releases:
        return None, None
    skipped_keys = {parse_final(v) for v in skipped} - {None}
    newer = sorted(v for v in releases if v > current)
    if not newer:
        return None, None
    if normalize(name) in ALWAYS_LATEST:
        best = newer[-1]
        return (None if best in skipped_keys else releases[best]), None
    same = [v for v in newer if major(v) == major(current)]
    offered = None
    if same and same[-1] not in skipped_keys:
        offered = releases[same[-1]]
    later = [v for v in newer if major(v) > major(current)]
    blocked = releases[later[-1]] if later and later[-1] not in skipped_keys else None
    return offered, blocked


@dataclass
class CheckResult:
    offers: list[Offer]
    installed: dict[str, dict[str, str]]  # engine -> name -> installed version
    reached_pypi: bool  # at least one PyPI answer arrived; only then is the check recorded


def check(
    settings: Settings,
    *,
    fetch: Fetcher = fetch_pypi,
    root: Path | None = None,
    req_dir: Path | None = None,
) -> CheckResult:
    """Run the check now, ignoring the throttle. Never raises for network or data problems."""
    tracked = tracked_packages(req_dir)
    answers: dict[str, dict | None] = {}  # yt-dlp is in two envs; ask PyPI once
    offers: list[Offer] = []
    installed_by_engine: dict[str, dict[str, str]] = {}
    for engine, names in tracked.items():
        site = env_site_packages(engine, root)
        if site is None:
            continue
        installed = installed_versions(site)
        installed_by_engine[engine] = {n: installed[n] for n in names if n in installed}
        for name in names:
            version = installed.get(name)
            if version is None:
                continue
            if name not in answers:
                answers[name] = fetch(name)
            offered, blocked = choose(
                name, version, release_versions(answers[name]), settings.update_skips.get(name, ())
            )
            if offered or blocked:
                offers.append(Offer(engine, name, version, offered, name in ALWAYS_LATEST, blocked))
    reached = any(answer is not None for answer in answers.values())
    return CheckResult(offers, installed_by_engine, reached)


def is_due(settings: Settings, now: float | None = None) -> bool:
    """Whether the startup check should run: switched on and 24 h since the last success."""
    if not settings.update_check_on_start:
        return False
    now = time.time() if now is None else now
    last = settings.update_last_check
    # A clock set backwards would otherwise silence the check until it caught up.
    return last <= 0 or now < last or now - last >= CHECK_INTERVAL


def run_check(
    settings: Settings,
    *,
    force: bool = False,
    now: float | None = None,
    fetch: Fetcher = fetch_pypi,
    root: Path | None = None,
    req_dir: Path | None = None,
) -> CheckResult | None:
    """The startup check (None when not due) or, with ``force``, the Check for updates button.

    Records the check time on ``settings`` when PyPI answered; the caller saves settings."""
    now = time.time() if now is None else now
    if not force and not is_due(settings, now):
        return None
    result = check(settings, fetch=fetch, root=root, req_dir=req_dir)
    if result.reached_pypi:
        settings.update_last_check = now
    return result


def skip_version(settings: Settings, name: str, version: str) -> None:
    """Never offer this exact version of ``name`` again; the caller saves settings."""
    name = normalize(name)
    skipped = settings.update_skips.setdefault(name, [])
    if version not in skipped:
        skipped.append(version)
