import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import common  # noqa: E402
import speak  # noqa: E402
VOICE = "1bf2dee1ca2848b5bc0580a4d9301341"
FAKE_AUDIO = b"ID3" + b"audio" * 100


def payload(text="A small joke.", voice=VOICE):
    return {"model": "fish-audio/drama-3", "voice": voice,
            "input": [{"voice": voice, "text": text, "instructions": "Wry and smiling."}],
            "response_format": "mp3"}


class SpeechTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.output = self.root / "story.mp3"

    def tearDown(self):
        self.temp.cleanup()

    def fake_inspect(self, path):
        return {"bytes": Path(path).stat().st_size, "duration_seconds": 1.0}

    def fake_combine(self, parts, output):
        output.write_bytes(b"".join(p.read_bytes() for p in parts))
        return self.fake_inspect(output)

    def test_dry_run_needs_no_key_or_network_and_preserves_script(self):
        text = "[wry] Not <stress>another</stress> pigeon!"
        script = self.root / "script.txt"
        script.write_text(text)
        stdout = io.StringIO()
        with patch.object(speak, "resolve_key") as keys, patch.object(speak, "post_speech") as network:
            with contextlib.redirect_stdout(stdout):
                result = speak.main(["--script", str(script), "--dry-run"])
        self.assertEqual(result, 0)
        keys.assert_not_called()
        network.assert_not_called()
        data = json.loads(stdout.getvalue())["requests"][0]
        self.assertEqual(data["voice"], VOICE)
        self.assertEqual(data["input"][0]["voice"], VOICE)
        self.assertEqual(data["input"][0]["text"], text)

    def test_bad_plan_fails_before_loading_credentials(self):
        plan = self.root / "plan.json"
        plan.write_text(json.dumps({"beats": [{"text": "<stress>unfinished", "mode": "ironic"}]}))
        stderr = io.StringIO()
        with patch.object(speak, "resolve_key") as keys, contextlib.redirect_stderr(stderr):
            result = speak.main(["--plan", str(plan), "--out", str(self.output)])
        self.assertEqual(result, 1)
        self.assertIn("Unclosed", stderr.getvalue())
        keys.assert_not_called()

    def test_local_key_file_environment_priority_and_explicit_override(self):
        skill = self.root / "agent-audio"
        skill.mkdir()
        legacy = "sk-or-v1-legacy-unit-test"
        (skill / ".env").write_text(legacy + "\n")
        explicit = self.root / "custom.env"
        explicit.write_text('export OPENROUTER_API_KEY="sk-or-v1-explicit-unit-test" # local\n')
        with patch.object(common, "ROOT", skill), patch.dict(os.environ, {}, clear=True):
            self.assertEqual(speak.resolve_key(), legacy)
            os.environ["OPENROUTER_API_KEY"] = "sk-or-v1-environment-unit-test"
            self.assertEqual(speak.resolve_key(), os.environ["OPENROUTER_API_KEY"])
            self.assertEqual(speak.resolve_key(explicit), "sk-or-v1-explicit-unit-test")

    def test_provider_json_is_not_saved_as_audio(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {"Content-Type": "application/json"}
        response.read.return_value = b'{"error":"not audio"}'
        with patch.object(common.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(speak.SpeechError, "Expected MP3"):
                speak.post_speech(payload(), "sk-or-v1-test", 10)

    def test_http_errors_redact_key_and_do_not_retry(self):
        secret = "sk-or-v1-private-unit-test"
        error = urllib.error.HTTPError(speak.ENDPOINT, 429, "limited", {}, io.BytesIO(secret.encode()))
        with patch.object(common.urllib.request, "urlopen", side_effect=error) as request:
            with self.assertRaises(speak.SpeechError) as caught:
                speak.post_speech(payload(), secret, 10)
        self.assertNotIn(secret, str(caught.exception))
        self.assertIn("[REDACTED]", str(caught.exception))
        self.assertEqual(request.call_count, 1)

    def test_resume_reuses_paid_parts_and_completed_output(self):
        requests = [payload("First beat."), payload("Second beat.")]
        with patch.object(speak.shutil, "which", return_value="ffmpeg"), \
                patch.object(speak, "inspect_audio", side_effect=self.fake_inspect), \
                patch.object(speak, "combine", side_effect=self.fake_combine):
            with patch.object(speak, "post_speech", side_effect=[(FAKE_AUDIO, {}), speak.SpeechError("network")]) as api:
                with self.assertRaisesRegex(speak.SpeechError, "network"):
                    speak.render(requests, self.output, "sk-or-v1-test")
                self.assertEqual(api.call_count, 2)
            manifest = speak.read_json(str(self.output) + ".manifest.json")
            self.assertFalse(manifest["complete"])
            self.assertEqual(len(manifest["parts"]), 1)
            self.assertFalse(self.output.exists())
            with patch.object(speak, "post_speech", return_value=(FAKE_AUDIO, {})) as api:
                speak.render(requests, self.output, "sk-or-v1-test", resume=True)
                self.assertEqual(api.call_count, 1)
                self.assertEqual(api.call_args.args[0], requests[1])
            with patch.object(speak, "post_speech") as api:
                speak.render(requests, self.output, "unused", resume=True)
                api.assert_not_called()

    def test_changed_request_cannot_resume_existing_audio(self):
        with patch.object(speak, "post_speech", return_value=(FAKE_AUDIO, {})), \
                patch.object(speak, "inspect_audio", side_effect=self.fake_inspect):
            speak.render([payload()], self.output, "sk-or-v1-test")
        before = self.output.read_bytes()
        with patch.object(speak, "post_speech") as api:
            with self.assertRaisesRegex(speak.SpeechError, "Resume refused"):
                speak.render([payload("Different words.")], self.output, "unused", resume=True)
            api.assert_not_called()
        self.assertEqual(self.output.read_bytes(), before)

    def test_changed_cached_audio_stops_before_new_paid_request(self):
        requests = [payload("First."), payload("Second.")]
        with patch.object(speak.shutil, "which", return_value="ffmpeg"), \
                patch.object(speak, "inspect_audio", side_effect=self.fake_inspect), \
                patch.object(speak, "post_speech", side_effect=[(FAKE_AUDIO, {}), speak.SpeechError("stop")]):
            with self.assertRaises(speak.SpeechError):
                speak.render(requests, self.output, "sk-or-v1-test")
        part = Path(str(self.output) + ".parts") / "part-0001.mp3"
        part.write_bytes(b"changed")
        with patch.object(speak, "post_speech") as api, patch.object(speak.shutil, "which", return_value="ffmpeg"):
            with self.assertRaisesRegex(speak.SpeechError, "missing or changed"):
                speak.render(requests, self.output, "unused", resume=True)
            api.assert_not_called()

    def test_explicit_fish_url_changes_all_turn_voices(self):
        other = "802e3bc2b27e49c2995d23ef70e6ac89"
        script = self.root / "script.txt"
        script.write_text("First sentence.\n\nSecond sentence.")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = speak.main(["--script", str(script), "--voice", f"https://fish.audio/app/m/{other}/", "--dry-run"])
        self.assertEqual(result, 0)
        data = json.loads(stdout.getvalue())["requests"][0]
        self.assertEqual(data["voice"], other)
        self.assertEqual({t["voice"] for t in data["input"]}, {other})

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
    def test_real_mp3_parts_join_and_decode_without_network(self):
        sample = self.root / "sample.mp3"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.2",
                        "-c:a", "libmp3lame", str(sample)], check=True)
        with patch.object(speak, "post_speech", return_value=(sample.read_bytes(), {})):
            result = speak.render([payload("One."), payload("Two.")], self.output, "sk-or-v1-test")
        self.assertTrue(result["complete"])
        self.assertGreater(result["duration_seconds"], 0.3)
        self.assertLess(result["duration_seconds"], 1)
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(self.output), "-f", "null", "-"], check=True)


if __name__ == "__main__":
    unittest.main()


class PlanFieldTests(unittest.TestCase):
    def test_shot_field_is_accepted_but_never_sent(self):
        with tempfile.TemporaryDirectory() as folder:
            plan = Path(folder) / "plan.json"
            plan.write_text(json.dumps({"source": "spec", "beats": [{"shot": "k01", "mode": "deadpan", "text": "Bold."}]}))
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(speak.main(["--plan", str(plan), "--dry-run"]), 0)
        turn = json.loads(stdout.getvalue())["requests"][0]["input"][0]
        self.assertEqual(set(turn), {"text", "voice", "instructions"})
        self.assertIn("mock-serious", turn["instructions"])
