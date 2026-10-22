#!/usr/bin/env python3
"""Study reference videos: local loudness/music-bed measurements and an optional Gemini video+audio audit.

  analyze_refs.py levels --refs DIR --out levels.json            # free, local ffmpeg only
  analyze_refs.py study  --refs DIR --out study.json [--dry-run]  # paid: sends compressed clips to Gemini

`study` sends the picture and the sound together, so the model can tie effects to visible events.
Its labels and times are model output: verify a disputed moment against the clip before copying it.
"""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import tempfile

from common import (AudioError, USER_ERRORS, decode_mono, fail, fingerprint, frame_levels, media_duration, percentile,
                    post_json, read_json, require_ffmpeg, resolve_key, run_ffmpeg, write_json)

MODEL = "google/gemini-3.8-flash"
LONG_SEGMENTS = (0.04, 0.45, 0.8)  # fractions of a long video to excerpt
LONG_SEGMENT_SECONDS = 100
SHORT_MAX_SECONDS = 75
LEVEL_WINDOW_SECONDS = 240

DIRECTION = """You are a re-recording mixer studying the SOUND DESIGN of a finished animated history explainer.
You receive the picture and the soundtrack. Ignore historical facts. Report only what you can actually hear;
use the picture to say what each sound is synchronised with. Uncertainty and empty lists beat invented events.

Return one JSON object:
{
 "music": [{"start": s, "end": s, "mood": "...", "instruments": "...", "tempo_feel": "slow|medium|fast",
            "narrative_role": "what story beat it supports", "entry": "hard cut|fade|stinger|under speech",
            "exit": "hard stop|fade|stinger|cut to silence|continues", "level_vs_voice": "barely audible|low|medium|loud"}],
 "sfx": [{"time": s, "sound": "...", "trigger": "visual event or spoken word it lands on",
          "category": "whoosh|impact|pop|ui-click|ding|riser|stinger|foley|ambience|comedic|other",
          "confidence": "high|medium|low"}],
 "silences": [{"time": s, "duration": s, "purpose": "..."}],
 "techniques": ["short reusable sound-design observations, e.g. music stops before punchline"],
 "sfx_per_minute": number, "music_changes_per_minute": number,
 "summary": "3-4 sentences on how music and effects support the comedy and the story"
}
Times are seconds from the start of THIS clip. Write in English."""


def list_videos(refs):
    videos = sorted(path for path in Path(refs).expanduser().resolve().rglob("*") if path.suffix.lower() in (".mp4", ".mov", ".webm"))
    if not videos:
        raise AudioError(f"No videos found under {refs}")
    return videos


def is_long(path, seconds):
    return path.parent.name == "long" or seconds > 180


# ---------- levels (local) ----------

def loudness(path, seconds):
    process = run_ffmpeg(["-nostats", "-t", str(seconds), "-i", str(path), "-vn", "-af", "ebur128=peak=true", "-f", "null", "-"],
                         "ebur128", "info")
    summary = {}
    for line in process.stderr.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in ("I:", "LRA:", "Peak:"):
            summary[parts[0][:-1]] = float(parts[1])
    return {"integrated_lufs": summary.get("I"), "lra_lu": summary.get("LRA"), "true_peak_dbtp": summary.get("Peak")}


def bed_estimate(path, seconds):
    """Speech peaks (p90 of 50 ms RMS) versus the floor between words (p10): a proxy for music-bed depth."""
    levels = frame_levels(decode_mono(path, 16000, duration=seconds), 16000)
    speech, floor = percentile(levels, 0.9), percentile(levels, 0.1)
    return {"speech_p90_dbfs": round(speech, 1), "floor_p10_dbfs": round(floor, 1), "bed_depth_db": round(speech - floor, 1)}


def measure_levels(refs):
    rows = []
    for path in list_videos(refs):
        seconds = min(media_duration(path), LEVEL_WINDOW_SECONDS)
        rows.append({"file": str(path.relative_to(Path(refs).expanduser().resolve())),
                     "measured_seconds": round(seconds, 1), **loudness(path, seconds), **bed_estimate(path, seconds)})
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    return {"scope": f"First {LEVEL_WINDOW_SECONDS} s of each video; bed_depth is speech p90 minus floor p10 of 50 ms RMS, "
                     "a proxy (the floor includes ambience and effect tails).", "videos": rows}


# ---------- study (paid) ----------

def clip_plan(refs, limit_long=None, limit_short=None):
    root = Path(refs).expanduser().resolve()
    plan, long_count, short_count = [], 0, 0
    for path in list_videos(root):
        seconds = media_duration(path)
        if is_long(path, seconds):
            if limit_long is not None and long_count >= limit_long:
                continue
            long_count += 1
            for fraction in LONG_SEGMENTS:
                start = max(0.0, min(seconds * fraction, seconds - LONG_SEGMENT_SECONDS))
                plan.append({"file": str(path.relative_to(root)), "start": round(start, 2),
                             "duration": round(min(LONG_SEGMENT_SECONDS, seconds - start), 2), "form": "long"})
        else:
            if limit_short is not None and short_count >= limit_short:
                continue
            short_count += 1
            plan.append({"file": str(path.relative_to(root)), "start": 0.0,
                         "duration": round(min(seconds, SHORT_MAX_SECONDS), 2), "form": "short"})
    for number, clip in enumerate(plan, 1):
        clip["id"] = f"clip-{number:03d}"
    return plan


def compress_clip(item):
    clip, root, folder = item
    output = Path(folder) / f"{clip['id']}.mp4"
    run_ffmpeg(["-y", "-ss", str(clip["start"]), "-i", str(root / clip["file"]), "-t", str(clip["duration"]),
                "-vf", "scale=-2:360,fps=4", "-c:v", "libx264", "-preset", "veryfast", "-crf", "32",
                "-c:a", "aac", "-b:a", "96k", "-ac", "2", str(output)], "clip extraction")
    return output


def analyze_clip(path, key, model):
    payload = {"model": model, "temperature": 0.1, "response_format": {"type": "json_object"},
               "messages": [{"role": "user", "content": [
                   {"type": "text", "text": DIRECTION},
                   {"type": "video_url", "video_url": {"url": "data:video/mp4;base64," + base64.b64encode(path.read_bytes()).decode()}},
               ]}]}
    data = post_json("/chat/completions", payload, key, timeout=300, title="agent-audio reference study")
    content = str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    try:
        answer = json.loads(content.strip().removeprefix("```json").removesuffix("```"))
    except json.JSONDecodeError:
        answer = {"unparsed": content}  # paid text is kept; the clip is not re-sent
    return {"analysis": answer, "model": data.get("model"), "usage": data.get("usage")}


def run_study(args, key_loader):
    root = Path(args.refs).expanduser().resolve()
    plan = clip_plan(root, args.limit_long, args.limit_short)
    request = fingerprint({"plan": plan, "model": args.model, "direction": DIRECTION})
    if args.dry_run:
        total = sum(clip["duration"] for clip in plan)
        print(json.dumps({"model": args.model, "clips": plan, "total_seconds": round(total, 1)}, indent=2, ensure_ascii=False))
        return 0
    out = Path(args.out).expanduser().resolve()
    report = read_json(out) if out.exists() else None
    if report and report.get("request_sha256") != request:
        raise AudioError("Existing report used a different clip plan or model; choose a new --out.")
    report = report or {"scope": "Gemini video+audio audit of excerpts; model output, not ground truth.",
                        "reference_root": str(root), "model": args.model, "request_sha256": request,
                        "clips": plan, "results": {}}
    out.parent.mkdir(parents=True, exist_ok=True)
    write_json(out, report)
    todo = [clip for clip in plan if clip["id"] not in report["results"]]
    if not todo:
        print(f"Study already complete: {out}")
        return 0
    pending = [clip_id for clip_id in report.get("pending", []) if clip_id not in report["results"]]
    if pending and not args.retry_pending:
        raise AudioError(f"Clips {', '.join(pending)} were sent before but never answered (maybe billed). "
                         "Rerun with --retry-pending to send them again.")
    key = key_loader()
    report = {**report, "pending": [clip["id"] for clip in todo]}
    write_json(out, report)
    with tempfile.TemporaryDirectory(prefix="agent-audio-study-") as folder:
        with ThreadPoolExecutor(max_workers=4) as pool:
            paths = list(pool.map(compress_clip, [(clip, root, folder) for clip in todo]))

        def one(pair):
            clip, path = pair
            return clip["id"], analyze_clip(path, key, args.model)

        failures = []
        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            futures = [pool.submit(one, pair) for pair in zip(todo, paths)]
            for future in as_completed(futures):  # save each paid answer the moment it arrives
                try:
                    clip_id, result = future.result()
                except USER_ERRORS as error:
                    failures.append(str(error))
                    continue
                report = {**report, "results": {**report["results"], clip_id: result},
                          "pending": [item for item in report["pending"] if item != clip_id]}
                write_json(out, report)
                print(f"analyzed {clip_id}", flush=True)
        if failures:
            raise AudioError(f"{len(failures)} clip(s) failed; finished ones are saved. First error: {failures[0]}")
    print(f"Saved study: {out}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("levels", "study"))
    parser.add_argument("--refs", required=True, type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--limit-long", type=int, help="Study at most N long videos (3 excerpts each).")
    parser.add_argument("--limit-short", type=int, help="Study at most N shorts.")
    parser.add_argument("--parallel", type=int, default=3)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="study: list clips and seconds without a key or request.")
    parser.add_argument("--retry-pending", action="store_true", help="study: resend clips that were sent but never answered.")
    args = parser.parse_args(argv)
    try:
        require_ffmpeg()
        if args.command == "levels":
            result = measure_levels(args.refs)
            if args.out:
                write_json(args.out, result)
            return 0
        if not args.out and not args.dry_run:
            raise AudioError("--out is required for study.")
        return run_study(args, lambda: resolve_key(args.env_file))
    except USER_ERRORS as error:
        return fail(error)


if __name__ == "__main__":
    raise SystemExit(main())
