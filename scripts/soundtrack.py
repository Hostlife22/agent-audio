#!/usr/bin/env python3
"""Build a finished soundtrack from a cue plan: generate missing assets, mix, master, QA, mux into the video.

  soundtrack.py --plan soundtrack.json --dry-run                     # resolve every anchor, list assets and cost
  soundtrack.py --plan soundtrack.json --assets-only                 # generate assets, audition before mixing
  soundtrack.py --plan soundtrack.json --out mix/final.wav --video-out film_scored.mp4

Times in the plan are anchors, so cues survive a re-timed film: 12.5 | "k12" | "k12+1.4" | "k12:end-0.3" |
"word:die" | "word:reporters@2" | "word:lead~140" (occurrence nearest 140 s); any anchor may end in +/-seconds.
Shot anchors need "timeline" (timeline.py output); word anchors need "words" (transcribe.py output).
See references/soundtrack-plan.md for every field.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
import re
import sys
import tempfile

from common import (AudioError, USER_ERRORS, audio_info, digest_file, fail, media_duration, read_json, require_ffmpeg,
                    resolve_key, write_json)
from generate_asset import (adopt_legacy, asset_path, library_path, library_request, make_payload, render_asset,
                            render_library, speech_in)
import music_sections
import mix
import qa

PLAN_FIELDS = {"narration", "timeline", "words", "video", "profile", "duration_seconds", "duck",
               "voice_compression", "assets", "music", "ambience", "sfx", "dips", "notes"}
ASSET_FIELDS = {"file", "library", "music_library", "kind", "model", "prompt", "prompt_file", "seconds", "sections",
                "notes"}
BED_FIELDS = {"asset", "section", "start", "end", "source_start", "gain_db", "fade_in", "fade_out", "loop", "notes"}
WEAK_SECTION = 1.0  # music_sections strength below this: the music barely changes there
SFX_FIELDS = {"asset", "at", "source_start", "duration", "gain_db", "fade_in", "fade_out", "notes"}
DIP_FIELDS = {"start", "end", "gain_db", "ramp_in", "ramp_out", "notes"}
# Approximate OpenRouter prices on 2026-10-22, for the dry-run estimate only.
PRICE = {"clip": 0.04, "pro": 0.08, "sfx_per_second": 0.0025, "speech_check": 0.006}
PARALLEL_REQUESTS = 4
NUMBER = re.compile(r"[+-]?\d+(?:\.\d+)?")
ANCHOR = re.compile(r"^(?P<base>word:[\w'’]+(?:@\d+|~\d+(?:\.\d+)?)?(?::end)?|[^:+~@]+?(?::end)?)"
                    r"(?P<offset>[+-]\d+(?:\.\d+)?)?$")


def normalise(word):
    return re.sub(r"[^a-z0-9']+", "", word.lower().replace("’", "'"))


class Anchors:
    def __init__(self, timeline=None, words=None):
        shots = (timeline or {}).get("shots", [])
        self.shots = {shot["id"]: shot for shot in shots}
        self.duplicates = {shot["id"] for shot in shots if [s["id"] for s in shots].count(shot["id"]) > 1}
        self.words = words or []

    def __call__(self, value, label):
        if isinstance(value, bool) or value is None:
            raise AudioError(f"{label}: missing time")
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip()
        if text in self.shots:  # a shot id wins over a numeric reading ("12" can be a shot)
            return self._shot(text, label)
        if NUMBER.fullmatch(text):
            return float(text)
        match = ANCHOR.match(text)
        if not match:
            raise AudioError(f"{label}: cannot read anchor {text!r}")
        offset = float(match.group("offset") or 0)
        base = match.group("base")
        return (self._word(base, label) if base.startswith("word:") else self._shot(base, label)) + offset

    def _shot(self, base, label):
        name, _, end = base.partition(":")
        if name in self.duplicates:
            raise AudioError(f"{label}: shot id {name!r} appears more than once in the timeline")
        if name not in self.shots:
            raise AudioError(f"{label}: shot {name!r} not in the timeline" if self.shots else
                             f"{label}: shot anchors need \"timeline\" in the plan")
        return float(self.shots[name]["end" if end == "end" else "start"])

    def _word(self, base, label):
        if not self.words:
            raise AudioError(f"{label}: word anchors need \"words\" in the plan")
        body = base[5:]
        use_end = body.endswith(":end")
        body = body.removesuffix(":end")
        word, nth, near = re.match(r"([^@~]+)(?:@(\d+))?(?:~([\d.]+))?$", body).groups()
        hits = [item for item in self.words if normalise(item["word"]) == normalise(word)]
        if not hits:
            raise AudioError(f"{label}: word {word!r} not found in the transcript")
        if near is not None:
            hit = min(hits, key=lambda item: abs(item["start"] - float(near)))
        else:
            index = int(nth or 1) - 1
            if index < 0:
                raise AudioError(f"{label}: occurrences count from @1")
            if index >= len(hits):
                raise AudioError(f"{label}: word {word!r} occurs only {len(hits)} time(s)")
            hit = hits[index]
        return float(hit["end" if use_end else "start"])


def number(entry, field, default, label, low=-math.inf, high=math.inf):
    value = entry.get(field, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise AudioError(f"{label}: {field} must be a number between {low} and {high}")
    return float(value)


def check_fields(entry, allowed, label):
    if not isinstance(entry, dict):
        raise AudioError(f"{label} must be an object")
    unknown = set(entry) - allowed
    if unknown:
        raise AudioError(f"{label}: unknown field(s) {', '.join(sorted(unknown))}")


# ---------- plan ----------

def load_plan(path):
    path = Path(path).expanduser().resolve()
    plan = read_json(path)
    check_fields(plan, PLAN_FIELDS, "plan")
    base = path.parent

    def local(value, label, required=True):
        if value is None and not required:
            return None
        if not isinstance(value, str) or not value:
            raise AudioError(f"{label} must be a file path")
        result = (base / Path(value).expanduser()).resolve()
        if not result.is_file():
            raise AudioError(f"{label}: file not found: {result}")
        return result

    profile = plan.get("profile", "long")
    if profile not in mix.PROFILES:
        raise AudioError(f"profile must be one of {', '.join(mix.PROFILES)}")
    return {"path": path, "base": base, "raw": plan, "profile": profile, "local": local,
            "narration": local(plan.get("narration"), "narration"),
            "timeline": read_json(local(plan["timeline"], "timeline")) if plan.get("timeline") else None,
            "words": read_json(local(plan["words"], "words"))["words"] if plan.get("words") else None,
            "video": local(plan.get("video"), "video", required=False)}


def asset_specs(plan):
    specs = {}
    assets = plan["raw"].get("assets", {})
    if not isinstance(assets, dict):
        raise AudioError("assets must be an object of name -> asset")
    for name, entry in assets.items():
        label = f"asset {name}"
        check_fields(entry, ASSET_FIELDS, label)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise AudioError(f"{label}: use letters, digits, dot, dash or underscore in asset names")
        if "file" in entry:
            specs[name] = {"type": "file", "path": plan["local"](entry["file"], label)}
        elif "library" in entry or "music_library" in entry:
            kind = "sfx" if "library" in entry else "music"
            library_name = entry.get("library", entry.get("music_library"))
            _, _, model, payload = library_request(library_name, kind)
            path = library_path(library_name, kind)
            adopt_legacy(path.parent / f"{library_name}.wav", path, kind, payload)
            specs[name] = {"type": "library", "kind": kind, "model": model, "name": library_name, "path": path}
        else:
            prompt = entry.get("prompt")
            if entry.get("prompt_file"):
                prompt = plan["local"](entry["prompt_file"], label).read_text(encoding="utf-8")
            kind, model = entry.get("kind"), entry.get("model", "clip")
            seconds = number(entry, "seconds", None, label, 0.05, 200)
            payload = make_payload(kind, prompt, seconds, model)  # validates before anything is paid
            path = asset_path(plan["base"] / "assets", name, payload)
            adopt_legacy(plan["base"] / "assets" / f"{name}.wav", path, kind, payload)
            specs[name] = {"type": "generate", "kind": kind, "model": model, "prompt": prompt, "seconds": seconds,
                           "path": path}
        specs[name]["sections"] = section_map(entry.get("sections"), label)
    return specs


def section_map(value, label):
    """{"name": start seconds as requested in the prompt}; checked here, refined against the audio later."""
    if value is None:
        return {}
    if not isinstance(value, dict) or not value:
        raise AudioError(f"{label}: sections must be an object of name -> start seconds")
    for name, at in value.items():
        if isinstance(at, bool) or not isinstance(at, (int, float)) or at < 0:
            raise AudioError(f"{label}: section {name!r} needs a start in seconds (>= 0)")
    return {name: float(at) for name, at in value.items()}


def section_starts(specs, files, measure=True):
    """Asset -> {section: {"requested", "start", "strength"}}; starts move to where the music really changes."""
    result = {}
    for name, spec in specs.items():
        if spec.get("sections"):
            result[name] = (music_sections.refine(files[name], spec["sections"]) if measure else
                            {key: {"requested": at, "start": at, "strength": None} for key, at in spec["sections"].items()})
    return result


def estimate_cost(specs):
    total = 0.0
    for spec in specs.values():
        if spec["type"] == "generate" and not _complete(spec["path"]):
            total += (PRICE[spec["model"]] + PRICE["speech_check"] if spec["kind"] == "music"
                      else PRICE["sfx_per_second"] * max(1.0, spec["seconds"]))
        elif spec["type"] == "library" and not _complete(spec["path"]):
            seconds = library_request(spec["name"], spec["kind"])[1]
            total += (PRICE[spec["model"]] + PRICE["speech_check"] if spec["kind"] == "music"
                      else PRICE["sfx_per_second"] * max(1.0, seconds))
    return round(total, 3)


def _complete(path):
    manifest = Path(str(path) + ".manifest.json")
    return Path(path).is_file() and manifest.is_file() and read_json(manifest).get("complete", False)


def ensure_assets(specs, key_loader):
    """Render or re-cut every asset. Cached sources are reused; the key is read only if a paid request is needed."""
    key = []

    def lazy_key():
        if not key:
            key.append(key_loader())
        return key[0]

    def one(item):
        name, spec = item
        if not _complete(spec["path"]):
            print(f"generate {name} ({spec['type']})", file=sys.stderr, flush=True)
        if spec["type"] == "library":
            render_library(spec["name"], lazy_key, kind=spec["kind"])
        else:
            render_asset(spec["kind"], spec["prompt"], spec["seconds"], spec["path"], lazy_key, resume=True,
                         model=spec["model"])
        return name

    unique = {}
    for name, spec in specs.items():
        if spec["type"] != "file":
            unique.setdefault(str(spec["path"]), (name, spec))  # one request per file, however many names use it
    todo = list(unique.values())
    if any(not _complete(spec["path"]) for _, spec in todo):
        lazy_key()  # fail on a missing key before starting any request
    failures = []
    with ThreadPoolExecutor(max_workers=PARALLEL_REQUESTS) as pool:
        for name, future in [(item[0], pool.submit(one, item)) for item in todo]:
            try:
                future.result()
            except USER_ERRORS as error:
                failures.append(f"{name}: {error}")
    if failures:
        raise AudioError("Some assets failed (completed ones are kept):\n  " + "\n  ".join(failures))
    return {name: Path(spec["path"]) for name, spec in specs.items()}


# ---------- cue sheet ----------

def resolve_cues(plan, files, length, anchors, sections=None):
    cues, warnings = [], []
    raw = plan["raw"]
    for bus in ("music", "ambience", "sfx", "dips"):
        if not isinstance(raw.get(bus, []), list):
            raise AudioError(f"{bus} must be a list")
    for bus in ("music", "ambience"):
        for index, entry in enumerate(raw.get(bus, []), 1):
            label = f"{bus}[{index}]"
            check_fields(entry, BED_FIELDS, label)
            cues.append(_bed_cue(entry, bus, label, files, length, anchors, warnings, sections or {}))
    for index, entry in enumerate(raw.get("sfx", []), 1):
        label = f"sfx[{index}]"
        check_fields(entry, SFX_FIELDS, label)
        cues.append(_sfx_cue(entry, label, files, length, anchors))
    dips = []
    for index, entry in enumerate(raw.get("dips", []), 1):
        label = f"dips[{index}]"
        check_fields(entry, DIP_FIELDS, label)
        start, end = anchors(entry.get("start"), label), anchors(entry.get("end"), label)
        if not 0 <= start < end <= length + 1:
            raise AudioError(f"{label}: needs 0 <= start < end <= film length")
        dips.append({"start": round(start, 3), "end": round(end, 3), "gain_db": number(entry, "gain_db", -60, label, -96, 0),
                     "ramp_in": number(entry, "ramp_in", 0.04, label, 0, 5), "ramp_out": number(entry, "ramp_out", 0.35, label, 0, 5)})
    return sorted(cues, key=lambda cue: cue["at"]), dips, warnings


def _asset_file(entry, files, label):
    name = entry.get("asset")
    if name not in files:
        raise AudioError(f"{label}: unknown asset {name!r}")
    return files[name]


def _bed_cue(entry, bus, label, files, length, anchors, warnings, sections):
    source = _asset_file(entry, files, label)
    available = audio_info(source)["duration_seconds"]
    start = anchors(entry.get("start", 0), label + ".start")
    end = anchors(entry.get("end", length), label + ".end")
    source_start = number(entry, "source_start", 0, label, 0, available)
    if "section" in entry:
        source_start = _section_offset(entry, label, sections, source_start, min(end, length) - start, warnings)
    loop = entry.get("loop", False)
    if not isinstance(loop, bool):
        raise AudioError(f"{label}: loop must be true or false")
    if loop and source_start > 0:
        raise AudioError(f"{label}: loop replays the whole file; export the section as its own asset instead of source_start")
    if not 0 <= start < min(end, length):
        raise AudioError(f"{label}: needs 0 <= start < end within the film (start {start:.2f}, end {end:.2f}, "
                         f"film {length:.2f})")
    end = min(end, length)
    duration = end - start
    if not loop and duration > available - source_start + 0.01:
        warnings.append(f"{label}: {entry['asset']} has {available - source_start:.1f}s from source_start but the cue "
                        f"spans {duration:.1f}s; it stops early at {start + available - source_start:.2f}s "
                        "(set loop, add a cue, or end earlier).")
        duration = available - source_start
    for span in speech_in(source):
        if span["start"] < source_start + duration and span["end"] > source_start:
            warnings.append(f"{label}: {entry['asset']} has speech at {span['start']:.1f}-{span['end']:.1f}s of the "
                            f"asset ({span['words'][:40]!r}) inside this cue; move source_start or regenerate.")
    fade_in = number(entry, "fade_in", min(0.3, duration / 2), label, 0, duration)
    fade_out = number(entry, "fade_out", min(0.6, duration / 2), label, 0, duration)
    return {"bus": bus, "asset": entry["asset"], "file": str(source), "at": round(start, 3), "duration": round(duration, 3),
            "source_start": source_start, "gain_db": number(entry, "gain_db", 0, label, -40, 20),
            "fade_in": fade_in, "fade_out": fade_out, "loop": loop}


def _section_offset(entry, label, sections, offset, span, warnings):
    """source_start for a named section: its measured start plus any source_start given in the cue."""
    table = sections.get(entry["asset"], {})
    if entry["section"] not in table:
        known = ", ".join(table) or "none; add \"sections\" to the asset"
        raise AudioError(f"{label}: asset {entry['asset']!r} has no section {entry['section']!r} (sections: {known})")
    found = table[entry["section"]]
    strength = found.get("strength")
    if strength is not None and found["requested"] > 0 and strength < WEAK_SECTION:
        warnings.append(f"{label}: no clear change near {found['requested']}s in {entry['asset']} (used "
                        f"{found['start']}s); listen to that join or set source_start by hand.")
    following = sorted(item["start"] for item in table.values() if item["start"] > found["start"])
    if following and offset + span > following[0] - found["start"] + 0.5:
        warnings.append(f"{label}: plays {offset + span:.1f}s of section {entry['section']!r}, which lasts about "
                        f"{following[0] - found['start']:.1f}s; the next section's music will be heard.")
    return found["start"] + offset


def _sfx_cue(entry, label, files, length, anchors):
    source = _asset_file(entry, files, label)
    available = audio_info(source)["duration_seconds"]
    at = anchors(entry.get("at"), label + ".at")
    if not 0 <= at < length:
        raise AudioError(f"{label}: at={at:.2f}s is outside the film (0-{length:.2f}s)")
    source_start = number(entry, "source_start", 0, label, 0, available)
    duration = min(number(entry, "duration", available - source_start, label, 0.01, 600), available - source_start, length - at)
    return {"bus": "sfx", "asset": entry["asset"], "file": str(source), "at": round(at, 3), "duration": round(duration, 3),
            "source_start": source_start, "gain_db": number(entry, "gain_db", 0, label, -40, 20),
            "fade_in": number(entry, "fade_in", 0.002, label, 0, duration),
            "fade_out": number(entry, "fade_out", min(0.03, duration / 2), label, 0, duration), "loop": False}


def cue_sheet(cues, dips):
    rows = [f"{cue['at']:8.2f} {cue['bus']:<8} {cue['asset']:<22} {cue['duration']:6.2f}s gain {cue['gain_db']:+.1f}"
            + (f" placed {cue['placed_gain_db']:+.1f}dB" if "placed_gain_db" in cue else "") for cue in cues]
    rows += [f"{dip['start']:8.2f} dip      to {dip['end']:.2f} at {dip['gain_db']} dB" for dip in dips]
    return "\n".join(sorted(rows))


# ---------- build ----------

def film_length(plan):
    raw = plan["raw"]
    if "duration_seconds" in raw:
        return number(raw, "duration_seconds", None, "plan", 0.1, 86400)
    if plan["video"]:
        return media_duration(plan["video"])
    if plan["timeline"]:
        return float(plan["timeline"]["duration_seconds"])
    return audio_info(plan["narration"])["duration_seconds"]


def build(plan, files, out, video_out=None, run_qa=True, sections=None):
    length = film_length(plan)
    anchors = Anchors(plan["timeline"], plan["words"])
    if sections is None:
        sections = section_starts(asset_specs(plan), files)
    cues, dips, warnings = resolve_cues(plan, files, length, anchors, sections)
    out = Path(out).expanduser().resolve()
    stems = Path(str(out) + ".stems")
    meter = mix.Meter(plan["base"] / ".agent-audio-levels.json")
    voice_lufs = meter.measure(plan["narration"], "lufs")
    if voice_lufs < -60:
        raise AudioError(f"The narration measures {voice_lufs} LUFS (silent); check {plan['narration']}")
    protected = {Path(plan["narration"]).resolve(), *(Path(cue["file"]).resolve() for cue in cues)}
    if plan["video"]:
        protected.add(Path(plan["video"]).resolve())
    for target in (out, Path(video_out).expanduser().resolve() if video_out else None):
        if target in protected:
            raise AudioError(f"Refusing to overwrite an input file: {target}")
    placed = [{**cue, "placed_gain_db": mix.placement_gain(meter, cue, plan["profile"], mix.VOICE_LUFS)} for cue in cues]
    raw = plan["raw"]
    job = {"narration": str(plan["narration"]), "duration_seconds": round(length, 3), "profile": plan["profile"],
           "voice_gain_db": round(mix.VOICE_LUFS - voice_lufs, 2), "voice_compression": raw.get("voice_compression", True),
           "duck": raw.get("duck", True), "cues": placed, "dips": dips}
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"mixing {len(placed)} cues over {length:.1f}s", file=sys.stderr, flush=True)
    outputs = mix.render_premix(job, stems)
    mastered = mix.master(outputs["premix"], out, mix.PROFILES[plan["profile"]]["target_lufs"])
    result = {"output": str(out), "stems": str(stems), "duration_seconds": round(length, 3), "master": mastered,
              "sections": sections,
              "warnings": warnings, "cue_sheet": cue_sheet(placed, dips).splitlines()}
    if video_out:
        result["mux"] = mix.mux(plan["video"], out, Path(video_out).expanduser().resolve())
    if run_qa:
        sfx_count = sum(cue["bus"] == "sfx" for cue in placed)
        music_count = sum(cue["bus"] == "music" for cue in placed)
        result["qa"] = qa.analyze(stems, out, plan["words"], plan["profile"], sfx_count, music_count)
    write_json(Path(str(out) + ".manifest.json"), {
        **result, "plan": str(plan["path"]), "job": job,
        "inputs_sha256": {path: digest_file(path) for path in {job["narration"], *(cue["file"] for cue in placed)}}})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--out", type=Path, help="final mix: .wav, .mp3 or .m4a (stems go to <out>.stems/)")
    parser.add_argument("--video", type=Path, help="picture to score (overrides the plan's \"video\")")
    parser.add_argument("--video-out", type=Path, help="new MP4: the video's picture with this soundtrack")
    parser.add_argument("--dry-run", action="store_true", help="resolve anchors and estimate cost; no key, no requests")
    parser.add_argument("--assets-only", action="store_true", help="generate missing assets, then stop")
    parser.add_argument("--no-qa", action="store_true")
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args(argv)
    try:
        require_ffmpeg()
        plan = load_plan(args.plan)
        if args.video:
            plan["video"] = args.video.expanduser().resolve()
            if not plan["video"].is_file():
                raise AudioError(f"Video not found: {plan['video']}")
        if args.video_out and not plan["video"]:
            raise AudioError("--video-out needs a picture: set \"video\" in the plan or pass --video")
        specs = asset_specs(plan)
        if args.dry_run:
            return dry_run(plan, specs)
        files = ensure_assets(specs, lambda: resolve_key(args.env_file))
        if args.assets_only:
            print(json.dumps({name: str(path) for name, path in files.items()}, indent=2))
            return 0
        if not args.out:
            raise AudioError("--out is required to mix")
        result = build(plan, files, args.out, args.video_out, not args.no_qa, section_starts(specs, files))
        print("\n".join(result["cue_sheet"]), file=sys.stderr)
        for warning in result["warnings"]:
            print("WARN " + warning, file=sys.stderr)
        if "qa" in result:
            print(qa.summary(result["qa"]), file=sys.stderr)
        print(json.dumps({key: result[key] for key in ("output", "stems", "duration_seconds", "master") if key in result}
                         | ({"video": result["mux"]["video"]} if "mux" in result else {}), indent=2))
        return 0
    except USER_ERRORS as error:
        return fail(error)


def dry_run(plan, specs):
    length = film_length(plan)
    anchors = Anchors(plan["timeline"], plan["words"])
    missing = {name: spec for name, spec in specs.items() if spec["type"] != "file" and not _complete(spec["path"])}
    # Unrendered assets cannot be measured yet; stand in a long silent file so anchors and fields still validate.
    with tempfile.TemporaryDirectory(prefix="agent-audio-dry-") as folder:
        stand_in = Path(folder) / "stand-in.wav"
        if missing:
            mix.run_ffmpeg(["-y", "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono", "-t", "200", str(stand_in)], "stand-in")
        files = {name: (stand_in if name in missing else Path(spec["path"])) for name, spec in specs.items()}
        found = section_starts(specs, files, measure=False)
        cues, dips, warnings = resolve_cues(plan, files, length, anchors, found)
    for cue in cues:
        if cue["asset"] in missing:
            cue["duration"] = float("nan")  # unknown until generated
    print(cue_sheet(cues, dips).replace("   nans", "      ?"))
    for warning in warnings:
        if not any(name in warning for name in missing):
            print("WARN " + warning)
    print(json.dumps({"film_seconds": round(length, 2), "cues": len(cues), "dips": len(dips),
                      "assets_to_generate": sorted(missing), "estimated_cost_usd": estimate_cost(specs),
                      "sfx_per_minute": round(sum(c["bus"] == "sfx" for c in cues) / (length / 60), 1)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
