"""
clipper/engine.py - Core processing engine (shared between CLI and GUI)
"""

import csv
import json
import shutil
import subprocess
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any, Callable

from clipper import __version__
from clipper.config import load_config, save_config
from clipper.paths import ROOT, TIMINGS_DIR, OUTPUT_DIR
from clipper.downloader import download_full_video, download_clip_direct
from clipper.ffmpeg import validate_video, clip_from_local
from clipper.lang import tr, set_language
from clipper.progress import ETAEstimator
from clipper.stats import print_stats
from clipper.timings import parse_timings, analyze_timings
from clipper.utils import safe_name, clip_filename, format_eta

# ======================================================================
# IO Helpers
# ======================================================================

def save_clips_csv(project_root: Path, clips: List[Dict[str, Any]]) -> None:
    """Save clips data to CSV."""
    with open(project_root / "clips.csv", 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=["index", "start", "end", "duration", "filename"])
        w.writeheader()
        w.writerows(clips)

def create_readme(project_root: Path, project_name: str, meta: Dict[str, Any],
                  total_clips: int, success: int, failed: int, elapsed: str) -> None:
    """Create README.txt in project folder."""
    lines = [
        f"Clipper {__version__}",
        "=" * 40,
        f"{tr('project')}: {project_name}",
        f"{tr('video')}: {meta.get('title', 'Unknown')}",
        f"{tr('channel')}: {meta.get('channel', 'Unknown')}",
        f"{tr('created')}: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "-" * 40,
        f"{tr('total_clips')}: {total_clips}",
        f"{tr('success')}: {success}",
        f"{tr('failed')}: {failed}",
        f"{tr('elapsed')}: {elapsed}",
        "=" * 40,
        "Clips are in the 'Clips' folder.",
        "For details see clips.csv, project.json, and logs.txt."
    ]
    (project_root / "README.txt").write_text("\n".join(lines), encoding='utf-8')

def save_project_json(project_root: Path, project_name: str, meta: Dict[str, Any],
                      clips_data: List[Dict], failed: int, total_clips: int,
                      elapsed: str, elapsed_seconds: float, processed: int,
                      thumbnail: Optional[str] = None, status: str = "completed") -> None:
    data = {
        "schema": 1,
        "version": __version__,
        "project": project_name,
        "video_title": meta.get("title", ""),
        "channel": meta.get("channel", ""),
        "video_url": meta.get("url", ""),
        "thumbnail": thumbnail,
        "clips_total": total_clips,
        "success": len(clips_data),
        "failed": failed,
        "processed": processed,
        "status": status,
        "last_clip": processed,
        "created": datetime.now().isoformat(),
        "elapsed": elapsed,
        "elapsed_seconds": elapsed_seconds
    }
    with open(project_root / "project.json", 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)

def save_state(project_root: Path, meta: Dict[str, Any], total_clips: int,
               clips_data: List[Dict], failed: List, processed: int, elapsed: float,
               thumbnail: Optional[str] = None) -> None:
    elapsed_str = format_eta(elapsed)
    save_project_json(
        project_root, project_root.name, meta, clips_data,
        len(failed), total_clips, elapsed_str, elapsed, processed,
        thumbnail, status="running"
    )

def finish_project(project_root: Path, meta: Dict[str, Any], clips_data: List[Dict],
                   failed: int, total_clips: int, processed: int, elapsed: float,
                   thumbnail: Optional[str] = None) -> None:
    elapsed_str = format_eta(elapsed)
    save_project_json(
        project_root, project_root.name, meta, clips_data,
        failed, total_clips, elapsed_str, elapsed, processed,
        thumbnail, status="completed"
    )

# ======================================================================
# Resume / Existing Project Helpers
# ======================================================================

def check_existing_project(project_name: str) -> Tuple[bool, Optional[Path], int, List[int]]:
    """Check if a project already exists and has been partially processed."""
    root = OUTPUT_DIR / project_name
    if not root.exists():
        return False, None, 0, []
    pj = root / "project.json"
    if not pj.exists():
        return False, None, 0, []
    try:
        with open(pj, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if data.get("status") in ("running", "interrupted"):
            processed = data.get("processed", 0)
            existing = []
            clips_dir = root / "Clips"
            if clips_dir.exists():
                for f in clips_dir.glob("*.mp4"):
                    try:
                        idx = int(f.stem.split('_')[0])
                        existing.append(idx)
                    except:
                        pass
            return True, root, processed, existing
    except:
        pass
    return False, None, 0, []

def ask_resume(project_name: str, processed: int, total: int) -> bool:
    """Ask user if they want to resume an existing project."""
    print(f"\n[!] {tr('resume_info').format(project_name)}")
    print(f"    {tr('resume_progress').format(processed, total)}")
    return input(tr("resume_prompt") + " ").strip().lower() == 'y'

def retry_failed_clips(url: str, project_root: Path, failed_clips: List[Tuple[int, float, float, str]],
                       config: Dict[str, Any], full_video_path: Optional[Path] = None) -> Tuple[List, List]:
    """Retry failed clips and return (still_failed, new_successful_clips)."""
    if not failed_clips:
        return [], []
    print(f"\n{tr('retrying').format(len(failed_clips))}")
    clips_dir = project_root / "Clips"
    retry_success = []
    still_failed = []
    use_local = full_video_path is not None and full_video_path.exists()

    for idx, start, end, fname in failed_clips:
        out_path = clips_dir / fname
        print(f"  Retrying {fname}...")
        if use_local:
            ok = clip_from_local(full_video_path, start, end, out_path, config)
        else:
            ok = download_clip_direct(url, start, end, out_path, config)
        if ok:
            retry_success.append({
                "index": idx,
                "start": start,
                "end": end,
                "duration": end - start,
                "filename": fname
            })
        else:
            still_failed.append((idx, start, end, fname))
    return still_failed, retry_success

# ======================================================================
# Core Engine
# ======================================================================

class ClipperEngine:
    """Core engine that handles all processing logic."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or load_config()
        self.running = False
        self.stop_requested = False
        self.progress_callback: Optional[Callable] = None
        self.log_callback: Optional[Callable] = None
        self._thumb_path: Optional[Path] = None

    def set_callbacks(self, progress_callback: Optional[Callable] = None,
                      log_callback: Optional[Callable] = None):
        self.progress_callback = progress_callback
        self.log_callback = log_callback

    def log(self, msg: str):
        if self.log_callback:
            self.log_callback(msg)
        else:
            print(msg)

    def update_progress(self, value: float, text: str = ""):
        if self.progress_callback:
            self.progress_callback(value, text)

    # ------------------------------------------------------------------
    # Core processing
    # ------------------------------------------------------------------

    def process(self, url: str, timings_file: str, project_name: Optional[str] = None) -> Dict[str, Any]:
        self.running = True
        self.stop_requested = False
        results = {
            "success": 0,
            "failed": 0,
            "total": 0,
            "elapsed": 0,
            "project_root": None,
            "project_name": "",
            "status": "running"
        }

        try:
            # Create directories
            for d in (ROOT, TIMINGS_DIR, OUTPUT_DIR):
                d.mkdir(parents=True, exist_ok=True)

            # Parse timings
            timings = parse_timings(Path(timings_file))
            if not timings:
                self.log(tr("no_valid_timings"))
                results["status"] = "error"
                return results

            # Get metadata
            self.log(tr("fetching_metadata"))
            meta, thumb = self._fetch_metadata_and_thumbnail(url)
            meta["url"] = url

            # Determine project name
            if not project_name:
                project_name = safe_name(f"{meta['channel']} - {meta['title']}")
            project_root = OUTPUT_DIR / project_name
            self._create_project_folders(project_root)

            self.log(tr("project_created").format(project_root))

            # Move thumbnail
            if thumb and self._thumb_path and self._thumb_path.exists():
                try:
                    shutil.move(str(self._thumb_path), str(project_root / thumb))
                    self.log(f"Thumbnail moved: {thumb}")
                except Exception as e:
                    self.log(f"Warning: Could not move thumbnail: {e}")
            elif thumb:
                thumb_files = sorted(Path(".").glob("thumbnail.*"))
                if thumb_files:
                    try:
                        shutil.move(str(thumb_files[0]), str(project_root / thumb_files[0].name))
                        self.log(f"Thumbnail moved (fallback): {thumb_files[0].name}")
                    except Exception as e:
                        self.log(f"Warning: Could not move thumbnail (fallback): {e}")
            else:
                self.log("No thumbnail downloaded.")

            # Save info
            (project_root / "info.json").write_text(
                json.dumps(meta, indent=2, ensure_ascii=False),
                encoding='utf-8'
            )

            # Resume logic
            clips_data, start_from = self._handle_resume(project_root, project_name, timings)

            # Determine download mode
            full_video_path = self._prepare_full_video(url, project_root, len(timings))

            # Process clips
            clips_data, failed, elapsed = self._process_clips(
                url, timings, project_root, meta, start_from, thumb, full_video_path
            )

            # Save results
            self._save_results(project_root, meta, clips_data, failed, len(timings), elapsed)

            # Save final state
            finish_project(project_root, meta, clips_data, len(failed), len(timings),
                           len(clips_data) + len(failed), elapsed, thumb)

            # Cleanup
            if full_video_path and full_video_path.exists() and not self.config.get("keep_full_video", False):
                full_video_path.unlink()

            # Return results
            results["success"] = len(clips_data)
            results["failed"] = len(failed)
            results["total"] = len(timings)
            results["elapsed"] = elapsed
            results["project_root"] = str(project_root)
            results["project_name"] = project_name
            results["status"] = "completed"

            # Retry failed if any and not stopped
            if failed and not self.stop_requested:
                self._retry_failed(url, project_root, failed, full_video_path, meta, timings, clips_data)

            return results

        except Exception as e:
            self.log(f"ERROR: {str(e)}")
            import traceback
            self.log(traceback.format_exc())
            results["status"] = "error"
            return results
        finally:
            self.running = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_metadata_and_thumbnail(self, url: str) -> Tuple[Dict, Optional[str]]:
        try:
            cmd = ["yt-dlp", "-J", "--no-warnings", url]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            data = json.loads(result.stdout)
            meta = {
                "title": data.get("title", "Unknown"),
                "channel": data.get("uploader", "Unknown"),
                "duration": data.get("duration", 0),
                "upload_date": data.get("upload_date", ""),
                "view_count": data.get("view_count", 0),
            }
            thumb_base = Path("thumbnail")
            subprocess.run(
                ["yt-dlp", "--write-thumbnail", "--skip-download",
                 "--no-playlist", "--no-warnings", "-o", str(thumb_base), url],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            thumb_files = sorted(Path(".").glob("thumbnail.*"))
            if thumb_files:
                thumb_name = thumb_files[0].name
                self._thumb_path = thumb_files[0]
                return meta, thumb_name
            self._thumb_path = None
            return meta, None
        except Exception as e:
            raise RuntimeError(f"Failed to fetch metadata: {e}")

    def _create_project_folders(self, project_root: Path):
        (project_root / "Clips").mkdir(parents=True, exist_ok=True)

    def _handle_resume(self, project_root: Path, project_name: str, timings: List) -> Tuple[List[Dict], int]:
        start_from = 0
        clips_data = []
        if self.config.get("resume", True):
            exists, existing_root, processed_count, existing_indices = check_existing_project(project_name)
            if exists and ask_resume(project_name, processed_count, len(timings)):
                start_from = max(existing_indices) if existing_indices else processed_count
                self.log(tr("resuming_from").format(start_from + 1))
                csv_path = existing_root / "clips.csv"
                if csv_path.exists():
                    with open(csv_path, 'r', encoding='utf-8') as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            clips_data.append(row)
        return clips_data, start_from

    def _prepare_full_video(self, url: str, project_root: Path, total_clips: int) -> Optional[Path]:
        download_mode = self.config.get("download_mode", "auto")
        if download_mode == "full" or (download_mode == "auto" and total_clips > 10):
            full_video_path = project_root / "full_video.mp4"
            self.log(tr("downloading_full_video").format(full_video_path))
            if download_full_video(url, full_video_path, self.config):
                self.log(tr("full_video_downloaded"))
                return full_video_path
            else:
                self.log(tr("full_video_fallback"))
                return None
        return None

    def _process_clips(self, url: str, timings: List, project_root: Path, meta: Dict,
                       start_from: int, thumb: Optional[str], full_video_path: Optional[Path]) -> Tuple[List[Dict], List, float]:
        total = len(timings)
        workers = self.config.get("parallel_workers", 4)
        tasks = [(idx, start, end, name) for idx, (start, end, name) in enumerate(timings, start=1) if idx > start_from]
        results = {}
        failed = []
        processed = 0
        start_time = time.time()
        eta_estimator = ETAEstimator()
        lock = threading.Lock()

        def process_one(task):
            if self.stop_requested:
                return None
            idx, start, end, name = task
            fname = clip_filename(idx, start, end)
            out_path = project_root / "Clips" / fname
            if self.config.get("skip_existing", True) and out_path.exists() and validate_video(out_path, self.config.get("min_file_size_kb", 50)):
                return idx, {"index": idx, "start": start, "end": end, "duration": end-start, "filename": fname}, None
            if full_video_path and full_video_path.exists():
                ok = clip_from_local(full_video_path, start, end, out_path, self.config)
            else:
                ok = download_clip_direct(url, start, end, out_path, self.config)
            if ok:
                return idx, {"index": idx, "start": start, "end": end, "duration": end-start, "filename": fname}, None
            else:
                return idx, None, (idx, start, end, fname)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(process_one, task) for task in tasks]
            for future in as_completed(futures):
                if self.stop_requested:
                    executor.shutdown(wait=False)
                    break
                result = future.result()
                if result is None:
                    continue
                idx, clip_data, fail = result
                if clip_data:
                    results[idx] = clip_data
                else:
                    if fail:
                        failed.append(fail)
                processed += 1
                progress_value = (processed / total) * 100
                self.update_progress(progress_value, f"Clip {idx} / {total}")

                if processed % 5 == 0:
                    with lock:
                        current_clips = list(results.values())
                        save_state(project_root, meta, total, current_clips, failed, processed,
                                   time.time() - start_time, thumb)

        clips_data = [results[i] for i in sorted(results.keys())]
        elapsed = time.time() - start_time
        return clips_data, failed, elapsed

    def _save_results(self, project_root: Path, meta: Dict, clips_data: List[Dict],
                      failed: List, total_clips: int, elapsed: float):
        save_clips_csv(project_root, clips_data)
        elapsed_str = format_eta(elapsed)
        create_readme(project_root, project_root.name, meta, total_clips, len(clips_data), len(failed), elapsed_str)
        with open(project_root / "completed.txt", 'w', encoding='utf-8') as f:
            f.write(f"Project: {project_root.name}\n")
            f.write(f"Video: {meta.get('title', 'Unknown')}\n")
            f.write("-" * 40 + "\n")
            f.write(f"Downloaded: {len(clips_data)}\n")
            f.write(f"Failed: {len(failed)}\n")
            f.write(f"Elapsed: {elapsed_str}\n")

    def _retry_failed(self, url: str, project_root: Path, failed: List,
                      full_video_path: Optional[Path], meta: Dict, timings: List,
                      clips_data: List[Dict]):
        self.log(f"\n{len(failed)} clips failed.")
        if input(tr("retry_failed") + " ").strip().lower() == 'y':
            still_failed, retry_success = retry_failed_clips(url, project_root, failed, self.config, full_video_path)
            if retry_success:
                clips_data.extend(retry_success)
                clips_data.sort(key=lambda x: int(x["index"]))
                save_clips_csv(project_root, clips_data)
                finish_project(project_root, meta, clips_data, len(still_failed), len(timings),
                               len(clips_data) + len(still_failed), time.time(), None)
                self.log(tr("retry_success").format(len(clips_data), len(still_failed)))

    def stop(self):
        self.stop_requested = True

# ======================================================================
# Compatibility wrapper
# ======================================================================

def run_engine(url: str, timings_file: str, project_name: Optional[str] = None,
               progress_callback: Optional[Callable] = None,
               log_callback: Optional[Callable] = None) -> Dict[str, Any]:
    engine = ClipperEngine()
    engine.set_callbacks(progress_callback, log_callback)
    return engine.process(url, timings_file, project_name)