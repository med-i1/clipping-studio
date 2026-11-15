"use strict";

const $ = (id) => document.getElementById(id);

class ApiError extends Error {
  constructor(message, status, data) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.data = data;
  }
}

const state = {
  token: "",
  jobs: [],
  currentJobId: null,
  currentJob: null,
  result: null,
  pollTimer: null,
  logTimer: null,
  toastTimer: null,
  activeTab: "clips",
  reportJobId: null,
  reportText: null,
  jobsSignature: "",
};

/* ------------------------------------------------------------------ */
/* Small DOM helpers                                                   */
/* ------------------------------------------------------------------ */

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}

function button(label, className, onClick) {
  const element = node("button", className, label);
  element.type = "button";
  if (onClick) element.addEventListener("click", onClick);
  return element;
}

function link(label, href, className, download) {
  const element = node("a", className || "link-btn", label);
  element.href = href;
  if (download) element.setAttribute("download", "");
  return element;
}

function emptyState(title, message) {
  const wrap = node("div", "empty");
  wrap.append(node("strong", "", title));
  if (message) wrap.append(node("span", "", message));
  return wrap;
}

function clear(element) {
  element.replaceChildren();
}

/* ------------------------------------------------------------------ */
/* Formatting                                                          */
/* ------------------------------------------------------------------ */

function parseDate(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function formatDate(value) {
  const date = parseDate(value);
  return date ? date.toLocaleString() : value || "";
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours) return `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  return `${minutes}:${String(secs).padStart(2, "0")}`;
}

function timeAgo(value) {
  const date = parseDate(value);
  if (!date) return "";
  const seconds = Math.max(0, Math.round((Date.now() - date.getTime()) / 1000));
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

function formatTimestamp(value) {
  const date = parseDate(value);
  if (!date) return value || "";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function timezoneLabel() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  } catch {
    return "";
  }
}

/* ------------------------------------------------------------------ */
/* API and URLs                                                        */
/* ------------------------------------------------------------------ */

function withToken(url) {
  if (!state.token) return url;
  const separator = url.includes("?") ? "&" : "?";
  return `${url}${separator}token=${encodeURIComponent(state.token)}`;
}

function fileUrl(jobId, relative) {
  if (!relative) return "";
  const clean = String(relative).replace(/\\/g, "/").replace(/^\/+/, "");
  const encoded = clean.split("/").map(encodeURIComponent).join("/");
  return withToken(`/files/${encodeURIComponent(jobId)}/${encoded}`);
}

function downloadUrl(jobId, kind) {
  return withToken(`/api/jobs/${encodeURIComponent(jobId)}/${kind}`);
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) {
    showTokenGate();
    throw new ApiError("A valid API token is required.", 401);
  }
  const contentType = response.headers.get("Content-Type") || "";
  let data;
  if (contentType.includes("application/json")) {
    data = await response.json().catch(() => ({}));
  } else {
    data = await response.text();
  }
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    if (data && typeof data === "object" && data.error) {
      message = `${data.error.code || "ERROR"}: ${data.error.message || ""}`;
    } else if (typeof data === "string" && data.trim()) {
      message = data.trim().slice(0, 300);
    }
    throw new ApiError(message, response.status, data);
  }
  return data;
}

/* ------------------------------------------------------------------ */
/* Status, toasts and the token gate                                   */
/* ------------------------------------------------------------------ */

function setServerStatus(mode) {
  const element = $("server-status");
  const labels = {
    connecting: "Connecting…",
    online: "Server online",
    locked: "Token required",
    offline: "Server offline",
  };
  const classes = {
    connecting: "pill pill-muted",
    online: "pill status-completed",
    locked: "pill status-queued",
    offline: "pill status-failed",
  };
  element.className = classes[mode] || classes.connecting;
  element.textContent = labels[mode] || labels.connecting;
}

function showToast(message, isError = false) {
  const toast = $("toast");
  toast.textContent = message;
  toast.className = isError ? "toast is-error" : "toast";
  toast.hidden = false;
  if (state.toastTimer) clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => {
    toast.hidden = true;
  }, 4200);
}

function showTokenGate() {
  setServerStatus("locked");
  $("token-btn").hidden = false;
  const dialog = $("token-dialog");
  if (!dialog.open) {
    $("token-error").hidden = true;
    dialog.showModal();
    setTimeout(() => $("token-input").focus(), 0);
  }
}

async function submitToken(event) {
  event.preventDefault();
  const input = $("token-input");
  const error = $("token-error");
  const token = input.value.trim();
  if (!token) {
    error.textContent = "Enter the API token.";
    error.hidden = false;
    return;
  }
  const previous = state.token;
  state.token = token;
  try {
    await api("/api/jobs");
    sessionStorage.setItem("viral_token", token);
    $("token-dialog").close();
    $("token-btn").hidden = false;
    error.hidden = true;
    setServerStatus("online");
    await loadJobs({ initial: !state.currentJobId });
  } catch (failure) {
    state.token = previous;
    error.textContent = failure.status === 401 ? "That token was rejected." : failure.message;
    error.hidden = false;
  }
}

/* ------------------------------------------------------------------ */
/* Sidebar jobs                                                        */
/* ------------------------------------------------------------------ */

function visibleJobs() {
  const filter = $("filter").value;
  if (filter === "active") {
    return state.jobs.filter((job) => jobIsActive(job));
  }
  if (filter === "completed" || filter === "failed") {
    return state.jobs.filter((job) => job.status === filter);
  }
  return state.jobs;
}

function jobSignature(jobs) {
  return jobs
    .map((job) => `${job.job_id}:${job.status}:${job.progress}:${job.current_step}:${job.attempts}`)
    .join("|");
}

function renderJobs() {
  const list = $("job-list");
  const jobs = visibleJobs();
  clear(list);
  if (!jobs.length) {
    list.append(node("p", "job-empty", state.jobs.length ? "No jobs match this filter." : "No jobs yet. Create one to get started."));
    return;
  }
  for (const job of jobs) {
    const active = jobIsActive(job);
    const item = node("div", "job-item");
    if (job.job_id === state.currentJobId) item.classList.add("is-active");

    const main = node("button", "job-item-main");
    main.type = "button";
    main.title = job.source_url || job.job_id;
    main.addEventListener("click", () => selectJob(job.job_id));

    const top = node("span", "job-item-top");
    const dot = node("span", `job-dot status-${job.status}`);
    const id = node("span", "job-id", job.job_id.slice(0, 8));
    const status = node("span", "job-state", job.status);
    top.append(dot, id, status);

    const url = node("span", "job-url", job.source_url.replace(/^https?:\/\//, ""));
    const bar = node("span", "job-item-bar");
    const fill = node("i");
    fill.style.width = `${Math.max(0, Math.min(100, Number(job.progress) || 0))}%`;
    bar.append(fill);

    main.append(top, url, bar);

    const remove = node("button", "job-delete", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", `Delete job ${job.job_id.slice(0, 8)}`);
    remove.title = active ? "Wait for the job to finish before deleting" : "Delete job";
    remove.disabled = active;
    remove.addEventListener("click", () => deleteJob(job.job_id));

    item.append(main, remove);
    list.append(item);
  }
}

async function loadJobs({ initial = false } = {}) {
  try {
    const data = await api("/api/jobs");
    state.jobs = Array.isArray(data.jobs) ? data.jobs : [];
    setServerStatus("online");
    const signature = jobSignature(state.jobs);
    if (signature !== state.jobsSignature) {
      state.jobsSignature = signature;
      renderJobs();
    }
    if (initial && !state.currentJobId) {
      const active = state.jobs.find((job) => job.status === "running" || job.status === "queued");
      const first = active || state.jobs[0];
      if (first) {
        await selectJob(first.job_id);
      } else {
        showForm();
      }
    }
    if (!state.jobs.length && !state.currentJobId) showForm();
  } catch (failure) {
    if (failure.status === 401) {
      setServerStatus("locked");
      return;
    }
    setServerStatus("offline");
    if (initial) showToast(failure.message, true);
  }
}

/* ------------------------------------------------------------------ */
/* Views, tabs and job polling                                         */
/* ------------------------------------------------------------------ */

function showForm() {
  stopPolling();
  stopLogTimer();
  state.currentJobId = null;
  state.currentJob = null;
  state.result = null;
  state.reportJobId = null;
  state.reportText = null;
  $("job-view").hidden = true;
  $("form-view").hidden = false;
  renderJobs();
  setTimeout(() => $("url").focus(), 0);
}

function showJobView() {
  $("form-view").hidden = true;
  $("job-view").hidden = false;
}

function setActiveTab(name) {
  state.activeTab = name;
  const tabs = [
    ["clips", "tab-clips", "panel-clips"],
    ["logs", "tab-logs", "panel-logs"],
    ["report", "tab-report", "panel-report"],
  ];
  for (const [key, tabId, panelId] of tabs) {
    const tab = $(tabId);
    const panel = $(panelId);
    const active = key === name;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-selected", String(active));
    panel.hidden = !active;
  }
  if (name === "logs") {
    loadLogs(true);
  } else if (name === "report") {
    loadReport();
  } else {
    stopLogTimer();
  }
}

function stopPolling() {
  if (state.pollTimer) {
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
  }
}

function stopLogTimer() {
  if (state.logTimer) {
    clearTimeout(state.logTimer);
    state.logTimer = null;
  }
}

const ACTIVE_JOB_STATUSES = [
  "queued",
  "downloading",
  "transcribing",
  "analyzing",
  "validating",
  "extracting_clips",
  "packaging",
];

function jobIsActive(job) {
  return Boolean(job && ACTIVE_JOB_STATUSES.includes(job.status));
}

function statusPill(job) {
  const pill = $("job-status");
  pill.className = `pill status-${job.status || "queued"}`;
  pill.textContent = job.status || "queued";
}

function applyJob(job) {
  state.currentJob = job;
  showJobView();
  statusPill(job);
  $("job-source").textContent = job.source_url || job.job_id;
  $("job-source").title = job.source_url || job.job_id;
  if (!state.result) {
    $("job-title").textContent = `Job ${job.job_id.slice(0, 8)}`;
  }
  const parts = [`Created ${formatDate(job.created_at)}`, `Attempt ${job.attempts || 1}`, `Updated ${formatDate(job.updated_at)}`];
  $("job-meta").textContent = parts.join(" · ");
  const progress = Math.max(0, Math.min(100, Number(job.progress) || 0));
  $("job-progress").style.width = `${progress}%`;
  $("job-progress-label").textContent = `${Math.round(progress)}%`;
  const progressBar = document.querySelector(".progress");
  if (progressBar) progressBar.setAttribute("aria-valuenow", String(Math.round(progress)));
  $("job-step").textContent = String(job.current_step || job.status || "").replace(/_/g, " ");
  renderJobActions(job);
}

function renderJobActions(job) {
  const actions = $("job-actions");
  clear(actions);
  if (job.status === "failed") {
    actions.append(button("Retry job", "btn btn-primary btn-small", () => retryJob(job.job_id)));
  }
  if (job.status === "completed") {
    actions.append(link("Download ZIP", downloadUrl(job.job_id, "download"), "btn btn-ghost btn-small", true));
    actions.append(link("Open report", withToken(`/api/jobs/${job.job_id}/report?inline=1`), "btn btn-ghost btn-small"));
  }
  actions.append(button("Refresh", "btn btn-ghost btn-small", () => selectJob(job.job_id)));
  if (!jobIsActive(job)) {
    actions.append(button("Delete job", "btn btn-danger btn-small", () => deleteJob(job.job_id)));
  }
}

async function selectJob(jobId) {
  if (!jobId) return;
  stopPolling();
  stopLogTimer();
  state.currentJobId = jobId;
  state.currentJob = null;
  state.result = null;
  state.reportJobId = null;
  state.reportText = null;
  showJobView();
  setActiveTab("clips");
  renderJobs();
  $("job-title").textContent = `Job ${jobId.slice(0, 8)}`;
  $("job-status").className = "pill status-queued";
  $("job-status").textContent = "loading";
  $("job-progress").style.width = "0%";
  $("job-progress-label").textContent = "0%";
  $("job-step").textContent = "Loading job…";
  $("job-meta").textContent = "";
  clear($("job-actions"));
  clear($("panel-clips"));
  $("panel-clips").append(emptyState("Loading job…", "Reading the latest state."));
  try {
    const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
    applyJob(job);
    if (job.status === "completed") {
      await loadResult(jobId);
    } else if (job.status === "failed") {
      renderFailure(job);
    } else {
      startPolling(jobId);
    }
  } catch (failure) {
    if (failure.status === 401) return;
    $("job-step").textContent = "Could not load the job";
    $("panel-clips").replaceChildren(emptyState("Could not load the job", failure.message));
    showToast(failure.message, true);
  }
}

function startPolling(jobId) {
  stopPolling();
  const tick = async () => {
    if (state.currentJobId !== jobId) return;
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
      if (state.currentJobId !== jobId) return;
      applyJob(job);
      if (job.status === "completed") {
        await loadResult(jobId);
        loadJobs();
        return;
      }
      if (job.status === "failed") {
        renderFailure(job);
        loadJobs();
        return;
      }
      state.pollTimer = setTimeout(tick, 2000);
    } catch (failure) {
      if (failure.status === 401) return;
      state.pollTimer = setTimeout(tick, 4000);
    }
  };
  state.pollTimer = setTimeout(tick, 1200);
}

/* ------------------------------------------------------------------ */
/* Results                                                             */
/* ------------------------------------------------------------------ */

function renderFailure(job) {
  const panel = $("panel-clips");
  clear(panel);
  const error = job.error || {};
  const wrap = emptyState(
    error.code || "Job failed",
    error.message || "The pipeline stopped before producing clips."
  );
  if (error.retryable) wrap.append(node("span", "hint", "This error is marked retryable."));
  panel.append(wrap);
}

async function loadResult(jobId) {
  try {
    const result = await api(`/api/jobs/${encodeURIComponent(jobId)}/results`);
    if (state.currentJobId !== jobId) return;
    state.result = result;
    if (result.video && result.video.title) {
      $("job-title").textContent = result.video.title;
    }
    const job = state.currentJob;
    if (job) {
      const bits = [
        `Created ${formatDate(job.created_at)}`,
        formatDuration(result.video && result.video.duration),
        `${(result.clips || []).length} clips`,
        `Attempt ${job.attempts || 1}`,
      ];
      $("job-meta").textContent = bits.filter(Boolean).join(" · ");
    }
    renderResult(result);
    setActiveTab(state.activeTab);
  } catch (failure) {
    if (failure.status === 401) return;
    $("panel-clips").replaceChildren(emptyState("Result is not available", failure.message));
  }
}

function clipSubtitle(clip) {
  const bits = [
    formatDuration(clip.duration),
    `${formatDuration(clip.start_time)}–${formatDuration(clip.end_time)}`,
    clip.category,
  ];
  if (clip.confidence) bits.push(`${Math.round(clip.confidence * 100)}% confidence`);
  return bits.filter(Boolean).join(" · ");
}

function copyCaption(clip) {
  const hashtags = (clip.keywords || [])
    .map((keyword) => `#${String(keyword).replace(/\s+/g, "")}`)
    .join(" ");
  return [clip.title, clip.hook, clip.summary, hashtags].filter(Boolean).join("\n\n");
}

async function copyText(text, label) {
  try {
    await navigator.clipboard.writeText(text);
    showToast(label || "Copied to clipboard.");
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.append(area);
    area.select();
    try {
      document.execCommand("copy");
      showToast(label || "Copied to clipboard.");
    } catch {
      showToast("Copy failed. Select the text manually.", true);
    } finally {
      area.remove();
    }
  }
}

function renderResult(result) {
  const jobId = state.currentJobId;
  const panel = $("panel-clips");
  clear(panel);
  const video = result.video || {};
  const clips = Array.isArray(result.clips) ? result.clips : [];

  const head = node("div", "result-head");
  const headMain = node("div");
  headMain.append(node("h2", "", "Ranked clips"));
  headMain.append(node("p", "muted", `${clips.length} clip${clips.length === 1 ? "" : "s"} from ${formatDuration(video.duration)} of source${video.quality ? ` · ${video.quality}` : ""}`));
  const stats = node("div", "result-stats");
  const addStat = (label, value) => {
    if (!value) return;
    const item = node("span");
    item.append(node("strong", "", `${label}: `), document.createTextNode(String(value)));
    stats.append(item);
  };
  addStat("Language", result.transcript && result.transcript.language);
  addStat("Transcript", result.transcript && result.transcript.provider);
  addStat("Analysis", result.analysis && (result.analysis.model || result.analysis.provider));
  addStat("Uploader", video.uploader);
  headMain.append(stats);

  const actions = node("div", "result-actions");
  if (result.source_url) {
    const source = link("Source video", result.source_url, "btn btn-ghost btn-small");
    source.target = "_blank";
    source.rel = "noopener noreferrer";
    actions.append(source);
  }
  if (video.original_file) {
    actions.append(link("Original file", fileUrl(jobId, video.original_file), "btn btn-ghost btn-small", true));
  }
  head.append(headMain, actions);
  panel.append(head);

  if (result.transcript) {
    const links = node("div", "clip-actions");
    const label = node("span", "hint", "Transcript:");
    links.append(label);
    for (const [key, text] of [["txt_file", "TXT"], ["srt_file", "SRT"], ["vtt_file", "VTT"]]) {
      if (result.transcript[key]) {
        links.append(link(text, fileUrl(jobId, result.transcript[key]), "link-btn", true));
      }
    }
    panel.append(links);
  }

  if (!clips.length) {
    panel.append(emptyState("No clips were produced", "The analysis returned no usable moments for this video."));
    return;
  }

  for (const clip of clips) {
    panel.append(renderClip(jobId, clip, video));
  }
}

function renderClip(jobId, clip, video) {
  const card = node("article", "clip-card");

  const videoWrap = node("div", "clip-video");
  if (clip.file) {
    const player = document.createElement("video");
    player.controls = true;
    player.preload = "metadata";
    player.src = fileUrl(jobId, clip.file);
    if (video && video.thumbnail) player.poster = fileUrl(jobId, video.thumbnail);
    videoWrap.append(player);
  } else {
    videoWrap.append(emptyState("Clip file missing", "It may have been removed or discarded."));
  }

  const body = node("div", "clip-body");
  const top = node("div", "clip-top");
  top.append(node("h3", "clip-title", `${clip.rank}. ${clip.title || "Untitled clip"}`));
  const score = node("span", "score", String(clip.viral_score ?? "–"));
  score.append(node("small", "", "/100"));
  top.append(score);
  body.append(top);
  body.append(node("p", "clip-sub", clipSubtitle(clip)));

  const tags = node("div", "tags");
  for (const platform of clip.platforms || []) tags.append(node("span", "tag tag-platform", platform));
  if (clip.category) tags.append(node("span", "tag", clip.category));
  for (const keyword of (clip.keywords || []).slice(0, 8)) tags.append(node("span", "tag", keyword));
  if ((clip.adjustments || []).length) tags.append(node("span", "tag", `${clip.adjustments.length} timing fix${clip.adjustments.length === 1 ? "" : "es"}`));
  if (tags.childElementCount) body.append(tags);

  if (clip.hook) body.append(node("p", "clip-hook", `“${clip.hook}”`));
  if (clip.reason) body.append(node("p", "clip-reason", clip.reason));
  if (clip.summary) body.append(node("p", "clip-summary", clip.summary));

  const actions = node("div", "clip-actions");
  if (clip.file) actions.append(link("Download clip", fileUrl(jobId, clip.file), "link-btn accent", true));
  if (clip.srt_file) actions.append(link("SRT", fileUrl(jobId, clip.srt_file), "link-btn", true));
  if (clip.vtt_file) actions.append(link("VTT", fileUrl(jobId, clip.vtt_file), "link-btn", true));
  if (clip.txt_file) actions.append(link("Caption TXT", fileUrl(jobId, clip.txt_file), "link-btn", true));
  actions.append(button("Copy hook", "link-btn", () => copyText(clip.hook || "", "Hook copied.")));
  actions.append(button("Copy caption", "link-btn", () => copyText(copyCaption(clip), "Caption and hashtags copied.")));
  body.append(actions);

  card.append(videoWrap, body);
  return card;
}

/* ------------------------------------------------------------------ */
/* Logs and report                                                     */
/* ------------------------------------------------------------------ */

function logMessage(line) {
  const parts = [];
  for (const key of ["message", "detail", "error"]) {
    if (line[key] !== undefined && line[key] !== null && line[key] !== "") {
      parts.push(String(line[key]));
    }
  }
  if (line.duration_seconds !== undefined) parts.push(`${line.duration_seconds}s`);
  if (line.clip_rank !== undefined) parts.push(`clip ${line.clip_rank}`);
  if (!parts.length) {
    for (const key of Object.keys(line)) {
      if (["job_id", "timestamp", "stage", "level"].includes(key)) continue;
      let value;
      try {
        value = typeof line[key] === "object" ? JSON.stringify(line[key]) : String(line[key]);
      } catch {
        value = "[unserializable]";
      }
      parts.push(`${key}=${value}`);
    }
  }
  return parts.join(" · ");
}

function renderLogs(lines) {
  const panel = $("panel-logs");
  clear(panel);
  const wrap = node("div", "log-wrap");
  const head = node("div", "log-head");
  head.append(node("span", "", `${lines.length} event${lines.length === 1 ? "" : "s"}`));
  head.append(node("span", "", timezoneLabel()));
  wrap.append(head);
  if (!lines.length) {
    wrap.append(node("div", "log-empty", "No log entries yet. They appear as the job runs."));
    panel.append(wrap);
    return;
  }
  const list = node("div", "log-list");
  for (const line of lines) {
    const row = node("div", "log-row");
    row.append(node("span", "log-time", formatTimestamp(line.timestamp)));
    const level = node("span", `log-level ${line.level || "INFO"}`, line.level || "INFO");
    row.append(level);
    row.append(node("span", "log-stage", line.stage || ""));
    row.append(node("span", "log-message", logMessage(line)));
    list.append(row);
  }
  wrap.append(list);
  panel.append(wrap);
  list.scrollTop = list.scrollHeight;
}

async function loadLogs(initial) {
  stopLogTimer();
  const jobId = state.currentJobId;
  if (!jobId) return;
  try {
    const data = await api(`/api/jobs/${encodeURIComponent(jobId)}/log?tail=500`);
    if (state.currentJobId !== jobId || state.activeTab !== "logs") return;
    renderLogs(Array.isArray(data.lines) ? data.lines : []);
    if (jobIsActive(state.currentJob)) {
      state.logTimer = setTimeout(() => loadLogs(false), 2500);
    }
  } catch (failure) {
    if (failure.status === 401) return;
    if (!initial) return;
    const panel = $("panel-logs");
    clear(panel);
    panel.append(emptyState("Could not load logs", failure.message));
  }
}

async function loadReport() {
  const jobId = state.currentJobId;
  if (!jobId) return;
  const panel = $("panel-report");
  if (state.reportJobId === jobId && state.reportText !== null) {
    renderReport(jobId, state.reportText);
    return;
  }
  clear(panel);
  panel.append(emptyState("Loading report…", "Reading report.md."));
  try {
    const text = await api(`/api/jobs/${encodeURIComponent(jobId)}/report`);
    if (state.currentJobId !== jobId) return;
    state.reportJobId = jobId;
    state.reportText = text;
    renderReport(jobId, text);
  } catch (failure) {
    if (failure.status === 401) return;
    clear(panel);
    panel.append(emptyState("Report is not ready", failure.message));
  }
}

function renderReport(jobId, text) {
  const panel = $("panel-report");
  clear(panel);
  const toolbar = node("div", "report-toolbar");
  toolbar.append(node("span", "hint", "report.md"));
  const actions = node("div", "clip-actions");
  actions.append(link("Download", downloadUrl(jobId, "report"), "link-btn", true));
  actions.append(link("Open in new tab", withToken(`/api/jobs/${jobId}/report?inline=1`), "link-btn"));
  toolbar.append(actions);
  panel.append(toolbar);
  panel.append(node("pre", "report-pre", text));
}

/* ------------------------------------------------------------------ */
/* Create and retry                                                    */
/* ------------------------------------------------------------------ */

function formNumber(id) {
  return Number($(id).value);
}

const QUALITY_FORMATS = {
  best: null,
  "2160p": "bv*[height<=2160]+ba/b[height<=2160]",
  "1440p": "bv*[height<=1440]+ba/b[height<=1440]",
  "1080p": "bv*[height<=1080]+ba/b[height<=1080]",
  "720p": "bv*[height<=720]+ba/b[height<=720]",
  "480p": "bv*[height<=480]+ba/b[height<=480]",
};

function qualityFormat() {
  const quality = $("quality").value;
  return Object.prototype.hasOwnProperty.call(QUALITY_FORMATS, quality) ? QUALITY_FORMATS[quality] : null;
}

function readForm() {
  const platforms = [];
  if ($("platform-tiktok").checked) platforms.push("tiktok");
  if ($("platform-instagram-reels").checked) platforms.push("instagram_reels");
  if ($("platform-youtube-shorts").checked) platforms.push("youtube_shorts");
  return {
    url: $("url").value.trim(),
    options: {
      number_of_clips: formNumber("clips"),
      min_clip_duration: formNumber("min-duration"),
      max_clip_duration: formNumber("max-duration"),
      language: $("language").value,
      platforms,
      generate_subtitles: $("subtitles").value === "true",
      download_original: $("keep-original").value === "true",
      download_format: $("format").value.trim() || qualityFormat(),
    },
  };
}

function validateForm(payload) {
  const { url, options } = payload;
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return "Enter a valid video URL starting with http:// or https://.";
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return "Only http:// and https:// URLs are supported.";
  }
  if (!Number.isFinite(options.number_of_clips) || options.number_of_clips < 1 || options.number_of_clips > 10) {
    return "Clips must be a whole number between 1 and 10.";
  }
  if (!Number.isFinite(options.min_clip_duration) || options.min_clip_duration < 5) {
    return "Minimum clip duration must be at least 5 seconds.";
  }
  if (!Number.isFinite(options.max_clip_duration) || options.max_clip_duration > 600) {
    return "Maximum clip duration must be at most 600 seconds.";
  }
  if (options.min_clip_duration > options.max_clip_duration) {
    return "Minimum duration cannot be greater than maximum duration.";
  }
  if (!options.platforms.length) {
    return "Choose at least one platform.";
  }
  return "";
}

function showFormError(message) {
  const element = $("form-error");
  element.textContent = message;
  element.hidden = !message;
}

function saveForm(payload) {
  try {
    const quality = $("quality");
    const saved = quality ? { ...payload, quality: quality.value } : payload;
    localStorage.setItem("viral-form-v1", JSON.stringify(saved));
  } catch {
    /* storage may be unavailable */
  }
}

function restoreForm() {
  let saved;
  try {
    saved = JSON.parse(localStorage.getItem("viral-form-v1") || "null");
  } catch {
    saved = null;
  }
  if (!saved) return;
  const options = saved.options || {};
  const values = {
    clips: options.number_of_clips,
    "min-duration": options.min_clip_duration,
    "max-duration": options.max_clip_duration,
    language: options.language,
    format: options.download_format,
  };
  for (const [id, value] of Object.entries(values)) {
    if (value !== undefined && value !== null && $(id)) $(id).value = value;
  }
  if (Array.isArray(options.platforms)) {
    $("platform-tiktok").checked = options.platforms.includes("tiktok");
    $("platform-instagram-reels").checked = options.platforms.includes("instagram_reels");
    $("platform-youtube-shorts").checked = options.platforms.includes("youtube_shorts");
  }
  if (typeof options.generate_subtitles === "boolean") $("subtitles").value = String(options.generate_subtitles);
  if (typeof options.download_original === "boolean") $("keep-original").value = String(options.download_original);
  if ($("quality")) {
    if (saved.quality && Object.prototype.hasOwnProperty.call(QUALITY_FORMATS, saved.quality)) {
      $("quality").value = saved.quality;
    } else {
      const match = Object.entries(QUALITY_FORMATS).find(([, selector]) => selector && selector === options.download_format);
      if (match) $("quality").value = match[0];
    }
  }
}

async function createJob(event) {
  event.preventDefault();
  showFormError("");
  const payload = readForm();
  const problem = validateForm(payload);
  if (problem) {
    showFormError(problem);
    return;
  }
  const submit = $("submit-btn");
  submit.disabled = true;
  submit.textContent = "Creating…";
  try {
    const created = await api("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    saveForm({ url: payload.url, options: payload.options });
    $("url").value = "";
    await loadJobs();
    await selectJob(created.job_id);
  } catch (failure) {
    if (failure.status !== 401) showFormError(failure.message);
  } finally {
    submit.disabled = false;
    submit.textContent = "Create job";
  }
}

async function retryJob(jobId) {
  try {
    await api(`/api/jobs/${encodeURIComponent(jobId)}/retry`, { method: "POST" });
    await selectJob(jobId);
    await loadJobs();
  } catch (failure) {
    if (failure.status !== 401) showToast(failure.message, true);
  }
}

function confirmDelete(jobId) {
  const dialog = $("delete-dialog");
  const message = `Job ${jobId.slice(0, 8)} and all of its clips, logs, and downloads will be permanently removed.`;
  if (!dialog || typeof dialog.showModal !== "function") {
    return Promise.resolve(window.confirm(`${message}\n\nThis cannot be undone.`));
  }
  if (dialog.open) return Promise.resolve(false);
  $("delete-message").textContent = message;
  dialog.returnValue = "cancel";
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), { once: true });
  });
}

async function deleteJob(jobId) {
  if (!(await confirmDelete(jobId))) return;
  const wasCurrent = state.currentJobId === jobId;
  try {
    await api(`/api/jobs/${encodeURIComponent(jobId)}`, { method: "DELETE" });
    if (wasCurrent) {
      stopPolling();
      stopLogTimer();
      state.currentJobId = null;
      state.currentJob = null;
      state.result = null;
    }
    await loadJobs();
    if (wasCurrent) {
      if (state.jobs.length) {
        await selectJob(state.jobs[0].job_id);
      } else {
        showForm();
      }
    }
    showToast(`Deleted job ${jobId.slice(0, 8)}.`);
  } catch (failure) {
    if (failure.status !== 401) showToast(failure.message, true);
  }
}

/* ------------------------------------------------------------------ */
/* Wiring                                                              */
/* ------------------------------------------------------------------ */

function bindEvents() {
  $("job-form").addEventListener("submit", createJob);
  $("new-job-btn").addEventListener("click", showForm);
  $("refresh-btn").addEventListener("click", () => loadJobs());
  $("filter").addEventListener("change", renderJobs);
  $("token-btn").addEventListener("click", () => {
    $("token-input").value = "";
    $("token-error").hidden = true;
    showTokenGate();
  });
  $("token-form").addEventListener("submit", submitToken);

  for (const id of ["tab-clips", "tab-logs", "tab-report"]) {
    const name = id.replace("tab-", "");
    $(id).addEventListener("click", () => setActiveTab(name));
  }

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) loadJobs();
  });
}

function init() {
  const params = new URLSearchParams(location.search);
  const urlToken = params.get("token") || "";
  if (urlToken) {
    state.token = urlToken;
    try {
      sessionStorage.setItem("viral_token", urlToken);
    } catch {
      /* storage may be unavailable */
    }
    history.replaceState({}, "", location.pathname);
  } else {
    try {
      state.token = sessionStorage.getItem("viral_token") || "";
    } catch {
      state.token = "";
    }
  }
  bindEvents();
  restoreForm();
  setServerStatus("connecting");
  loadJobs({ initial: true });
  setInterval(() => loadJobs(), 5000);
}

document.addEventListener("DOMContentLoaded", init);
