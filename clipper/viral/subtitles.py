"""Subtitle formatting, caption-file parsing, and per-clip caption output."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from clipper.viral.models import ClipCandidate, Transcript, TranscriptSegment

_VTT_TIME_RE = re.compile(
    r"(?P<sh>\d{1,2}):(?P<sm>\d{2}):(?P<ss>\d{2})[.,](?P<sms>\d{3})\s*-->\s*"
    r"(?P<eh>\d{1,2}):(?P<em>\d{2}):(?P<es>\d{2})[.,](?P<ems>\d{3})"
)
_SRT_TIME_RE = _VTT_TIME_RE
_TAG_RE = re.compile(r"<[^>]+>")


def format_srt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    total_ms = int(round(seconds * 1000))
    hours, remainder = divmod(total_ms, 3600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def format_vtt_time(seconds: float) -> str:
    return format_srt_time(seconds).replace(",", ".")


def normalize_caption_text(text: str) -> str:
    text = html.unescape(text or "")
    text = _TAG_RE.sub("", text)
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _safe_segments(segments: Sequence[TranscriptSegment]) -> List[TranscriptSegment]:
    cleaned: List[TranscriptSegment] = []
    for segment in sorted(segments, key=lambda s: (s.start, s.end)):
        text = normalize_caption_text(segment.text)
        start = max(0.0, float(segment.start))
        end = max(start, float(segment.end))
        if not text or end - start <= 0.01:
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


def to_srt(segments: Sequence[TranscriptSegment]) -> str:
    blocks = []
    for segment in _safe_segments(segments):
        blocks.append(
            f"{segment.id}\n{format_srt_time(segment.start)} --> {format_srt_time(segment.end)}\n{segment.text}"
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def to_vtt(segments: Sequence[TranscriptSegment]) -> str:
    blocks = ["WEBVTT"]
    for segment in _safe_segments(segments):
        blocks.append(
            f"{segment.id}\n{format_vtt_time(segment.start)} --> {format_vtt_time(segment.end)}\n{segment.text}"
        )
    return "\n\n".join(blocks) + "\n"


def to_plain_text(segments: Sequence[TranscriptSegment]) -> str:
    return " ".join(s.text for s in _safe_segments(segments)).strip() + ("\n" if segments else "")


def segments_in_range(
    segments: Sequence[TranscriptSegment],
    start: float,
    end: float,
    offset_to_zero: bool = True,
) -> List[TranscriptSegment]:
    result = []
    for segment in segments:
        seg_start = max(start, segment.start)
        seg_end = min(end, segment.end)
        if seg_end - seg_start <= 0.01:
            continue
        result.append(
            TranscriptSegment(
                id=len(result) + 1,
                start=seg_start - start if offset_to_zero else seg_start,
                end=seg_end - start if offset_to_zero else seg_end,
                text=segment.text,
                confidence=segment.confidence,
            )
        )
    return result


def transcript_for_range(transcript: Transcript, start: float, end: float) -> Transcript:
    return Transcript(
        language=transcript.language,
        duration=max(0.0, end - start),
        segments=segments_in_range(transcript.segments, start, end, offset_to_zero=True),
        provider=transcript.provider,
    )


def write_transcript_files(transcript: Transcript, directory: Path) -> Dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = {
        "srt": directory / "full.srt",
        "vtt": directory / "full.vtt",
        "txt": directory / "full.txt",
    }
    paths["srt"].write_text(to_srt(transcript.segments), encoding="utf-8")
    paths["vtt"].write_text(to_vtt(transcript.segments), encoding="utf-8")
    paths["txt"].write_text(to_plain_text(transcript.segments), encoding="utf-8")
    return paths


def caption_block(clip: ClipCandidate) -> str:
    lines = [clip.title.strip(), "", clip.hook.strip()]
    if clip.summary.strip():
        lines.extend(["", clip.summary.strip()])
    hashtags = []
    for keyword in clip.keywords:
        tag = re.sub(r"\s+", "", str(keyword)).strip("#")
        if tag:
            hashtags.append("#" + tag)
    if hashtags:
        lines.extend(["", " ".join(hashtags)])
    return "\n".join(line for line in lines if line is not None).strip() + "\n"


def write_clip_subtitles(
    clip: ClipCandidate,
    transcript: Transcript,
    clip_dir: Path,
    base_name: str,
) -> Dict[str, Path]:
    clip_dir.mkdir(parents=True, exist_ok=True)
    clip_transcript = transcript_for_range(transcript, clip.start_time, clip.end_time)
    paths = {
        "srt": clip_dir / f"{base_name}.srt",
        "vtt": clip_dir / f"{base_name}.vtt",
        "txt": clip_dir / f"{base_name}.txt",
        "transcript": clip_dir / f"{base_name}.transcript.txt",
    }
    paths["srt"].write_text(to_srt(clip_transcript.segments), encoding="utf-8")
    paths["vtt"].write_text(to_vtt(clip_transcript.segments), encoding="utf-8")
    paths["txt"].write_text(caption_block(clip), encoding="utf-8")
    paths["transcript"].write_text(to_plain_text(clip_transcript.segments), encoding="utf-8")
    return paths


# ----------------------------------------------------------------------
# Caption parsing
# ----------------------------------------------------------------------

def _seconds(hours: str, minutes: str, seconds: str, millis: str) -> float:
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000.0


def parse_srt_text(text: str) -> List[TranscriptSegment]:
    cues: List[TranscriptSegment] = []
    blocks = re.split(r"\n\s*\n", (text or "").replace("\r\n", "\n").replace("\r", "\n"))
    for block in blocks:
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        time_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if time_index is None:
            continue
        match = _SRT_TIME_RE.search(lines[time_index])
        if not match:
            continue
        start = _seconds(match.group("sh"), match.group("sm"), match.group("ss"), match.group("sms"))
        end = _seconds(match.group("eh"), match.group("em"), match.group("es"), match.group("ems"))
        content = normalize_caption_text(" ".join(lines[time_index + 1:]))
        if content:
            cues.append(TranscriptSegment(id=len(cues) + 1, start=start, end=end, text=content))
    return _dedupe_rolling(cues)


def parse_vtt_text(text: str) -> List[TranscriptSegment]:
    cleaned = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", cleaned)
    cues: List[TranscriptSegment] = []
    for block in blocks:
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines or lines[0].upper().startswith("WEBVTT"):
            continue
        time_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if time_index is None:
            continue
        line = lines[time_index]
        match = _VTT_TIME_RE.search(line)
        if not match:
            continue
        start = _seconds(match.group("sh"), match.group("sm"), match.group("ss"), match.group("sms"))
        end = _seconds(match.group("eh"), match.group("em"), match.group("es"), match.group("ems"))
        content = normalize_caption_text(" ".join(lines[time_index + 1:]))
        if content:
            cues.append(TranscriptSegment(id=len(cues) + 1, start=start, end=end, text=content))
    return _dedupe_rolling(cues)


def parse_json3_text(text: str) -> List[TranscriptSegment]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    events = data.get("events", []) if isinstance(data, dict) else []
    cues: List[TranscriptSegment] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        segs = event.get("segs") or []
        content = normalize_caption_text("".join(str(s.get("utf8", "")) for s in segs if isinstance(s, dict)))
        if not content:
            continue
        start = float(event.get("tStartMs", 0)) / 1000.0
        duration = float(event.get("dDurationMs", 0)) / 1000.0
        if duration <= 0:
            duration = 2.0
        cues.append(
            TranscriptSegment(
                id=len(cues) + 1,
                start=start,
                end=start + duration,
                text=content,
            )
        )
    return _dedupe_rolling(cues)


def _dedupe_rolling(segments: Sequence[TranscriptSegment]) -> List[TranscriptSegment]:
    """Collapse YouTube-style rolling captions into stable segments."""
    result: List[TranscriptSegment] = []
    for segment in sorted(segments, key=lambda s: (s.start, s.end)):
        if not result:
            result.append(segment)
            continue
        previous = result[-1]
        close = segment.start <= previous.end + 0.75
        if close and segment.text == previous.text:
            previous.end = max(previous.end, segment.end)
            continue
        if close and segment.text.startswith(previous.text) and segment.start <= previous.end + 0.75:
            previous.text = segment.text
            previous.end = max(previous.end, segment.end)
            continue
        if close and previous.text.startswith(segment.text):
            previous.end = max(previous.end, segment.end)
            continue
        result.append(segment)
    for index, segment in enumerate(result, start=1):
        segment.id = index
    return result


def parse_caption_file(path: Path) -> List[TranscriptSegment]:
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if suffix == ".json3":
        return parse_json3_text(text)
    if suffix == ".vtt":
        return parse_vtt_text(text)
    if suffix == ".srt":
        return parse_srt_text(text)
    return []


def caption_language_from_path(path: Path) -> str:
    parts = path.name.split(".")
    if len(parts) >= 3 and re.fullmatch(r"[a-zA-Z]{2,3}(?:-[a-zA-Z0-9]{2,8})?", parts[-2]):
        return parts[-2].split("-")[0].lower()
    return "unknown"
