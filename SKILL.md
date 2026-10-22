---
name: agent-audio
description: "Voice and score animated explainer videos through one OpenRouter key: expressive narration (Fish Audio ENERGETIC voice with irony/emotion tags), word-timed transcripts and SRT, original instrumental music (Lyria 3 Clip/Pro), cartoon sound effects (Seed Audio, cached library), and a finished mix with ducking, music cuts before punchlines, stems, -14 LUFS mastering, QA and muxing into the MP4. Reads toon-explainer spec.json to place music and effects on shots and on-screen events. Use when the user wants narration/voiceover, a re-voice of a film, background music, sound effects, sound design, a soundtrack or mix for a video or short, or audio matched to the reference channel style."
---

# Agent Audio

Turns a script or a toon-explainer film into finished audio: narration, music, effects, mix, video. Scripts do the
deterministic work (API calls, timing, mixing, measuring); you do the creative part (delivery tags, choosing which
moments get sound, music direction) and judge the result with the QA numbers.

All scripts live in `scripts/`, run with plain `python3` (3.10+, standard library) plus `ffmpeg`/`ffprobe`, from any
directory. Every paid request needs only `OPENROUTER_API_KEY` (env var, this skill's `.env`, or `--env-file`).
Never print the key. Every script has `--help`; paid ones have `--dry-run`, which reads no key and calls nothing.

## Pick the job

| Request | Do | Read |
| --- | --- | --- |
| Narration for a script | tag beats -> `speak.py` -> `transcribe.py --script` to verify | [delivery-modes](references/delivery-modes.md), [tags](references/tags.md) |
| Score or re-voice a toon-explainer film | full film workflow | [film-workflow](references/film-workflow.md) |
| A short (9:16) | film workflow with `"profile": "short"` | [sound-design](references/sound-design.md) |
| One music cue or effect | `generate_asset.py` (or `--library` / `--music-library NAME`) | [sound-design](references/sound-design.md) |
| Mix existing audio onto a video | write a soundtrack plan -> `soundtrack.py` | [soundtrack-plan](references/soundtrack-plan.md) |
| Study new reference videos | `analyze_refs.py levels` (free) / `study` (paid) | [reference-study](references/reference-study.md) |

Generate only what was asked: plain narration does not imply music. When the user asks for audio, produce it in the
same turn; no separate approval of the plan is needed unless they asked to review it first.

## Narration defaults

- Voice **ENERGETIC** (`1bf2dee1ca2848b5bc0580a4d9301341`), model `fish-audio/drama-3`. Change the voice or model only
  when the user picks another; never substitute silently.
- Delivery: lively, conversational, smiling irony with clear emotional turns; sincere passages stay sincere. No
  mournful, sleepy or shouty reads unless the scene calls for it. Keep the natural American pronunciation.
- Write new scripts in English unless asked otherwise; keep a supplied script's words and language. Talk to the user
  in their language.
- Split into beats of 1-4 sentences, give each a `mode`, add a short direction only where intent changes, and one or
  two tags per beat at most (`[pause]`, `[chuckling]`, `<stress>word</stress>`). Plan format:
  [examples/film-narration.json](examples/film-narration.json).

```bash
python3 scripts/speak.py --plan narration.json --out vo.mp3          # --dry-run first if unsure
python3 scripts/transcribe.py vo.mp3 --out vo.words.json --script narration.json   # also writes vo.srt
```

`transcribe.py` reports `script_match` and every difference with its time. "a hundred"/"100" style differences are
harmless; a spoken tag ("pause") or a missing phrase means re-render that beat. Re-render only the affected beats
(new plan, new `--out`); a new synthesis never matches the old one exactly.

## Scoring rules (from the reference channel)

Measured on the user's 20 reference videos, details in [reference-study](references/reference-study.md):

- **Music changes with the story** about 3 times a minute: new setting, new mood, new act. Build a small palette of
  moods and cut them to shot boundaries; do not loop one track under everything.
- **Pay for music once.** First reuse themes from the shared music library (`{"music_library": "ragtime-america"}`,
  free after the first film). For the moods it lacks, write one Lyria Pro track per act with timed sections
  (`"sections": {...}`, $0.08 for up to 3 minutes) instead of separate 30 s clips; cues pick a section by name.
  Never mention a narrator or voice-over in a music prompt: it invites Lyria to add mumbled speech.
- **Silence is the punchline.** Cut the music (a `dip` or a cue end) right before the joke or reveal and bring it back
  on the next beat. A hard cut on a glitch or a scene change is better than a fade.
- **Effects are literal and sparse.** Long videos about 4-7 a minute, shorts 8-20: impacts, whooshes, pops, foley and
  UI clicks on visible actions. Tier-A events from `timeline.py` almost always; tier B when the shot is about it;
  tier C almost never. Reuse one sound for a recurring action.
- **Levels** come from the profile (`long` or `short`); trim with `gain_db`. Long videos keep music well under the
  voice; shorts run a louder, denser bed. Deliver at -14 LUFS, peaks under -1 dBTP.

## Film workflow in short

1. `timeline.py beats spec.json` -> tag the beats -> `speak.py` -> `transcribe.py --script`.
2. Re-time the film to the new voice in toon-explainer (`script2spec.py --srt vo.srt --merge`, `lipsync.py`), render.
3. `timeline.py events spec.json --words vo.words.json --md timeline.md` and read `timeline.md`.
4. Write `soundtrack.json`: assets (music prompts, library effects), music cues per section, effects on events,
   dips before punchlines. Anchors like `"k12+1.4"` or `"word:knock"` survive re-timing.
5. `soundtrack.py --dry-run` (cost, cue sheet) -> `--assets-only` -> build with `--out` and `--video-out`.
6. Fix QA warnings, then optionally `review.py` on the scored video. Iterate on single cues; assets are cached.

Full commands, toon-explainer hand-off and pitfalls: [film-workflow](references/film-workflow.md). Every plan field
and anchor: [soundtrack-plan](references/soundtrack-plan.md). Prompting Lyria and Seed: [sound-design](references/sound-design.md).

## Money and safety

- Measured costs (2026-10-22): narration about $0.012 per 1000 characters, transcript $0.004/min, Lyria Clip
  $0.04, Lyria Pro $0.08, an effect $0.002-0.009 (Seed bills about $0.0025 per second of output, so a 24 s ambience
  bed is $0.06), AI review about $0.025/min with Pro. The 89 s test scene cost $0.34 with four separate music cues and
  $0.23 with one sectioned Pro track; a full 9-minute film is roughly $0.5-0.8 once the libraries are warm.
  `soundtrack.py --dry-run` estimates a plan.
- No script retries a paid request. A timeout may still be billed: inspect the manifest, then decide. Paid bytes are
  kept (`*.source.mp3`); re-cutting, re-trimming and re-mixing never pay again.
- Library effects and music themes are cached in `library/` (or `AGENT_AUDIO_LIBRARY`) and shared by every film.
  Every generated music track is checked for hidden speech once (about $0.006); a cue that would play it is flagged.
  Asset files are named `<name>-<request hash>.wav`, so a changed prompt or length makes a new file and never
  overwrites a paid one; a lock file stops two runs from paying for the same asset.
- Outputs are never overwritten silently; the mix writes a new file plus `<out>.stems/` and `<out>.manifest.json`.
  The source video is never modified; `--video-out` writes a new MP4 with the picture copied bit for bit.

## Check before delivering

- `soundtrack.py` prints QA: loudness, true peak, music under the voice while words are spoken, effects that cover
  words for 0.25 s or more, dead air, effects per minute. Fix every WARN or say why it is intentional.
- `review.py` (paid) watches and listens like a supervising editor. It is a second opinion: confirm each issue in the
  stems before acting, and never put the expected result in `--context` (it biases the model into false failures).
- You cannot hear the mix. Say which checks were automated, give the user the file paths (final MP4, mix, stems), the
  cost, and the moments worth a listen.
