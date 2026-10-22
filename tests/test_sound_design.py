import base64
import contextlib
import io
import json
import math
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from common import AudioError, decode_mono, frame_levels  # noqa: E402
import generate_asset as asset  # noqa: E402
import mix  # noqa: E402
import soundtrack  # noqa: E402

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def event(data):
    return ["data: " + json.dumps(data), ""]


class AssetRequestTests(unittest.TestCase):
    def test_music_stream_extracts_audio_and_metadata_ignoring_text(self):
        first, second = b"ID3" + b"a" * 199, b"b" * 200
        lines = [": heartbeat", ""]
        for part in (first, second):
            lines += event({"id": "gen-test", "choices": [{"delta": {"content": "[[A0]]",
                             "audio": {"data": base64.b64encode(part).decode()}}}]})
        lines += event({"usage": {"cost": .08}, "choices": []}) + ["data: [DONE]", ""]
        body, metadata = asset.parse_music_stream([line.encode() for line in lines])
        self.assertEqual(body, first + second)
        self.assertEqual(metadata, {"generation_id": "gen-test", "usage": {"cost": .08}})

    def test_music_stream_rejects_error_empty_and_truncated_audio(self):
        cases = [event({"error": {"message": "provider failed"}}),
                 event({"choices": [{"delta": {"content": "not audio"}}]}) + ["data: [DONE]", ""],
                 event({"choices": [{"delta": {"audio": {"data": base64.b64encode(b"ID3" + b"a" * 200).decode()}}}]})]
        for lines in cases:
            with self.subTest(lines=str(lines)[:60]), self.assertRaises(AudioError):
                asset.parse_music_stream(lines)

    def test_payloads_respect_model_limits_and_never_send_a_voice(self):
        sfx = asset.make_payload("sfx", "A short wooden knock.", 1)
        self.assertEqual(set(sfx), {"model", "input", "response_format"})
        clip = asset.make_payload("music", "Sparse playful pizzicato.", 12)
        self.assertEqual(clip["model"], asset.MUSIC_MODELS["clip"])
        self.assertTrue(clip["stream"])
        with self.assertRaises(AudioError):
            asset.make_payload("music", "Too long for a clip.", 90)
        pro = asset.make_payload("music", "[0:00 - 0:30] Intro.", 90, "pro")
        self.assertEqual(pro["model"], asset.MUSIC_MODELS["pro"])
        self.assertIn("about 90 seconds", pro["messages"][0]["content"])
        self.assertIn("no voice of any kind", pro["messages"][0]["content"])

    def test_dry_run_never_reads_credentials_or_calls_provider(self):
        with patch.object(asset, "resolve_key") as key, patch.object(asset, "request_audio") as network, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(asset.main(["--kind", "music", "--prompt", "Curious underscore.", "--seconds", "15", "--dry-run"]), 0)
            self.assertEqual(asset.main(["--library", "stamp-thud", "--dry-run"]), 0)
        key.assert_not_called()
        network.assert_not_called()

    def test_every_library_sound_builds_a_valid_request(self):
        sounds = json.loads(asset.LIBRARY_FILE.read_text())["sounds"]
        for name, entry in sounds.items():
            with self.subTest(name=name):
                asset.make_payload("sfx", entry["prompt"], entry["seconds"])
        with self.assertRaisesRegex(AudioError, "Unknown library sound"):
            asset.library_entry("no-such-sound")


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg/ffprobe required")
class AudioFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def sine(self, name, frequency, seconds, volume=1.0):
        path = self.root / name
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"sine=frequency={frequency}:duration={seconds}:sample_rate=48000",
                        "-af", f"volume={volume}", str(path)], check=True)
        return path

    def gated_voice(self, name, seconds, on):
        """440 Hz 'speech' only inside the (start, end) windows in `on`."""
        path, rate = self.root / name, 48000
        with wave.open(str(path), "wb") as stream:
            stream.setparams((1, 2, rate, 0, "NONE", "not compressed"))
            samples = [int(.3 * 32767 * math.sin(2 * math.pi * 440 * i / rate))
                       if any(a <= i / rate < b for a, b in on) else 0 for i in range(int(rate * seconds))]
            stream.writeframes(struct.pack("<" + "h" * len(samples), *samples))
        return path

    def levels(self, path, window=0.1):
        return frame_levels(decode_mono(path, 8000), 8000, window)


class AssetCacheTests(AudioFixture):
    def test_paid_source_survives_conversion_failure_and_resume_needs_no_key(self):
        source = self.sine("fixture.mp3", 440, .4)
        output = self.root / "effect.wav"
        with patch.object(asset, "request_audio", return_value=(source.read_bytes(), {})) as network, \
                patch.object(asset, "convert_asset", side_effect=AudioError("disk failure")):
            with self.assertRaisesRegex(AudioError, "disk failure"):
                asset.render_asset("sfx", "A chime.", 2, output, lambda: "fake-key")
            self.assertEqual(network.call_count, 1)
        key = unittest.mock.Mock()
        with patch.object(asset, "request_audio") as network:
            result = asset.render_asset("sfx", "A chime.", 2, output, key, resume=True)
            network.assert_not_called()
        key.assert_not_called()
        self.assertTrue(result["complete"])
        self.assertLess(result["duration_seconds"], 1)  # never padded to the requested 2 s
        with patch.object(asset, "request_audio") as network:
            self.assertTrue(asset.render_asset("sfx", "A chime.", 2, output, key)["reused"])
            with self.assertRaisesRegex(AudioError, "different prompt"):
                asset.render_asset("sfx", "A different sound.", 2, output, key, resume=True)
            network.assert_not_called()

    def test_interrupted_request_is_never_retried_automatically(self):
        output = self.root / "unknown.wav"
        with patch.object(asset, "request_audio", side_effect=AudioError("timeout")):
            with self.assertRaisesRegex(AudioError, "timeout"):
                asset.render_asset("sfx", "Knock.", 1, output, lambda: "fake")
        with patch.object(asset, "request_audio") as network:
            with self.assertRaisesRegex(AudioError, "No automatic retry"):
                asset.render_asset("sfx", "Knock.", 1, output, lambda: "fake", resume=True)
            network.assert_not_called()


class MusicSectionAndSpeechTests(AudioFixture):
    def test_sections_snap_to_the_real_change_and_flag_speech(self):
        # 6 s quiet low tone, then 6 s loud bright tone: the change is at 6.0 s, the prompt asked for 5.0 s.
        path = self.root / "track.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "aevalsrc=if(lt(t\\,6)\\,0.05*sin(2*PI*150*t)\\,0.5*sin(2*PI*2500*t)):s=16000:d=12",
                        str(path)], check=True)
        import music_sections
        found = music_sections.refine(path, {"calm": 0, "loud": 5.0})
        self.assertAlmostEqual(found["loud"]["start"], 6.0, delta=0.15)
        self.assertGreater(found["loud"]["strength"], 1.0)
        import transcribe
        words = {"words": [{"word": w, "start": 7 + i * .3, "end": 7.2 + i * .3}
                           for i, w in enumerate(["[music]", "dragons", "off", "the", "cars"])]}
        manifest = {"kind": "music", "complete": True}
        manifest_path = Path(str(path) + ".manifest.json")
        with patch.object(transcribe, "post_json", return_value=words), contextlib.redirect_stderr(io.StringIO()) as err:
            result = asset.check_speech("music", path, manifest, manifest_path, lambda: "key")
        self.assertEqual(result["speech_check"]["spans"][0]["words"], "dragons off the cars")
        self.assertIn("speech heard", err.getvalue())
        self.assertEqual(asset.speech_in(path)[0]["start"], 7.3)
        with patch.object(transcribe, "post_json") as network:  # recorded once, never re-checked
            asset.check_speech("music", path, result, manifest_path, lambda: "key")
            network.assert_not_called()
        self.timeline_plan(path, found)

    def timeline_plan(self, track, found):
        self.gated_voice("voice.wav", 12, [(1, 2)])
        (self.root / "plan.json").write_text(json.dumps({
            "narration": "voice.wav",
            "assets": {"score": {"file": "track.wav", "sections": {"calm": 0, "loud": 5.0}}},
            "music": [{"asset": "score", "section": "loud", "start": 0, "end": 4}]}))
        plan = soundtrack.load_plan(self.root / "plan.json")
        specs = soundtrack.asset_specs(plan)
        files = soundtrack.ensure_assets(specs, lambda: "unused")
        cues, _, warnings = soundtrack.resolve_cues(plan, files, 12, soundtrack.Anchors(),
                                                    soundtrack.section_starts(specs, files))
        self.assertAlmostEqual(cues[0]["source_start"], found["loud"]["start"])
        self.assertTrue(any("speech at 7.3" in warning for warning in warnings), warnings)
        with self.assertRaisesRegex(AudioError, "no section 'intro'"):
            plan["raw"]["music"][0]["section"] = "intro"
            soundtrack.resolve_cues(plan, files, 12, soundtrack.Anchors(), soundtrack.section_starts(specs, files))

    def test_music_library_themes_are_valid_and_cached_under_music(self):
        themes = json.loads(asset.MUSIC_LIBRARY_FILE.read_text())["themes"]
        for name in themes:
            with self.subTest(name=name):
                _, _, model, payload = asset.library_request(name, "music")
                self.assertEqual(model, "pro")
                self.assertNotIn("narrat", payload["messages"][0]["content"].lower())
                self.assertEqual(asset.library_path(name, "music").parent.name, "music")
        with self.assertRaisesRegex(AudioError, "Unknown library music theme"):
            asset.library_entry("polka-moon", "music")


class SoundtrackTests(AudioFixture):
    def write_plan(self, document, name="plan.json"):
        path = self.root / name
        path.write_text(json.dumps(document))
        return path

    def timeline(self):
        shots = [{"id": "k01", "start": 0.0, "end": 2.0}, {"id": "k02", "start": 2.0, "end": 4.0}]
        (self.root / "timeline.json").write_text(json.dumps({"duration_seconds": 4.0, "shots": shots}))

    def test_cues_land_on_shot_anchors_and_dips_silence_the_music(self):
        self.timeline()
        self.gated_voice("voice.wav", 4, [(3.2, 3.8)])
        self.sine("bed.wav", 1700, 6, .3)
        self.sine("hit.wav", 900, .2, .8)
        plan = soundtrack.load_plan(self.write_plan({
            "narration": "voice.wav", "timeline": "timeline.json", "profile": "long",
            "assets": {"bed": {"file": "bed.wav"}, "hit": {"file": "hit.wav"}},
            "music": [{"asset": "bed", "start": 0, "end": "k02:end", "fade_in": 0, "fade_out": 0.05}],
            "sfx": [{"asset": "hit", "at": "k02+0.5"}],
            "dips": [{"start": "k01+1.0", "end": "k01+1.6", "ramp_in": 0.02, "ramp_out": 0.02}]}))
        files = soundtrack.ensure_assets(soundtrack.asset_specs(plan), lambda: self.fail("no key needed"))
        result = soundtrack.build(plan, files, self.root / "out" / "final.wav")
        stems = Path(result["stems"])
        sfx, music = self.levels(stems / "sfx.wav"), self.levels(stems / "music.wav")
        loud = [index for index, level in enumerate(sfx) if level > -50]
        self.assertEqual(loud[0], 25)  # k02 starts at 2.0 s, +0.5 s -> frame 25
        self.assertLess(music[13], -60)  # inside the dip (1.0-1.6 s)
        self.assertGreater(music[5], -40)
        self.assertGreater(music[20], -40)  # recovers after the dip
        self.assertAlmostEqual(result["duration_seconds"], 4.0, places=2)
        self.assertEqual(len(self.levels(stems / "premix.wav")), 40)

    def test_music_ducks_under_voice_and_recovers(self):
        self.gated_voice("voice.wav", 4, [(1, 2)])
        self.sine("bed.wav", 1700, 4, .3)
        plan = soundtrack.load_plan(self.write_plan({
            "narration": "voice.wav", "assets": {"bed": {"file": "bed.wav"}},
            "music": [{"asset": "bed", "start": 0, "fade_in": 0, "fade_out": 0}]}))
        files = soundtrack.ensure_assets(soundtrack.asset_specs(plan), lambda: "unused")
        result = soundtrack.build(plan, files, self.root / "ducked.wav", run_qa=False)
        music = self.levels(Path(result["stems"]) / "music.wav")
        before, during, after = music[6], music[16], music[34]
        self.assertLess(during, before - 4)
        self.assertGreater(after, before - 1.5)

    def test_review_fixes_guard_inputs_and_edge_cases(self):
        self.timeline()
        self.gated_voice("voice.wav", 4, [(0.5, 1.5)])
        self.gated_voice("silent.wav", 4, [])
        self.sine("bed.wav", 1700, 6, .3)
        base = {"narration": "voice.wav", "timeline": "timeline.json", "assets": {"bed": {"file": "bed.wav"}}}

        def build(extra, out="final.wav", name="p.json"):
            plan = soundtrack.load_plan(self.write_plan({**base, **extra}, name))
            files = soundtrack.ensure_assets(soundtrack.asset_specs(plan), lambda: "unused")
            return soundtrack.build(plan, files, self.root / out, run_qa=False)

        with self.assertRaisesRegex(AudioError, "overwrite an input"):
            build({"music": [{"asset": "bed"}]}, out="bed.wav")
        with self.assertRaisesRegex(AudioError, "silent"):
            build({"narration": "silent.wav"})
        with self.assertRaisesRegex(AudioError, "loop replays"):
            build({"music": [{"asset": "bed", "loop": True, "source_start": 1}]})
        with self.assertRaisesRegex(AudioError, "within the film"):
            build({"music": [{"asset": "bed", "start": 9}]})
        short = build({"music": [{"asset": "bed", "start": "k02", "end": "k02+0.2"}]}, out="short.wav")
        cue = short["cue_sheet"][0]
        self.assertIn("0.20s", cue)  # default fades clamp to a 0.2 s cue instead of failing

    def test_identical_assets_are_requested_once_and_changes_get_new_files(self):
        self.gated_voice("voice.wav", 1, [(0.2, 0.8)])
        plan = soundtrack.load_plan(self.write_plan({"narration": "voice.wav", "assets": {
            "a": {"library": "pop"}, "b": {"library": "pop"},
            "c": {"kind": "sfx", "prompt": "One knock.", "seconds": 1}}}))
        specs = soundtrack.asset_specs(plan)
        self.assertEqual(specs["a"]["path"], specs["b"]["path"])
        changed = soundtrack.load_plan(self.write_plan({"narration": "voice.wav", "assets": {
            "c": {"kind": "sfx", "prompt": "One knock.", "seconds": 2}}}, "changed.json"))
        self.assertNotEqual(soundtrack.asset_specs(changed)["c"]["path"], specs["c"]["path"])
        source = self.sine("src.mp3", 500, .3)
        with patch.object(asset, "library_dir", return_value=self.root / "lib"), \
                patch.object(asset, "request_audio", return_value=(source.read_bytes(), {})) as network:
            specs = soundtrack.asset_specs(plan)
            soundtrack.ensure_assets(specs, lambda: "key")
        self.assertEqual(network.call_count, 2)  # pop once (two names), knock once

    def test_lock_blocks_a_second_payer_and_rejected_bytes_are_kept(self):
        output = self.root / "fx.wav"
        Path(str(output) + ".lock").write_text("")
        with patch.object(asset, "request_audio") as network:
            with self.assertRaisesRegex(AudioError, "Another run"):
                asset.render_asset("sfx", "Knock.", 1, output, lambda: "key")
            network.assert_not_called()
        Path(str(output) + ".lock").unlink()
        with patch.object(asset, "request_audio", side_effect=asset.RejectedAudio("not mp3", b"{json}")):
            with self.assertRaisesRegex(AudioError, "kept in fx.wav.rejected"):
                asset.render_asset("sfx", "Knock.", 1, output, lambda: "key")
        self.assertEqual(Path(str(output) + ".rejected").read_bytes(), b"{json}")
        self.assertFalse(Path(str(output) + ".lock").exists())

    def test_legacy_unhashed_assets_are_adopted_without_paying(self):
        source = self.sine("src.mp3", 500, .3)
        legacy = self.root / "lib" / "pop.wav"
        legacy.parent.mkdir()
        with patch.object(asset, "request_audio", return_value=(source.read_bytes(), {})):
            entry = asset.library_entry("pop")
            asset.render_asset("sfx", entry["prompt"], entry["seconds"], legacy, lambda: "key")
        with patch.object(asset, "library_dir", return_value=self.root / "lib"), \
                patch.object(asset, "request_audio") as network:
            output, result = asset.render_library("pop", lambda: self.fail("no key"))
            network.assert_not_called()
        self.assertTrue(output.name.startswith("pop-") and output.is_file())
        self.assertFalse(legacy.exists())

    def test_bad_plans_fail_before_any_paid_request(self):
        self.gated_voice("voice.wav", 1, [])
        cases = [({"narration": "voice.wav", "surprise": 1}, "unknown field"),
                 ({"narration": "voice.wav", "profile": "tiktok"}, "profile"),
                 ({"narration": "voice.wav", "assets": {"m": {"kind": "music", "prompt": "x", "seconds": 300}}}, "seconds"),
                 ({"narration": "voice.wav", "assets": {"m": {"library": "nope"}}}, "Unknown library")]
        for number, (document, message) in enumerate(cases):
            with self.subTest(message=message), self.assertRaisesRegex(AudioError, message):
                plan = soundtrack.load_plan(self.write_plan(document, f"bad{number}.json"))
                soundtrack.asset_specs(plan)

    def test_video_mux_copies_picture_and_matches_length(self):
        self.gated_voice("voice.wav", 2, [(0.2, 1.5)])
        video = self.root / "film.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=64x36:d=2.5:r=10",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)], check=True)
        plan = soundtrack.load_plan(self.write_plan({"narration": "voice.wav", "video": "film.mp4"}))
        result = soundtrack.build(plan, {}, self.root / "final.wav", self.root / "scored.mp4", run_qa=False)
        self.assertAlmostEqual(result["mux"]["video_seconds"], 2.5, places=1)
        streams = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name",
                                           "-of", "csv=p=0", str(self.root / "scored.mp4")], text=True).split()
        self.assertEqual(streams, ["h264", "aac"])


class ExamplePlanTests(unittest.TestCase):
    def test_example_plan_uses_only_known_fields_and_valid_assets(self):
        plan = json.loads((Path(__file__).resolve().parents[1] / "examples" / "soundtrack-plan.json").read_text())
        soundtrack.check_fields(plan, soundtrack.PLAN_FIELDS, "plan")
        for name, entry in plan["assets"].items():
            soundtrack.check_fields(entry, soundtrack.ASSET_FIELDS, name)
            if "library" in entry:
                asset.library_entry(entry["library"])
            else:
                asset.make_payload(entry["kind"], entry["prompt"], entry["seconds"], entry.get("model", "clip"))
        for bus, allowed in (("music", soundtrack.BED_FIELDS), ("ambience", soundtrack.BED_FIELDS),
                             ("sfx", soundtrack.SFX_FIELDS), ("dips", soundtrack.DIP_FIELDS)):
            for entry in plan.get(bus, []):
                soundtrack.check_fields(entry, allowed, bus)
                if "asset" in entry:
                    self.assertIn(entry["asset"], plan["assets"])


class DipExpressionTests(unittest.TestCase):
    def evaluate(self, expression, t):
        return eval(expression, {"clip": lambda x, a, b: max(a, min(b, x)), "min": min, "t": t})

    def test_envelope_cuts_fast_and_recovers_after_end(self):
        expression = mix.dip_expression([{"start": 10, "end": 12, "gain_db": -60, "ramp_in": 0.04, "ramp_out": 0.4}])
        self.assertAlmostEqual(self.evaluate(expression, 9.9), 1.0)
        self.assertLess(self.evaluate(expression, 10.0), 0.01)
        self.assertLess(self.evaluate(expression, 11.9), 0.01)
        self.assertAlmostEqual(self.evaluate(expression, 12.2), 0.5, places=2)
        self.assertAlmostEqual(self.evaluate(expression, 12.5), 1.0)


if __name__ == "__main__":
    unittest.main()
