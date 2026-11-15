"""
clipper/main.py - CLI entry point
"""

import sys
import json
from pathlib import Path

from clipper import __version__
from clipper.config import load_config
from clipper.paths import ROOT
from clipper.lang import tr, set_language
from clipper.engine import ClipperEngine
from clipper.utils import format_eta


def main() -> None:
    # Load config & language
    config = load_config()

    # Get inputs
    url = input(tr("enter_url") + " ").strip()
    while not url:
        url = input(tr("enter_url") + " ").strip()

    timings_file = input(tr("enter_timings") + " ").strip()
    while not timings_file or not Path(timings_file).exists():
        print(tr("not_found").format(timings_file))
        timings_file = input(tr("enter_timings") + " ").strip()

    # Run engine
    engine = ClipperEngine(config)
    results = engine.process(url, timings_file)

    # Print summary
    print("\n--- " + tr("summary") + " ---")
    print(f"{tr('total')}: {results['total']}")
    print(f"{tr('success')}: {results['success']}")
    print(f"{tr('failed')}: {results['failed']}")
    print(f"{tr('elapsed')}: {format_eta(results['elapsed'])}")
    print(tr("folder_icon").format(results['project_root']))
    print("\n" + tr("thumbs_up"))


if __name__ == "__main__":
    main()