import tempfile
import unittest
from pathlib import Path

from clipper.viral import errors
from clipper.viral.config import ViralConfig
from clipper.viral.errors import PipelineError
from clipper.viral.models import JobOptions
from clipper.viral.validation import (
    check_disk_space,
    ensure_within,
    safe_filename,
    validate_job_options,
    validate_url,
)


class UrlValidationTests(unittest.TestCase):
    def test_accepts_public_http_urls(self):
        self.assertEqual(
            validate_url("https://www.youtube.com/watch?v=abc123"),
            "https://www.youtube.com/watch?v=abc123",
        )
        self.assertTrue(validate_url("http://example.com/video").startswith("http://"))

    def test_rejects_empty_or_malformed(self):
        for value in ("", "   ", "not a url", "https://"):
            with self.subTest(value=value):
                with self.assertRaises(PipelineError) as ctx:
                    validate_url(value)
                self.assertIn(ctx.exception.code, (errors.INVALID_URL, errors.UNSUPPORTED_URL))

    def test_rejects_non_http_schemes(self):
        for value in ("file:///etc/passwd", "ftp://example.com/a", "javascript:alert(1)"):
            with self.subTest(value=value):
                with self.assertRaises(PipelineError) as ctx:
                    validate_url(value)
                self.assertEqual(ctx.exception.code, errors.UNSUPPORTED_URL)

    def test_rejects_local_and_private_hosts(self):
        for value in (
            "http://localhost:8000/video",
            "http://127.0.0.1/video",
            "http://10.0.0.5/video",
            "http://192.168.1.10/video",
            "http://[::1]/video",
            "http://printer.local/video",
        ):
            with self.subTest(value=value):
                with self.assertRaises(PipelineError) as ctx:
                    validate_url(value)
                self.assertEqual(ctx.exception.code, errors.UNSUPPORTED_URL)

    def test_rejects_credentials_and_control_characters(self):
        for value in ("https://user:pass@example.com/v", "https://example.com/a\nb"):
            with self.subTest(value=value):
                with self.assertRaises(PipelineError):
                    validate_url(value)


class FilenameTests(unittest.TestCase):
    def test_removes_path_separators(self):
        name = safe_filename("../../etc/passwd")
        self.assertNotIn("/", name)
        self.assertNotIn("\\", name)
        self.assertTrue(name)

    def test_removes_windows_reserved_names(self):
        self.assertTrue(safe_filename("CON").startswith("_"))
        self.assertTrue(safe_filename("lpt1.txt").startswith("_"))

    def test_keeps_arabic_text(self):
        self.assertEqual(safe_filename("ملخص الفيديو"), "ملخص الفيديو")

    def test_falls_back_for_empty_values(self):
        self.assertEqual(safe_filename("   ...   ", fallback="clip"), "clip")

    def test_ensure_within_blocks_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inside = ensure_within(root, root / "clips" / "a.mp4")
            self.assertEqual(inside.parent.name, "clips")
            with self.assertRaises(PipelineError) as ctx:
                ensure_within(root, root / ".." / "outside.mp4")
            self.assertEqual(ctx.exception.code, errors.UNSAFE_PATH)


class OptionTests(unittest.TestCase):
    def setUp(self):
        self.config = ViralConfig(max_clips=10)

    def test_defaults_are_valid(self):
        options = validate_job_options({}, self.config)
        self.assertEqual(options.number_of_clips, 5)
        self.assertEqual(options.min_clip_duration, 20.0)
        self.assertIn("tiktok", options.platforms)

    def test_rejects_bad_clip_counts(self):
        for value in (0, -1, 11, "abc", True):
            with self.subTest(value=value):
                with self.assertRaises(PipelineError) as ctx:
                    validate_job_options({"number_of_clips": value}, self.config)
                self.assertEqual(ctx.exception.code, errors.INVALID_OPTIONS)

    def test_rejects_bad_durations_and_platforms(self):
        with self.assertRaises(PipelineError):
            validate_job_options({"min_clip_duration": 100, "max_clip_duration": 20}, self.config)
        with self.assertRaises(PipelineError):
            validate_job_options({"platforms": ["myspace"]}, self.config)
        with self.assertRaises(PipelineError):
            validate_job_options({"platforms": []}, self.config)

    def test_accepts_arabic_language_and_deduplicates_platforms(self):
        options = validate_job_options(
            {"language": "AR", "platforms": ["tiktok", "tiktok", "youtube_shorts"]},
            self.config,
        )
        self.assertEqual(options.language, "ar")
        self.assertEqual(options.platforms, ["tiktok", "youtube_shorts"])

    def test_disk_space_guard(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(PipelineError) as ctx:
                check_disk_space(Path(temp), 10 ** 18, 0)
            self.assertEqual(ctx.exception.code, errors.INSUFFICIENT_STORAGE)


if __name__ == "__main__":
    unittest.main()
