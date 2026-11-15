"""Tkinter window for the viral clipping pipeline (optional UI)."""

from __future__ import annotations

import json
import os
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk
from typing import Any, Dict, Optional

from clipper.viral.config import ViralConfig, load_config
from clipper.viral.errors import PipelineError
from clipper.viral.jobs import JobManager
from clipper.viral.models import JobRecord


class ViralClipsWindow(tk.Toplevel):
    def __init__(self, master: tk.Misc, config: Optional[ViralConfig] = None) -> None:
        super().__init__(master)
        self.title("Viral Clips")
        self.geometry("980x720")
        self.minsize(820, 600)
        self.config_obj = config or load_config()
        self.manager = JobManager(self.config_obj)
        self.job_id: Optional[str] = None
        self._poll_id: Optional[str] = None
        self._running = False
        self._result: Optional[Dict[str, Any]] = None
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)

        url_frame = ttk.LabelFrame(outer, text="Long video URL", padding=6)
        url_frame.pack(fill=tk.X, pady=4)
        self.url_var = tk.StringVar()
        entry = ttk.Entry(url_frame, textvariable=self.url_var)
        entry.pack(fill=tk.X)

        options = ttk.LabelFrame(outer, text="Options", padding=6)
        options.pack(fill=tk.X, pady=4)
        row = ttk.Frame(options)
        row.pack(fill=tk.X)
        ttk.Label(row, text="Clips:").pack(side=tk.LEFT)
        self.clips_var = tk.IntVar(value=5)
        ttk.Spinbox(row, from_=1, to=self.config_obj.max_clips, textvariable=self.clips_var, width=4).pack(side=tk.LEFT, padx=(4, 12))
        ttk.Label(row, text="Min sec:").pack(side=tk.LEFT)
        self.min_var = tk.DoubleVar(value=self.config_obj.default_min_clip_duration)
        ttk.Spinbox(row, from_=5, to=600, textvariable=self.min_var, width=6).pack(side=tk.LEFT, padx=(4, 12))
        ttk.Label(row, text="Max sec:").pack(side=tk.LEFT)
        self.max_var = tk.DoubleVar(value=self.config_obj.default_max_clip_duration)
        ttk.Spinbox(row, from_=5, to=600, textvariable=self.max_var, width=6).pack(side=tk.LEFT, padx=(4, 12))
        ttk.Label(row, text="Language:").pack(side=tk.LEFT)
        self.language_var = tk.StringVar(value="auto")
        ttk.Combobox(row, textvariable=self.language_var, values=("auto", "ar", "en"), width=7, state="readonly").pack(side=tk.LEFT, padx=4)
        self.subtitles_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="Subtitles", variable=self.subtitles_var).pack(side=tk.LEFT, padx=(8, 0))
        self.original_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="Keep original", variable=self.original_var).pack(side=tk.LEFT, padx=8)

        progress_frame = ttk.LabelFrame(outer, text="Progress", padding=6)
        progress_frame.pack(fill=tk.X, pady=4)
        self.progress_var = tk.DoubleVar(value=0)
        ttk.Progressbar(progress_frame, variable=self.progress_var, maximum=100).pack(fill=tk.X)
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(progress_frame, textvariable=self.status_var).pack(anchor=tk.W, pady=(4, 0))

        results_frame = ttk.LabelFrame(outer, text="Clips", padding=6)
        results_frame.pack(fill=tk.BOTH, expand=True, pady=4)
        columns = ("rank", "title", "score", "duration", "category", "file")
        self.tree = ttk.Treeview(results_frame, columns=columns, show="headings", height=10)
        for column, heading, width in (
            ("rank", "#", 42), ("title", "Title", 310), ("score", "Score", 62),
            ("duration", "Length", 70), ("category", "Category", 150), ("file", "File", 250),
        ):
            self.tree.heading(column, text=heading)
            self.tree.column(column, width=width, anchor=tk.W)
        self.tree.pack(fill=tk.BOTH, expand=True)
        self.tree.bind("<Double-1>", self._open_selected_clip)

        log_frame = ttk.LabelFrame(outer, text="Log", padding=6)
        log_frame.pack(fill=tk.BOTH, expand=False, pady=4)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=7, state=tk.DISABLED, wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True)

        buttons = ttk.Frame(outer)
        buttons.pack(fill=tk.X, pady=4)
        self.start_btn = ttk.Button(buttons, text="Start", command=self._start)
        self.start_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.retry_btn = ttk.Button(buttons, text="Retry", command=self._retry, state=tk.DISABLED)
        self.retry_btn.pack(side=tk.LEFT, padx=6)
        ttk.Button(buttons, text="Open job folder", command=self._open_folder).pack(side=tk.LEFT, padx=6)
        ttk.Button(buttons, text="Open report", command=self._open_report).pack(side=tk.LEFT, padx=6)
        ttk.Button(buttons, text="Close", command=self._close).pack(side=tk.RIGHT)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _start(self) -> None:
        if self._running:
            return
        url = self.url_var.get().strip()
        if not url:
            messagebox.showerror("Viral Clips", "Enter a video URL.", parent=self)
            return
        options = {
            "number_of_clips": self.clips_var.get(),
            "min_clip_duration": self.min_var.get(),
            "max_clip_duration": self.max_var.get(),
            "language": self.language_var.get(),
            "generate_subtitles": self.subtitles_var.get(),
            "download_original": self.original_var.get(),
        }
        try:
            record = self.manager.create_job(url, options)
        except PipelineError as exc:
            messagebox.showerror("Viral Clips", f"{exc.code}: {exc.message}", parent=self)
            return
        self.job_id = record.job_id
        self._result = None
        self.tree.delete(*self.tree.get_children())
        self._running = True
        self.start_btn.config(state=tk.DISABLED)
        self.retry_btn.config(state=tk.DISABLED)
        self._log(f"Job {record.job_id} queued.")
        self._poll()

    def _retry(self) -> None:
        if not self.job_id or self._running:
            return
        try:
            record = self.manager.retry_job(self.job_id)
        except PipelineError as exc:
            messagebox.showerror("Viral Clips", f"{exc.code}: {exc.message}", parent=self)
            return
        self._running = True
        self.start_btn.config(state=tk.DISABLED)
        self.retry_btn.config(state=tk.DISABLED)
        self._log(f"Retry queued (attempt {record.attempts}).")
        self._poll()

    def _poll(self) -> None:
        if not self.job_id:
            return
        try:
            record = self.manager.get_job(self.job_id)
        except PipelineError as exc:
            self._log(f"ERROR: {exc.message}")
            self._finish_running()
            return
        self.progress_var.set(record.progress)
        self.status_var.set(f"{record.status} · {record.current_step} · {record.progress:.0f}%")
        if record.status in ("completed", "failed"):
            if record.status == "completed":
                self._load_result(record)
                self._log("Completed.")
            else:
                error = record.error or {}
                self._log(f"FAILED: {error.get('code', '')}: {error.get('message', '')}")
                self.retry_btn.config(state=tk.NORMAL)
            self._finish_running()
            return
        self._poll_id = self.after(1000, self._poll)

    def _finish_running(self) -> None:
        self._running = False
        self.start_btn.config(state=tk.NORMAL)
        if self._poll_id:
            try:
                self.after_cancel(self._poll_id)
            except tk.TclError:
                pass
            self._poll_id = None

    def _load_result(self, record: JobRecord) -> None:
        if not record.result_path:
            return
        try:
            self._result = json.loads(Path(record.result_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self._log(f"Could not read results: {exc}")
            return
        self.tree.delete(*self.tree.get_children())
        for clip in self._result.get("clips", []):
            self.tree.insert(
                "",
                tk.END,
                values=(
                    clip.get("rank", ""),
                    clip.get("title", ""),
                    clip.get("viral_score", ""),
                    f"{clip.get('duration', 0):.1f}s",
                    clip.get("category", ""),
                    clip.get("file", ""),
                ),
            )
        self.status_var.set(
            f"Completed · {len(self._result.get('clips', []))} clips · "
            f"{self._result.get('video', {}).get('title', '')}"
        )

    def _selected_file(self) -> Optional[Path]:
        selection = self.tree.selection()
        if not selection or not self.job_id:
            return None
        values = self.tree.item(selection[0], "values")
        if len(values) < 6:
            return None
        try:
            record = self.manager.get_job(self.job_id)
        except PipelineError:
            return None
        return Path(record.output_path) / str(values[5])

    def _open_selected_clip(self, _event: Any = None) -> None:
        path = self._selected_file()
        if path and path.exists():
            self._open_path(path)

    def _open_folder(self) -> None:
        if not self.job_id:
            return
        try:
            record = self.manager.get_job(self.job_id)
        except PipelineError:
            return
        self._open_path(Path(record.output_path))

    def _open_report(self) -> None:
        if not self.job_id:
            return
        try:
            record = self.manager.get_job(self.job_id)
        except PipelineError:
            return
        if record.report_path and Path(record.report_path).exists():
            self._open_path(Path(record.report_path))

    def _open_path(self, path: Path) -> None:
        try:
            if os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif os.name == "posix":
                os.system(f'xdg-open "{path}"')
            else:
                os.system(f'open "{path}"')
        except OSError as exc:
            messagebox.showerror("Viral Clips", str(exc), parent=self)

    def _log(self, message: str) -> None:
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _close(self) -> None:
        if self._poll_id:
            try:
                self.after_cancel(self._poll_id)
            except tk.TclError:
                pass
            self._poll_id = None
        self.manager.shutdown(wait=False)
        self.destroy()
