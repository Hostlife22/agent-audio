#!/usr/bin/env python3
"""Render tagged Fish Audio narration through OpenRouter (standard library only).

  speak.py --plan narration.json --out vo.mp3 [--dry-run] [--resume]
  speak.py --script tagged.txt --mode ironic --out vo.mp3
"""

import argparse
import json
from pathlib import Path
import re
import shutil
import sys
import time
import urllib.parse

from common import (AudioError, ROOT, USER_ERRORS, audio_info, digest, digest_file, fail, fingerprint, looks_like_mp3,
                    post, read_json, resolve_key, run_ffmpeg, write_json)

ROUTE = "/audio/speech"
ENDPOINT = "https://openrouter.ai/api/v1" + ROUTE
SpeechError = AudioError  # Kept for older imports.
BEAT_FIELDS = {"text", "mode", "direction", "shot"}


def voice_id(value):
    value = value.strip()
    if re.fullmatch(r"[a-fA-F0-9]{32}", value):
        return value.lower()
    url = urllib.parse.urlsplit(value)
    if url.scheme == "https" and url.hostname in ("fish.audio", "www.fish.audio"):
        match = re.fullmatch(r"/(?:app/)?m/([a-fA-F0-9]{32})/?", url.path)
        if match:
            return match.group(1).lower()
    raise SpeechError("Voice must be a 32-character Fish voice ID or its https://fish.audio/m/... URL.")


def check_paired_tags(text):
    stack = []
    for match in re.finditer(r"<(/?)([A-Za-z][A-Za-z0-9_-]*)>", text):
        closing, tag = match.groups()
        if closing:
            if not stack or stack.pop() != tag:
                raise SpeechError(f"Unbalanced paired tag: {match.group(0)}")
        else:
            stack.append(tag)
    if stack:
        raise SpeechError(f"Unclosed paired tag: <{stack[-1]}>")


def load_beats(args, config):
    if args.plan:
        document = read_json(args.plan)
        if not isinstance(document, dict) or set(document) - {"beats", "source", "notes"}:
            raise SpeechError('Plan must be an object with "beats" (optional: "source", "notes").')
        beats = document.get("beats")
        if not isinstance(beats, list) or not beats:
            raise SpeechError('Plan needs a non-empty "beats" array.')
    else:
        text = Path(args.script).read_text(encoding="utf-8-sig").strip()
        beats = [{"text": paragraph} for paragraph in re.split(r"\n\s*\n", text) if paragraph.strip()]
    result = []
    for number, beat in enumerate(beats, 1):
        if not isinstance(beat, dict) or set(beat) - BEAT_FIELDS:
            raise SpeechError(f"Beat {number}: allowed fields are {', '.join(sorted(BEAT_FIELDS))}.")
        text = beat.get("text")
        if not isinstance(text, str) or not text.strip():
            raise SpeechError(f"Beat {number}: text is empty or not a string.")
        text = text.strip()
        mode = beat.get("mode", args.mode)
        if not isinstance(mode, str) or mode not in config["modes"]:
            raise SpeechError(f"Beat {number}: unknown mode {mode!r}.")
        direction = beat.get("direction", "")
        if not isinstance(direction, str):
            raise SpeechError(f"Beat {number}: direction must be text.")
        check_paired_tags(text)
        # A beat is atomic: never cut a selected phrase or its paired tags in half.
        if len(text) > args.chunk_chars:
            raise SpeechError(f"Beat {number} exceeds --chunk-chars; split it into shorter beats/paragraphs.")
        style = config["base_direction"] + " " + config["modes"][mode]["direction"]
        if direction.strip():
            style += " " + direction.strip()
        result.append({"text": text, "voice": args.voice, "instructions": style})
    if not result:
        raise SpeechError("The script contains no speech.")
    return result


def make_payloads(beats, model, selected_voice, chunk_chars):
    groups, current, size = [], [], 0
    for beat in beats:
        if current and size + len(beat["text"]) > chunk_chars:
            groups.append(current)
            current, size = [], 0
        current.append(beat)
        size += len(beat["text"])
    if current:
        groups.append(current)
    return [{"model": model, "voice": selected_voice, "input": group, "response_format": "mp3"}
            for group in groups]


def post_speech(payload, key, timeout):
    body, metadata = post(ROUTE, payload, key, timeout)
    if "json" in metadata["content_type"] or "html" in metadata["content_type"] or not looks_like_mp3(body):
        # Do not dump an unexpected response: it may echo request data.
        raise SpeechError(f"Expected MP3; got {metadata['content_type']!r}, {len(body)} bytes. No audio saved.")
    return body, metadata


def inspect_audio(path):
    info = audio_info(path)
    if info.get("codec_name") != "mp3":
        raise SpeechError("Generated file contains no playable MP3 audio.")
    return {"bytes": Path(path).stat().st_size, **info}


def combine(parts, output):
    temporary = output.with_name(output.stem + ".assembling.mp3")
    if temporary.exists():
        raise SpeechError(f"Temporary output already exists; inspect it before retrying: {temporary}")
    try:
        if len(parts) == 1:
            shutil.copyfile(parts[0], temporary)
        else:
            playlist = parts[0].parent / "concat.txt"
            # Only generated basenames enter the concat file; user paths stay in argv.
            playlist.write_text("".join(f"file '{part.name}'\n" for part in parts), encoding="utf-8")
            run_ffmpeg(["-n", "-f", "concat", "-safe", "1", "-i", str(playlist), "-vn",
                        "-c:a", "libmp3lame", "-q:a", "2", str(temporary)], "joining audio parts")
        info = inspect_audio(temporary)
        if output.exists():
            raise SpeechError(f"Refusing to overwrite {output}")
        temporary.replace(output)
        return info
    finally:
        temporary.unlink(missing_ok=True)


def render(payloads, output, key, resume=False, timeout=240):
    output = Path(output).expanduser().resolve()
    if output.suffix.lower() != ".mp3":
        raise SpeechError("--out must end in .mp3")
    manifest_path = Path(str(output) + ".manifest.json")
    parts_dir = Path(str(output) + ".parts")
    request_hash = fingerprint(payloads)
    manifest = None
    if manifest_path.exists():
        if not resume:
            raise SpeechError(f"Output state exists; use --resume or a different --out: {manifest_path}")
        manifest = read_json(manifest_path)
        if manifest.get("request_sha256") != request_hash:
            raise SpeechError("Resume refused: text, voice, model, directions, or chunking changed.")
    elif resume and (output.exists() or parts_dir.exists()):
        raise SpeechError("Resume refused: output/parts exist without a matching manifest.")
    if output.exists():
        if (manifest and manifest.get("complete")
                and digest_file(output) == manifest.get("output_sha256")):
            print(f"Already complete: {output}", flush=True)
            return manifest
        raise SpeechError(f"Refusing to overwrite an existing or changed file: {output}")
    if len(payloads) > 1 and not shutil.which("ffmpeg"):
        raise SpeechError("ffmpeg is required to join multiple requests; install it before rendering.")
    output.parent.mkdir(parents=True, exist_ok=True)
    if manifest is None:
        if parts_dir.exists():
            raise SpeechError(f"Parts directory already exists: {parts_dir}")
        parts_dir.mkdir()
        manifest = {"schema_version": 1, "endpoint": ENDPOINT, "requested_model": payloads[0]["model"],
                    "requested_voice": payloads[0]["voice"], "request_sha256": request_hash,
                    "complete": False, "parts": []}
        # Payloads contain text and voice settings, never credentials.
        write_json(parts_dir / "requests.json", payloads)
        write_json(manifest_path, manifest)
    # Verify all completed parts before any additional paid request.
    for index, record in enumerate(manifest["parts"]):
        part = parts_dir / f"part-{index + 1:04d}.mp3"
        if not part.is_file() or digest_file(part) != record["sha256"]:
            raise SpeechError(f"Cached part {index + 1} is missing or changed; not regenerating it automatically.")
    paths = []
    for index, payload in enumerate(payloads):
        part = parts_dir / f"part-{index + 1:04d}.mp3"
        paths.append(part)
        if index < len(manifest["parts"]):
            print(f"Reuse part {index + 1}/{len(payloads)}", file=sys.stderr, flush=True)
            continue
        if part.exists():
            raise SpeechError(f"Uncheckpointed audio exists; inspect before another paid request: {part}")
        print(f"Generate part {index + 1}/{len(payloads)}; voice {payload['voice']}", file=sys.stderr, flush=True)
        started = time.monotonic()
        body, metadata = post_speech(payload, key, timeout)
        part.write_bytes(body)
        audio_info = inspect_audio(part)
        manifest["parts"].append({"file": part.name, "sha256": digest(body),
                                  "elapsed_seconds": round(time.monotonic() - started, 2),
                                  **metadata, **audio_info})
        write_json(manifest_path, manifest)
    info = combine(paths, output)
    manifest.update(complete=True, output=str(output), output_sha256=digest_file(output), **info)
    write_json(manifest_path, manifest)
    print(json.dumps({"output": str(output), "requested_voice": manifest["requested_voice"],
                      "model": manifest["requested_model"], **info}, ensure_ascii=False), flush=True)
    return manifest


def main(argv=None):
    config = read_json(ROOT / "references" / "presets.json")
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--script", type=Path, help="UTF-8 text; blank lines separate beats.")
    source.add_argument("--plan", type=Path, help="JSON beats with individual modes/directions.")
    parser.add_argument("--out", type=Path, help="Destination .mp3; required except with --dry-run.")
    parser.add_argument("--mode", choices=config["modes"], default=config["default_mode"])
    parser.add_argument("--voice", default=config["voice_id"], help="Fish voice ID or voice page URL.")
    parser.add_argument("--model", choices=("fish-audio/drama-3", "fish-audio/s2.1-pro"), default=config["model"])
    parser.add_argument("--env-file", type=Path, help="Explicit credentials file; never copied into output.")
    parser.add_argument("--chunk-chars", type=int, default=2400, help="Spoken text characters per request (not an API limit).")
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--dry-run", action="store_true", help="Print payloads without loading keys or calling APIs.")
    parser.add_argument("--resume", action="store_true", help="Reuse verified parts from the same request.")
    args = parser.parse_args(argv)
    key = ""
    try:
        if args.chunk_chars < 100 or args.timeout < 1:
            raise SpeechError("--chunk-chars must be at least 100 and --timeout must be positive.")
        args.voice = voice_id(args.voice)
        beats = load_beats(args, config)
        payloads = make_payloads(beats, args.model, args.voice, args.chunk_chars)
        if args.dry_run:
            print(json.dumps({"requests": payloads, "request_count": len(payloads)}, ensure_ascii=False, indent=2))
            return 0
        if args.out is None:
            raise SpeechError("--out is required for generation.")
        key = resolve_key(args.env_file)
        render(payloads, args.out, key, args.resume, args.timeout)
        return 0
    except USER_ERRORS as error:
        return fail(error, key)


if __name__ == "__main__":
    raise SystemExit(main())
