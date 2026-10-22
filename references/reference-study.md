# Reference study

Source: the user's reference corpus (`new/ref/`: 8 long animated history videos, 12 shorts), studied 2026-10-22.
These numbers set the mixer profiles, QA targets and the event table. The study informs the style; never copy, sample
or imitate a specific track or effect from the references.

## Loudness and music depth (local measurement, `analyze_refs.py levels`)

First 240 s of each video. `bed depth` = speech peaks (p90 of 50 ms RMS) minus the floor between words (p10), a proxy
for how far music and ambience sit under the voice.

| | Integrated | LRA | True peak | Bed depth |
| --- | --- | --- | --- | --- |
| Long (8) | -13.4 to -17.0 LUFS, median -14.7 | 2.7-5.3 LU (one 15) | -0.1 to +1.8 dBTP | 18.4-24.9 dB, median 20.5 |
| Shorts (12) | -11.7 to -16.4 LUFS, median -15.2 | 0.8-1.4 LU | -2.4 to +0.3 dBTP | 8.3-13.1 dB, median 10.6 |

Reading: narration is heavily compressed (narrow LRA). Long videos keep music well under the voice; shorts run a bed
only about 10 dB down, dense and continuous. The references overshoot 0 dBTP; we deliver -14 LUFS / -1 dBTP instead.

## Picture + sound audit (`analyze_refs.py study`, Gemini 3.8 Flash, $0.20)

14 excerpts: three 100 s excerpts from each of 3 long videos and the first 40-75 s of 5 shorts. Model output, so
treat counts as estimates.

| | Long | Shorts |
| --- | --- | --- |
| Effects per minute | 4.2-10.2, median 4.8 | 8.5-22.4, median 13.5 |
| Music changes per minute | 1.2-4.8, median 3.0 | 0-5.3, median 3.2 |
| Music level vs voice | mostly "low", some "medium" | mostly "medium" |

Music entry: mostly already under speech (26), fades (13), hard cuts (10), stingers (5).
Music exit: fades (19), hard stops (12), continues into the next scene (11), cut to silence (9).

Effect categories heard (160 events): foley 40, impacts 34, whooshes 22, comedic 16, UI clicks 15, dings 13,
stingers 9, pops 6, ambience 3, risers 1, other 1. Typical triggers:

- **Impact**: a heavy object slammed down, a body hitting the floor, a cage dropping, an explosion, a weight labelled
  with the joke's word ("INFLATION") landing on a table.
- **Whoosh**: a character thrown or falling, a camera zoom into a painting, a map cutaway, a blade swing.
- **Foley**: paper lifted, a coin dropped, a spotlight snapping on, typing, a bell, an axe.
- **UI click / ding / pop**: chat bubbles, counters updating ("YEARS PASSED"), map pins, an icon appearing, a
  level-up graphic, a "$$$" sign.
- **Stinger**: a menacing reveal, a sudden horror face, an error buzzer when the mood flips.
- **Comedic**: slide whistle, boing, record scratch, squelch, facepalm smack, belch.

Recurring techniques reported:

- Music drops out before violent or comic punchlines and before scene changes; the joke lands dry.
- Lingering silence after a physical gag sells the deadpan line that follows.
- Genre and instrument shifts mark place and period immediately (accordion for Italy, hopak for Stalin, pub music,
  Japanese instruments, ragtime for 1920s America); playful pizzicato and brisk jazz under explanations.
- Effects are literal (a heavy weight slam for a metaphorical burden) and synchronised to on-screen gags.
- Period speech gets a band-pass "radio/TV" filter when a character speaks through a device.
- Game and internet audio idioms (level-up chimes, notification pings) for modern-joke graphics.

## How the skill uses this

- `mix.py` profiles: `long` music -14 dB / effects -7 dB, `short` music -11 dB / effects -4 dB relative to the voice.
  Calibrated on a test scene to land music about 21-24 dB under the voice during speech (reference range 14-26).
- `qa.py` targets: effects per minute 3-9 (long) / 7-22 (short); music under voice 14-26 / 7-16 dB.
- [event-sounds.json](event-sounds.json): which toon-explainer events get a sound by default.
- [sound-design.md](sound-design.md): palettes and prompts that follow these techniques.

## Re-running

```bash
python3 scripts/analyze_refs.py levels --refs /path/to/refs --out levels.json
python3 scripts/analyze_refs.py study --refs /path/to/refs --limit-long 3 --limit-short 5 --out study.json --dry-run
python3 scripts/analyze_refs.py study --refs /path/to/refs --limit-long 3 --limit-short 5 --out study.json
```

`study` compresses each excerpt to 360p/4 fps, sends picture and sound, and saves results per clip (an interrupted
run continues where it stopped). About $0.015 per 100 s excerpt.
