# Viral Clips Local Web UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the inline status page with a complete, dependency-free single-page UI for creating, monitoring, retrying and inspecting viral clipping jobs on localhost.

**Architecture:** Static `index.html`, `styles.css` and `app.js` live in `clipper/viral/web/` and are served by the existing `ThreadingHTTPServer`. Two small API additions expose job logs and inline reports; the rest of the UI uses existing endpoints. No framework, CDN or build step.

**Tech Stack:** Python 3.11+ standard library, HTML5, CSS, vanilla JavaScript, `unittest`.

**Spec:** `docs/superpowers/specs/2026-11-11-viral-web-ui-design.md`

**Status (2026-11-11):** Implemented and verified. All six tasks are complete on disk, the
full suite passes (107 tests), and a synthetic completed job was smoke-tested in the
browser. The plan's per-task `git commit` steps were not run because this folder is not a
git repository. The real server is left running on `http://127.0.0.1:8765/` for the
user's real-video test.

## Global Constraints

- Standard library only; no new runtime dependencies.
- Keep every existing API response backward compatible.
- All existing 101 tests must pass.
- Static files must resolve in source installs and PyInstaller builds.
- Dynamic page content must be inserted as text, never as raw HTML.
- API token protection and path-traversal checks must remain in force.

## Review Focus

- A user opens `/` without a token while `VIRAL_API_TOKEN` is set: the page must load and ask for the token, not return JSON 401.
- A queued job has no log file yet: the logs tab must show an empty state, not an error.
- A log line is malformed or huge: the page must not crash or execute it.
- A clip filename is missing or the original video was discarded: result rendering must handle absent links.
- A failed job is retried from the page: polling must resume and the old error must clear.

---

### Task 1: Serve static UI assets and keep the API token-gated

**Files:**
- Create: `clipper/viral/web/index.html`
- Create: `clipper/viral/web/styles.css`
- Create: `clipper/viral/web/app.js`
- Modify: `clipper/viral/server.py`
- Test: `tests/test_viral_server.py`

**Interfaces:**
- Consumes: existing `ViralAPIHandler._send`, `_authorized`.
- Produces: `_web_dir() -> Path`, `WEB_ASSETS: dict[str, str]` mapping fixed URL paths to
  `(filename, content_type)`.

- [ ] **Step 1: Write failing tests for static assets**

```python
def test_static_ui_loads_without_a_token(self):
    response, payload = self._request("GET", "/")
    self.assertEqual(response.status, 200)
    self.assertIn("text/html", response.getheader("Content-Type", ""))
    self.assertIn(b"/app.js", payload)

    response, payload = self._request("GET", "/app.js")
    self.assertEqual(response.status, 200)
    self.assertIn("javascript", response.getheader("Content-Type", ""))

    response, _ = self._request("GET", "/styles.css")
    self.assertEqual(response.status, 200)

def test_static_ui_does_not_weaken_the_api_token(self):
    response, _ = self._request("GET", "/api/jobs")
    self.assertEqual(response.status, 401)
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `python -m unittest tests.test_viral_server.ServerTests.test_static_ui_loads_without_a_token -v`
Expected: FAIL with status 401 (current auth check runs before the page route).

- [ ] **Step 3: Add the static routes**

In `server.py`, before `_authorized()` in `do_GET`, serve only these fixed paths:

```python
WEB_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}
```

`_web_dir()` returns `Path(sys._MEIPASS) / "web"` when `sys._MEIPASS` exists, otherwise
`Path(__file__).resolve().parent / "web"`. `_serve_static` reads the fixed file, sends
`Cache-Control: no-store` and a CSP of
`default-src 'self'; img-src 'self' data:; media-src 'self' blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'`.
Unknown paths still return the JSON 404.

- [ ] **Step 4: Create minimal placeholder assets**

Create valid `index.html` and `styles.css`, and an `app.js` with
`document.addEventListener('DOMContentLoaded', () => {});` so the routes have real files
while the UI tasks proceed.

- [ ] **Step 5: Run the tests and verify they pass**

Run: `python -m unittest tests.test_viral_server -v`
Expected: PASS, including the existing token, range, HEAD and traversal tests.

- [ ] **Step 6: Commit**

```bash
git add clipper/viral/server.py clipper/viral/web tests/test_viral_server.py
git commit -m "feat(web): serve static UI assets"
```

---

### Task 2: Add the job log endpoint and inline report option

**Files:**
- Modify: `clipper/viral/server.py`
- Test: `tests/test_viral_server.py`

**Interfaces:**
- Produces: `GET /api/jobs/{job_id}/log?tail=200` returning
  `{"job_id": str, "lines": list[dict]}`, newest records last.
- Produces: `GET /api/jobs/{job_id}/report?inline=1` without an attachment header.

- [ ] **Step 1: Write failing tests**

Create `job_root/logs/job.jsonl` with three JSON lines whose `stage` values are
`transcription_started`, `analysis_started`, `clip_ready`. Then:

```python
def test_job_log_endpoint_returns_the_requested_tail(self):
    headers = {"Authorization": "Bearer secret-token"}
    response, payload = self._request(
        "GET", f"/api/jobs/{self.job_id}/log?tail=2", headers=headers
    )
    self.assertEqual(response.status, 200)
    lines = json.loads(payload)["lines"]
    self.assertEqual([line["stage"] for line in lines], ["analysis_started", "clip_ready"])

def test_job_log_endpoint_handles_a_missing_log(self):
    headers = {"Authorization": "Bearer secret-token"}
    response, payload = self._request(
        "GET", f"/api/jobs/{self.job_id}/log", headers=headers
    )
    self.assertEqual(response.status, 200)
    self.assertEqual(json.loads(payload)["lines"], [])

def test_report_can_be_served_inline(self):
    headers = {"Authorization": "Bearer secret-token"}
    response, payload = self._request(
        "GET", f"/api/jobs/{self.job_id}/report?inline=1", headers=headers
    )
    self.assertEqual(response.status, 200)
    self.assertEqual(payload, b"# Report\n")
    self.assertIsNone(response.getheader("Content-Disposition"))
```

Note: the missing-log test removes the log file first because the fixture job has none.

- [ ] **Step 2: Run the tests and verify they fail**

Run: `python -m unittest tests.test_viral_server -v`
Expected: FAIL with 404 for `/log`; the inline test sees an attachment header.

- [ ] **Step 3: Implement `_serve_job_log` and `inline` handling**

Route `len(parts) == 4 and parts[3] == "log"` to `_serve_job_log`.
- Resolve `job_root / "logs" / "job.jsonl"` with `ensure_within`.
- Return `{"job_id": ..., "lines": []}` when the file does not exist.
- Read text as UTF-8 with `errors="replace"`, split lines, take the last `tail` (default
  200, clamp `1..1000`), parse each with `json.loads`; on failure return
  `{"level": "INFO", "stage": "log", "message": line}`.
- In `_serve_artifact`, read `inline=1` from the query and pass
  `attachment_name=None` for the report.

- [ ] **Step 4: Run the tests and verify they pass**

Run: `python -m unittest tests.test_viral_server -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add clipper/viral/server.py tests/test_viral_server.py
git commit -m "feat(api): expose job logs and inline reports"
```

---

### Task 3: Build the HTML shell and styles

**Files:**
- Replace: `clipper/viral/web/index.html`
- Replace: `clipper/viral/web/styles.css`
- Test: `tests/test_viral_server.py`

**Interfaces:**
- Produces these stable ids for `app.js`:
  `token-dialog`, `token-form`, `token-input`, `app`, `new-job-btn`, `refresh-btn`,
  `filter`, `job-list`, `view`, `form-view`, `job-view`, `job-form`, `url`, `clips`,
  `min-duration`, `max-duration`, `language`, `platform-tiktok`,
  `platform-instagram-reels`, `platform-youtube-shorts`, `subtitles`,
  `keep-original`, `format`, `submit-btn`, `form-error`, `job-title`, `job-status`,
  `job-progress`, `job-step`, `job-meta`, `job-actions`, `tabs`, `tab-clips`,
  `tab-logs`, `tab-report`, `panel-clips`, `panel-logs`, `panel-report`.

- [ ] **Step 1: Write the failing structure test**

```python
def test_ui_shell_exposes_the_expected_controls(self):
    response, payload = self._request("GET", "/app.js")
    self.assertEqual(response.status, 200)
    response, payload = self._request("GET", "/")
    html = payload.decode("utf-8")
    for element_id in ("job-form", "job-list", "tab-logs", "token-dialog"):
        self.assertIn(f'id="{element_id}"', html)
```

- [ ] **Step 2: Run it and verify it fails**

Run: `python -m unittest tests.test_viral_server.ServerTests.test_ui_shell_exposes_the_expected_controls -v`
Expected: FAIL because the placeholder lacks the controls.

- [ ] **Step 3: Write the shell**

Semantic structure: top bar, sidebar with New job, filter and `#job-list`, main `#view`
containing `#form-view` and `#job-view`, a token dialog, and a toast region.
Labels wrap every input; buttons are real `<button>` elements; tabs use `role="tablist"`,
`role="tab"`, `aria-selected` and `hidden` panels. The form includes all ids listed
above and a platform checkbox group. The report and log panels start empty.

- [ ] **Step 4: Write the styles**

Dark theme using the existing palette (`#101114`, `#191b20`, `#7dd3a8`), CSS grid for
sidebar/main, cards, status badges (queued/running/completed/failed), progress bar,
clip grid, log table, dialog and toast. Add `:focus-visible`, `prefers-reduced-motion`,
and a single-column layout below 860px. No external fonts or images.

- [ ] **Step 5: Run the structure test and the suite**

Run: `python -m unittest tests.test_viral_server -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add clipper/viral/web/index.html clipper/viral/web/styles.css tests/test_viral_server.py
git commit -m "feat(web): add the UI shell and theme"
```

---

### Task 4: Implement the UI behaviour

**Files:**
- Replace: `clipper/viral/web/app.js`
- Test: browser smoke (Task 6)

**Interfaces:**
- Consumes: `GET /api/jobs`, `POST /api/jobs`, `GET /api/jobs/{id}`,
  `GET /api/jobs/{id}/results`, `GET /api/jobs/{id}/log`, `POST /api/jobs/{id}/retry`,
  `GET /api/jobs/{id}/download`, `GET /api/jobs/{id}/report`, `GET /files/{id}/{path}`.
- Produces: `api(path, options)`, `loadJobs()`, `selectJob(jobId)`, `pollJob(jobId)`,
  `renderJob(job)`, `renderResult(result)`, `renderLogs(lines)`, `renderReport(text)`,
  `retryJob(jobId)`.

- [ ] **Step 1: Implement the API client and token gate**

Read the token from `?token=` first, then `sessionStorage`. If a URL token exists, store
it and remove it with `history.replaceState`. On 401, open `#token-dialog`; submitting
stores the token and retries the last request. Attach the token to `Authorization` and
to media/download URLs as `?token=`.

- [ ] **Step 2: Implement jobs, form and polling**

`loadJobs()` renders every job with a status dot, 8-character id, progress and relative
time, applies the status filter, and keeps the selected job highlighted. `createJob()`
validates the URL and duration order, disables the submit button, POSTs the full options
object, selects the new job and starts polling. `pollJob()` uses one timer, stops on
completion/failure or when another job is selected, and refreshes the sidebar.

- [ ] **Step 3: Implement job detail and results**

`renderJob()` sets text, badge class, progress width, step, source URL, attempts and
error. Completed jobs load `results`; failed jobs show the error and a Retry button that
POSTs `/retry` and resumes polling. `renderResult()` builds a header with video title,
duration, clip count, ZIP/report actions and a responsive clip grid. Every clip shows the
video, rank/title, score, duration, category, platforms, hook, reason, summary, keywords,
copy-hook/copy-caption buttons and links for MP4/SRT/VTT/TXT. Missing files hide the
link. All text uses `textContent`.

- [ ] **Step 4: Implement logs, report and tabs**

The Logs tab fetches `/log?tail=500`, renders time/level/stage/message, keeps the newest
line visible when the user is near the bottom, and refreshes every 2 s while the job is
active. The Report tab fetches `/api/jobs/{id}/report` as text and displays it in a
`<pre>` with a download link; `?inline=1` is used for "open in new tab". Tabs toggle
`hidden` and `aria-selected`.

- [ ] **Step 5: Add convenience behaviour**

Persist the last form values in `localStorage` under `viral-form-v1`; restore on load.
Show a toast for copy/download errors. Disable actions while a request is in flight.
Handle empty job lists and jobs with zero clips.

- [ ] **Step 6: Syntax-check and commit**

Run: `node --check clipper/viral/web/app.js` (skip if Node is unavailable).
Expected: exit 0.

```bash
git add clipper/viral/web/app.js
git commit -m "feat(web): complete the interactive job UI"
```

---

### Task 5: Package the assets and document the UI

**Files:**
- Modify: `Clipping Studio.spec`
- Modify: `build_exe.bat`
- Modify: `README.md`
- Modify: `docs/pipeline.md`

- [ ] **Step 1: Add web assets to the PyInstaller inputs**

Spec: add `('clipper/viral/web', 'web')` to `datas`. Build script: add
`--add-data "clipper/viral/web;web"`.

- [ ] **Step 2: Update documentation**

README: replace the "small status UI" wording with the full feature list, document the
token dialog and `?token=` fallback, and add `GET /api/jobs/{id}/log` and
`report?inline=1` to the endpoint table. `docs/pipeline.md`: describe the static asset
directory and the log endpoint in the API section.

- [ ] **Step 3: Run the full suite**

Run: `python -m unittest discover -s tests -v`
Expected: all tests pass (101 existing + new).

- [ ] **Step 4: Commit**

```bash
git add "Clipping Studio.spec" build_exe.bat README.md docs/pipeline.md
git commit -m "docs(web): package and document the local UI"
```

---

### Task 6: Verify in a real browser

**Files:** none.

- [ ] **Step 1: Compile and start the server**

Run: `python -m compileall -q clipper viral_clips.py tests`
Run: `python viral_clips.py serve --host 127.0.0.1 --port 8765`

- [ ] **Step 2: Smoke-test the page**

Open `http://127.0.0.1:8765/` in the in-app browser. Confirm:
- the sidebar, New job form, token gate and empty state render;
- no console errors and no failed asset requests;
- the form accepts values and the platform checkboxes are present;
- switching filters does not throw;
- with a synthetic completed job, clips, log lines and report preview render, and
  missing optional files do not produce broken links.

- [ ] **Step 3: Stop the server and record the result**

Stop the background server after the check. Record the browser result in the final
summary.

## Self-Review

- Spec coverage: static serving, token boundary, log endpoint, inline report, full form,
  job list/filter/retry, live progress, clip results, logs, report, packaging and docs
  are each owned by a task.
- Type consistency: route names and element ids match between tasks; `lines` is always a
  list of objects; result fields match `build_result` in `packaging.py`.
- Review focus: token-without-URL, missing log, malformed log, missing clip file and
  retry-after-failure each have a test or an explicit smoke check.
- Proportion: the plan names interfaces and tests; it does not transcribe the JS.
