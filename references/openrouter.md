# OpenRouter routes, models and failures

Verified with live requests on 2026-10-22. Model lists and prices change: check
`GET https://openrouter.ai/api/v1/models?output_modalities=speech|audio|transcription` before promising a model.
One OpenRouter key covers every route; no Fish, Google or ByteDance key is needed.

| Job | Model | Route | Notes |
| --- | --- | --- | --- |
| Narration | `fish-audio/drama-3` | `POST /api/v1/audio/speech` | Returns MP3 bytes. `input` is an array of turns, each with `voice`, `instructions`, `text` |
| Music, 30 s | `google/lyria-3-clip-preview` | `POST /api/v1/chat/completions` | `modalities: ["audio"]`, `audio: {format: mp3}`, `stream: true`; base64 MP3 in `delta.audio.data`; $0.04 |
| Music, 1-3 min | `google/lyria-3-pro-preview` | same | Follows a stated length and `[m:ss - m:ss]` sections; 44.1 kHz stereo; $0.08 |
| Effects, ambience | `bytedance-seed/seed-audio-1-0` | `POST /api/v1/audio/speech` | Natural-language `input`; send no `voice` (a voice turns it into speech) |
| Word timestamps | `elevenlabs/scribe-v2` | `POST /api/v1/audio/transcriptions` | `input_audio` base64, `response_format: verbose_json`, `timestamp_granularities: ["word"]`; about $0.0037/min |
| Reference audit | `google/gemini-3.8-flash` | chat completions | `video_url` with a base64 `data:video/mp4` URL; picture and sound |
| Mix review | `google/gemini-3.1-pro-preview` | chat completions | Same input; Flash invented timestamps in testing |

Narration request as sent by `speak.py`:

```json
{"model": "fish-audio/drama-3", "voice": "1bf2dee1ca2848b5bc0580a4d9301341", "response_format": "mp3",
 "input": [{"voice": "1bf2dee1ca2848b5bc0580a4d9301341",
            "instructions": "Keep the selected voice's natural timbre... Dry, mock-serious understatement...",
            "text": "Not a chemist. Not a doctor. [pause] A mechanical engineer with a gift for selling ideas."}]}
```

`fish-audio/s2.1-pro` is an explicit alternative (`--model`); the tags here target Drama 3. The API `speed` field is
not sent: pace is directed in words.

## Files a request leaves behind

- Narration `vo.mp3`: `vo.mp3.manifest.json` (voice, model, per-part hashes and durations), `vo.mp3.parts/`
  (`requests.json` with the text sent, `part-0001.mp3`...). Requests group whole beats up to 2400 characters.
- Assets `name-<hash>.wav`: `.source.mp3` (the paid bytes, kept), `.manifest.json` (request, provider metadata,
  processing, and for music a `speech_check` with any words heard). A response that fails validation is kept as
  `.rejected`; `.lock` exists only while a request runs. `AGENT_AUDIO_SKIP_SPEECH_CHECK=1` skips the music check.
- Transcripts checkpoint every paid chunk in `<out>.partial.json`; a rerun after a failure pays only for the rest.
- Reference studies save each clip's answer as it arrives; clips sent but never answered need `--retry-pending`.
- Reviews are saved to `--out` (default `<video>.review.json`), including the raw text if it was not JSON.
- Transcripts carry `request_sha256` of the audio and settings; the same audio is never transcribed twice.

None of these contain the key. Scripts and prompts are user material: do not publish them unasked.

## Failures

- No script retries a paid request. A timeout or dropped stream may still be billed.
- Narration: completed parts are checkpointed; `--resume` continues with the remaining parts after verifying hashes.
  Changed text or settings need a new `--out`.
- Assets: if the source was saved, any later run re-cuts it for free. If a request failed before the source arrived,
  the manifest stays in `request-started`; the error says so. Look at the provider error (it is printed, redacted),
  then delete that `.manifest.json` to pay for a new attempt. A transient Lyria stream error succeeded on the second
  try in testing; a safety rejection needs a reworded prompt.
- HTTP errors print the provider's message with any key redacted.
- STT providers time out after about 60 s of processing; `transcribe.py` splits long audio at pauses every 240 s.

Offline checks (no key, no network):

```bash
python3 scripts/speak.py --plan examples/film-narration.json --dry-run
python3 scripts/generate_asset.py --library stamp-thud --dry-run
python3 -m unittest discover -s tests
```
