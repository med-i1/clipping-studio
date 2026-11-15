"""Timing file parsing and analysis"""

from pathlib import Path
from typing import List, Tuple, Optional

from clipper.lang import tr

def parse_time(t: str) -> Optional[float]:
    """Convert time string to seconds."""
    t = t.strip()
    try:
        return float(t)
    except ValueError:
        pass
    parts = t.split(':')
    if len(parts) == 2:
        try:
            return int(parts[0]) * 60 + float(parts[1])
        except ValueError:
            return None
    if len(parts) == 3:
        try:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        except ValueError:
            return None
    return None

def parse_timings(file_path: Path) -> List[Tuple[float, float, Optional[str]]]:
    """Parse timing file: each line: start end [name]."""
    timings = []
    with open(file_path, 'r', encoding='utf-8') as f:
        for num, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            start = parse_time(parts[0])
            end = parse_time(parts[1])
            if start is None or end is None or start >= end:
                continue
            name = " ".join(parts[2:]) if len(parts) > 2 else None
            timings.append((start, end, name))
    return timings

def analyze_timings(timings: List[Tuple[float, float, Optional[str]]]) -> None:
    """Print analysis of timings."""
    if not timings:
        return
    total = len(timings)
    durations = [e - s for s, e, _ in timings]
    total_duration = sum(durations)
    avg = total_duration / total
    min_dur = min(durations)
    max_dur = max(durations)
    print("\n" + "-" * 50)
    print(tr("timings_analysis") + ":")
    print(f"  {tr('total_clips')}: {total}")
    print(f"  {tr('total_duration')}: {total_duration:.1f}s ({total_duration/60:.1f}min)")
    print(f"  {tr('average_duration')}: {avg:.1f}s")
    print(f"  {tr('shortest')}: {min_dur:.1f}s")
    print(f"  {tr('longest')}: {max_dur:.1f}s")
    print("-" * 50)