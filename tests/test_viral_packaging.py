import json
import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from clipper.viral.models import (
    ClipCandidate,
    DownloadedVideo,
    JobOptions,
    Transcript,
    TranscriptSegment,
)
from clipper.viral.packaging import (
    address,
    build_result,
    build_zip,
    create_job_dirs,
    format_duration,
    read_json,
    write_clip_metadata,
    write_json,
    write_report,
)


class PackagingTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.job = self.root / "job"
        self.dirs = create_job_dirs(self.job)
        self.video_path = self.dirs["original"] / "video.mp4"
        self.video_path.write_bytes(b"video")
        self.thumbnail = self.dirs["metadata"] / "thumbnail.jpg"
        self.thumbnail.write_bytes(b"thumb")
        self.transcript = Transcript(
            language="en",
            duration=120.0,
            provider="test",
            segments=[
                TranscriptSegment(1, 10.0, 20.0, "First."),
                TranscriptSegment(2, 20.0, 40.0, "Second."),
            ],
        )
        self.clip = ClipCandidate(
            rank=1,
            title="A | title\nwith a newline",
            hook="The hook",
            start_time=10.0,
            end_time=40.0,
            duration=30.0,
            viral_score=92,
            category="insight",
            reason="Because it is good.",
            summary="Summary.",
            platforms=["tiktok", "youtube_shorts"],
            keywords=["alpha", "beta"],
            confidence=0.9,
            adjustments=["snapped_start_to_sentence"],
        )
        self.clip_file = self.dirs["clips"] / "clip_001.mp4"
        self.clip_file.write_bytes(b"clip")
        self.srt_file = self.dirs["clips"] / "clip_001.srt"
        self.srt_file.write_text("1\n", encoding="utf-8")
        self.transcript_files = {
            "raw": write_json(self.dirs["transcript"] / "raw.json", {"segments": []}),
            "clean": write_json(self.dirs["transcript"] / "clean.json", {"segments": []}),
            "srt": self.dirs["transcript"] / "full.srt",
            "vtt": self.dirs["transcript"] / "full.vtt",
            "txt": self.dirs["transcript"] / "full.txt",
        }
        for path in (self.transcript_files["srt"], self.transcript_files["vtt"], self.transcript_files["txt"]):
            path.write_text("text", encoding="utf-8")
        self.video = DownloadedVideo(
            path=self.video_path,
            title="Video",
            duration=120.0,
            source_url="https://example.com/v",
            quality="1080p",
            filesize=5,
            thumbnail=self.thumbnail,
        )

    def tearDown(self):
        self.temp.cleanup()

    def _result(self, original_retained=True):
        return build_result(
            job_id="a" * 32,
            source_url="https://example.com/v",
            created_at="2026-01-01T00:00:00+00:00",
            options=JobOptions(number_of_clips=1),
            video=self.video,
            transcript=self.transcript,
            clips=[self.clip],
            job_root=self.job,
            transcript_files=self.transcript_files,
            clip_files={
                1: {"video": self.clip_file, "srt": self.srt_file, "vtt": None, "txt": None}
            },
            analysis_meta={"provider": "test", "model": "m", "attempts": 1},
            original_retained=original_retained,
        )


class JobFolderTests(PackagingTestCase):
    def test_create_job_dirs_and_json_roundtrip(self):
        for name in ("original", "transcript", "clips", "metadata", "logs", "temp"):
            self.assertTrue(self.dirs[name].is_dir())
        path = write_json(self.job / "metadata" / "data.json", {"مرحبا": "world"})
        self.assertEqual(read_json(path), {"مرحبا": "world"})
        self.assertFalse(path.with_suffix(".json.tmp").exists())
        self.assertEqual(read_json(self.job / "missing.json", default={"x": 1}), {"x": 1})
        (self.job / "metadata" / "broken.json").write_text("{", encoding="utf-8")
        self.assertIsNone(read_json(self.job / "metadata" / "broken.json"))

    def test_write_json_retries_a_transient_windows_lock(self):
        path = self.job / "metadata" / "locked.json"
        real_replace = os.replace
        calls = {"count": 0}

        def flaky_replace(source, target):
            calls["count"] += 1
            if calls["count"] == 1:
                raise PermissionError(32, "The process cannot access the file")
            return real_replace(source, target)

        with patch(
            "clipper.viral.packaging.os.replace", side_effect=flaky_replace
        ):
            write_json(path, {"ok": True})
        self.assertEqual(calls["count"], 2)
        self.assertEqual(read_json(path), {"ok": True})
        self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_address_is_relative_and_refuses_escapes(self):
        self.assertEqual(address(self.job, self.video_path), "original/video.mp4")
        self.assertIsNone(address(self.job, self.root / "elsewhere.txt"))
        self.assertIsNone(address(self.job, None))

    def test_format_duration(self):
        self.assertEqual(format_duration(59), "0:59")
        self.assertEqual(format_duration(3661), "1:01:01")


class ResultTests(PackagingTestCase):
    def test_build_result_uses_relative_paths_and_clip_fields(self):
        result = self._result()
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["video"]["original_file"], "original/video.mp4")
        self.assertEqual(result["video"]["thumbnail"], "metadata/thumbnail.jpg")
        self.assertEqual(result["transcript"]["raw_file"], "transcript/raw.json")
        clip = result["clips"][0]
        self.assertEqual(clip["file"], "clips/clip_001.mp4")
        self.assertEqual(clip["srt_file"], "clips/clip_001.srt")
        self.assertIsNone(clip["vtt_file"])
        self.assertEqual(clip["viral_score"], 92)
        self.assertEqual(clip["adjustments"], ["snapped_start_to_sentence"])
        self.assertEqual(result["analysis"]["provider"], "test")

    def test_discarded_original_is_not_referenced(self):
        result = self._result(original_retained=False)
        self.assertFalse(result["video"]["original_retained"])
        self.assertIsNone(result["video"]["original_file"])

    def test_report_escapes_table_cells(self):
        report_path = self.job / "report.md"
        write_report(self._result(), report_path)
        report = report_path.read_text(encoding="utf-8")
        self.assertIn("# Viral Clip Report", report)
        self.assertIn("A \\| title with a newline", report)
        self.assertIn("92/100", report)
        self.assertIn("snapped_start_to_sentence", report)
        self.assertIn("Because it is good.", report)
        self.assertIn("clips/clip_001.mp4", report)

    def test_report_handles_no_clips(self):
        result = self._result()
        result["clips"] = []
        report_path = self.job / "report.md"
        write_report(result, report_path)
        self.assertIn("_No clips were produced._", report_path.read_text(encoding="utf-8"))

    def test_write_clip_metadata(self):
        path = write_clip_metadata(
            self.clip,
            self.dirs["metadata"],
            {"video": self.clip_file, "srt": self.srt_file, "vtt": None},
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["title"], self.clip.title)
        self.assertEqual(data["files"]["srt"], str(self.srt_file))
        self.assertIsNone(data["files"]["vtt"])


class ZipTests(PackagingTestCase):
    def test_zip_contains_artifacts_and_excludes_temp_and_nested_zip(self):
        result_path = write_json(self.job / "result.json", self._result())
        report_path = self.job / "report.md"
        write_report(self._result(), report_path)
        (self.job / "temp" / "scratch.txt").write_text("temp", encoding="utf-8")
        (self.job / "metadata" / "job.json").write_text("{}", encoding="utf-8")
        package = self.job / "package.zip"
        build_zip(self.job, result_path, report_path, package, include_original=True)

        with zipfile.ZipFile(package) as archive:
            names = set(archive.namelist())
        self.assertIn("result.json", names)
        self.assertIn("report.md", names)
        self.assertIn("original/video.mp4", names)
        self.assertIn("clips/clip_001.mp4", names)
        self.assertIn("transcript/full.srt", names)
        self.assertIn("metadata/job.json", names)
        self.assertNotIn("temp/scratch.txt", names)
        self.assertNotIn("package.zip", names)
        self.assertTrue(all(not name.startswith("..") for name in names))

    def test_zip_can_exclude_original(self):
        result_path = write_json(self.job / "result.json", self._result(original_retained=False))
        report_path = self.job / "report.md"
        write_report(self._result(original_retained=False), report_path)
        package = self.job / "package.zip"
        build_zip(self.job, result_path, report_path, package, include_original=False)
        with zipfile.ZipFile(package) as archive:
            names = set(archive.namelist())
        self.assertNotIn("original/video.mp4", names)
        self.assertIn("clips/clip_001.mp4", names)


if __name__ == "__main__":
    unittest.main()
