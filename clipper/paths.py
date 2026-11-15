"""Centralized path definitions - independent of all other modules"""

import sys
from pathlib import Path

def get_base_dir() -> Path:
    """Return the base directory (where the app is running or installed)."""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    else:
        return Path.cwd()

def get_meipass_dir() -> Path:
    """Return the temporary directory used by PyInstaller for bundled files."""
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS)
    return Path.cwd()

# Main directories
BASE_DIR = get_base_dir()
ROOT = BASE_DIR / "Clipping"

# Directory for language files
if getattr(sys, 'frozen', False):
    LANG_DIR = get_meipass_dir() / "lang"
else:
    LANG_DIR = ROOT / "lang"

# Subdirectories
TIMINGS_DIR = ROOT / "timings"
OUTPUT_DIR = ROOT / "output"
CONFIG_FILE = ROOT / "config.json"