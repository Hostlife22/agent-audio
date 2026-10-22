import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from common import AudioError, read_json  # noqa: E402
import qa  # noqa: E402
import soundtrack  # noqa: E402
import timeline  # noqa: E402
import transcribe  # noqa: E402

TABLE = read_json(timeline.EVENT_TABLE)["events"]
SPEC = {"title": "Test", "fps": 30, "shots": [
    {"id": "k01", "dur": 2.5, "vo": "October, 1924. Reporters wait.", "bg": {"kind": "set"},
     "layers": [{"type": "fx", "kind": "flash", "at": 1.0, "dur": 0.1}, {"type": "fx", "kind": "particles"},
                {"type": "doc", "kind": "stamp", "text": "SAFE", "at": 2.0}]},
    {"id": "k02", "dur": 3.0, "vo": "Workers started to die.", "transition": {"kind": "whip", "dir": "left"},
     "camera": {"punch": [{"at": 1.5, "amount": 0.1}]}, "grade": {"preset": "solemn", "at": 0.5}, "bg": {"kind": "factory"},
     "layers": [{"type": "bean", "who": "midgley", "enter": "drop-squash", "at": 0.2, "takes": [{"at": 2.0, "kind": "jolt"}]}]},
]}
WORDS = [{"word": w, "start": s, "end": e} for w, s, e in [
    ("October,", 0.2, 0.7), ("1924.", 0.8, 1.4), ("Reporters", 1.6, 2.0), ("wait.", 2.0, 2.3),
    ("Workers", 3.0, 3.4), ("started", 3.4, 3.8), ("to", 3.8, 3.9), ("die.", 3.9, 4.3)]]


class TimelineTests(unittest.TestCase):
    def test_events_get_absolute_times_tiers_and_suggestions(self):
        result = timeline.build_timeline(SPEC, TABLE)
        first, second = result["shots"]
        self.assertEqual((second["start"], second["end"]), (2.5, 5.5))
        kinds = {item["kind"]: item for shot in result["shots"] for item in shot["events"]}
        self.assertEqual(kinds["stamp"]["time"], 2.0)
        self.assertEqual(kinds["stamp"]["tier"], "A")
        self.assertEqual(kinds["transition:whip"]["time"], 2.5)
        self.assertEqual(kinds["camera:punch"]["time"], 4.0)
        self.assertEqual(kinds["camera:punch"]["sound"], "impact-hit")
        self.assertEqual(kinds["fx:particles"]["tier"], "C")
        self.assertEqual(kinds["take:jolt"]["time"], 4.5)
        self.assertEqual(kinds["enter:drop-squash"]["tier"], "B")
        self.assertEqual(kinds["grade-change"]["time"], 3.0)

    def test_every_suggested_sound_exists_in_the_library(self):
        library = read_json(timeline.ROOT / "references" / "sfx-library.json")["sounds"]
        for kind, entry in TABLE.items():
            sound = entry["sound"]
            if sound and not sound.startswith(("music:", "ambience:")):
                self.assertIn(sound, library, kind)

    def test_words_give_speech_spans_and_pauses(self):
        result = timeline.build_timeline(SPEC, TABLE, WORDS)
        self.assertEqual(result["shots"][0]["speech"], [0.2, 2.3])
        self.assertEqual(result["shots"][1]["speech"], [3.0, 4.3])
        self.assertEqual(result["speech_gaps"], [{"start": 2.3, "end": 3.0, "duration": 0.7}])
        markdown = timeline.to_markdown(result, "AB")
        self.assertIn("*k02", markdown)  # background change marks a music-section candidate
        self.assertNotIn("fx:particles", markdown)

    def test_beats_plan_keeps_spoken_text_per_shot(self):
        plan = timeline.narration_plan(SPEC, "ironic")
        self.assertEqual([beat["shot"] for beat in plan["beats"]], ["k01", "k02"])
        self.assertEqual(plan["beats"][1]["text"], "Workers started to die.")
        with self.assertRaises(AudioError):
            timeline.narration_plan({"shots": [{"id": "x", "dur": 1}]}, "ironic")


STORYBOARD = """# Test film
Central question: why?
Music map:
- k01-k01: cold mystery, muted pizzicato
- k02: hard cut to dark drone, silence before "die"

**Shot 1** 🎬
- Voiceover: "October, 1924. Reporters wait."
- On screen: flashbulbs pop
- Sound: flashbulb pops on the flashes; low crowd murmur

**Shot 2**
- Voiceover: "Workers started to die."
Sound: music cuts dead on the glitch; heavy thud on "die"

**Shot 9**
- Voiceover: "Not in the film."
- Sound: should be reported as unmatched
"""


class SoundPlanTests(unittest.TestCase):
    def test_storyboard_sound_lines_map_to_shot_ids_and_brief(self):
        plan = timeline.sound_plan(STORYBOARD)
        self.assertEqual(plan["shots"]["k01"], "flashbulb pops on the flashes; low crowd murmur")
        self.assertEqual(plan["shots"]["k02"], 'music cuts dead on the glitch; heavy thud on "die"')
        self.assertEqual(plan["brief"], ["Music map:", "- k01-k01: cold mystery, muted pizzicato",
                                         '- k02: hard cut to dark drone, silence before "die"'])

    def test_timeline_shows_planned_sound_next_to_events(self):
        result = timeline.build_timeline(SPEC, TABLE, sound=timeline.sound_plan(STORYBOARD))
        self.assertEqual(result["shots"][1]["sound_plan"], 'music cuts dead on the glitch; heavy thud on "die"')
        self.assertEqual(result["sound_plan_unmatched"], ["k09"])
        markdown = timeline.to_markdown(result, "AB")
        self.assertIn("    SOUND PLAN: flashbulb pops", markdown)
        self.assertIn("- k02: hard cut to dark drone", markdown)
        self.assertIn("k09", markdown)

    def test_storyboard_without_sound_lines_changes_nothing(self):
        plain = timeline.build_timeline(SPEC, TABLE, sound=timeline.sound_plan("**Shot 1**\n- Voiceover: hi"))
        self.assertNotIn("sound_plan", plain["shots"][0])
        self.assertEqual(plain, timeline.build_timeline(SPEC, TABLE))


class AnchorTests(unittest.TestCase):
    def setUp(self):
        self.anchor = soundtrack.Anchors(timeline.build_timeline(SPEC, TABLE), WORDS + [
            {"word": "die", "start": 9.0, "end": 9.3}])

    def test_numbers_shots_offsets_and_ends(self):
        self.assertEqual(self.anchor(12.5, "t"), 12.5)
        self.assertEqual(self.anchor("7", "t"), 7.0)
        self.assertEqual(self.anchor("k02", "t"), 2.5)
        self.assertAlmostEqual(self.anchor("k02+1.25", "t"), 3.75)
        self.assertAlmostEqual(self.anchor("k01:end-0.5", "t"), 2.0)

    def test_words_occurrence_nearest_and_end(self):
        self.assertEqual(self.anchor("word:die", "t"), 3.9)
        self.assertEqual(self.anchor("word:die@2", "t"), 9.0)
        self.assertEqual(self.anchor("word:die~8", "t"), 9.0)
        self.assertAlmostEqual(self.anchor("word:die:end+0.2", "t"), 4.5)
        self.assertEqual(self.anchor("word:october", "t"), 0.2)

    def test_numeric_shot_ids_and_duplicates(self):
        spec = {"shots": [{"id": "12", "dur": 1.0, "layers": []}, {"id": "x", "dur": 1.0, "layers": []},
                          {"id": "x", "dur": 1.0, "layers": []}]}
        anchor = soundtrack.Anchors(timeline.build_timeline(spec, TABLE))
        self.assertEqual(anchor("12", "t"), 0.0)  # the shot, not 12 seconds
        self.assertEqual(anchor(12, "t"), 12.0)   # a JSON number is always seconds
        with self.assertRaisesRegex(AudioError, "more than once"):
            anchor("x+0.5", "t")

    def test_unknown_anchors_explain_themselves(self):
        for value, message in [("k99", "not in the timeline"), ("word:pigeon", "not found"),
                               ("word:die@3", "occurs only"), ("word:die@0", "count from @1"),
                               (None, "missing")]:
            with self.subTest(value=value), self.assertRaisesRegex(AudioError, message):
                self.anchor(value, "t")
        with self.assertRaisesRegex(AudioError, "need \"timeline\""):
            soundtrack.Anchors()("k01", "t")


class TranscriptTests(unittest.TestCase):
    def test_srt_cues_break_at_sentences_and_pauses(self):
        cues = transcribe.subtitle_cues(WORDS)
        self.assertEqual([cue["text"] for cue in cues], ["October, 1924.", "Reporters wait.", "Workers started to die."])
        srt = transcribe.to_srt(cues)
        self.assertTrue(srt.startswith("1\n00:00:00,200 --> 00:00:01,400\nOctober, 1924.\n"))

    def test_script_check_finds_spoken_tags_and_missing_words(self):
        with tempfile.TemporaryDirectory() as folder:
            plan = Path(folder) / "plan.json"
            plan.write_text(json.dumps({"beats": [{"text": "[wry] Workers <stress>started</stress> to [pause] die."}]}))
            expected = transcribe.script_words(plan)
        self.assertEqual(expected, ["workers", "started", "to", "die"])
        heard = [{"word": w, "start": i, "end": i + .5} for i, w in enumerate(["Workers", "started", "pause", "die."])]
        report = transcribe.compare(expected, heard)
        self.assertEqual(report["differences"], [{"type": "replace", "time": 2, "script": "to", "heard": "pause"}])

    def test_chunk_cut_prefers_the_quietest_frame(self):
        levels = [-20.0] * 100
        levels[42] = -70.0
        self.assertAlmostEqual(transcribe.quietest_point(levels, 0.05, 2.5, 1.0), 2.125)


class QaHelperTests(unittest.TestCase):
    def test_runs_and_range_checks(self):
        self.assertEqual(qa.runs([1, 2, 3, 7, 8, 10]), [[1, 3], [7, 8], [10, 10]])
        self.assertEqual(qa.check(20, 18, 25), "ok")
        self.assertEqual(qa.check(30, 18, 25), "high")
        self.assertEqual(qa.speech_frames([-20] * 50, WORDS[:1], 50), [2, 3, 4, 5, 6])


if __name__ == "__main__":
    unittest.main()
