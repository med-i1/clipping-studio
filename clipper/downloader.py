"""Download full video or clips directly using yt-dlp"""

import json
import subprocess
import time
from pathlib import Path
from typing import Optional, Dict, Any, List

from clipper.ffmpeg import validate_video, convert_to_mp4
from clipper.utils import to_timestamp, clip_filename
from clipper.lang import tr

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv", ".ts"}

def download_full_video(url: str, output_path: Path, config: Dict[str, Any]) -> bool:
    """Download full video once, forcing MP4 container."""
    if output_path.exists() and validate_video(output_path, config.get("min_file_size_kb", 50)):
        return True

    # Use a temporary name without extension to avoid yt-dlp adding its own
    temp_output = output_path.parent / f"{output_path.stem}_full_temp"

    cmd = [
        "yt-dlp",
        "-f", config.get("format", "bv*+ba/b"),
        "-o", str(temp_output),  # No extension, yt-dlp will choose
        "--merge-output-format", "mp4",   # Force MP4 container
        "--remux-video", "mp4",           # Fallback remux to MP4
        "--no-playlist",
        "--no-warnings",
        "--no-part"
    ]
    if config.get("force_keyframes", False):
        cmd.append("--force-keyframes-at-cuts")
    cmd.append(url)

    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

        # Find the downloaded file (yt-dlp may add an extension)
        downloaded_files = list(temp_output.parent.glob(f"{temp_output.stem}.*"))
        if not downloaded_files:
            return False

        # If the downloaded file is already MP4, just rename
        raw_file = downloaded_files[0]
        if raw_file.suffix.lower() == ".mp4":
            raw_file.rename(output_path)
            return validate_video(output_path, config.get("min_file_size_kb", 50))

        # Otherwise convert to MP4 using our existing converter
        if convert_to_mp4(raw_file, output_path, config):
            raw_file.unlink(missing_ok=True)
            return True

        return False

    except subprocess.CalledProcessError as e:
        print(f"Failed to download full video: {e.stderr}")
        return False

def download_raw_clip(url: str, start: float, end: float, temp_path: Path,
                      config: Dict[str, Any]) -> Optional[Path]:
    """Download a single clip directly using --download-sections."""
    section = f"*{to_timestamp(start)}-{to_timestamp(end)}"
    temp_pattern = str(temp_path.parent / f"{temp_path.stem}.%(ext)s")
    cmd = [
        "yt-dlp",
        "-f", config.get("format", "bv*+ba/b"),
        "--download-sections", section,
        "-o", temp_pattern,
        "--no-playlist",
        "--no-warnings",
        "--no-part"
    ]
    if config.get("force_keyframes", False):
        cmd.append("--force-keyframes-at-cuts")
    cmd.append(url)

    retries = config.get("retry", 3)
    for attempt in range(1, retries + 1):
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            files = [f for f in temp_path.parent.glob(f"{temp_path.stem}.*") if f.suffix.lower() in VIDEO_EXTENSIONS]
            if files:
                raw_file = sorted(files)[0]
                if validate_video(raw_file, config.get("min_file_size_kb", 50)):
                    return raw_file
            return None
        except subprocess.CalledProcessError:
            if attempt < retries:
                time.sleep(2 ** attempt)
    return None

def download_clip_direct(url: str, start: float, end: float, output_path: Path,
                         config: Dict[str, Any]) -> bool:
    """Download and convert a single clip directly."""
    temp_dir = output_path.parent / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_pattern = temp_dir / f"{output_path.stem}_raw"

    try:
        raw_path = download_raw_clip(url, start, end, temp_pattern, config)
        if raw_path is None or not raw_path.exists():
            return False

        if not convert_to_mp4(raw_path, output_path, config):
            return False

        return True

    finally:
        for f in temp_dir.glob(f"{output_path.stem}_raw*"):
            try: f.unlink()
            except: pass
        try: temp_dir.rmdir()
        except: pass