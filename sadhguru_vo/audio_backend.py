"""
The one place that imports pydub — and the only place that knows ffmpeg has to
be told not to open a window.

pydub shells out to ffmpeg/ffprobe for every decode and every export. It passes
no creation flags, so on Windows each of those calls opens a console window for
as long as ffmpeg runs. Under `pythonw.exe` — which is what the pinned shortcut
launches — that shows up as a black window blinking on top of the app.

For one script it blinks a couple of times. A dialogue decodes and exports every
turn, so a sixty-turn conversation blinks well over a hundred times while it
assembles. That is the whole reason this module exists.

Both call styles in pydub have to be covered, because they resolve the name
differently:

    pydub/utils.py          from subprocess import Popen   →  module-level name
    pydub/audio_segment.py  import subprocess              →  attribute lookup

so one patches the bound name and the other patches the module reference.
"""

import subprocess
import sys

_IS_WINDOWS = sys.platform.startswith("win")

# subprocess.CREATE_NO_WINDOW exists on Windows from 3.7. The literal is the
# fallback for anything that somehow lacks the constant.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

_patched = False


def _quiet_kwargs(kwargs: dict) -> dict:
    """Add the no-window flag without discarding flags the caller set."""
    kwargs["creationflags"] = kwargs.get("creationflags", 0) | _CREATE_NO_WINDOW
    return kwargs


def _quiet_popen(original):
    def popen(*args, **kwargs):
        return original(*args, **_quiet_kwargs(kwargs))
    popen.__doc__ = "subprocess.Popen with CREATE_NO_WINDOW forced on."
    return popen


class _QuietSubprocess:
    """Stand-in for the subprocess module that hides the console window.

    Everything except Popen passes straight through, so pydub's use of
    subprocess.PIPE, subprocess.DEVNULL and the rest is unaffected.
    """

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def Popen(self, *args, **kwargs):        # noqa: N802 — matching stdlib
        return self._real.Popen(*args, **_quiet_kwargs(kwargs))


def silence_ffmpeg_windows() -> bool:
    """
    Stop pydub's ffmpeg calls flashing a console window. Idempotent.

    Returns True if the patch is in place. A no-op off Windows, and best-effort
    everywhere: a pydub layout this does not recognise means visible flashes,
    which is cosmetic — never a reason to fail a render.
    """
    global _patched
    if _patched or not _IS_WINDOWS:
        return _patched
    try:
        import pydub.audio_segment
        import pydub.utils

        if getattr(pydub.utils.Popen, "__name__", "") != "popen":
            pydub.utils.Popen = _quiet_popen(pydub.utils.Popen)
        if not isinstance(pydub.audio_segment.subprocess, _QuietSubprocess):
            pydub.audio_segment.subprocess = _QuietSubprocess(
                pydub.audio_segment.subprocess)
        _patched = True
    except Exception:
        pass
    return _patched


def ffmpeg_exe() -> str:
    """The ffmpeg pydub resolved, so a direct call uses the same binary."""
    try:
        from pydub.utils import which
        return which("ffmpeg") or "ffmpeg"
    except Exception:
        return "ffmpeg"


def run_ffmpeg(args, timeout: float = 300.0) -> tuple:
    """
    Call ffmpeg directly, without a console window. Returns (ok, stderr).

    Dub Sync time-stretches with the `atempo` filter, which pydub does not
    expose — its own speedup() is an overlap-add that sounds like an artefact on
    speech, and the entire point of the fitting stage is to stay inaudible. So
    that one filter is invoked here rather than through pydub, and it goes
    through this module so it inherits the no-window handling like every other
    ffmpeg call in the app.
    """
    cmd = [ffmpeg_exe(), "-nostdin", "-loglevel", "error", "-y"] + list(args)
    kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    if _IS_WINDOWS:
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    try:
        proc = subprocess.run(cmd, timeout=timeout, **kwargs)
    except FileNotFoundError:
        return False, ("ffmpeg was not found on PATH. "
                       "Install it with `winget install Gyan.FFmpeg`.")
    except subprocess.TimeoutExpired:
        return False, f"ffmpeg timed out after {timeout:.0f}s."
    err = (proc.stderr or b"").decode("utf-8", "replace").strip()
    return proc.returncode == 0, err


def audio_segment():
    """
    Import pydub with ffmpeg already silenced, and return AudioSegment.

    Every part of the app that needs pydub goes through here, so the patch
    cannot be missed by whichever code path happens to run first.

    Raises ImportError with an actionable message — callers decide whether that
    is fatal. Step 1 degrades to raw byte concatenation; dialogue assembly
    cannot, since laying clips on a timeline requires decoding them.
    """
    try:
        from pydub import AudioSegment
    except ImportError as e:
        raise ImportError(
            "pydub is needed to join and assemble audio.\n"
            "  pip install pydub audioop-lts\n"
            "  winget install Gyan.FFmpeg\n"
            f"(import failed: {e})") from None
    silence_ffmpeg_windows()
    return AudioSegment
