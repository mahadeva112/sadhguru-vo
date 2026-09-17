"""
Constants, paths and the dark theme palette for the standalone Sadhguru VO app.

Everything the two-step pipeline needs to know that isn't user state lives
here. User state (API key, chosen voices / models, favourites) lives in
prefs.py and is persisted next to the app.
"""

import os
import ssl
import sys

APP_NAME    = "Sadhguru VO"
APP_VERSION = "1.0.0"

# Stable id so Windows groups the taskbar button under our own icon instead of
# lumping it in with a generic "Python" button. Set by gui.py at startup.
APP_USER_MODEL_ID = "SadhguruVO.Standalone.1"

# ─── Paths ───────────────────────────────────────────────────────────────────
# APP_DIR is the folder holding the app (one level above this package), so all
# config files sit next to the launcher where the user can see them. Under a
# PyInstaller one-file build sys.frozen is set and the exe's own folder is used.
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROMPTS_DIR       = os.path.join(APP_DIR, "prompts")
ASSETS_DIR        = os.path.join(APP_DIR, "assets")
ICON_PATH         = os.path.join(ASSETS_DIR, "sadhguru_vo.ico")

API_KEY_FILE      = os.path.join(APP_DIR, "api.txt")
PREFS_FILE        = os.path.join(APP_DIR, "sadhguru_vo_prefs.json")
FAV_VOICES_FILE   = os.path.join(APP_DIR, "fav_voices.json")
# Default cast for the Dialogue tab. A project can point at its own file
# instead; this one is just what the tab loads and auto-saves when it isn't told
# otherwise.
CAST_FILE         = os.path.join(APP_DIR, "cast.json")
LLM_SETTINGS_FILE = os.path.join(APP_DIR, "llm_settings.json")
ERROR_LOG_FILE    = os.path.join(APP_DIR, "error_log.txt")

# ─── Platform ────────────────────────────────────────────────────────────────
IS_WINDOWS = sys.platform.startswith("win")
IS_MAC     = sys.platform == "darwin"

if IS_WINDOWS:
    MONO_FONT, UI_FONT = "Consolas", "Segoe UI"
elif IS_MAC:
    MONO_FONT, UI_FONT = "Menlo", "Helvetica Neue"
else:
    MONO_FONT, UI_FONT = "DejaVu Sans Mono", "DejaVu Sans"

# ─── TLS ─────────────────────────────────────────────────────────────────────
# Verification is disabled to match the original app's behaviour: several of the
# corporate networks this runs on terminate TLS with a self-signed root that is
# not in the Python trust store, and the ElevenLabs calls fail outright with
# verification on. Only api.elevenlabs.io and the configured LLM endpoint are
# ever contacted.
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode    = ssl.CERT_NONE

# ─── ElevenLabs models ───────────────────────────────────────────────────────
# Step 1 (text-to-speech). Only eleven_v3 understands inline audio tags
# ([calm], [pause], …) — for the other models the tags are stripped before
# sending so they are not read aloud.
ELEVENLABS_TTS_MODELS = {
    "eleven_v3":              "v3 — expressive (audio tags)",
    "eleven_multilingual_v2": "Multilingual v2 — stable",
    "eleven_turbo_v2_5":      "Turbo v2.5 — fast",
    "eleven_flash_v2_5":      "Flash v2.5 — fastest",
}
ELEVENLABS_TTS_MODEL = "eleven_v3"

# Step 2 (speech-to-speech / voice changer). STS keeps the source performance
# (timing, emotion, delivery) and re-renders it in the target voice.
ELEVENLABS_STS_MODELS = {
    "eleven_multilingual_sts_v2": "Multilingual STS v2 — all languages",
    "eleven_english_sts_v2":      "English STS v2 — English only",
}
ELEVENLABS_STS_MODEL = "eleven_multilingual_sts_v2"

# Preference order used when the account cannot serve the model that was asked
# for. The catalogue at /v1/models is per-key — a model can be missing because
# the tier does not include it, because it was retired, or because a stale
# preferences file still names one that no longer exists. Rather than let the
# render fail with a 400 half an hour in, the first entry here the account
# *does* have is substituted automatically (see elevenlabs_api.resolve_model).
ELEVENLABS_TTS_FALLBACKS = (
    "eleven_v3",
    "eleven_multilingual_v2",
    "eleven_turbo_v2_5",
    "eleven_flash_v2_5",
)
ELEVENLABS_STS_FALLBACKS = (
    "eleven_multilingual_sts_v2",
    "eleven_english_sts_v2",
)

# ─── Pinned pipeline defaults ────────────────────────────────────────────────
# Step 1 renders the script with a TTS voice; Step 2 pushes that audio through
# speech-to-speech in a second voice. Both stay editable in the UI / on the
# command line — these are just the defaults the "↺" reset buttons snap back to.
#
# The actual voice ids and their display names are studio-specific, so they are
# NOT committed. They live in voices.local.json next to the app (gitignored);
# copy voices.example.json to voices.local.json and fill in your own. Without
# that file the app still runs — the reset buttons simply have nothing pinned
# and you pick voices from the dropdown as usual.
VOICES_FILE = os.path.join(APP_DIR, "voices.local.json")

_VOICE_DEFAULTS = {
    "step1_voice_id":   "",
    "step1_voice_name": "Step-1 voice",
    "step2_voice_id":   "",
    "step2_voice_name": "Step-2 voice",
}


def _load_pinned_voices():
    """Read voices.local.json, falling back to the neutral defaults above.

    Forgiving on purpose, matching how cast.py reads its own JSON: a missing
    file, bad JSON or a stray field must never stop the app from starting.
    """
    values = dict(_VOICE_DEFAULTS)
    try:
        import json
        with open(VOICES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for key in _VOICE_DEFAULTS:
                val = data.get(key)
                if isinstance(val, str) and val.strip():
                    values[key] = val.strip()
    except (OSError, ValueError):
        pass
    return values


_PINNED = _load_pinned_voices()

STEP1_VOICE_ID   = _PINNED["step1_voice_id"]
STEP1_VOICE_NAME = _PINNED["step1_voice_name"]
STEP2_VOICE_ID   = _PINNED["step2_voice_id"]
STEP2_VOICE_NAME = _PINNED["step2_voice_name"]

# Language used for the optional emotion-tag pass and for the ✦ "voice supports
# this language" hint in the voice dropdown.
VO_LANGUAGE = "Hindi"

# ElevenLabs voice-metadata tokens per language, used to sort voices that
# advertise support for the target language to the top of the dropdown.
LANGUAGE_TOKENS = {
    "Hindi":     ("hi", "hin", "hindi", "हिन्दी", "hi-in"),
    "Bengali":   ("bn", "ben", "bengali", "bangla", "bn-in", "bn-bd", "বাংলা"),
    "Tamil":     ("ta", "tam", "tamil", "தமிழ்", "ta-in"),
    "Telugu":    ("te", "tel", "telugu", "తెలుగు", "te-in"),
    "Kannada":   ("kn", "kan", "kannada", "ಕನ್ನಡ", "kn-in"),
    "Malayalam": ("ml", "mal", "malayalam", "മലയാളം", "ml-in"),
    "Marathi":   ("mr", "mar", "marathi", "मराठी", "mr-in"),
    "Gujarati":  ("gu", "guj", "gujarati", "ગુજરાતી", "gu-in"),
    "Odia":      ("or", "ori", "odia", "oriya", "ଓଡ଼ିଆ", "or-in"),
    "Assamese":  ("as", "asm", "assamese", "অসমীয়া", "as-in"),
    "Nepali":    ("ne", "nep", "nepali", "नेपाली", "ne-np"),
}

# Characters per ElevenLabs TTS request chunk.
ELEVENLABS_CHUNK_CHARS = 1000

# Audio format requested from TTS, in order of preference.
#
# OPT-IN, and only the Dialogue pipeline opts in. The single-speaker tab passes
# no ladder at all and keeps the original MP3 request and pydub join, seam
# artefacts included, because its output is pinned to what it has always
# produced. Everything below is about what the Dialogue path gets.
#
# PCM rather than MP3, and this is the whole reason the joins between chunks
# used to tick. MP3 is a block format: every file carries encoder priming
# samples at the front and zero padding to fill its last 1152-sample frame, and
# its first and last blocks decode wrong because the overlap-add has no
# neighbouring block to work with. Butt two decoded MP3s together and you get a
# few ms of dead air plus a few ms of malformed waveform at every seam — which
# no fade can remove, because the artefact is inside the audio rather than at
# the boundary. Raw PCM has no frames, no priming and no padding, so joining two
# chunks is a byte append and the seam is not merely quiet but absent.
#
# The ladder exists because pcm_44100 needs a paid ElevenLabs tier. A rejection
# on format grounds steps down; pcm_24000 is still gapless (the point) and 12 kHz
# of bandwidth is plenty for speech. MP3 is last so a render never fails outright
# on an account that allows no PCM at all.
ELEVENLABS_TTS_FORMATS = ("pcm_44100", "pcm_24000", "mp3_44100_128")

# Same ladder for the voice changer, and again Dialogue-only. Step 2 is one
# request per turn, so there is no seam to worry about — this is about what
# leaves the API. The chain is already TTS then speech-to-speech; taking MP3 out
# of Step 2 removes a whole lossy generation from it, and on the Dialogue path it
# also removes an MP3 decode per turn, since the assembler decodes every clip
# anyway to lay it on the timeline.
ELEVENLABS_STS_FORMATS = ("pcm_44100", "pcm_24000", "mp3_44100_128")

# Give each chunk the text on either side of it. The model then generates the
# chunk knowing how the sentence before it ended and how the next one begins, so
# intonation carries across a seam instead of every chunk closing on a
# sentence-final fall and reopening at full energy. Costs nothing — these fields
# are context, not billed characters. Rides along with the format ladder, so it
# too applies to the Dialogue path only.
ELEVENLABS_STITCH_CONTEXT = True

# How much neighbouring text to send as that context.
ELEVENLABS_STITCH_CHARS = 400

# ─── Pipeline modes ──────────────────────────────────────────────────────────
# These live here rather than in pipeline.py so cast.py can name a speaker's
# mode without importing the pipeline (which imports cast). pipeline.py
# re-exports them, so `from .pipeline import MODE_BOTH` still works.
MODE_BOTH = "both"      # TTS, then speech-to-speech onto the target voice
MODE_TTS  = "tts"       # TTS only — for speakers using a stock voice as-is
MODE_VC   = "vc"        # speech-to-speech only, on audio that already exists
MODES     = (MODE_BOTH, MODE_TTS, MODE_VC)

# Indic-tuned TTS voice settings. Lower stability + raised style give eleven_v3
# room to act on the inline emotion / accent tags, so delivery is reflective
# rather than flat.
#
# This is the SINGLE-SPEAKER tab's setting, and it is pinned. That tab sends the
# script in ~1000-character blocks, so stability is sampled two or three times
# across a whole piece and each block is internally consistent — the variance
# buys expression and costs nothing.
DEFAULT_VOICE_SETTINGS = {
    "stability": 0.35,
    "similarity_boost": 0.80,
    "style": 0.40,
    "use_speaker_boost": True,
}

# The same settings for the CHUNKED tabs (Dialogue, Studio), where one API call
# is one turn or one pause-split chunk.
#
# Stability is raised because the arithmetic changes completely at that
# granularity. 0.35 across the single-speaker tab's two or three calls is
# expression; the same 0.35 across a 225-chunk dub re-rolls the delivery every
# four seconds, and what the listener hears is not expression but a voice whose
# energy and pitch will not settle. Trading some per-chunk range for consistency
# across the piece is the right side of that trade when the piece is made of
# hundreds of pieces.
#
# Separate from DEFAULT_VOICE_SETTINGS rather than a change to it, because the
# single-speaker tab's output is pinned to what it has always produced. Only the
# cast (cast.py) and the unlabelled-script fallback (dub_render) read this.
CHUNKED_VOICE_SETTINGS = {
    "stability": 0.50,
    "similarity_boost": 0.80,
    "style": 0.40,
    "use_speaker_boost": True,
}

# ─── Dialogue (multi-speaker) defaults ───────────────────────────────────────
# Silence inserted between turns. The gap after a speaker change is longer than
# the gap between two turns of the same speaker, which is what makes a rendered
# conversation sound like turn-taking rather than one continuous read.
DIALOGUE_GAP_SAME_MS   = 250
DIALOGUE_GAP_SWITCH_MS = 500

# Speech-to-speech needs material to work with — a one-second "हाँ।" comes back
# with artefacts. Turns shorter than this are padded with silence before Step 2
# and trimmed afterwards.
DIALOGUE_MIN_STS_MS = 1500
DIALOGUE_STS_PAD_MS = 700

# Per-speaker loudness match target (RMS dBFS). Without this the interviewer
# routinely lands several dB under Sadhguru and an editor has to fix it by hand.
DIALOGUE_TARGET_DBFS = -20.0

# ── Turn edges ───────────────────────────────────────────────────────────────
# ElevenLabs TTS hands back audio that ends the instant the last phoneme does,
# with the waveform still well away from zero. Dropped straight onto a silent
# timeline that is a step discontinuity, and a step discontinuity is a click —
# audible at the end of every turn. A short fade takes the waveform to zero;
# at this length it is inaudible on speech but removes the click completely.
DIALOGUE_EDGE_FADE_MS = 12

# Clips also arrive with a variable amount of silence already on them — the
# speech-to-speech ones especially. Left alone, the gap between two turns is
# the configured gap PLUS whatever each clip happened to carry, so the pacing
# drifts. Trimming first makes the configured gap the whole gap.
DIALOGUE_TRIM_TURN_SILENCE = True
DIALOGUE_TRIM_KEEP_MS      = 50      # margin left so a soft onset is never cut
DIALOGUE_TRIM_THRESHOLD_DB = -50.0   # below the quietest real speech

# Export sample rate / channels for the assembled master and stems.
DIALOGUE_FRAME_RATE = 44100
DIALOGUE_CHANNELS   = 1

# ─── Dub Sync (pause-aware dubbing) ──────────────────────────────────────────
# A pause only counts as a pause if it is long enough to be heard as one. Below
# this the "silence" is the stop before a plosive or the gap between two words,
# and cutting there would shatter one sentence into a dozen chunks nobody can
# translate. 350 ms is roughly the shortest gap a listener reads as deliberate.
DUB_MIN_PAUSE_MS   = 350

# Detected speech shorter than this is a lip smack, a breath or a click, not a
# chunk. Absorbed into the previous segment rather than dropped, so no audio
# goes missing from the timeline.
DUB_MIN_SEGMENT_MS = 200

# Detection runs on a mono 8 kHz copy. Pause boundaries are wanted to ~10 ms,
# nowhere near the precision that the extra 36 kHz would buy, and the downsample
# makes the level profile roughly 5× faster to compute on a long recording.
DUB_ANALYSIS_RATE = 8000
DUB_FRAME_MS      = 20      # level-profile resolution
DUB_SEEK_STEP_MS  = 5       # boundary resolution used by the silence scan

# The silence threshold is derived from the recording rather than fixed. A fixed
# dBFS floor is the usual reason pause detection fails: set it for a loud studio
# read and a quiet phone recording comes back as one unbroken segment; set it
# for the quiet one and the loud one shatters. So the noise floor and the speech
# level are measured per file (low / high percentile of the frame levels) and
# the threshold is placed between them.
DUB_NOISE_PCT   = 10        # percentile taken as the noise floor
DUB_SPEECH_PCT  = 90        # percentile taken as the speech level
DUB_NOISE_MARGIN_DB    = 6.0    # threshold sits this far above the noise floor
DUB_SPEECH_HEADROOM_DB = 8.0    # …but never closer than this to speech level
# If the gap between floor and speech is smaller than this the recording is too
# compressed or too noisy to read percentiles off, and a plain offset below the
# speech level is the safer guess.
DUB_MIN_RANGE_DB       = 10.0
DUB_FALLBACK_OFFSET_DB = 16.0

# ── Sync modes ───────────────────────────────────────────────────────────────
# Elastic reproduces every pause at its source length and lets each chunk run as
# long as it naturally runs, so nothing is time-stretched and nothing sounds
# processed — the dub drifts away from the source instead, and the preview's job
# is to show by how much. Hard lock pins each chunk to its source timestamp and
# buys the difference back by stretching within a transparent range and eating
# into the pause that follows. Elastic is the default: an unprocessed voice with
# known drift beats a locked one that sounds squeezed.
# ── Timing source ────────────────────────────────────────────────────────────
# The one thing that separates a dialogue render from a dub. Everything else —
# the cast, the per-speaker recipes, the renderer, the assembler — is already
# shared, so this is the only switch the Studio tab actually needs.
#
#   TIMING_SCRIPT   no recording. The gap after a chunk comes from the gap rule
#                   (longer after a speaker change than within one speaker's
#                   run), which is what makes a rendered script sound like a
#                   conversation rather than one continuous read.
#   TIMING_AUDIO    a source recording. The gap after a chunk is the pause that
#                   was actually measured there.
#
# Downstream, script timing behaves exactly like elastic sync: advance by what
# was spoken, then by the gap. That is why the estimator needs no special case.
TIMING_SCRIPT = "script"
TIMING_AUDIO  = "audio"
TIMING_MODES  = (TIMING_SCRIPT, TIMING_AUDIO)
TIMING_LABELS = {
    TIMING_SCRIPT: "Script only — gaps computed",
    TIMING_AUDIO:  "Source audio — pauses measured",
}
DEFAULT_TIMING = TIMING_SCRIPT

SYNC_ELASTIC = "elastic"
SYNC_LOCK    = "lock"
SYNC_MODES   = (SYNC_ELASTIC, SYNC_LOCK)
SYNC_MODE_LABELS = {
    SYNC_ELASTIC: "Elastic — preserve pauses (drift shown)",
    SYNC_LOCK:    "Hard lock — pin to source timestamps",
}
DUB_DEFAULT_SYNC_MODE = SYNC_ELASTIC

# ── Fitting limits (hard-lock mode) ──────────────────────────────────────────
# ffmpeg's atempo is transparent on speech to roughly ±15%. Past that it is
# audible as processing — the thing the whole feature exists to avoid — so a
# chunk needing more is reported as needing a shorter translation instead of
# being quietly mangled.
DUB_MAX_STRETCH = 1.15      # fastest playback allowed
DUB_MIN_STRETCH = 0.85      # slowest
# Within this much of the slot, leave the chunk alone entirely.
DUB_FIT_TOLERANCE = 0.03
# A pause may be eaten into to buy room, but never below this — shorter and it
# stops reading as a pause, which is the rhythm the dub is trying to keep.
DUB_PAUSE_FLOOR_MS = 150

# ── Chunk trimming ───────────────────────────────────────────────────────────
# Deliberately not DIALOGUE_TRIM_KEEP_MS. That path leaves 50 ms of silence at
# each edge so a soft onset is never clipped, and it can afford to: the gap
# between two dialogue turns is a configured number, so a little extra silence
# inside the clip just makes the gap slightly longer than asked.
#
# Here the gap is *measured from the source recording*, and reproducing it is
# the entire feature. A 50 ms margin at each edge adds 100 ms to every pause —
# on a sixty-chunk piece that is six seconds of drift injected by the trimmer
# alone, in the mode whose whole promise is that pauses come back exactly.
#
# So the margin goes to zero and the threshold drops instead: at -55 dBFS only
# true silence is removed, and the edge fade that follows takes the waveform to
# zero, which is what actually prevents the click.
DUB_TRIM_KEEP_MS      = 0
DUB_TRIM_THRESHOLD_DB = -55.0

# ── Keeping the ending human ─────────────────────────────────────────────────
# The margin above is zero because kept silence used to land inside the next
# pause. It no longer does: the clip is measured (assembly.timed_clip) so the
# timeline is driven by where speech *stops*, not by where the file ends, and
# the retained release simply rings on into a gap that was silent anyway.
#
# That decoupling is what makes a real margin affordable, and it is needed. A
# spoken phrase does not stop, it releases — the last vowel or consonant decays
# over something like 100-150 ms. Cut that off and every chunk ends the way a
# tape splice does, which is audible as a click's slower cousin: not a tick, but
# a phrase that is plainly chopped.
#
# The tail is much longer than the head because the two problems are not equal.
# A clipped onset costs a soft consonant; a clipped release costs the ending of
# every single sentence in the dub.
DUB_KEEP_HEAD_MS = 40
DUB_KEEP_TAIL_MS = 160

# Trim threshold for finding the speech itself. Higher than DUB_TRIM_THRESHOLD_DB
# because it now marks where speech *stops* rather than where the file may be
# cut — the decay below this point is kept as the release, not discarded.
DUB_SPEECH_THRESHOLD_DB = -45.0

# The final ramp. Long enough to be a release rather than a gate, short enough
# not to swallow a real syllable. Only ever lands on the decay, since the tail
# margin above keeps the loud part well clear of it.
DUB_RELEASE_FADE_MS = 45

# ── Where the voice change happens ───────────────────────────────────────────
# Per chunk, or once over the finished mix.
#
# Per chunk is the safe default and the only option when a dub has more than one
# target voice: speech-to-speech is a whole-file operation with a single target,
# so a two-voice conversation pushed through one pass comes back with everyone
# in the same voice.
#
# But when there IS only one target voice, converting per chunk is strictly
# worse. Speech-to-speech carries the source performance across, and on a
# four-second fragment there is barely any performance to carry — which is why
# short clips need the padding guard at all. Handed minutes at a time it has the
# whole delivery to work from, which is exactly what the single-speaker tab gets
# by converting its file in one go.
DUB_STS_PER_CHUNK  = "per_chunk"
DUB_STS_WHOLE      = "whole"
DUB_STS_MODES      = (DUB_STS_PER_CHUNK, DUB_STS_WHOLE)
DUB_STS_MODE_LABELS = {
    DUB_STS_PER_CHUNK: "Per chunk — safe, works with any cast",
    DUB_STS_WHOLE:     "Once on the finished mix — better flow, one voice only",
}
DUB_DEFAULT_STS_MODE = DUB_STS_PER_CHUNK

# The voice changer refuses an upload past a certain size, so a long dub cannot
# always go up in one piece. It is split into the largest blocks that fit, and
# the splits are made *between* chunks — always inside a pause — so a seam falls
# in silence rather than mid-word. Each converted block is then laid back at its
# own original timestamp, so a block whose length the converter did not preserve
# exactly cannot push everything after it out of sync.
#
# A dub short enough to fit becomes a single block, which is literally the
# single-speaker tab's behaviour.
DUB_STS_MAX_UPLOAD_MB = 40
DUB_STS_MIN_BLOCK_MS  = 20_000    # below this, blocking has stopped helping

# ── Duration estimate ────────────────────────────────────────────────────────
# Speaking rate in syllable-ish units per second (see dub_estimate.speech_units).
# These are starting values, not measurements. The estimator calibrates the
# source language against the recording it was just given — which costs nothing
# and is exact for that speaker — and updates the target language from every
# real render, so the numbers below matter most on the very first preview.
DUB_RATES_FILE = os.path.join(APP_DIR, "dub_rates.json")
DUB_DEFAULT_RATES = {
    "English":   4.4,
    "Hindi":     5.0,
    "Bengali":   5.0,
    "Tamil":     5.2,
    "Telugu":    5.2,
    "Kannada":   5.1,
    "Malayalam": 5.4,
    "Marathi":   5.0,
    "Gujarati":  5.0,
    "Odia":      4.9,
    "Assamese":  4.9,
    "Nepali":    4.9,
}
DUB_FALLBACK_RATE = 4.8

# The estimate is a model, and it is being used to decide whether to spend
# money, so it is shown as a band rather than a single number. The band starts
# wide and narrows as real renders accumulate.
DUB_BAND_UNCALIBRATED = 0.25
DUB_BAND_CALIBRATED   = 0.08
DUB_CALIBRATION_FULL  = 20      # samples at which the band reaches its floor

# ─── LLM providers (used only by the optional emotion-tag pass) ──────────────
LLM_PROVIDER_VERTEX = "Vertex AI (JSON file)"
LLM_PROVIDER_GEMINI = "Gemini API key"
LLM_PROVIDER_OPENAI = "OpenAI-compatible (Base URL)"
LLM_PROVIDERS       = [LLM_PROVIDER_VERTEX, LLM_PROVIDER_GEMINI, LLM_PROVIDER_OPENAI]

GEMINI_DEFAULT_MODEL = "gemini-2.5-pro"

# ─── Colour palette (deep slate panels, moonlight text, vibrant accents) ─────
BG           = "#0b1220"
PANEL        = "#111827"
PANEL2       = "#1e293b"
PANEL_BORDER = "#334155"
ACCENT       = "#34d399"
TEXT         = "#e2e8f0"
TEXT_MUTED   = "#94a3b8"
TEXT_FAINT   = "#64748b"
# Input fields use a LIGHT bg + DARK text. macOS Tk 9 (Aqua) ignores a dark bg
# on Entry/Combobox and draws a native white field, which would make light text
# invisible; light fields render correctly on both macOS and Windows.
INPUT_BG     = "#e2e8f0"
INPUT_FG     = "#0f172a"
BTN_BG       = "#1e293b"
BTN_FG       = "#e2e8f0"
BTN_ACT      = "#334155"
TR_ACCENT    = "#22c55e"
ERR_RED      = "#f87171"
WARN_AMBER   = "#d97706"
STEP1_GOLD   = "#facc15"
STEP2_VIOLET = "#a78bfa"
REG_LABEL    = "#93c5fd"
SVO_PANEL    = "#1e1b3a"   # indigo-tinted panel used by the step rows

# ─── Mode rail ───────────────────────────────────────────────────────────────
# The rail sits a shade darker than BG so it reads as chrome rather than as
# another panel floating on the content. Selected and hover are two small steps
# up from it — enough to see, not enough to compete with the tab body.
RAIL_W       = 196
RAIL_BG      = "#0d1424"
RAIL_HOVER   = "#111a2e"
RAIL_SEL     = "#141c33"
RAIL_ACCENT  = "#8b7cf0"   # selected stripe + the selected mode's blurb

AUDIO_EXTENSIONS = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".webm")


def btn_fg(color):
    """macOS Aqua draws native LIGHT buttons and ignores tk.Button bg=.
    Pale foreground colours become invisible there — map them to a dark
    equivalent. On Windows/Linux (bg honoured, dark buttons) colours pass
    through unchanged."""
    if not IS_MAC:
        return color
    try:
        c = str(color)
        if c.lower() in ("white", "snow", "ivory", "ghostwhite"):
            return "#0f172a"
        if c.startswith("#") and len(c) == 7:
            r, g, b = int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)
            if (0.299 * r + 0.587 * g + 0.114 * b) / 255.0 > 0.55:
                return "#0f172a"
    except Exception:
        pass
    return color
