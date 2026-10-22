"""Find where a multi-section music track really changes, near the times its prompt asked for.

Lyria Pro follows "[0:25 - 0:40] ..." sections, but each boundary lands a second or two off. A cue that reads one
section should start where the music actually turns, so each requested boundary is moved to the strongest change
(loudness and brightness before vs after) within a few seconds of it.
"""
from __future__ import annotations

import math

from common import decode_mono

RATE = 8000
FRAME = 0.1
HALF_WINDOW = 15   # frames compared on each side (1.5 s)
SEARCH = 3.0       # seconds either side of the requested boundary


def features(samples):
    """Per 100 ms frame: RMS level (dB) and zero-crossing rate (a brightness proxy)."""
    size = int(RATE * FRAME)
    rows = []
    for start in range(0, len(samples) - size + 1, size):
        block = samples[start:start + size]
        power = sum(value * value for value in block) / size
        crossings = sum(1 for a, b in zip(block, block[1:]) if (a < 0) != (b < 0)) / size
        rows.append((10 * math.log10(power / 32768 ** 2 + 1e-12), crossings))
    return rows


def novelty(rows, index, scale):
    before, after = rows[max(0, index - HALF_WINDOW):index], rows[index:index + HALF_WINDOW]
    if not before or not after:
        return 0.0
    total = 0.0
    for dimension, spread in enumerate(scale):
        mean_before = sum(row[dimension] for row in before) / len(before)
        mean_after = sum(row[dimension] for row in after) / len(after)
        total += ((mean_after - mean_before) / spread) ** 2
    return math.sqrt(total)


def refine_rows(rows, requested, search=SEARCH):
    """Requested section starts (s) -> measured starts (s) with a strength score; 0 stays 0."""
    if not rows:
        return {name: {"requested": at, "start": at, "strength": 0.0} for name, at in requested.items()}
    scale = []
    for dimension in range(len(rows[0])):
        values = [row[dimension] for row in rows]
        mean = sum(values) / len(values)
        scale.append(max(1e-6, math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))))
    result = {}
    for name, at in requested.items():
        if at <= 0:
            result[name] = {"requested": at, "start": 0.0, "strength": 0.0}
            continue
        low = max(1, int((at - search) / FRAME))
        high = min(len(rows) - 1, int((at + search) / FRAME))
        if low > high:
            result[name] = {"requested": at, "start": at, "strength": 0.0}
            continue
        best = max(range(low, high + 1), key=lambda index: novelty(rows, index, scale))
        result[name] = {"requested": at, "start": round(best * FRAME, 2), "strength": round(novelty(rows, best, scale), 2)}
    return result


def refine(path, requested, search=SEARCH):
    return refine_rows(features(decode_mono(path, RATE)), requested, search)
