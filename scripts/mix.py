"""Mixing engine: level-matched buses, ducking, music dips, stems, mastering and video mux.

Used by soundtrack.py. Levels are relative: every asset is measured and placed against the narration using a
profile measured on the reference channel (references/reference-study.md), then `gain_db` trims from there.
"""
from __future__ import annotations

from pathlib import Path
import re

from common import AudioError, audio_info, decode_mono, digest_file, frame_levels, media_duration, read_json, run_ffmpeg, write_json

RATE = 48000
VOICE_LUFS = -16.0
# Offsets relative to the narration's integrated loudness. Music/ambience: integrated LUFS of the asset.
# SFX: loudest 100 ms RMS of the effect. Calibrated so the finished mix lands near the references'
# speech-to-floor depth (about 20 dB in long videos, about 10 dB in shorts).
PROFILES = {
    "long": {"music": -14.0, "ambience": -22.0, "sfx": -7.0, "duck_ratio": 2.5, "target_lufs": -14.0},
    "short": {"music": -11.0, "ambience": -20.0, "sfx": -4.0, "duck_ratio": 1.8, "target_lufs": -14.0},
}
TRUE_PEAK_LIMIT = 10 ** (-1.5 / 20)  # sample-peak ceiling; lands near -1 dBTP after encoding
BUSES = ("music", "ambience", "sfx")


# ---------- measurement (cached by file hash) ----------

def integrated_lufs(path):
    process = run_ffmpeg(["-nostats", "-i", str(path), "-vn", "-af", "ebur128=peak=true", "-f", "null", "-"], "loudness", "info")
    summary = {}
    for line in process.stderr.splitlines():
        match = re.match(r"\s+(I|LRA|Peak):\s+(-?[\d.]+|-inf)", line)
        if match:
            summary[match.group(1)] = float(match.group(2)) if match.group(2) != "-inf" else -99.0
    if "I" not in summary:
        raise AudioError(f"Could not measure loudness of {path}")
    return summary


def peak_rms(path):
    levels = frame_levels(decode_mono(path, 16000), 16000, 0.1)
    return max(levels) if levels else -99.0


class Meter:
    """Measures files once per content hash; results persist in a JSON cache beside the plan."""

    def __init__(self, cache_path):
        self.cache_path = Path(cache_path)
        self.cache = read_json(self.cache_path) if self.cache_path.exists() else {}

    def measure(self, path, how):
        key = f"{digest_file(path)}:{how}"
        if key not in self.cache:
            self.cache[key] = integrated_lufs(path)["I"] if how == "lufs" else peak_rms(path)
            write_json(self.cache_path, self.cache)
        return self.cache[key]


def placement_gain(meter, cue, profile, voice_lufs):
    """dB to apply so the asset sits at the profile level, plus the cue's own trim."""
    offset = PROFILES[profile][cue["bus"]]
    how = "peak" if cue["bus"] == "sfx" else "lufs"
    level = meter.measure(cue["file"], how)
    if level < -70:
        raise AudioError(f"{cue['file']} is silent; regenerate it.")
    return round(voice_lufs + offset - level + cue.get("gain_db", 0.0), 2)


# ---------- filter graph ----------

def dip_expression(dips):
    """Volume envelope: 1 outside dips, 10^(gain/20) inside, linear ramps at the edges."""
    terms = []
    for dip in dips:
        depth = 1 - 10 ** (dip["gain_db"] / 20)
        a, b = dip["start"], dip["end"]
        ramp_in, ramp_out = max(dip["ramp_in"], 0.005), max(dip["ramp_out"], 0.005)
        rise = f"(t-({a - ramp_in:.4f}))/{ramp_in:.4f}"
        fall = f"(({b + ramp_out:.4f})-t)/{ramp_out:.4f}"
        terms.append(f"(1-{depth:.5f}*clip(min({rise},{fall}),0,1))")
    return "*".join(terms)


def fit(length):
    """Pad or cut to the film length by sample count; timestamp-based atrim drifts after adelay in ffmpeg 9."""
    return f"asetpts=N/SR/TB,apad=whole_dur={length:.4f},atrim=end_sample={round(length * RATE)}"


def input_files(cues):
    """Unique (file, loop) inputs in first-use order; ffmpeg opens each once (macOS allows 256 open files)."""
    order = []
    for cue in cues:
        key = (cue["file"], bool(cue.get("loop")))
        if key not in order:
            order.append(key)
    return order


def cue_chain(index, cue, length, source_index, split_label=None):
    duration = cue["duration"]
    fade_out_start = max(0.0, duration - cue["fade_out"])
    source = f"[{split_label}]" if split_label else f"[{source_index}:a]"
    return (f"{source}aresample={RATE},aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"atrim=start={cue['source_start']:.4f}:duration={duration:.4f},asetpts=PTS-STARTPTS,"
            f"volume={cue['placed_gain_db']}dB,afade=t=in:d={cue['fade_in']:.4f},"
            f"afade=t=out:st={fade_out_start:.4f}:d={cue['fade_out']:.4f},"
            f"adelay={round(cue['at'] * RATE)}S:all=1,{fit(length)}[c{index}]")


def silent_bus(label, length):
    return f"anullsrc=r={RATE}:cl=stereo,atrim=end_sample={round(length * RATE)}[{label}]"


def filter_graph(job):
    """Graph with outputs [voice] [music] [ambience] [sfx] [premix]; input 0 is the narration."""
    length = job["duration_seconds"]
    profile = PROFILES[job["profile"]]
    voice = (f"[0:a]aresample={RATE},aformat=sample_fmts=fltp:channel_layouts=stereo,"
             f"atrim=duration={length:.4f},asetpts=PTS-STARTPTS,highpass=f=70,volume={job['voice_gain_db']}dB")
    if job.get("voice_compression", True):
        voice += ",acompressor=threshold=0.125:ratio=2.5:attack=8:release=150:makeup=1.6"
    lines = [voice + f",{fit(length)},asplit=3[voice][vkey][vmix]"]
    groups = {bus: [] for bus in BUSES}
    inputs = input_files(job["cues"])
    users = {key: [] for key in inputs}
    for index, cue in enumerate(job["cues"], start=1):
        users[(cue["file"], bool(cue.get("loop")))].append(index)
    split = {}
    for number, key in enumerate(inputs, start=1):
        if len(users[key]) > 1:  # one decoded input feeding several cues
            labels = [f"s{number}_{n}" for n in range(len(users[key]))]
            lines.append(f"[{number}:a]asplit={len(labels)}" + "".join(f"[{label}]" for label in labels))
            split.update(zip(users[key], labels))
    for index, cue in enumerate(job["cues"], start=1):
        source_index = inputs.index((cue["file"], bool(cue.get("loop")))) + 1
        lines.append(cue_chain(index, cue, length, source_index, split.get(index)))
        groups[cue["bus"]].append(f"c{index}")
    for bus in BUSES:
        labels = groups[bus]
        raw = f"{bus}_raw"
        if labels:
            lines.append("".join(f"[{label}]" for label in labels) +
                         f"amix=inputs={len(labels)}:normalize=0:duration=longest,{fit(length)}[{raw}]")
        else:
            lines.append(silent_bus(raw, length))
    dips = dip_expression(job["dips"]) if job["dips"] else None
    lines.append("[vkey]asplit=2[key_music][key_ambience]" if job["duck"] else "[vkey]anullsink")
    for bus in ("music", "ambience"):
        chain = f"[{bus}_raw]" + (f"volume='{dips}':eval=frame" if dips else "anull")
        if not job["duck"]:
            lines.append(chain + f",asplit=2[{bus}][{bus}_mix]")
            continue
        ratio = profile["duck_ratio"] if bus == "music" else max(1.2, profile["duck_ratio"] - 1)
        lines.append(chain + f"[{bus}_pre]")
        lines.append(f"[{bus}_pre][key_{bus}]sidechaincompress=threshold=0.05:ratio={ratio}:attack=25:"
                     f"release=400:knee=4:makeup=1,asplit=2[{bus}][{bus}_mix]")
    lines.append("[sfx_raw]asplit=2[sfx][sfx_mix]")
    lines.append("[vmix][music_mix][ambience_mix][sfx_mix]amix=inputs=4:normalize=0:duration=longest,"
                 f"{fit(length)}[premix]")
    return ";\n".join(lines)


def render_premix(job, folder):
    """Write stems and the unmastered premix (24-bit WAV) into `folder`."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    command = ["-y", "-i", job["narration"]]
    for path, loop in input_files(job["cues"]):
        if loop:
            command += ["-stream_loop", "-1"]
        command += ["-i", path]
    graph = filter_graph(job)
    (folder / "graph.txt").write_text(graph, encoding="utf-8")  # kept for debugging
    command += ["-filter_complex", graph.replace(";\n", ";")]
    outputs = {}
    for label in ("voice", "music", "ambience", "sfx", "premix"):
        outputs[label] = folder / f"{label}.wav"
        command += ["-map", f"[{label}]", "-ar", str(RATE), "-c:a", "pcm_s24le", str(outputs[label])]
    run_ffmpeg(command, "mix")
    return outputs


# ---------- mastering and delivery ----------

def master(premix, output, target_lufs):
    """Gain to target loudness, then a lookahead limiter; one corrective pass if limiting pulled it down."""
    gain = target_lufs - integrated_lufs(premix)["I"]
    for _ in range(2):
        _encode(premix, output, f"volume={gain:.3f}dB,alimiter=limit={TRUE_PEAK_LIMIT:.5f}:level=false:latency=true")
        measured = integrated_lufs(output)
        miss = target_lufs - measured["I"]
        if abs(miss) <= 0.5:
            break
        gain += miss
    return {"integrated_lufs": measured["I"], "lra_lu": measured.get("LRA"), "true_peak_dbfs": measured.get("Peak"),
            "master_gain_db": round(gain, 2)}


def _encode(source, output, filters):
    output = Path(output)
    suffix = output.suffix.lower()
    codecs = {".wav": ["-c:a", "pcm_s24le"], ".mp3": ["-c:a", "libmp3lame", "-b:a", "256k"],
              ".m4a": ["-c:a", "aac", "-b:a", "256k"]}
    if suffix not in codecs:
        raise AudioError("Final audio must be .wav, .mp3 or .m4a")
    run_ffmpeg(["-y", "-i", str(source), "-af", filters, "-ar", str(RATE), *codecs[suffix], str(output)], "mastering")


def mux(video, audio, output):
    """Replace the video's audio with the soundtrack; the picture is copied, never re-encoded."""
    video_length = media_duration(video)
    output = Path(output)
    if output.resolve() == Path(video).resolve():
        raise AudioError("The muxed video must be a new file, not the source video.")
    temporary = output.with_name(output.stem + ".muxing" + output.suffix)
    run_ffmpeg(["-y", "-i", str(video), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
                "-af", "apad", "-c:a", "aac", "-b:a", "256k", "-t", f"{video_length:.3f}",
                "-movflags", "+faststart", str(temporary)], "mux")
    temporary.replace(output)
    return {"video": str(output), "video_seconds": round(video_length, 3), "audio_seconds": audio_info(audio)["duration_seconds"]}
