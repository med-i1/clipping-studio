# Viral Clips Local Web UI — Design

**Date:** 2026-11-11
**Status:** Approved in conversation
**Scope:** Complete the existing localhost HTML interface for the viral clip pipeline.

## Intent

A user should be able to open one local page, paste a video URL, choose the clipping
options, watch the job run, inspect the ranked clips, read the report and logs, retry a
failed job, and download every artifact — without touching the CLI.

The current page already covers the happy path (create a job, progress, clip playback,
ZIP download, recent jobs). It is a single inline HTML string and lacks job filtering,
retry controls, live logs, report preview, token entry, and robust empty/error states.

## Goals

- One page at `/` served by `viral_clips.py serve`.
- Full job form: URL, clip count, min/max duration, language, platforms, subtitles,
  original-video retention, optional yt-dlp format.
- Live job status, progress, current step and elapsed time.
- Job sidebar with status filter and retry for failed jobs.
- Result view with clip playback, score, hook, reason, summary, keywords and downloads
  for MP4, SRT, VTT, TXT, ZIP and report.
- Live log tail and report preview inside the page.
- Token gate when `VIRAL_API_TOKEN` is configured.
- Responsive, keyboard-usable, accessible dark interface consistent with the desktop app.
- No new runtime dependencies.

## Non-goals

- Multi-user authentication, accounts or remote exposure.
- Deleting jobs or editing completed results.
- Uploading local video files (the pipeline is URL-based).
- A React/Vite build step or any external CDN asset.
- Changing pipeline, analysis, transcription or extraction behaviour.

## Constraints

- Python standard library only; static HTML/CSS/JS only.
- Python `>=3.11`; existing API responses stay backward compatible.
- All 101 existing tests must keep passing.
- The UI assets must work in a PyInstaller one-file build and in a source install.
- Existing path-traversal, token and artifact checks stay intact.

## Approaches Considered

1. **Grow the inline `_INDEX_HTML` string.** Fastest, but the page is already ~180 lines
   and a complete UI would make `server.py` unmaintainable and hard to test.
2. **Static assets in `clipper/viral/web/`, served by the existing stdlib server.**
   Chosen. Clean separation, no build tool, no dependencies, straightforward packaging.
3. **React/Vite SPA.** Best component ergonomics, but adds Node, a build artifact and
   packaging complexity for a single-user local tool. Rejected.

## Design

### Files

- `clipper/viral/web/index.html` — static shell and semantic structure.
- `clipper/viral/web/styles.css` — dark responsive theme.
- `clipper/viral/web/app.js` — API client, state, polling and rendering.
- `clipper/viral/server.py` — static routes, log endpoint, inline report option.

### Server routes

- `GET /`, `/index.html`, `/app.js`, `/styles.css` — static assets, served without a
  token because they contain no data. Every API route still requires the token.
- `GET /api/jobs/{job_id}/log?tail=200` — last `tail` JSONL records from
  `logs/job.jsonl`. `tail` is clamped to `1..1000`; a missing log returns an empty list.
- `GET /api/jobs/{job_id}/report?inline=1` — same report, without
  `Content-Disposition: attachment`, for in-page preview and new-tab viewing.
- Existing create, list, status, result, retry, download and file routes are unchanged.

Static serving uses a fixed filename allow-list, never a user-supplied path. `_web_dir()`
resolves `sys._MEIPASS/web` in a frozen build and `clipper/viral/web` in source installs.
HTML/CSS/JS responses are `Cache-Control: no-store` and carry a restrictive CSP.

### Interface

- **Top bar:** product name, server state, token state.
- **Sidebar:** New job button, status filter, job list with status dot, progress,
  short id, age. Selecting a job opens it.
- **New job view:** the full form with inline validation, advanced options collapsed by
  default, and the last values remembered in `localStorage`.
- **Job view:** header (source URL, status, elapsed), progress bar and step, actions
  (retry when failed, ZIP, report), then tabs:
  - **Clips:** result header, video title/metadata, clip cards with player, score,
    category, platforms, hook, reason, summary, keywords, copy buttons and download links.
  - **Logs:** live tail while the job runs, level/stage/time columns.
  - **Report:** rendered as safe preformatted Markdown text with a download link.
- **Token gate:** a 401 opens a dialog; the token is kept in `sessionStorage` and removed
  from the visible URL with `history.replaceState`. Media and download links receive it
  as a query parameter.

### Data flow

`loadJobs()` refreshes the sidebar every 5 s when idle and every 2 s while a job is
active. A single job poll updates progress and step, then loads results or the error.
Polling stops on completion/failure and when a different job is opened. No overlapping
poll loops.

### Safety

- All dynamic strings are inserted with `textContent` or `createElement`, never as HTML.
- Log lines are parsed as JSON and rendered as text.
- Artifact and log paths are resolved with the existing `ensure_within` check.
- The API token is never written to logs or the page URL after initialization.

## Acceptance

- Existing 101 tests plus new server/static/log tests pass.
- `python -m compileall` passes.
- Browser smoke test: page loads without console errors, form is usable, empty state is
  clear, and a completed job renders clips and downloads.

## Left to the Builder

Exact visual polish, copy, and the internal organisation of `app.js` are implementation
choices; the routes, data shapes and security rules above are fixed.
