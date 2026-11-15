"""End-to-end orchestration for one viral clipping job."""

from __future__ import annotations

import shutil
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol

from clipper.viral import errors
from clipper.viral.analysis import AnalysisResult, TranscriptAnalyzer, build_analyzer
from clipper.viral.config import ViralConfig
from clipper.viral.download_adapter import ExistingYtDlpDownloader, VideoDownloader
from clipper.viral.errors import PipelineError
from clipper.viral.extraction import ClipExtractor, FFmpegClipExtractor, clip_filename
from clipper.viral.logging_utils import JobLogger
from clipper.viral.models import (
    DownloadedVideo,
    DownloadOptions,
    JobOptions,
    PipelineResult,
    Transcript,
    utc_now,
)
from clipper.viral.packaging import (
    address,
    build_result,
    build_zip,
    create_job_dirs,
    write_clip_metadata,
    write_json,
    write_report,
)
from clipper.viral.subtitles import write_clip_subtitles, write_transcript_files
from clipper.viral.timing import validate_clips
from clipper.viral.transcription import Transcriber, TranscriptOptions, build_transcriber
from clipper.viral.validation import ensure_within, validate_job_id

ProgressCallback = Callable[[float, str, str], None]


class ViralPipeline:
    def __init__(
        self,
        config: ViralConfig,
        downloader: VideoDownloader,
        transcriber: Transcriber,
        analyzer: TranscriptAnalyzer,
        extractor: ClipExtractor,
    ) -> None:
        self.config = config
        self.downloader = downloader
        self.transcriber = transcriber
        self.analyzer = analyzer
        self.extractor = extractor

    def run(
        self,
        job_id: str,
        source_url: str,
        options: JobOptions,
        created_at: Optional[str] = None,
        progress_callback: Optional[ProgressCallback] = None,
        logger: Optional[JobLogger] = None,
        job_root: Optional[Path] = None,
    ) -> PipelineResult:
        job_id = validate_job_id(job_id)
        job_root = ensure_within(
            self.config.output_dir,
            Path(job_root) if job_root is not None else self.config.output_dir / job_id,
        )
        dirs = create_job_dirs(job_root)
        logger = logger or JobLogger(job_id, dirs["logs"] / "job.jsonl")

        def progress(value: float, step: str, detail: str = "") -> None:
            if progress_callback:
                progress_callback(max(0.0, min(100.0, value)), step, detail)

        try:
            # 1. Download -------------------------------------------------
            progress(5, "downloading", "Preparing the download")
            download_options = DownloadOptions(
                output_dir=dirs["original"],
                metadata_dir=dirs["metadata"],
                cache_dir=Path(self.config.cache_dir or (self.config.output_dir / ".cache")),
                format=options.download_format or self.config.download_format,
                max_retries=self.config.max_retries,
                timeout_seconds=self.config.download_timeout_seconds,
                force=False,
            )
            video = self.downloader.download(source_url, download_options)
            if not video.path.exists():
                raise PipelineError(errors.DOWNLOAD_FAILED, "The downloaded video file is missing.", retryable=True)
            if video.duration <= 0:
                raise PipelineError(errors.DOWNLOAD_FAILED, "The downloaded video has no readable duration.", retryable=True)
            write_json(dirs["metadata"] / "download.json", video.to_dict())
            progress(25, "downloading", "Video ready")

            # 2. Transcription --------------------------------------------
            logger.started("transcription", provider=self.config.transcription_provider)
            progress(30, "transcribing", "Extracting the transcript")
            transcript = self.transcriber.transcribe(
                video,
                TranscriptOptions(
                    language=options.language,
                    output_dir=dirs["transcript"],
                    chunk_seconds=self.config.stt_chunk_seconds,
                    keep_audio=self.config.keep_audio,
                    timeout_seconds=self.config.download_timeout_seconds,
                ),
            )
            if not transcript.segments:
                raise PipelineError(errors.TRANSCRIPTION_FAILED, "The transcript contains no speech.", retryable=True)
            transcript_files = self._write_transcripts(transcript, dirs["transcript"])
            logger.completed(
                "transcription",
                segments=len(transcript.segments),
                provider=transcript.provider,
                language=transcript.language,
            )
            progress(50, "transcribing", "Transcript ready")

            # 3. Analysis -------------------------------------------------
            logger.started("analysis", provider=self.config.llm_provider, model=self.config.llm_model)
            progress(55, "analyzing", "Looking for viral moments")
            analysis = self.analyzer.analyze(transcript, options)
            write_json(
                dirs["metadata"] / "analysis_raw.json",
                {
                    "provider": analysis.provider,
                    "model": analysis.model,
                    "attempts": analysis.attempts,
                    "clips": analysis.clips,
                },
            )
            logger.completed(
                "analysis",
                proposals=len(analysis.clips),
                provider=analysis.provider,
                model=analysis.model,
            )
            progress(70, "analyzing", "Candidate moments selected")

            # 4. Validation ----------------------------------------------
            logger.started("validation", proposals=len(analysis.clips))
            progress(72, "validating", "Checking sentence boundaries and durations")
            clips = validate_clips(analysis.clips, transcript, options, self.config)
            write_json(dirs["metadata"] / "validated_clips.json", [clip.to_dict() for clip in clips])
            logger.completed(
                "validation",
                accepted=len(clips),
                requested=options.number_of_clips,
                rejected=max(0, len(analysis.clips) - len(clips)),
            )
            progress(78, "validating", f"{len(clips)} clips validated")

            # 5. Extraction ----------------------------------------------
            logger.started("clip_extraction", clips=len(clips))
            clip_files: Dict[int, Dict[str, Optional[Path]]] = {}
            for index, clip in enumerate(clips, start=1):
                name = clip_filename(index, clip)
                output_path = dirs["clips"] / name
                progress(80 + (14 * (index - 1) / max(1, len(clips))), "extracting_clips", f"Clip {index}/{len(clips)}")
                try:
                    self.extractor.extract(video, clip, output_path)
                except PipelineError as exc:
                    raise PipelineError(
                        exc.code,
                        exc.message,
                        technical_details=f"clip rank {clip.rank} ({clip.title}): {exc.technical_details}",
                        retryable=exc.retryable,
                    ) from exc
                files: Dict[str, Optional[Path]] = {"video": output_path, "srt": None, "vtt": None, "txt": None}
                if options.generate_subtitles:
                    subtitle_paths = write_clip_subtitles(clip, transcript, dirs["clips"], output_path.stem)
                    files.update(
                        {
                            "srt": subtitle_paths.get("srt"),
                            "vtt": subtitle_paths.get("vtt"),
                            "txt": subtitle_paths.get("txt"),
                            "transcript": subtitle_paths.get("transcript"),
                        }
                    )
                clip_files[clip.rank] = files
                write_clip_metadata(clip, dirs["metadata"], files)
                logger.event("clip_ready", rank=clip.rank, file=output_path.name, duration=clip.duration)
            logger.completed("clip_extraction", clips=len(clips))
            progress(95, "extracting_clips", "Clips extracted")

            # 6. Packaging -----------------------------------------------
            logger.started("packaging")
            progress(96, "packaging", "Building the report and ZIP")
            original_retained = bool(options.download_original)
            if not original_retained:
                video.path.unlink(missing_ok=True)
            result = build_result(
                job_id=job_id,
                source_url=source_url,
                created_at=created_at or utc_now(),
                options=options,
                video=video,
                transcript=transcript,
                clips=clips,
                job_root=job_root,
                transcript_files=transcript_files,
                clip_files=clip_files,
                analysis_meta={
                    "provider": analysis.provider,
                    "model": analysis.model,
                    "attempts": analysis.attempts,
                },
                original_retained=original_retained,
            )
            result_path = write_json(job_root / "result.json", result)
            report_path = write_report(result, job_root / "report.md")
            package_path = build_zip(
                job_root,
                result_path,
                report_path,
                job_root / "package.zip",
                include_original=original_retained,
            )
            logger.completed("packaging", package=str(package_path), clips=len(clips))
            logger.event("job_completed", clips=len(clips), package=str(package_path))
            progress(100, "completed", "Done")
            return PipelineResult(
                result=result,
                output_dir=job_root,
                result_path=result_path,
                report_path=report_path,
                package_path=package_path,
            )
        except PipelineError:
            raise
        except Exception as exc:
            raise PipelineError(
                errors.INTERNAL_ERROR,
                "An unexpected error interrupted the job.",
                technical_details=traceback.format_exc(),
                retryable=True,
            ) from exc
        finally:
            # Keep the temp folder only when it still contains useful debug
            # artifacts; normal transcriber cleanup leaves it empty.
            try:
                if dirs.get("temp") and dirs["temp"].exists() and not any(dirs["temp"].iterdir()):
                    dirs["temp"].rmdir()
            except OSError:
                pass

    def _write_transcripts(self, transcript: Transcript, directory: Path) -> Dict[str, Path]:
        raw_segments = transcript.raw.get("segments") if isinstance(transcript.raw, dict) else None
        if not isinstance(raw_segments, list) or not raw_segments:
            raw_segments = [segment.to_dict() for segment in transcript.segments]
        raw_payload = {
            "language": transcript.language,
            "duration": transcript.duration,
            "provider": transcript.provider,
            "segments": raw_segments,
            "meta": {k: v for k, v in transcript.raw.items() if k != "segments"},
        }
        raw_path = write_json(directory / "raw.json", raw_payload)
        clean_path = write_json(directory / "clean.json", transcript.to_dict())
        paths = write_transcript_files(transcript, directory)
        result = {"raw": raw_path, "clean": clean_path, **paths}
        return result


def build_pipeline(config: ViralConfig, logger: Optional[JobLogger] = None) -> ViralPipeline:
    """Construct the production pipeline with the project's existing downloader."""
    return ViralPipeline(
        config=config,
        downloader=ExistingYtDlpDownloader(config, logger),
        transcriber=build_transcriber(config, logger),
        analyzer=build_analyzer(config, logger),
        extractor=FFmpegClipExtractor(config, logger),
    )
