# cli.py - نقطة الدخول للتشغيل (GUI أو CLI)
import sys
from clipper.gui import main as gui_main
from clipper.main import main as cli_main

if __name__ == "__main__":
    # إذا كان هناك وسيط سطر أوامر --cli، شغّل CLI، وإلا شغّل GUI
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        cli_main()
    else:
        gui_main()  