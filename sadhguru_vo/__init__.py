"""
Sadhguru VO — standalone two-step ElevenLabs voice-over pipeline.

    Step 1  script       → ElevenLabs TTS              (Sadhguru Hindi voice)
    Step 2  step-1 audio → ElevenLabs speech-to-speech (step-2 voice)

Lifted out of the bulk-video-processing project as an independent app: its own
dependencies, its own config files, and no imports from the original.

Layout:
    config.py          constants, paths, theme palette
    prefs.py           persisted API key / voice + model choices / favourites
    elevenlabs_api.py  the ElevenLabs HTTP calls
    llm.py             the optional emotion-tag pass (3 providers)
    pipeline.py        the two-step run, with no UI in it
    gui.py             standalone Tkinter window
    cli.py             argparse front-end over the same pipeline
"""

from .config import APP_NAME, APP_VERSION

__all__ = ["APP_NAME", "APP_VERSION"]
__version__ = APP_VERSION
