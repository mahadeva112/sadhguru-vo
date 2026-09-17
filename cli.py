#!/usr/bin/env python3
"""
Sadhguru VO — CLI entry point.

    python cli.py --script script.txt --out out.mp3
    python cli.py --help
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sadhguru_vo.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
