#!/usr/bin/env python3
"""Objective checks of a finished soundtrack against the reference ranges. Automated, not a listening test.

  qa.py --stems final.wav.stems --final final.wav [--words vo.words.json] [--profile long] [--out qa.json]

Reports loudness, how far music sits under the voice while words are spoken, the speech-to-floor depth measured
the same way as the references, effects that cover words, dead air, and effect/music density.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics

from common import AudioError, USER_ERRORS, decode_mono, fail, frame_levels, percentile, read_json, require_ffmpeg
from mix import integrated_lufs

RATE = 8000
WINDOW = 0.1
SILENT = -60.0
# Ranges from references/reference-study.md (20 reference videos, measured 2026-10-22).
TARGETS = {
    "long": {"bed_depth_db": (18.0, 25.0), "music_under_voice_db": (14.0, 26.0), "sfx_per_minute": (3.0, 9.0)},
    "short": {"bed_depth_db": (8.0, 13.5), "music_under_voice_db": (7.0, 16.0), "sfx_per_minute": (7.0, 22.0)},
}
LOUDNESS_TOLERANCE = 1.0
TRUE_PEAK_MAX = -0.8
MASK_MARGIN_DB = 2.0
MASK_MIN_SECONDS = 0.25  # a short transient on a word is normal; a sustained effect over words is not
DEAD_AIR_SECONDS = 2.0


def levels(path):
    return frame_levels(decode_mono(path, RATE), RATE, WINDOW) if Path(path).is_file() else []


def speech_frames(voice, words, count):
    if words:
        active = set()
        for word in words:
            active.update(range(int(word["start"] / WINDOW), int(word["end"] / WINDOW) + 1))
        return sorted(index for index in active if index < count)
    threshold = percentile(voice, 0.9) - 15
    return [index for index, level in enumerate(voice) if level > threshold]


def runs(indices):
    """Group sorted frame indices into (first, last) runs."""
    groups = []
    for index in indices:
        if groups and index == groups[-1][1] + 1:
            groups[-1][1] = index
        else:
            groups.append([index, index])
    return groups


def check(value, low, high):
    return "ok" if low <= value <= high else ("low" if value < low else "high")


def analyze(stems, final, words=None, profile="long", sfx_count=None, music_cues=None):
    stems = Path(stems)
    voice, music, ambience, sfx = (levels(stems / f"{name}.wav") for name in ("voice", "music", "ambience", "sfx"))
    count = min(len(voice), len(music) or len(voice), len(sfx) or len(voice))
    if not count:
        raise AudioError(f"No stems found in {stems}")
    speech = speech_frames(voice[:count], words, count)
    minutes = count * WINDOW / 60
    report = {"profile": profile, "duration_seconds": round(count * WINDOW, 1), "checks": {}, "warnings": []}

    loud = integrated_lufs(final)
    report["final"] = {"integrated_lufs": loud["I"], "lra_lu": loud.get("LRA"), "true_peak_dbfs": loud.get("Peak")}
    report["checks"]["loudness"] = "ok" if abs(loud["I"] + 14) <= LOUDNESS_TOLERANCE else "off-target"
    report["checks"]["true_peak"] = "ok" if loud.get("Peak", 0) <= TRUE_PEAK_MAX else "high"

    final_levels = levels(final)
    depth = percentile(final_levels, 0.9) - percentile(final_levels, 0.1)
    target = TARGETS[profile]
    # Informational: speech p90 minus floor p10, measured like the references. Dense narration with few pauses
    # lowers it without the music being louder, so music_under_voice (from stems) is the level check.
    report["bed_depth_db"] = round(depth, 1)
    report["bed_depth_reference_range"] = list(target["bed_depth_db"])

    under = [voice[index] - music[index] for index in speech if index < len(music) and music[index] > SILENT]
    if under:
        report["music_under_voice_db"] = round(statistics.median(under), 1)
        report["music_presence_during_speech"] = round(len(under) / max(1, len(speech)), 2)
        report["checks"]["music_level"] = check(report["music_under_voice_db"], *target["music_under_voice_db"])
    else:
        report["checks"]["music_level"] = "no music under speech"

    speech_set = set(speech)
    masked = [index for index in speech if index < len(sfx) and sfx[index] > voice[index] - MASK_MARGIN_DB]
    report["sfx_masking"] = [{"start": round(a * WINDOW, 2), "end": round((b + 1) * WINDOW, 2)}
                             for a, b in runs(masked) if (b - a + 1) * WINDOW >= MASK_MIN_SECONDS]
    if report["sfx_masking"]:
        report["warnings"].append(f"{len(report['sfx_masking'])} effect(s) as loud as the words; lower them or move "
                                  "them into a pause (times in sfx_masking).")

    beds = [max(values[index] if index < len(values) else -99 for values in (voice, music, ambience, sfx))
            for index in range(count)]
    quiet = [index for index, level in enumerate(beds) if level < SILENT and index not in speech_set]
    dead = [(a, b) for a, b in runs(quiet) if (b - a + 1) * WINDOW >= DEAD_AIR_SECONDS]
    report["dead_air"] = [{"start": round(a * WINDOW, 2), "end": round((b + 1) * WINDOW, 2)} for a, b in dead]
    if dead:
        report["warnings"].append("Silence of 2 s or more; intended only for a deliberate beat.")

    if sfx_count is not None:
        density = sfx_count / minutes
        report["sfx_per_minute"] = round(density, 1)
        report["checks"]["sfx_density"] = check(density, *target["sfx_per_minute"])
    if music_cues is not None:
        report["music_cues_per_minute"] = round(music_cues / minutes, 2)
    for name, state in report["checks"].items():
        if state != "ok":
            report["warnings"].append(f"{name}: {state}")
    report["note"] = "Automated measurements. They catch level and timing faults; taste still needs a listen."
    return report


def summary(report):
    lines = [f"QA ({report['profile']}): {report['final']['integrated_lufs']} LUFS, peak {report['final']['true_peak_dbfs']} dBFS, "
             f"bed depth {report['bed_depth_db']} dB, music under voice {report.get('music_under_voice_db', '-')} dB, "
             f"sfx/min {report.get('sfx_per_minute', '-')}"]
    lines += [f"  WARN {warning}" for warning in report["warnings"]] or ["  all checks ok"]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stems", type=Path, required=True)
    parser.add_argument("--final", type=Path, required=True)
    parser.add_argument("--words", type=Path)
    parser.add_argument("--profile", choices=tuple(TARGETS), default="long")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        require_ffmpeg()
        words = read_json(args.words)["words"] if args.words else None
        report = analyze(args.stems, args.final, words, args.profile)
        if args.out:
            args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(summary(report))
        return 0
    except USER_ERRORS as error:
        return fail(error)


if __name__ == "__main__":
    raise SystemExit(main())
