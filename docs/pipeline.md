# Viral clip pipeline

This document describes the new workflow in `clipper/viral/`. It is additive:
the existing timed-clip engine and GUI behavior are unchanged.

---

## 1. End-to-end stages

```text
URL
 │
 ├─ validate_url / validate_job_options
 │
 ├─ 1. Download      ExistingYtDlpDownloader
 │                   ├─ metadata via yt-dlp -J
 │                   ├─ limits: duration, file size, disk space
 │                   ├─ cache by SHA-256(url + format)
 │                   ├─ retries around clipper.downloader.download_full_video
 │                   └─ thumbnail + source_info.json
 │
 ├─ 2. Transcript    FallbackTranscriber
 │                   ├─ native manual captions
 │                   ├─ native automatic captions
 │                   ├─ OpenAI-compatible Whisper API
 │                   └─ local faster-whisper (if installed)
 │
 ├─ 3. Analysis      LLM (OpenAI-compatible) or offline heuristic
 │                   ├─ composite prompt file
 │                   ├─ transcript chunking
 │                   ├─ strict JSON validation + bounded repair retries
 │                   └─ merge/deduplicate proposals
 │
 ├─ 4. Validation    transcript-aware timing repair
 │                   ├─ clamp to [0, duration]
 │                   ├─ snap to sentence boundaries
 │                   ├─ enforce min/max duration
 │                   ├─ reject large overlaps/duplicates
 │                   └─ rank and keep requested clip count
 │
 ├─ 5. Extraction    FFmpeg
 │                   ├─ stream copy first in auto mode
 │                   ├─ re-encode with libx264/AAC only if needed
 │                   ├─ verify video stream and duration
 │                   └─ per-clip SRT/VTT/caption/transcript files
 │
 └─ 6. Packaging     result.json + report.md + package.zip
```

Every job runs in its own folder. No stage mutates a previous job.

---

## 2. Modules

| Module | Responsibility |
|---|---|
| `validation.py` | URL/SSRF checks, job IDs, safe filenames, traversal guard, option validation, disk-space guard. |
| `models.py` | Dataclasses for options, downloaded videos, transcripts, clips, job records, pipeline results. |
| `config.py` | `.env` + environment loading, defaults, secret-free public config. |
| `errors.py` | Stable error codes and secret redaction. |
| `logging_utils.py` | Per-job JSONL logs and stage timing. |
| `net.py` | Standard-library JSON POST and multipart upload helpers. |
| `media.py` | ffprobe/ffmpeg wrappers, audio extraction/chunking, tool checks. |
| `download_adapter.py` | Cache, metadata, retries, typed download errors; calls the legacy downloader. |
| `transcription.py` | Caption parsers, cleaning, Whisper API/client, local Whisper, fallback chain. |
| `analysis.py` | Prompt rendering, chunking, JSON extraction/repair, LLM client, heuristic fallback. |
| `timing.py` | Transcript-aware timing validation and clip selection. |
| `subtitles.py` | SRT/VTT/JSON3 parsing, time formatting, per-clip caption output. |
| `extraction.py` | Stream-copy/re-encode extraction and output validation. |
| `packaging.py` | Job folders, relative paths, `result.json`, Markdown report, ZIP. |
| `pipeline.py` | Stage orchestration, progress callbacks, transcript file output. |
| `jobs.py` | Disk-backed job records, in-process executor, retry, detached worker helper. |
| `worker.py` | Runs one queued job in a separate process for CLI `--no-wait`. |
| `cli.py` / `gui.py` / `server.py` | Optional front ends. |

The local web UI lives in `clipper/viral/web/` (`index.html`, `styles.css`,
`app.js`) and is served by `server.py` as fixed static routes. It uses the same
API, keeps the token in session storage, and adds a job-log endpoint
(`/api/jobs/{id}/log`) plus an inline report view
(`/api/jobs/{id}/report?inline=1`). The assets are bundled under `web/` in
PyInstaller builds.

---

## 3. Download adapter

The legacy function remains the only downloader:

```python
from clipper.downloader import download_full_video
```

`ExistingYtDlpDownloader.download()` adds:

1. **Cache key:** `sha256(url + "|" + format)`, stored in
   `VIRAL_CACHE_DIR/index.json`.
2. **Cache hit:** hard-link or copy the cached file into the new job folder.
   Cache metadata is reused, including the resolved duration.
3. **Metadata:** `yt-dlp -J --no-playlist` before downloading; rejects
   playlists, live streams, private/members-only videos, and videos over
   `MAX_VIDEO_DURATION_SECONDS`.
4. **Limits:** estimated file size × 2 and `MIN_FREE_SPACE_MB` before the
   download; actual `MAX_FILE_SIZE_MB` after it.
5. **Retries:** `MAX_RETRIES` attempts with exponential backoff, cleaning
   partial `.part`/`.ytdl` files between attempts.
6. **Typed failures:** private/auth, unsupported URL, unavailable video, or
   generic download failure.
7. **Artifacts:** `metadata/source_info.json`, optional thumbnail, duration
   probed with ffprobe.

The legacy command still uses its own hard-coded `yt-dlp`; metadata, captions,
and thumbnails use `YTDLP_BIN`. This is a documented compatibility constraint.

`download_original=false` still downloads the source for processing, then
deletes it before packaging and excludes it from the ZIP.

---

## 4. Transcription

Provider selection:

| `TRANSCRIPTION_PROVIDER` | Behavior |
|---|---|
| `auto` (default) | Native captions → Whisper API if a key exists → local faster-whisper if installed. |
| `captions` | Native captions only. |
| `openai` | Whisper-compatible API; requires a key. |
| `local` | `faster-whisper`; requires the package. |
| `none` | Disabled; raises `TRANSCRIPTION_UNAVAILABLE`. |

Native captions are selected using the requested language, the video language,
then Arabic/English, then other available tracks. Supported formats are SRT,
VTT, and YouTube JSON3. Rolling duplicate cues are collapsed.

When speech-to-text is needed, audio is extracted to mono 16 kHz MP3 and split
into `STT_CHUNK_SECONDS` chunks. Segment timestamps are offset back onto the
full timeline. The API key falls back to `LLM_API_KEY` when
`TRANSCRIPTION_API_KEY` is empty.

Outputs:

- `transcript/raw.json` — provider segments untouched plus metadata;
- `transcript/clean.json` — normalized segments;
- `transcript/full.srt`, `full.vtt`, `full.txt`.

---

## 5. LLM analysis

The analyzer is OpenAI-compatible chat completions:

```text
POST {LLM_BASE_URL}/chat/completions
```

Required variables: `LLM_API_KEY`, `LLM_MODEL`, optionally `LLM_BASE_URL`.
`LLM_PROVIDER=auto` uses the heuristic analyzer when no key is present.
`LLM_PROVIDER=gemini` (aliases `google`, `google_gemini`) targets
`https://generativelanguage.googleapis.com/v1beta/openai` with
`gemini-3.8-flash` unless a custom base URL or model is set.

The prompt is separate and editable:
`prompts/viral_clip_analysis.txt`. Placeholders:

- `{{VIDEO_DURATION}}`
- `{{NUMBER_OF_CLIPS}}`
- `{{MIN_DURATION}}`
- `{{MAX_DURATION}}`
- `{{PLATFORMS}}`
- `{{LANGUAGE}}`
- `{{CHUNK_LABEL}}`
- `{{TRANSCRIPT}}`

Long transcripts are chunked by `LLM_MAX_TRANSCRIPT_CHARS`, capped at
`LLM_MAX_CHUNKS`. Each chunk asks for JSON only. Valid JSON is normalized to the
internal schema; invalid JSON gets a bounded correction attempt that echoes the
bad answer and asks for corrected JSON only.

The merge step sorts by score, removes duplicates by overlap/title, and keeps up
to `number_of_clips × 3` proposals for timing validation.

Provider/model/attempts/raw proposals are saved to
`metadata/analysis_raw.json`; no API key is written.

---

## 6. Timing validation

The model's timestamps are treated as proposals, never as facts.

For each proposal:

1. Clamp start/end to the video duration.
2. Snap the start to the containing/nearest sentence boundary within
   `SNAP_TOLERANCE_SECONDS`; a start inside a sentence moves to that sentence's
   start.
3. Snap the end to the sentence end; an end mid-sentence moves forward to the
   sentence end.
4. Extend to `min_clip_duration` using the nearest sentence boundary when
   possible.
5. Shorten to `max_clip_duration` without cutting mid-sentence when possible.
6. Reject if the repaired duration is still outside the configured range or if
   no transcript text falls inside the window.
7. Reject duplicates/large overlaps:
   - overlap > `MAX_OVERLAP_SECONDS`, and
   - overlap ratio > `MAX_CLIP_OVERLAP_RATIO`, or
   - text similarity ≥ 0.85.
8. Sort by score/confidence and keep `number_of_clips`, renumbering ranks 1..N.

Every repair is recorded in `clip.adjustments` and shown in the report.

---

## 7. Extraction

`FFmpegClipExtractor` uses:

```text
ffmpeg -ss START -i INPUT -t DURATION -map 0:v:0 -map 0:a? ...
```

- `CLIP_COPY_MODE=auto` (default): stream copy only when the source is H.264
  video with AAC (or no) audio; otherwise re-encode. A copied clip is still
  checked for a video stream and a duration within tolerance, and re-encoded
  if it fails.
- `copy`: always stream copy; fail instead of re-encoding.
- `reencode`: skip the copy attempt.

Re-encoding uses `libx264`, `yuv420p`, `CLIP_CRF`, `CLIP_PRESET`, AAC 192k, and
VFR handling (`-fps_mode vfr`, with a legacy `-vsync vfr` fallback for older
FFmpeg builds). Output verification requires a video stream and a duration
within `max(1s, min(4s, expected × 8%))`.

Filenames follow:

```text
clip_001_rank_1_viral_score_92.mp4
```

Per-clip text artifacts:

- `.srt`, `.vtt` — captions shifted to clip-relative time;
- `.txt` — title/hook/summary/hashtags for posting;
- `.transcript.txt` — plain transcript for the clip window.

---

## 8. Job state machine

```text
queued → downloading → transcribing → analyzing → validating
       → extracting_clips → packaging → completed
                                      ↘ failed
```

`failed` jobs can be retried; `completed` jobs cannot. Active states are stored
in `metadata/job.json`; the list/status API reads the same file. A job record
also stores progress, current step, attempts, error code/message, and artifact
paths.

`JobManager` uses a thread pool (`JOB_WORKERS`) for the GUI/API. CLI
`--no-wait` creates the record without submitting it, then starts
`python -m clipper.viral.worker JOB_ID` as a detached process. The worker calls
`execute_job()` and exits when the job is terminal.

Important: the pipeline emits `progress(100, "completed")` before returning.
The job manager deliberately keeps the status active at that moment and marks
it completed only after result paths are attached, so a waiter never sees a
completed record without artifacts.

---

## 9. Result format

`result.json` (schema version 1):

```json
{
  "schema_version": 1,
  "job_id": "32 hex chars",
  "source_url": "https://...",
  "created_at": "2026-01-01T00:00:00+00:00",
  "status": "completed",
  "options": { "...": "validated JobOptions" },
  "video": {
    "title": "...",
    "duration": 1234.5,
    "original_file": "original/video.mp4",
    "original_retained": true,
    "quality": "1080p",
    "filesize": 12345678,
    "thumbnail": "metadata/thumbnail.jpg",
    "uploader": "...",
    "from_cache": false
  },
  "transcript": {
    "language": "en",
    "provider": "youtube_captions:auto:en",
    "segments": 321,
    "raw_file": "transcript/raw.json",
    "clean_file": "transcript/clean.json",
    "srt_file": "transcript/full.srt",
    "vtt_file": "transcript/full.vtt",
    "txt_file": "transcript/full.txt"
  },
  "analysis": { "provider": "...", "model": "...", "attempts": 1 },
  "clips": [
    {
      "rank": 1,
      "file": "clips/clip_001_rank_1_viral_score_92.mp4",
      "srt_file": "clips/clip_001_rank_1_viral_score_92.srt",
      "vtt_file": "clips/clip_001_rank_1_viral_score_92.vtt",
      "txt_file": "clips/clip_001_rank_1_viral_score_92.txt",
      "start_time": 10.0,
      "end_time": 40.0,
      "duration": 30.0,
      "viral_score": 92,
      "title": "...",
      "hook": "...",
      "reason": "...",
      "summary": "...",
      "category": "insight",
      "platforms": ["tiktok"],
      "keywords": ["..."],
      "confidence": 0.9,
      "adjustments": ["snapped_start_to_sentence"]
    }
  ]
}
```

All paths are relative to the job folder. `report.md` repeats the key fields in
human-readable Markdown. `package.zip` contains the result, report, clips,
transcript, metadata, logs, and optionally the original — never `temp/` and
never a nested ZIP.

---

## 10. Error codes

| Code | Typical cause | Retryable |
|---|---|---|
| `INVALID_URL` | Empty/malformed URL, credentials, control characters. | No |
| `UNSUPPORTED_URL` | Non-http scheme, localhost/private IP, playlist/channel. | No |
| `INVALID_OPTIONS` | Bad clip count/duration/platforms/body. | No |
| `UNSAFE_PATH` | Path traversal attempt. | No |
| `CONFIGURATION_ERROR` | Missing key/tool/prompt. | No |
| `DOWNLOAD_FAILED` | Generic yt-dlp/network failure. | Yes |
| `NETWORK_ERROR` | An LLM/STT/provider endpoint could not be reached. | Yes |
| `VIDEO_PRIVATE_OR_AUTH_REQUIRED` | Private, age-restricted, members-only. | No |
| `VIDEO_UNAVAILABLE` | Removed/unavailable/live stream. | No |
| `VIDEO_TOO_LONG` | Exceeds `MAX_VIDEO_DURATION_SECONDS`. | No |
| `FILE_TOO_LARGE` | Exceeds `MAX_FILE_SIZE_MB`. | No |
| `INSUFFICIENT_STORAGE` | Disk guard. | No |
| `TRANSCRIPTION_FAILED` / `TRANSCRIPTION_UNAVAILABLE` | No captions/provider or STT failure. | Usually |
| `ANALYSIS_FAILED` / `ANALYSIS_INVALID_JSON` | No usable proposals or invalid JSON after retries. | Usually |
| `NO_VALID_CLIPS` | All proposals failed timing validation. | No |
| `FFMPEG_NOT_FOUND` | Missing binary. | No |
| `CLIP_EXTRACTION_FAILED` | FFmpeg output invalid. | Usually |
| `PACKAGING_FAILED` | ZIP write failure. | Yes |
| `JOB_NOT_FOUND` / `JOB_NOT_RETRYABLE` / `JOB_NOT_DELETABLE` | Bad job ID, invalid transition, or deletion of an active job. | No |
| `JOB_INTERRUPTED` | The process running the job exited before it finished; set on the next app start. | Yes |
| `TIMEOUT` | Metadata/download/wait timeout. | Yes |
| `INTERNAL_ERROR` | Unexpected exception; details are captured in the job log. | Yes |

Public error payloads omit `technical_details`; logs retain redacted details.

---

## 11. Security

- **SSRF gate:** only public `http`/`https`; rejects localhost, `.local`,
  single-label hosts, private/loopback/link-local/reserved/multicast IP
  literals, embedded credentials, control characters, and overlong URLs.
- **Path safety:** job IDs are 32 lowercase hex; all generated paths are
  resolved and checked against the job root; ZIP entries use relative paths and
  skip escapes.
- **Filename safety:** Unicode-preserving sanitization (including Arabic),
  Windows reserved-name protection, separators/control characters removed.
- **API token:** optional bearer token or `?token=` for GETs. Empty token means
  no authentication; the default host is loopback. The static UI shell is served
  without a token because it contains no data; every API and artifact route is
  still token-gated.
- **Secrets:** `.env.example` has placeholders only; logs/errors redact
  `sk-...`, bearer tokens, and `api_key/token/secret` assignments.
- **Artifact serving:** Range supported; HEAD sends headers only; files must
  resolve inside the job folder.

---

## 12. Extension points

- Add an analyzer by implementing
  `analyze(transcript, options) -> AnalysisResult` and returning normalized clip
  dicts.
- Add a transcriber by implementing
  `transcribe(video, options) -> Transcript`; insert it in
  `build_transcriber()`.
- Add a downloader by implementing `download(url, options) ->
  DownloadedVideo`; the pipeline depends only on that protocol.
- Add a submission target by consuming `result.json`; no pipeline change is
  needed.
- For a multi-user deployment, replace `JobManager`'s in-process executor with
  a real queue and keep the existing disk record/state contract.

---

## 13. Assumptions

- A single operator runs the app; no multi-tenant isolation is attempted.
- yt-dlp can access the URL; DRM/login-gated sources are out of scope.
- The transcript is the source of truth for sentence timing.
- An OpenAI-compatible chat-completions endpoint is available when an LLM is
  configured; otherwise the heuristic fallback is acceptable for smoke tests,
  not final editorial quality.
- Platform labels are metadata, not upload integrations.
