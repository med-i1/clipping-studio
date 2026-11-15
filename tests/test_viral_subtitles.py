import tempfile
import unittest
from pathlib import Path

from clipper.viral.models import ClipCandidate, Transcript, TranscriptSegment
from clipper.viral.subtitles import (
    format_srt_time,
    format_vtt_time,
    parse_json3_text,
    parse_srt_text,
    parse_vtt_text,
    to_srt,
    to_vtt,
    write_clip_subtitles,
)


class TimestampTests(unittest.TestCase):
    def test_formats_srt_and_vtt_timestamps(self):
        self.assertEqual(format_srt_time(3661.5), "01:01:01,500")
        self.assertEqual(format_vtt_time(3661.5), "01:01:01.500")
        self.assertEqual(format_srt_time(-2), "00:00:00,000")


class ParserTests(unittest.TestCase):
    def test_parses_vtt_with_tags_and_rolling_duplicates(self):
        text = """WEBVTT

00:00:01.000 --> 00:00:03.000
<v Speaker>Hello</v>

00:00:03.000 --> 00:00:05.000
Hello world

00:00:05.000 --> 00:00:07.000
A different line
"""
        segments = parse_vtt_text(text)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0].text, "Hello world")
        self.assertEqual(segments[1].text, "A different line")

    def test_parses_srt(self):
        text = """1
00:00:01,000 --> 00:00:04,250
First sentence.

2
00:00:04,250 --> 00:00:08,000
Second sentence.
"""
        segments = parse_srt_text(text)
        self.assertEqual(len(segments), 2)
        self.assertAlmostEqual(segments[0].end, 4.25)

    def test_parses_json3(self):
        text = '{"events":[{"tStartMs":1000,"dDurationMs":2000,"segs":[{"utf8":"Hello "},{"utf8":"there"}]}]}'
        segments = parse_json3_text(text)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].text, "Hello there")
        self.assertEqual(segments[0].start, 1.0)
        self.assertEqual(segments[0].end, 3.0)


class SubtitleOutputTests(unittest.TestCase):
    def setUp(self):
        self.transcript = Transcript(
            language="en",
            duration=120,
            segments=[
                TranscriptSegment(1, 10, 20, "First line."),
                TranscriptSegment(2, 20, 30, "Second line."),
            ],
        )
        self.clip = ClipCandidate(
            rank=1,
            title="A title",
            hook="A hook",
            start_time=10,
            end_time=30,
            duration=20,
            viral_score=90,
            category="insight",
            reason="Reason",
            summary="Summary",
            platforms=["tiktok"],
            keywords=["viral", "#clip"],
        )

    def test_srt_and_vtt_are_valid(self):
        srt = to_srt(self.transcript.segments)
        self.assertIn("00:00:10,000 --> 00:00:20,000", srt)
        self.assertIn("00:00:20,000 --> 00:00:30,000", srt)
        self.assertTrue(to_vtt(self.transcript.segments).startswith("WEBVTT"))

    def test_writes_clip_files_with_relative_timing(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = write_clip_subtitles(self.clip, self.transcript, Path(temp), "clip_001")
            self.assertEqual(set(paths), {"srt", "vtt", "txt", "transcript"})
            srt = paths["srt"].read_text(encoding="utf-8")
            self.assertIn("00:00:00,000 --> 00:00:10,000", srt)
            caption = paths["txt"].read_text(encoding="utf-8")
            self.assertIn("#viral", caption)
            self.assertIn("#clip", caption)


if __name__ == "__main__":
    unittest.main()
