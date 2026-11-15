import unittest

from clipper.viral import errors
from clipper.viral.config import ViralConfig
from clipper.viral.errors import PipelineError
from clipper.viral.models import JobOptions, Transcript, TranscriptSegment
from clipper.viral.timing import validate_clips


def make_transcript(duration=120.0):
    segments = []
    for index in range(int(duration // 10)):
        segments.append(
            TranscriptSegment(
                id=index + 1,
                start=float(index * 10),
                end=float((index + 1) * 10),
                text=f"This is sentence number {index + 1} with enough words.",
            )
        )
    return Transcript(language="en", duration=duration, segments=segments)


def proposal(start, end, score=80, title="A clip", hook="A hook"):
    return {
        "rank": 1,
        "title": title,
        "hook": hook,
        "start_time": start,
        "end_time": end,
        "duration": end - start,
        "viral_score": score,
        "category": "insight",
        "reason": "Because it is strong.",
        "summary": "Summary.",
        "platforms": ["tiktok"],
        "keywords": ["clip"],
        "confidence": 0.8,
    }


class TimingValidationTests(unittest.TestCase):
    def setUp(self):
        self.config = ViralConfig()
        self.options = JobOptions(number_of_clips=3, min_clip_duration=20, max_clip_duration=90)
        self.transcript = make_transcript()

    def test_snaps_mid_sentence_start_and_end(self):
        clips = validate_clips([proposal(25, 45)], self.transcript, self.options, self.config)
        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0].start_time, 20.0)
        self.assertEqual(clips[0].end_time, 50.0)
        self.assertEqual(clips[0].duration, 30.0)
        self.assertIn("snapped_start_to_sentence", clips[0].adjustments)
        self.assertIn("snapped_end_to_sentence", clips[0].adjustments)

    def test_clamps_out_of_range_timestamps(self):
        clips = validate_clips([proposal(-5, 500)], self.transcript, self.options, self.config)
        self.assertEqual(len(clips), 1)
        self.assertGreaterEqual(clips[0].start_time, 0)
        self.assertLessEqual(clips[0].end_time, 120)
        self.assertLessEqual(clips[0].duration, 90)
        self.assertIn("clamped_start_to_zero", clips[0].adjustments)
        self.assertIn("clamped_end_to_video_duration", clips[0].adjustments)

    def test_extends_a_too_short_clip_to_minimum(self):
        clips = validate_clips([proposal(22, 28)], self.transcript, self.options, self.config)
        self.assertEqual(len(clips), 1)
        self.assertGreaterEqual(clips[0].duration, 19.9)
        self.assertLessEqual(clips[0].duration, 90)

    def test_shortens_a_too_long_clip_to_maximum(self):
        clips = validate_clips([proposal(0, 200)], self.transcript, self.options, self.config)
        self.assertEqual(len(clips), 1)
        self.assertLessEqual(clips[0].duration, 90.1)

    def test_rejects_large_overlap(self):
        clips = validate_clips(
            [proposal(0, 40, score=95, title="First"), proposal(20, 60, score=70, title="Second")],
            self.transcript,
            self.options,
            self.config,
        )
        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0].title, "First")

    def test_recomputes_model_duration(self):
        bad = proposal(10, 40)
        bad["duration"] = 999
        clips = validate_clips([bad], self.transcript, self.options, self.config)
        self.assertAlmostEqual(clips[0].duration, 30.0)

    def test_keeps_only_requested_number_sorted_by_score(self):
        clips = validate_clips(
            [
                proposal(0, 30, score=60, title="Low"),
                proposal(30, 60, score=90, title="High"),
                proposal(60, 90, score=75, title="Mid"),
            ],
            self.transcript,
            JobOptions(number_of_clips=2, min_clip_duration=20, max_clip_duration=90),
            self.config,
        )
        self.assertEqual([clip.title for clip in clips], ["High", "Mid"])
        self.assertEqual([clip.rank for clip in clips], [1, 2])

    def test_raises_when_nothing_survives(self):
        with self.assertRaises(PipelineError) as ctx:
            validate_clips([proposal(500, 600)], self.transcript, self.options, self.config)
        self.assertEqual(ctx.exception.code, errors.NO_VALID_CLIPS)

    def test_invalid_proposal_is_skipped(self):
        bad = proposal(10, 20)
        bad["end_time"] = 5
        with self.assertRaises(PipelineError) as ctx:
            validate_clips([bad], self.transcript, self.options, self.config)
        self.assertEqual(ctx.exception.code, errors.NO_VALID_CLIPS)


if __name__ == "__main__":
    unittest.main()
