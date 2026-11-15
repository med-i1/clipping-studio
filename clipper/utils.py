"""Utility functions: safe names, time formatting, etc."""

import re
from pathlib import Path
from typing import Optional

def safe_name(name: str, max_len: int = 120) -> str:
    """Remove invalid characters and trim to max_len."""
    cleaned = re.sub(r'[\\/*?:"<>|]', '', name)
    if len(cleaned) <= max_len:
        return cleaned.rstrip()
    trimmed = cleaned[:max_len]
    if ' ' in trimmed:
        trimmed = trimmed[:trimmed.rfind(' ')]
    return trimmed.rstrip() + "..."

def fmt_time(sec: float) -> str:
    """Convert seconds to MM-SS or HH-MM-SS for filenames."""
    sec = int(sec)
    if sec < 3600:
        m, s = divmod(sec, 60)
        return f"{m:02d}-{s:02d}"
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}-{m:02d}-{s:02d}"

def clip_filename(index: int, start: float, end: float) -> str:
    """Generate filename like 001_30s_01-12_01-42.mp4"""
    dur = int(end - start)
    return f"{index:03d}_{dur}s_{fmt_time(start)}_{fmt_time(end)}.mp4"

def to_timestamp(sec: float) -> str:
    """Convert seconds to HH:MM:SS.mmm for yt-dlp sections."""
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"

def format_eta(seconds: float) -> str:
    if seconds < 0:
        return "???   "
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"