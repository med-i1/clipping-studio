import json
import shutil
import subprocess
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path

from clipper.viral.analysis import AnalysisResult
from clipper.viral.config import ViralConfig
from clipper.viral.extraction import FFmpegClipExtractor
from clipper.viral.media import probe_duration
from clipper.viral.models import (
    DownloadedVideo,
    DownloadOptions,
    JobOptions,
    Transcript,
    TranscriptSegment,
)
from clipper.viral.pipeline import ViralPipeline

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


class LocalDownloader:
    def __init__(self, source: Path):
        self.source = source

    def download(self, url, options: DownloadOptions) -> DownloadedVideo:
        options.output_dir.mkdir(parents=True, exist_ok=True)
        target = options.output_dir / "video.mp4"
        shutil.copy2(self.source, target)
        return DownloadedVideo(
            path=target,
            title="Synthetic smoke video",
            duration=6.0,
            source_url=url,
            quality="240p",
            filesize=target.stat().st_size,
        )


class LocalTranscriber:
    def transcribe(self, video, options):
        segments = [
            TranscriptSegment(index + 1, index, index + 1, f"Sentence {index + 1}.")
            for index in range(6)
        ]
        return Transcript("en", 6.0, segments, provider="test", raw={"segments": [s.to_dict() for s in segments]})


class LocalAnalyzer:
    def analyze(self, transcript, options):
        return AnalysisResult(
            clips=[
                {
                    "title": "A real clip",
                    "hook": "Hook",
                    "start_time": 1.0,
                    "end_time": 4.0,
                    "viral_score": 88,
                    "category": "test",
                    "reason": "Test",
                    "summary": "Test",
                    "platforms": ["tiktok"],
                    "keywords": ["test"],
                    "confidence": 0.8,
                }
            ],
            provider="test",
            model="test",
        )


@unittest.skipUnless(FFMPEG and FFPROBE, "ffmpeg and ffprobe are required")
class RealPipelineSmokeTest(unittest.TestCase):
    def test_pipeline_writes_real_mp4_clip_report_and_zip(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.mp4"
            subprocess.run(
                [
                    FFMPEG, "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=6",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", "-y", str(source),
                ],
                check=True,
                timeout=120,
            )
            config = ViralConfig(
                project_root=root,
                output_dir=root / "out",
                temp_dir=root / "tmp",
                cache_dir=root / "cache",
                prompt_file=root / "prompt.txt",
            )
            config.prompt_file.write_text("prompt", encoding="utf-8")
            pipeline = ViralPipeline(
                config,
                LocalDownloader(source),
                LocalTranscriber(),
                LocalAnalyzer(),
                FFmpegClipExtractor(config),
            )
            outcome = pipeline.run(
                job_id=uuid.uuid4().hex,
                source_url="https://example.com/v",
                options=JobOptions(
                    language="en",
                    number_of_clips=1,
                    min_clip_duration=1,
                    max_clip_duration=5,
                ),
            )
            result = json.loads(outcome.result_path.read_text(encoding="utf-8"))
            clip_path = outcome.output_dir / result["clips"][0]["file"]
            self.assertTrue(clip_path.exists())
            self.assertGreater(clip_path.stat().st_size, 10 * 1024)
            self.assertLessEqual(abs(probe_duration(clip_path, FFPROBE) - 3.0), 1.0)
            audio = subprocess.run(
                [
                    FFPROBE, "-v", "error", "-select_streams", "a:0",
                    "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(clip_path),
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertIn("audio", audio.stdout)
            self.assertTrue(outcome.report_path.exists())
            with zipfile.ZipFile(outcome.package_path) as archive:
                names = set(archive.namelist())
            self.assertIn(result["clips"][0]["file"], names)
            self.assertIn("result.json", names)


if __name__ == "__main__":
    unittest.main()
