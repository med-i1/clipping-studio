#!/usr/bin/env python3
"""
clipper/gui.py - Graphical User Interface
"""

import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from pathlib import Path
import json
import os

from clipper import __version__
from clipper.config import load_config, save_config
from clipper.paths import CONFIG_FILE, OUTPUT_DIR
from clipper.lang import tr, set_language, create_default_language_files
from clipper.engine import ClipperEngine
from clipper.utils import format_eta


class ClipperGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"Clipper {__version__}")
        self.root.geometry("900x700")
        self.root.minsize(800, 600)

        # Load config and language
        self.config = load_config()
        self.lang = self.config.get("language", "en")
        set_language(self.lang)
        create_default_language_files()

        self.engine = None
        self.worker_thread = None
        self.running = False
        self.stop_requested = False

        self.create_menu()
        self.create_widgets()

    # ------------------------------------------------------------------
    # Menu
    # ------------------------------------------------------------------

    def create_menu(self):
        menubar = tk.Menu(self.root)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label=tr("open_project"), command=self.open_project)
        file_menu.add_separator()
        file_menu.add_command(label=tr("exit"), command=self.root.quit)
        menubar.add_cascade(label=tr("file"), menu=file_menu)

        settings_menu = tk.Menu(menubar, tearoff=0)
        settings_menu.add_command(label=tr("language"), command=self.change_language)
        settings_menu.add_command(label=tr("config"), command=self.edit_config)
        menubar.add_cascade(label=tr("settings"), menu=settings_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label=tr("about"), command=self.show_about)
        menubar.add_cascade(label=tr("help"), menu=help_menu)

        tools_menu = tk.Menu(menubar, tearoff=0)
        tools_menu.add_command(label="Viral Clips...", command=self.open_viral_clips)
        menubar.add_cascade(label="Tools", menu=tools_menu)

        self.root.config(menu=menubar)

    # ------------------------------------------------------------------
    # Context menu for text fields
    # ------------------------------------------------------------------

    def _create_context_menu(self, widget):
        """Create right-click context menu with text operations."""
        menu = tk.Menu(widget, tearoff=0)
        menu.add_command(label=tr("cut"), command=lambda: widget.event_generate("<<Cut>>"))
        menu.add_command(label=tr("copy"), command=lambda: widget.event_generate("<<Copy>>"))
        menu.add_command(label=tr("paste"), command=lambda: widget.event_generate("<<Paste>>"))
        menu.add_separator()
        menu.add_command(label=tr("select_all"), command=lambda: widget.select_range(0, tk.END))

        def show_menu(event):
            menu.tk_popup(event.x_root, event.y_root)

        widget.bind("<Button-3>", show_menu)
        return menu

    # ------------------------------------------------------------------
    # Widgets
    # ------------------------------------------------------------------

    def create_widgets(self):
        main_frame = ttk.Frame(self.root, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # URL Entry
        url_frame = ttk.LabelFrame(main_frame, text=tr("video_input"), padding=5)
        url_frame.pack(fill=tk.X, pady=5)

        self.url_entry = ttk.Entry(url_frame, width=70)
        self.url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        self.url_entry.insert(0, "https://www.youtube.com/watch?v=...")

        # Enable paste via right-click and Ctrl+V
        self._create_context_menu(self.url_entry)
        self.url_entry.bind("<Control-v>", lambda e: self.url_entry.event_generate("<<Paste>>"))

        # Paste button
        paste_btn = ttk.Button(url_frame, text="📋 Paste", command=lambda: self.url_entry.event_generate("<<Paste>>"))
        paste_btn.pack(side=tk.RIGHT, padx=5)

        self.playlist_var = tk.BooleanVar(value=False)
        self.playlist_check = ttk.Checkbutton(
            url_frame, text=tr("playlist"), variable=self.playlist_var
        )
        self.playlist_check.pack(side=tk.RIGHT, padx=5)

        # Timings File
        timings_frame = ttk.LabelFrame(main_frame, text=tr("timings_file"), padding=5)
        timings_frame.pack(fill=tk.X, pady=5)

        self.timings_path = tk.StringVar()
        self.timings_entry = ttk.Entry(timings_frame, textvariable=self.timings_path)
        self.timings_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        self._create_context_menu(self.timings_entry)

        ttk.Button(timings_frame, text=tr("browse"), command=self.browse_timings).pack(side=tk.RIGHT)

        # Project Name
        project_frame = ttk.LabelFrame(main_frame, text=tr("project_name"), padding=5)
        project_frame.pack(fill=tk.X, pady=5)

        self.project_name_var = tk.StringVar()
        self.project_name_entry = ttk.Entry(project_frame, textvariable=self.project_name_var)
        self.project_name_entry.pack(fill=tk.X)
        self._create_context_menu(self.project_name_entry)

        # Settings
        settings_frame = ttk.LabelFrame(main_frame, text=tr("settings"), padding=5)
        settings_frame.pack(fill=tk.X, pady=5)

        row1 = ttk.Frame(settings_frame)
        row1.pack(fill=tk.X, pady=2)

        ttk.Label(row1, text=tr("output_mode") + ":").pack(side=tk.LEFT)
        self.output_mode_var = tk.StringVar(value=self.config.get("output_mode", "fast"))
        mode_menu = ttk.Combobox(row1, textvariable=self.output_mode_var, values=["fast", "compatible"], width=10)
        mode_menu.pack(side=tk.LEFT, padx=5)

        ttk.Label(row1, text=tr("workers") + ":").pack(side=tk.LEFT, padx=(10, 0))
        self.workers_var = tk.IntVar(value=self.config.get("parallel_workers", 4))
        ttk.Spinbox(row1, from_=1, to=16, textvariable=self.workers_var, width=5).pack(side=tk.LEFT, padx=5)

        ttk.Label(row1, text=tr("download_mode") + ":").pack(side=tk.LEFT, padx=(10, 0))
        self.download_mode_var = tk.StringVar(value=self.config.get("download_mode", "auto"))
        dl_mode_menu = ttk.Combobox(row1, textvariable=self.download_mode_var, values=["clip", "full", "auto"], width=8)
        dl_mode_menu.pack(side=tk.LEFT, padx=5)

        row2 = ttk.Frame(settings_frame)
        row2.pack(fill=tk.X, pady=2)

        self.skip_var = tk.BooleanVar(value=self.config.get("skip_existing", True))
        ttk.Checkbutton(row2, text=tr("skip_existing"), variable=self.skip_var).pack(side=tk.LEFT, padx=5)

        self.keyframes_var = tk.BooleanVar(value=self.config.get("force_keyframes", False))
        ttk.Checkbutton(row2, text=tr("force_keyframes"), variable=self.keyframes_var).pack(side=tk.LEFT, padx=5)

        self.keep_full_var = tk.BooleanVar(value=self.config.get("keep_full_video", False))
        ttk.Checkbutton(row2, text=tr("keep_full_video"), variable=self.keep_full_var).pack(side=tk.LEFT, padx=5)

        self.resume_var = tk.BooleanVar(value=self.config.get("resume", True))
        ttk.Checkbutton(row2, text=tr("resume"), variable=self.resume_var).pack(side=tk.LEFT, padx=5)

        # Progress Bar
        progress_frame = ttk.LabelFrame(main_frame, text=tr("progress"), padding=5)
        progress_frame.pack(fill=tk.X, pady=5)

        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress_bar = ttk.Progressbar(progress_frame, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill=tk.X)

        self.status_label = ttk.Label(progress_frame, text=tr("ready"))
        self.status_label.pack(anchor=tk.W, pady=2)

        # Log output
        log_frame = ttk.LabelFrame(main_frame, text=tr("log"), padding=5)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        self.log_text = scrolledtext.ScrolledText(log_frame, height=10, wrap=tk.WORD, state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # Buttons
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill=tk.X, pady=5)

        self.start_btn = ttk.Button(button_frame, text=tr("start"), command=self.start_processing)
        self.start_btn.pack(side=tk.LEFT, padx=5)

        self.stop_btn = ttk.Button(button_frame, text=tr("stop"), command=self.stop_processing, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=5)

        self.open_output_btn = ttk.Button(button_frame, text=tr("open_output"), command=self.open_output_folder)
        self.open_output_btn.pack(side=tk.LEFT, padx=5)

    # ------------------------------------------------------------------
    # Event Handlers
    # ------------------------------------------------------------------

    def browse_timings(self):
        file_path = filedialog.askopenfilename(
            title=tr("select_timings_file"),
            filetypes=[("Text files", "*.txt"), ("CSV files", "*.csv"), ("All files", "*.*")],
            initialdir=Path("Clipping/timings")
        )
        if file_path:
            self.timings_path.set(file_path)

    def open_project(self):
        project_dir = filedialog.askdirectory(
            title=tr("select_project_folder"),
            initialdir=OUTPUT_DIR
        )
        if project_dir:
            project_path = Path(project_dir)
            if (project_path / "project.json").exists():
                with open(project_path / "project.json", 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.project_name_var.set(data.get("project", project_path.name))
                self.timings_path.set(str(project_path / "timings.txt"))
                self.url_entry.delete(0, tk.END)
                self.url_entry.insert(0, data.get("video_url", ""))
            else:
                messagebox.showerror(tr("error"), tr("not_a_clipper_project"))

    def change_language(self):
        new_lang = "ar" if self.lang == "en" else "en"
        self.lang = new_lang
        self.config["language"] = new_lang
        save_config(self.config)
        set_language(new_lang)
        # Update window title
        self.root.title(f"Clipper {__version__}")
        messagebox.showinfo(tr("info"), tr("language_selected").format(tr("lang_" + new_lang)))

    def edit_config(self):
        os.system(f"xdg-open {CONFIG_FILE}" if os.name == "posix" else f"start {CONFIG_FILE}")

    def show_about(self):
        messagebox.showinfo(
            tr("about"),
            f"Clipper {__version__}\n\n"
            "YouTube Video Clipper\n"
            "Parallel processing, resume, playlist support\n"
            "Built with Python, yt-dlp, FFmpeg\n\n"
            "© 2024 Clipper Team"
        )

    def open_output_folder(self):
        if OUTPUT_DIR.exists():
            os.system(f"xdg-open {OUTPUT_DIR}" if os.name == "posix" else f"start {OUTPUT_DIR}")

    def open_viral_clips(self):
        """Open the optional viral clipping workflow without affecting the main engine."""
        from clipper.viral.gui import ViralClipsWindow

        ViralClipsWindow(self.root)

    # ------------------------------------------------------------------
    # Processing
    # ------------------------------------------------------------------

    def start_processing(self):
        if self.running:
            return

        url = self.url_entry.get().strip()
        if not url or url == "https://www.youtube.com/watch?v=...":
            messagebox.showerror(tr("error"), tr("enter_valid_url"))
            return

        timings_file = self.timings_path.get().strip()
        if not timings_file or not Path(timings_file).exists():
            messagebox.showerror(tr("error"), tr("timings_file_not_found"))
            return

        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.running = True
        self.stop_requested = False

        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete(1.0, tk.END)
        self.log_text.config(state=tk.DISABLED)

        # Update config from GUI
        self.config["output_mode"] = self.output_mode_var.get()
        self.config["parallel_workers"] = self.workers_var.get()
        self.config["download_mode"] = self.download_mode_var.get()
        self.config["skip_existing"] = self.skip_var.get()
        self.config["force_keyframes"] = self.keyframes_var.get()
        self.config["keep_full_video"] = self.keep_full_var.get()
        self.config["resume"] = self.resume_var.get()
        save_config(self.config)

        self.engine = ClipperEngine(self.config)
        self.engine.set_callbacks(
            progress_callback=self.update_progress,
            log_callback=self.log_message
        )

        self.worker_thread = threading.Thread(
            target=self.run_engine,
            args=(url, timings_file)
        )
        self.worker_thread.daemon = True
        self.worker_thread.start()

    def run_engine(self, url: str, timings_file: str):
        try:
            results = self.engine.process(url, timings_file)
            self.root.after(0, lambda: self.on_engine_finished(results))
        except Exception as e:
            self.root.after(0, lambda: self.log_message(f"ERROR: {str(e)}"))
            import traceback
            self.root.after(0, lambda: self.log_message(traceback.format_exc()))
            self.root.after(0, self.on_engine_failed)

    def on_engine_failed(self):
        self.stop_btn.config(state=tk.DISABLED)
        self.start_btn.config(state=tk.NORMAL)
        self.running = False

    def on_engine_finished(self, results):
        self.stop_btn.config(state=tk.DISABLED)
        self.start_btn.config(state=tk.NORMAL)
        self.progress_var.set(100)

        if results["status"] == "completed":
            self.log_message(tr("processing_complete"))
            self.log_message(f"{tr('success')}: {results['success']}")
            self.log_message(f"{tr('failed')}: {results['failed']}")
            self.log_message(f"{tr('elapsed')}: {format_eta(results['elapsed'])}")
            self.log_message(tr("folder_icon").format(results['project_root']))
        else:
            self.log_message(tr("processing_interrupted"))

        self.running = False

    def stop_processing(self):
        if self.engine:
            self.engine.stop()
            self.log_message(tr("stop_requested"))
            self.stop_btn.config(state=tk.DISABLED)

    def update_progress(self, value: float, text: str):
        self.root.after(0, lambda: self.progress_var.set(value))
        self.root.after(0, lambda: self.status_label.config(text=text))

    def log_message(self, msg: str):
        self.root.after(0, lambda: self._append_log(msg))

    def _append_log(self, msg: str):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self):
        self.root.mainloop()


def main():
    create_default_language_files()
    root = tk.Tk()
    app = ClipperGUI(root)
    app.run()


if __name__ == "__main__":
    main()
