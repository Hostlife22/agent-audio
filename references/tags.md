# Fish Audio Drama 3 tags

This is **not** Gemini syntax. The source `google-ai-studio-tts` skill uses
standalone `<chuckle>` for a reaction; Fish uses `[chuckling]`.
Angle brackets in Drama 3 delimit **paired** controls on selected words.

## Direction and emotion

Put a short natural-language direction in square brackets before the line.
Directions can be written in English or the user's language.

```text
[wry, smiling, conversational] At nine, the king declared war on a pigeon.
[dry irony] A bold strategic move.
[amused disbelief] Twelve soldiers? For one bird?
[excited] And then the pigeon moved six inches to the left!
[deadpan] A devastating tactical retreat.
```

One direction per change of intent and one or two local reactions per beat
are usually enough. Do not repeat a long style instruction after each word.

| Intent | Useful directions |
|---|---|
| Irony and a smile | `[dry irony, smiling]`, `[wry, amused]` |
| Joy and surprise | `[excited]`, `[surprised, delighted]` |
| Embarrassment | `[embarrassed]` |
| Comic indignation | `[amused disbelief, moderate intensity]` |
| Real anger, when appropriate | `[angry]` |
| Gentleness | `[soft]`, `[warm, gently reassuring]` |
| Sadness, only when the text calls for it | `[sad, restrained]` |
| Whisper | `[whispering]` on a brief line, followed by a normal-delivery beat |
| Breathy delivery | `[breathy]` when intentionally needed |

## Sounds and pauses

Useful reactions include `[chuckling]`, `[laughing]`, `[sighing]`,
`[clear throat]`, `[groaning]`, `[panting]`, `[sobbing]`, `[crying loudly]`.
Pauses use `[pause]` and `[long pause]`.

```text
[amused] The army lost the ladder. [chuckling] Naturally, they blamed the gardener.
[deadpan] Final score: the kingdom, zero. [pause] Pigeon, one.
```

Do not turn irony into a laugh track. Pause duration is not fixed. Add music,
crowds, and background effects in the edit unless generating them is requested.

## Control selected words

The official Drama 3 examples confirm these forms:

```text
The king was <stress>absolutely certain</stress> this would work.
Between us, <whisper>the pigeon had no flag</whisper>.
He called it <slow>a magnificent victory</slow>.
```

Close the tag before words that should return to normal. Keep paired tags
within one beat. The CLI checks balancing, not whether an arbitrary tag name
is supported. For an unfamiliar effect, prefer `[slightly faster, playful]`
over inventing an XML tag.

Fish's menu also offers Emphasis, Soft, Faster, and Forceful. Check current
documentation or the editor's inserted markup for their exact paired syntax:
a button label alone does not establish the tag name.

Syntax source: [Fish Audio Drama 3](https://fish.audio/drama-3/).
A tag's presence does not prove that the effect sounded good; check the output.
