#!/usr/bin/env python3
"""Generate original music or non-speech effects through OpenRouter.

  generate_asset.py --kind music --model pro --prompt-file act1.txt --seconds 95 --out m_act1.wav
  generate_asset.py --kind music --prompt "..." --seconds 30 --out m_sting.wav     # Lyria 3 Clip (30 s cue)
  generate_asset.py --kind sfx --prompt "One rubber stamp thud..." --seconds 0.6 --out stamp.wav
  generate_asset.py --library stamp-thud                                            # shared cached effect
  generate_asset.py --music-library ragtime-america                                 # shared cached music theme

Music: `clip` always returns about 30 s; `pro` follows a target length stated in the prompt (about 3 minutes max)
and accepts timed sections like "[0:20 - 0:45] ...". Section boundaries land within a second or two, so make exact
stops and silences in the mix, not in the prompt. `--seconds` trims locally; it never pads or stretches.
Each output keeps `<out>.source.mp3` (the paid bytes) and `<out>.manifest.json`; --resume reuses them.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import math
import os
import sys
from pathlib import Path

from common import (AudioError, ROOT, audio_info, decode_mono, digest, digest_file, fingerprint, frame_levels,
                    looks_like_mp3, post, post_stream, read_json, redact, require_ffmpeg, resolve_key, run_ffmpeg,
                    write_json, USER_ERRORS, fail)

MUSIC_MODELS = {"clip": "google/lyria-3-clip-preview", "pro": "google/lyria-3-pro-preview"}
SFX_MODEL = "bytedance-seed/seed-audio-1-0"
MAX_SECONDS = {"clip": 30.0, "pro": 200.0, "sfx": 30.0}
SFX_PROMPT_LIMIT = 3000
LIBRARY_FILE = ROOT / "references" / "sfx-library.json"
MUSIC_LIBRARY_FILE = ROOT / "references" / "music-library.json"
LIBRARIES = {"sfx": (LIBRARY_FILE, "sounds", ""), "music": (MUSIC_LIBRARY_FILE, "themes", "music")}
PROCESSING_VERSION = 3
SFX_ONSET_DB = 24   # attack: first 5 ms window within this many dB of the peak
SFX_TAIL_DB = 48    # tail: last window within this many dB of the peak
# Never mention a narrator or voice-over here: in testing it invited Lyria to add mumbled speech.
MUSIC_SUFFIX = (" Purely instrumental throughout: no voice of any kind, no singing, no humming, no choir, no spoken"
                " words, no sound effects. Keep the arrangement light in the midrange.")
SFX_SUFFIX = (" Isolated sound effect only. No speech, no human voice, no music."
              " Clean close recording on silence, no unrelated background noise.")


class RejectedAudio(AudioError):
    """A paid response that failed validation; `body` is kept so the bytes are not lost."""

    def __init__(self, message, body):
        super().__init__(message)
        self.body = body


def asset_path(folder, name, payload):
    """`<name>-<request hash>.wav`: a changed prompt or length gets a new file and never overwrites a paid one."""
    return Path(folder) / f"{name}-{fingerprint(payload)[:8]}.wav"


def library_dir():
    return Path(os.environ.get("AGENT_AUDIO_LIBRARY") or ROOT / "library").expanduser().resolve()


def library_entry(name, kind="sfx"):
    """An effect from sfx-library.json or (kind="music") a theme from music-library.json."""
    path, section, _ = LIBRARIES[kind]
    entries = read_json(path)[section]
    if not isinstance(name, str) or name not in entries:
        label = "sound" if kind == "sfx" else "music theme"
        raise AudioError(f"Unknown library {label} {name!r}. Known: {', '.join(sorted(entries))}")
    return entries[name]


def library_request(name, kind="sfx"):
    """(prompt, seconds, model, payload) for a library entry."""
    entry = library_entry(name, kind)
    model = entry.get("model", "clip") if kind == "music" else "clip"
    return entry["prompt"], entry["seconds"], model, make_payload(kind, entry["prompt"], entry["seconds"], model)


def make_payload(kind, prompt, seconds, model="clip"):
    """Build the request; raises before any credential or network use."""
    if kind not in ("music", "sfx") or (kind == "music" and model not in MUSIC_MODELS):
        raise AudioError("kind must be music (model clip|pro) or sfx.")
    if not isinstance(prompt, str) or not prompt.strip():
        raise AudioError("Prompt is empty or not text.")
    limit = MAX_SECONDS["sfx" if kind == "sfx" else model]
    if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0.05 <= seconds <= limit:
        raise AudioError(f"--seconds must be between 0.05 and {limit:g} for {kind if kind == 'sfx' else model}.")
    if kind == "music":
        length = ("Create an original 30-second instrumental cue. " if model == "clip"
                  else f"Create an original instrumental piece about {round(seconds)} seconds long. ")
        return {"model": MUSIC_MODELS[model], "messages": [{"role": "user", "content": length + prompt.strip() + MUSIC_SUFFIX}],
                "modalities": ["audio"], "audio": {"format": "mp3"}, "stream": True}
    text = f"A sound effect about {seconds:g} seconds long. " + prompt.strip() + SFX_SUFFIX
    if len(text) > SFX_PROMPT_LIMIT:
        raise AudioError(f"SFX prompt exceeds {SFX_PROMPT_LIMIT} characters including directions.")
    # No voice or input_references: a voice routes Seed toward speech synthesis.
    return {"model": SFX_MODEL, "input": text, "response_format": "mp3"}


def music_collector():
    chunks, metadata = [], {}

    def consume(data):
        event = json.loads(data)
        if event.get("error"):
            detail = redact(json.dumps(event["error"])[:400])
            raise AudioError(f"Music provider returned a stream error: {detail}. No automatic retry; "
                             "it may have been billed. A prompt rejected by the safety filter needs rewording.")
        if event.get("id"):
            metadata["generation_id"] = event["id"]
        if event.get("usage"):
            metadata["usage"] = event["usage"]
        for choice in event.get("choices", []):
            if choice.get("finish_reason") == "error":
                raise AudioError("Music stream ended with a provider error; no automatic retry.")
            encoded = ((choice.get("delta") or {}).get("audio") or {}).get("data")
            if encoded:
                chunks.append(base64.b64decode(encoded, validate=True))

    return chunks, metadata, consume


def parse_music_stream(lines):
    """Decode SSE events from Lyria into MP3 bytes. Kept separate for offline tests."""
    from common import parse_sse
    chunks, metadata, consume = music_collector()
    if not parse_sse(lines, consume):
        raise AudioError("Music stream was interrupted before [DONE]; it may have been billed.")
    return finish_music(chunks, metadata)


def finish_music(chunks, metadata):
    body = b"".join(chunks)
    if not looks_like_mp3(body):
        raise RejectedAudio("Music stream did not contain valid-looking MP3 audio.", body)
    return body, metadata


def request_audio(payload, key, timeout):
    if payload.get("stream"):
        chunks, metadata, consume = music_collector()
        try:
            done = post_stream("/chat/completions", payload, key, timeout, consume, "agent-audio music")
        except (ValueError, binascii.Error) as error:
            raise AudioError(f"Malformed music stream ({error}); it may have been billed.") from None
        if not done:
            raise RejectedAudio("Music stream was interrupted before [DONE]; it may have been billed.", b"".join(chunks))
        return finish_music(chunks, metadata)
    body, metadata = post("/audio/speech", payload, key, timeout, "agent-audio sfx")
    if "json" in metadata["content_type"] or not looks_like_mp3(body):
        raise RejectedAudio(f"Expected MP3 effect; got {metadata['content_type']!r}, {len(body)} bytes.", body)
    return body, metadata


def sfx_bounds(source, seconds):
    """Start at the attack (so the hit lands on its anchor) and keep the natural tail.

    Seed often returns leading silence and a longer tail than asked; a plain trim would land late and chop the decay.
    """
    window = 0.005
    levels = frame_levels(decode_mono(source, 16000), 16000, window)
    if not levels or max(levels) < -70:
        raise AudioError(f"{source} is silent; regenerate the effect.")
    peak = max(levels)
    onset = next(index for index, level in enumerate(levels) if level > peak - SFX_ONSET_DB)
    tail = max(index for index, level in enumerate(levels) if level > peak - SFX_TAIL_DB)
    start = max(0.0, onset * window - 0.01)
    natural = (tail + 1) * window + 0.05 - start
    return round(start, 3), round(min(natural, max(seconds * 1.5, seconds + 0.5)), 3)


def convert_asset(source, output, seconds, kind):
    """Music: trim to the requested length. SFX: cut from the attack, keep the tail. 48 kHz stereo WAV/MP3."""
    available = audio_info(source)["duration_seconds"]
    start, length = (0.0, min(seconds, available)) if kind == "music" else sfx_bounds(source, seconds)
    fade_in = min(0.05 if kind == "music" else 0.003, length / 4)
    fade_out = min(0.8 if kind == "music" else 0.04, length / 4)
    filters = (f"atrim=start={start}:duration={length},asetpts=PTS-STARTPTS,afade=t=in:d={fade_in},"
               f"afade=t=out:st={length - fade_out}:d={fade_out}")
    temporary = output.with_name(output.stem + ".working" + output.suffix)
    temporary.unlink(missing_ok=True)
    codec = ["-c:a", "pcm_s24le"] if output.suffix.lower() == ".wav" else ["-c:a", "libmp3lame", "-b:a", "192k"]
    try:
        run_ffmpeg(["-n", "-i", str(source), "-af", filters, "-ar", "48000", "-ac", "2", *codec, str(temporary)],
                   "asset conversion")
        info = audio_info(temporary)
        temporary.replace(output)
        return {**info, "source_offset_seconds": start}
    finally:
        temporary.unlink(missing_ok=True)


def render_asset(kind, prompt, seconds, output, key_loader, resume=False, timeout=600, model="clip"):
    """Pay once per distinct request; any later change of length or processing re-cuts the cached source for free."""
    payload = make_payload(kind, prompt, seconds, model)
    output = Path(output).expanduser().resolve()
    if output.suffix.lower() not in (".mp3", ".wav"):
        raise AudioError("Output extension must be .mp3 or .wav.")
    require_ffmpeg()
    request_hash = fingerprint({"kind": kind, "request": payload})
    processing = {"requested_seconds": seconds, "output_format": output.suffix.lower(),
                  "processing_version": PROCESSING_VERSION}
    manifest_path = Path(str(output) + ".manifest.json")
    source = Path(str(output) + ".source.mp3")
    if manifest_path.exists():
        manifest = read_json(manifest_path)
        same_request = (manifest.get("request_sha256") == request_hash
                        or (manifest.get("kind") == kind and manifest.get("request") == payload))  # older manifests
        if not same_request:
            raise AudioError(f"{output.name} exists with a different prompt/model; choose a new output path.")
        if (manifest.get("complete") and manifest.get("processing") == processing and output.is_file()
                and digest_file(output) == manifest.get("output_sha256")):
            return {**check_speech(kind, output, manifest, manifest_path, key_loader), "reused": True}
        if not source.is_file() or digest_file(source) != manifest.get("source_sha256"):
            raise AudioError("Paid source is missing, changed, or was never received. No automatic retry; inspect "
                             f"the request, then delete {manifest_path.name} to pay for a new attempt.")
        if not resume and not manifest.get("complete"):
            raise AudioError(f"Unfinished asset state at {manifest_path}; rerun with --resume to reuse the paid source.")
        output.unlink(missing_ok=True)
    else:
        if output.exists() or source.exists():
            raise AudioError(f"{output} or its source already exists without a manifest; choose a new output path.")
        key = key_loader()  # Credentials load only when a paid request is needed.
        output.parent.mkdir(parents=True, exist_ok=True)
        manifest = paid_request(payload, key, timeout, output, manifest_path, source,
                                {"kind": kind, "request": payload, "request_sha256": request_hash})
    info = convert_asset(source, output, seconds, kind)
    manifest = {**manifest, **info, "processing": processing, "complete": True, "state": "complete",
                "output_sha256": digest_file(output), "source_duration_seconds": audio_info(source)["duration_seconds"]}
    write_json(manifest_path, manifest)
    return check_speech(kind, output, manifest, manifest_path, key_loader)


def check_speech(kind, output, manifest, manifest_path, key_loader):
    """Music only: transcribe once (about $0.004/min) and record any speech the model slipped into the track."""
    if kind != "music" or "speech_check" in manifest or os.environ.get("AGENT_AUDIO_SKIP_SPEECH_CHECK"):
        return manifest
    from transcribe import speech_spans  # local import: transcribe is only needed for music
    manifest = {**manifest, "speech_check": speech_spans(output, key_loader())}
    write_json(manifest_path, manifest)
    for span in manifest["speech_check"]["spans"]:
        print(f"WARN {output.name}: speech heard at {span['start']:.1f}-{span['end']:.1f}s ({span['words']!r}); "
              "keep cues out of that span or regenerate.", file=sys.stderr)
    return manifest


def speech_in(path):
    """Speech spans recorded for a generated asset (empty if it was never checked or is not music)."""
    manifest = Path(str(path) + ".manifest.json")
    return read_json(manifest).get("speech_check", {}).get("spans", []) if manifest.is_file() else []


def paid_request(payload, key, timeout, output, manifest_path, source, base):
    """One request under an exclusive lock, so parallel or concurrent runs never pay twice for one file."""
    lock = Path(str(output) + ".lock")
    try:
        handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise AudioError(f"Another run is generating {output.name} (lock {lock.name}); delete the lock if it is stale.") from None
    try:
        if manifest_path.exists():
            raise AudioError(f"{output.name} was started by another run; rerun to reuse its result.")
        manifest = {**base, "complete": False, "state": "request-started"}
        write_json(manifest_path, manifest)
        try:
            body, metadata = request_audio(payload, key, timeout)
        except RejectedAudio as error:
            rejected = Path(str(output) + ".rejected")
            rejected.write_bytes(error.body)
            write_json(manifest_path, {**manifest, "state": "rejected", "rejected_file": rejected.name})
            raise AudioError(f"{error} The paid response is kept in {rejected.name}. Delete "
                             f"{manifest_path.name} to pay for a new attempt.") from None
        with source.open("xb") as stream:
            stream.write(body)
        manifest = {**manifest, "source_sha256": digest(body), "provider": metadata, "state": "source-saved"}
        write_json(manifest_path, manifest)
        return manifest
    finally:
        os.close(handle)
        lock.unlink(missing_ok=True)


def adopt_legacy(legacy, output, kind, payload):
    """Move a pre-hash asset (`name.wav` + source + manifest) to its hashed path if it holds this exact request."""
    legacy, output = Path(legacy), Path(output)
    old_manifest = Path(str(legacy) + ".manifest.json")
    if Path(str(output) + ".manifest.json").exists() or not old_manifest.exists():
        return False
    manifest = read_json(old_manifest)
    if manifest.get("kind") != kind or manifest.get("request") != payload:
        return False
    for suffix in (".source.mp3", "", ".manifest.json"):
        old = Path(str(legacy) + suffix)
        if old.exists():
            old.replace(Path(str(output) + suffix))
    return True


def library_path(name, kind="sfx"):
    payload = library_request(name, kind)[3]
    return asset_path(library_dir() / LIBRARIES[kind][2], name, payload)


def render_library(name, key_loader, timeout=600, kind="sfx"):
    """Render a library entry once into the shared cache; every later call reuses it without a request."""
    prompt, seconds, model, payload = library_request(name, kind)
    output = library_path(name, kind)
    adopt_legacy(output.parent / f"{name}.wav", output, kind, payload)
    return output, render_asset(kind, prompt, seconds, output, key_loader, resume=True, timeout=timeout, model=model)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kind", choices=("music", "sfx"))
    parser.add_argument("--model", choices=tuple(MUSIC_MODELS), default="clip", help="music model (default clip)")
    prompts = parser.add_mutually_exclusive_group()
    prompts.add_argument("--prompt")
    prompts.add_argument("--prompt-file", type=Path)
    prompts.add_argument("--library", help="Effect from references/sfx-library.json, rendered into the shared cache.")
    prompts.add_argument("--music-library", help="Theme from references/music-library.json, rendered into the shared cache.")
    parser.add_argument("--seconds", type=float)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args(argv)
    key_loader = lambda: resolve_key(args.env_file)
    try:
        if args.library or args.music_library:
            kind, name = ("sfx", args.library) if args.library else ("music", args.music_library)
            if args.dry_run:
                print(json.dumps({"request": library_request(name, kind)[3], "output": str(library_path(name, kind))},
                                 indent=2))
                return 0
            output, result = render_library(name, key_loader, args.timeout, kind)
            print(json.dumps({"output": str(output), "duration_seconds": result["duration_seconds"],
                              "reused": result.get("reused", False)}, indent=2))
            return 0
        if not args.kind or args.seconds is None or not (args.prompt or args.prompt_file):
            raise AudioError("Give --kind, --seconds and --prompt/--prompt-file, or --library / --music-library NAME.")
        prompt = args.prompt if args.prompt is not None else args.prompt_file.read_text(encoding="utf-8")
        payload = make_payload(args.kind, prompt, args.seconds, args.model)
        if args.dry_run:
            print(json.dumps({"request": payload, "requested_seconds": args.seconds}, indent=2, ensure_ascii=False))
            return 0
        if args.out is None:
            raise AudioError("--out is required unless --dry-run is used.")
        result = render_asset(args.kind, prompt, args.seconds, args.out, key_loader, args.resume, args.timeout, args.model)
        print(json.dumps({"output": str(args.out.expanduser().resolve()), "model": result["request"]["model"],
                          "duration_seconds": result["duration_seconds"],
                          "source_duration_seconds": result.get("source_duration_seconds"),
                          "reused": result.get("reused", False)}, indent=2))
        return 0
    except USER_ERRORS as error:
        return fail(error)


if __name__ == "__main__":
    raise SystemExit(main())
