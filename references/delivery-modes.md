# Delivery modes

Beat-based direction, tempo contrasts, and the mode palette are adapted from
`google-ai-studio-tts/references/delivery-modes.md` and `channel-voice-profile.md`.
The directions were rewritten for Fish Audio, the American ENERGETIC voice,
and the user's preference for expressive irony. This skill has no runtime
dependency on that repository. Gemini/Enzo measurements are not transferred:
they do not calibrate this voice.

Exact API directions live in [presets.json](presets.json). Mode names match
`--mode` and the `mode` field in JSON plans.

| Mode | Use | Delivery contrast |
|---|---|---|
| `ironic` | Main narration, funny stories, explainers | Conversational, smiling, wry emphasis |
| `short` | Short-video hook and main story | Fast, punchy, animated key words |
| `fast-ironic` | Quick recaps | Flowing quick phrases, mock disbelief |
| `deadpan` | A short punchline | A tiny pause and dry comic landing |
| `dramatic` | Rising stakes, a twist, a climax | More intensity without a slow, deep trailer voice |
| `serious` | Sincere explanation, grief, tragedy | Honest, without forced irony or theatrical crying |
| `rapid` | Lists, time skips, montages | Fast and rhythmic, still intelligible |
| `sponsor` | Ads or ad parodies | Brighter and more assured, same narrator |
| `mock-outrage` | Comic indignation, rhetorical questions | Surprise through intonation, not harsh yelling |
| `conspiratorial` | A brief secret shared with the viewer | Softer and closer, then back to narration |
| `character` | A character's quoted line | Act the intention while retaining the chosen timbre |
| `host` | A mock presenter or official | Slightly pompous formality |
| `poetry` | A short quotation or verse | Rhythm and melody, without endless pauses |
| `outro` | Ending or call to action | Warm, upbeat, a confident final beat |
| `calm` | A relaxed explanation | Friendly and clear, not sleepy |

## Choose by meaning

For an ironic short, start with `short` or `ironic`, use `conspiratorial` for
a brief aside, `mock-outrage` for one reaction, and `deadpan` for the final joke.
Do not cycle through every mode just to demonstrate them.

For long videos, keep `ironic` as the base and change at narrative turns,
not on a quota. A sincere scene calls for `serious`, even on a comedy channel.

Begin with beats of 1–4 sentences. Neighbouring beats with the same intent
can be merged while they fit the request size. A new emotion does not require
a new speaker or voice.

Keep a rhetorical question and its answer as short phrases:
“A bold strategy? Absolutely. A good one? Not even slightly.”
Place the joke at the end of the sentence. A comma, period, or one `[pause]`
is usually enough for timing; repeated ellipses can slow the performance.

## Correct the delivery

- Too sad: remove `sad`, `mournful`, `hushed`, and long pauses; try `amused`, `smiling`, `conversational`. Do not compensate by changing the voice's pitch or age.
- Too shouty: replace `[angry]` with `[amused disbelief, moderate intensity]`; limit `mock-outrage` to one line.
- Too flat: isolate a punchline or aside into its own beat; do not add emotion on every word.
- Too commercial: choose `ironic` or `calm` instead of `sponsor` and shorten the overall direction.
- Wrong pace: measure actual duration and inspect pauses. A WPM number written in a direction is a loose artistic target, not an exact control.
- One bad passage: render that passage separately and replace it in the edit. A new synthesis is not deterministic.
