# Clipping Studio

A Windows desktop and command-line tool for turning long videos into short clips.

The project has two workflows that share the same FFmpeg and yt-dlp tooling:

1. **Timed clips (existing):** supply a video URL and a timings file, and the
   app cuts exactly the requested ranges.
2. **Viral clips (new):** supply only a video URL. The app downloads the video,
   gets a timestamped transcript, asks an LLM to propose viral moments,
   validates their timings against the transcript, extracts the clips, and
   packages the results.

The existing downloader, GUI, CLI, configuration, and output layout are kept.
The viral workflow is an additional package: `clipper/viral/`.

---

## Requirements

- Python 3.11 or newer.
- `yt-dlp` on `PATH` (or set `YTDLP_BIN`).
- `ffmpeg` and `ffprobe` on `PATH` (or set `FFMPEG_BIN` / `FFPROBE_BIN`).
- Tkinter for the desktop GUI (normally included with the Windows Python
  installer).
- Optional: an OpenAI-compatible API key for LLM analysis and Whisper
  transcription.
- Optional: `faster-whisper` for local speech-to-text when native captions are
  unavailable.

The viral pipeline itself uses only the Python standard library. It does not
require `requests`, `openai`, `pytest`, or a database.

Check the tools:

```powershell
python --version
yt-dlp --version
ffmpeg -version
ffprobe -version
```

---

## Run the existing timed-clip workflow

Desktop GUI:

```powershell
python cli.py
```

The GUI also exposes the viral workflow under **Tools → Viral Clips...**.

Interactive CLI:

```powershell
python -m clipper
```

The CLI asks for a URL and a timings file, then writes clips under
`Clipping/output/`.

---

## Run the viral clip pipeline

### Command line

```powershell
# Run and wait for the result (recommended for the first run)
python viral_clips.py run --url "https://www.youtube.com/watch?v=VIDEO_ID" --clips 5
```

On Windows, the convenience file is equivalent to:

```powershell
python -m clipper.viral.cli run --url "https://www.youtube.com/watch?v=VIDEO_ID"
```

Common options:

```powershell
python viral_clips.py run `
  --url "https://www.youtube.com/watch?v=VIDEO_ID" `
  --clips 5 `
  --min-duration 20 `
  --max-duration 90 `
  --language auto `
  --platforms tiktok youtube_shorts `
  --timeout 7200
```

Useful flags:

| Flag | Meaning |
|---|---|
| `--discard-original` | Remove the downloaded source video before packaging. |
| `--no-subtitles` | Skip per-clip SRT/VTT/caption files. |
| `--format` | Override the yt-dlp format selector. |
| `--no-wait` | Return immediately and run a detached worker process (source install only). |
| `--json` | Print the final job record as JSON. |

Other commands:

```powershell
python viral_clips.py list
python viral_clips.py status JOB_ID
python viral_clips.py retry JOB_ID --wait
python viral_clips.py serve --host 127.0.0.1 --port 8765
```

### Desktop GUI

Open the main app and choose **Tools → Viral Clips...**. The window accepts a
URL, clip count, duration range, language, subtitle/original-video switches,
shows progress and clip rows, and can open the job folder or report.

The GUI persists jobs in the same folders as the CLI, so a job can be inspected
with `viral_clips.py status` after the window is closed.

### Configuration

Copy `.env.example` to `.env` and fill in what you need:

```powershell
Copy-Item .env.example .env
```

Important variables:

```ini
# Use a real LLM for production-quality selection.
LLM_PROVIDER=auto
LLM_MODEL=gpt-4o-mini
LLM_API_KEY=
LLM_BASE_URL=https://api.openai.com/v1

# auto = captions, then Whisper API, then local faster-whisper.
TRANSCRIPTION_PROVIDER=auto
TRANSCRIPTION_MODEL=whisper-1
TRANSCRIPTION_API_KEY=
TRANSCRIPTION_BASE_URL=

# Output and processing limits.
VIRAL_OUTPUT_DIR=Clipping/output/viral
MAX_VIDEO_DURATION_SECONDS=14400
MAX_CLIPS=10

# Optional local API token.
VIRAL_API_TOKEN=
```

`LLM_PROVIDER=auto` uses the LLM when `LLM_API_KEY` is set; otherwise it falls
back to a deterministic offline heuristic analyzer. The heuristic mode is
useful for smoke tests and for running without secrets, but a configured LLM
produces better editorial selections.

For a Google AI Studio (Gemini) key, use the Gemini OpenAI-compatible surface:

```ini
LLM_PROVIDER=gemini
LLM_API_KEY=your-gemini-key
LLM_MODEL=gemini-3.8-flash
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
```

`LLM_MODEL` and `LLM_BASE_URL` can be omitted with `LLM_PROVIDER=gemini`: the
OpenAI defaults are replaced with `gemini-3.8-flash` and the Gemini endpoint
automatically. `google` and `google_gemini` are accepted as aliases. A YouTube
Data API key is not a Gemini key and cannot run the analysis step.

The analysis prompt is a separate editable file:
`prompts/viral_clip_analysis.txt`. Override it with `LLM_PROMPT_FILE`.

---

## Output layout

Each job gets an isolated folder:

```text
Clipping/output/viral/{job_id}/
├── original/
│   └── video.mp4                 # omitted from the ZIP with --discard-original
├── transcript/
│   ├── raw.json                  # provider output / raw segments
│   ├── clean.json                # normalized transcript
│   ├── full.srt
│   ├── full.vtt
│   └── full.txt
├── clips/
│   ├── clip_001_rank_1_viral_score_92.mp4
│   ├── clip_001_rank_1_viral_score_92.srt
│   ├── clip_001_rank_1_viral_score_92.vtt
│   ├── clip_001_rank_1_viral_score_92.txt          # caption/hashtags
│   └── clip_001_rank_1_viral_score_92.transcript.txt
├── metadata/
│   ├── job.json
│   ├── download.json
│   ├── source_info.json
│   ├── analysis_raw.json
│   ├── validated_clips.json
│   ├── clip_001.json
│   └── thumbnail.jpg
├── logs/
│   └── job.jsonl                 # structured, one JSON object per line
├── result.json                   # stable machine-readable result
├── report.md                     # human-readable report
└── package.zip                   # everything above except temp/nested ZIPs
```

`result.json` contains relative paths, the video/transcript metadata, the
analysis provider/model, and for every clip: rank, title, hook, start/end,
duration, score, category, reason, summary, platforms, keywords, confidence,
timing repairs, and output files.

---

## Local API and web UI

```powershell
python viral_clips.py serve
```

Open `http://127.0.0.1:8765/` for the local web UI. The page can create a job,
choose clips/duration/language/platforms/subtitles, watch live progress, filter
and open past jobs, retry failures, delete finished jobs, preview clips, copy hooks/captions, tail the
job log, read the report, and download MP4/SRT/VTT/TXT/ZIP artifacts. The
interface is plain HTML/CSS/JS served by the Python process; it needs no build
step or external CDN. The server binds to `127.0.0.1` by default.

Endpoints:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Health check. |
| `GET` | `/api/jobs` | List jobs. |
| `GET` | `/api/jobs/{job_id}` | Job status and progress. |
| `GET` | `/api/jobs/{job_id}/results` | Completed `result.json`. |
| `GET` | `/api/jobs/{job_id}/log?tail=200` | Latest structured log records (`tail` clamped to 1–1000). |
| `GET` | `/api/jobs/{job_id}/download` | Download `package.zip`. |
| `GET` | `/api/jobs/{job_id}/report` | Download `report.md`; add `?inline=1` to view it in the browser. |
| `GET` | `/files/{job_id}/{relative_path}` | Serve an artifact, with Range support. |
| `POST` | `/api/jobs` | Create a job: `{"url": "...", "options": {...}}`. |
| `POST` | `/api/jobs/{job_id}/retry` | Retry a failed job. |
| `DELETE` | `/api/jobs/{job_id}` | Delete a finished job record and its output folder. |

If `VIRAL_API_TOKEN` is set, the page loads and asks for the token; it is kept in
session storage and removed from the address bar. You can also open
`http://127.0.0.1:8765/?token=TOKEN`. API clients can send
`Authorization: Bearer TOKEN` or add `?token=TOKEN` to a GET URL. Artifact paths
are checked against the job folder; traversal attempts are rejected.

---

## Tests

The test suite uses `unittest` and mocks network/API calls. It does not need a
real video URL or an API key.

```powershell
python -m unittest discover -s tests -v
```

Real-FFmpeg extraction tests are skipped automatically when `ffmpeg` or
`ffprobe` is missing. They generate a small synthetic video locally.

The existing GUI recovery test remains part of the suite.

---

## Documentation

- [`docs/inspection-report.md`](docs/inspection-report.md) — project audit,
  integration points, storage/auth findings, and constraints.
- [`docs/pipeline.md`](docs/pipeline.md) — pipeline stages, validation rules,
  state machine, error codes, security notes, and extension points.

---

## Known limitations

- **LLM quality depends on configuration.** Without `LLM_API_KEY`, analysis is
  heuristic and should be treated as a baseline rather than a final editor.
- **Native captions may not exist.** The fallback needs an API key or a local
  `faster-whisper` installation for videos without captions.
- **The packaged EXE cannot use `--no-wait`.** The detached worker is available
  in the source installation; the desktop app keeps jobs in its own process.
- **The job queue is in-process and disk-backed.** It is suitable for one
  desktop user, not a multi-tenant server. Jobs must be retried manually after
  an app crash.
- **Clip extraction is CPU/disk intensive for long videos.** Stream copy is
  used only when the source is already H.264/AAC; VP9, AV1, Opus, and other
  incompatible sources are re-encoded so the MP4 is accepted by social
  platforms.
- **Platform checks are advisory.** The pipeline tags clips for TikTok,
  Instagram Reels, and YouTube Shorts, but does not upload them or enforce each
  platform's current encoding rules beyond MP4/H.264/AAC output.

---

## Security notes

- Do not commit `.env` or API keys. `.env.example` contains placeholders only.
- The API is local by default. If you expose it, set `VIRAL_API_TOKEN` and put
  it behind a trusted reverse proxy.
- URL validation rejects `file://`, localhost, private IP literals, embedded
  credentials, and control characters before a URL reaches yt-dlp.
- Job IDs and artifact paths are validated to prevent directory traversal.
- Logs and error payloads redact common API-key/token patterns.
