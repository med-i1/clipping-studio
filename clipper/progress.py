"""Progress bar with ETA"""

import sys
import time
from pathlib import Path
from collections import deque

from clipper.utils import format_eta

class ETAEstimator:
    def __init__(self, window_size=10):
        self.times = deque(maxlen=window_size)
        self.start_time = time.time()
        self.processed = 0
        self.last_time = self.start_time

    def update(self, elapsed_since_last=None):
        if elapsed_since_last is None:
            now = time.time()
            if self.processed > 0:
                self.times.append(now - self.last_time)
            self.last_time = now
            self.processed += 1
        else:
            self.times.append(elapsed_since_last)
            self.processed += 1

    def get_eta(self, total, current):
        if current == 0:
            return "???"
        if len(self.times) < 2:
            elapsed = time.time() - self.start_time
            avg = elapsed / current if current > 0 else 0
        else:
            avg = sum(self.times) / len(self.times)
        remaining = total - current
        eta_sec = avg * remaining
        return format_eta(eta_sec)

def progress_bar(current: int, total: int, start_time: float, eta_estimator: ETAEstimator,
                 clip_info: str = "", extra: str = "") -> None:
    clip_info = str(Path(clip_info).name)
    if len(clip_info) > 40:
        clip_info = clip_info[:37] + "..."
    elapsed = time.time() - start_time
    percent = current / total if total else 0
    bar_len = 30
    filled = int(bar_len * percent)
    bar = '█' * filled + '░' * (bar_len - filled)
    eta = eta_estimator.get_eta(total, current) if current > 0 else "???"
    sys.stdout.write(f"\r[{bar}] {percent*100:5.1f}%  {current}/{total}  ETA: {eta}  {extra} {clip_info}")
    sys.stdout.flush()