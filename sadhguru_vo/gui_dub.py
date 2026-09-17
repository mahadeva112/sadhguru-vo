"""
The Dub Sync tab.

Mixed into SadhguruVOApp rather than written inline, for the reason the Dialogue
tab already established: the single-speaker tab is in daily production use and
every line added to gui.py is a line that can break it. Everything here is
namespaced `_dub_*` and touches nothing the other two tabs own.

The tab is arranged around one claim — that you can see whether a dub will work
before paying for it. So the expensive button is last, disabled until a preview
exists, and everything above it runs locally:

    source audio ──► pause map ──┐
                                 ├──► chunks ──► estimate ──► timeline
    source + target script ──────┘                              │
                                                                ▼
                                                    ▶ Generate  (the only
                                                       step that costs)

Re-estimating is instant and free, so it happens on every edit. Switching sync
mode, retyping a translation, dragging a boundary — all of it re-draws the
timeline without touching the network.
"""

import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from .config import (ACCENT, BG, BTN_ACT, BTN_BG, BTN_FG, DEFAULT_TIMING,
                     DIALOGUE_GAP_SAME_MS, DIALOGUE_GAP_SWITCH_MS,
                     DUB_MIN_PAUSE_MS, DUB_DEFAULT_STS_MODE,
                     DUB_STS_MODES, DUB_STS_MODE_LABELS, DUB_STS_WHOLE,
                     DUB_DEFAULT_SYNC_MODE, ERR_RED, INPUT_BG, INPUT_FG,
                     PANEL, PANEL2, PANEL_BORDER, REG_LABEL,
                     STEP1_GOLD, STEP2_VIOLET, SVO_PANEL, SYNC_LOCK,
                     SYNC_MODE_LABELS, SYNC_MODES, TEXT,
                     TEXT_FAINT, TEXT_MUTED, TIMING_AUDIO, TIMING_LABELS,
                     TIMING_MODES, TIMING_SCRIPT, TR_ACCENT,
                     VO_LANGUAGE, WARN_AMBER)
from .scaling import (F_MONO7, F_MONO7B, F_MONO8, F_MONO8B, F_MONO9,
                      F_MONO9B, F_MONO11B, F_UI8, F_UI9, S)
from .cast import cast_for_speakers, validate_cast
from .dub_align import (align, merge, plan_from_script,
                        speakers_in_script, split)
from .prefs import write_prefs
from .dub_estimate import (EMPTY, FIT, OVER, REWRITE, SHORT, TIGHT,
                           DUB_DEFAULT_RATES, preview)
from .dub_render import DubRenderError, plan_whole_sts, render_dub
from .pause_map import PauseMapError, detect_segments, merge_at
from .pause_map import split_at as pm_split_at
from .script_parser import normalize_speaker

_DUB_SUBTITLE = ("Pause-aware dubbing:  source audio → pause map → chunked "
                 "script → estimate → locked render")

_AUDIO_FILETYPES = [("Audio files", "*.mp3 *.wav *.m4a *.aac *.flac *.ogg *.webm"),
                    ("All files", "*.*")]

# One colour per verdict, used by both the timeline and the table so a red block
# and a red badge always mean the same thing.
VERDICT_COLOUR = {
    FIT:     ACCENT,
    SHORT:   "#60a5fa",
    TIGHT:   WARN_AMBER,
    OVER:    "#fb923c",
    REWRITE: ERR_RED,
    EMPTY:   TEXT_FAINT,
}
VERDICT_HELP = {
    FIT:     "lands inside its slot",
    SHORT:   "finishes early — padded with silence",
    TIGHT:   "overruns, absorbed by the pause or a small stretch",
    OVER:    "runs past its slot — drift, in elastic mode",
    REWRITE: "too long to fit without audible stretching — shorten it",
    EMPTY:   "no target text — renders as silence",
}

SOURCE_LANGUAGES = ["English"] + sorted(DUB_DEFAULT_RATES.keys())

# Timeline geometry.
_RULER_H, _LANE_H, _LANE_GAP = 22, 26, 10
_TL_HEIGHT = _RULER_H + _LANE_H * 2 + _LANE_GAP + 14


class DubSyncTabMixin:
    """Everything the Dub Sync tab needs. Mixed into SadhguruVOApp."""

    # ═════════════════════════════════════════════════════════════════════════
    #  State
    # ═════════════════════════════════════════════════════════════════════════
    def _dub_init_state(self):
        """Called from __init__ before the tab is built."""
        self._dub_audio_path = ""
        self._dub_out_path = ""
        self._dub_pmap = None            # PauseMap
        self._dub_chunks = []            # List[Chunk]
        self._dub_preview = None         # Preview
        self._dub_report = None          # AlignReport
        self._dub_rows = []              # per-chunk widget dicts
        self._dub_cast_rows = []         # cast rows, shared with the Dialogue tab
        self._dub_speakers = []          # speakers found in the dub script
        self._dub_running = False
        self._dub_cancel = False
        self._dub_buttons = []
        self._dub_last_output = ""
        self._dub_estimate_job = None
        self._dub_detect_job = None

        self._dub_voice_var    = tk.StringVar()
        self._dub_voice_id     = self._prefs.get("step1_voice", "")
        self._dub_src_lang_var = tk.StringVar(value="English")
        self._dub_tgt_lang_var = tk.StringVar(value=VO_LANGUAGE)
        self._dub_minpause_var = tk.StringVar(value=str(DUB_MIN_PAUSE_MS))
        self._dub_sens_var     = tk.StringVar(value="0")
        self._dub_mode_var     = tk.StringVar(
            value=SYNC_MODE_LABELS[DUB_DEFAULT_SYNC_MODE])
        # Whichever job was done last is the one waiting next time — a session
        # is normally all dubbing or all script work, not an alternation.
        self._dub_timing_var   = tk.StringVar(
            value=self._prefs.get("studio_timing", DEFAULT_TIMING))
        self._dub_sts_var      = tk.StringVar(
            value=DUB_STS_MODE_LABELS[self._prefs.get("studio_sts",
                                                      DUB_DEFAULT_STS_MODE)])
        self._dub_gap_same_var   = tk.StringVar(value=str(DIALOGUE_GAP_SAME_MS))
        self._dub_gap_switch_var = tk.StringVar(value=str(DIALOGUE_GAP_SWITCH_MS))
        self._dub_merge_var      = tk.BooleanVar(value=True)
        self._dub_zoom_var     = tk.DoubleVar(value=60.0)   # pixels per second

    # ═════════════════════════════════════════════════════════════════════════
    #  Layout
    # ═════════════════════════════════════════════════════════════════════════
    def _build_dub_tab(self):
        root = self._tab_dub

        self._dub_build_timing_bar(root)
        self._dub_build_source_bar(root)
        # Built before the scripts/cast/timeline/table — see the docstring on
        # _dub_build_actions for why the order matters here.
        self._dub_build_actions(root)
        self._dub_build_scripts(root)
        self._dub_build_cast(root)
        self._dub_build_timeline(root)
        self._dub_build_table(root)
        self._dub_apply_timing()

    def _dub_build_cast(self, root):
        """The cast table — only shown once the script names speakers.

        Same rows as the Dialogue tab, editing the same cast.json. A dub of a
        real conversation needs exactly what that tab already models: each
        speaker's own voice, and each speaker's own number of steps, so
        Sadhguru runs TTS → speech-to-speech while an interviewer runs TTS
        alone.
        """
        self._dub_cast_frame = tk.Frame(root, bg=BG)
        # Packed only when there are speakers — see _dub_rebuild_cast.

        head = tk.Frame(self._dub_cast_frame, bg=BG)
        head.pack(fill="x", padx=S(2), pady=(S(4), S(2)))
        tk.Label(head, text="Cast  (shared with the Dialogue tab — one cast.json):",
                 bg=BG, fg=TEXT, font=F_MONO9B).pack(side="left")
        self._dub_cast_note = tk.Label(head, text="", bg=BG, fg=TEXT_MUTED,
                                       font=F_UI8)
        self._dub_cast_note.pack(side="right")

        hdr = tk.Frame(self._dub_cast_frame, bg=PANEL2)
        hdr.pack(fill="x", padx=S(2))
        for text, width in (("SPEAKER", 16), ("STEPS", 26), ("VOICE (Step 1)", 34),
                            ("TARGET VOICE (Step 2)", 34), ("DELIVERY NOTE", 22)):
            tk.Label(hdr, text=text, bg=PANEL2, fg=TEXT_FAINT,
                     font=F_MONO8B, width=width, anchor="w"
                     ).pack(side="left", padx=(S(6), S(0)), pady=S(3))

        self._dub_cast_inner = tk.Frame(self._dub_cast_frame, bg=BG)
        self._dub_cast_inner.pack(fill="x", padx=S(2))

    def _dub_build_timing_bar(self, root):
        """The one control that decides what this tab is doing.

        Script only and Source audio differ in exactly one thing — where the gap
        after each chunk comes from — so they are one tab with a switch rather
        than two tabs with a shared implementation.
        """
        bar = tk.Frame(root, bg=PANEL2, height=S(34), bd=0,
                       highlightbackground=PANEL_BORDER, highlightthickness=1)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        self._dub_timing_bar = bar

        tk.Label(bar, text="Timing from:", bg=PANEL2, fg=TEXT_MUTED,
                 font=F_UI9).pack(side="left", padx=(S(14), S(8)), pady=S(7))
        for mode in TIMING_MODES:
            tk.Radiobutton(bar, text=TIMING_LABELS[mode], value=mode,
                           variable=self._dub_timing_var,
                           command=self._dub_on_timing_change,
                           bg=PANEL2, fg=TEXT, selectcolor=PANEL2,
                           activebackground=PANEL2, activeforeground=ACCENT,
                           font=F_UI9).pack(side="left", padx=(S(0), S(12)))
        self._dub_timing_note = tk.Label(bar, text="", bg=PANEL2, fg=TEXT_FAINT,
                                         font=F_MONO8, anchor="w")
        self._dub_timing_note.pack(side="left", fill="x", expand=True, padx=S(6))

    def _dub_build_source_bar(self, root):
        bar = tk.Frame(root, bg=SVO_PANEL, bd=0,
                       highlightbackground="#5b4fbf", highlightthickness=1)
        bar.pack(fill="x")
        self._dub_audio_bar = bar
        row = tk.Frame(bar, bg=SVO_PANEL, height=S(40))
        row.pack(fill="x")
        row.pack_propagate(False)

        tk.Label(row, text="Source audio:", bg=SVO_PANEL, fg=TEXT_MUTED,
                 font=F_UI9).pack(side="left", padx=(S(14), S(6)), pady=S(9))
        self._dub_audio_label = tk.Label(
            row, text="— none chosen —", bg=SVO_PANEL, fg=TEXT_FAINT,
            font=F_MONO9, anchor="w", width=34)
        self._dub_audio_label.pack(side="left")
        self._btn(row, "Browse", self._dub_pick_audio,
                  bg=BTN_BG, fg=BTN_FG, abg=BTN_ACT).pack(side="left", padx=S(6), pady=S(6))

        tk.Label(row, text="from:", bg=SVO_PANEL, fg=TEXT_MUTED,
                 font=F_UI9).pack(side="left", padx=(S(12), S(3)))
        ttk.Combobox(row, textvariable=self._dub_src_lang_var, width=10,
                     values=SOURCE_LANGUAGES, state="readonly").pack(side="left")
        tk.Label(row, text="→", bg=SVO_PANEL, fg=STEP1_GOLD,
                 font=F_MONO11B).pack(side="left", padx=S(5))
        tgt = ttk.Combobox(row, textvariable=self._dub_tgt_lang_var, width=11,
                           values=sorted(DUB_DEFAULT_RATES.keys()), state="readonly")
        tgt.pack(side="left")
        tgt.bind("<<ComboboxSelected>>", lambda _e: self._dub_schedule_estimate())

        tk.Label(row, text="Target voice:", bg=SVO_PANEL, fg=TEXT_MUTED,
                 font=F_UI9).pack(side="left", padx=(S(14), S(4)))
        self._dub_voice_cb = ttk.Combobox(row, textvariable=self._dub_voice_var,
                                          width=26, state="readonly")
        self._dub_voice_cb.pack(side="left")
        self._dub_voice_cb.bind("<<ComboboxSelected>>", self._dub_on_voice_pick)

        # ── Detection row ────────────────────────────────────────────────────
        det = tk.Frame(root, bg=PANEL2, height=S(38), bd=0,
                       highlightbackground=PANEL_BORDER, highlightthickness=1)
        det.pack(fill="x")
        det.pack_propagate(False)
        self._dub_detect_bar = det

        tk.Label(det, text="Min pause (ms):", bg=PANEL2, fg=TEXT_MUTED,
                 font=F_UI9).pack(side="left", padx=(S(14), S(3)), pady=S(8))
        tk.Entry(det, textvariable=self._dub_minpause_var, width=6, bg=INPUT_BG,
                 fg=INPUT_FG, insertbackground=INPUT_FG, relief="flat",
                 font=F_UI9).pack(side="left")
        tk.Label(det, text="Sensitivity (dB):", bg=PANEL2, fg=TEXT_MUTED,
                 font=F_UI9).pack(side="left", padx=(S(12), S(3)))
        tk.Entry(det, textvariable=self._dub_sens_var, width=5, bg=INPUT_BG,
                 fg=INPUT_FG, insertbackground=INPUT_FG, relief="flat",
                 font=F_UI9).pack(side="left")
        analyse = self._btn(det, "⟳  Analyse Pauses", self._dub_analyse,
                            bg=TR_ACCENT, fg="#052e16", abg="#16a34a")
        analyse.pack(side="left", padx=S(10), pady=S(5))
        self._dub_buttons.append(analyse)

        tk.Label(det, text="Sync:", bg=PANEL2, fg=TEXT_MUTED,
                 font=F_UI9).pack(side="left", padx=(S(14), S(3)))
        mode_cb = ttk.Combobox(det, textvariable=self._dub_mode_var, width=34,
                               values=[SYNC_MODE_LABELS[m] for m in SYNC_MODES],
                               state="readonly")
        mode_cb.pack(side="left")
        mode_cb.bind("<<ComboboxSelected>>", lambda _e: self._dub_estimate())

        self._dub_detect_note = tk.Label(det, text="", bg=PANEL2, fg=TEXT_FAINT,
                                         font=F_MONO8, anchor="w")
        self._dub_detect_note.pack(side="left", fill="x", expand=True, padx=S(10))

        # ── Gap row — the script-timing counterpart of the detection row ─────
        gaps = tk.Frame(root, bg=SVO_PANEL, height=S(38), bd=0,
                        highlightbackground="#5b4fbf", highlightthickness=1)
        gaps.pack(fill="x")
        gaps.pack_propagate(False)
        self._dub_gaps_bar = gaps

        tk.Label(gaps, text="Gap ms — same:", bg=SVO_PANEL, fg=TEXT_MUTED,
                 font=F_UI9).pack(side="left", padx=(S(14), S(3)), pady=S(8))
        tk.Entry(gaps, textvariable=self._dub_gap_same_var, width=5, bg=INPUT_BG,
                 fg=INPUT_FG, insertbackground=INPUT_FG, relief="flat",
                 font=F_UI9).pack(side="left")
        tk.Label(gaps, text="switch:", bg=SVO_PANEL, fg=TEXT_MUTED,
                 font=F_UI9).pack(side="left", padx=(S(10), S(3)))
        tk.Entry(gaps, textvariable=self._dub_gap_switch_var, width=5, bg=INPUT_BG,
                 fg=INPUT_FG, insertbackground=INPUT_FG, relief="flat",
                 font=F_UI9).pack(side="left")
        tk.Checkbutton(gaps, text="Merge same speaker",
                       variable=self._dub_merge_var,
                       command=self._dub_build_chunks,
                       bg=SVO_PANEL, fg=TEXT, selectcolor=SVO_PANEL,
                       activebackground=SVO_PANEL, activeforeground=STEP2_VIOLET,
                       font=F_UI9).pack(side="left", padx=(S(14), S(0)))
        plan = self._btn(gaps, "⟳  Plan Timeline", self._dub_build_chunks,
                         bg=TR_ACCENT, fg="#052e16", abg="#16a34a")
        plan.pack(side="left", padx=S(10), pady=S(5))
        self._dub_buttons.append(plan)
        tk.Label(gaps, text="no recording — positions are computed",
                 bg=SVO_PANEL, fg=TEXT_FAINT, font=F_MONO8, anchor="w"
                 ).pack(side="left", fill="x", expand=True, padx=S(10))

    def _dub_build_scripts(self, root):
        wrap = tk.Frame(root, bg=BG)
        wrap.pack(fill="x", padx=S(10), pady=(S(6), S(0)))

        head = tk.Frame(wrap, bg=BG)
        head.pack(fill="x", pady=(S(2), S(2)))
        tk.Label(head, text="Scripts  (one line per pause makes the split exact):",
                 bg=BG, fg=TEXT, font=F_MONO9B).pack(side="left")
        self._btn(head, "Re-split Script", self._dub_build_chunks,
                  bg="#172554", fg=REG_LABEL, abg="#1e3a8a").pack(side="right")
        self._btn(head, "Load target .txt",
                  lambda: self._dub_load_into(self._dub_tgt_text),
                  bg=BTN_BG, fg=BTN_FG, abg=BTN_ACT).pack(side="right", padx=S(6))
        self._btn(head, "Load source .txt",
                  lambda: self._dub_load_into(self._dub_src_text),
                  bg=BTN_BG, fg=BTN_FG, abg=BTN_ACT).pack(side="right")

        cols = tk.Frame(wrap, bg=BG)
        cols.pack(fill="both", expand=True)

        left = tk.Frame(cols, bg=BG)
        left.pack(side="left", fill="both", expand=True, padx=(S(0), S(4)))
        tk.Label(left, text="SOURCE SCRIPT", bg=BG, fg=STEP1_GOLD,
                 font=F_MONO8B, anchor="w").pack(fill="x")
        self._dub_src_text = scrolledtext.ScrolledText(
            left, wrap="word", bg=INPUT_BG, fg=INPUT_FG, insertbackground=INPUT_FG,
            font=F_MONO9, relief="flat", bd=0, height=5,
            selectbackground="#1e3a8a", selectforeground="#f8fafc")
        self._dub_src_text.pack(fill="both", expand=True)
        # Speakers are read from the source script, so typing there is what
        # brings the cast table up — same trigger the Dialogue tab uses.
        self._dub_src_text.bind("<KeyRelease>", self._dub_schedule_detect)

        right = tk.Frame(cols, bg=BG)
        right.pack(side="left", fill="both", expand=True, padx=(S(4), S(0)))
        tk.Label(right, text="TARGET SCRIPT", bg=BG, fg=STEP2_VIOLET,
                 font=F_MONO8B, anchor="w").pack(fill="x")
        self._dub_tgt_text = scrolledtext.ScrolledText(
            right, wrap="word", bg=INPUT_BG, fg=INPUT_FG, insertbackground=INPUT_FG,
            font=F_MONO9, relief="flat", bd=0, height=5,
            selectbackground="#1e3a8a", selectforeground="#f8fafc")
        self._dub_tgt_text.pack(fill="both", expand=True)

        self._dub_align_note = tk.Label(wrap, text="", bg=BG, fg=TEXT_MUTED,
                                        font=F_UI8, anchor="w",
                                        justify="left", wraplength=S(1060))
        self._dub_align_note.pack(fill="x", pady=(S(2), S(0)))

    def _dub_build_timeline(self, root):
        frame = tk.Frame(root, bg=BG)
        frame.pack(fill="x", padx=S(10), pady=(S(6), S(0)))
        self._dub_timeline_frame = frame

        head = tk.Frame(frame, bg=BG)
        head.pack(fill="x")
        tk.Label(head, text="TIMELINE  ", bg=BG, fg=TEXT,
                 font=F_MONO9B).pack(side="left")
        self._dub_drift_label = tk.Label(head, text="", bg=BG, fg=TEXT_MUTED,
                                         font=F_MONO9)
        self._dub_drift_label.pack(side="left")
        tk.Label(head, text="zoom", bg=BG, fg=TEXT_FAINT,
                 font=F_UI8).pack(side="right", padx=(S(4), S(0)))
        tk.Scale(head, from_=8, to=300, orient="horizontal",
                 variable=self._dub_zoom_var, showvalue=False, length=120,
                 bg=BG, fg=TEXT_MUTED, troughcolor=PANEL2, highlightthickness=0,
                 sliderrelief="flat", command=lambda _v: self._dub_draw_timeline()
                 ).pack(side="right")

        holder = tk.Frame(frame, bg=PANEL2, highlightbackground=PANEL_BORDER,
                          highlightthickness=1)
        holder.pack(fill="x")
        self._dub_canvas = tk.Canvas(holder, bg="#0d1526", height=S(_TL_HEIGHT),
                                     highlightthickness=0, bd=0)
        hbar = ttk.Scrollbar(holder, orient="horizontal",
                             command=self._dub_canvas.xview)
        self._dub_canvas.configure(xscrollcommand=hbar.set)
        hbar.pack(side="bottom", fill="x")
        self._dub_canvas.pack(side="top", fill="x")
        self._dub_canvas.bind("<Button-1>", self._dub_canvas_click)
        self._dub_canvas.bind("<Button-3>", self._dub_canvas_split)

        tk.Label(frame, text="click a block to jump to its row · right-click a "
                             "SRC block to split it there · ⇊ in the table merges "
                             "a chunk with the next",
                 bg=BG, fg=TEXT_FAINT, font=F_UI8, anchor="w"
                 ).pack(fill="x", pady=(S(2), S(0)))

    def _dub_build_table(self, root):
        frame = tk.Frame(root, bg=BG)
        frame.pack(fill="both", expand=True, padx=S(10), pady=(S(6), S(0)))

        hdr = tk.Frame(frame, bg=PANEL2)
        hdr.pack(fill="x")
        for text, width in (("#", 4), ("SPEAKER", 13), ("SOURCE IN", 11),
                            ("SLOT", 8), ("PAUSE", 6),
                            ("SOURCE TEXT", 26), ("TARGET TEXT (editable)", 32),
                            ("EST", 12), ("FIT", 9)):
            tk.Label(hdr, text=text, bg=PANEL2, fg=TEXT_FAINT,
                     font=F_MONO8B, width=width, anchor="w"
                     ).pack(side="left", padx=(S(4), S(0)), pady=S(3))

        wrapper = tk.Frame(frame, bg=BG)
        wrapper.pack(fill="both", expand=True)
        self._dub_table_canvas = tk.Canvas(wrapper, bg=BG, highlightthickness=0, bd=0)
        vbar = ttk.Scrollbar(wrapper, orient="vertical",
                             command=self._dub_table_canvas.yview)
        self._dub_table_canvas.configure(yscrollcommand=vbar.set)
        vbar.pack(side="right", fill="y")
        self._dub_table_canvas.pack(side="left", fill="both", expand=True)
        self._dub_table_inner = tk.Frame(self._dub_table_canvas, bg=BG)
        self._dub_table_window = self._dub_table_canvas.create_window(
            (0, 0), window=self._dub_table_inner, anchor="nw")
        self._dub_table_inner.bind(
            "<Configure>",
            lambda _e: self._dub_table_canvas.configure(
                scrollregion=self._dub_table_canvas.bbox("all")))
        self._dub_table_canvas.bind(
            "<Configure>",
            lambda e: self._dub_table_canvas.itemconfigure(
                self._dub_table_window, width=e.width))
        self._dub_table_canvas.bind_all(
            "<MouseWheel>", self._dub_on_wheel, add="+")

    def _dub_build_actions(self, root):
        """The Generate Dub bar and the status strip above it.

        Both are pinned with side="bottom" and built *before* the scripts,
        cast table, timeline and chunk table (see _build_dub_tab) — pack
        carves the cavity in the order slaves are registered, so a bottom bar
        registered late can be starved down to an unmapped sliver once the
        cast table and 225-row chunk table above it want more height than the
        window has. Reserving its space first is what keeps the button
        reachable regardless of how tall the script grows.
        """
        bot = tk.Frame(root, bg=PANEL, height=S(48), bd=0,
                       highlightbackground=PANEL_BORDER, highlightthickness=1)
        bot.pack(fill="x", side="bottom")
        bot.pack_propagate(False)

        # Where step 2 happens. Pinned to the bottom with the rest of the render
        # controls, and shown in both timing modes, because it is a property of
        # how the dub is voiced rather than of where its timings came from.
        sts = tk.Frame(root, bg=PANEL2, height=S(32), bd=0,
                       highlightbackground=PANEL_BORDER, highlightthickness=1)
        sts.pack(fill="x", side="bottom")
        sts.pack_propagate(False)
        tk.Label(sts, text="Voice change:", bg=PANEL2, fg=TEXT_MUTED,
                 font=F_UI9).pack(side="left", padx=(S(14), S(6)), pady=S(6))
        sts_cb = ttk.Combobox(sts, textvariable=self._dub_sts_var, width=44,
                              values=[DUB_STS_MODE_LABELS[m]
                                      for m in DUB_STS_MODES],
                              state="readonly")
        sts_cb.pack(side="left")
        sts_cb.bind("<<ComboboxSelected>>", lambda _e: self._dub_on_sts_change())
        self._dub_sts_note = tk.Label(sts, text="", bg=PANEL2, fg=TEXT_FAINT,
                                      font=F_MONO8, anchor="w")
        self._dub_sts_note.pack(side="left", fill="x", expand=True, padx=S(10))

        notes = tk.Frame(root, bg="#162032", bd=0,
                         highlightbackground="#3b82f6", highlightthickness=1)
        notes.pack(fill="x", side="bottom", padx=S(0), pady=(S(6), S(0)))
        self._dub_notes = tk.Label(notes, text="Choose a source audio file and "
                                               "paste both scripts, then Analyse.",
                                   bg="#162032", fg=TEXT_MUTED,
                                   font=F_MONO8, anchor="w",
                                   justify="left", wraplength=S(1080))
        self._dub_notes.pack(fill="x", padx=S(10), pady=S(6))

        self._dub_gen_btn = self._btn(bot, "  ▶  Generate Dub  ", self._dub_generate,
                                      bg="#0f1d14", fg=TR_ACCENT, abg="#1f4d2e")
        self._dub_gen_btn.pack(side="left", padx=(S(14), S(6)), pady=S(8))
        self._dub_gen_btn.config(state="disabled")
        self._dub_buttons.append(self._dub_gen_btn)

        self._btn(bot, "Choose Output", self._dub_pick_output,
                  bg=BTN_BG, fg=BTN_FG, abg=BTN_ACT).pack(side="left", padx=(S(0), S(6)), pady=S(8))
        self._btn(bot, "▶ Play", self._dub_play,
                  bg=BTN_BG, fg=BTN_FG, abg=BTN_ACT).pack(side="left", padx=(S(0), S(6)), pady=S(8))
        self._btn(bot, "📁 Folder", self._dub_open_folder,
                  bg=BTN_BG, fg=BTN_FG, abg=BTN_ACT).pack(side="left", padx=(S(0), S(6)), pady=S(8))
        self._dub_status = tk.Label(bot, text="", bg=PANEL, fg=ACCENT,
                                    font=F_MONO9, anchor="w")
        self._dub_status.pack(side="left", fill="x", expand=True, padx=S(8))
    # ═════════════════════════════════════════════════════════════════════════
    #  Shared-chrome hooks
    # ═════════════════════════════════════════════════════════════════════════
    def _dub_sync_voices(self):
        """Refresh the target-voice dropdown from the shared catalogue.

        Called by _sync_voice_lists, so the tab picks up a reloaded voice list
        without fetching it again.
        """
        cb = getattr(self, "_dub_voice_cb", None)
        if cb is None:
            return
        opts = self._ordered_options()
        try:
            cb["values"] = [o["label"] for o in opts]
            by_id = {o["voice_id"]: o["label"] for o in opts}
            self._dub_voice_var.set(by_id.get(self._dub_voice_id,
                                              self._dub_voice_id or ""))
        except tk.TclError:
            pass

    def _dub_on_voice_pick(self, _event=None):
        self._dub_voice_id = self._voice_id_for_label(self._dub_voice_var.get())

    # ── Cast ─────────────────────────────────────────────────────────────────
    def _dub_rebuild_cast(self):
        """One row per speaker in the dub script, or hide the table entirely.

        A single-speaker dub has no cast to choose, and an empty table in that
        case is a control that does nothing — so it is not shown at all, and
        the tab keeps behaving exactly as it did before speakers existed.
        """
        for row in self._dub_cast_rows:
            row["frame"].destroy()
        self._dub_cast_rows = []

        if not self._dub_speakers:
            self._dub_cast_frame.pack_forget()
            return

        # Fill in any speaker the cast has never seen. cast_for_speakers gives
        # Sadhguru the pinned two-step recipe and leaves everyone else blank on
        # purpose, so validate_cast() forces a real choice rather than quietly
        # rendering an interviewer in Sadhguru's voice.
        self._cast = cast_for_speakers(self._dub_speakers, self._cast)
        for name in self._dub_speakers:
            self._dub_cast_rows.append(
                self._make_cast_row(name, parent=self._dub_cast_inner))

        self._dub_cast_frame.pack(fill="x", padx=S(10), pady=(S(6), S(0)),
                                  before=self._dub_timeline_frame)
        self._sync_cast_voice_lists()
        self._dub_cast_refresh()

    def _dub_cast_refresh(self):
        """Update the cast note. Called by the Dialogue tab too, so it has to
        survive being invoked before this tab has a table."""
        note = getattr(self, "_dub_cast_note", None)
        if note is None or not self._dub_speakers:
            return
        problems = validate_cast(self._cast, self._dub_speakers)
        two = sum(1 for n in self._dub_speakers
                  if self._cast[normalize_speaker(n)].two_step)
        one = len(self._dub_speakers) - two
        bits = []
        if two:
            bits.append(f"{two} two-step")
        if one:
            bits.append(f"{one} one-step")
        if problems:
            note.config(text=f"⚠ {len(problems)} to fix — {'; '.join(problems[:2])}",
                        fg=ERR_RED)
        else:
            note.config(text=" · ".join(bits) + " — ready", fg=TR_ACCENT)
        # Changing a speaker's steps or target voice can make the whole-mix pass
        # newly possible or newly impossible.
        self._dub_sts_refresh()

    def _dub_on_wheel(self, event):
        """Scroll the chunk table only when the pointer is actually over it."""
        try:
            widget = self.root.winfo_containing(event.x_root, event.y_root)
        except (tk.TclError, KeyError):
            return
        node = widget
        while node is not None:
            if node is self._dub_table_canvas or node is self._dub_table_inner:
                self._dub_table_canvas.yview_scroll(
                    -1 if event.delta > 0 else 1, "units")
                return
            node = getattr(node, "master", None)

    # ═════════════════════════════════════════════════════════════════════════
    #  Files
    # ═════════════════════════════════════════════════════════════════════════
    def _dub_pick_audio(self):
        path = filedialog.askopenfilename(title="Choose the source recording",
                                          filetypes=_AUDIO_FILETYPES)
        if not path:
            return
        self._dub_audio_path = path
        name = os.path.basename(path)
        self._dub_audio_label.config(
            text=name if len(name) <= 34 else "…" + name[-33:], fg=TEXT)
        self._dub_set_status(f"Loaded {name} — press Analyse Pauses.", TEXT_MUTED)

    def _dub_pick_output(self):
        path = filedialog.asksaveasfilename(
            title="Save the dub as", defaultextension=".mp3",
            filetypes=[("MP3", "*.mp3"), ("WAV", "*.wav")])
        if path:
            self._dub_out_path = path
            self._dub_set_status(f"Output: {os.path.basename(path)}", TEXT_MUTED)

    def _dub_load_into(self, widget):
        path = filedialog.askopenfilename(
            title="Load script", filetypes=[("Text", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as fh:
                body = fh.read()
        except (OSError, UnicodeDecodeError) as e:
            messagebox.showerror("Sadhguru VO", f"Could not read that file:\n{e}")
            return
        widget.delete("1.0", "end")
        widget.insert("1.0", body)

    def _dub_play(self):
        self._open_with_os(self._dub_last_output, "Nothing rendered yet.")

    def _dub_open_folder(self):
        target = self._dub_last_output or self._dub_out_path
        self._open_with_os(os.path.dirname(target) if target else "",
                           "No output folder yet.")

    # ═════════════════════════════════════════════════════════════════════════
    #  Analysis
    # ═════════════════════════════════════════════════════════════════════════
    def _dub_int(self, var, fallback: int) -> int:
        try:
            return int(str(var.get()).strip())
        except (ValueError, AttributeError):
            return fallback

    def _dub_float(self, var, fallback: float) -> float:
        try:
            return float(str(var.get()).strip())
        except (ValueError, AttributeError):
            return fallback

    def _dub_mode(self) -> str:
        label = self._dub_mode_var.get()
        for mode, text in SYNC_MODE_LABELS.items():
            if text == label:
                return mode
        return DUB_DEFAULT_SYNC_MODE

    def _dub_sts_mode(self) -> str:
        label = self._dub_sts_var.get()
        for mode, text in DUB_STS_MODE_LABELS.items():
            if text == label:
                return mode
        return DUB_DEFAULT_STS_MODE

    def _dub_on_sts_change(self):
        self._prefs["studio_sts"] = self._dub_sts_mode()
        write_prefs(self._prefs)
        self._dub_sts_refresh()

    def _dub_sts_refresh(self):
        """Say now whether the whole-mix pass can actually run on this cast.

        The check is the same one render_dub makes, run against the chunks as
        they stand — so a cast that cannot use the mode says so while it is
        still free to change, rather than after the render has quietly fallen
        back to per-chunk."""
        note = getattr(self, "_dub_sts_note", None)
        if note is None:
            return
        if self._dub_sts_mode() != DUB_STS_WHOLE:
            note.config(text="each chunk converted on its own — safe for any cast",
                        fg=TEXT_FAINT)
            return
        if not self._dub_chunks:
            note.config(text="build the timeline to check this cast", fg=TEXT_FAINT)
            return
        plan = plan_whole_sts(self._dub_chunks,
                             dict(self._cast) if self._dub_speakers else None,
                             self._dub_voice_id, self._prefs["step1_model"], None)
        if plan.ok:
            note.config(text="✔ one voice — the mix is converted in one pass",
                        fg=TR_ACCENT)
        else:
            note.config(text=f"⚠ falls back to per chunk — {plan.reason}",
                        fg=WARN_AMBER)

    def _dub_set_status(self, text: str, colour: str = ACCENT):
        self._dub_status.config(text=text, fg=colour)

    def _dub_analyse(self):
        """Detect pauses, then align and estimate. Local only — no API calls."""
        if self._dub_running:
            return
        if not self._dub_audio_path or not os.path.isfile(self._dub_audio_path):
            messagebox.showwarning("Sadhguru VO",
                                   "Choose a source audio file first.")
            return

        min_pause = self._dub_int(self._dub_minpause_var, DUB_MIN_PAUSE_MS)
        sens = self._dub_float(self._dub_sens_var, 0.0)
        self._dub_running = True
        for b in self._dub_buttons:
            b.config(state="disabled")
        self._dub_set_status("Analysing…", ACCENT)

        def _worker():
            try:
                pmap = detect_segments(self._dub_audio_path,
                                       min_pause_ms=min_pause,
                                       sensitivity_db=sens,
                                       status_cb=lambda m: self._ui(
                                           lambda: self._dub_set_status(m, ACCENT)))
            except PauseMapError as e:
                self._ui(lambda: self._dub_analysis_failed(str(e)))
                return
            except Exception as e:                       # noqa: BLE001 — surfaced
                self._ui(lambda: self._dub_analysis_failed(
                    f"Unexpected error while analysing:\n{e}"))
                return
            self._ui(lambda: self._dub_analysis_done(pmap))

        threading.Thread(target=_worker, daemon=True).start()

    def _dub_analysis_failed(self, message: str):
        self._dub_running = False
        for b in self._dub_buttons:
            b.config(state="normal")
        self._dub_gen_btn.config(state="disabled")
        self._dub_set_status("Analysis failed.", ERR_RED)
        messagebox.showerror("Sadhguru VO", message)

    def _dub_analysis_done(self, pmap):
        self._dub_running = False
        for b in self._dub_buttons:
            b.config(state="normal")
        self._dub_pmap = pmap
        self._dub_detect_note.config(text=pmap.summary(), fg=TEXT_MUTED)
        self._dub_set_status(f"Found {pmap.count} segments.", ACCENT)
        self._dub_align_scripts()

    # ═════════════════════════════════════════════════════════════════════════
    #  Timing source
    # ═════════════════════════════════════════════════════════════════════════
    def _dub_timing(self) -> str:
        value = self._dub_timing_var.get()
        return value if value in TIMING_MODES else DEFAULT_TIMING

    def _dub_on_timing_change(self):
        """Switch modes, remember the choice, and rebuild from what is there."""
        self._prefs["studio_timing"] = self._dub_timing()
        write_prefs(self._prefs)
        self._dub_apply_timing()
        # A pause map belongs to a recording. Keeping it across a switch to
        # script timing would leave the timeline showing measured positions for
        # chunks that are no longer measured against anything.
        if self._dub_timing() == TIMING_SCRIPT:
            self._dub_pmap = None
        self._dub_build_chunks(quiet=True)

    def _dub_apply_timing(self):
        """Show only the controls that mean something in the current mode."""
        audio = self._dub_timing() == TIMING_AUDIO

        for widget, wanted in ((self._dub_audio_bar, audio),
                               (self._dub_detect_bar, audio),
                               (self._dub_gaps_bar, not audio)):
            if wanted:
                widget.pack(fill="x", after=self._dub_timing_bar_anchor(widget))
            else:
                widget.pack_forget()

        # The sync dropdown is about holding a dub to a recording's timestamps.
        # With no recording there is nothing to hold to, so it is not offered
        # rather than offered and quietly ignored.
        self._dub_timing_note.config(
            text=("pauses measured from the recording" if audio
                  else "gaps computed between turns, as the Dialogue tab does"))
        self._dub_gen_btn.config(
            text="  ▶  Generate Dub  " if audio else "  ▶  Render Script  ")

    def _dub_timing_bar_anchor(self, widget):
        """Keep the bars in their declared order when re-packed."""
        order = [self._dub_audio_bar, self._dub_detect_bar, self._dub_gaps_bar]
        index = order.index(widget)
        for earlier in reversed(order[:index]):
            if earlier.winfo_manager():
                return earlier
        return self._dub_timing_bar

    # ═════════════════════════════════════════════════════════════════════════
    #  Building the chunk list
    # ═════════════════════════════════════════════════════════════════════════
    def _dub_build_chunks(self, quiet: bool = False):
        """Produce chunks from whichever timing source is selected."""
        if self._dub_timing() == TIMING_AUDIO:
            if self._dub_pmap is None:
                if not quiet:
                    messagebox.showinfo(
                        "Sadhguru VO",
                        "Analyse the source audio first — in this mode the "
                        "chunks come from its pauses.")
                return
            self._dub_align_scripts()
            return

        src = self._dub_src_text.get("1.0", "end").strip()
        tgt = self._dub_tgt_text.get("1.0", "end").strip()
        if not src:
            if not quiet:
                messagebox.showinfo("Sadhguru VO",
                                    "Paste a script — that is what gets rendered.")
            self._dub_chunks = []
            self._dub_build_rows()
            self._dub_estimate()
            return

        self._dub_chunks, self._dub_report = plan_from_script(
            src, tgt,
            gap_same_ms=self._dub_int(self._dub_gap_same_var, DIALOGUE_GAP_SAME_MS),
            gap_switch_ms=self._dub_int(self._dub_gap_switch_var,
                                        DIALOGUE_GAP_SWITCH_MS),
            merge_same_speaker=bool(self._dub_merge_var.get()))
        self._dub_align_note.config(text="  ".join(self._dub_report.notes()))
        # Same two steps as the audio path. A planned script names its speakers
        # exactly like a dubbed one does, so it needs the same cast table —
        # refreshing the note alone leaves the table hidden, because the note is
        # a no-op until _dub_speakers is populated.
        self._dub_speakers = list(self._dub_report.speakers)
        self._dub_rebuild_cast()
        self._dub_build_rows()
        self._dub_estimate()

    # ═════════════════════════════════════════════════════════════════════════
    #  Speaker detection
    # ═════════════════════════════════════════════════════════════════════════
    def _dub_schedule_detect(self, _event=None):
        """Debounce detection so it runs after typing stops, not during."""
        if self._dub_detect_job is not None:
            try:
                self.root.after_cancel(self._dub_detect_job)
            except tk.TclError:
                pass
        self._dub_detect_job = self.root.after(700, self._dub_detect_speakers)

    def _dub_detect_speakers(self):
        """Show the cast as soon as the script names somebody.

        The Dialogue tab has always done this on a timer, and it matters more
        here than it looks: with no cast table there is no per-speaker recipe,
        so a Sadhguru line would render one-step in whatever single voice the
        Target dropdown happened to hold — quietly, and only discovered after
        paying for it.

        Only speakers are recomputed. Re-chunking on every keystroke would
        discard per-chunk translation edits, so that stays on the button.
        """
        self._dub_detect_job = None
        script = self._dub_src_text.get("1.0", "end").strip()
        names = speakers_in_script(script)

        # Rebuilding an unchanged table would close a dropdown mid-choice.
        if names == self._dub_speakers:
            return

        self._dub_speakers = names
        if names:
            # Merge in defaults for anyone new, keeping every voice already
            # chosen. Sadhguru arrives two-step with both voices filled; anyone
            # else arrives blank on purpose, so validate_cast() forces a real
            # decision rather than lending them Sadhguru's voice.
            self._cast = cast_for_speakers(names, self._cast)
        self._dub_rebuild_cast()

        if not self._dub_chunks:
            self._dub_align_note.config(
                text=(f"{len(names)} speaker(s) detected — set their voices "
                      f"above, then build the timeline."
                      if names else
                      "No speaker labels found — this renders as a single "
                      "voice. Label lines as  NAME: text  to cast them."))

    def _dub_align_scripts(self):
        """Split both scripts across the detected segments and re-estimate."""
        if self._dub_pmap is None:
            messagebox.showinfo("Sadhguru VO",
                                "Analyse the source audio first — the chunks come "
                                "from its pauses.")
            return
        src = self._dub_src_text.get("1.0", "end").strip()
        tgt = self._dub_tgt_text.get("1.0", "end").strip()
        self._dub_chunks, self._dub_report = align(self._dub_pmap, src, tgt)
        self._dub_align_note.config(text="  ".join(self._dub_report.notes()))
        self._dub_speakers = list(self._dub_report.speakers)
        self._dub_rebuild_cast()
        self._dub_build_rows()
        self._dub_estimate()

    # ═════════════════════════════════════════════════════════════════════════
    #  Chunk table
    # ═════════════════════════════════════════════════════════════════════════
    def _dub_build_rows(self):
        """Rebuild the table. Only called when the chunk list itself changes —
        editing text updates labels in place instead."""
        for child in self._dub_table_inner.winfo_children():
            child.destroy()
        self._dub_rows = []

        for chunk in self._dub_chunks:
            row = tk.Frame(self._dub_table_inner, bg=BG)
            row.pack(fill="x", pady=S(1))

            tk.Label(row, text=str(chunk.index + 1), bg=BG, fg=TEXT_FAINT,
                     font=F_MONO8, width=4, anchor="w"
                     ).pack(side="left", padx=(S(4), S(0)))

            # Gold for a two-step speaker, blue for one-step — the same colour
            # language the Dialogue tab's cast table uses.
            recipe = self._cast.get(chunk.key) if chunk.speaker else None
            tk.Label(row, text=_clip(chunk.speaker, 13) or "—", bg=BG,
                     fg=(STEP1_GOLD if recipe is not None and recipe.two_step
                         else REG_LABEL if chunk.speaker else TEXT_FAINT),
                     font=F_MONO8B, width=13, anchor="w"
                     ).pack(side="left", padx=(S(4), S(0)))

            tk.Label(row, text=_ts(chunk.start_ms), bg=BG, fg=TEXT_MUTED,
                     font=F_MONO8, width=11, anchor="w"
                     ).pack(side="left", padx=(S(4), S(0)))
            tk.Label(row, text=f"{chunk.duration_ms}", bg=BG, fg=TEXT_MUTED,
                     font=F_MONO8, width=8, anchor="w"
                     ).pack(side="left", padx=(S(4), S(0)))
            tk.Label(row, text=f"{chunk.pause_after_ms}", bg=BG, fg=TEXT_FAINT,
                     font=F_MONO8, width=6, anchor="w"
                     ).pack(side="left", padx=(S(4), S(0)))
            tk.Label(row, text=_clip(chunk.source_text, 26), bg=BG, fg=TEXT_MUTED,
                     font=F_MONO8, width=26, anchor="w"
                     ).pack(side="left", padx=(S(4), S(0)))

            var = tk.StringVar(value=chunk.target_text)
            entry = tk.Entry(row, textvariable=var, width=32, bg=INPUT_BG,
                             fg=INPUT_FG, insertbackground=INPUT_FG,
                             relief="flat", font=F_MONO8)
            entry.pack(side="left", padx=(S(4), S(0)))
            var.trace_add("write",
                          lambda *_a, i=chunk.index: self._dub_text_edited(i))

            est = tk.Label(row, text="—", bg=BG, fg=TEXT_FAINT,
                           font=F_MONO8, width=12, anchor="w")
            est.pack(side="left", padx=(S(4), S(0)))
            fit = tk.Label(row, text="—", bg=BG, fg=TEXT_FAINT,
                           font=F_MONO8B, width=9, anchor="w")
            fit.pack(side="left", padx=(S(4), S(0)))

            merge_btn = tk.Label(row, text="⇊", bg=BG, fg=TEXT_FAINT,
                                 font=F_MONO9, cursor="hand2")
            merge_btn.pack(side="left", padx=(S(4), S(0)))
            merge_btn.bind("<Button-1>",
                           lambda _e, i=chunk.index: self._dub_merge_row(i))

            self._dub_rows.append({"frame": row, "var": var, "entry": entry,
                                   "est": est, "fit": fit, "index": chunk.index})

    def _dub_text_edited(self, index: int):
        """Copy an edited translation back into the chunk and re-estimate."""
        for row in self._dub_rows:
            if row["index"] == index:
                for c in self._dub_chunks:
                    if c.index == index:
                        c.target_text = row["var"].get()
                break
        self._dub_schedule_estimate()

    def _dub_schedule_estimate(self):
        """Debounce — re-estimating on every keystroke redraws the whole
        timeline while the user is still typing a word."""
        if self._dub_estimate_job is not None:
            try:
                self.root.after_cancel(self._dub_estimate_job)
            except tk.TclError:
                pass
        self._dub_estimate_job = self.root.after(350, self._dub_estimate)

    def _dub_merge_row(self, index: int):
        """Join this chunk with the next — detection split one sentence."""
        if not self._dub_chunks or index >= len(self._dub_chunks) - 1:
            return
        self._dub_chunks = merge(self._dub_chunks, index)
        if self._dub_pmap is not None:
            self._dub_pmap = merge_at(self._dub_pmap, index)
        self._dub_build_rows()
        self._dub_estimate()

    # ═════════════════════════════════════════════════════════════════════════
    #  Estimate
    # ═════════════════════════════════════════════════════════════════════════
    def _dub_estimate(self):
        """Re-run the whole preview. Local, instant, free."""
        self._dub_estimate_job = None
        if not self._dub_chunks:
            self._dub_gen_btn.config(state="disabled")
            return

        mode = self._dub_mode()
        lead_in = self._dub_pmap.lead_in_ms if self._dub_pmap else 0
        self._dub_preview = preview(self._dub_chunks,
                                    target_language=self._dub_tgt_lang_var.get(),
                                    source_language=self._dub_src_lang_var.get(),
                                    mode=mode,
                                    lead_in_ms=lead_in)

        by_index = {e.index: e for e in self._dub_preview.estimates}
        for row in self._dub_rows:
            e = by_index.get(row["index"])
            if e is None:
                continue
            colour = VERDICT_COLOUR.get(e.verdict, TEXT_FAINT)
            row["est"].config(text=f"{e.estimated_ms} ms", fg=colour)
            badge = e.verdict
            if e.verdict == TIGHT and e.speed > 1.0:
                badge = f"{e.speed:.2f}×"
            row["fit"].config(text=badge, fg=colour)

        self._dub_notes.config(text="\n".join(self._dub_preview.notes()))
        drift = self._dub_preview.final_drift_ms
        if self._dub_preview.planned:
            # Nothing to drift against. The number worth showing is the one you
            # would otherwise only learn by paying for the render.
            total = self._dub_preview.dub_total_ms
            self._dub_drift_label.config(
                text=f"  estimated length: {total // 60000}:"
                     f"{(total % 60000) / 1000.0:04.1f}", fg=TEXT_MUTED)
        elif mode == SYNC_LOCK:
            self._dub_drift_label.config(text="  locked — zero drift", fg=ACCENT)
        else:
            colour = ACCENT if abs(drift) < 1000 else (
                WARN_AMBER if abs(drift) < 5000 else ERR_RED)
            self._dub_drift_label.config(
                text=f"  drift at end: {drift / 1000.0:+.1f}s", fg=colour)

        self._dub_draw_timeline()
        # The whole-mix check depends on the cast and on which chunks have text,
        # both of which this pass has just settled.
        self._dub_sts_refresh()
        ready = bool(self._dub_chunks) and any(
            c.target_text.strip() for c in self._dub_chunks)
        self._dub_gen_btn.config(state="normal" if ready else "disabled")

    # ═════════════════════════════════════════════════════════════════════════
    #  Timeline drawing
    # ═════════════════════════════════════════════════════════════════════════
    def _dub_draw_timeline(self):
        c = getattr(self, "_dub_canvas", None)
        if c is None:
            return
        c.delete("all")
        if not self._dub_chunks or self._dub_preview is None:
            c.configure(scrollregion=(0, 0, 0, S(_TL_HEIGHT)))
            return

        pps = max(4.0, float(self._dub_zoom_var.get())) / 1000.0   # px per ms
        pv = self._dub_preview
        total_ms = max(pv.source_total_ms, pv.dub_total_ms) + 500
        width = int(total_ms * pps) + 20
        c.configure(scrollregion=(0, 0, width, S(_TL_HEIGHT)))

        y_src = S(_RULER_H)
        y_dub = S(_RULER_H) + S(_LANE_H) + S(_LANE_GAP)

        # ── Ruler ────────────────────────────────────────────────────────────
        step = _ruler_step(pps)
        t = 0
        while t <= total_ms:
            x = 10 + t * pps
            c.create_line(x, S(_RULER_H) - S(6), x, S(_RULER_H), fill="#334155")
            c.create_text(x + 2, 6, text=_ts(t, short=True), anchor="w",
                          fill=TEXT_FAINT, font=F_MONO7)
            t += step

        # A planned timeline has no source to draw. Showing an empty SRC lane
        # would invite the eye to compare the dub against nothing, so the plan
        # gets the single lane it deserves.
        planned = pv.planned
        if planned:
            y_dub = y_src
        else:
            c.create_text(S(4), y_src + S(_LANE_H) / 2, text="SRC", anchor="w",
                          fill=TEXT_FAINT, font=F_MONO7B)
        c.create_text(S(4), y_dub + S(_LANE_H) / 2, text="PLAN" if planned else "DUB",
                      anchor="w", fill=TEXT_FAINT, font=F_MONO7B)

        by_index = {e.index: e for e in pv.estimates}
        for chunk in self._dub_chunks:
            e = by_index.get(chunk.index)
            if e is None:
                continue

            # Source lane — what the original speaker did.
            x0 = 10 + chunk.start_ms * pps
            x1 = 10 + chunk.end_ms * pps
            if not planned:
                c.create_rectangle(x0, y_src, max(x1, x0 + 2), y_src + S(_LANE_H),
                                   fill="#1e3a5f", outline="#3b82f6",
                                   tags=(f"chunk{chunk.index}",))
                if x1 - x0 > 18:
                    c.create_text(x0 + S(3), y_src + S(_LANE_H) / 2, anchor="w",
                                  text=str(chunk.index + 1), fill="#93c5fd",
                                  font=F_MONO7)

            # Dub lane — where the translation actually lands.
            d0 = 10 + e.dub_start_ms * pps
            d1 = 10 + max(e.dub_end_ms, e.dub_start_ms + 1) * pps
            colour = VERDICT_COLOUR.get(e.verdict, TEXT_FAINT)
            c.create_rectangle(d0, y_dub, max(d1, d0 + 2), y_dub + S(_LANE_H),
                               fill=_dim(colour), outline=colour,
                               tags=(f"chunk{chunk.index}",))
            if d1 - d0 > 18:
                c.create_text(d0 + S(3), y_dub + S(_LANE_H) / 2, anchor="w",
                              text=str(chunk.index + 1), fill=colour,
                              font=F_MONO7)

            # Drift connector — the visible answer to "how far has it slipped".
            if e.drift_ms:
                c.create_line(x0, y_src + S(_LANE_H), d0, y_dub,
                              fill=ERR_RED if abs(e.drift_ms) > 1000 else WARN_AMBER,
                              dash=(2, 2))

    def _dub_canvas_click(self, event):
        """Clicking a block scrolls the table to that chunk and focuses it."""
        c = self._dub_canvas
        items = c.find_overlapping(c.canvasx(event.x) - 1, event.y - 1,
                                   c.canvasx(event.x) + 1, event.y + 1)
        for item in items:
            for tag in c.gettags(item):
                if not tag.startswith("chunk"):
                    continue
                try:
                    index = int(tag[5:])
                except ValueError:
                    continue
                for pos, row in enumerate(self._dub_rows):
                    if row["index"] != index:
                        continue
                    total = max(1, len(self._dub_rows))
                    self._dub_table_canvas.yview_moveto(max(0.0, pos / total - 0.1))
                    row["entry"].focus_set()
                    return

    def _dub_canvas_split(self, event):
        """Right-click a source block to cut it at that point.

        The other half of the merge control. Detection cannot hear that two
        sentences ran together under the pause threshold, so when they did, the
        fix is to say where the boundary should have been — and the click
        position is already exactly that.
        """
        if not self._dub_chunks:
            return
        if self._dub_timing() == TIMING_SCRIPT:
            # Splitting cuts a measured segment at a point in a recording. A
            # planned chunk is a line of script — split it by editing the
            # script, which is where its boundaries actually live.
            self._dub_set_status(
                "Split the line in the script box — planned chunks have no "
                "recording to cut.", WARN_AMBER)
            return
        pps = max(4.0, float(self._dub_zoom_var.get())) / 1000.0
        at_ms = int((self._dub_canvas.canvasx(event.x) - 10) / pps)

        for i, chunk in enumerate(self._dub_chunks):
            if not chunk.start_ms < at_ms < chunk.end_ms:
                continue
            # Refuse a cut that would leave a fragment too short to be a chunk.
            if min(at_ms - chunk.start_ms, chunk.end_ms - at_ms) < 120:
                self._dub_set_status(
                    "Too close to the edge of that chunk to split there.",
                    WARN_AMBER)
                return
            self._dub_chunks = split(self._dub_chunks, i, at_ms)
            if self._dub_pmap is not None:
                self._dub_pmap = pm_split_at(self._dub_pmap, i, at_ms)
            self._dub_build_rows()
            self._dub_estimate()
            self._dub_set_status(f"Split chunk {i + 1} at {_ts(at_ms)}.", ACCENT)
            return

    # ═════════════════════════════════════════════════════════════════════════
    #  Generation — the only step that spends anything
    # ═════════════════════════════════════════════════════════════════════════
    def _dub_generate(self):
        if self._dub_running:
            return
        if self._dub_preview is None or not self._dub_chunks:
            return

        api_key = self._api_key_var.get().strip()
        if not api_key:
            messagebox.showwarning("Sadhguru VO", "Paste your ElevenLabs API key "
                                                  "in the box at the top.")
            return
        voice_id = self._voice_id_for_label(self._dub_voice_var.get()) \
            or self._dub_voice_id

        # With speakers, the cast supplies the voices and has to be complete
        # before the first call — a run that dies on chunk forty because one
        # speaker had no voice has already burnt thirty-nine chunks of quota.
        if self._dub_speakers:
            problems = validate_cast(self._cast, self._dub_speakers)
            if problems:
                messagebox.showwarning(
                    "Sadhguru VO",
                    "This cast cannot render the dub yet:\n  - "
                    + "\n  - ".join(problems)
                    + "\n\nFill the gaps in the cast table above.")
                return
        elif not voice_id:
            messagebox.showwarning("Sadhguru VO", "Choose a target voice.")
            return

        if not self._dub_out_path:
            self._dub_pick_output()
            if not self._dub_out_path:
                return

        pv = self._dub_preview
        billable = sum(1 for c in self._dub_chunks if c.target_text.strip())
        chars = sum(len(c.target_text) for c in self._dub_chunks)

        # A two-step speaker costs two calls per chunk, not one. Worth saying
        # before the button is pressed rather than discovering it on the bill.
        two_step = sum(1 for c in self._dub_chunks
                       if c.target_text.strip() and c.speaker
                       and self._cast.get(c.key) is not None
                       and self._cast[c.key].two_step)
        # A whole-mix conversion replaces every per-chunk step 2 with a handful
        # of passes over the finished audio, so the call count drops sharply.
        sts_mode = self._dub_sts_mode()
        sts_line = ""
        if sts_mode == DUB_STS_WHOLE:
            plan = plan_whole_sts(
                self._dub_chunks,
                dict(self._cast) if self._dub_speakers else None,
                voice_id, self._prefs["step1_model"], None)
            if plan.ok:
                calls = billable
                sts_line = ("\nVoice change: once over the finished mix "
                            "(a few passes at most, not one per chunk)")
            else:
                calls = billable + two_step
                sts_line = f"\nVoice change: per chunk — {plan.reason}"
        else:
            calls = billable + two_step

        cast_line = ""
        if self._dub_speakers:
            per = ", ".join(
                f"{n} ({'2-step' if self._cast[normalize_speaker(n)].two_step else '1-step'})"
                for n in self._dub_speakers)
            cast_line = f"\nCast: {per}"

        warn = ""
        if pv.counts.get(REWRITE):
            warn = (f"\n\n⚠ {pv.counts[REWRITE]} chunk(s) are flagged REWRITE — "
                    f"they will run past their slot rather than be stretched "
                    f"audibly. Shortening those translations first costs nothing.")
        if pv.counts.get(EMPTY):
            warn += (f"\n⚠ {pv.counts[EMPTY]} chunk(s) have no translation and "
                     f"will be silent.")

        if not messagebox.askyesno(
                "Generate dub — this spends credits",
                f"{billable} chunks · about {chars} characters · "
                f"{calls} API calls\n"
                f"Mode: {SYNC_MODE_LABELS[self._dub_mode()]}"
                + (cast_line if cast_line
                   else f"\nVoice: {self._dub_voice_var.get() or voice_id}")
                + sts_line
                + f"{warn}\n\nGenerate now?"):
            return

        mode = self._dub_mode()
        lead_in = self._dub_pmap.lead_in_ms if self._dub_pmap else 0
        source_total = self._dub_pmap.total_ms if self._dub_pmap else 0
        chunks = list(self._dub_chunks)
        out_path = self._dub_out_path
        language = self._dub_tgt_lang_var.get()
        cast = dict(self._cast) if self._dub_speakers else None

        self._dub_running = True
        self._dub_cancel = False
        for b in self._dub_buttons:
            b.config(state="disabled")
        self._dub_set_status("Generating…", ACCENT)

        def _worker():
            try:
                result = render_dub(
                    chunks, out_path, api_key=api_key, voice_id=voice_id,
                    target_language=language, mode=mode, lead_in_ms=lead_in,
                    source_total_ms=source_total, cast=cast,
                    sts_mode=sts_mode,
                    on_status=lambda m: self._ui(
                        lambda: self._dub_set_status(m, ACCENT)),
                    on_chunk=lambda n, total, rc: self._ui(
                        lambda: self._dub_set_status(
                            f"Chunk {n}/{total} · {rc.final_ms} ms · {rc.verdict}",
                            ACCENT)),
                    should_cancel=lambda: self._dub_cancel)
            except (DubRenderError, Exception) as e:      # noqa: BLE001
                self._ui(lambda: self._dub_generate_failed(str(e)))
                return
            self._ui(lambda: self._dub_generate_done(result))

        threading.Thread(target=_worker, daemon=True).start()

    def _dub_generate_failed(self, message: str):
        self._dub_running = False
        for b in self._dub_buttons:
            b.config(state="normal")
        self._dub_set_status("Generation failed.", ERR_RED)
        messagebox.showerror("Sadhguru VO", message)

    def _dub_generate_done(self, result):
        self._dub_running = False
        for b in self._dub_buttons:
            b.config(state="normal")
        self._dub_last_output = result.output_path
        self._dub_set_status(result.summary(), ACCENT)

        extra = ""
        if result.rate_updated is not None:
            extra = (f"\n\nSpeaking rate for {self._dub_tgt_lang_var.get()} updated "
                     f"to {result.rate_updated.units_per_sec:.2f} syllables/sec "
                     f"from this render — the next preview will be closer.")
        if result.unfitted:
            extra += (f"\n\n⚠ {result.unfitted} chunk(s) ran past their slot rather "
                      f"than be stretched past the transparent limit. They are "
                      f"listed in the manifest.")
        messagebox.showinfo(
            "Dub complete",
            f"{result.summary()}\n\n{result.output_path}"
            + (f"\n{result.manifest_path}" if result.manifest_path else "")
            + extra)


# ═════════════════════════════════════════════════════════════════════════════
#  Small helpers
# ═════════════════════════════════════════════════════════════════════════════

def _ts(ms: int, short: bool = False) -> str:
    ms = max(0, int(ms))
    minutes, rem = divmod(ms, 60_000)
    if short:
        return f"{minutes}:{rem // 1000:02d}"
    return f"{minutes}:{rem / 1000:06.3f}"


def _clip(text: str, width: int) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= width else flat[:width - 1] + "…"


def _ruler_step(px_per_ms: float) -> int:
    """A tick spacing that keeps labels from colliding at this zoom."""
    for step in (1000, 2000, 5000, 10_000, 15_000, 30_000, 60_000, 120_000):
        if step * px_per_ms >= 55:
            return step
    return 300_000


def _dim(hex_colour: str, factor: float = 0.32) -> str:
    """A muted fill for a block whose outline is *hex_colour*."""
    try:
        h = hex_colour.lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, IndexError):
        return "#1f2937"
    return "#%02x%02x%02x" % (int(r * factor), int(g * factor), int(b * factor))
