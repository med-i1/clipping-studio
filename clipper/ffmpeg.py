"""FFmpeg operations: validation, clipping, conversion"""

import subprocess
import time
from pathlib import Path
from typing import Optional, Dict, Any

def validate_video(file_path: Path, min_size_kb: int = 50) -> bool:
    """Check if file exists, has size > min_size_kb, and contains video stream."""
    if not file_path.exists() or file_path.stat().st_size < min_size_kb * 1024:
        return False
    try:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_type",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(file_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        return "video" in result.stdout
    except Exception:
        return False

def get_video_duration(file_path: Path) -> Optional[float]:
    try:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(file_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.stdout.strip():
            return float(result.stdout.strip())
        return None
    except Exception:
        return None

def get_video_codec(file_path: Path) -> Optional[str]:
    try:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(file_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.stdout.strip():
            return result.stdout.strip()
        return None
    except Exception:
        return None

def convert_to_mp4(input_path: Path, output_path: Path, config: Dict[str, Any]) -> bool:
    """Convert any video to MP4. Tries copy, falls back to re-encode if needed."""
    if output_path.exists() and validate_video(output_path, config.get("min_file_size_kb", 50)):
        return True

    output_mode = config.get("output_mode", "fast")
    retries = config.get("retry", 3)

    # Check if we can use copy mode (fast) and if codec is compatible
    use_copy = (output_mode == "fast")
    if use_copy:
        codec = get_video_codec(input_path)
        # Only allow copy for compatible codecs (h264, h265, mpeg4, etc.)
        if codec and codec.lower() in ("vp9", "av1", "vp8", "theora", "wmv3"):
            use_copy = False

    if use_copy:
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", str(input_path),
            "-map", "0",
            "-c", "copy",
            "-y", str(output_path)
        ]
    else:
        preset = config.get("ffmpeg_preset", "veryfast")
        crf = config.get("ffmpeg_crf", 20)
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", str(input_path),
            "-map", "0",
            "-c:v", "libx264",
            "-preset", preset,
            "-crf", str(crf),
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "128k",
            "-c:s", "copy",
            "-movflags", "+faststart",
            "-y", str(output_path)
        ]

    for attempt in range(1, retries + 1):
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            if validate_video(output_path, config.get("min_file_size_kb", 50)):
                return True
            if output_path.exists():
                output_path.unlink()
            if attempt < retries:
                time.sleep(2 ** attempt)
        except subprocess.CalledProcessError:
            if attempt < retries:
                time.sleep(2 ** attempt)
    return False

def clip_from_local(input_path: Path, start: float, end: float, output_path: Path,
                    config: Dict[str, Any]) -> bool:
    """Clip video from local file using FFmpeg."""
    if output_path.exists() and validate_video(output_path, config.get("min_file_size_kb", 50)):
        return True

    duration = end - start
    output_mode = config.get("output_mode", "fast")
    retries = config.get("retry", 3)

    use_copy = (output_mode == "fast")
    if use_copy:
        codec = get_video_codec(input_path)
        if codec and codec.lower() in ("vp9", "av1", "vp8", "theora", "wmv3"):
            use_copy = False

    if use_copy:
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", str(start),
            "-i", str(input_path),
            "-t", str(duration),
            "-map", "0",
            "-c", "copy",
            "-avoid_negative_ts", "1",
            "-y", str(output_path)
        ]
    else:
        preset = config.get("ffmpeg_preset", "veryfast")
        crf = config.get("ffmpeg_crf", 20)
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", str(start),
            "-i", str(input_path),
            "-t", str(duration),
            "-map", "0",
            "-c:v", "libx264",
            "-preset", preset,
            "-crf", str(crf),
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "128k",
            "-c:s", "copy",
            "-movflags", "+faststart",
            "-y", str(output_path)
        ]

    for attempt in range(1, retries + 1):
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            if validate_video(output_path, config.get("min_file_size_kb", 50)):
                return True
            if output_path.exists():
                output_path.unlink()
            if attempt < retries:
                time.sleep(2 ** attempt)
        except subprocess.CalledProcessError:
            if attempt < retries:
                time.sleep(2 ** attempt)
    return False
