"""
`python -m sadhguru_vo` launches the GUI; `python -m sadhguru_vo.cli` the CLI.
"""

from .gui import main

if __name__ == "__main__":
    raise SystemExit(main())
