#!/usr/bin/env python3
"""
Clipper - YouTube Video Clipper
Entry point for both GUI and CLI modes
"""

import sys
import os

# تأكد من أن المجلد الحالي في المسار
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        from clipper.main import main as cli_main
        cli_main()
    else:
        from clipper.gui import main as gui_main
        gui_main()

if __name__ == "__main__":
    main()