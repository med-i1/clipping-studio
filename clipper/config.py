"""Configuration management"""

import json
from pathlib import Path
from typing import Dict, Any

from clipper.paths import ROOT, CONFIG_FILE

DEFAULT_CONFIG = {
    "format": "bv*+ba/b",
    "retry": 3,
    "force_keyframes": False,
    "skip_existing": True,
    "output_mode": "fast",
    "ffmpeg_preset": "veryfast",
    "ffmpeg_crf": 20,
    "resume": True,
    "parallel_workers": 4,
    "min_file_size_kb": 50,
    "download_full_video": True,
    "keep_full_video": False,
    "download_mode": "auto",     # "clip", "full", "auto"
    "language": "en",
}

def load_config() -> Dict[str, Any]:
    """Load config from Clipping/config.json, create with defaults if missing."""
    from clipper.lang import set_language

    if not CONFIG_FILE.exists():
        ROOT.mkdir(parents=True, exist_ok=True)
        cfg = DEFAULT_CONFIG.copy()
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        return cfg
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        lang = cfg.get("language", "en")
        set_language(lang)
        return cfg
    except Exception as e:
        print(f"Error loading config: {e}")
        return DEFAULT_CONFIG.copy()

def save_config(cfg: Dict[str, Any]) -> None:
    """Save configuration to file."""
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)