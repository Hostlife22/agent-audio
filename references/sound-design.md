# Music and effects: design and prompting

Style rules come from [reference-study.md](reference-study.md). This file turns them into choices and prompts.

## Music: a palette, cut to the picture

Write one cue per mood, not one per shot, and cut cues on shot boundaries. A typical 8-10 minute history film needs
6-10 cues; a 60 s short 2-4. Match the setting and period first, the emotion second:

| Moment | Palette (adapt to period and place) |
| --- | --- |
| Comic explanation (the base) | Pizzicato strings, bassoon or clarinet, plucked bass, light woodblock or brushed snare, 96-110 BPM |
| A place or era | Its idiom: ragtime piano for 1920s America, accordion for Paris or Italy, balalaika for Russia, koto for Japan, harpsichord for a royal court |
| Mystery, a secret | Muted pizzicato, low clarinet, ticking percussion, minor key, sparse |
| Rising stakes, a question | Staccato low strings in eighths, ticking hi-hat, timpani rolls building |
| Dark turn, death, poison | Low cello/bass drone, distant metallic hits, a slow heartbeat drum; no melody |
| Action, chaos, montage | Driving strings or an electronic pulse, 120+ BPM; keep the biggest hits for speech gaps |
| Triumph, a twist of fate | Brass swell, snare roll, short and slightly ironic |
| Sincere, tragic | Solo piano or a soft string pad; no comic punctuation at all |
| Modern joke, game or internet reference | Chiptune or synth pop, very short |

Edit like the references:

- Hard cut music on a glitch, a smash cut, a scene change (`"end": "k06"`, `fade_out` 0.04).
- Put a `dip` from just before a punchline to the next beat; the joke lands dry and the music returning is the button.
- Leave a stinger or impact in the silence after the music stops, not on top of a word.
- For a return of a mood, reuse its cue from another point (`source_start`) rather than generating a new one.

### Writing Lyria prompts

Lead with function and genre, then instruments, tempo and key, energy, density, ending. The script appends "purely
instrumental, no voice of any kind, no singing, humming, choir or spoken words". Do not write "narrator", "voice-over",
"narrated" or "documentary narration" in a music prompt: in testing such wording produced a track with mumbled
speech. Every generated music track is transcribed once and any speech is reported with its time.

```
Sly 1920s newsroom mystery underscore for a cartoon history documentary. Muted pizzicato strings, soft brushed
snare, ticking woodblock, a low clarinet hinting at a secret. 96 BPM, D minor, curious and slightly suspicious,
light and sparse.
```

Lyria Pro (`"model": "pro"`) follows a target length and timed sections; boundaries land up to 3 s off, so declare
them in `"sections"` and let `soundtrack.py` snap each to the measured change. Do exact stops in the mix:

```
Playful early-1920s American ragtime underscore for a comedic animated history explainer, 104 BPM, F major.
[0:00 - 0:15] Bouncy honky-tonk piano with pizzicato strings and a light snare, cheerful and cheeky.
[0:15 - 0:30] Add a tuba bass line and slide trombone accents, mischievous comic trouble.
[0:30 - 0:45] Back to light piano and pizzicato, confident and a little smug, ending on a clean final chord.
```

Cheapest first: a theme from [music-library.json](music-library.json) costs nothing after its first render. Then
one Pro track per act ($0.08 for up to about 3 minutes) holding several moods as sections, each introduced with "hard
cut to" so the change is clear. Clip ($0.04 per 30 s) only for a single stinger or a one-off short scene. Example of
an act track:

```
Cartoon underscore in five sharply contrasting sections, each change is a hard cut to new instruments.
[0:00 - 0:25] Sly 1920s mystery: muted pizzicato strings, soft brushed snare, ticking woodblock, low clarinet, 96 BPM.
[0:25 - 0:40] Hard cut to a dark ominous drone: only low cello and bass cluster, slow heartbeat drum, no melody.
[0:40 - 0:52] Building tension: pulsing staccato low strings, ticking hi-hat, timpani rolls, 110 BPM.
[0:52 - 1:28] Hard cut to playful early-1920s American ragtime: honky-tonk piano, pizzicato, tuba, 104 BPM.
[1:28 - 1:40] Hard cut back to the dark low drone, cold, fading out.
```
with `"sections": {"press": 0, "dark": 25, "question": 40, "dayton": 52, "ending": 88}`. Measured starts in the test:
22.7, 40.2, 50.3 and 85.1 s. Lyria cannot make true silence on request (a requested 4 s "near silence" came out only 6 dB
quieter). Prompts naming real artists or songs are blocked by the provider.

## Effects

Start from [sfx-library.json](sfx-library.json) (45 cached sounds for common toon-explainer events). Write a custom
`sfx` asset when the gag needs something specific: describe the source, the action, the material, how many events,
the perspective and the tail, in one sentence or two.

```
One rubber office stamp slammed hard onto paper on a wooden desk, firm thud, tiny paper rattle.
Indoor ambience of a 1920s press room: a crowd of reporters murmuring quietly, pencils scribbling, occasional paper
rustle. Steady background bed, no clear words.
```

Seed returns MP3s with variable leading silence and tails; the generator cuts from the attack so the effect lands on
its anchor, and keeps the natural decay. Seed can do ambience beds up to 30 s; loop or chain them for longer scenes.

Choosing effects from `timeline.md`:

- Tier A (stamps, punch-zooms, impact frames, explosions, glitches, titles, whip and zoom transitions): sound them.
- Tier B (flashes, counters, maps, shakes, drop-in entrances, takes): only when the shot is about that action or the
  narration points at it. Several flashes in a row: sound one or two.
- Tier C (particles, auras, small pops, fades, labels): leave silent unless it is the punchline.
- Same action, same sound, for the whole film. Two effects closer than about 0.3 s blur together; pick one.
- Keep effects off important words; QA flags any that cover speech for 0.25 s or more. Into a pause, or `gain_db`
  down.

## Shorts

`"profile": "short"`: music about 6 dB louder relative to the voice, effects louder and about twice as frequent,
music cuts every 15-20 s or on every new gag. Start the music on the first frame; the hook line still needs to sit
clearly on top.
