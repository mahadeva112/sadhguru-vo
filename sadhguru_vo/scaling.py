"""
One scale factor for the whole window.

Before this module the GUI was written against a single implicit assumption:
that a pixel on the developer's screen is a pixel on yours. It is not. A 14"
laptop at 150% Windows scaling, a 4K edit bay, a 1366x768 field machine and a
window dragged from the laptop panel onto an external monitor are four
different pixel grids, and the app was built for exactly one of them.

The symptoms all came from the same place — sizes stated as bare numbers, in
140 different files' worth of call sites, with no authority above them:

    root.geometry("1317x740")     a window wider than a 1366px desktop at 125%
    font=(UI_FONT, 9)             9pt resolved once, at whatever DPI Tk guessed
    padx=8, height=44             pixels that stay 8 and 44 forever
    (no DPI awareness at all)     Windows upscales the whole window -> blurry

So this module is the missing authority. It does four things, in this order:

  1. `enable_dpi_awareness()` — told to Windows *before* the Tk root exists,
     because after that the process's DPI mode is fixed. Without it Windows
     hands Tk a lie (a 96-DPI virtual screen) and stretches the result, which
     is where the blur comes from.

  2. `init(root)` — reads the real DPI off the root window and derives `k`, the
     factor everything else multiplies by. `k` is 1.0 at 96 DPI, 1.5 at 144.
     Tk's own `tk scaling` is set from the same number so that point-sized
     fonts land on the right pixel count.

  3. Named fonts. Tk resolves a font *tuple* at widget-creation time and forgets
     it; a named font stays live, so when the window crosses onto a monitor with
     a different DPI every label using it re-renders at the new size. The eleven
     names below are a 1:1 mechanical mapping of the eleven (family, size,
     weight) tuples the GUI already used — deliberately mechanical, because the
     single-speaker tab is in daily production and a rename per call site is a
     judgement per call site.

  4. `S(n)` — the pixel helper. Every literal padx/pady/wraplength and every
     Frame width/height goes through it.

What this module does *not* do is change any layout. Same widgets, same order,
same proportions — the numbers just come from one place now.
"""

import sys
import tkinter as tk
import tkinter.font as tkfont

from .config import IS_WINDOWS, MONO_FONT, UI_FONT

# The DPI the existing numbers were written against. k == 1.0 here.
BASE_DPI = 96.0

# Below this the fonts stop being legible and above it the window stops fitting
# on anything; both ends are defensive, not expected.
MIN_SCALE, MAX_SCALE = 1.0, 3.0

# Set by init(). Module-level because S() is called from inside widget
# constructors all over the GUI, where threading a factor through every call
# would be noise.
_k = 1.0
_root = None
_dpi = BASE_DPI

# The live Font objects, held here on purpose. tkinter's Font.__del__ issues
# `font delete`, so a Font that is created and dropped takes its name with it —
# and Tk then treats a widget's `font="SvoMono9"` not as an error but as a
# request for a font *family* called SvoMono9, silently falling back to the
# default. That failure is invisible until you notice nothing scales.
_fonts = {}

# ── the eleven fonts ─────────────────────────────────────────────────────────
# name -> (family, points, weight). Read by init() to create the named fonts and
# again by _refont() when the DPI changes under a window that already exists.
_FONT_SPECS = {
    "SvoUI8":     (UI_FONT,   8,  "normal"),
    "SvoUI9":     (UI_FONT,   9,  "normal"),
    "SvoMono7":   (MONO_FONT, 7,  "normal"),
    "SvoMono7B":  (MONO_FONT, 7,  "bold"),
    "SvoMono8":   (MONO_FONT, 8,  "normal"),
    "SvoMono8B":  (MONO_FONT, 8,  "bold"),
    "SvoMono9":   (MONO_FONT, 9,  "normal"),
    "SvoMono9B":  (MONO_FONT, 9,  "bold"),
    "SvoMono10":  (MONO_FONT, 10, "normal"),
    "SvoMono10B": (MONO_FONT, 10, "bold"),
    "SvoMono11B": (MONO_FONT, 11, "bold"),
}

# Public aliases, so call sites read as names rather than strings.
F_UI8, F_UI9 = "SvoUI8", "SvoUI9"
F_MONO7, F_MONO7B = "SvoMono7", "SvoMono7B"
F_MONO8, F_MONO8B = "SvoMono8", "SvoMono8B"
F_MONO9, F_MONO9B = "SvoMono9", "SvoMono9B"
F_MONO10, F_MONO10B = "SvoMono10", "SvoMono10B"
F_MONO11B = "SvoMono11B"


# ── step 1: before the root exists ───────────────────────────────────────────
def enable_dpi_awareness() -> None:
    """Tell Windows this process draws at the monitor's real DPI.

    Must run before `tk.Tk()`. Per-monitor-v2 awareness is what makes a window
    dragged between two differently-scaled monitors report the new DPI instead
    of the one it was born on; the older per-monitor and system-aware modes are
    tried in turn for Windows 8.1 and 7. A failure here is not fatal — the app
    falls back to the blurry-but-working behaviour it has today.
    """
    if not IS_WINDOWS:
        return
    try:
        import ctypes
    except ImportError:
        return

    # Windows 10 1703+: per-monitor v2, the only mode where a DPI change is
    # reported to a window that is already open.
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)
        return
    except (AttributeError, OSError):
        pass
    # Windows 8.1+: 2 == PROCESS_PER_MONITOR_DPI_AWARE.
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except (AttributeError, OSError):
        pass
    # Windows 7: system-wide awareness, fixed at login.
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


# ── step 2: once the root exists ─────────────────────────────────────────────
def init(root: tk.Misc) -> float:
    """Derive the scale factor from the root window and build the named fonts.

    Returns `k` so the caller can size the window with it before anything is
    packed.
    """
    global _k, _root, _dpi
    _root = root
    _dpi = _measure_dpi(root)
    _k = _clamp(_dpi / BASE_DPI)

    # Tk states font sizes in points and converts with this factor, so setting
    # it is what makes a 9pt label 9pt on every screen rather than 9 * 96/72
    # pixels on all of them.
    try:
        root.tk.call("tk", "scaling", _dpi / 72.0)
    except tk.TclError:
        pass

    for name, (family, points, weight) in _FONT_SPECS.items():
        existing = _fonts.get(name)
        if existing is not None:
            existing.configure(family=family, size=points, weight=weight)
            continue
        try:
            _fonts[name] = tkfont.Font(root=root, name=name, family=family,
                                       size=points, weight=weight, exists=False)
        except tk.TclError:
            # The name is already registered in this interpreter — a second Tk
            # root in the same process. Adopt it rather than fight over it.
            _fonts[name] = tkfont.Font(root=root, name=name, exists=True)
            _fonts[name].configure(family=family, size=points, weight=weight)
    return _k


def _measure_dpi(root: tk.Misc) -> float:
    """Pixels per inch, as the window itself reports them.

    `winfo_fpixels('1i')` is the honest answer on every platform once the
    process is DPI-aware: Windows returns the monitor's effective DPI, macOS
    reports 72 in points (Retina is handled by the backing store, so the app
    should not scale again), Linux returns whatever the X server was told.
    """
    try:
        dpi = float(root.winfo_fpixels("1i"))
    except (tk.TclError, ValueError):
        return BASE_DPI
    if dpi <= 0:
        return BASE_DPI
    if sys.platform == "darwin":
        # Aqua already draws at 2x on Retina; scaling on top would double it.
        return BASE_DPI
    return dpi


def _clamp(k: float) -> float:
    return max(MIN_SCALE, min(MAX_SCALE, k))


# ── step 3: the helpers every call site uses ─────────────────────────────────
def S(n) -> int:
    """Scale a pixel constant. `padx=8` becomes `padx=S(8)`.

    Rounds away from zero so a 1px separator stays a visible 1px rather than
    disappearing, which is the one rounding error that reads as a bug.
    """
    if n == 0:
        return 0
    scaled = int(round(n * _k))
    return scaled if scaled else (1 if n > 0 else -1)


def SP(pair):
    """Scale a `(left, right)` padding pair, which Tk accepts wherever it
    accepts a single number."""
    return (S(pair[0]), S(pair[1]))


def scale() -> float:
    """The current factor, for callers that need to do their own arithmetic —
    the Dub Sync timeline canvas, mainly."""
    return _k


def dpi() -> float:
    return _dpi


# ── step 4: the window moved to another monitor ──────────────────────────────
def refresh(root: tk.Misc) -> bool:
    """Re-read the DPI and, if it changed, resize every named font to match.

    Returns True when something changed, so the caller can re-clamp the window
    geometry to the new screen.

    Padding and frame heights keep the pixel sizes they were built with — those
    were resolved once, at construction, and re-deriving them would mean tearing
    the window down and rebuilding it mid-session. Fonts are the part that
    actually hurts to get wrong, and containers grow to fit them, so the window
    stays usable; it is just a touch tighter or roomier than a fresh launch on
    that monitor would be.
    """
    global _k, _dpi
    new_dpi = _measure_dpi(root)
    if abs(new_dpi - _dpi) < 1.0:
        return False
    _dpi, _k = new_dpi, _clamp(new_dpi / BASE_DPI)
    try:
        root.tk.call("tk", "scaling", _dpi / 72.0)
    except tk.TclError:
        pass
    for name, (family, points, weight) in _FONT_SPECS.items():
        f = _fonts.get(name)
        if f is None:
            continue
        try:
            f.configure(family=family, size=points, weight=weight)
        except tk.TclError:
            pass
    return True


# ── window sizing ────────────────────────────────────────────────────────────
# Chrome the window has to live beside: the taskbar at the bottom, and enough
# margin that the frame is grabbable rather than flush with the screen edge.
_SCREEN_MARGIN_W = 80
_SCREEN_MARGIN_H = 90


def fit_window(root: tk.Tk, base_w: int, base_h: int,
               min_w: int, min_h: int) -> None:
    """Size and centre the window against the screen it is opening on.

    The wanted size is the design size scaled by `k`; the allowed size is what
    the desktop actually has. Taking the smaller of the two is the whole fix for
    the 1366x768-at-125% case, where the design size alone is wider than the
    screen and the action bar at the foot of the window falls off the bottom.

    minsize is clamped the same way. A minimum larger than the screen is not a
    minimum, it is a window the user cannot resize.
    """
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    avail_w = max(320, sw - S(_SCREEN_MARGIN_W))
    avail_h = max(240, sh - S(_SCREEN_MARGIN_H))

    w = min(S(base_w), avail_w)
    h = min(S(base_h), avail_h)
    root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
    root.minsize(min(S(min_w), avail_w), min(S(min_h), avail_h))
