"""Timing validation and sentence-boundary repair for proposed clips."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from clipper.viral import errors
from clipper.viral.analysis import AnalysisJSONError, normalize_clip_payload
from clipper.viral.config import ViralConfig
from clipper.viral.errors import PipelineError
from clipper.viral.models import ClipCandidate, JobOptions, Transcript, TranscriptSegment

_EPS = 0.05


def _boundaries(transcript: Transcript) -> Tuple[List[float], List[float]]:
    starts = sorted({round(s.start, 3) for s in transcript.segments})
    ends = sorted({round(s.end, 3) for s in transcript.segments})
    return starts, ends


def _containing(segments: Sequence[TranscriptSegment], point: float) -> Optional[TranscriptSegment]:
    for segment in segments:
        if segment.start - _EPS <= point <= segment.end + _EPS:
            return segment
    return None


def _snap_start(start: float, transcript: Transcript, tolerance: float) -> Tuple[float, bool]:
    starts, _ = _boundaries(transcript)
    segment = _containing(transcript.segments, start)
    if segment is not None:
        if start - segment.start <= 0.35:
            return segment.start, abs(segment.start - start) > _EPS
        if segment.end - start <= 0.35:
            return segment.end, abs(segment.end - start) > _EPS
        # Starting inside a sentence is the most common failure mode.
        return segment.start, True
    if not starts:
        return start, False
    nearest = min(starts, key=lambda value: abs(value - start))
    if abs(nearest - start) <= tolerance:
        return nearest, abs(nearest - start) > _EPS
    return start, False


def _snap_end(end: float, transcript: Transcript, tolerance: float) -> Tuple[float, bool]:
    _, ends = _boundaries(transcript)
    segment = _containing(transcript.segments, end)
    if segment is not None:
        if segment.end - end <= 0.35:
            return segment.end, abs(segment.end - end) > _EPS
        if end - segment.start <= 0.35:
            return segment.start, abs(segment.start - end) > _EPS
        # Ending mid-sentence cuts the payoff; move to the sentence end.
        return segment.end, True
    if not ends:
        return end, False
    nearest = min(ends, key=lambda value: abs(value - end))
    if abs(nearest - end) <= tolerance:
        return nearest, abs(nearest - end) > _EPS
    return end, False


def _text_for_range(segments: Sequence[TranscriptSegment], start: float, end: float) -> str:
    parts = []
    for segment in segments:
        if segment.end <= start or segment.start >= end:
            continue
        parts.append(segment.text)
    return " ".join(parts).strip()


def _tokens(text: str) -> set:
    return {token for token in re.findall(r"[\w\u0600-\u06FF]{2,}", text.lower())}


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    left, right = _tokens(a), _tokens(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def validate_clips(
    proposals: Sequence[Dict[str, Any]],
    transcript: Transcript,
    options: JobOptions,
    config: ViralConfig,
) -> List[ClipCandidate]:
    """Validate and repair model timings against the transcript.

    The model is never trusted for boundaries, duration, or overlap. Every
    adjustment is recorded on the clip for the final report.
    """
    video_duration = float(transcript.duration or 0)
    if video_duration <= 0 and transcript.segments:
        video_duration = max(s.end for s in transcript.segments)
    if video_duration <= 0:
        raise PipelineError(errors.NO_VALID_CLIPS, "The transcript has no usable duration.")
    starts, ends = _boundaries(transcript)
    duration = video_duration
    accepted: List[ClipCandidate] = []
    rejected: List[str] = []

    normalized: List[Dict[str, Any]] = []
    for index, raw in enumerate(proposals):
        try:
            normalized.append(normalize_clip_payload(raw, index))
        except AnalysisJSONError as exc:
            rejected.append(f"proposal {index + 1}: {exc}")

    for proposal in normalized:
        adjustments: List[str] = []
        start = float(proposal["start_time"])
        end = float(proposal["end_time"])

        if start < 0:
            start = 0.0
            adjustments.append("clamped_start_to_zero")
        if end > duration:
            end = duration
            adjustments.append("clamped_end_to_video_duration")
        if start >= duration:
            rejected.append(f"{proposal['title']}: starts outside the video")
            continue
        if end <= start:
            rejected.append(f"{proposal['title']}: end <= start after clamping")
            continue

        snapped_start, changed = _snap_start(start, transcript, config.snap_tolerance_seconds)
        if changed and 0 <= snapped_start < end:
            start = snapped_start
            adjustments.append("snapped_start_to_sentence")
        snapped_end, changed = _snap_end(end, transcript, config.snap_tolerance_seconds)
        if changed and snapped_end > start:
            end = snapped_end
            adjustments.append("snapped_end_to_sentence")

        # Enforce minimum length by extending to the next sentence boundary.
        if end - start < options.min_clip_duration - _EPS:
            desired_end = start + options.min_clip_duration
            limit_end = min(duration, start + options.max_clip_duration)
            candidate_ends = [value for value in ends if desired_end - _EPS <= value <= limit_end + _EPS]
            if candidate_ends:
                end = candidate_ends[0]
                adjustments.append("extended_end_to_min_duration")
            else:
                desired_start = end - options.min_clip_duration
                candidate_starts = [
                    value for value in starts
                    if max(0.0, end - options.max_clip_duration) - _EPS <= value <= desired_start + _EPS
                ]
                if candidate_starts:
                    start = candidate_starts[-1]
                    adjustments.append("extended_start_to_min_duration")
                elif end - options.min_clip_duration >= 0:
                    start = end - options.min_clip_duration
                    adjustments.append("extended_start_to_min_duration")
                elif start + options.min_clip_duration <= duration:
                    end = start + options.min_clip_duration
                    adjustments.append("extended_end_to_min_duration")

        # Enforce maximum length without cutting mid-sentence when possible.
        if end - start > options.max_clip_duration + _EPS:
            limit = start + options.max_clip_duration
            candidate_ends = [
                value for value in ends
                if start + options.min_clip_duration - _EPS <= value <= limit + _EPS
            ]
            if candidate_ends:
                end = candidate_ends[-1]
            else:
                end = limit
            adjustments.append("shortened_to_max_duration")

        start = max(0.0, min(start, duration))
        end = max(0.0, min(end, duration))
        clip_duration = end - start
        if clip_duration < options.min_clip_duration - _EPS:
            rejected.append(f"{proposal['title']}: shorter than the minimum after repair")
            continue
        if clip_duration > options.max_clip_duration + _EPS:
            rejected.append(f"{proposal['title']}: longer than the maximum after repair")
            continue

        text = _text_for_range(transcript.segments, start, end)
        if not text:
            rejected.append(f"{proposal['title']}: no transcript text in the selected range")
            continue
        accepted.append(
            ClipCandidate(
                rank=0,
                title=proposal["title"],
                hook=proposal["hook"],
                start_time=round(start, 3),
                end_time=round(end, 3),
                duration=round(clip_duration, 3),
                viral_score=proposal["viral_score"],
                category=proposal["category"],
                reason=proposal["reason"],
                summary=proposal["summary"],
                platforms=proposal["platforms"],
                keywords=proposal["keywords"],
                confidence=proposal["confidence"],
                adjustments=adjustments,
                text=text,
            )
        )

    # Keep the strongest non-duplicated clips.
    accepted.sort(key=lambda clip: (-clip.viral_score, -clip.confidence, clip.start_time))
    selected: List[ClipCandidate] = []
    for clip in accepted:
        duplicate = False
        for existing in selected:
            overlap = min(existing.end_time, clip.end_time) - max(existing.start_time, clip.start_time)
            if overlap <= 0:
                continue
            smaller = min(existing.duration, clip.duration)
            ratio = overlap / smaller if smaller > 0 else 1.0
            if overlap > config.max_overlap_seconds and ratio > config.max_clip_overlap_ratio:
                duplicate = True
                break
            if _similarity(existing.text, clip.text) >= 0.85:
                duplicate = True
                break
        if not duplicate:
            selected.append(clip)
        if len(selected) >= options.number_of_clips:
            break

    if not selected:
        details = " | ".join(rejected[:8]) or "No proposals survived validation."
        raise PipelineError(
            errors.NO_VALID_CLIPS,
            "None of the proposed clips has valid timings.",
            technical_details=details,
        )
    for index, clip in enumerate(selected, start=1):
        clip.rank = index
    selected.sort(key=lambda clip: clip.rank)
    return selected
