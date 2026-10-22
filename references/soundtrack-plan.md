# soundtrack.json reference

`soundtrack.py` reads one JSON object. Relative paths resolve from the plan's folder. Unknown fields are errors, so a
typo fails before anything is paid. Every object may carry a free-text `notes` field. Example:
[../examples/soundtrack-plan.json](../examples/soundtrack-plan.json).

## Top level

| Field | Required | Meaning |
| --- | --- | --- |
| `narration` | yes | Voice recording; the film timeline starts at its first sample |
| `timeline` | for shot anchors | `timeline.py events` output |
| `words` | for word anchors | `transcribe.py` output |
| `video` | for `--video-out` | Picture to mux onto (or pass `--video`) |
| `profile` | no | `long` (default) or `short`; sets level offsets, ducking and QA targets |
| `duration_seconds` | no | Film length; default: video length, else timeline, else narration |
| `duck` | no | Default `true`: music and ambience dip under the voice (sidechain) |
| `voice_compression` | no | Default `true`: high-pass and gentle compression on the narration |
| `assets` | no | Name -> asset (below) |
| `music`, `ambience` | no | Bed cues (below) |
| `sfx` | no | Point cues (below) |
| `dips` | no | Music/ambience mutes (below) |

## Assets

Names use letters, digits, `.`, `-`, `_`. One of:

```json
"bed":   {"file": "my-track.wav"}
"stamp": {"library": "stamp-thud"}
"rag":   {"music_library": "ragtime-america"}
"act1":  {"kind": "music", "model": "pro", "seconds": 100, "prompt": "[0:00 - 0:25] ... [0:25 - 0:40] ...",
          "sections": {"press": 0, "dark": 25}}
"m_act1": {"kind": "music", "model": "pro", "seconds": 95, "prompt": "..."}
"m_sting": {"kind": "music", "model": "clip", "seconds": 30, "prompt_file": "prompts/sting.txt"}
"crowd": {"kind": "sfx", "seconds": 20, "prompt": "..."}
```

`sections` (any music asset) names where each part starts in the prompt's timeline. At build time each start
moves to the strongest change in the audio within 3 s; the found starts are printed and stored in the mix manifest.
Library themes (`music_library`, [music-library.json](music-library.json)) are one mood each, about 90 s, rendered
once into `<library>/music/`.

Generated assets go to `assets/<name>-<hash>.wav` beside the plan with their paid source and manifest; the hash is
the request's, so editing a prompt or length creates a new file and the old paid one stays. Library sounds are in
[sfx-library.json](sfx-library.json) and render once into the shared cache. Clip music is always about 30 s; Pro
follows the requested length up to about 3 minutes. Effects are cut from their attack and keep their natural tail
(up to 1.5x the requested length).

## Times: anchors

Any time field accepts:

| Anchor | Resolves to |
| --- | --- |
| `12.5` or `"12.5"` | Absolute seconds |
| `"k12"` | Start of shot k12 |
| `"k12:end"` | End of shot k12 |
| `"word:knock"` | Start of the first spoken "knock" |
| `"word:knock@2"` | Second occurrence |
| `"word:lead~140"` | Occurrence nearest 140 s |
| `"word:die:end"` | End of that word |

Each may end with an offset: `"k12+1.4"`, `"k12:end-0.3"`, `"word:mechanical-0.35"`. Word matching ignores case and
punctuation; a transcript may write numbers as digits ("100"), so anchor on a neighbouring word or the shot. Shot
anchors are the robust choice for on-screen events: event times from `timeline.md` are `shot start + local time`, so
write `"k12+1.94"` rather than `56.66` and the cue follows the shot when the film is re-timed.

## Bed cues (`music`, `ambience`)

| Field | Default | Meaning |
| --- | --- | --- |
| `asset` | required | Asset name |
| `start`, `end` | 0, film end | Where the cue plays on the timeline |
| `section` | - | Name from the asset's `sections`: reading starts at that section's measured start |
| `source_start` | 0 | Where to start reading the asset (with `section`: seconds into that section) |
| `gain_db` | 0 | Trim relative to the profile level (-40 to +20) |
| `fade_in`, `fade_out` | 0.3, 0.6 | Seconds; `fade_out` 0.04 makes a hard stop |
| `loop` | false | Repeat the asset to fill the span (audible seam; prefer a longer asset or a second cue) |

If the asset runs out before `end`, the cue stops early and a warning says where. Warnings also flag a cue that runs
past the end of its section, a section whose change is faint, and a cue that would play speech found in the music.

## Effect cues (`sfx`)

`asset`, `at` (required), `gain_db` (0), `source_start` (0), `duration` (to the end of the asset), `fade_in`
(0.002), `fade_out` (0.03).

## Dips

`{"start": ..., "end": ..., "gain_db": -60, "ramp_in": 0.04, "ramp_out": 0.35}` mutes music and ambience between
`start` and `end` (effects and voice are untouched). The music is fully down at `start` and fully back at
`end + ramp_out`. Use one per dry punchline; for a permanent stop end the cue instead.

## Levels

Each asset is measured once (cached in `.agent-audio-levels.json` beside the plan) and placed relative to the
narration normalised to -16 LUFS:

| Profile | Music | Ambience | Effects (loudest 100 ms) | Duck ratio |
| --- | --- | --- | --- | --- |
| `long` | -14 dB | -22 dB | -7 dB | 2.5 |
| `short` | -11 dB | -20 dB | -4 dB | 1.8 |

Music and ambience are integrated loudness; ducking takes a further 4-8 dB off while words are spoken. The master is
gained to -14 LUFS and limited. With these defaults the test scene measured music 23.7 dB under the voice during
speech; references range about 14-26 dB for long videos. Trim per cue with `gain_db`.

## Outputs

`--out final.wav|mp3|m4a` writes the master, `final.wav.stems/` (`voice`, `music`, `ambience`, `sfx`, `premix`,
plus `graph.txt`, the ffmpeg graph used) and `final.wav.manifest.json` (resolved cue sheet, placed gains, input
hashes, master and QA results). `--video-out` writes a new MP4: picture stream copied, AAC 256k soundtrack, cut to the
video's length.
