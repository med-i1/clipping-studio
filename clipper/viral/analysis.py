"""Transcript analysis with a strict JSON contract and pluggable providers."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, Sequence, Tuple

from clipper.viral import errors
from clipper.viral.config import ViralConfig
from clipper.viral.errors import PipelineError, redact
from clipper.viral.models import (
    AnalysisResult,
    JobOptions,
    SUPPORTED_PLATFORMS,
    Transcript,
    TranscriptSegment,
)
from clipper.viral.net import HttpRequestError, post_json

_SYSTEM_PROMPT = (
    "You are a meticulous viral short-form video analyst. "
    "You must answer with one valid JSON object only."
)


class AnalysisJSONError(Exception):
    """The provider answered, but not with a usable schema."""


def _extract_json(text: str) -> Dict[str, Any]:
    value = (text or "").strip().lstrip("\ufeff")
    fenced = re.search(r"```(?:json)?\s*(.*?)```", value, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        value = fenced.group(1).strip()
    else:
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", "", value)
    start = value.find("{")
    end = value.rfind("}")
    if start < 0 or end <= start:
        raise AnalysisJSONError("The response did not contain a JSON object.")
    value = value[start:end + 1]
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        pass
    # Safe, bounded repairs for common model formatting mistakes.
    repaired = re.sub(r",\s*([}\]])", r"\1", value)
    repaired = repaired.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    try:
        parsed = json.loads(repaired)
    except json.JSONDecodeError as exc:
        raise AnalysisJSONError(f"Invalid JSON: {exc.msg} at line {exc.lineno}, column {exc.colno}.") from exc
    if not isinstance(parsed, dict):
        raise AnalysisJSONError("The JSON root must be an object.")
    return parsed


def _as_float(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise AnalysisJSONError(f"{name} must be a number.")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise AnalysisJSONError(f"{name} must be a number.") from exc


def _as_int(value: Any, name: str, default: Optional[int] = None) -> int:
    if isinstance(value, bool):
        raise AnalysisJSONError(f"{name} must be an integer.")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        if default is not None:
            return default
        raise AnalysisJSONError(f"{name} must be an integer.") from exc


def _as_text(value: Any, name: str, default: str = "", max_len: int = 1000) -> str:
    if value is None:
        return default
    if not isinstance(value, (str, int, float)):
        raise AnalysisJSONError(f"{name} must be text.")
    return str(value).strip()[:max_len]


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def normalize_clip_payload(item: Dict[str, Any], index: int) -> Dict[str, Any]:
    if not isinstance(item, dict):
        raise AnalysisJSONError("Each clip must be a JSON object.")
    start_raw = item.get("start_time", item.get("start", item.get("startTime")))
    end_raw = item.get("end_time", item.get("end", item.get("endTime")))
    if start_raw is None or end_raw is None:
        raise AnalysisJSONError("Every clip needs start_time and end_time.")
    start = _as_float(start_raw, "start_time")
    end = _as_float(end_raw, "end_time")
    if end <= start:
        raise AnalysisJSONError("end_time must be greater than start_time.")
    title = _as_text(item.get("title"), "title", max_len=200)
    if not title:
        raise AnalysisJSONError("Every clip needs a title.")
    score_raw = item.get("viral_score", item.get("score", 50))
    score = _as_int(score_raw, "viral_score", default=50)
    score = max(0, min(100, score))
    platforms = []
    for platform in _as_list(item.get("platforms", list(SUPPORTED_PLATFORMS))):
        value = str(platform).strip().lower()
        if value in SUPPORTED_PLATFORMS and value not in platforms:
            platforms.append(value)
    if not platforms:
        platforms = list(SUPPORTED_PLATFORMS)
    keywords = []
    for keyword in _as_list(item.get("keywords")):
        word = str(keyword).strip().lstrip("#")[:80]
        if word and word not in keywords:
            keywords.append(word)
    confidence_raw = item.get("confidence", 0.5)
    try:
        confidence = max(0.0, min(1.0, float(confidence_raw)))
    except (TypeError, ValueError):
        confidence = 0.5
    return {
        "rank": _as_int(item.get("rank", index + 1), "rank", default=index + 1),
        "title": title,
        "hook": _as_text(item.get("hook"), "hook", default=title, max_len=500),
        "start_time": start,
        "end_time": end,
        "duration": round(end - start, 3),
        "viral_score": score,
        "category": _as_text(item.get("category"), "category", default="general", max_len=80),
        "reason": _as_text(item.get("reason"), "reason", default="", max_len=1000),
        "summary": _as_text(item.get("summary"), "summary", default="", max_len=1000),
        "platforms": platforms,
        "keywords": keywords[:20],
        "confidence": round(confidence, 3),
    }


def validate_analysis_payload(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not isinstance(data, dict):
        raise AnalysisJSONError("The JSON root must be an object.")
    clips = data.get("clips")
    if not isinstance(clips, list):
        raise AnalysisJSONError("The response must contain a 'clips' array.")
    if not clips:
        raise AnalysisJSONError("The 'clips' array is empty.")
    normalized = []
    for index, item in enumerate(clips):
        normalized.append(normalize_clip_payload(item, index))
    return normalized


def format_clock(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:06.3f}"


def transcript_lines(segments: Sequence[TranscriptSegment]) -> str:
    return "\n".join(
        f"[{segment.id}] {format_clock(segment.start)} - {format_clock(segment.end)} | {segment.text}"
        for segment in segments
    )


def chunk_transcript(
    segments: Sequence[TranscriptSegment],
    max_chars: int,
    max_chunks: int,
) -> List[List[TranscriptSegment]]:
    if not segments:
        return []
    total = sum(len(s.text) + 40 for s in segments)
    if total <= max_chars:
        return [list(segments)]
    target = max(max_chars, math.ceil(total / max(1, max_chunks)))
    chunks: List[List[TranscriptSegment]] = []
    current: List[TranscriptSegment] = []
    size = 0
    for segment in segments:
        segment_size = len(segment.text) + 40
        if current and size + segment_size > target:
            chunks.append(current)
            current = current[-1:]  # one segment of overlap for context
            size = sum(len(s.text) + 40 for s in current)
        current.append(segment)
        size += segment_size
    if current:
        chunks.append(current)
    return chunks


class TranscriptAnalyzer(Protocol):
    name: str

    def analyze(self, transcript: Transcript, options: JobOptions) -> AnalysisResult:
        ...


class OpenAICompatibleAnalyzer:
    """Chat-completions analyzer for OpenAI-compatible APIs."""

    name = "openai_compatible"

    def __init__(
        self,
        config: ViralConfig,
        logger: Any = None,
        transport: Optional[Callable[..., Dict[str, Any]]] = None,
        prompt_path: Optional[Path] = None,
    ) -> None:
        self.config = config
        self.logger = logger
        self.transport = transport or post_json
        self.prompt_path = Path(prompt_path or config.prompt_file)

    def analyze(self, transcript: Transcript, options: JobOptions) -> AnalysisResult:
        if not self.config.llm_api_key:
            raise PipelineError(
                errors.CONFIGURATION_ERROR,
                "No analysis API key is configured.",
                technical_details="Set LLM_API_KEY in .env.",
            )
        template = self._read_prompt()
        chunks = chunk_transcript(
            transcript.segments,
            self.config.llm_max_transcript_chars,
            self.config.llm_max_chunks,
        )
        if not chunks:
            raise PipelineError(errors.ANALYSIS_FAILED, "The transcript is empty.")
        proposals: List[Dict[str, Any]] = []
        raw_parts: List[str] = []
        attempts = 0
        for index, chunk in enumerate(chunks, start=1):
            label = (
                f"This is part {index} of {len(chunks)} of the transcript. "
                "Analyze only this part, using context from the transcript as a whole."
                if len(chunks) > 1 else ""
            )
            prompt = self._render_prompt(template, chunk, transcript, options, label)
            clips, raw, used = self._analyze_chunk(prompt)
            attempts += used
            proposals.extend(clips)
            raw_parts.append(raw)
        merged = self._merge_proposals(proposals, options.number_of_clips)
        if not merged:
            raise PipelineError(
                errors.ANALYSIS_FAILED,
                "The analysis model did not return any usable clips.",
                retryable=True,
            )
        return AnalysisResult(
            clips=merged,
            provider=f"openai_compatible:{self.config.llm_base_url}",
            model=self.config.llm_model,
            attempts=attempts,
            raw_response="\n---\n".join(raw_parts)[:1_000_000],
        )

    def _read_prompt(self) -> str:
        try:
            return self.prompt_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PipelineError(
                errors.CONFIGURATION_ERROR,
                "The analysis prompt file is missing.",
                technical_details=f"{self.prompt_path}: {exc}",
            ) from exc

    def _render_prompt(
        self,
        template: str,
        segments: Sequence[TranscriptSegment],
        transcript: Transcript,
        options: JobOptions,
        chunk_label: str,
    ) -> str:
        replacements = {
            "{{VIDEO_DURATION}}": f"{transcript.duration:.2f}",
            "{{NUMBER_OF_CLIPS}}": str(options.number_of_clips),
            "{{MIN_DURATION}}": f"{options.min_clip_duration:.1f}",
            "{{MAX_DURATION}}": f"{options.max_clip_duration:.1f}",
            "{{PLATFORMS}}": ", ".join(options.platforms),
            "{{LANGUAGE}}": options.language,
            "{{CHUNK_LABEL}}": chunk_label,
            "{{TRANSCRIPT}}": transcript_lines(segments),
        }
        rendered = template
        for key, value in replacements.items():
            rendered = rendered.replace(key, value)
        return rendered

    def _analyze_chunk(self, prompt: str) -> Tuple[List[Dict[str, Any]], str, int]:
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        payload: Dict[str, Any] = {
            "model": self.config.llm_model,
            "messages": messages,
            "temperature": self.config.llm_temperature,
        }
        if self.config.llm_json_mode:
            payload["response_format"] = {"type": "json_object"}
        url = f"{self.config.llm_base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.config.llm_api_key}",
            "Content-Type": "application/json",
        }
        raw_parts: List[str] = []
        last_error = ""
        attempts_allowed = max(0, self.config.llm_max_retries) + 1
        for attempt in range(1, attempts_allowed + 1):
            if self.logger:
                self.logger.event("analysis_attempt", attempt=attempt, total=attempts_allowed)
            try:
                response = self.transport(url, payload, headers, self.config.llm_timeout_seconds)
                content = self._response_content(response)
                raw_parts.append(content)
                try:
                    data = _extract_json(content)
                    clips = validate_analysis_payload(data)
                except AnalysisJSONError as exc:
                    last_error = str(exc)
                    if self.logger:
                        self.logger.event("analysis_json_invalid", attempt=attempt, detail=str(exc))
                    messages = messages[:2] + [
                        {"role": "assistant", "content": content[:8000]},
                        {
                            "role": "user",
                            "content": (
                                f"Your answer was rejected: {exc}. "
                                "Return ONLY the corrected JSON object matching the schema. "
                                "No Markdown and no explanation."
                            ),
                        },
                    ]
                    payload = dict(payload)
                    payload["messages"] = messages
                    continue
                return clips, "\n".join(raw_parts), attempt
            except HttpRequestError as exc:
                last_error = f"HTTP {exc.status}: {exc.body[:500]}"
                if exc.status == 400 and "response_format" in payload:
                    payload = {k: v for k, v in payload.items() if k != "response_format"}
                    self.config.llm_json_mode = False
                    continue
                if exc.status in (401, 403):
                    raise PipelineError(
                        errors.CONFIGURATION_ERROR,
                        "The analysis provider rejected the API key.",
                        technical_details=f"HTTP {exc.status}: {exc.body[:500]}",
                    ) from exc
                if exc.status in (408, 409, 429) or exc.status >= 500:
                    continue
                break
            except PipelineError as exc:
                last_error = str(exc)
                if not exc.retryable:
                    raise
                continue
            except Exception as exc:
                last_error = str(exc)
                continue
        raise PipelineError(
            errors.ANALYSIS_INVALID_JSON,
            "The analysis model did not return valid JSON after retries.",
            technical_details=redact(last_error),
            retryable=True,
        )

    def _response_content(self, response: Dict[str, Any]) -> str:
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise AnalysisJSONError("The provider response has no choices.")
        message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        if not isinstance(content, str) or not content.strip():
            raise AnalysisJSONError("The provider returned an empty answer.")
        return content.strip()

    def _merge_proposals(self, proposals: Sequence[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
        ordered = sorted(proposals, key=lambda c: (-int(c.get("viral_score", 0)), float(c.get("start_time", 0))))
        merged: List[Dict[str, Any]] = []
        for proposal in ordered:
            duplicate = False
            for existing in merged:
                overlap = min(proposal["end_time"], existing["end_time"]) - max(proposal["start_time"], existing["start_time"])
                if overlap > 2.0:
                    duplicate = True
                    break
                if proposal["title"].strip().casefold() == existing["title"].strip().casefold():
                    duplicate = True
                    break
            if not duplicate:
                merged.append(proposal)
        cap = max(1, limit * 3)
        return merged[:cap]


# ----------------------------------------------------------------------
# Offline heuristic analyzer
# ----------------------------------------------------------------------

_HOOK_WORDS = (
    "why", "how", "what", "never", "everyone", "nobody", "truth", "secret",
    "mistake", "wrong", "best", "worst", "stop", "imagine", "listen",
    "لماذا", "كيف", "ماذا", "أفضل", "أسوأ", "خطأ", "الحقيقة", "السر",
    "لا أحد", "كل الناس", "توقف", "تخيل", "اسمع", "أهم", "مشكلة",
)
_CONTROVERSY_WORDS = (
    "but", "however", "actually", "wrong", "lie", "controversial", "debate",
    "لكن", "الحقيقة", "خطأ", "خلاف", "جدل", "كذب", "بصراحة", "رأيي",
)
_EMOTION_WORDS = (
    "love", "fear", "pain", "lost", "cry", "afraid", "alone", "family",
    "حزن", "خوف", "ألم", "فقدت", "بكاء", "وحيد", "عائلة", "حب",
)
_EDUCATION_WORDS = (
    "because", "learn", "step", "method", "example", "means", "research",
    "لأن", "تعلم", "خطوة", "طريقة", "مثال", "يعني", "بحث", "درس",
)
_STOPWORDS = {
    "the", "and", "that", "this", "with", "from", "have", "your", "you",
    "are", "was", "were", "for", "not", "but", "they", "will", "can",
    "all", "about", "what", "when", "who", "how", "why", "into", "than",
    "في", "من", "على", "إلى", "عن", "أن", "هذا", "هذه", "التي", "الذي",
    "كان", "كانت", "مع", "لا", "ما", "هو", "هي", "ثم", "قد", "كل",
}


class HeuristicAnalyzer:
    """Deterministic offline fallback used when no LLM key is configured."""

    name = "heuristic"
    model = "heuristic-v1"

    def __init__(self, config: ViralConfig, logger: Any = None) -> None:
        self.config = config
        self.logger = logger

    def analyze(self, transcript: Transcript, options: JobOptions) -> AnalysisResult:
        segments = transcript.segments
        if not segments:
            raise PipelineError(errors.ANALYSIS_FAILED, "The transcript is empty.")
        windows: List[Tuple[float, int, int]] = []
        count = len(segments)
        for start_index in range(count):
            start = segments[start_index].start
            for end_index in range(start_index, count):
                duration = segments[end_index].end - start
                if duration < options.min_clip_duration:
                    continue
                if duration > options.max_clip_duration:
                    break
                windows.append((duration, start_index, end_index))
        if not windows:
            start = segments[0].start
            end = min(transcript.duration, segments[-1].end)
            if end - start < 5:
                raise PipelineError(errors.NO_VALID_CLIPS, "No usable clip window exists in this transcript.")
            windows = [(end - start, 0, len(segments) - 1)]

        scored = []
        for duration, start_index, end_index in windows:
            chunk = segments[start_index:end_index + 1]
            score, category, reason = self._score(chunk, duration)
            hook = chunk[0].text.strip()
            title = self._title(hook)
            summary = " ".join(s.text for s in chunk[:2]).strip()
            scored.append(
                {
                    "rank": 0,
                    "title": title,
                    "hook": hook[:300],
                    "start_time": round(chunk[0].start, 3),
                    "end_time": round(chunk[-1].end, 3),
                    "duration": round(chunk[-1].end - chunk[0].start, 3),
                    "viral_score": score,
                    "category": category,
                    "reason": reason,
                    "summary": summary[:500],
                    "platforms": list(options.platforms),
                    "keywords": self._keywords(" ".join(s.text for s in chunk)),
                    "confidence": 0.55,
                }
            )
        scored.sort(key=lambda item: (-item["viral_score"], item["start_time"]))
        selected: List[Dict[str, Any]] = []
        for candidate in scored:
            if len(selected) >= options.number_of_clips:
                break
            if any(self._overlap(candidate, chosen) > self.config.max_overlap_seconds for chosen in selected):
                continue
            selected.append(candidate)
        for index, candidate in enumerate(selected, start=1):
            candidate["rank"] = index
        if not selected:
            raise PipelineError(errors.NO_VALID_CLIPS, "No distinct clip candidates were found.")
        return AnalysisResult(
            clips=selected,
            provider="heuristic",
            model=self.model,
            attempts=1,
            raw_response=json.dumps({"clips": selected}, ensure_ascii=False),
        )

    def _score(self, chunk: Sequence[TranscriptSegment], duration: float) -> Tuple[int, str, str]:
        text = " ".join(s.text for s in chunk)
        lower = text.lower()
        first = chunk[0].text.lower()
        score = 28.0
        reasons = []
        if "?" in chunk[0].text or "؟" in chunk[0].text:
            score += 14
            reasons.append("strong question hook")
        if any(word in first for word in _HOOK_WORDS):
            score += 10
            reasons.append("curiosity-driven opening")
        if re.search(r"\d", text):
            score += 5
            reasons.append("concrete numbers")
        controversy = sum(1 for word in _CONTROVERSY_WORDS if word in lower)
        if controversy:
            score += min(12, controversy * 4)
            reasons.append("contrarian tension")
        emotion = sum(1 for word in _EMOTION_WORDS if word in lower)
        if emotion:
            score += min(10, emotion * 3)
            reasons.append("emotional stake")
        education = sum(1 for word in _EDUCATION_WORDS if word in lower)
        if education:
            score += min(6, education * 2)
            reasons.append("useful insight")
        if re.search(r"[.!?؟]$", chunk[-1].text.strip()):
            score += 5
            reasons.append("complete ending")
        score += max(0.0, 10 - abs(duration - 50) / 5)
        words = len(re.findall(r"\S+", text))
        score += min(6.0, (words / max(1.0, duration)) * 12)
        category = "general"
        if controversy >= 2:
            category = "controversial_opinion"
        elif emotion >= 2:
            category = "emotional_story"
        elif education >= 2:
            category = "educational"
        elif "?" in chunk[0].text or "؟" in chunk[0].text:
            category = "curiosity_hook"
        viral = int(max(35, min(88, round(45 + score * 0.85))))
        reason = "Selected for " + ", ".join(reasons[:3]) if reasons else "Selected for a self-contained, high-signal moment."
        return viral, category, reason

    def _title(self, hook: str) -> str:
        text = re.sub(r"\s+", " ", hook).strip().strip('"').strip("'")
        words = text.split()
        title = " ".join(words[:9])
        if len(words) > 9:
            title += "..."
        return title[:120] or "Untitled moment"

    def _keywords(self, text: str) -> List[str]:
        tokens = re.findall(r"[\w\u0600-\u06FF]{3,}", text.lower())
        counts = Counter(token for token in tokens if token not in _STOPWORDS)
        return [word for word, _ in counts.most_common(8)]

    def _overlap(self, a: Dict[str, Any], b: Dict[str, Any]) -> float:
        return min(a["end_time"], b["end_time"]) - max(a["start_time"], b["start_time"])


def build_analyzer(config: ViralConfig, logger: Any = None) -> TranscriptAnalyzer:
    provider = (config.llm_provider or "auto").lower()
    if provider == "auto":
        if config.llm_api_key:
            return OpenAICompatibleAnalyzer(config, logger)
        if logger:
            logger.event(
                "analysis_provider",
                level="WARNING",
                message="LLM_API_KEY is not set; using the offline heuristic analyzer.",
            )
        return HeuristicAnalyzer(config, logger)
    if provider in ("openai", "openai_compatible", "chat_completions", "google", "gemini", "google_gemini"):
        if not config.llm_api_key:
            raise PipelineError(
                errors.CONFIGURATION_ERROR,
                "LLM_PROVIDER requires LLM_API_KEY.",
            )
        return OpenAICompatibleAnalyzer(config, logger)
    if provider == "heuristic":
        return HeuristicAnalyzer(config, logger)
    raise PipelineError(
        errors.CONFIGURATION_ERROR,
        f"Unknown LLM_PROVIDER '{config.llm_provider}'.",
        technical_details="Use auto, openai_compatible, gemini, or heuristic.",
    )
