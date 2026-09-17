"""
Standalone Tkinter window: a rail of three modes on the left, and the mode you
picked filling everything to the right of it.

  Sadhguru VO  the original single-speaker pipeline — API key row, a Step-1
               panel (TTS voice + model + emotion toggle), a Step-2 panel
               (target voice + STS model + keep-intermediate toggle), path
               bars, script box, per-step progress and the action bar. Carried
               over unchanged; only the frame it packs into is different.

  Dialogue     the multi-speaker path. A script with speaker labels, a cast
               table giving each speaker their own voices and their own number
               of steps, and a render that assembles a master, per-speaker
               stems and a timecoded manifest.

  Dub Sync     dubbing an existing recording. Pause detection on the source
               audio gives the chunk boundaries, both scripts are split across
               them, and a local estimate says whether each translation will
               fit its slot — all before anything is generated. Lives in
               gui_dub.py and is mixed in.

The API key and the loaded voice catalogue are window chrome shared by all three
modes — one key, one validation, one voice list. They live at the foot of the
rail, behind a status line that opens a popover, because a field you fill in
once a month should not own a strip across the top of the window forever.

Each mode's body is a frame that is packed and unpacked as the rail selection
changes; the bodies themselves know nothing about the rail.
"""

import os
import re
import subprocess
import threading
import tkinter as tk
from tkinter import (filedialog, font as tkfont, messagebox,
                     scrolledtext, ttk)
from typing import Dict, List

from .assembly import AssemblyError
from .cast import (CAST_MODE_LABELS, CAST_MODES, SpeakerRecipe,
                   cast_for_speakers, read_cast, validate_cast, write_cast)
from .config import (ACCENT, APP_NAME, APP_USER_MODEL_ID, APP_VERSION, BG,
                     BTN_ACT, BTN_BG, BTN_FG, CAST_FILE,
                     DIALOGUE_GAP_SAME_MS, DIALOGUE_GAP_SWITCH_MS,
                     ELEVENLABS_STS_MODELS, ELEVENLABS_TTS_MODELS, ERR_RED,
                     GEMINI_DEFAULT_MODEL, ICON_PATH, INPUT_BG, INPUT_FG,
                     IS_MAC, IS_WINDOWS, PANEL, PANEL2,
                     PANEL_BORDER, RAIL_ACCENT, RAIL_BG, RAIL_HOVER, RAIL_SEL,
                     REG_LABEL, STEP1_GOLD, STEP1_VOICE_ID,
                     STEP1_VOICE_NAME, STEP2_VIOLET, STEP2_VOICE_ID,
                     STEP2_VOICE_NAME, SVO_PANEL, TEXT, TEXT_FAINT,
                     TEXT_MUTED, TR_ACCENT, VO_LANGUAGE, WARN_AMBER,
                     btn_fg)
from .elevenlabs_api import (clear_voice_cache, fetch_voices, validate_api_key,
                             voice_cache_get)
from .gui_dub import _DUB_SUBTITLE, DubSyncTabMixin
from .scaling import (F_MONO8, F_MONO8B, F_MONO9, F_MONO9B, F_MONO10,
                      F_MONO10B, F_UI8, F_UI9, S, enable_dpi_awareness,
                      fit_window)
from . import scaling
from .llm import llm_provider_label
from .pipeline import (MODE_BOTH, MODE_TTS, MODE_VC, DialogueRequest,
                       VoRequest, prepare_dialogue, run_dialogue, run_pipeline,
                       step1_target, validate_request)
from .prefs import (get_api_key, read_api_key_file, read_fav_voices,
                    read_prefs, sanitize_voice_id, set_runtime_api_key,
                    write_api_key_file, write_prefs)
from .script_parser import (ScriptFormatError, normalize_speaker, parse_script,
                            speakers_in)

_AUDIO_FILETYPES = [("Audio files", "*.mp3 *.wav *.m4a *.aac *.flac *.ogg *.webm"),
                    ("All files", "*.*")]
_STEP1_AUTO_NOTE = ("Auto — written next to the final output as "
                    "“…_step1_sadhguru.mp3”")

_SOLO_SUBTITLE = (f"One pipeline:  ①  script → TTS ({STEP1_VOICE_NAME})   "
                  f"→   ②  voice change ({STEP2_VOICE_NAME})")
_DLG_SUBTITLE  = ("Many speakers:  script → turns → each speaker's own voice "
                  "→ master + stems + manifest")

_DLG_PLACEHOLDER = """SADHGURU: जीवन एक अवसर है। इसे व्यर्थ मत गँवाइए।
INTERVIEWER: But Sadhguru, how does one actually begin?
SADHGURU: You begin by sitting still. Just a few minutes each morning.
INTERVIEWER: That sounds simple enough."""

class _Tooltip:
    """Hover text for one widget.

    Exists so a status readout can be short without throwing information away:
    the label shows "✔ 4048 voices", the tooltip carries the rest.
    """

    def __init__(self, widget, delay_ms: int = 300):
        self._widget = widget
        self._delay = delay_ms
        self._text = ""
        self._tip = None
        self._job = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def set(self, text: str) -> None:
        self._text = (text or "").strip()
        if self._tip is not None:
            self.hide()

    @property
    def text(self) -> str:
        """The same detail, for somewhere that has room to show it outright."""
        return self._text

    def _schedule(self, _event=None):
        self._cancel()
        if self._text:
            try:
                self._job = self._widget.after(self._delay, self._show)
            except tk.TclError:
                pass

    def _cancel(self):
        if self._job is not None:
            try:
                self._widget.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None

    def _show(self):
        self._job = None
        if self._tip is not None or not self._text:
            return
        try:
            x = self._widget.winfo_rootx() + 10
            y = self._widget.winfo_rooty() + self._widget.winfo_height() + 6
            self._tip = tk.Toplevel(self._widget)
            self._tip.wm_overrideredirect(True)
            self._tip.wm_geometry(f"+{x}+{y}")
            tk.Label(self._tip, text=self._text, bg=PANEL2, fg=TEXT,
                     font=F_UI8, justify="left", anchor="w",
                     padx=S(9), pady=S(6), bd=0,
                     highlightbackground=PANEL_BORDER, highlightthickness=1
                     ).pack()
        except tk.TclError:
            self._tip = None

    def hide(self, _event=None):
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None


def _short_error(message: str) -> str:
    """Two words at most for the status pill. The full text goes in the tooltip.

    Matched on the messages elevenlabs_api.py raises, so the pill names the
    thing the user has to fix — a bad key and a dead network need different
    actions, and "✗ failed" for both would hide that.
    """
    m = (message or "").lower()
    if "401" in m or "expired" in m or "rejected the api key" in m:
        return "✗ bad key"
    if "429" in m or "rate limit" in m:
        return "✗ rate limit"
    if "network" in m or "cannot reach" in m or "timed out" in m:
        return "✗ offline"
    if "no voices" in m:
        return "✗ no voices"
    if "empty" in m:
        return "✗ no key"
    return "✗ failed"


def _norm_label(text: str) -> str:
    """Collapse whitespace in a dropdown label so it can be looked up reliably.

    Voice labels carry a leading marker column — "✦ " when the voice advertises
    the target language, two spaces when it does not — and favourites gain a
    "★ " on top of that. Any lookup that strips the label first therefore misses
    every plain voice, which is most of an account.
    """
    return re.sub(r"\s+", " ", str(text or "")).strip()


_BTN_BORDER_BY_BG = {
    "#0f1d14": "#22c55e",
    "#3b2f10": "#d97706",
    SVO_PANEL: "#5b4fbf",
    "#172554": "#1d4ed8",
    TR_ACCENT: "#16a34a",
}


class SadhguruVOApp(DubSyncTabMixin):

    # Rail glyph, name, the line under it, and the subtitle the mode header
    # shows. Order is the order of the rail and of the tab frames.
    _MODES = (
        ("①②", "Sadhguru VO", "one voice, two steps",      _SOLO_SUBTITLE),
        ("⇄",  "Dialogue",    "many speakers, one master", _DLG_SUBTITLE),
        ("◉",  "Studio",      "dub to an existing take",   _DUB_SUBTITLE),
    )

    # Leading glyphs the status text may carry — peeled off onto the rail's dot
    # so the label itself stays a word.
    _STATUS_GLYPHS = "●✔✗◌⚠"

    def __init__(self, root: tk.Tk):
        self.root = root
        self._closing = False

        # Before any widget exists, so every font and every S() below resolves
        # against this screen's DPI rather than against the 96 the code was
        # written on. Idempotent — safe if a caller already ran it.
        scaling.init(root)
        self._dpi_job = None

        self._prefs        = read_prefs()
        self._step1_voice  = self._prefs["step1_voice"]
        self._step2_voice  = self._prefs["step2_voice"]
        self._out_path     = ""
        self._step1_path   = ""
        self._last_output  = ""
        self._running      = False
        self._buttons: List[tk.Button] = []

        self._voice_options: List[Dict[str, str]] = []
        self._label_to_id: Dict[str, str] = {}
        self._fav_ids = read_fav_voices()
        self._last_validated_key = None

        # ── Shell state ──────────────────────────────────────────────────────
        self._mode_index = 0
        self._tab_hits: List[tuple] = []    # (x0, x1) per tab, set by _draw_tabs
        self._tab_hover = -1
        self._tabs_compact = False
        self._key_pop = None            # the API-key popover, when open
        self._pop_status = None
        self._pop_detail = None
        self._pop_binds: List[str] = []
        self._pop_anchor = None

        # ── Dialogue tab state ───────────────────────────────────────────────
        # The cast persists across sessions, so re-opening a script does not
        # mean re-choosing everybody's voice.
        self._cast: Dict[str, SpeakerRecipe] = read_cast(CAST_FILE)
        self._cast_rows: List[dict] = []
        self._dlg_speakers: List[str] = []
        self._dlg_out_path = ""
        self._dlg_running = False
        self._dlg_buttons: List[tk.Button] = []
        self._dlg_last_output = ""
        self._detect_job = None

        root.title(f"{APP_NAME} {APP_VERSION}")
        root.configure(bg=BG)
        # The whole width goes to the content now — the modes are a tab strip
        # across the top, so nothing stands to the left of the body any more.
        # The chrome above the body is two short strips (the tabs, then the line
        # saying what the mode does) where the rail build had one.
        #
        # Those are design sizes, at 96 DPI. fit_window scales them by the
        # screen's DPI and then clips the result to what the desktop actually
        # has, which is the difference between opening correctly on a 1366x768
        # laptop at 125% and opening wider than the screen it is on.
        fit_window(root, 1120, 760, 940, 600)
        self._apply_window_icon()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._api_key_var    = tk.StringVar(value=read_api_key_file())
        self._step1_voice_var = tk.StringVar()
        self._step2_voice_var = tk.StringVar()
        self._step1_model_var = tk.StringVar(value=self._prefs["step1_model"])
        self._step2_model_var = tk.StringVar(value=self._prefs["step2_model"])
        self._emotion_var     = tk.BooleanVar(value=False)
        self._keep_step1_var  = tk.BooleanVar(value=True)

        self._dlg_emotion_var   = tk.BooleanVar(value=False)
        self._dlg_merge_var     = tk.BooleanVar(value=True)
        self._dlg_loudness_var  = tk.BooleanVar(value=True)
        self._dlg_stems_var     = tk.BooleanVar(value=True)
        self._dlg_manifest_var  = tk.BooleanVar(value=True)
        self._dlg_gap_same_var  = tk.StringVar(value=str(DIALOGUE_GAP_SAME_MS))
        self._dlg_gap_switch_var = tk.StringVar(value=str(DIALOGUE_GAP_SWITCH_MS))

        self._dub_init_state()

        self._style_ttk()
        self._build()

        # A key cached in api.txt is validated (and voices loaded) on startup so
        # the dropdowns are populated without the user touching anything.
        if self._api_key_var.get().strip():
            self.root.after(200, self._on_api_key_changed)

    # ── chrome ───────────────────────────────────────────────────────────────
    def _apply_window_icon(self):
        """Window + taskbar icon. On Windows the explicit AppUserModelID stops
        the taskbar button from being grouped under a generic Python entry, which
        is what makes a pinned shortcut keep our own icon."""
        if IS_WINDOWS:
            try:
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                    APP_USER_MODEL_ID)
            except Exception:
                pass
        if os.path.exists(ICON_PATH):
            try:
                self.root.iconbitmap(default=ICON_PATH)
                return
            except tk.TclError:
                pass    # non-Windows Tk cannot read .ico — fall through
            try:
                self.root.iconphoto(True, tk.PhotoImage(file=ICON_PATH))
            except Exception:
                pass

    def _style_ttk(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TCombobox", fieldbackground=INPUT_BG,
                        background=INPUT_BG, foreground=INPUT_FG,
                        arrowcolor=INPUT_FG, selectbackground=INPUT_BG,
                        selectforeground=INPUT_FG)
        style.map("TCombobox",
                  fieldbackground=[("readonly", INPUT_BG)],
                  foreground=[("readonly", INPUT_FG)],
                  selectbackground=[("readonly", INPUT_BG)],
                  selectforeground=[("readonly", INPUT_FG)])

    def _btn(self, parent, text, cmd, bg=BTN_BG, fg=BTN_FG, abg=BTN_ACT,
             border=None):
        if border is None:
            border = _BTN_BORDER_BY_BG.get(bg, PANEL_BORDER)
        fg = btn_fg(fg)
        return tk.Button(parent, text=text, command=cmd,
                         bg=bg, fg=fg, activebackground=abg,
                         activeforeground=fg, relief="raised", bd=2,
                         highlightbackground=border, highlightcolor=border,
                         highlightthickness=1,
                         font=F_MONO10B,
                         padx=S(10), pady=S(4), cursor="hand2")

    # ═════════════════════════════════════════════════════════════════════════
    #  Layout
    # ═════════════════════════════════════════════════════════════════════════
    def _build(self):
        """A browser tab strip across the top; everything under it is content.

        The strip carries the brand, the three modes as tabs, and the key/voice
        status — one 38px row in place of the three stacked full-width strips
        (title bar, API key row, notebook tabs) the app started with, and in
        place of the 196px rail that replaced those. The modes read as tabs
        because they are tabs, and the body gets the full window width back.

        The key and the voice catalogue are still chrome shared by all three
        modes — one key, one validation, one voice list — but they are touched
        about once a month, so they sit at the right end of the strip behind a
        status pill instead of owning a strip of their own.
        """
        shell = tk.Frame(self.root, bg=BG)
        shell.pack(fill="both", expand=True)

        self._build_tabstrip(shell)

        content = tk.Frame(shell, bg=BG)
        content.pack(side="top", fill="both", expand=True)
        self._build_mode_header(content)

        body = tk.Frame(content, bg=BG)
        body.pack(fill="both", expand=True)
        self._tab_solo = tk.Frame(body, bg=BG)
        self._tab_dlg  = tk.Frame(body, bg=BG)
        self._tab_dub  = tk.Frame(body, bg=BG)

        self._build_solo_tab()
        self._build_dialogue_tab()
        self._build_dub_tab()
        self._sync_voice_lists()
        self._show_mode(0)
        self._bind_reflow()

    # ── the tab strip ─────────────────────────────────────────────
    # The three modes are browser tabs across the top rather than a rail down the
    # side. The reason is width: the rail spent 196px of every window on three
    # labels that are read once and then ignored, while the mode bodies — a
    # 38-character voice dropdown beside a model dropdown beside a button — are
    # what was actually short. A tab strip costs 38px of height instead, which
    # nothing in the bodies was competing for.
    #
    # Tabs are drawn on a Canvas rather than packed as Frames, because the
    # browser read comes entirely from the shape: rounded top corners and no
    # bottom edge, so the selected tab runs into the header below it as one
    # continuous surface. Tk has no rounded Frame, so the shape is two
    # rectangles and two pie slices in the tab's own fill.

    TABSTRIP_H = 38                 # design px, like every other size here
    TAB_RADIUS = 9
    TAB_PAD_X  = 15                 # text inset inside a tab
    TAB_GAP    = 3
    TAB_INSET  = 4                  # before the first tab, so its left edge shows
    TAB_ICON_GAP = 9                # between a tab's glyph and its name

    # One tint per mode, drawn on the glyph and nowhere else. A browser tab is
    # findable at a glance because its favicon is a colour before it is a
    # picture, and these three glyphs are small enough at 9pt that shape alone
    # does not carry them. The colours are the ones each mode already uses in
    # its own body — Step 1's gold, the shell's violet, the render green — so
    # the strip is naming things the user has already met rather than inventing
    # a second code.
    _TAB_TINT = (STEP1_GOLD, RAIL_ACCENT, TR_ACCENT)

    def _build_tabstrip(self, parent):
        strip = tk.Frame(parent, bg=RAIL_BG, height=S(self.TABSTRIP_H))
        strip.pack(side="top", fill="x")
        strip.pack_propagate(False)
        self._strip = strip

        # Brand left, key right, tabs in the space between — packed in that
        # order because the canvas is the one that takes what is left over.
        self._build_brand(strip)
        self._build_key_status(strip)

        self._tabs = tk.Canvas(strip, bg=RAIL_BG, highlightthickness=0, bd=0,
                               cursor="hand2")
        self._tabs.pack(side="left", fill="both", expand=True)
        self._tabs.bind("<Configure>", lambda _e: self._draw_tabs())
        self._tabs.bind("<Motion>", self._tabs_motion)
        self._tabs.bind("<Leave>", lambda _e: self._tabs_motion(None))
        self._tabs.bind("<Button-1>", self._tabs_click)

    def _build_brand(self, strip):
        holder = tk.Frame(strip, bg=RAIL_BG)
        holder.pack(side="left", fill="y", padx=(S(13), S(12)))
        inner = tk.Frame(holder, bg=RAIL_BG)
        inner.pack(expand=True)         # vertically centred in the strip
        self._brand_holder = holder
        self._rail_brand = tk.Label(inner, text="SADHGURU VO", bg=RAIL_BG,
                                    fg=STEP1_GOLD, font=F_MONO9B)
        self._rail_brand.pack(side="left")
        self._rail_ver = tk.Label(inner, text=f"v{APP_VERSION}", bg=RAIL_BG,
                                  fg=TEXT_FAINT, font=F_MONO8)
        self._rail_ver.pack(side="left", padx=(S(6), 0))

    def _tab_fonts(self):
        """The glyph is set in the UI face, the name in the mono one.

        Not a flourish: the mono face has no circled digits or dubbing marks, so
        a glyph set in it falls back per-character to whatever Tk finds, and
        "①②" comes out as two indistinguishable dots. The UI face has them."""
        return (tkfont.Font(root=self.root, name=F_UI9, exists=True),
                tkfont.Font(root=self.root, name=F_MONO9B, exists=True))

    def _tab_boxes(self):
        """Where each tab starts and ends, and the glyph and name inside it.

        Tabs are sized to their own text rather than to a common width: three
        equal tabs would make "Dialogue" and "Sadhguru VO" look like the same
        amount of thing, and in compact mode they would be three identical
        blanks with a glyph floating in the middle of each."""
        ui, mono = self._tab_fonts()
        pad, gap = S(self.TAB_PAD_X), S(self.TAB_GAP)
        boxes, x = [], S(self.TAB_INSET)
        for glyph, name, _blurb, _sub in self._MODES:
            name = "" if self._tabs_compact else name
            w = ui.measure(glyph) + 2 * pad
            if name:
                w += S(self.TAB_ICON_GAP) + mono.measure(name)
            boxes.append((x, x + w, glyph, name))
            x += w + gap
        return boxes

    def _draw_tabs(self):
        c = getattr(self, "_tabs", None)
        if c is None or self._closing:
            return
        h = c.winfo_height()
        if h <= 1:              # not mapped yet; <Configure> will call back
            return
        c.delete("all")
        boxes = self._tab_boxes()
        self._tab_hits = [(x0, x1) for x0, x1, _g, _n in boxes]
        ui, _mono = self._tab_fonts()
        r, pad = S(self.TAB_RADIUS), S(self.TAB_PAD_X)

        # The strip's own bottom edge. Drawn first so the selected tab paints
        # over its own span of it — which is the whole trick: an unbroken line
        # under the tabs would undo the join between the selected one and the
        # body below it.
        c.create_line(0, h - S(1), c.winfo_width(), h - S(1), fill=PANEL_BORDER)

        for i, (x0, x1, glyph, name) in enumerate(boxes):
            on, hot = (i == self._mode_index), (i == self._tab_hover)
            top = S(4) if on else S(7)  # the selected tab stands a little taller
            if on or hot:
                self._tab_shape(c, x0, x1, top, h, r,
                                PANEL if on else RAIL_HOVER,
                                PANEL_BORDER if on else "")
            if on:
                c.create_line(x0 + r, top + S(1), x1 - r, top + S(1),
                              fill=RAIL_ACCENT, width=S(2))
            cy = (top + h) // 2
            c.create_text(x0 + pad, cy, text=glyph, font=F_UI9, anchor="w",
                          fill=self._TAB_TINT[i])
            if name:
                c.create_text(x0 + pad + ui.measure(glyph) + S(self.TAB_ICON_GAP),
                              cy, text=name, font=F_MONO9B, anchor="w",
                              fill=TEXT if (on or hot) else TEXT_MUTED)

        # A hairline between two tabs, dropped whenever either side is painted,
        # because a painted edge already separates them.
        for i in range(len(boxes) - 1):
            if self._mode_index in (i, i + 1) or self._tab_hover in (i, i + 1):
                continue
            mid = (boxes[i][1] + boxes[i + 1][0]) // 2
            c.create_line(mid, S(13), mid, h - S(9), fill=PANEL_BORDER)

    def _tab_shape(self, c, x0, x1, top, bottom, r, fill, outline):
        """A rectangle with two rounded top corners and no bottom edge."""
        c.create_rectangle(x0, top + r, x1, bottom, fill=fill, outline=fill)
        c.create_rectangle(x0 + r, top, x1 - r, top + r, fill=fill, outline=fill)
        c.create_arc(x0, top, x0 + 2 * r, top + 2 * r, start=90, extent=90,
                     style="pieslice", fill=fill, outline=fill)
        c.create_arc(x1 - 2 * r, top, x1, top + 2 * r, start=0, extent=90,
                     style="pieslice", fill=fill, outline=fill)
        if not outline:
            return
        c.create_arc(x0, top, x0 + 2 * r, top + 2 * r, start=90, extent=90,
                     style="arc", outline=outline)
        c.create_arc(x1 - 2 * r, top, x1, top + 2 * r, start=0, extent=90,
                     style="arc", outline=outline)
        c.create_line(x0 + r, top, x1 - r, top, fill=outline)
        c.create_line(x0, top + r, x0, bottom, fill=outline)
        c.create_line(x1, top + r, x1, bottom, fill=outline)

    def _tab_at(self, x: int) -> int:
        for i, (x0, x1) in enumerate(self._tab_hits):
            if x0 <= x <= x1:
                return i
        return -1

    def _tabs_motion(self, event):
        """Hover only speaks for the tabs you are not already on — the selected
        one is already painted, so lighting it up would say nothing."""
        i = self._tab_at(event.x) if event is not None else -1
        if i == self._mode_index:
            i = -1
        if i != self._tab_hover:
            self._tab_hover = i
            self._draw_tabs()

    def _tabs_click(self, event):
        i = self._tab_at(event.x)
        if i >= 0:
            self._show_mode(i)

    # ── the window changed shape ──────────────────────────────────
    # Two things can happen to a window that is already open: it can be resized
    # (including snapped to half the screen), and it can be dragged onto a
    # monitor with different DPI. Both arrive as <Configure> on the root, so
    # both are answered here.
    #
    # The tab labels are what gives. Three names cost about 300px of strip, and
    # the mode you are in is named again by the header line directly underneath
    # the tabs, so below the threshold the strip keeps the glyphs and drops the
    # words — along with the brand and the key's label, which are the only other
    # things up there.
    #
    # The threshold is the window's own declared minimum width: 940 is the
    # narrowest the content is allowed to be, so the strip gives up its words
    # exactly when the window can no longer give the content that minimum.
    #
    # At 100% on any normal screen this never fires, because minsize forbids the
    # window from getting that narrow in the first place. It fires when the
    # screen is too small for the scaled minimum and fit_window had to clamp —
    # 1366x768 at 125%, say, where the window tops out at 1266px and the scaled
    # minimum is 1175.
    TABS_COMPACT_AT = 940               # design px; scaled at compare time


    def _bind_reflow(self):
        # None rather than False so the first pass always applies, whichever
        # side of the threshold the window happened to open on.
        self._tabs_compact = None
        self.root.bind("<Configure>", self._on_root_configure)
        self.root.after(80, lambda: self._apply_density(self.root.winfo_width()))

    def _on_root_configure(self, event):
        # Configure events from child widgets reach the toplevel's bindtags too;
        # only the window's own geometry is interesting here.
        if event.widget is not self.root or self._closing:
            return
        if self._dpi_job is not None:
            try:
                self.root.after_cancel(self._dpi_job)
            except tk.TclError:
                pass
        # Resizing fires continuously; answering every pixel would re-pack the
        # rail dozens of times per drag.
        self._dpi_job = self.root.after(120, self._settled)

    def _settled(self):
        self._dpi_job = None
        if self._closing:
            return
        if scaling.refresh(self.root):
            # The window crossed onto a monitor with different DPI. Named fonts
            # have already resized themselves; the window may now be bigger than
            # this screen, so clamp it.
            self._clamp_to_screen()
        self._apply_density(self.root.winfo_width())

    def _clamp_to_screen(self):
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        nw, nh = min(w, max(320, sw - S(80))), min(h, max(240, sh - S(90)))
        if (nw, nh) != (w, h):
            self.root.geometry(f"{nw}x{nh}")
        self.root.minsize(min(S(940), sw - S(40)),
                          min(S(600), sh - S(60)))

    def _apply_density(self, width: int):
        """Drop the tabs to their glyphs on a narrow window, restore the words on
        a wide one. The brand and the key's word go with them; the tabs and the
        key's dot are the half that has to survive."""
        if width <= 1:                  # not mapped yet
            return
        compact = width < S(self.TABS_COMPACT_AT)
        if compact == self._tabs_compact:
            return
        self._tabs_compact = compact

        if compact:
            self._brand_holder.pack_forget()
            self._el_status.pack_forget()
            self._el_dot.pack_configure(padx=0)
        else:
            self._brand_holder.pack(side="left", fill="y",
                                    padx=(S(13), S(12)), before=self._tabs)
            self._el_dot.pack_configure(padx=(0, S(6)))
            self._el_status.pack(side="left")
        self._draw_tabs()

    def _show_mode(self, index: int):
        self._mode_index = index
        self._draw_tabs()

        _glyph, _name, blurb, subtitle = self._MODES[index]
        self._mode_title_var.set(blurb)
        self._subtitle_var.set(subtitle)
        for i, frame in enumerate((self._tab_solo, self._tab_dlg, self._tab_dub)):
            if i == index:
                frame.pack(fill="both", expand=True)
            else:
                frame.pack_forget()

    def _build_mode_header(self, parent):
        """The strip under the tabs: the short line the rail used to carry under
        each mode's name, and the longer one saying what the mode actually does.

        The name itself is on the tab now, so it is not repeated here — a title
        saying the same word as the tab two pixels above it is a line of chrome
        that tells you nothing."""
        bar = tk.Frame(parent, bg=PANEL, height=S(40), bd=0)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)
        tk.Frame(bar, bg=RAIL_ACCENT, width=S(2), height=S(20)).pack(
            side="left", padx=(S(15), S(11)), pady=S(10))
        self._mode_title_var = tk.StringVar(value=self._MODES[0][2])
        tk.Label(bar, textvariable=self._mode_title_var, bg=PANEL,
                 fg=RAIL_ACCENT, font=F_MONO9B).pack(side="left")
        tk.Label(bar, text="·", bg=PANEL, fg=TEXT_FAINT,
                 font=F_MONO9).pack(side="left", padx=S(10))
        self._subtitle_var = tk.StringVar(value=self._MODES[0][3])
        tk.Label(bar, textvariable=self._subtitle_var, bg=PANEL, fg=TEXT_FAINT,
                 font=F_MONO9).pack(side="left")
        tk.Frame(parent, bg=PANEL_BORDER, height=S(1)).pack(fill="x", side="top")

    # ── key + voices, at the right end of the strip ───────────────────
    def _build_key_status(self, strip):
        """A dot and a word, where a browser puts its account button.

        The key itself lives one click away, because a 26-character field you
        fill in once a month does not earn a permanent strip across the top of
        the window. It is a pill rather than bare text so that it reads as the
        one thing up here you can press that is not a tab."""
        holder = tk.Frame(strip, bg=RAIL_BG)
        holder.pack(side="right", fill="y", padx=(S(10), S(12)))
        pill = tk.Frame(holder, bg=RAIL_SEL, bd=0,
                        highlightbackground=PANEL_BORDER, highlightthickness=1)
        pill.pack(expand=True)          # vertically centred in the strip
        inner = tk.Frame(pill, bg=RAIL_SEL)
        inner.pack(padx=S(9), pady=S(4))
        self._el_dot = tk.Label(inner, text="●", bg=RAIL_SEL, fg=TEXT_MUTED,
                                font=F_MONO9)
        self._el_dot.pack(side="left", padx=(0, S(6)))
        self._el_status = tk.Label(inner, text="no key", bg=RAIL_SEL,
                                   fg=TEXT_MUTED, font=F_MONO9B, anchor="w")
        self._el_status.pack(side="left")
        self._el_pill_parts = (pill, inner, self._el_dot, self._el_status)

        self._el_tip = _Tooltip(self._el_status)
        self._el_tip.set("No ElevenLabs API key yet. Click here and paste one — "
                         "it is validated and saved automatically.")
        for w in (holder, pill, inner, self._el_dot, self._el_status):
            w.configure(cursor="hand2")
            w.bind("<Button-1>", self._toggle_key_popover)
            w.bind("<Enter>", lambda _e: self._key_pill_hover(True))
            w.bind("<Leave>", lambda _e: self._key_pill_hover(False))

    def _key_pill_hover(self, entering: bool):
        bg = PANEL2 if entering else RAIL_SEL
        for w in self._el_pill_parts:
            w.configure(bg=bg)

    def _toggle_key_popover(self, _event=None):
        if self._key_pop is not None and self._key_pop.winfo_exists():
            self._close_key_popover()
        else:
            self._open_key_popover()

    def _open_key_popover(self):
        pop = tk.Toplevel(self.root)
        self._key_pop = pop
        # No transient() here: on Windows it pins an overrideredirect toplevel
        # to 0,0 and wm_geometry stops taking. The window-move binding below
        # does the job transient() would have done.
        pop.wm_overrideredirect(True)
        pop.configure(bg="#5b4fbf")

        card = tk.Frame(pop, bg=SVO_PANEL)
        card.pack(padx=S(1), pady=S(1), fill="both", expand=True)
        tk.Label(card, text="ELEVENLABS ACCOUNT", bg=SVO_PANEL, fg=REG_LABEL,
                 font=F_MONO9B, anchor="w").pack(
            fill="x", padx=S(14), pady=(S(12), S(8)))

        tk.Label(card, text="API key", bg=SVO_PANEL, fg=TEXT_MUTED,
                 font=F_UI9, anchor="w").pack(fill="x", padx=S(14))
        entry = tk.Entry(card, textvariable=self._api_key_var, width=34,
                         bg=INPUT_BG, fg=INPUT_FG, insertbackground=INPUT_FG,
                         relief="flat", font=F_UI9, show="•")
        entry.pack(fill="x", padx=S(14), pady=(S(4), S(10)))
        entry.bind("<<Paste>>",
                   lambda _e: self.root.after(50, self._on_api_key_changed))
        entry.bind("<Return>", lambda _e: self._on_api_key_changed())

        row = tk.Frame(card, bg=SVO_PANEL)
        row.pack(fill="x", padx=S(14))
        self._btn(row, "Validate", self._on_api_key_changed,
                  bg="#172554", fg=REG_LABEL, abg="#1e3a8a").pack(side="left")
        self._btn(row, "↻  Reload Voices", self._refresh_voices,
                  bg=TR_ACCENT, fg="#052e16", abg="#16a34a").pack(
            side="left", padx=(S(8), S(0)))

        self._pop_status = tk.Label(card, text=self._el_status.cget("text"),
                                    bg=SVO_PANEL, fg=self._el_status.cget("fg"),
                                    font=F_MONO9B, anchor="w")
        self._pop_status.pack(fill="x", padx=S(14), pady=(S(12), S(2)))
        self._pop_detail = tk.Label(card, text=self._el_tip.text, bg=SVO_PANEL,
                                    fg=TEXT_FAINT, font=F_UI8,
                                    anchor="w", justify="left", wraplength=S(300))
        self._pop_detail.pack(fill="x", padx=S(14), pady=(S(0), S(13)))

        # Dropped from the status pill it belongs to and right-aligned with it,
        # the way a browser's account menu is, then nudged back on screen if the
        # window is sitting near an edge.
        pop.update_idletasks()
        h, w = pop.winfo_reqheight(), pop.winfo_reqwidth()
        x = self._el_status.winfo_rootx() + self._el_status.winfo_width() - w + S(12)
        y = self._el_status.winfo_rooty() + self._el_status.winfo_height() + S(12)
        y = max(20, min(y, self.root.winfo_screenheight() - h - 20))
        x = max(0, min(x, self.root.winfo_screenwidth() - w))
        pop.wm_geometry(f"+{x}+{y}")
        pop.lift()

        pop.bind("<Escape>", lambda _e: self._close_key_popover())
        pop.bind("<FocusOut>", self._popover_focus_out)
        # A detached window would otherwise sit still while the app moves. Tk
        # fires <Configure> for plenty of things that are not a move, so the
        # window's actual geometry is what decides.
        self._pop_anchor = self._root_geometry()
        self._pop_binds = [self.root.bind("<Configure>", self._popover_follow,
                                          add="+"),
                           self.root.bind("<Unmap>",
                                          lambda _e: self._close_key_popover(),
                                          add="+")]
        entry.focus_set()

    def _root_geometry(self):
        return (self.root.winfo_rootx(), self.root.winfo_rooty(),
                self.root.winfo_width(), self.root.winfo_height())

    def _popover_follow(self, _event=None):
        """The popover is anchored to a point in the window; if the window has
        actually moved or resized, that anchor is gone."""
        if self._key_pop is not None and self._root_geometry() != self._pop_anchor:
            self._close_key_popover()

    def _popover_focus_out(self, _event=None):
        """Close when focus lands anywhere outside the popover — but not while
        it is only moving between the popover's own entry and buttons, which
        also fires FocusOut."""
        def _check():
            pop = self._key_pop
            if pop is None or not pop.winfo_exists():
                return
            try:
                focused = pop.focus_displayof()
            except (KeyError, tk.TclError):
                focused = None
            path = str(focused) if focused is not None else ""
            if not path.startswith(str(pop)):
                self._close_key_popover()
        self.root.after(80, _check)

    def _close_key_popover(self):
        pop, self._key_pop = self._key_pop, None
        self._pop_status = self._pop_detail = None
        for seq, funcid in zip(("<Configure>", "<Unmap>"), self._pop_binds):
            try:
                self.root.unbind(seq, funcid)
            except tk.TclError:
                pass
        self._pop_binds = []
        if pop is not None:
            try:
                pop.destroy()
            except tk.TclError:
                pass
        # Closing is as good a moment as any to act on a key that was typed and
        # never submitted.
        self._on_api_key_changed()

    # ═════════════════════════════════════════════════════════════════════════
    #  Tab 1 — the original single-speaker pipeline, unchanged
    # ═════════════════════════════════════════════════════════════════════════
    def _build_solo_tab(self):
        root = self._tab_solo

        # ── Step 1 panel — TTS ───────────────────────────────────────────────
        s1 = tk.Frame(root, bg=SVO_PANEL, bd=0,
                      highlightbackground=WARN_AMBER, highlightthickness=1)
        s1.pack(fill="x", side="top", pady=(S(4), S(0)))
        s1r = tk.Frame(s1, bg=SVO_PANEL, height=S(42))
        s1r.pack(fill="x")
        s1r.pack_propagate(False)
        tk.Label(s1r, text="①  STEP 1 · TTS", bg=SVO_PANEL, fg="#fbbf24",
                 font=F_MONO9B).pack(side="left", padx=(S(14), S(10)), pady=S(10))
        tk.Frame(s1r, bg=WARN_AMBER, width=S(2), height=S(24)).pack(side="left", padx=(S(0), S(12)), pady=S(9))
        tk.Label(s1r, text="Voice:", bg=SVO_PANEL, fg=TEXT,
                 font=F_UI9).pack(side="left", padx=(S(0), S(4)))
        self._step1_cb = ttk.Combobox(s1r, textvariable=self._step1_voice_var,
                                      state="readonly", width=38, font=F_UI9)
        self._step1_cb.pack(side="left", padx=(S(0), S(6)))
        self._step1_cb.bind("<<ComboboxSelected>>", lambda _e: self._pick_voice(1))
        self._btn(s1r, f"↺ {STEP1_VOICE_NAME}", lambda: self._reset_voice(1),
                  bg="#3b2f10", fg=STEP1_GOLD, abg="#713f12").pack(
            side="left", padx=(S(0), S(10)), pady=S(6))
        tk.Label(s1r, text="Model:", bg=SVO_PANEL, fg=TEXT,
                 font=F_UI9).pack(side="left", padx=(S(0), S(4)))
        self._step1_model_disp = tk.StringVar(
            value=ELEVENLABS_TTS_MODELS.get(self._step1_model_var.get(),
                                            self._step1_model_var.get()))
        s1_model_cb = ttk.Combobox(s1r, textvariable=self._step1_model_disp,
                                   state="readonly", width=26, font=F_UI9,
                                   values=list(ELEVENLABS_TTS_MODELS.values()))
        s1_model_cb.pack(side="left", padx=(S(0), S(10)))
        s1_model_cb.bind("<<ComboboxSelected>>", lambda _e: self._pick_model(1))
        tk.Checkbutton(s1r, text=f"Emotion tags ({VO_LANGUAGE})",
                       variable=self._emotion_var, command=self._on_emotion_toggle,
                       bg=SVO_PANEL, fg=TEXT, selectcolor=SVO_PANEL,
                       activebackground=SVO_PANEL, activeforeground="#fbbf24",
                       font=F_UI9).pack(side="left", padx=(S(0), S(8)))
        self._note1 = tk.Label(s1, text="", bg=SVO_PANEL, fg=TEXT_MUTED,
                               font=F_UI8, anchor="w")
        self._note1.pack(fill="x", padx=S(16), pady=(S(0), S(5)))

        # ── Step 2 panel — Voice Changer ─────────────────────────────────────
        s2 = tk.Frame(root, bg=SVO_PANEL, bd=0,
                      highlightbackground=STEP2_VIOLET, highlightthickness=1)
        s2.pack(fill="x", side="top", pady=(S(4), S(0)))
        s2r = tk.Frame(s2, bg=SVO_PANEL, height=S(42))
        s2r.pack(fill="x")
        s2r.pack_propagate(False)
        tk.Label(s2r, text="②  STEP 2 · VOICE CHANGE", bg=SVO_PANEL, fg=STEP2_VIOLET,
                 font=F_MONO9B).pack(side="left", padx=(S(14), S(10)), pady=S(10))
        tk.Frame(s2r, bg="#5b4fbf", width=S(2), height=S(24)).pack(side="left", padx=(S(0), S(12)), pady=S(9))
        tk.Label(s2r, text="Target Voice:", bg=SVO_PANEL, fg=TEXT,
                 font=F_UI9).pack(side="left", padx=(S(0), S(4)))
        self._step2_cb = ttk.Combobox(s2r, textvariable=self._step2_voice_var,
                                      state="readonly", width=38, font=F_UI9)
        self._step2_cb.pack(side="left", padx=(S(0), S(6)))
        self._step2_cb.bind("<<ComboboxSelected>>", lambda _e: self._pick_voice(2))
        self._btn(s2r, f"↺ {STEP2_VOICE_NAME}", lambda: self._reset_voice(2),
                  bg=SVO_PANEL, fg=STEP2_VIOLET, abg="#3d3580").pack(
            side="left", padx=(S(0), S(10)), pady=S(6))
        tk.Label(s2r, text="STS Model:", bg=SVO_PANEL, fg=TEXT,
                 font=F_UI9).pack(side="left", padx=(S(0), S(4)))
        self._step2_model_disp = tk.StringVar(
            value=ELEVENLABS_STS_MODELS.get(self._step2_model_var.get(),
                                            self._step2_model_var.get()))
        s2_model_cb = ttk.Combobox(s2r, textvariable=self._step2_model_disp,
                                   state="readonly", width=30, font=F_UI9,
                                   values=list(ELEVENLABS_STS_MODELS.values()))
        s2_model_cb.pack(side="left", padx=(S(0), S(10)))
        s2_model_cb.bind("<<ComboboxSelected>>", lambda _e: self._pick_model(2))
        tk.Checkbutton(s2r, text="Keep Step-1 audio",
                       variable=self._keep_step1_var,
                       bg=SVO_PANEL, fg=TEXT, selectcolor=SVO_PANEL,
                       activebackground=SVO_PANEL, activeforeground=STEP2_VIOLET,
                       font=F_UI9).pack(side="left", padx=(S(0), S(8)))
        self._note2 = tk.Label(s2, text="", bg=SVO_PANEL, fg=TEXT_MUTED,
                               font=F_UI8, anchor="w")
        self._note2.pack(fill="x", padx=S(16), pady=(S(0), S(5)))

        # ── Final output path bar ────────────────────────────────────────────
        out_bar = tk.Frame(root, bg=PANEL2, height=S(38), bd=0,
                           highlightbackground=PANEL_BORDER, highlightthickness=1)
        out_bar.pack(fill="x")
        out_bar.pack_propagate(False)
        tk.Label(out_bar, text="Final Output:", bg=PANEL2, fg=TEXT_FAINT,
                 font=F_MONO9).pack(side="left", padx=(S(14), S(6)), pady=S(8))
        self._out_label = tk.Label(
            out_bar, text="No output path chosen — will be prompted on Run",
            bg=PANEL2, fg=TEXT_MUTED, font=F_MONO9, anchor="w")
        self._out_label.pack(side="left", fill="x", expand=True, padx=S(4))
        self._btn(out_bar, "Choose Path", self._pick_output,
                  bg=BTN_BG, fg=BTN_FG, abg=BTN_ACT).pack(side="right", padx=S(10), pady=S(7))

        # ── Step-1 audio bar ─────────────────────────────────────────────────
        mid_bar = tk.Frame(root, bg=PANEL2, height=S(38), bd=0,
                           highlightbackground=PANEL_BORDER, highlightthickness=1)
        mid_bar.pack(fill="x")
        mid_bar.pack_propagate(False)
        tk.Label(mid_bar, text="Step-1 Audio:", bg=PANEL2, fg=TEXT_FAINT,
                 font=F_MONO9).pack(side="left", padx=(S(14), S(6)), pady=S(8))
        self._step1_label = tk.Label(mid_bar, text=_STEP1_AUTO_NOTE, bg=PANEL2,
                                     fg=TEXT_MUTED, font=F_MONO9, anchor="w")
        self._step1_label.pack(side="left", fill="x", expand=True, padx=S(4))
        self._btn(mid_bar, "Clear", self._clear_step1,
                  bg=BTN_BG, fg=BTN_FG, abg=BTN_ACT).pack(side="right", padx=(S(0), S(10)), pady=S(7))
        self._btn(mid_bar, "Choose Audio", self._pick_step1,
                  bg=BTN_BG, fg=BTN_FG, abg=BTN_ACT).pack(side="right", padx=S(4), pady=S(7))

        # ── Script text area ─────────────────────────────────────────────────
        text_frame = tk.Frame(root, bg=BG)
        text_frame.pack(fill="both", expand=True, padx=S(10), pady=(S(8), S(4)))
        head = tk.Frame(text_frame, bg=BG)
        head.pack(fill="x", padx=S(2), pady=(S(4), S(2)))
        tk.Label(head, text=f"Script  ({VO_LANGUAGE} text for Step 1):",
                 bg=BG, fg=TEXT, font=F_MONO9B).pack(side="left")
        self._btn(head, "Load .txt", self._load_script,
                  bg=BTN_BG, fg=BTN_FG, abg=BTN_ACT).pack(side="right")
        self._text = scrolledtext.ScrolledText(
            text_frame, wrap="word",
            bg=INPUT_BG, fg=INPUT_FG, insertbackground=INPUT_FG,
            font=F_MONO10, relief="flat", bd=0,
            selectbackground="#1e3a8a", selectforeground="#f8fafc",
            height=12)
        self._text.pack(fill="both", expand=True, pady=(S(0), S(4)))

        # ── Per-step progress ────────────────────────────────────────────────
        prog = tk.Frame(root, bg="#162032", bd=0,
                        highlightbackground="#3b82f6", highlightthickness=1)
        prog.pack(fill="x")
        self._step_vars: Dict[str, tk.StringVar] = {}
        for tag, desc in (("1", f"TTS — {STEP1_VOICE_NAME}"),
                          ("2", "Voice Change — Speech-to-Speech")):
            row = tk.Frame(prog, bg="#162032")
            row.pack(fill="x", padx=S(14), pady=S(2))
            tk.Label(row, text=f" [S{tag}]", bg="#162032", fg="#60a5fa",
                     font=F_MONO9B, width=6, anchor="w").pack(side="left")
            tk.Label(row, text=desc, bg="#162032", fg=TEXT_FAINT,
                     font=F_MONO9, width=40, anchor="w").pack(side="left")
            var = tk.StringVar(value="—")
            tk.Label(row, textvariable=var, bg="#162032", fg=TEXT_MUTED,
                     font=F_MONO9, anchor="w").pack(
                side="left", fill="x", expand=True, padx=(S(8), S(0)))
            self._step_vars[tag] = var

        # ── Bottom action bar ────────────────────────────────────────────────
        bot_bar = tk.Frame(root, bg=PANEL, height=S(48), bd=0,
                           highlightbackground=PANEL_BORDER, highlightthickness=1)
        bot_bar.pack(fill="x", side="bottom")
        bot_bar.pack_propagate(False)
        run_btn = self._btn(bot_bar, "  ▶  Run Both Steps  ",
                            lambda: self._run(MODE_BOTH),
                            bg="#0f1d14", fg=TR_ACCENT, abg="#1f4d2e")
        run_btn.pack(side="left", padx=(S(14), S(6)), pady=S(8))
        b1 = self._btn(bot_bar, "①  TTS Only", lambda: self._run(MODE_TTS),
                       bg="#3b2f10", fg=STEP1_GOLD, abg="#713f12")
        b1.pack(side="left", padx=(S(0), S(6)), pady=S(8))
        b2 = self._btn(bot_bar, "②  Voice Change Only", lambda: self._run(MODE_VC),
                       bg=SVO_PANEL, fg=STEP2_VIOLET, abg="#3d3580")
        b2.pack(side="left", padx=(S(0), S(6)), pady=S(8))
        self._btn(bot_bar, "▶ Play Output", self._play_output,
                  bg=BTN_BG, fg=BTN_FG, abg=BTN_ACT).pack(side="left", padx=(S(0), S(6)), pady=S(8))
        self._buttons = [run_btn, b1, b2]
        self._status = tk.Label(bot_bar, text="", bg=PANEL, fg=ACCENT,
                                font=F_MONO9, anchor="w")
        self._status.pack(side="left", fill="x", expand=True, padx=S(8))


    # ═════════════════════════════════════════════════════════════════════════
    #  Tab 2 — Dialogue (multi-speaker)
    # ═════════════════════════════════════════════════════════════════════════
    def _build_dialogue_tab(self):
        root = self._tab_dlg

        # ── Output bar ───────────────────────────────────────────────────────
        out_bar = tk.Frame(root, bg=PANEL2, height=S(38), bd=0,
                           highlightbackground=PANEL_BORDER, highlightthickness=1)
        out_bar.pack(fill="x")
        out_bar.pack_propagate(False)
        tk.Label(out_bar, text="Master Output:", bg=PANEL2, fg=TEXT_FAINT,
                 font=F_MONO9).pack(side="left", padx=(S(14), S(6)), pady=S(8))
        self._dlg_out_label = tk.Label(
            out_bar, text="No output path chosen — will be prompted on Render",
            bg=PANEL2, fg=TEXT_MUTED, font=F_MONO9, anchor="w")
        self._dlg_out_label.pack(side="left", fill="x", expand=True, padx=S(4))
        self._btn(out_bar, "Choose Path", self._dlg_pick_output,
                  bg=BTN_BG, fg=BTN_FG, abg=BTN_ACT).pack(side="right", padx=S(10), pady=S(7))

        # ── Script box ───────────────────────────────────────────────────────
        script_frame = tk.Frame(root, bg=BG)
        script_frame.pack(fill="both", expand=True, padx=S(10), pady=(S(8), S(0)))
        head = tk.Frame(script_frame, bg=BG)
        head.pack(fill="x", padx=S(2), pady=(S(4), S(2)))
        tk.Label(head, text="Script  (one line per turn, written as  NAME: text):",
                 bg=BG, fg=TEXT, font=F_MONO9B).pack(side="left")
        self._btn(head, "Load .txt", self._dlg_load_script,
                  bg=BTN_BG, fg=BTN_FG, abg=BTN_ACT).pack(side="right")
        self._btn(head, "⟳ Detect Speakers", self._dlg_detect_speakers,
                  bg=TR_ACCENT, fg="#052e16", abg="#16a34a").pack(side="right", padx=S(6))
        self._dlg_text = scrolledtext.ScrolledText(
            script_frame, wrap="word",
            bg=INPUT_BG, fg=INPUT_FG, insertbackground=INPUT_FG,
            font=F_MONO10, relief="flat", bd=0,
            selectbackground="#1e3a8a", selectforeground="#f8fafc",
            height=9)
        self._dlg_text.pack(fill="both", expand=True, pady=(S(0), S(2)))
        self._dlg_text.insert("1.0", _DLG_PLACEHOLDER)
        # Re-detect shortly after typing stops rather than on every keystroke —
        # parsing on each key would re-create every cast row mid-edit and steal
        # focus from whatever dropdown the user had open.
        self._dlg_text.bind("<KeyRelease>", self._dlg_schedule_detect)

        self._dlg_parse_note = tk.Label(script_frame, text="", bg=BG,
                                        fg=TEXT_MUTED, font=F_UI8,
                                        anchor="w", justify="left")
        self._dlg_parse_note.pack(fill="x", padx=S(2), pady=(S(0), S(4)))

        # ── Cast table ───────────────────────────────────────────────────────
        cast_frame = tk.Frame(root, bg=BG)
        cast_frame.pack(fill="both", expand=True, padx=S(10), pady=(S(2), S(0)))
        chead = tk.Frame(cast_frame, bg=BG)
        chead.pack(fill="x", padx=S(2), pady=(S(2), S(2)))
        tk.Label(chead, text="Cast  (each speaker gets their own voice and "
                             "their own number of steps):",
                 bg=BG, fg=TEXT, font=F_MONO9B).pack(side="left")
        self._dlg_cast_note = tk.Label(chead, text="", bg=BG, fg=TEXT_MUTED,
                                       font=F_UI8)
        self._dlg_cast_note.pack(side="right")

        # Column headers, aligned with the row widgets below.
        hdr = tk.Frame(cast_frame, bg=PANEL2)
        hdr.pack(fill="x", padx=S(2))
        for text, width in (("SPEAKER", 16), ("STEPS", 26), ("VOICE (Step 1)", 34),
                            ("TARGET VOICE (Step 2)", 34), ("DELIVERY NOTE", 22)):
            tk.Label(hdr, text=text, bg=PANEL2, fg=TEXT_FAINT,
                     font=F_MONO8B, width=width, anchor="w"
                     ).pack(side="left", padx=(S(6), S(0)), pady=S(3))

        # A canvas so a long cast scrolls instead of squeezing the script box.
        canvas_wrap = tk.Frame(cast_frame, bg=BG, height=S(132))
        canvas_wrap.pack(fill="both", expand=True, padx=S(2))
        canvas_wrap.pack_propagate(False)
        self._cast_canvas = tk.Canvas(canvas_wrap, bg=BG, highlightthickness=0, bd=0)
        cast_scroll = ttk.Scrollbar(canvas_wrap, orient="vertical",
                                    command=self._cast_canvas.yview)
        self._cast_canvas.configure(yscrollcommand=cast_scroll.set)
        cast_scroll.pack(side="right", fill="y")
        self._cast_canvas.pack(side="left", fill="both", expand=True)
        self._cast_inner = tk.Frame(self._cast_canvas, bg=BG)
        self._cast_window = self._cast_canvas.create_window(
            (0, 0), window=self._cast_inner, anchor="nw")
        self._cast_inner.bind(
            "<Configure>",
            lambda _e: self._cast_canvas.configure(
                scrollregion=self._cast_canvas.bbox("all")))
        self._cast_canvas.bind(
            "<Configure>",
            lambda e: self._cast_canvas.itemconfigure(self._cast_window, width=e.width))

        # ── Options row ──────────────────────────────────────────────────────
        opt = tk.Frame(root, bg=SVO_PANEL, bd=0,
                       highlightbackground="#5b4fbf", highlightthickness=1)
        opt.pack(fill="x", pady=(S(6), S(0)))
        orow = tk.Frame(opt, bg=SVO_PANEL, height=S(40))
        orow.pack(fill="x")
        orow.pack_propagate(False)

        def _chk(text, var, fg=STEP2_VIOLET, cmd=None):
            tk.Checkbutton(orow, text=text, variable=var, command=cmd,
                           bg=SVO_PANEL, fg=TEXT, selectcolor=SVO_PANEL,
                           activebackground=SVO_PANEL, activeforeground=fg,
                           font=F_UI9).pack(side="left", padx=(S(10), S(0)))

        _chk(f"Emotion tags ({VO_LANGUAGE})", self._dlg_emotion_var,
             "#fbbf24", self._dlg_on_emotion_toggle)
        _chk("Merge same speaker", self._dlg_merge_var)
        _chk("Match loudness", self._dlg_loudness_var)
        _chk("Stems", self._dlg_stems_var)
        _chk("Manifest", self._dlg_manifest_var)

        tk.Label(orow, text="Gap ms — same:", bg=SVO_PANEL, fg=TEXT_MUTED,
                 font=F_UI9).pack(side="left", padx=(S(14), S(3)))
        tk.Entry(orow, textvariable=self._dlg_gap_same_var, width=5,
                 bg=INPUT_BG, fg=INPUT_FG, insertbackground=INPUT_FG,
                 relief="flat", font=F_UI9).pack(side="left")
        tk.Label(orow, text="switch:", bg=SVO_PANEL, fg=TEXT_MUTED,
                 font=F_UI9).pack(side="left", padx=(S(8), S(3)))
        tk.Entry(orow, textvariable=self._dlg_gap_switch_var, width=5,
                 bg=INPUT_BG, fg=INPUT_FG, insertbackground=INPUT_FG,
                 relief="flat", font=F_UI9).pack(side="left")

        # ── Progress log ─────────────────────────────────────────────────────
        prog = tk.Frame(root, bg="#162032", bd=0,
                        highlightbackground="#3b82f6", highlightthickness=1)
        prog.pack(fill="both", expand=True, pady=(S(6), S(0)))
        phead = tk.Frame(prog, bg="#162032")
        phead.pack(fill="x")
        tk.Label(phead, text=" PROGRESS", bg="#162032", fg="#60a5fa",
                 font=F_MONO8B).pack(side="left", padx=S(10), pady=(S(4), S(0)))
        self._dlg_progress_var = tk.StringVar(value="—")
        tk.Label(phead, textvariable=self._dlg_progress_var, bg="#162032",
                 fg=TEXT_MUTED, font=F_MONO8).pack(side="left", padx=S(8), pady=(S(4), S(0)))
        self._dlg_log = scrolledtext.ScrolledText(
            prog, wrap="none", bg="#0d1526", fg=TEXT_MUTED,
            insertbackground=TEXT_MUTED, font=F_MONO8,
            relief="flat", bd=0, height=6, state="disabled")
        self._dlg_log.pack(fill="both", expand=True, padx=S(8), pady=(S(2), S(8)))

        # ── Action bar ───────────────────────────────────────────────────────
        bot = tk.Frame(root, bg=PANEL, height=S(48), bd=0,
                       highlightbackground=PANEL_BORDER, highlightthickness=1)
        bot.pack(fill="x", side="bottom")
        bot.pack_propagate(False)
        run_btn = self._btn(bot, "  ▶  Render Dialogue  ", self._dlg_run,
                            bg="#0f1d14", fg=TR_ACCENT, abg="#1f4d2e")
        run_btn.pack(side="left", padx=(S(14), S(6)), pady=S(8))
        self._dlg_buttons = [run_btn]
        self._btn(bot, "▶ Play Master", self._dlg_play,
                  bg=BTN_BG, fg=BTN_FG, abg=BTN_ACT).pack(side="left", padx=(S(0), S(6)), pady=S(8))
        self._btn(bot, "📁 Open Folder", self._dlg_open_folder,
                  bg=BTN_BG, fg=BTN_FG, abg=BTN_ACT).pack(side="left", padx=(S(0), S(6)), pady=S(8))
        self._dlg_status = tk.Label(bot, text="", bg=PANEL, fg=ACCENT,
                                    font=F_MONO9, anchor="w")
        self._dlg_status.pack(side="left", fill="x", expand=True, padx=S(8))

        self._dlg_detect_speakers()

    # ── Cast table plumbing ──────────────────────────────────────────────────
    def _dlg_schedule_detect(self, _event=None):
        """Debounce re-parsing so it happens after typing stops, not during."""
        if self._detect_job is not None:
            try:
                self.root.after_cancel(self._detect_job)
            except tk.TclError:
                pass
        self._detect_job = self.root.after(700, self._dlg_detect_speakers)

    def _dlg_detect_speakers(self):
        """Parse the script and rebuild the cast table to match it."""
        self._detect_job = None
        script = self._dlg_text.get("1.0", "end").strip()
        if not script:
            self._dlg_speakers = []
            self._dlg_parse_note.config(text="Script is empty.", fg=TEXT_MUTED)
            self._rebuild_cast_rows()
            return
        try:
            turns = parse_script(script)
        except ScriptFormatError as e:
            self._dlg_speakers = []
            self._dlg_parse_note.config(
                text=str(e).replace("\n", "   "), fg=WARN_AMBER)
            self._rebuild_cast_rows()
            return

        self._dlg_speakers = speakers_in(turns)
        # Merge in defaults for anyone new, keeping every voice already chosen.
        self._cast = cast_for_speakers(self._dlg_speakers, self._cast)
        chars = sum(len(t.text) for t in turns)
        self._dlg_parse_note.config(
            text=f"✓ {len(turns)} turn(s), {len(self._dlg_speakers)} speaker(s), "
                 f"{chars} characters.", fg=TR_ACCENT)
        self._rebuild_cast_rows()

    def _rebuild_cast_rows(self):
        """Recreate one row per speaker in the current script."""
        for row in self._cast_rows:
            row["frame"].destroy()
        self._cast_rows = []

        if not self._dlg_speakers:
            self._dlg_cast_note.config(text="")
            return

        for name in self._dlg_speakers:
            self._cast_rows.append(self._make_cast_row(name))
        self._sync_cast_voice_lists()
        self._update_cast_note()

    def _make_cast_row(self, speaker: str, parent=None) -> dict:
        """One editable cast row.

        *parent* defaults to the Dialogue tab's table. Dub Sync passes its own
        frame and gets identical rows editing the same shared cast — one
        cast.json, two places to see it, no second implementation to drift.
        """
        key = normalize_speaker(speaker)
        recipe = self._cast[key]

        frame = tk.Frame(parent if parent is not None else self._cast_inner, bg=BG)
        frame.pack(fill="x", pady=S(1))

        tk.Label(frame, text=speaker, bg=BG,
                 fg=STEP1_GOLD if recipe.two_step else REG_LABEL,
                 font=F_MONO9B, width=16, anchor="w"
                 ).pack(side="left", padx=(S(6), S(0)))

        mode_var = tk.StringVar(value=CAST_MODE_LABELS[recipe.mode])
        mode_cb = ttk.Combobox(frame, textvariable=mode_var, state="readonly",
                               width=24, font=F_UI8,
                               values=[CAST_MODE_LABELS[m] for m in CAST_MODES])
        mode_cb.pack(side="left", padx=(S(6), S(0)))

        v1_var = tk.StringVar()
        v1_cb = ttk.Combobox(frame, textvariable=v1_var, state="readonly",
                             width=32, font=F_UI8)
        v1_cb.pack(side="left", padx=(S(6), S(0)))

        v2_var = tk.StringVar()
        v2_cb = ttk.Combobox(frame, textvariable=v2_var, state="readonly",
                             width=32, font=F_UI8)
        v2_cb.pack(side="left", padx=(S(6), S(0)))

        style_var = tk.StringVar(value=recipe.style)
        style_entry = tk.Entry(frame, textvariable=style_var, width=22,
                               bg=INPUT_BG, fg=INPUT_FG, insertbackground=INPUT_FG,
                               relief="flat", font=F_UI8)
        style_entry.pack(side="left", padx=(S(6), S(0)))

        row = {"frame": frame, "key": key, "speaker": speaker,
               "mode_var": mode_var, "v1_var": v1_var, "v2_var": v2_var,
               "style_var": style_var, "v1_cb": v1_cb, "v2_cb": v2_cb}

        mode_cb.bind("<<ComboboxSelected>>", lambda _e, r=row: self._cast_changed(r))
        v1_cb.bind("<<ComboboxSelected>>", lambda _e, r=row: self._cast_changed(r))
        v2_cb.bind("<<ComboboxSelected>>", lambda _e, r=row: self._cast_changed(r))
        style_entry.bind("<FocusOut>", lambda _e, r=row: self._cast_changed(r))
        return row

    def _cast_changed(self, row: dict):
        """Pull one row's widgets back into its recipe and persist the cast."""
        recipe = self._cast.get(row["key"])
        if recipe is None:
            return

        for mode, label in CAST_MODE_LABELS.items():
            if label == row["mode_var"].get():
                recipe.mode = mode
                break

        # Resolve display labels back to voice ids through the shared map, the
        # same way the single-speaker tab does — a truncated label can never
        # reach the API as a voice_id.
        for var, attr in ((row["v1_var"], "step1_voice"),
                          (row["v2_var"], "step2_voice")):
            vid = self._voice_id_for_label(var.get())
            if vid:
                setattr(recipe, attr, vid)

        recipe.style = row["style_var"].get().strip()
        write_cast(CAST_FILE, self._cast)
        self._sync_cast_voice_lists()
        self._update_cast_note()
        # Dub Sync shares this cast, so a change made in either table has to be
        # reflected in both. A no-op until that tab has a table.
        self._dub_cast_refresh()

    def _sync_cast_voice_lists(self):
        """Refresh every cast row's dropdowns from the loaded voice catalogue.

        Also greys out the target-voice column for one-step speakers, so the
        table shows at a glance who is going through Step 2.
        """
        rows = list(self._cast_rows) + list(getattr(self, "_dub_cast_rows", []))
        if not rows:
            return
        opts   = self._ordered_options()
        labels = [o["label"] for o in opts]
        by_id  = {o["voice_id"]: o["label"] for o in opts}

        for row in rows:
            recipe = self._cast.get(row["key"])
            if recipe is None:
                continue
            try:
                row["v1_cb"]["values"] = labels
                row["v2_cb"]["values"] = labels
                row["v1_var"].set(by_id.get(recipe.step1_voice,
                                            recipe.step1_voice or ""))
                row["v2_var"].set(by_id.get(recipe.step2_voice,
                                            recipe.step2_voice or ""))
                row["v2_cb"].config(state="readonly" if recipe.two_step else "disabled")
            except tk.TclError:
                continue

    def _update_cast_note(self):
        """Summarise the cast, and say plainly what is still missing."""
        problems = validate_cast(self._cast, self._dlg_speakers)
        two = sum(1 for n in self._dlg_speakers
                  if self._cast[normalize_speaker(n)].two_step)
        one = len(self._dlg_speakers) - two
        bits = []
        if two:
            bits.append(f"{two} × 2-step")
        if one:
            bits.append(f"{one} × 1-step")
        summary = ", ".join(bits)
        if problems:
            self._dlg_cast_note.config(
                text=f"{summary}  ·  {len(problems)} still need a voice", fg=WARN_AMBER)
        else:
            self._dlg_cast_note.config(text=f"{summary}  ·  ready", fg=TR_ACCENT)

    # ── Dialogue actions ─────────────────────────────────────────────────────
    def _dlg_on_emotion_toggle(self):
        if self._dlg_emotion_var.get():
            self._dlg_status.config(
                text=f"Emotion pass will use {llm_provider_label()} — one call "
                     "for the whole conversation", fg=TEXT_FAINT)
        else:
            self._dlg_status.config(text="", fg=ACCENT)

    def _dlg_pick_output(self):
        path = filedialog.asksaveasfilename(
            title="Save the assembled dialogue as…",
            defaultextension=".wav", initialfile="Dialogue.wav",
            filetypes=[("WAV audio", "*.wav"), ("MP3 audio", "*.mp3"),
                       ("All files", "*.*")])
        if path:
            self._dlg_out_path = path
            self._dlg_out_label.config(text=path, fg=TEXT)

    def _dlg_load_script(self):
        path = filedialog.askopenfilename(
            title="Load a dialogue script…",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            messagebox.showerror("Could Not Read File", str(e))
            return
        self._dlg_text.delete("1.0", "end")
        self._dlg_text.insert("1.0", content)
        self._dlg_status.config(text=f"Loaded {os.path.basename(path)}", fg=TR_ACCENT)
        self._dlg_detect_speakers()

    def _dlg_log_line(self, text: str):
        def _append():
            try:
                self._dlg_log.config(state="normal")
                self._dlg_log.insert("end", text + "\n")
                self._dlg_log.see("end")
                self._dlg_log.config(state="disabled")
            except tk.TclError:
                pass
        self._ui(_append)

    def _dlg_clear_log(self):
        try:
            self._dlg_log.config(state="normal")
            self._dlg_log.delete("1.0", "end")
            self._dlg_log.config(state="disabled")
        except tk.TclError:
            pass

    def _dlg_play(self):
        self._open_with_os(self._dlg_last_output, "Run a dialogue first.")

    def _dlg_open_folder(self):
        path = self._dlg_last_output or self._dlg_out_path
        if not path:
            messagebox.showinfo("Nothing Yet", "Choose an output path or run a "
                                               "dialogue first.")
            return
        self._open_with_os(os.path.dirname(os.path.abspath(path)),
                           "Nothing to open yet.")

    def _open_with_os(self, path: str, empty_msg: str):
        if not path or not os.path.exists(path):
            messagebox.showinfo("Nothing to Open", empty_msg)
            return
        try:
            if IS_WINDOWS:
                os.startfile(path)                      # noqa — Windows only
            elif IS_MAC:
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception:
            pass

    def _dlg_int(self, var, fallback: int) -> int:
        try:
            return max(0, int(str(var.get()).strip()))
        except (TypeError, ValueError):
            return fallback

    def _dlg_run(self):
        if self._dlg_running:
            return

        script = self._dlg_text.get("1.0", "end").strip()
        if not script:
            messagebox.showwarning("No Script",
                                   "Write the dialogue in the script box first.")
            return

        out_path = self._dlg_out_path
        if not out_path:
            out_path = filedialog.asksaveasfilename(
                title="Save the assembled dialogue as…",
                defaultextension=".wav", initialfile="Dialogue.wav",
                filetypes=[("WAV audio", "*.wav"), ("MP3 audio", "*.mp3"),
                           ("All files", "*.*")])
            if not out_path:
                return
            self._dlg_out_path = out_path
            self._dlg_out_label.config(text=out_path, fg=TEXT)

        try:
            api_key = get_api_key()
        except Exception as e:
            messagebox.showerror("ElevenLabs API Key Error", str(e))
            return

        # Everything checkable offline is checked before the first API call, so
        # a missing voice costs nothing instead of dying halfway through.
        try:
            req, turns = prepare_dialogue(DialogueRequest(
                script=script, out_path=os.path.abspath(out_path),
                api_key=api_key, cast=self._cast,
                emotion=bool(self._dlg_emotion_var.get()),
                merge_same_speaker=bool(self._dlg_merge_var.get()),
                match_loudness=bool(self._dlg_loudness_var.get()),
                write_stems=bool(self._dlg_stems_var.get()),
                write_manifest=bool(self._dlg_manifest_var.get()),
                gap_same_ms=self._dlg_int(self._dlg_gap_same_var,
                                          DIALOGUE_GAP_SAME_MS),
                gap_switch_ms=self._dlg_int(self._dlg_gap_switch_var,
                                            DIALOGUE_GAP_SWITCH_MS),
                language=VO_LANGUAGE, llm_model=GEMINI_DEFAULT_MODEL))
        except (ValueError, ScriptFormatError) as e:
            messagebox.showerror("Cannot Render", str(e))
            return

        write_cast(CAST_FILE, self._cast)

        self._dlg_running = True
        for b in self._dlg_buttons:
            b.config(state="disabled")
        self._dlg_clear_log()
        total = len(turns)
        self._dlg_progress_var.set(f"0 / {total} turns")
        self._dlg_status.config(text="Starting…", fg=TEXT_FAINT)

        def _status(msg, colour=ACCENT):
            self._ui(lambda m=msg, c=colour: self._dlg_status.config(text=m, fg=c))

        def _worker():
            try:
                def _on_status(msg: str) -> None:
                    self._dlg_log_line(msg)
                    _status(msg)

                def _on_turn(pos: int, count: int, _turn, _msg: str) -> None:
                    self._ui(lambda: self._dlg_progress_var.set(
                        f"{pos} / {count} turns"))

                result = run_dialogue(req, turns, status_cb=_on_status,
                                      turn_cb=_on_turn)

                self._dlg_last_output = result.master_path
                self._dlg_log_line("")
                self._dlg_log_line(f"master   : {result.master_path}")
                for key, path in sorted(result.stem_paths.items()):
                    self._dlg_log_line(f"stem     : {path}")
                if result.manifest_path:
                    self._dlg_log_line(f"manifest : {result.manifest_path}")
                self._dlg_log_line(f"turns    : {result.turn_dir}")
                for note in result.notes:
                    self._dlg_log_line(f"note     : {note}")

                _status(f"✔ Done → {os.path.basename(result.master_path)} "
                        f"({result.duration_ms / 1000.0:.1f}s, {total} turns)",
                        TR_ACCENT)
            except (ValueError, AssemblyError, RuntimeError) as exc:
                err = str(exc)
                self._dlg_log_line(f"ERROR: {err}")
                self._ui(lambda e=err: self._dlg_status.config(
                    text=f"Error: {e}", fg=ERR_RED))
                self._ui(lambda e=err: messagebox.showerror("Dialogue Error", e))
            except Exception as exc:                      # unexpected — still show it
                err = f"{type(exc).__name__}: {exc}"
                self._dlg_log_line(f"ERROR: {err}")
                self._ui(lambda e=err: self._dlg_status.config(
                    text=f"Error: {e}", fg=ERR_RED))
                self._ui(lambda e=err: messagebox.showerror("Dialogue Error", e))
            finally:
                self._dlg_running = False
                self._ui(lambda: [b.config(state="normal") for b in self._dlg_buttons])

        threading.Thread(target=_worker, daemon=True).start()

    # ═════════════════════════════════════════════════════════════════════════
    #  API key + voice list
    # ═════════════════════════════════════════════════════════════════════════
    def _set_el_status(self, text: str, colour: str = TEXT_MUTED,
                       detail: str = ""):
        """Set the strip's key/voice readout, with the long version on hover.

        Callers pass a glyph-prefixed string ("✔ 42 voices"); the glyph goes to
        the pill's dot and the words to the label beside it. When the popover is
        open it says the same thing, with the detail spelled out in full."""
        if self._closing:
            return
        glyph, label = "●", text
        if text[:1] in self._STATUS_GLYPHS:
            glyph, label = text[0], text[1:].strip()
        self._el_dot.config(text=glyph, fg=colour)
        self._el_status.config(text=label, fg=colour)
        self._el_tip.set(detail or label)
        if self._pop_status is not None and self._pop_status.winfo_exists():
            self._pop_status.config(text=f"{glyph}  {label}", fg=colour)
            self._pop_detail.config(text=detail or "")

    def _voice_summary(self, voices: List[Dict[str, str]]) -> str:
        """The full story, shown on hover rather than in the bar."""
        total = len(voices)
        tagged = sum(1 for v in voices if v["label"].lstrip("★ ").startswith("✦"))
        lines = [f"API key valid · {total} voice(s) on this account."]
        if tagged:
            lines.append(f"✦ {tagged} advertise {VO_LANGUAGE} support and sort "
                         f"to the top of every dropdown.")
        else:
            lines.append(f"None advertise {VO_LANGUAGE} — eleven_v3 auto-detects "
                         f"the language from the text, so any voice still works.")
        if self._fav_ids:
            lines.append(f"★ {len(self._fav_ids)} favourite(s) pinned above them.")
        lines.append("↻ Reload Voices after adding a voice in ElevenLabs.")
        return "\n".join(lines)

    def _on_api_key_changed(self):
        """Validate the key on a worker thread; on success fetch voices and
        populate both dropdowns."""
        key = (self._api_key_var.get() or "").strip()
        if not key:
            set_runtime_api_key(None)
            self._set_el_status("● no key", TEXT_MUTED,
                                "No ElevenLabs API key yet. Click the status at "
                                "the foot of the rail and paste one — it is "
                                "validated and saved automatically.")
            self._set_voice_options([])
            return

        # Same key we already validated — just re-show the cached list.
        cached = voice_cache_get(key, VO_LANGUAGE)
        if self._last_validated_key == key and cached is not None:
            self._set_voice_options(cached)
            self._set_el_status(f"✔ {len(cached)} voices", TR_ACCENT,
                                self._voice_summary(cached))
            return

        set_runtime_api_key(key)
        self._set_el_status("◌ checking…", WARN_AMBER,
                            "Validating the API key against ElevenLabs…")

        def _worker():
            try:
                validate_api_key(key)
            except Exception as e:
                self._ui(lambda err=str(e): self._set_el_status(
                    _short_error(err), ERR_RED, err))
                return
            self._ui(lambda: self._set_el_status(
                "◌ loading…", WARN_AMBER,
                f"Key accepted. Fetching the voice catalogue and sorting "
                f"{VO_LANGUAGE} voices to the top…"))
            try:
                voices = fetch_voices(key, VO_LANGUAGE, force_refresh=True)
            except Exception as e:
                self._ui(lambda err=str(e): self._set_el_status(
                    _short_error(err), ERR_RED, err))
                return

            write_api_key_file(key)

            def _apply():
                self._last_validated_key = key
                self._set_voice_options(voices)
                if voices:
                    self._set_el_status(f"✔ {len(voices)} voices", TR_ACCENT,
                                        self._voice_summary(voices))
                else:
                    self._set_el_status(
                        "✗ no voices", ERR_RED,
                        "The key is valid but this account has no voices. "
                        "Add or clone a voice in ElevenLabs, then hit "
                        "↻ Reload Voices.")

            self._ui(_apply)

        threading.Thread(target=_worker, daemon=True).start()

    def _refresh_voices(self):
        """Force-refresh the voice list."""
        key = (self._api_key_var.get() or "").strip()
        if not key:
            self._set_el_status("✗ no key", ERR_RED,
                                "Paste an ElevenLabs API key first — there is "
                                "nothing to reload against.")
            return
        clear_voice_cache(language=VO_LANGUAGE, api_key=key)
        self._last_validated_key = None
        self._fav_ids = read_fav_voices()
        self._on_api_key_changed()

    def _set_voice_options(self, options: List[Dict[str, str]]):
        """Sanitize the incoming list, rebuild the label→id map, and refresh both
        dropdowns. Sanitizing here means a stale display label can never reach
        the API as a voice_id."""
        clean: List[Dict[str, str]] = []
        for o in (options or []):
            if not isinstance(o, dict):
                continue
            vid = sanitize_voice_id(o.get("voice_id"))
            if not vid:
                continue
            label = str(o.get("label") or vid)
            if label.startswith("★"):
                label = label[1:].lstrip()      # never stack stars on re-decorate
            clean.append({
                "voice_id": vid,
                "name": str(o.get("name") or "Unnamed voice"),
                "label": f"★ {label}" if vid in self._fav_ids else label,
            })
        self._voice_options = clean
        # Keyed by the exact label AND by a whitespace-normalised form, so a
        # lookup still succeeds if the caller has trimmed the marker column.
        self._label_to_id = {}
        for o in clean:
            self._label_to_id[o["label"]] = o["voice_id"]
            self._label_to_id.setdefault(_norm_label(o["label"]), o["voice_id"])
        self._sync_voice_lists()

    def _voice_id_for_label(self, selection: str) -> str:
        """Resolve a dropdown selection back to a raw voice_id, or "".

        Tried exact first, then whitespace-normalised, then as a literal id.
        Never mangles a label into a fake id — a truncated 8-character fragment
        cannot reconstruct a real voice_id and ElevenLabs answers 404.
        """
        if not selection:
            return ""
        return (self._label_to_id.get(selection)
                or self._label_to_id.get(_norm_label(selection))
                or sanitize_voice_id(selection.strip()))

    def _ordered_options(self) -> List[Dict[str, str]]:
        """Voices with favourites pinned to the top."""
        opts = list(self._voice_options)
        if not self._fav_ids:
            return opts
        return ([o for o in opts if o["voice_id"] in self._fav_ids]
                + [o for o in opts if o["voice_id"] not in self._fav_ids])

    def _sync_voice_lists(self):
        """Refresh both dropdowns, keeping each step's remembered voice shown."""
        cb1 = getattr(self, "_step1_cb", None)
        if cb1 is None:
            return
        opts   = self._ordered_options()
        labels = [o["label"] for o in opts]
        by_id  = {o["voice_id"]: o["label"] for o in opts}
        for cb, var, vid in ((self._step1_cb, self._step1_voice_var, self._step1_voice),
                             (self._step2_cb, self._step2_voice_var, self._step2_voice)):
            try:
                cb["values"] = labels
                # Show the label when the voice is loaded, else the raw id so it
                # is obvious which voice the step is aimed at.
                var.set(by_id.get(vid, vid))
            except tk.TclError:
                continue
        self._update_voice_notes()
        # The Dialogue tab's per-speaker dropdowns draw from the same catalogue,
        # so they refresh with it rather than needing their own reload. Dub Sync
        # takes its target voice from the same list for the same reason.
        self._sync_cast_voice_lists()
        self._dub_sync_voices()

    def _update_voice_notes(self):
        """Say plainly whether each step's voice exists on this account."""
        loaded = {o["voice_id"] for o in self._voice_options}
        for lbl, vid, default_name in (
                (self._note1, self._step1_voice, STEP1_VOICE_NAME),
                (self._note2, self._step2_voice, STEP2_VOICE_NAME)):
            if not loaded:
                lbl.config(text=f"Voice list not loaded yet — default: "
                                f"{default_name} ({vid or '—'}). Validate the API key.",
                           fg=TEXT_MUTED)
            elif vid in loaded:
                lbl.config(text=f"✓ voice_id {vid} found on this account.", fg=TR_ACCENT)
            else:
                lbl.config(text=f"⚠ voice_id {vid or '—'} is NOT on this account — "
                                f"pick a voice from the dropdown.", fg=ERR_RED)

    # ═════════════════════════════════════════════════════════════════════════
    #  Pickers
    # ═════════════════════════════════════════════════════════════════════════
    def _pick_voice(self, step: int):
        """User picked a voice — resolve the label back to its id and persist."""
        var = self._step1_voice_var if step == 1 else self._step2_voice_var
        vid = self._voice_id_for_label(var.get())
        if not vid:
            # Say so rather than returning quietly. A silent no-op here reads as
            # "the choice was accepted" and only shows up as the wrong voice in
            # a finished render.
            self._status.config(
                text=f"Could not resolve that Step-{step} voice selection — "
                     "hit ↻ Reload Voices and pick again.", fg=ERR_RED)
            return
        key = "step1_voice" if step == 1 else "step2_voice"
        if step == 1:
            self._step1_voice = vid
        else:
            self._step2_voice = vid
        self._prefs[key] = vid
        write_prefs(self._prefs)
        self._update_voice_notes()

    def _reset_voice(self, step: int):
        """Snap a step back to its pinned default voice."""
        vid = STEP1_VOICE_ID if step == 1 else STEP2_VOICE_ID
        if step == 1:
            self._step1_voice = vid
            self._prefs["step1_voice"] = vid
        else:
            self._step2_voice = vid
            self._prefs["step2_voice"] = vid
        write_prefs(self._prefs)
        self._sync_voice_lists()

    def _pick_model(self, step: int):
        """Map the picked model label back to its raw id and persist it."""
        if step == 1:
            label, table, var, key = (self._step1_model_disp.get(),
                                      ELEVENLABS_TTS_MODELS,
                                      self._step1_model_var, "step1_model")
        else:
            label, table, var, key = (self._step2_model_disp.get(),
                                      ELEVENLABS_STS_MODELS,
                                      self._step2_model_var, "step2_model")
        for mid, lbl in table.items():
            if lbl == label:
                var.set(mid)
                self._prefs[key] = mid
                write_prefs(self._prefs)
                break

    def _on_emotion_toggle(self):
        """Name the LLM that will run the pass, so a misconfigured provider is
        visible before a run rather than as a silent skip afterwards."""
        if self._emotion_var.get():
            self._status.config(text=f"Emotion pass will use {llm_provider_label()}",
                                fg=TEXT_FAINT)
        else:
            self._status.config(text="", fg=ACCENT)

    def _pick_output(self):
        path = filedialog.asksaveasfilename(
            title="Save final Sadhguru VO audio as…",
            defaultextension=".mp3", initialfile="Sadhguru_VO.mp3",
            filetypes=[("MP3 audio", "*.mp3"), ("All files", "*.*")])
        if path:
            self._out_path = path
            self._out_label.config(text=path, fg=TEXT)

    def _pick_step1(self):
        path = filedialog.askopenfilename(
            title="Choose Step-1 audio to voice-change…",
            filetypes=_AUDIO_FILETYPES)
        if path:
            self._step1_path = path
            self._step1_label.config(text=path, fg=TEXT)

    def _clear_step1(self):
        self._step1_path = ""
        self._step1_label.config(text=_STEP1_AUTO_NOTE, fg=TEXT_MUTED)

    def _load_script(self):
        path = filedialog.askopenfilename(
            title="Load script from a text file…",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            messagebox.showerror("Could Not Read File", str(e))
            return
        self._text.delete("1.0", "end")
        self._text.insert("1.0", content)
        self._status.config(text=f"Loaded {os.path.basename(path)}", fg=TR_ACCENT)

    def _play_output(self):
        """Play the newest output of this session with the OS player."""
        path = self._last_output or self._step1_path
        if not path or not os.path.isfile(path):
            messagebox.showinfo("Nothing to Play",
                                "Run a step first — the newest output is played here.")
            return
        try:
            if IS_WINDOWS:
                os.startfile(path)                      # noqa — Windows only
            elif IS_MAC:
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception:
            pass
        self._status.config(text=f"▶ Playing {os.path.basename(path)}…", fg=TR_ACCENT)

    # ═════════════════════════════════════════════════════════════════════════
    #  Run
    # ═════════════════════════════════════════════════════════════════════════
    def _ui(self, fn):
        """Marshal a callable onto the Tk thread, unless we're shutting down."""
        if not self._closing:
            try:
                self.root.after(0, fn)
            except tk.TclError:
                pass

    def _set_step(self, tag: str, text: str):
        var = self._step_vars.get(tag)
        if var is not None:
            self._ui(lambda: var.set(text))

    def _run(self, mode: str = MODE_BOTH):
        if self._running:
            return

        script = self._text.get("1.0", "end").strip()
        if mode in (MODE_BOTH, MODE_TTS) and not script:
            messagebox.showwarning(
                "No Script", f"Paste or type the {VO_LANGUAGE} script in the text box first.")
            return

        # The final output path is needed in every mode — the Step-1 filename
        # derives from it — so prompt for it once here.
        out_path = self._out_path
        if not out_path:
            out_path = filedialog.asksaveasfilename(
                title="Save final Sadhguru VO audio as…",
                defaultextension=".mp3", initialfile="Sadhguru_VO.mp3",
                filetypes=[("MP3 audio", "*.mp3"), ("All files", "*.*")])
            if not out_path:
                return
            self._out_path = out_path
            self._out_label.config(text=out_path, fg=TEXT)

        # A Step-2-only run needs existing audio: the user's pick, else the
        # auto-named Step-1 file if it is already on disk, else ask.
        vc_input = ""
        if mode == MODE_VC:
            auto = step1_target(out_path)
            vc_input = self._step1_path or (auto if os.path.isfile(auto) else "")
            if not vc_input or not os.path.isfile(vc_input):
                vc_input = filedialog.askopenfilename(
                    title="Choose Step-1 audio to voice-change…",
                    filetypes=_AUDIO_FILETYPES)
                if not vc_input:
                    return
                self._step1_path = vc_input
                self._step1_label.config(text=vc_input, fg=TEXT)

        try:
            api_key = get_api_key()
        except Exception as e:
            messagebox.showerror("ElevenLabs API Key Error", str(e))
            return

        try:
            req = validate_request(VoRequest(
                mode=mode, script=script, out_path=out_path, api_key=api_key,
                step1_voice=self._step1_voice, step2_voice=self._step2_voice,
                step1_model=self._step1_model_var.get(),
                step2_model=self._step2_model_var.get(),
                emotion=bool(self._emotion_var.get()),
                keep_step1=bool(self._keep_step1_var.get()),
                vc_input=vc_input, language=VO_LANGUAGE,
                llm_model=GEMINI_DEFAULT_MODEL))
        except ValueError as e:
            messagebox.showerror("Cannot Run", str(e))
            return

        self._running = True
        for b in self._buttons:
            b.config(state="disabled")
        self._status.config(text="Starting…", fg=TEXT_FAINT)
        self._set_step("1", "—")
        self._set_step("2", "—")

        def _status(msg, colour=ACCENT):
            self._ui(lambda m=msg, c=colour: self._status.config(text=m, fg=c))

        def _worker():
            try:
                def _step_cb(tag: str, msg: str):
                    self._set_step(tag, msg)
                    _status(msg)

                result = run_pipeline(req, step_cb=_step_cb)

                if req.mode in (MODE_BOTH, MODE_TTS) and not result.step1_removed:
                    self._ui(lambda p=result.step1_path: (
                        setattr(self, "_step1_path", p),
                        self._step1_label.config(text=p, fg=TEXT)))
                self._last_output = result.final_path
                _status(f"✔ Done → {os.path.basename(result.final_path)}", TR_ACCENT)
            except Exception as exc:
                err = str(exc)
                self._ui(lambda e=err: self._status.config(text=f"Error: {e}", fg=ERR_RED))
                self._ui(lambda e=err: messagebox.showerror("Sadhguru VO Error", e))
            finally:
                self._running = False
                self._ui(lambda: [b.config(state="normal") for b in self._buttons])

        threading.Thread(target=_worker, daemon=True).start()

    def _on_close(self):
        # Mark the app as closing so any in-flight `after` callbacks bail out
        # before they touch destroyed widgets.
        self._closing = True
        if self._dpi_job is not None:
            try:
                self.root.after_cancel(self._dpi_job)
            except tk.TclError:
                pass
            self._dpi_job = None
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def main() -> int:
    # Has to happen before the Tk root exists: after that, the process's DPI
    # mode is fixed for its lifetime and Windows will upscale the window rather
    # than let it draw at the monitor's real resolution.
    enable_dpi_awareness()
    root = tk.Tk()
    SadhguruVOApp(root)
    root.mainloop()
    return 0
