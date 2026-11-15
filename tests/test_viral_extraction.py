import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clipper.viral.config import ViralConfig
from clipper.viral.extraction import FFmpegClipExtractor
from clipper.viral.media import probe_duration
from clipper.viral.models import ClipCandidate, DownloadedVideo

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


class ExtractionDecisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = ViralConfig(
            project_root=self.root,
            output_dir=self.root / "out",
            temp_dir=self.root / "tmp",
            cache_dir=self.root / "cache",
        )
        self.video = DownloadedVideo(
            path=self.root / "source.mp4",
            title="Synthetic",
            duration=6.0,
            source_url="https://example.com/v",
        )
        self.clip = ClipCandidate(
            rank=1,
            title="Clip",
            hook="Hook",
            start_time=1.0,
            end_time=4.0,
            duration=3.0,
            viral_score=90,
            category="test",
            reason="Test",
            summary="Test",
            platforms=["tiktok"],
            keywords=[],
        )

    def tearDown(self):
        self.temp.cleanup()

    def _extract(self, codecs, mode="auto"):
        self.config.clip_copy_mode = mode
        extractor = FFmpegClipExtractor(self.config)
        with patch("clipper.viral.extraction.require_tools"), patch(
            "clipper.viral.extraction.stream_codecs", return_value=codecs
        ), patch.object(extractor, "_extract_copy") as copy_mock, patch.object(
            extractor, "_extract_reencode"
        ) as reencode_mock, patch.object(
            extractor, "_existing_output_is_valid", side_effect=[False, True]
        ):
            extractor.extract(self.video, self.clip, self.root / "out.mp4")
        return copy_mock, reencode_mock

    def test_auto_copies_h264_aac_sources(self):
        copy_mock, reencode_mock = self._extract([("video", "h264"), ("audio", "aac")])
        copy_mock.assert_called_once()
        reencode_mock.assert_not_called()

    def test_auto_reencodes_incompatible_codecs(self):
        copy_mock, reencode_mock = self._extract([("video", "av1"), ("audio", "opus")])
        copy_mock.assert_not_called()
        reencode_mock.assert_called_once()

    def test_explicit_copy_mode_still_forces_a_copy(self):
        copy_mock, reencode_mock = self._extract(
            [("video", "av1"), ("audio", "opus")], mode="copy"
        )
        copy_mock.assert_called_once()
        reencode_mock.assert_not_called()


@unittest.skipUnless(FFMPEG and FFPROBE, "ffmpeg and ffprobe are required")
class RealFFmpegExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.source = cls.root / "input.mp4"
        subprocess.run(
            [
                FFMPEG, "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=6",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                "-shortest", "-y", str(cls.source),
            ],
            check=True,
            timeout=120,
        )
        cls.video = DownloadedVideo(
            path=cls.source,
            title="Synthetic",
            duration=6.0,
            source_url="https://example.com/v",
        )

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.config = ViralConfig(
            project_root=self.root,
            output_dir=self.root / "out",
            temp_dir=self.root / "tmp",
            cache_dir=self.root / "cache",
        )

    def _clip(self, start=1.0, end=4.0):
        return ClipCandidate(
            rank=1,
            title="Clip",
            hook="Hook",
            start_time=start,
            end_time=end,
            duration=end - start,
            viral_score=90,
            category="test",
            reason="Test",
            summary="Test",
            platforms=["tiktok"],
            keywords=[],
        )

    def _assert_video(self, path: Path, expected: float):
        self.assertTrue(path.exists())
        self.assertGreater(path.stat().st_size, 10 * 1024)
        actual = probe_duration(path, FFPROBE)
        self.assertIsNotNone(actual)
        self.assertLessEqual(abs(actual - expected), 1.0)
        audio = subprocess.run(
            [
                FFPROBE, "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertIn("audio", audio.stdout)

    def test_stream_copy_extracts_a_valid_clip_with_audio(self):
        output = self.root / "copy.mp4"
        extractor = FFmpegClipExtractor(self.config)
        extractor.extract(self.video, self._clip(), output)
        self._assert_video(output, 3.0)

    def test_reencode_mode_extracts_a_valid_clip_with_audio(self):
        self.config.clip_copy_mode = "reencode"
        output = self.root / "reencode.mp4"
        extractor = FFmpegClipExtractor(self.config)
        extractor.extract(self.video, self._clip(2.0, 5.0), output)
        self._assert_video(output, 3.0)


if __name__ == "__main__":
    unittest.main()
