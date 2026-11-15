import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clipper.viral import errors
from clipper.viral.config import ViralConfig
from clipper.viral.errors import PipelineError
from clipper.viral.transcription import (
    CaptionTranscriber,
    FallbackTranscriber,
    NoCaptionsAvailable,
    OpenAIWhisperTranscriber,
    TranscriptOptions,
    build_transcriber,
    clean_transcript_segments,
)
from clipper.viral.models import DownloadedVideo, Transcript, TranscriptSegment


class TranscriptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = ViralConfig(
            project_root=self.root,
            output_dir=self.root / "out",
            temp_dir=self.root / "tmp",
            cache_dir=self.root / "cache",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_auto_without_keys_or_local_package_uses_captions(self):
        with patch("clipper.viral.transcription.importlib.util.find_spec", return_value=None):
            transcriber = build_transcriber(self.config)
        self.assertIsInstance(transcriber, FallbackTranscriber)
        self.assertEqual([backend.name for backend in transcriber.backends], ["captions"])
        self.assertIsInstance(transcriber.backends[0], CaptionTranscriber)

    def test_openai_provider_requires_a_key(self):
        self.config.transcription_provider = "openai"
        self.config.transcription_api_key = ""
        with self.assertRaises(PipelineError) as caught:
            build_transcriber(self.config)
        self.assertEqual(caught.exception.code, errors.CONFIGURATION_ERROR)

    def test_local_provider_requires_faster_whisper(self):
        self.config.transcription_provider = "local"
        with patch("clipper.viral.transcription.importlib.util.find_spec", return_value=None):
            with self.assertRaises(PipelineError) as caught:
                build_transcriber(self.config)
        self.assertEqual(caught.exception.code, errors.CONFIGURATION_ERROR)

    def test_none_provider_is_disabled(self):
        self.config.transcription_provider = "none"
        with self.assertRaises(PipelineError) as caught:
            build_transcriber(self.config)
        self.assertEqual(caught.exception.code, errors.TRANSCRIPTION_UNAVAILABLE)

    def test_fallback_tries_caption_then_whisper_and_reports_failure(self):
        class MissingCaptions:
            name = "captions"

            def transcribe(self, video, options):
                from clipper.viral.transcription import NoCaptionsAvailable

                raise NoCaptionsAvailable("none")

        class Working:
            name = "whisper"

            def transcribe(self, video, options):
                return Transcript("en", 10, [TranscriptSegment(1, 0, 5, "Hello")], provider="test")

        video = DownloadedVideo(path=self.root / "v.mp4", title="v", duration=10, source_url="https://example.com/v")
        result = FallbackTranscriber([MissingCaptions(), Working()]).transcribe(
            video, TranscriptOptions(output_dir=self.root)
        )
        self.assertEqual(result.provider, "test")

        with self.assertRaises(PipelineError) as caught:
            FallbackTranscriber([MissingCaptions()]).transcribe(video, TranscriptOptions(output_dir=self.root))
        self.assertEqual(caught.exception.code, errors.TRANSCRIPTION_FAILED)
        self.assertIn("none", caught.exception.technical_details)

    def test_partial_caption_download_keeps_written_files(self):
        video = DownloadedVideo(
            path=self.root / "v.mp4",
            title="v",
            duration=10,
            source_url="https://example.com/v",
            raw_metadata={
                "language": "ar",
                "automatic_captions": {"ar": [], "en": [], "aa": []},
            },
        )
        events = []

        class Logger:
            def event(self, stage, level="INFO", **fields):
                events.append((stage, level, fields))

        def fake_run(cmd, **kwargs):
            template = Path(cmd[cmd.index("-o") + 1])
            output = template.with_name("captions.ar.json3")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(
                    {
                        "events": [
                            {
                                "tStartMs": 0,
                                "dDurationMs": 3000,
                                "segs": [{"utf8": "مرحبا"}],
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="HTTP Error 429: Too Many Requests"
            )

        with patch("clipper.viral.transcription.subprocess.run", side_effect=fake_run):
            transcript = CaptionTranscriber(self.config, Logger()).transcribe(
                video, TranscriptOptions(language="ar", output_dir=self.root / "transcript")
            )
        self.assertEqual(transcript.provider, "youtube_captions:auto:ar")
        self.assertEqual([segment.text for segment in transcript.segments], ["مرحبا"])
        self.assertTrue(
            any(
                stage == "transcription_captions_partial" and level == "WARNING"
                for stage, level, _ in events
            )
        )

    def test_caption_download_failure_without_files_is_reported(self):
        video = DownloadedVideo(
            path=self.root / "v.mp4",
            title="v",
            duration=10,
            source_url="https://example.com/v",
            raw_metadata={"language": "ar", "automatic_captions": {"ar": []}},
        )

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="HTTP Error 429: Too Many Requests"
            )

        with patch("clipper.viral.transcription.subprocess.run", side_effect=fake_run):
            with self.assertRaises(NoCaptionsAvailable):
                CaptionTranscriber(self.config).transcribe(
                    video, TranscriptOptions(language="ar", output_dir=self.root / "transcript")
                )

    def test_whisper_response_segments_are_offset_and_clamped(self):
        transcriber = OpenAIWhisperTranscriber(self.config)
        segments = transcriber._segments_from_response(
            {
                "segments": [
                    {"start": 1.0, "end": 3.0, "text": " one ", "avg_logprob": -0.1},
                    {"start": 9.0, "end": 20.0, "text": "two"},
                ]
            },
            offset=10.0,
            chunk_duration=12.0,
        )
        self.assertEqual(segments[0].start, 11.0)
        self.assertEqual(segments[0].end, 13.0)
        self.assertEqual(segments[0].text, "one")
        self.assertIsNotNone(segments[0].confidence)
        self.assertEqual(segments[1].start, 19.0)
        self.assertEqual(segments[1].end, 22.0)

    def test_clean_transcript_drops_non_speech_and_overlaps_duplicates(self):
        segments = [
            TranscriptSegment(1, 0, 2, "[Music]"),
            TranscriptSegment(2, 2, 4, "Hello"),
            TranscriptSegment(3, 4, 6, "Hello"),
            TranscriptSegment(4, 5.9, 8, "World"),
        ]
        cleaned = clean_transcript_segments(segments, 10)
        self.assertEqual([segment.text for segment in cleaned], ["Hello", "World"])
        self.assertEqual(cleaned[0].end, 6)


if __name__ == "__main__":
    unittest.main()
