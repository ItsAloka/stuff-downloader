"""One-off multilingual Spotify match/download smoke run; evidence stays beside this script."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests" / "network")]

import netjob  # noqa: E402
from stuff_downloader.core import spotify  # noqa: E402
from stuff_downloader.core.protocol import JobSpec  # noqa: E402
from stuff_downloader.core.runner import default_worker_command  # noqa: E402

SAMPLES = {
    "japanese": "1zVsw1SqQKgtzE4aqmE8nE",  # YOASOBI, Idol
    "chinese": "1ivCIgrYZyE0BvItL4Z8lk",  # Jay Chou, 告白氣球
    "english": "6Jkm5kUldvIqxzI1Pa7nXH",  # Leah Kate, 10 Things I Hate About You
    "sinhala": "73fXPtSK5jS0MaGUdGDUqJ",  # Yohani, Manike Mage Hithe
}
COMMAND = default_worker_command("spotdl")
OUT = ROOT / ".devteam-smoke"


def job(url, options, directory):
    spec = JobSpec(
        job_id="smoke-" + str(len(list(OUT.glob("*.json")))),
        engine="spotdl", url=url, output_dir=str(directory), options=options,
    )
    terminal, _ = netjob.run_job_once(spec, COMMAND, 180)
    return {"type": terminal.type, "data": terminal.data}


for language, track_id in SAMPLES.items():
    if len(sys.argv) > 1 and language not in sys.argv[1:]:
        continue
    path = OUT / f"{language}.json"
    url = f"https://open.spotify.com/track/{track_id}"
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        if previous.get("review") and previous.get("match", {}).get("type") == "result":
            video_id = previous["match"]["data"]["video_id"]
            directory = OUT / language
            directory.mkdir(exist_ok=True)
            previous["download"] = job(url, spotify.download_options(video_id), directory)
            path.write_text(json.dumps(previous, ensure_ascii=False, indent=2), encoding="utf-8")
        print(language, "resumed", flush=True)
        continue
    evidence = {"spotify_url": url}
    try:
        analyze = job(url, spotify.analyze_options(), OUT)
        evidence["analyze"] = analyze
        if analyze["type"] != "result":
            continue
        listing = spotify.parse_listing(analyze["data"])
        if not listing or not listing.tracks:
            evidence["parse_error"] = "empty listing"
            continue
        track = listing.tracks[0]
        match_result = job(url, spotify.match_options(), OUT)
        evidence["match"] = match_result
        if match_result["type"] != "result":
            continue
        match = spotify.parse_match(match_result["data"], track)
        if not match:
            evidence["parse_error"] = "invalid match"
            continue
        evidence["review"] = {
            "spotify_title": track.title, "spotify_artist": track.artist,
            "spotify_duration": track.duration, "youtube_url": match.url,
            "youtube_title": match.title, "youtube_channel": match.channel,
            "youtube_duration": match.duration, "duration_diff": match.duration_diff,
            "score": match.score, "uncertain": spotify.is_uncertain(match),
        }
        if spotify.is_uncertain(match):
            evidence["download_skipped"] = "uncertain match requires a human recording review"
            continue
        directory = OUT / language
        directory.mkdir(exist_ok=True)
        evidence["download"] = job(url, spotify.download_options(match.video_id), directory)
    except Exception as exc:
        evidence["exception"] = repr(exc)
    finally:
        path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print(language, json.dumps(evidence.get("review") or evidence.get("analyze") or evidence.get("exception"), ensure_ascii=True), flush=True)
