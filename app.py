#!/usr/bin/env python3
"""
Sadhguru VO — GUI entry point.

    python app.py

The launcher (Sadhguru VO.bat / the pinned shortcut) runs this with pythonw.exe
so no console window appears.
"""

import os
import sys

# Run correctly whether launched from this folder or anywhere else.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _fatal(title: str, msg: str) -> int:
    """Print + log + show a dialog, then exit non-zero so a .bat launcher keeps
    its console window open long enough to read the message."""
    print(f"{title}: {msg}", file=sys.stderr)
    try:
        log = os.path.join(os.path.dirname(os.path.abspath(__file__)), "error_log.txt")
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"\n[startup] {title}: {msg}\n")
    except OSError:
        pass
    try:
        import tkinter.messagebox as mb
        mb.showerror(title, msg)
    except Exception:
        pass
    return 1


def main() -> int:
    try:
        import tkinter  # noqa: F401
    except ImportError as e:
        return _fatal("Tkinter Missing",
                      f"Python was built without Tkinter ({e}).\n\n"
                      "Install a python.org build, or on Linux: "
                      "apt install python3-tk")
    try:
        from sadhguru_vo.gui import main as gui_main
    except Exception as e:
        return _fatal("Startup Failed",
                      f"Could not load the app:\n\n{e!r}\n\n"
                      "Fix: run setup_windows.bat to (re)install dependencies.")
    return gui_main()


if __name__ == "__main__":
    raise SystemExit(main())
