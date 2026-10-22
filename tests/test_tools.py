"""CLI paths of the network tools, with OpenRouter replaced by fakes. Needs ffmpeg; never touches the network."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import analyze_refs  # noqa: E402
from common import AudioError, read_json  # noqa: E402,F401
import review  # noqa: E402
import soundtrack  # noqa: E402
import timeline  # noqa: E402
import transcribe  # noqa: E402

KEY = "sk-or-v1-unit-test-key"


def run(main, argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
class ToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict("os.environ", {"OPENROUTER_API_KEY": KEY})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def tone(self, name, seconds, pattern="sin(2*PI*220*t)*0.3"):
        path = self.root / name
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"aevalsrc={pattern}:s=16000:d={seconds}", str(path)], check=True)
        return path

    def video(self, name, seconds):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=gray:s=64x36:d={seconds}:r=10",
                        "-f", "lavfi", "-i", f"sine=f=330:d={seconds}", "-shortest", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)], check=True)
        return path

    # ---------- transcribe ----------

    def test_transcribe_splits_long_audio_at_pauses_and_shifts_word_times(self):
        # Tone with a silent gap every 10 s; chunks of about 10 s must cut inside a gap.
        audio = self.tone("vo.wav", 25, "if(lt(mod(t\\,10)\\,9.5)\\,sin(2*PI*220*t)*0.3\\,0)")
        calls = []

        def fake(route, payload, key, timeout, title):
            self.assertEqual((route, key), ("/audio/transcriptions", KEY))
            calls.append(payload)
            return {"words": [{"word": f"w{len(calls)}", "start": 0.5, "end": 0.9}], "usage": {"cost": 0.001}}

        script = self.root / "script.txt"
        script.write_text("[wry] w1 w2 <stress>w3</stress>")
        with patch.object(transcribe, "post_json", side_effect=fake):
            code, out, _ = run(transcribe.main, [str(audio), "--out", str(self.root / "vo.words.json"),
                                                 "--chunk-seconds", "10", "--script", str(script)])
        self.assertEqual(code, 0)
        result = read_json(self.root / "vo.words.json")
        starts = [word["start"] for word in result["words"]]
        self.assertEqual(len(calls), 3)
        self.assertAlmostEqual(starts[0], 0.5)
        self.assertTrue(9.5 < starts[1] - 0.5 < 10.1, starts)  # chunks begin inside the silent gaps
        self.assertTrue(19.5 < starts[2] - 0.5 < 20.1, starts)
        self.assertEqual(json.loads(out)["script_match"], 1.0)
        self.assertTrue((self.root / "vo.srt").read_text().startswith("1\n00:00:00,500"))
        with patch.object(transcribe, "post_json") as network:  # same audio and settings: reuse, no request
            self.assertEqual(run(transcribe.main, [str(audio), "--out", str(self.root / "vo.words.json"),
                                                   "--chunk-seconds", "10"])[0], 0)
            network.assert_not_called()

    def test_transcribe_checkpoints_paid_chunks_after_a_failure(self):
        audio = self.tone("vo.wav", 25, "if(lt(mod(t\\,10)\\,9.5)\\,sin(2*PI*220*t)*0.3\\,0)")
        out = self.root / "vo.words.json"
        answers = [{"words": [{"word": "one", "start": 0.1, "end": 0.4}]}, AudioError("timeout")]
        with patch.object(transcribe, "post_json", side_effect=answers):
            code, _, err = run(transcribe.main, [str(audio), "--out", str(out), "--chunk-seconds", "10"])
        self.assertEqual(code, 1)
        self.assertIn("timeout", err)
        self.assertTrue(Path(str(out) + ".partial.json").exists())
        later = [{"words": [{"word": "two", "start": 0.1, "end": 0.4}]}, {"words": [{"word": "three", "start": 0.1, "end": 0.4}]}]
        with patch.object(transcribe, "post_json", side_effect=later) as network:
            self.assertEqual(run(transcribe.main, [str(audio), "--out", str(out), "--chunk-seconds", "10"])[0], 0)
        self.assertEqual(network.call_count, 2)  # the first chunk was not paid for again
        self.assertEqual([word["word"] for word in read_json(out)["words"]], ["one", "two", "three"])
        self.assertFalse(Path(str(out) + ".partial.json").exists())

    def test_spec_with_bad_fields_fails_cleanly(self):
        (self.root / "spec.json").write_text(json.dumps({"shots": [{"id": "k01", "dur": None, "layers": []}]}))
        code, _, err = run(timeline.main, ["events", str(self.root / "spec.json")])
        self.assertEqual(code, 1)
        self.assertIn("numeric dur", err)
        (self.root / "spec.json").write_text(json.dumps({"shots": [{"id": "k01", "dur": 1, "camera": [1]}]}))
        self.assertIn("camera must be a dict", run(timeline.main, ["events", str(self.root / "spec.json")])[2])

    def test_transcribe_rejects_models_without_word_times(self):
        audio = self.tone("vo.wav", 2)
        with patch.object(transcribe, "post_json", return_value={"text": "hi"}):
            code, _, err = run(transcribe.main, [str(audio), "--out", str(self.root / "w.json")])
        self.assertEqual(code, 1)
        self.assertIn("no word timestamps", err)

    # ---------- review ----------

    def test_review_maps_times_to_the_film_and_drops_impossible_ones(self):
        film = self.video("film.mp4", 6)
        answer = {"scores": {"overall": 8}, "summary": "ok", "issues": [
            {"time": 1.5, "severity": "low", "category": "sfx-late", "description": "late", "fix": "earlier"},
            {"time": 115, "severity": "high", "category": "style", "description": "invented", "fix": "-"}]}
        reply = {"model": "m", "usage": {"cost": 0.01}, "choices": [{"message": {"content": json.dumps(answer)}}]}
        with patch.object(review, "post_json", return_value=reply) as network:
            code, out, _ = run(review.main, [str(film), "--start", "2", "--duration", "3", "--out",
                                             str(self.root / "review.json"), "--context", "a quiet scene"])
        self.assertEqual(code, 0)
        payload = network.call_args.args[1]
        self.assertIn("intent only", payload["messages"][0]["content"][0]["text"])
        saved = read_json(self.root / "review.json")
        self.assertEqual([issue["time"] for issue in saved["issues"]], [3.5])
        self.assertEqual(len(saved["rejected_issues"]), 1)
        self.assertTrue(saved["automated"])
        self.assertNotIn(KEY, out)

    def test_review_reports_non_json_answers(self):
        film = self.video("film.mp4", 2)
        reply = {"choices": [{"message": {"content": "I liked it"}}]}
        with patch.object(review, "post_json", return_value=reply):
            code, _, err = run(review.main, [str(film)])
        self.assertEqual(code, 1)
        self.assertIn("non-JSON", err)

    # ---------- analyze_refs ----------

    def test_reference_levels_and_study_plan_resume(self):
        refs = self.root / "refs"
        self.video("refs/shorts/a.mp4", 3)
        self.video("refs/long/b.mp4", 4)
        code, out, _ = run(analyze_refs.main, ["levels", "--refs", str(refs), "--out", str(self.root / "levels.json")])
        self.assertEqual(code, 0)
        rows = read_json(self.root / "levels.json")["videos"]
        self.assertEqual(len(rows), 2)
        self.assertIsNotNone(rows[0]["integrated_lufs"])
        plan = analyze_refs.clip_plan(refs)
        self.assertEqual([clip["form"] for clip in plan], ["long"] * 3 + ["short"])
        reply = {"model": "m", "usage": {}, "choices": [{"message": {"content": json.dumps({"sfx": []})}}]}
        with patch.object(analyze_refs, "post_json", return_value=reply) as network:
            self.assertEqual(run(analyze_refs.main, ["study", "--refs", str(refs), "--out", str(self.root / "s.json")])[0], 0)
            self.assertEqual(network.call_count, 4)
            self.assertEqual(run(analyze_refs.main, ["study", "--refs", str(refs), "--out", str(self.root / "s.json")])[0], 0)
            self.assertEqual(network.call_count, 4)  # complete report: nothing re-sent
        self.assertEqual(len(read_json(self.root / "s.json")["results"]), 4)
        with patch.object(analyze_refs, "post_json", side_effect=AudioError("timeout")):
            self.assertEqual(run(analyze_refs.main, ["study", "--refs", str(refs), "--limit-long", "0",
                                                     "--out", str(self.root / "t.json")])[0], 1)
        with patch.object(analyze_refs, "post_json", return_value=reply) as network:
            code, _, err = run(analyze_refs.main, ["study", "--refs", str(refs), "--limit-long", "0", "--out", str(self.root / "t.json")])
            self.assertEqual(code, 1)
            self.assertIn("--retry-pending", err)
            network.assert_not_called()
            self.assertEqual(run(analyze_refs.main, ["study", "--refs", str(refs), "--limit-long", "0", "--retry-pending",
                                                     "--out", str(self.root / "t.json")])[0], 0)
            self.assertEqual(network.call_count, 1)
        self.assertEqual(len(read_json(self.root / "s.json")["results"]), 4)

    # ---------- soundtrack and timeline CLIs ----------

    def test_soundtrack_cli_dry_run_build_and_errors(self):
        spec = {"title": "T", "shots": [{"id": "k01", "dur": 2, "vo": "Hello there.", "layers": [
            {"type": "doc", "kind": "stamp", "at": 0.5, "text": "OK"}]}, {"id": "k02", "dur": 2, "vo": "Bye.", "layers": []}]}
        (self.root / "spec.json").write_text(json.dumps(spec))
        code, _, err = run(timeline.main, ["events", str(self.root / "spec.json"), "--out", str(self.root / "timeline.json"),
                                           "--md", str(self.root / "timeline.md")])
        self.assertEqual(code, 0)
        self.assertIn("events by tier", err)
        self.assertIn("stamp", (self.root / "timeline.md").read_text())
        self.tone("vo.wav", 4)
        self.tone("hit.wav", 0.3, "sin(2*PI*900*t)*0.8")
        plan = {"narration": "vo.wav", "timeline": "timeline.json",
                "assets": {"hit": {"file": "hit.wav"}, "stamp": {"library": "stamp-thud"}},
                "sfx": [{"asset": "hit", "at": "k01+0.5"}, {"asset": "stamp", "at": "k02"}]}
        (self.root / "plan.json").write_text(json.dumps(plan))
        with patch("generate_asset.library_dir", return_value=self.root / "lib"):
            code, out, _ = run(soundtrack.main, ["--plan", str(self.root / "plan.json"), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn('"assets_to_generate": [\n    "stamp"', out)
        self.assertIn("?", out)  # unknown length of an ungenerated asset
        plan["sfx"].pop()
        plan["assets"].pop("stamp")
        (self.root / "plan.json").write_text(json.dumps(plan))
        code, out, err = run(soundtrack.main, ["--plan", str(self.root / "plan.json"), "--out", str(self.root / "mix.wav")])
        self.assertEqual(code, 0, err)
        self.assertIn("QA (long)", err)
        self.assertTrue((self.root / "mix.wav.stems" / "sfx.wav").is_file())
        code, _, err = run(soundtrack.main, ["--plan", str(self.root / "plan.json"), "--video-out", str(self.root / "x.mp4")])
        self.assertEqual(code, 1)
        self.assertIn("needs a picture", err)
        plan["sfx"][0]["at"] = "k09"
        (self.root / "plan.json").write_text(json.dumps(plan))
        code, _, err = run(soundtrack.main, ["--plan", str(self.root / "plan.json"), "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("not in the timeline", err)

    def test_missing_key_fails_before_any_request(self):
        self.tone("vo.wav", 1)
        (self.root / "plan.json").write_text(json.dumps({"narration": "vo.wav", "assets": {
            "m": {"kind": "music", "prompt": "Calm piano.", "seconds": 10}}}))
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": ""}), patch.object(soundtrack, "render_asset") as paid, \
                patch("common.ROOT", self.root):
            code, _, err = run(soundtrack.main, ["--plan", str(self.root / "plan.json"), "--assets-only"])
        self.assertEqual(code, 1)
        self.assertIn("OPENROUTER_API_KEY", err)
        paid.assert_not_called()


if __name__ == "__main__":
    unittest.main()
