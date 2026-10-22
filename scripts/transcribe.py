#!/usr/bin/env python3
"""Word-timed transcript of a narration through OpenRouter speech-to-text, plus SRT and a script check.

  transcribe.py vo.mp3 --out vo.words.json                      # writes vo.words.json and vo.srt
  transcribe.py vo.mp3 --out vo.words.json --script plan.json   # also reports words missing/added vs the script

The SRT feeds toon-explainer: `script2spec.py film/script.md --srt vo.srt --merge film/spec.json`.
Long audio is split at pauses near every --chunk-seconds so each request stays under the provider timeout.
"""
from __future__ import annotations

import argparse
import base64
import difflib
import json
from pathlib import Path
import re
import sys
import tempfile

from common import (AudioError, USER_ERRORS, audio_info, decode_mono, digest_file, fail, fingerprint, frame_levels,
                    post_json, read_json, require_ffmpeg, resolve_key, run_ffmpeg, write_json)

MODEL = "elevenlabs/scribe-v2"
CHUNK_SECONDS = 240.0
SEARCH_SECONDS = 20.0   # look this far either side of a chunk boundary for a pause
SRT_MAX_CHARS = 60
SRT_MAX_SECONDS = 5.0
TAG = re.compile(r"\[[^\]]*\]|<[^>]+>")


# ---------- chunking ----------

def quietest_point(levels, window, centre, search):
    """Time (s) of the quietest 50 ms frame within +/- search of centre."""
    first = max(0, int((centre - search) / window))
    last = min(len(levels) - 1, int((centre + search) / window))
    if first > last:
        return centre
    best = min(range(first, last + 1), key=lambda index: levels[index])
    return (best + 0.5) * window


def chunk_bounds(path, duration, chunk_seconds):
    """Cut points near every chunk_seconds, moved to the quietest frame nearby; always strictly advancing."""
    if duration <= chunk_seconds * 1.15:
        return [(0.0, duration)]
    window = 0.05
    levels = frame_levels(decode_mono(path, 8000), 8000, window)
    search = min(SEARCH_SECONDS, chunk_seconds * 0.25)
    cuts = [0.0]
    while cuts[-1] + chunk_seconds < duration - chunk_seconds * 0.15:
        target = cuts[-1] + chunk_seconds
        cuts.append(max(quietest_point(levels, window, target, search), cuts[-1] + chunk_seconds * 0.5))
    cuts.append(duration)
    return list(zip(cuts[:-1], cuts[1:]))


# ---------- request ----------

def transcribe_chunk(path, start, end, key, model, language, folder):
    piece = Path(folder) / f"chunk-{start:09.3f}.mp3"
    run_ffmpeg(["-y", "-ss", f"{start:.3f}", "-i", str(path), "-t", f"{end - start:.3f}", "-vn",
                "-ac", "1", "-ar", "16000", "-b:a", "64k", str(piece)], "chunk extraction")
    payload = {"model": model, "input_audio": {"data": base64.b64encode(piece.read_bytes()).decode(), "format": "mp3"},
               "response_format": "verbose_json", "timestamp_granularities": ["word", "segment"]}
    if language:
        payload["language"] = language
    data = post_json("/audio/transcriptions", payload, key, timeout=180, title="agent-audio transcribe")
    words = data.get("words")
    if not isinstance(words, list) or not words:
        raise AudioError(f"{model} returned no word timestamps; choose a model that supports them.")
    words = [item for item in words if isinstance(item, dict) and {"word", "start", "end"} <= set(item)]
    shifted = [{"word": str(item["word"]).strip(), "start": round(float(item["start"]) + start, 3),
                "end": round(float(item["end"]) + start, 3)}
               for item in words if str(item.get("word", "")).strip()]
    return shifted, (data.get("usage") or {}).get("cost")


# ---------- speech inside music ----------

SPEECH_GAP = 3.0       # words closer than this belong to one span
SPEECH_MIN_WORDS = 3   # fewer words is usually a misheard instrument


def speech_spans(path, key, model=MODEL):
    """Spoken words hidden in a music track. Bracketed tags such as "[music]" are sound labels, not speech."""
    duration = audio_info(path)["duration_seconds"]
    words, cost = [], 0.0
    with tempfile.TemporaryDirectory(prefix="agent-audio-speech-") as folder:
        for start, end in chunk_bounds(path, duration, CHUNK_SECONDS):
            try:
                chunk, chunk_cost = transcribe_chunk(path, start, end, key, model, "en", folder)
            except AudioError as error:
                if "no word timestamps" not in str(error):
                    raise
                chunk, chunk_cost = [], 0
            words += [word for word in chunk if not word["word"].startswith(("[", "("))]
            cost += chunk_cost or 0
    spans, current = [], []
    for word in words:
        if current and word["start"] - current[-1]["end"] > SPEECH_GAP:
            spans.append(current)
            current = []
        current.append(word)
    if current:
        spans.append(current)
    found = [{"start": group[0]["start"], "end": group[-1]["end"], "words": " ".join(w["word"] for w in group)[:120]}
             for group in spans if len(group) >= SPEECH_MIN_WORDS]
    return {"model": model, "spans": found, "cost_usd": round(cost, 5)}


# ---------- outputs ----------

def srt_time(seconds):
    millis = int(round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def subtitle_cues(words):
    """Group words into readable cues: break at sentence ends, long pauses, length or duration limits."""
    cues, current = [], []
    for index, word in enumerate(words):
        current.append(word)
        text = " ".join(item["word"] for item in current)
        following = words[index + 1] if index + 1 < len(words) else None
        pause = following["start"] - word["end"] if following else 0
        if (following is None or word["word"].endswith((".", "?", "!")) or pause > 0.6
                or len(text) >= SRT_MAX_CHARS or word["end"] - current[0]["start"] >= SRT_MAX_SECONDS
                or (word["word"].endswith((",", ";", ":")) and len(text) > SRT_MAX_CHARS * 0.6)):
            cues.append({"start": current[0]["start"], "end": word["end"], "text": text})
            current = []
    return cues


def to_srt(cues):
    return "".join(f"{number}\n{srt_time(cue['start'])} --> {srt_time(cue['end'])}\n{cue['text']}\n\n"
                   for number, cue in enumerate(cues, 1))


def normalise(word):
    return re.sub(r"[^a-z0-9']+", "", word.lower().replace("’", "'"))


def script_words(path):
    """Spoken words of a .txt or a speak.py plan, with Fish tags removed."""
    path = Path(path)
    if path.suffix.lower() == ".json":
        document = read_json(path)
        text = " ".join(beat.get("text", "") for beat in document.get("beats", []))
    else:
        text = path.read_text(encoding="utf-8-sig")
    return [word for word in (normalise(token) for token in TAG.sub(" ", text).split()) if word]


def compare(expected, heard_words):
    heard = [normalise(item["word"]) for item in heard_words]
    heard_index = [index for index, word in enumerate(heard) if word]
    heard = [heard[index] for index in heard_index]
    issues = []
    matcher = difflib.SequenceMatcher(None, expected, heard, autojunk=False)
    for operation, a0, a1, b0, b1 in matcher.get_opcodes():
        if operation == "equal":
            continue
        at = heard_words[heard_index[min(b0, len(heard_index) - 1)]]["start"] if heard_index else 0
        issues.append({"type": operation, "time": at, "script": " ".join(expected[a0:a1]),
                       "heard": " ".join(heard[b0:b1])})
    return {"match_ratio": round(matcher.ratio(), 4), "differences": issues,
            "note": "Numbers, names and homophones often differ harmlessly; listen to flagged times before re-rendering."}


def run_chunks(audio, duration, args, request, out):
    """Transcribe chunk by chunk; each paid chunk is checkpointed in <out>.partial.json and never re-sent."""
    partial_path = Path(str(out) + ".partial.json")
    partial = read_json(partial_path) if partial_path.exists() else {}
    if partial.get("request_sha256") != request:
        partial = {"request_sha256": request, "chunks": {}}
    key = None
    words, cost = [], 0.0
    with tempfile.TemporaryDirectory(prefix="agent-audio-stt-") as folder:
        for start, end in chunk_bounds(audio, duration, args.chunk_seconds):
            name = f"{start:.3f}-{end:.3f}"
            if name not in partial["chunks"]:
                key = key or resolve_key(args.env_file)
                print(f"transcribe {start:.1f}-{end:.1f}s", file=sys.stderr, flush=True)
                chunk, chunk_cost = transcribe_chunk(audio, start, end, key, args.model, args.language, folder)
                partial = {**partial, "chunks": {**partial["chunks"], name: {"words": chunk, "cost": chunk_cost or 0}}}
                out.parent.mkdir(parents=True, exist_ok=True)
                write_json(partial_path, partial)
            words.extend(partial["chunks"][name]["words"])
            cost += partial["chunks"][name]["cost"]
    partial_path.unlink(missing_ok=True)
    return words, cost


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("audio", type=Path)
    parser.add_argument("--out", type=Path, required=True, help="words JSON; the SRT is written beside it")
    parser.add_argument("--srt", type=Path, help="SRT path (default: <out without .words.json>.srt)")
    parser.add_argument("--script", type=Path, help="Tagged .txt or speak.py plan to check the wording against")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--language", default="en")
    parser.add_argument("--chunk-seconds", type=float, default=CHUNK_SECONDS)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args(argv)
    try:
        require_ffmpeg()
        audio = args.audio.expanduser().resolve()
        out = args.out.expanduser().resolve()
        duration = audio_info(audio)["duration_seconds"]
        request = fingerprint({"audio": digest_file(audio), "model": args.model, "language": args.language,
                               "chunk": args.chunk_seconds})
        existing = read_json(out) if out.exists() else None
        if existing and existing.get("request_sha256") == request:
            result = existing
            print(f"Reusing transcript {out}", file=sys.stderr)
        else:
            if existing:
                raise AudioError(f"{out} belongs to different audio or settings; choose a new --out.")
            words, cost = run_chunks(audio, duration, args, request, out)
            result = {"source": str(audio), "model": args.model, "language": args.language,
                      "duration_seconds": duration, "request_sha256": request, "cost_usd": round(cost, 5),
                      "words": words}
        if args.script:
            result = {**result, "script_check": compare(script_words(args.script), result["words"])}
        out.parent.mkdir(parents=True, exist_ok=True)
        write_json(out, result)
        srt = args.srt or out.with_name(out.name.removesuffix(".json").removesuffix(".words") + ".srt")
        Path(srt).write_text(to_srt(subtitle_cues(result["words"])), encoding="utf-8")
        summary = {"words": str(out), "srt": str(srt), "word_count": len(result["words"]),
                   "duration_seconds": duration, "cost_usd": result.get("cost_usd")}
        if "script_check" in result:
            summary["script_match"] = result["script_check"]["match_ratio"]
            summary["script_differences"] = len(result["script_check"]["differences"])
        print(json.dumps(summary, indent=2))
        return 0
    except USER_ERRORS as error:
        return fail(error)


if __name__ == "__main__":
    raise SystemExit(main())
