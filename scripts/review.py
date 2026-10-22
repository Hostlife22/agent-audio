#!/usr/bin/env python3
"""Automated watch-and-listen review of a scored video (paid, about $0.025 per minute with the default model).

  review.py film_scored.mp4 --out review.json [--profile long] [--start 0 --duration 120] [--context "..."]

Gemini receives the picture and the finished mix and reports problems with timestamps: narration masked,
music too loud or vocal, effects out of sync with the picture, style mismatches, abrupt edits. It is a second
opinion that catches what loudness numbers cannot. Label its findings as automated and confirm each one against the
stems (qa.py numbers) before changing a cue. In testing, telling the model what a moment SHOULD do made it report that
the moment failed even when it had not, so --context describes the scene, never the expected result.
"""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import sys
import tempfile

from common import (AudioError, USER_ERRORS, fail, media_duration, post_json, require_ffmpeg, resolve_key, run_ffmpeg,
                    write_json)

MODEL = "google/gemini-3.1-pro-preview"  # Flash invented timestamps in testing
MAX_SECONDS = 600
STYLE = {
    "long": "Reference style (long animated history explainers): music changes about 3 times a minute with the story's "
            "mood and setting; it sits clearly under the narrator and often cuts to silence right before a punchline. "
            "About 4-7 literal cartoon effects a minute, synced to visible actions (impacts, whooshes, pops, UI clicks).",
    "short": "Reference style (vertical shorts): denser, louder music bed that still leaves the narrator clear; about "
             "8-20 effects a minute synced to fast visual gags; music stops before punchlines.",
}
RUBRIC = """You are a supervising sound editor reviewing the final mix of an animated history explainer. You get the
picture and the sound. {style}
Judge only what you can see and hear. Return JSON:
{{"scores": {{"narration_clarity": 1-10, "music_level": 1-10, "music_fit": 1-10, "sfx_sync": 1-10, "sfx_fit": 1-10,
             "overall": 1-10}},
 "issues": [{{"time": seconds, "severity": "high|medium|low",
             "category": "masking|music-too-loud|music-too-quiet|vocals-in-music|sfx-late|sfx-early|sfx-missing|"
                         "sfx-wrong|sfx-too-loud|abrupt-edit|style|silence|other",
             "description": "what is wrong", "fix": "concrete change"}}],
 "strengths": ["..."],
 "summary": "2-3 sentences"}}
Times are seconds from the start of this clip. Be specific and do not invent problems; an empty issues list is fine.
{context}"""


def compress(video, start, duration, folder):
    output = Path(folder) / "review.mp4"
    run_ffmpeg(["-y", "-ss", str(start), "-i", str(video), "-t", str(duration), "-vf", "scale=-2:360,fps=6",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-c:a", "aac", "-b:a", "128k", str(output)],
               "review clip")
    return output


def review(video, key, profile="long", start=0.0, duration=None, context="", model=MODEL):
    length = media_duration(video)
    duration = min(duration or length - start, length - start, MAX_SECONDS)
    if duration <= 0:
        raise AudioError("Nothing to review: start is past the end of the video.")
    with tempfile.TemporaryDirectory(prefix="agent-audio-review-") as folder:
        clip = compress(video, start, duration, folder)
        encoded = base64.b64encode(clip.read_bytes()).decode()
    text = RUBRIC.format(style=STYLE[profile], context=(
        f"Scene context (intent only; it says nothing about what the mix actually does): {context}" if context else ""))
    payload = {"model": model, "temperature": 0.2, "response_format": {"type": "json_object"},
               "messages": [{"role": "user", "content": [
                   {"type": "text", "text": text},
                   {"type": "video_url", "video_url": {"url": "data:video/mp4;base64," + encoded}}]}]}
    data = post_json("/chat/completions", payload, key, timeout=300, title="agent-audio review")
    content = str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    try:
        result = json.loads(content.strip().removeprefix("```json").removesuffix("```"))
    except json.JSONDecodeError:
        result = None
    if not isinstance(result, dict):
        return {"automated": True, "model": data.get("model", model), "usage": data.get("usage"),
                "error": "Review model returned non-JSON output.", "raw": content}
    issues, rejected = [], []
    for issue in result.get("issues") or []:
        time = issue.get("time") if isinstance(issue, dict) else None
        if not isinstance(time, (int, float)) or not 0 <= time <= duration + 0.5:
            rejected.append(issue)  # a time outside the clip means the model is guessing
            continue
        issues.append({**issue, "time": round(time + start, 2)})  # back to film time
    result = {**result, "issues": issues, "rejected_issues": rejected}
    return {"automated": True, "model": data.get("model", model), "clip": {"start": start, "duration": duration},
            "usage": data.get("usage"), **result}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("video", type=Path)
    parser.add_argument("--out", type=Path, help="default: <video>.review.json")
    parser.add_argument("--profile", choices=tuple(STYLE), default="long")
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--duration", type=float)
    parser.add_argument("--context", default="", help="What the scene is about and its tone. Never the expected result.")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args(argv)
    try:
        require_ffmpeg()
        out = (args.out or Path(str(args.video) + ".review.json")).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)  # before paying, so the answer always has a home
        key = resolve_key(args.env_file)
        result = review(args.video.expanduser().resolve(), key, args.profile, args.start, args.duration, args.context, args.model)
        write_json(out, result)
        if "error" in result:
            raise AudioError(f"{result['error']} The raw answer is saved in {out}.")
        print(json.dumps({key_: result.get(key_) for key_ in ("scores", "issues", "summary")}, indent=2, ensure_ascii=False))
        print(f"Saved {out}", file=sys.stderr)
        return 0
    except USER_ERRORS as error:
        return fail(error)


if __name__ == "__main__":
    raise SystemExit(main())
