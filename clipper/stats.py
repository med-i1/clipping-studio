"""Statistics and summary"""

from typing import List, Dict, Any
from pathlib import Path

from clipper.utils import format_eta
from clipper.lang import tr

def print_stats(clips_data: List[Dict[str, Any]], failed: int, total: int, elapsed: float,
                project_root: Path) -> None:
    """Print detailed statistics."""
    success = len(clips_data)
    total_size = 0
    durations = []
    for clip in clips_data:
        dur = clip.get("duration", 0)
        durations.append(dur)
        # Actual file size
        fname = clip.get("filename")
        if fname:
            file_path = project_root / "Clips" / fname
            if file_path.exists():
                total_size += file_path.stat().st_size
    avg_dur = sum(durations) / len(durations) if durations else 0
    speed = success / elapsed if elapsed > 0 else 0

    print("\n--- " + tr("stats") + " ---")
    print(f"  {tr('total_clips')}: {total}")
    print(f"  {tr('success')}: {success}")
    print(f"  {tr('failed')}: {failed}")
    print(f"  {tr('elapsed')}: {format_eta(elapsed)}")
    print(f"  {tr('processing_speed')}: {speed:.2f} {tr('clips_per_sec')}")
    if durations:
        print(f"  {tr('shortest_clip')}: {min(durations):.1f}s")
        print(f"  {tr('longest_clip')}: {max(durations):.1f}s")
    if total_size > 0:
        size_mb = total_size / (1024 * 1024)
        if size_mb > 1024:
            print(f"  {tr('total_size')}: {size_mb/1024:.2f} GB")
        else:
            print(f"  {tr('total_size')}: {size_mb:.2f} MB")