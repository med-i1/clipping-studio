import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clipper.viral import errors
from clipper.viral.config import ViralConfig
from clipper.viral.download_adapter import ExistingYtDlpDownloader, _classify_ytdlp_error
from clipper.viral.errors import PipelineError
from clipper.viral.models import DownloadOptions


INFO = {
    "id": "abc123",
    "title": "A test video",
    "duration": 120,
    "uploader": "Tester",
    "formats": [
        {"vcodec": "avc1", "height": 720},
        {"vcodec": "avc1", "height": 1080},
        {"vcodec": "none", "acodec": "aac"},
    ],
}


def _write_video(path: Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(b"x" * (60 * 1024))


class DownloaderTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = ViralConfig(
            project_root=self.root,
            output_dir=self.root / "output",
            temp_dir=self.root / "tmp",
            cache_dir=self.root / "cache",
            max_retries=1,
        )
        self.options = DownloadOptions(
            output_dir=self.root / "job" / "original",
            metadata_dir=self.root / "job" / "metadata",
            cache_dir=self.root / "cache",
            max_retries=1,
        )
        self.downloader = ExistingYtDlpDownloader(self.config)

    def tearDown(self):
        self.temp.cleanup()

    def _download(self, info=None, url="https://example.com/watch?v=abc123"):
        with patch.object(
            ExistingYtDlpDownloader, "_fetch_info", return_value=dict(info or INFO)
        ), patch(
            "clipper.viral.download_adapter.download_full_video",
            side_effect=lambda _url, target, _config: _write_video(target) or True,
        ) as download_mock, patch(
            "clipper.viral.download_adapter.probe_duration", return_value=120.0
        ), patch.object(
            ExistingYtDlpDownloader, "_download_thumbnail", return_value=None
        ):
            video = self.downloader.download(url, self.options)
        return video, download_mock


class ExistingDownloaderTests(DownloaderTestCase):
    def test_uses_existing_downloader_and_records_metadata(self):
        video, download_mock = self._download()

        self.assertTrue(video.path.exists())
        self.assertEqual(video.title, "A test video")
        self.assertEqual(video.quality, "1080p")
        self.assertEqual(video.duration, 120.0)
        self.assertEqual(video.video_id, "abc123")
        self.assertFalse(video.from_cache)
        download_mock.assert_called_once()

        info_path = self.options.metadata_dir / "source_info.json"
        self.assertTrue(info_path.exists())
        saved = json.loads(info_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["_resolved_duration"], 120.0)

        index = json.loads((self.root / "cache" / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(len(index), 1)

    def test_cache_hit_does_not_call_the_downloader_again(self):
        first, download_mock = self._download()
        self.assertEqual(download_mock.call_count, 1)

        second_dir = self.root / "second"
        second_options = DownloadOptions(
            output_dir=second_dir / "original",
            metadata_dir=second_dir / "metadata",
            cache_dir=self.root / "cache",
            max_retries=1,
        )
        with patch(
            "clipper.viral.download_adapter.download_full_video"
        ) as second_download, patch.object(
            ExistingYtDlpDownloader, "_fetch_info"
        ) as fetch_info:
            second = self.downloader.download(
                "https://example.com/watch?v=abc123", second_options
            )
        second_download.assert_not_called()
        fetch_info.assert_not_called()
        self.assertTrue(second.from_cache)
        self.assertTrue(second.path.exists())
        self.assertEqual(second.title, "A test video")

        # The cached copy and the first download are separate job files.
        self.assertNotEqual(first.path, second.path)

    def test_existing_download_is_reused_without_fetching_metadata(self):
        _write_video(self.options.output_dir / "video.mp4")
        self.options.metadata_dir.mkdir(parents=True, exist_ok=True)
        (self.options.metadata_dir / "source_info.json").write_text(
            json.dumps(INFO), encoding="utf-8"
        )
        with patch.object(ExistingYtDlpDownloader, "_fetch_info") as fetch_info, patch(
            "clipper.viral.download_adapter.download_full_video"
        ) as download_mock:
            video = self.downloader.download(
                "https://example.com/watch?v=abc123", self.options
            )
        fetch_info.assert_not_called()
        download_mock.assert_not_called()
        self.assertFalse(video.from_cache)
        self.assertEqual(video.duration, 120.0)

    def test_playlist_is_rejected(self):
        with self.assertRaises(PipelineError) as caught:
            self._download(info={**INFO, "_type": "playlist", "entries": [{}]})
        self.assertEqual(caught.exception.code, errors.UNSUPPORTED_URL)

    def test_live_stream_is_rejected(self):
        with self.assertRaises(PipelineError) as caught:
            self._download(info={**INFO, "is_live": True})
        self.assertEqual(caught.exception.code, errors.VIDEO_UNAVAILABLE)

    def test_private_availability_is_rejected(self):
        with self.assertRaises(PipelineError) as caught:
            self._download(info={**INFO, "availability": "private"})
        self.assertEqual(caught.exception.code, errors.VIDEO_PRIVATE_OR_AUTH_REQUIRED)

    def test_too_long_video_is_rejected_before_download(self):
        self.config.max_video_duration_seconds = 60
        with patch.object(
            ExistingYtDlpDownloader, "_fetch_info", return_value=dict(INFO)
        ), patch("clipper.viral.download_adapter.download_full_video") as download_mock:
            with self.assertRaises(PipelineError) as caught:
                self.downloader.download(
                    "https://example.com/watch?v=abc123", self.options
                )
        self.assertEqual(caught.exception.code, errors.VIDEO_TOO_LONG)
        download_mock.assert_not_called()

    def test_download_failure_is_retried_then_classified(self):
        with patch.object(
            ExistingYtDlpDownloader, "_fetch_info", return_value=dict(INFO)
        ), patch(
            "clipper.viral.download_adapter.download_full_video",
            side_effect=RuntimeError("Private video. Sign in to confirm your age."),
        ) as download_mock, patch("clipper.viral.download_adapter.time.sleep"):
            with self.assertRaises(PipelineError) as caught:
                self.downloader.download(
                    "https://example.com/watch?v=abc123", self.options
                )
        self.assertEqual(caught.exception.code, errors.VIDEO_PRIVATE_OR_AUTH_REQUIRED)
        self.assertEqual(download_mock.call_count, 1)

    def test_retry_recovers_after_a_transient_failure(self):
        self.config.max_retries = 2
        self.options.max_retries = 2
        calls = {"count": 0}

        def flaky(_url, target, _config):
            calls["count"] += 1
            if calls["count"] == 1:
                return False
            _write_video(target)
            return True

        with patch.object(
            ExistingYtDlpDownloader, "_fetch_info", return_value=dict(INFO)
        ), patch(
            "clipper.viral.download_adapter.download_full_video", side_effect=flaky
        ) as download_mock, patch(
            "clipper.viral.download_adapter.probe_duration", return_value=120.0
        ), patch.object(
            ExistingYtDlpDownloader, "_download_thumbnail", return_value=None
        ), patch("clipper.viral.download_adapter.time.sleep"):
            video = self.downloader.download(
                "https://example.com/watch?v=abc123", self.options
            )
        self.assertEqual(download_mock.call_count, 2)
        self.assertTrue(video.path.exists())

    def test_oversized_file_is_removed_and_rejected(self):
        self.config.max_file_size_mb = 0
        with patch.object(
            ExistingYtDlpDownloader, "_fetch_info", return_value=dict(INFO)
        ), patch(
            "clipper.viral.download_adapter.download_full_video",
            side_effect=lambda _url, target, _config: _write_video(target) or True,
        ), patch("clipper.viral.download_adapter.probe_duration", return_value=120.0):
            with self.assertRaises(PipelineError) as caught:
                self.downloader.download(
                    "https://example.com/watch?v=abc123", self.options
                )
        self.assertEqual(caught.exception.code, errors.FILE_TOO_LARGE)
        self.assertFalse((self.options.output_dir / "video.mp4").exists())

    def test_cache_key_is_stable_and_format_specific(self):
        first = self.downloader._cache_key("https://example.com/v", "bv*+ba/b")
        second = self.downloader._cache_key("https://example.com/v", "bv*+ba/b")
        other = self.downloader._cache_key("https://example.com/v", "best")
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertEqual(len(first), 64)


class ClassificationTests(unittest.TestCase):
    def test_classifies_common_ytdlp_failures(self):
        self.assertEqual(
            _classify_ytdlp_error("ERROR: Private video")[0],
            errors.VIDEO_PRIVATE_OR_AUTH_REQUIRED,
        )
        self.assertEqual(
            _classify_ytdlp_error("ERROR: Unsupported URL")[0],
            errors.UNSUPPORTED_URL,
        )
        self.assertEqual(
            _classify_ytdlp_error("ERROR: Video unavailable")[0],
            errors.VIDEO_UNAVAILABLE,
        )
        code, _message, retryable = _classify_ytdlp_error("network timeout")
        self.assertEqual(code, errors.DOWNLOAD_FAILED)
        self.assertTrue(retryable)


if __name__ == "__main__":
    unittest.main()
