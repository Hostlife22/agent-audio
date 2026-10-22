"""Shared helpers for agent-audio: credentials, OpenRouter HTTP, JSON state, ffmpeg/ffprobe.

Standard library only. Every paid request goes through `post`; nothing here retries a paid call.
"""
from __future__ import annotations

import array
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
API = "https://openrouter.ai/api/v1"
APP_TITLE = "agent-audio"
KEY_PATTERN = r"sk-or-v1-[A-Za-z0-9_-]+"
_LOADED_KEYS = set()  # every key resolve_key returned; redact() strips them whatever their format


class AudioError(Exception):
    """A user-facing failure; the message is safe to print."""


# ---------- JSON state ----------

def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as error:
        raise AudioError(f"Invalid JSON in {path}: {error}") from None


def write_json(path, data):
    """Atomic write so an interrupted run never leaves half a manifest."""
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_file(path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            hasher.update(block)
    return hasher.hexdigest()


def fingerprint(value) -> str:
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8"))


# ---------- credentials ----------

def redact(message, key=""):
    message = str(message)
    for secret in {key, *_LOADED_KEYS} - {""}:
        message = message.replace(secret, "[REDACTED]")
    return re.sub(KEY_PATTERN, "[REDACTED]", message)


def key_from_file(path):
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if re.fullmatch(KEY_PATTERN, line):
            return line  # A file containing only the key.
        match = re.match(r"(?:export\s+)?OPENROUTER_API_KEY\s*=\s*(.*)$", line)
        if not match:
            continue
        value = match.group(1).strip()
        if value.startswith(("'", '"')):
            end = value.find(value[0], 1)
            if end < 0:
                raise AudioError(f"Unclosed quote in {path}")
            value = value[1:end]
        else:
            value = value.split(" #", 1)[0].strip()
        if value:
            return value
    return None


def resolve_key(env_file=None):
    """OPENROUTER_API_KEY, then the skill's .env; an explicit --env-file is the only source when given."""
    key = _find_key(env_file)
    # http.client would echo a bad header value (the key) in its error message, so reject it here instead.
    if not re.fullmatch(r"[\x21-\x7e]+", key):
        raise AudioError("The OpenRouter key contains spaces or non-printable characters; check the .env file.")
    _LOADED_KEYS.add(key)
    return key


def _find_key(env_file):
    if env_file:
        path = Path(env_file).expanduser().resolve()
        if not path.is_file():
            raise AudioError(f"Key file not found: {path}")
        key = key_from_file(path)
        if not key:
            raise AudioError(f"No OPENROUTER_API_KEY in {path}")
        return key
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    path = ROOT / ".env"
    if path.is_file():
        key = key_from_file(path)
        if key:
            return key
    raise AudioError("Set OPENROUTER_API_KEY, create agent-audio/.env, or pass --env-file.")


# ---------- OpenRouter HTTP ----------

def _request(route, payload, key, title):
    return urllib.request.Request(
        API + route, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST",
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                 "X-Title": title, "HTTP-Referer": "https://github.com/agent-audio"},
    )


def _http_failure(error, key):
    if isinstance(error, urllib.error.HTTPError):
        detail = redact(error.read(3000).decode("utf-8", errors="replace"), key)
        return AudioError(f"OpenRouter HTTP {error.code}: {detail}. No automatic retry was made.")
    return AudioError(f"OpenRouter request interrupted: {redact(error, key)}. "
                      "It may have been billed; no automatic retry was made.")


NETWORK_ERRORS = (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError)


def post(route, payload, key, timeout, title=APP_TITLE):
    """POST JSON, return (body bytes, response metadata). Never retries."""
    try:
        with urllib.request.urlopen(_request(route, payload, key, title), timeout=timeout) as response:
            body = response.read()
            meta = {"content_type": response.headers.get("Content-Type", "").lower(),
                    "generation_id": response.headers.get("X-Generation-Id")}
            return body, meta
    except NETWORK_ERRORS as error:
        raise _http_failure(error, key) from None


def post_json(route, payload, key, timeout, title=APP_TITLE):
    body, _ = post(route, payload, key, timeout, title)
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise AudioError(f"Expected JSON from {route}; got {len(body)} bytes.") from None


def post_stream(route, payload, key, timeout, consume, title=APP_TITLE):
    """POST and feed each SSE `data:` event (already joined) to `consume(text)`. Returns True on [DONE]."""
    try:
        with urllib.request.urlopen(_request(route, payload, key, title), timeout=timeout) as response:
            if "text/event-stream" not in response.headers.get("Content-Type", ""):
                raise AudioError("Expected an SSE stream; nothing was saved.")
            return parse_sse(response, consume)
    except NETWORK_ERRORS as error:
        raise _http_failure(error, key) from None


def parse_sse(lines, consume):
    """Join multi-line `data:` fields per event; stop at [DONE]. Returns whether [DONE] arrived."""
    pending = []
    for line in lines:
        line = (line.decode("utf-8") if isinstance(line, bytes) else line).rstrip("\r\n")
        if line.startswith("data:"):
            pending.append(line[5:].lstrip(" "))
            continue
        if line or not pending:
            continue
        data, pending = "\n".join(pending), []
        if data.strip() == "[DONE]":
            return True
        consume(data)
    if pending:
        data = "\n".join(pending)
        if data.strip() == "[DONE]":
            return True
        consume(data)
    return False


def looks_like_mp3(body: bytes) -> bool:
    if len(body) < 128:
        return False
    return body.startswith(b"ID3") or (body[0] == 0xFF and body[1] & 0xE0 == 0xE0)


# ---------- ffmpeg ----------

def require_ffmpeg():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise AudioError("Install ffmpeg and ffprobe (for example `brew install ffmpeg`).")


def run_ffmpeg(arguments, what="ffmpeg", loglevel="error"):
    process = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-v", loglevel, *arguments],
                             capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if process.returncode:
        raise AudioError(f"{what} failed: {process.stderr.strip()[-1500:]}")
    return process


def audio_info(path):
    """Codec, rate, channels and decoded duration of the first audio stream."""
    process = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name,sample_rate,channels:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True)
    try:
        data = json.loads(process.stdout) if not process.returncode else None
    except json.JSONDecodeError:
        data = None
    if data is None:
        raise AudioError(f"ffprobe could not read {path}")
    if not data.get("streams"):
        raise AudioError(f"No audio stream: {path}")
    try:
        seconds = float(data.get("format", {}).get("duration") or 0)
    except (TypeError, ValueError):
        seconds = 0.0
    if not math.isfinite(seconds) or seconds <= 0:
        raise AudioError(f"Invalid audio duration: {path}")
    return {**data["streams"][0], "duration_seconds": round(seconds, 3)}


def media_duration(path):
    process = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                             capture_output=True, text=True)
    try:
        seconds = float(json.loads(process.stdout)["format"]["duration"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        raise AudioError(f"ffprobe could not read a duration from {path}") from None
    if process.returncode or not math.isfinite(seconds) or seconds <= 0:
        raise AudioError(f"ffprobe could not read {path}")
    return seconds


def decode_mono(path, rate=16000, start=None, duration=None):
    """Decode to mono 16-bit samples (array 'h') for local measurements."""
    command = ["ffmpeg", "-nostdin", "-v", "error"]
    if start is not None:
        command += ["-ss", str(start)]
    command += ["-i", str(path)]
    if duration is not None:
        command += ["-t", str(duration)]
    command += ["-vn", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"]
    process = subprocess.run(command, capture_output=True, stdin=subprocess.DEVNULL)
    if process.returncode:
        raise AudioError(f"Could not decode {path}: {process.stderr.decode(errors='replace')[-500:]}")
    samples = array.array("h")
    samples.frombytes(process.stdout[: len(process.stdout) // 2 * 2])
    return samples


def frame_levels(samples, rate, window=0.05):
    """RMS level in dBFS for consecutive windows."""
    size = max(1, int(rate * window))
    levels = []
    for start in range(0, len(samples) - size + 1, size):
        block = samples[start:start + size]
        power = sum(value * value for value in block) / size
        levels.append(10 * math.log10(power / 32768 ** 2 + 1e-12))
    return levels


def percentile(values, fraction):
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    return ordered[min(len(ordered) - 1, max(0, int(len(ordered) * fraction)))]


USER_ERRORS = (AudioError, OSError, ValueError, KeyError, TypeError, AttributeError, IndexError)


def fail(error, key=""):
    """Print one redacted line for any error a CLI catches; unexpected types point at the input."""
    prefix = "Error: " if isinstance(error, (AudioError, OSError)) else f"Error ({type(error).__name__}, check the input file): "
    print(prefix + redact(error, key), file=sys.stderr)
    return 1
