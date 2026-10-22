#!/usr/bin/env python3
"""Read a toon-explainer spec and lay out what the soundtrack has to follow.

  timeline.py events film/spec.json [--words vo.words.json] [--script film/script.md] --out timeline.json --md timeline.md
  timeline.py beats  film/spec.json --out narration.json        # narration plan (one beat per shot) for speak.py

`events` lists every shot with absolute start/end, voiceover, grade/background changes and the on-screen events
(transitions, punch-zooms, shakes, stamps, flashes, counters, entrances...) at absolute times, each with a
sound suggestion from references/event-sounds.json. With --words it adds when speech actually runs in each shot and
the pauses where a stinger or a music stop can land. With --script it adds the director's sound plan from the
storyboard: each shot's "- Sound:" line, and a "Music map:" block written above the first shot. No network.
"""
from __future__ import annotations

import argparse
import difflib
import json
from pathlib import Path
import re
import sys

from common import AudioError, ROOT, USER_ERRORS, fail, read_json, write_json

EVENT_TABLE = ROOT / "references" / "event-sounds.json"
SHOT_LINE = re.compile(r"^\s*\**\s*Shot\s*(\d+)\b", re.IGNORECASE)
SOUND_LINE = re.compile(r"^\s*[\u2014\u2013\-*]?\s*Sound\s*:\s*(.+)$", re.IGNORECASE)
BRIEF_LINE = re.compile(r"^\s*(?:Music|Sound)(?:\s+(?:map|plan))?\s*:", re.IGNORECASE)
MIN_GAP_SECONDS = 0.6
TIER_ORDER = "ABC"


def load_spec(path):
    spec = read_json(path)
    shots = spec.get("shots") if isinstance(spec, dict) else None
    if not isinstance(shots, list) or not shots:
        raise AudioError(f"{path} is not a toon-explainer spec (no shots).")
    for index, shot in enumerate(shots, 1):
        label = f"shot {index} ({shot.get('id') if isinstance(shot, dict) else '?'})"
        if not isinstance(shot, dict) or not isinstance(shot.get("dur"), (int, float)) or shot["dur"] < 0:
            raise AudioError(f"{label}: every shot needs a numeric dur")
        for field, kind in (("layers", list), ("pinned", list), ("camera", dict), ("vo", str)):
            if shot.get(field) is not None and not isinstance(shot[field], kind):
                raise AudioError(f"{label}: {field} must be a {kind.__name__}")
    return spec


def first_value(value):
    """Animatable fields are a constant or [{at, v}]; take the first value for a label."""
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0].get("v")
    return value


# ---------- event extraction ----------

def event(kind, local, detail="", duration=None):
    item = {"kind": kind, "local": round(float(local), 3)}
    if detail:
        item["detail"] = detail
    if duration:
        item["duration"] = round(float(duration), 3)
    return item


def transition_events(shot):
    transition = shot.get("transition")
    if isinstance(transition, str):
        transition = {"kind": transition}
    if not isinstance(transition, dict) or transition.get("kind") in (None, "cut"):
        return []
    detail = transition["kind"] + (f" {transition['dir']}" if transition.get("dir") else "")
    return [event("transition:" + transition["kind"], 0, detail, transition.get("dur"))]


def camera_events(shot):
    camera = shot.get("camera") or {}
    found = [event("camera:punch", item.get("at", 0), f"amount {item.get('amount', '')}")
             for item in camera.get("punch") or [] if isinstance(item, dict)]
    found += [event("camera:shake", item.get("at", 0), f"amp {item.get('amp', '')}", item.get("dur"))
              for item in camera.get("shake") or [] if isinstance(item, dict)]
    return found


def layer_label(layer):
    text = layer.get("text") or layer.get("who") or layer.get("id") or ""
    return str(text)[:40]


def layer_events(layer):
    kind, sub = layer.get("type"), layer.get("kind")
    at = layer.get("at", 0) if isinstance(layer.get("at", 0), (int, float)) else 0
    found = []
    if kind == "fx" and sub:
        found.append(event("fx:" + sub, at, "", layer.get("dur")))
    elif kind == "doc" and sub == "stamp":
        found.append(event("stamp", at, layer_label(layer)))
    elif kind in ("date", "title", "card", "label", "counter", "map", "vehicle", "crowd", "creature"):
        detail = layer_label(layer) or (sub or "")
        if kind == "counter":
            detail = f"{layer.get('from', '')}->{layer.get('to', '')}{layer.get('suffix', '')}"
        found.append(event(kind + (":" + sub if kind in ("vehicle", "creature") and sub else ""), at, detail, layer.get("dur")))
    enter = layer.get("enter")
    if enter not in (None, "none") and kind not in ("date", "title", "card", "label", "counter"):
        found.append(event("enter:" + str(enter), at, f"{kind}{':' + sub if sub else ''} {layer_label(layer)}".strip()))
    for take in layer.get("takes") or []:
        if isinstance(take, dict):
            found.append(event("take:" + str(take.get("kind", "")), take.get("at", 0), layer_label(layer)))
    for jump in layer.get("jump") or []:
        if isinstance(jump, dict):
            found.append(event("jump", jump.get("at", 0), layer_label(layer)))
    return found


def shot_events(shot):
    found = transition_events(shot) + camera_events(shot)
    for layer in (shot.get("layers") or []) + (shot.get("pinned") or []):
        if isinstance(layer, dict):
            found += layer_events(layer)
    grade = shot.get("grade")
    if isinstance(grade, dict) and grade.get("at"):
        found.append(event("grade-change", grade["at"], str(grade.get("preset", ""))))
    return sorted(found, key=lambda item: item["local"])


def suggestion_for(kind, table):
    entry = table.get(kind) or table.get(kind.split(":", 1)[0] + ":*") or table.get(kind.split(":", 1)[0])
    return entry or {"tier": "C", "sound": ""}


# ---------- director's sound plan (storyboard) ----------

def sound_plan(text):
    """Storyboard -> {"shots": {kNN: text}, "brief": [lines]}.

    "- Sound:" lines belong to the shot above them (Shot N -> kNN, the id script2spec gives it). A "Music map:"
    or "Sound plan:" block before the first shot is the brief: its heading and following non-blank lines.
    """
    shots, brief, current, in_brief = {}, [], None, False
    for raw in text.splitlines():
        line = raw.strip()
        match = SHOT_LINE.match(line)
        if match:
            current, in_brief = f"k{int(match.group(1)):02d}", False
            continue
        if current is None:
            if BRIEF_LINE.match(line):
                in_brief = True
            elif not line:
                in_brief = False
            if in_brief and line:
                brief.append(line)
            continue
        match = SOUND_LINE.match(line)
        if match:
            shots[current] = "; ".join(filter(None, [shots.get(current), match.group(1).strip()]))
    return {"shots": shots, "brief": brief}


# ---------- speech alignment ----------

def normalise(word):
    return re.sub(r"[^a-z0-9']+", "", word.lower().replace("’", "'"))


def speech_spans(shots, words):
    """Per shot: first and last spoken word time, found by aligning shot `vo` text to the word timestamps."""
    script, owner = [], []
    for index, shot in enumerate(shots):
        for token in str(shot.get("vo", "")).split():
            if normalise(token):
                script.append(normalise(token))
                owner.append(index)
    heard = [normalise(item["word"]) for item in words]
    spans = {}
    matcher = difflib.SequenceMatcher(None, script, heard, autojunk=False)
    for a, b, size in matcher.get_matching_blocks():
        for offset in range(size):
            index, word = owner[a + offset], words[b + offset]
            first, last = spans.get(index, (word["start"], word["end"]))
            spans[index] = (min(first, word["start"]), max(last, word["end"]))
    return spans


def speech_gaps(words, minimum=MIN_GAP_SECONDS):
    return [{"start": round(left["end"], 3), "end": round(right["start"], 3),
             "duration": round(right["start"] - left["end"], 3)}
            for left, right in zip(words, words[1:]) if right["start"] - left["end"] >= minimum]


# ---------- build ----------

def build_timeline(spec, table, words=None, sound=None):
    shots, start = [], 0.0
    spans = speech_spans(spec["shots"], words) if words else {}
    for index, shot in enumerate(spec["shots"]):
        duration = float(shot.get("dur", 0))
        bg = shot.get("bg")
        if isinstance(bg, list):
            bg_kind = ",".join(str(item.get("kind")) for item in bg if isinstance(item, dict))
        else:
            bg_kind = bg.get("kind") if isinstance(bg, dict) else bg
        grade = shot.get("grade")
        entry = {"index": index + 1, "id": shot.get("id", f"#{index + 1}"), "start": round(start, 3),
                 "end": round(start + duration, 3), "dur": round(duration, 3), "vo": shot.get("vo", ""),
                 "bg": bg_kind, "grade": grade.get("preset") if isinstance(grade, dict) else first_value(grade),
                 "events": []}
        for item in shot_events(shot):
            hint = suggestion_for(item["kind"], table)
            entry["events"].append({**item, "time": round(start + item["local"], 3), "tier": hint.get("tier", "C"),
                                    "sound": hint.get("sound", "")})
        if index in spans:
            entry["speech"] = [round(spans[index][0], 3), round(spans[index][1], 3)]
        if sound and entry["id"] in sound["shots"]:
            entry["sound_plan"] = sound["shots"][entry["id"]]
        shots.append(entry)
        start += duration
    result = {"title": spec.get("title", ""), "fps": spec.get("fps"), "format": spec.get("format", "landscape"),
              "duration_seconds": round(start, 3), "shots": shots}
    if words:
        result["speech_gaps"] = speech_gaps(words)
    if sound and (sound["shots"] or sound["brief"]):
        ids = {shot["id"] for shot in shots}
        result["sound_brief"] = sound["brief"]
        result["sound_plan_unmatched"] = sorted(set(sound["shots"]) - ids)
    return result


def scene_changes(timeline):
    """Shots where background or grade changes: candidate music section boundaries."""
    changes, previous = [], None
    for shot in timeline["shots"]:
        key = (shot["bg"], shot["grade"])
        if key != previous:
            changes.append(shot["id"])
        previous = key
    return changes


def clock(seconds):
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)}:{rest:05.2f}"


def to_markdown(timeline, tiers):
    changes = set(scene_changes(timeline))
    lines = [f"# Timeline: {timeline['title']}", "",
             f"Duration {clock(timeline['duration_seconds'])} ({timeline['duration_seconds']} s), "
             f"{len(timeline['shots'])} shots, format {timeline['format']}. Times are absolute seconds. "
             f"Event tiers shown: {tiers}. `*` marks a background/grade change (music section candidate).", ""]
    if timeline.get("sound_brief"):
        lines += ["## Director's music map (storyboard)", "", *timeline["sound_brief"], ""]
    if timeline.get("sound_plan_unmatched"):
        lines += [f"Sound lines for shots missing from the spec: {', '.join(timeline['sound_plan_unmatched'])}", ""]
    for shot in timeline["shots"]:
        marker = "*" if shot["id"] in changes else " "
        speech = f" speech {shot['speech'][0]:.2f}-{shot['speech'][1]:.2f}" if "speech" in shot else ""
        lines.append(f"{marker}{shot['id']} {shot['start']:.2f}-{shot['end']:.2f} ({shot['dur']:.2f}s) "
                     f"bg={shot['bg']} grade={shot['grade']}{speech}")
        if shot["vo"]:
            lines.append(f"    VO: {shot['vo']}")
        if shot.get("sound_plan"):
            lines.append(f"    SOUND PLAN: {shot['sound_plan']}")
        for item in shot["events"]:
            if item["tier"] not in tiers:
                continue
            extra = f" {item['detail']}" if item.get("detail") else ""
            span = f" ({item['duration']}s)" if item.get("duration") else ""
            hint = f" -> {item['sound']}" if item["sound"] else ""
            lines.append(f"    {item['time']:8.2f} [{item['tier']}] {item['kind']}{extra}{span}{hint}")
    if timeline.get("speech_gaps"):
        lines += ["", f"## Speech pauses >= {MIN_GAP_SECONDS}s (stinger / music-stop slots)", ""]
        lines += [f"- {gap['start']:.2f}-{gap['end']:.2f} ({gap['duration']:.2f}s)" for gap in timeline["speech_gaps"]]
    return "\n".join(lines) + "\n"


def narration_plan(spec, mode):
    beats = [{"shot": shot.get("id", f"#{index + 1}"), "mode": mode, "text": shot["vo"].strip()}
             for index, shot in enumerate(spec["shots"]) if str(shot.get("vo", "")).strip()]
    if not beats:
        raise AudioError("The spec has no `vo` text on any shot.")
    return {"source": "toon-explainer spec; add Fish tags and per-beat modes before rendering", "beats": beats}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("events", "beats"))
    parser.add_argument("spec", type=Path)
    parser.add_argument("--words", type=Path, help="transcribe.py output for speech spans and pauses")
    parser.add_argument("--script", type=Path, help="events: storyboard with '- Sound:' lines and a 'Music map:' brief")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--md", type=Path, help="events: also write a compact Markdown timeline")
    parser.add_argument("--tiers", default="AB", help="events: tiers shown in Markdown (A strong, B medium, C minor)")
    parser.add_argument("--mode", default="ironic", help="beats: default delivery mode")
    args = parser.parse_args(argv)
    try:
        spec = load_spec(args.spec)
        if args.command == "beats":
            result = narration_plan(spec, args.mode)
        else:
            words = read_json(args.words)["words"] if args.words else None
            sound = sound_plan(args.script.read_text(encoding="utf-8-sig")) if args.script else None
            result = build_timeline(spec, read_json(EVENT_TABLE)["events"], words, sound)
            if args.md:
                args.md.write_text(to_markdown(result, args.tiers.upper()), encoding="utf-8")
        if args.out:
            write_json(args.out, result)
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == "events":
            counts = {tier: sum(item["tier"] == tier for shot in result["shots"] for item in shot["events"]) for tier in TIER_ORDER}
            print(f"{len(result['shots'])} shots, {result['duration_seconds']} s, events by tier {counts}", file=sys.stderr)
        return 0
    except USER_ERRORS as error:
        return fail(error)


if __name__ == "__main__":
    raise SystemExit(main())
