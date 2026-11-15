"""Caption download and speech-to-text adapters with a fallback chain."""

from __future__ import annotations

import importlib.util
import json
import math
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Tuple

from clipper.viral import errors
from clipper.viral.config import ViralConfig
from clipper.viral.errors import PipelineError, redact
from clipper.viral.media import (
    cut_audio_chunk,
    extract_audio,
    has_audio_stream,
    probe_duration,
)
from clipper.viral.models import DownloadedVideo, Transcript, TranscriptSegment
from clipper.viral.net import HttpRequestError, post_multipart
from clipper.viral.subtitles import (
    caption_language_from_path,
    normalize_caption_text,
    parse_caption_file,
)

_NON_SPEECH = {
    "music", "applause", "laughter", "silence", "موسيقى", "تصفيق", "ضحك",
    "صمت", "[music]", "[applause]", "[laughter]", "[silence]",
}


class NoCaptionsAvailable(Exception):
    """Internal signal that the next transcription backend should be tried."""


@dataclass
class TranscriptOptions:
    language: str = "auto"
    output_dir: Path = Path(".")
    chunk_seconds: int = 600
    keep_audio: bool = False
    timeout_seconds: int = 1800


class Transcriber(Protocol):
    name: str

    def transcribe(self, video: DownloadedVideo, options: TranscriptOptions) -> Transcript:
        ...


def clean_transcript_segments(
    segments: Sequence[TranscriptSegment],
    duration: float,
) -> List[TranscriptSegment]:
    """Normalize text/timings while preserving segment-level confidence."""
    cleaned: List[TranscriptSegment] = []
    for segment in sorted(segments, key=lambda s: (s.start, s.end)):
        text = normalize_caption_text(segment.text)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        if text.lower().strip("[]() ") in _NON_SPEECH or text in {"♪", "♫"}:
            continue
        start = max(0.0, float(segment.start))
        end = min(float(duration), float(segment.end)) if duration > 0 else float(segment.end)
        if end - start <= 0.01:
            continue
        if cleaned and cleaned[-1].text == text and start - cleaned[-1].end < 0.05:
            cleaned[-1].end = max(cleaned[-1].end, end)
            continue
        cleaned.append(
            TranscriptSegment(
                id=len(cleaned) + 1,
                start=start,
                end=end,
                text=text,
                confidence=segment.confidence,
            )
        )
    return cleaned


class CaptionTranscriber:
    """Download native subtitles/auto-captions through yt-dlp first."""

    name = "captions"

    def __init__(self, config: ViralConfig, logger: Any = None) -> None:
        self.config = config
        self.logger = logger

    def transcribe(self, video: DownloadedVideo, options: TranscriptOptions) -> Transcript:
        info = video.raw_metadata or {}
        manual = info.get("subtitles") or {}
        automatic = info.get("automatic_captions") or {}
        if not isinstance(manual, dict):
            manual = {}
        if not isinstance(automatic, dict):
            automatic = {}
        info_language = str(info.get("language") or "").split("-")[0].lower()
        requested = options.language if options.language != "auto" else (info_language or "auto")
        caption_dir = options.output_dir / "captions"
        caption_dir.mkdir(parents=True, exist_ok=True)

        attempts: List[Tuple[str, List[str]]] = []
        manual_langs = self._pick_languages(manual, requested, info_language)
        if manual_langs:
            attempts.append(("manual", manual_langs))
        auto_langs = self._pick_languages(automatic, requested, info_language)
        if auto_langs:
            attempts.append(("auto", auto_langs))
        if not attempts:
            raise NoCaptionsAvailable("No native captions are listed for this video.")

        errors_seen = []
        for kind, languages in attempts:
            try:
                files = self._download(video, kind, languages, caption_dir / kind, options.timeout_seconds)
            except PipelineError as exc:
                errors_seen.append(str(exc))
                continue
            for path in self._preferred_files(files, languages):
                segments = parse_caption_file(path)
                if not segments:
                    continue
                language = caption_language_from_path(path)
                cleaned = clean_transcript_segments(segments, video.duration)
                if not cleaned:
                    continue
                if language == "unknown":
                    language = requested if requested != "auto" else "unknown"
                if self.logger:
                    self.logger.event(
                        "transcription_captions",
                        source=kind,
                        language=language,
                        segments=len(cleaned),
                    )
                return Transcript(
                    language=language,
                    duration=video.duration,
                    segments=cleaned,
                    provider=f"youtube_captions:{kind}:{language}",
                    raw={
                        "caption_file": path.name,
                        "caption_kind": kind,
                        "available_manual": sorted(manual.keys()),
                        "available_automatic": sorted(automatic.keys()),
                        "segments": [segment.to_dict() for segment in segments],
                    },
                )
        reason = "; ".join(errors_seen) if errors_seen else "Caption files were empty or unparseable."
        raise NoCaptionsAvailable(reason)

    def _pick_languages(self, source: Dict[str, Any], requested: str, info_language: str) -> List[str]:
        keys = [str(k) for k in source.keys() if k and k not in ("live_chat",) and re.fullmatch(r"[a-zA-Z]{2,3}(?:-[a-zA-Z0-9]{2,8})?", str(k))]
        if not keys:
            return []
        order: List[str] = []

        def add(value: str) -> None:
            if value in keys and value not in order:
                order.append(value)

        if requested and requested != "auto":
            base = requested.split("-")[0]
            add(requested)
            add(base)
            add(base + "-orig")
            for key in keys:
                if key.lower().startswith(base.lower()):
                    add(key)
        for base in (info_language, "ar", "en"):
            if not base:
                continue
            add(base)
            add(base + "-orig")
            for key in keys:
                if key.lower().startswith(base.lower()):
                    add(key)
        for key in sorted(keys):
            add(key)
            if len(order) >= 4:
                break
        return order[:4]

    def _download(
        self,
        video: DownloadedVideo,
        kind: str,
        languages: List[str],
        caption_dir: Path,
        timeout: int,
    ) -> List[Path]:
        caption_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            self.config.ytdlp_bin,
            "--skip-download",
            "--no-playlist",
            "--no-warnings",
            "--socket-timeout", "20",
            "--sub-langs", ",".join(languages),
            "--sub-format", "json3/vtt/srt/best",
            "-o", str(caption_dir / "captions.%(ext)s"),
        ]
        if kind == "manual":
            cmd.append("--write-subs")
        else:
            cmd.append("--write-auto-subs")
        cmd.append(video.source_url)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=min(timeout, 300),
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as exc:
            raise PipelineError(
                errors.CONFIGURATION_ERROR,
                "yt-dlp was not found. Install it or set YTDLP_BIN.",
                technical_details=str(exc),
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise PipelineError(
                errors.TIMEOUT,
                "Downloading captions took too long.",
                technical_details=str(exc),
                retryable=True,
            ) from exc
        if result.returncode != 0:
            files = sorted(caption_dir.glob("captions.*"))
            if not files:
                raise PipelineError(
                    errors.TRANSCRIPTION_FAILED,
                    "Native captions could not be downloaded.",
                    technical_details=result.stderr or result.stdout,
                    retryable=True,
                )
            if self.logger:
                self.logger.event(
                    "transcription_captions_partial",
                    level="WARNING",
                    message=(
                        "Some caption languages failed to download; "
                        "using the files that were written."
                    ),
                    returncode=result.returncode,
                )
            return files
        return sorted(caption_dir.glob("captions.*"))

    def _preferred_files(self, files: Sequence[Path], languages: Sequence[str]) -> List[Path]:
        suffix_rank = {".json3": 0, ".vtt": 1, ".srt": 2}
        valid = [p for p in files if p.suffix.lower() in suffix_rank]
        language_rank = {lang: index for index, lang in enumerate(languages)}

        def key(path: Path) -> Tuple[int, int, str]:
            language = caption_language_from_path(path)
            return (
                language_rank.get(language, 99),
                suffix_rank.get(path.suffix.lower(), 99),
                path.name,
            )

        return sorted(valid, key=key)


class OpenAIWhisperTranscriber:
    """OpenAI-compatible ``/audio/transcriptions`` adapter."""

    name = "openai_whisper"

    def __init__(
        self,
        config: ViralConfig,
        logger: Any = None,
        transport: Optional[Callable[..., Dict[str, Any]]] = None,
    ) -> None:
        self.config = config
        self.logger = logger
        self.transport = transport or post_multipart

    def transcribe(self, video: DownloadedVideo, options: TranscriptOptions) -> Transcript:
        if not self.config.transcription_api_key:
            raise PipelineError(
                errors.CONFIGURATION_ERROR,
                "No transcription API key is configured.",
                technical_details="Set TRANSCRIPTION_API_KEY or LLM_API_KEY.",
            )
        if not has_audio_stream(video.path, self.config.ffprobe_bin):
            raise PipelineError(errors.NO_AUDIO_STREAM, "The video has no audio track.")

        audio_dir = options.output_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        audio_path = audio_dir / "audio.mp3"
        try:
            extract_audio(
                video.path,
                audio_path,
                ffmpeg_bin=self.config.ffmpeg_bin,
                timeout=options.timeout_seconds,
            )
            duration = float(probe_duration(audio_path, self.config.ffprobe_bin) or video.duration or 0)
            chunks = self._chunk_audio(audio_path, audio_dir, duration, options)
            all_segments: List[TranscriptSegment] = []
            detected_language = self.config.transcription_language
            for index, (chunk_path, offset, chunk_duration) in enumerate(chunks, start=1):
                if self.logger:
                    self.logger.event("transcription_chunk", chunk=index, total=len(chunks), offset=offset)
                response = self._transcribe_chunk(chunk_path, options.language)
                if detected_language == "auto" and response.get("language"):
                    detected_language = str(response["language"])
                all_segments.extend(self._segments_from_response(response, offset, chunk_duration))
            cleaned = clean_transcript_segments(all_segments, max(video.duration, duration))
            if not cleaned:
                raise PipelineError(
                    errors.TRANSCRIPTION_FAILED,
                    "The speech-to-text provider returned no speech.",
                    retryable=True,
                )
            return Transcript(
                language=detected_language if detected_language != "auto" else "unknown",
                duration=max(video.duration, duration),
                segments=cleaned,
                provider=f"openai_whisper:{self.config.transcription_model}",
                raw={
                    "chunks": len(chunks),
                    "segments": [segment.to_dict() for segment in all_segments],
                },
            )
        finally:
            if not options.keep_audio:
                shutil.rmtree(audio_dir, ignore_errors=True)

    def _chunk_audio(
        self,
        audio_path: Path,
        audio_dir: Path,
        duration: float,
        options: TranscriptOptions,
    ) -> List[Tuple[Path, float, float]]:
        chunk_seconds = max(30, options.chunk_seconds)
        if duration <= chunk_seconds or duration <= 0:
            return [(audio_path, 0.0, duration)]
        chunks = []
        start = 0.0
        index = 1
        while start < duration:
            length = min(chunk_seconds, duration - start)
            if length < 1:
                break
            chunk_path = audio_dir / f"chunk_{index:03d}.mp3"
            cut_audio_chunk(
                audio_path,
                chunk_path,
                start,
                length,
                ffmpeg_bin=self.config.ffmpeg_bin,
                timeout=options.timeout_seconds,
            )
            chunks.append((chunk_path, start, length))
            start += length
            index += 1
        return chunks

    def _transcribe_chunk(self, chunk_path: Path, language: str) -> Dict[str, Any]:
        url = f"{self.config.transcription_base_url.rstrip('/')}/audio/transcriptions"
        fields = {
            "model": self.config.transcription_model,
            "response_format": "verbose_json",
            "timestamp_granularities[]": "segment",
        }
        if language and language != "auto":
            fields["language"] = language
        headers = {"Authorization": f"Bearer {self.config.transcription_api_key}"}
        last_error: Optional[Exception] = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                return self.transport(url, fields, [("file", chunk_path, "audio/mpeg")], headers, 300)
            except HttpRequestError as exc:
                last_error = exc
                if exc.status in (400, 401, 403, 404):
                    raise PipelineError(
                        errors.CONFIGURATION_ERROR,
                        "The transcription provider rejected the request. Check the API key and model.",
                        technical_details=f"HTTP {exc.status}: {exc.body}",
                    ) from exc
                if attempt < self.config.max_retries:
                    time.sleep(min(8, 2 ** attempt))
                    continue
            except (ValueError, PipelineError) as exc:
                last_error = exc
                if isinstance(exc, PipelineError) and not exc.retryable:
                    raise
                if attempt < self.config.max_retries:
                    time.sleep(min(8, 2 ** attempt))
                    continue
        raise PipelineError(
            errors.TRANSCRIPTION_FAILED,
            "Speech-to-text failed after retries.",
            technical_details=redact(str(last_error)),
            retryable=True,
        )

    def _segments_from_response(
        self,
        response: Dict[str, Any],
        offset: float,
        chunk_duration: float,
    ) -> List[TranscriptSegment]:
        raw_segments = response.get("segments")
        segments: List[TranscriptSegment] = []
        if isinstance(raw_segments, list) and raw_segments:
            for item in raw_segments:
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text") or "").strip()
                if not text:
                    continue
                start = float(item.get("start") or 0) + offset
                end = float(item.get("end") or (item.get("start") or 0)) + offset
                if end <= start:
                    end = start + 1.0
                if chunk_duration > 0:
                    end = min(end, offset + chunk_duration)
                confidence = None
                if item.get("avg_logprob") is not None:
                    try:
                        confidence = max(0.0, min(1.0, math.exp(float(item["avg_logprob"]))))
                    except (TypeError, ValueError, OverflowError):
                        confidence = None
                segments.append(TranscriptSegment(len(segments) + 1, start, end, text, confidence))
        else:
            text = str(response.get("text") or "").strip()
            if text:
                segments.append(TranscriptSegment(1, offset, offset + max(1.0, chunk_duration), text, None))
        return segments


class LocalWhisperTranscriber:
    """Optional faster-whisper adapter; imported only when selected."""

    name = "faster_whisper"

    def __init__(self, config: ViralConfig, logger: Any = None) -> None:
        self.config = config
        self.logger = logger

    def transcribe(self, video: DownloadedVideo, options: TranscriptOptions) -> Transcript:
        if importlib.util.find_spec("faster_whisper") is None:
            raise PipelineError(
                errors.CONFIGURATION_ERROR,
                "faster-whisper is not installed.",
                technical_details="Install it with: pip install faster-whisper",
            )
        if not has_audio_stream(video.path, self.config.ffprobe_bin):
            raise PipelineError(errors.NO_AUDIO_STREAM, "The video has no audio track.")
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except ImportError as exc:
            raise PipelineError(
                errors.CONFIGURATION_ERROR,
                "faster-whisper could not be imported.",
                technical_details=str(exc),
            ) from exc
        model_name = self.config.local_whisper_model
        if self.logger:
            self.logger.event("transcription_local_model", model=model_name)
        try:
            model = WhisperModel(model_name, device="auto", compute_type="int8")
            language = None if options.language == "auto" else options.language
            segments_iter, info = model.transcribe(
                str(video.path),
                language=language,
                vad_filter=True,
                beam_size=5,
            )
            segments = []
            for item in segments_iter:
                confidence = None
                if getattr(item, "avg_logprob", None) is not None:
                    confidence = max(0.0, min(1.0, math.exp(float(item.avg_logprob))))
                segments.append(
                    TranscriptSegment(
                        id=len(segments) + 1,
                        start=float(item.start),
                        end=float(item.end),
                        text=str(item.text or ""),
                        confidence=confidence,
                    )
                )
        except Exception as exc:
            raise PipelineError(
                errors.TRANSCRIPTION_FAILED,
                "Local speech-to-text failed.",
                technical_details=str(exc),
                retryable=True,
            ) from exc
        cleaned = clean_transcript_segments(segments, video.duration)
        if not cleaned:
            raise PipelineError(errors.TRANSCRIPTION_FAILED, "No speech was detected in this video.", retryable=True)
        detected = getattr(info, "language", None) or options.language
        return Transcript(
            language=str(detected or "unknown"),
            duration=video.duration,
            segments=cleaned,
            provider=f"faster_whisper:{model_name}",
            raw={
                "language_probability": getattr(info, "language_probability", None),
                "segments": [segment.to_dict() for segment in segments],
            },
        )


class FallbackTranscriber:
    """Try each configured backend in order and aggregate useful errors."""

    name = "fallback"

    def __init__(self, backends: Sequence[Transcriber], logger: Any = None) -> None:
        self.backends = list(backends)
        self.logger = logger

    def transcribe(self, video: DownloadedVideo, options: TranscriptOptions) -> Transcript:
        failures: List[str] = []
        for backend in self.backends:
            if self.logger:
                self.logger.event("transcription_backend", backend=backend.name)
            try:
                return backend.transcribe(video, options)
            except NoCaptionsAvailable as exc:
                failures.append(f"{backend.name}: {exc}")
            except PipelineError as exc:
                failures.append(f"{backend.name}: {exc.code}: {exc.message}")
                if exc.code in (errors.CONFIGURATION_ERROR, errors.NO_AUDIO_STREAM) and backend is self.backends[-1]:
                    raise
            except Exception as exc:
                failures.append(f"{backend.name}: {exc}")
        if not self.backends:
            raise PipelineError(
                errors.TRANSCRIPTION_UNAVAILABLE,
                "No transcription provider is configured.",
                technical_details="Set TRANSCRIPTION_PROVIDER and related variables in .env.",
            )
        raise PipelineError(
            errors.TRANSCRIPTION_FAILED,
            "No transcript could be produced for this video.",
            technical_details=" | ".join(failures),
            retryable=True,
        )


def build_transcriber(config: ViralConfig, logger: Any = None) -> Transcriber:
    provider = (config.transcription_provider or "auto").lower()
    backends: List[Transcriber] = []
    if provider in ("auto", "captions", "youtube"):
        backends.append(CaptionTranscriber(config, logger))
    if provider in ("auto", "openai", "openai_whisper"):
        if config.transcription_api_key:
            backends.append(OpenAIWhisperTranscriber(config, logger))
        elif provider == "openai":
            raise PipelineError(
                errors.CONFIGURATION_ERROR,
                "TRANSCRIPTION_PROVIDER=openai requires TRANSCRIPTION_API_KEY.",
            )
    if provider in ("auto", "local", "faster_whisper"):
        if importlib.util.find_spec("faster_whisper") is not None:
            backends.append(LocalWhisperTranscriber(config, logger))
        elif provider == "local":
            raise PipelineError(
                errors.CONFIGURATION_ERROR,
                "TRANSCRIPTION_PROVIDER=local requires the faster-whisper package.",
                technical_details="Install it with: pip install faster-whisper",
            )
    if provider == "none":
        raise PipelineError(
            errors.TRANSCRIPTION_UNAVAILABLE,
            "Transcription is disabled (TRANSCRIPTION_PROVIDER=none).",
        )
    if not backends:
        raise PipelineError(
            errors.TRANSCRIPTION_UNAVAILABLE,
            "No transcription provider is available.",
            technical_details="Configure captions, an API key, or install faster-whisper.",
        )
    return FallbackTranscriber(backends, logger)
