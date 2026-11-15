import json
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path

from clipper.viral import errors
from clipper.viral.analysis import AnalysisResult
from clipper.viral.config import ViralConfig
from clipper.viral.errors import PipelineError
from clipper.viral.models import (
    DownloadedVideo,
    DownloadOptions,
    JobOptions,
    Transcript,
    TranscriptSegment,
)
from clipper.viral.pipeline import ViralPipeline


class FakeDownloader:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def download(self, url, options: DownloadOptions) -> DownloadedVideo:
        self.calls.append((url, options))
        if self.fail:
            raise PipelineError(errors.DOWNLOAD_FAILED, "No video.", retryable=True)
        options.output_dir.mkdir(parents=True, exist_ok=True)
        path = options.output_dir / "video.mp4"
        path.write_bytes(b"fake video bytes")
        options.metadata_dir.mkdir(parents=True, exist_ok=True)
        thumbnail = options.metadata_dir / "thumbnail.jpg"
        thumbnail.write_bytes(b"fake image")
        return DownloadedVideo(
            path=path,
            title="Test Video",
            duration=120.0,
            source_url=url,
            quality="1080p",
            filesize=path.stat().st_size,
            thumbnail=thumbnail,
            uploader="Tester",
            video_id="vid123",
            raw_metadata={"id": "vid123"},
        )


class FakeTranscriber:
    def transcribe(self, video, options):
        segments = [
            TranscriptSegment(index + 1, index * 5.0, (index + 1) * 5.0, f"Sentence {index + 1}.")
            for index in range(24)
        ]
        return Transcript(
            language="en",
            duration=120.0,
            segments=segments,
            provider="fake-captions",
            raw={"segments": [segment.to_dict() for segment in segments], "source": "test"},
        )


class FakeAnalyzer:
    def __init__(self, clips=None):
        self.clips = clips if clips is not None else [
            {
                "title": "The big idea",
                "hook": "You will not believe this.",
                "start_time": 10.0,
                "end_time": 40.0,
                "viral_score": 92,
                "category": "insight",
                "reason": "Strong payoff.",
                "summary": "A concise summary.",
                "platforms": ["tiktok", "youtube_shorts"],
                "keywords": ["idea", "viral"],
                "confidence": 0.9,
            }
        ]

    def analyze(self, transcript, options):
        return AnalysisResult(
            clips=self.clips,
            provider="fake-llm",
            model="fake-model",
            attempts=1,
        )


class FakeExtractor:
    def __init__(self, fail=False):
        self.fail = fail
        self.extracted = []

    def extract(self, video, clip, output_path: Path):
        if self.fail:
            raise PipelineError(
                errors.CLIP_EXTRACTION_FAILED,
                "FFmpeg failed.",
                technical_details="simulated failure",
            )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"clip bytes")
        self.extracted.append((clip.rank, output_path))
        return output_path


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = ViralConfig(
            project_root=self.root,
            output_dir=self.root / "output",
            temp_dir=self.root / "tmp",
            cache_dir=self.root / "cache",
            prompt_file=self.root / "prompt.txt",
        )
        self.config.prompt_file.parent.mkdir(parents=True, exist_ok=True)
        self.config.prompt_file.write_text("prompt", encoding="utf-8")
        self.job_id = uuid.uuid4().hex
        self.options = JobOptions(
            language="en",
            number_of_clips=1,
            min_clip_duration=10,
            max_clip_duration=60,
        )
        self.downloader = FakeDownloader()
        self.transcriber = FakeTranscriber()
        self.analyzer = FakeAnalyzer()
        self.extractor = FakeExtractor()
        self.pipeline = ViralPipeline(
            self.config,
            self.downloader,
            self.transcriber,
            self.analyzer,
            self.extractor,
        )

    def tearDown(self):
        self.temp.cleanup()

    def _run(self, options=None, progress=None):
        return self.pipeline.run(
            job_id=self.job_id,
            source_url="https://example.com/watch?v=vid123",
            options=options or self.options,
            created_at="2026-01-02T03:04:05+00:00",
            progress_callback=progress,
        )

    def test_full_pipeline_produces_clips_json_report_and_zip(self):
        progress = []
        outcome = self._run(progress=lambda value, step, detail: progress.append((value, step)))

        root = outcome.output_dir
        for name in ("original", "transcript", "clips", "metadata", "logs"):
            self.assertTrue((root / name).is_dir(), name)
        self.assertTrue(outcome.result_path.exists())
        self.assertTrue(outcome.report_path.exists())
        self.assertTrue(outcome.package_path.exists())
        self.assertEqual(progress[-1][0], 100.0)

        result = json.loads(outcome.result_path.read_text(encoding="utf-8"))
        self.assertEqual(result["job_id"], self.job_id)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["video"]["title"], "Test Video")
        self.assertEqual(result["video"]["original_file"], "original/video.mp4")
        self.assertEqual(result["transcript"]["provider"], "fake-captions")
        self.assertEqual(len(result["clips"]), 1)
        clip = result["clips"][0]
        self.assertEqual(clip["viral_score"], 92)
        self.assertEqual(clip["rank"], 1)
        self.assertEqual(
            clip["file"],
            "clips/clip_001_rank_1_viral_score_92.mp4",
        )
        self.assertTrue(clip["srt_file"].endswith(".srt"))
        self.assertEqual(clip["duration"], 30.0)

        self.assertTrue((root / clip["file"]).exists())
        self.assertTrue((root / clip["srt_file"]).exists())
        self.assertTrue((root / clip["vtt_file"]).exists())
        self.assertTrue((root / clip["txt_file"]).exists())

        self.assertTrue((root / "transcript" / "raw.json").exists())
        self.assertTrue((root / "transcript" / "clean.json").exists())
        self.assertTrue((root / "transcript" / "full.srt").exists())
        self.assertTrue((root / "metadata" / "download.json").exists())
        self.assertTrue((root / "metadata" / "analysis_raw.json").exists())
        self.assertTrue((root / "metadata" / "validated_clips.json").exists())
        self.assertTrue((root / "metadata" / "clip_001.json").exists())

        with zipfile.ZipFile(outcome.package_path) as archive:
            names = set(archive.namelist())
        self.assertIn("result.json", names)
        self.assertIn("report.md", names)
        self.assertIn("original/video.mp4", names)
        self.assertIn("transcript/full.srt", names)
        self.assertIn(clip["file"], names)
        self.assertFalse(any(name.startswith("temp/") for name in names))
        self.assertFalse(any("package.zip" in name for name in names))

        report = outcome.report_path.read_text(encoding="utf-8")
        self.assertIn("The big idea", report)
        self.assertIn("92/100", report)
        self.assertIn("Strong payoff.", report)

        log_lines = [
            json.loads(line)
            for line in (root / "logs" / "job.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        events = {line.get("stage") for line in log_lines}
        for stage in ("transcription", "analysis", "validation", "clip_extraction", "packaging"):
            self.assertIn(f"{stage}_started", events)
            self.assertIn(f"{stage}_completed", events)
        self.assertIn("job_completed", events)

    def test_discard_original_removes_video_and_excludes_it_from_zip(self):
        options = JobOptions(
            language="en",
            number_of_clips=1,
            min_clip_duration=10,
            max_clip_duration=60,
            download_original=False,
        )
        outcome = self._run(options=options)
        root = outcome.output_dir
        self.assertFalse((root / "original" / "video.mp4").exists())
        result = json.loads(outcome.result_path.read_text(encoding="utf-8"))
        self.assertFalse(result["video"]["original_retained"])
        self.assertIsNone(result["video"]["original_file"])
        with zipfile.ZipFile(outcome.package_path) as archive:
            names = set(archive.namelist())
        self.assertNotIn("original/video.mp4", names)
        self.assertTrue(any(name.startswith("clips/") for name in names))

    def test_extraction_failure_is_reported_with_clip_context(self):
        self.extractor.fail = True
        with self.assertRaises(PipelineError) as caught:
            self._run()
        self.assertEqual(caught.exception.code, errors.CLIP_EXTRACTION_FAILED)
        self.assertIn("clip rank 1", caught.exception.technical_details)

    def test_download_failure_stops_before_transcription(self):
        self.downloader.fail = True
        with self.assertRaises(PipelineError) as caught:
            self._run()
        self.assertEqual(caught.exception.code, errors.DOWNLOAD_FAILED)
        self.assertEqual(len(self.downloader.calls), 1)
        clips_dir = self.config.output_dir / self.job_id / "clips"
        self.assertEqual(list(clips_dir.glob("*.mp4")), [])

    def test_invalid_job_id_is_rejected_before_creating_folders(self):
        with self.assertRaises(PipelineError) as caught:
            self.pipeline.run(
                job_id="../../escape",
                source_url="https://example.com/v",
                options=self.options,
            )
        self.assertEqual(caught.exception.code, errors.JOB_NOT_FOUND)
        self.assertFalse((self.config.output_dir / ".." / "escape").exists())


if __name__ == "__main__":
    unittest.main()
