# Project inspection report

Date: 2026-11-11  
Scope: `H:\Cliping\Clipping Studio`  
Purpose: establish how the existing application works before adding the viral
clip pipeline.

---

## 1. What the project is

`Clipping Studio` is a Windows-oriented Python application for downloading a
video and cutting it into clips. It has two existing front ends:

- a **Tkinter desktop GUI**, started by `python cli.py` (default path) or
  `python -m clipper.gui`;
- an **interactive command-line flow**, started by `python cli.py --cli` or
  `python -m clipper`.

Both call `clipper.engine.ClipperEngine`, so the download/cut logic is shared.

The project is not a web application. There is no HTTP backend, database,
queue, authentication layer, or packaging service in the original code.

---

## 2. Language, framework, and runtime

| Item | Finding |
|---|---|
| Language | Python |
| Declared version | `>=3.11` (`pyproject.toml`) |
| GUI framework | Tkinter / ttk |
| CLI framework | `argparse` in the new viral CLI; the original CLI is interactive `input()` |
| Media tools | `yt-dlp`, `ffmpeg`, `ffprobe` as external processes |
| Packaging | PyInstaller (`Clipping Studio.spec`, `build_exe.bat`) |
| Test framework | `unittest`; no pytest configuration |
| Web/API | None in the original app |

The local runtime used for verification is Python 3.15.2. `yt-dlp` 2026.10.26,
`ffmpeg`, and `ffprobe` are available on `PATH`; Tkinter and `yt_dlp` are
importable. `requests`, `httpx`, `pytest`, `openai`, `anthropic`,
`faster_whisper`, and `python-dotenv` are **not** installed. The new code was
therefore written with the standard library and optional imports only.

---

## 3. Entry points and flow

### Desktop GUI

`cli.py` imports `clipper.gui.main` when no `--cli` argument is present.

`clipper/gui.py` builds the main window, loads configuration, accepts a URL and
a timings file, and runs `ClipperEngine` in a worker thread. It has a menu,
progress reporting, logs, and project-opening actions.

### CLI

`cli.py --cli` calls `clipper.main.main()`, which prompts for:

1. a video URL;
2. a timings file path.

It then invokes `ClipperEngine.process(url, timings_file)` and prints a summary.

### Engine

`clipper/engine.py` orchestrates project creation, yt-dlp download, FFmpeg
cutting, resume/retry, CSV/JSON/README output, and statistics. It supports
local full-video download and direct remote clip cutting.

### Configuration

The original configuration is JSON at `Clipping/config.json`
(`clipper/config.py`). Defaults include format, retry count, keyframe forcing,
output mode, FFmpeg preset/CRF, parallel workers, and language. It is not
environment-variable based.

---

## 4. Existing downloader

Location: `clipper/downloader.py`.

Key function:

```python
download_full_video(url: str, output_path: Path, config: Dict[str, Any]) -> bool
```

Behavior:

- returns `True` if `output_path` already exists and passes
  `validate_video(..., min_file_size_kb=50)`;
- runs the external `yt-dlp` executable directly (hard-coded name, not a
  configurable binary path);
- uses `-f bv*+ba/b` by default, `--merge-output-format mp4`,
  `--remux-video mp4`, `--no-playlist`, `--no-warnings`, `--no-part`;
- writes to a temporary stem and then renames or converts the result to MP4;
- returns `False` on download/conversion failure and prints yt-dlp's stderr;
- has **no built-in retry loop or typed errors** for the full-video download.

A second path, `download_clip_direct`, downloads one section with
`--download-sections` and retries that operation.

The viral pipeline must not replace this function. It wraps it in
`clipper/viral/download_adapter.py` via `ExistingYtDlpDownloader`, which adds
metadata, cache keys, retries, limits, typed errors, and thumbnail handling
while still calling `download_full_video` for the actual download.

---

## 5. Storage, jobs, and authentication

### Original storage

`clipper/paths.py` defines:

```text
BASE_DIR/Clipping/
├── config.json
├── lang/
├── timings/
└── output/
    └── {safe channel - title}/
        ├── Clips/
        ├── full_video.mp4        # depending on settings
        ├── clips.csv
        ├── project.json
        ├── README.txt
        ├── logs.txt
        └── state/thumbnail artifacts
```

`BASE_DIR` is the current working directory for source runs, or the executable
folder when frozen. Project names are sanitized with `safe_name`.

### Jobs

The original app has no durable job queue. A single engine run is synchronous
inside the CLI or a GUI worker thread. Resume support is project-file based
(`state.json`/`project.json` logic in `clipper/engine.py`).

### Authentication

None. There is no server and no user/account model. API keys are not used by
the original workflow.

---

## 6. Test coverage before the change

`tests/test_gui_error_recovery.py` exists and tests that a worker exception
re-enables the Start button and disables Stop. There was no broader unit or
integration suite, no fixtures, and no network mocking.

---

## 7. Best integration point

The new workflow is added as `clipper/viral/`, a separate package that:

- imports the existing `clipper.downloader.download_full_video` rather than
  editing it;
- has its own environment-based configuration (`clipper/viral/config.py`) so
  the existing JSON configuration remains untouched;
- writes to `Clipping/output/viral/{job_id}/`, leaving existing project
  folders alone;
- adds an optional **Tools → Viral Clips...** menu entry in `clipper/gui.py`;
- exposes a source-install CLI (`viral_clips.py`) and console script
  (`viral-clips`);
- optionally runs a local standard-library HTTP API/UI.

No existing function signature or GUI control was replaced.

---

## 8. Constraints and risks found

1. **No third-party Python HTTP libraries.** The LLM and Whisper clients use
   `urllib.request` from the standard library.
2. **No pytest/openai/whisper installed.** Tests use `unittest` and dependency
   injection; optional providers are lazy imports.
3. **Existing full-video downloader has no retries.** Retries and error
   classification are implemented in the adapter, not in the legacy function.
4. **The existing downloader hard-codes `yt-dlp`.** The viral adapter honors
   `YTDLP_BIN` for metadata but the final download still uses the legacy
   command; this preserves behavior but means a custom yt-dlp path applies only
   to metadata/thumbnail calls unless the legacy function is later changed.
5. **No persistent worker service.** The new `JobManager` is in-process with
   JSON persistence. A crashed process leaves a job in its last state and needs
   a manual retry. `--no-wait` uses a detached Python worker in source installs.
6. **PyInstaller needed data updates.** The analysis prompt is now bundled in
   `Clipping Studio.spec` and `build_exe.bat`; frozen output paths resolve next
   to the EXE.
7. **Platform output is advisory.** Clips are MP4/H.264/AAC where re-encoding
   is needed, but the app does not upload or enforce every platform's current
   spec.
8. **Real-network verification was not performed** because no test URL/API key
   was supplied. The download/LLM/transcription boundaries are covered with
   mocks; FFmpeg extraction is tested with a synthetic local video.
9. **The repository has no `.git` metadata in this environment.** Changes are
   on disk only and should be committed by the owner.

---

## 9. Verification commands

```powershell
cd "H:\Cliping\Clipping Studio"
python -m compileall clipper viral_clips.py
python -m unittest discover -s tests -v
```

The suite covers input/path validation, subtitles, timing repair, JSON
analysis/repair, downloader adapter behavior, job state/retry/persistence,
packaging/ZIP, the local API, and real FFmpeg extraction (skipped when FFmpeg
is unavailable).
