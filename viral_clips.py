#!/usr/bin/env python3
"""Convenience entry point for the viral clipping pipeline."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from clipper.viral.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
