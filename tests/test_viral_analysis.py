import json
import tempfile
import unittest
from pathlib import Path

from clipper.viral import errors
from clipper.viral.analysis import (
    AnalysisJSONError,
    HeuristicAnalyzer,
    OpenAICompatibleAnalyzer,
    _extract_json,
    build_analyzer,
    chunk_transcript,
    validate_analysis_payload,
)
from clipper.viral.config import ViralConfig
from clipper.viral.errors import PipelineError
from clipper.viral.models import JobOptions, Transcript, TranscriptSegment
from clipper.viral.net import HttpRequestError


def transcript(duration=120.0):
    return Transcript(
        language="en",
        duration=duration,
        segments=[
            TranscriptSegment(
                id=index + 1,
                start=float(index * 10),
                end=float((index + 1) * 10),
                text=f"Segment {index + 1}. Why does this matter to people?",
            )
            for index in range(int(duration // 10))
        ],
    )


def valid_content():
    return json.dumps(
        {
            "clips": [
                {
                    "rank": 1,
                    "title": "A strong moment",
                    "hook": "Why does this matter?",
                    "start_time": 10,
                    "end_time": 40,
                    "duration": 30,
                    "viral_score": 88,
                    "category": "curiosity_hook",
                    "reason": "Clear hook and payoff.",
                    "summary": "A concise explanation.",
                    "platforms": ["tiktok", "youtube_shorts"],
                    "keywords": ["matter", "people"],
                    "confidence": 0.9,
                }
            ]
        }
    )


class JsonParsingTests(unittest.TestCase):
    def test_extracts_fenced_json(self):
        data = _extract_json("```json\n{\"clips\": []}\n```")
        self.assertEqual(data, {"clips": []})

    def test_repairs_trailing_commas(self):
        data = _extract_json('{"clips": [{"title": "x",},],}')
        self.assertEqual(data["clips"][0]["title"], "x")

    def test_rejects_plain_text(self):
        with self.assertRaises(AnalysisJSONError):
            _extract_json("I cannot do that.")

    def test_normalizes_schema_and_clamps_score(self):
        clips = validate_analysis_payload(
            {"clips": [{"title": "x", "start_time": 1, "end_time": 4, "viral_score": 500}]}
        )
        self.assertEqual(clips[0]["duration"], 3)
        self.assertEqual(clips[0]["viral_score"], 100)
        self.assertTrue(clips[0]["platforms"])

    def test_missing_time_is_rejected(self):
        with self.assertRaises(AnalysisJSONError):
            validate_analysis_payload({"clips": [{"title": "x", "start_time": 1}]})

    def test_empty_clip_list_is_rejected(self):
        with self.assertRaises(AnalysisJSONError):
            validate_analysis_payload({"clips": []})

    def test_chunking_respects_budget(self):
        chunks = chunk_transcript(transcript(600).segments, max_chars=500, max_chunks=4)
        self.assertGreater(len(chunks), 1)
        self.assertLessEqual(len(chunks), 5)


class AnalyzerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.prompt = Path(self.temp.name) / "prompt.txt"
        self.prompt.write_text("DURATION {{VIDEO_DURATION}} N {{NUMBER_OF_CLIPS}} TRANSCRIPT {{TRANSCRIPT}}", encoding="utf-8")
        self.config = ViralConfig(
            output_dir=Path(self.temp.name) / "output",
            prompt_file=self.prompt,
            llm_api_key="test-key",
            llm_model="test-model",
            llm_max_retries=1,
        )
        self.options = JobOptions(number_of_clips=1, min_clip_duration=20, max_clip_duration=90)

    def tearDown(self):
        self.temp.cleanup()

    def test_returns_valid_clips(self):
        calls = []

        def transport(url, payload, headers, timeout):
            calls.append(payload)
            return {"choices": [{"message": {"content": valid_content()}}]}

        analyzer = OpenAICompatibleAnalyzer(self.config, transport=transport)
        result = analyzer.analyze(transcript(), self.options)
        self.assertEqual(len(result.clips), 1)
        self.assertEqual(result.clips[0]["title"], "A strong moment")
        self.assertEqual(result.attempts, 1)
        self.assertEqual(len(calls), 1)

    def test_retries_after_invalid_json(self):
        responses = iter([
            {"choices": [{"message": {"content": "not json"}}]},
            {"choices": [{"message": {"content": valid_content()}}]},
        ])

        def transport(url, payload, headers, timeout):
            return next(responses)

        analyzer = OpenAICompatibleAnalyzer(self.config, transport=transport)
        result = analyzer.analyze(transcript(), self.options)
        self.assertEqual(result.attempts, 2)

    def test_reports_exhausted_invalid_json(self):
        def transport(url, payload, headers, timeout):
            return {"choices": [{"message": {"content": "still not json"}}]}

        analyzer = OpenAICompatibleAnalyzer(self.config, transport=transport)
        self.config.llm_max_retries = 0
        with self.assertRaises(PipelineError) as ctx:
            analyzer.analyze(transcript(), self.options)
        self.assertEqual(ctx.exception.code, errors.ANALYSIS_INVALID_JSON)

    def test_maps_unauthorized_http_error(self):
        def transport(url, payload, headers, timeout):
            raise HttpRequestError(401, "bad key", url)

        analyzer = OpenAICompatibleAnalyzer(self.config, transport=transport)
        with self.assertRaises(PipelineError) as ctx:
            analyzer.analyze(transcript(), self.options)
        self.assertEqual(ctx.exception.code, errors.CONFIGURATION_ERROR)

    def test_heuristic_fallback_returns_ranked_clips(self):
        analyzer = HeuristicAnalyzer(self.config)
        result = analyzer.analyze(transcript(), self.options)
        self.assertTrue(result.clips)
        self.assertEqual([c["rank"] for c in result.clips], list(range(1, len(result.clips) + 1)))
        for clip in result.clips:
            self.assertGreaterEqual(clip["duration"], 20)
            self.assertLessEqual(clip["duration"], 90)
            self.assertGreaterEqual(clip["viral_score"], 0)
            self.assertLessEqual(clip["viral_score"], 100)

    def test_build_analyzer_accepts_gemini_aliases(self):
        for provider in ("gemini", "google", "google_gemini"):
            self.config.llm_provider = provider
            self.assertIsInstance(build_analyzer(self.config), OpenAICompatibleAnalyzer)

    def test_build_analyzer_requires_a_key_for_gemini(self):
        self.config.llm_provider = "gemini"
        self.config.llm_api_key = ""
        with self.assertRaises(PipelineError) as ctx:
            build_analyzer(self.config)
        self.assertEqual(ctx.exception.code, errors.CONFIGURATION_ERROR)


if __name__ == "__main__":
    unittest.main()
