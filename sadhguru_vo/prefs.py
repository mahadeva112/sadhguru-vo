"""
Persisted user state: the ElevenLabs API key, the two voice / model choices,
and the favourite-voice list.

Every write is best-effort — a read-only folder or a locked file must never
crash the app, it just means the choice isn't remembered next launch.
"""

import json
import os
import re
from typing import Dict, Optional

from .config import (API_KEY_FILE, FAV_VOICES_FILE, PREFS_FILE,
                     ELEVENLABS_STS_MODEL, ELEVENLABS_STS_MODELS,
                     ELEVENLABS_TTS_MODEL, ELEVENLABS_TTS_MODELS,
                     DEFAULT_TIMING, DUB_DEFAULT_STS_MODE, DUB_STS_MODES,
                     STEP1_VOICE_ID, STEP2_VOICE_ID, TIMING_MODES)

# ElevenLabs voice IDs are 20-char alphanumeric tokens.
VOICE_ID_RE = re.compile(r"^[A-Za-z0-9]{12,40}$")

# In-memory key set by the UI/CLI when one is supplied. Falls back to api.txt.
_API_KEY_RUNTIME: Optional[str] = None


def sanitize_voice_id(raw) -> str:
    """
    Return *raw* unchanged if it is a clean ElevenLabs voice_id, else "".

    The dropdown shows formatted labels like "✦ Aria — abc12345…  [hi · cloned]".
    Those must NOT be mangled into fake IDs by stripping punctuation: the
    truncated 8-char fragment in the label cannot reconstruct the real voice_id,
    and ElevenLabs answers HTTP 404 voice_not_found. Strict policy — the input
    must already match the voice_id shape, otherwise callers have to look it up
    in the options map by display label.
    """
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    return s if VOICE_ID_RE.match(s) else ""


def api_key_fingerprint(api_key: str) -> str:
    """Stable, non-sensitive cache key derived from the API key."""
    if not api_key:
        return ""
    return api_key[-8:] if len(api_key) >= 8 else "x" * len(api_key)


def redact_api_key(api_key: str) -> str:
    """Safe representation for logs / status messages."""
    if not api_key:
        return "<empty>"
    if len(api_key) <= 8:
        return "*" * len(api_key)
    return f"{api_key[:3]}…{api_key[-4:]}"


# ─── API key ─────────────────────────────────────────────────────────────────

def read_api_key_file() -> str:
    if not os.path.exists(API_KEY_FILE):
        return ""
    try:
        with open(API_KEY_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def write_api_key_file(api_key: str) -> None:
    """Persist a validated API key so the next launch picks it up."""
    if not api_key:
        return
    try:
        with open(API_KEY_FILE, "w", encoding="utf-8") as f:
            f.write(api_key.strip())
    except Exception:
        pass


def set_runtime_api_key(api_key: Optional[str]) -> None:
    """Update the in-memory key (called on paste, or from the CLI's --api-key)."""
    global _API_KEY_RUNTIME
    _API_KEY_RUNTIME = (api_key or "").strip() or None


def get_api_key() -> str:
    """
    Resolve the ElevenLabs API key.

    Order of precedence:
      1. In-memory key (latest paste / --api-key).
      2. api.txt cached next to the app from a previous successful validation.
    Raises ValueError with a friendly message when neither is available.
    """
    if _API_KEY_RUNTIME:
        return _API_KEY_RUNTIME
    key = read_api_key_file()
    if not key:
        raise ValueError(
            "No ElevenLabs API key configured.\n"
            "Paste your key into the API Key box at the top of the window "
            "(or pass --api-key on the command line).")
    return key


# ─── Voice / model choices ───────────────────────────────────────────────────

def read_prefs() -> Dict[str, str]:
    """Load the two voice + two model choices, falling back to the pinned
    defaults for anything missing or malformed."""
    defaults = {
        "step1_voice": STEP1_VOICE_ID,
        "step2_voice": STEP2_VOICE_ID,
        "step1_model": ELEVENLABS_TTS_MODEL,
        "step2_model": ELEVENLABS_STS_MODEL,
        # Which timing source the Studio tab last used. A session is usually all
        # one kind of work, so reopening in the other mode is a small tax paid
        # every single time.
        "studio_timing": DEFAULT_TIMING,
        # Where the Studio tab's voice change happens: per chunk, or once over
        # the finished mix.
        "studio_sts": DUB_DEFAULT_STS_MODE,
    }
    try:
        if not os.path.exists(PREFS_FILE):
            return defaults
        with open(PREFS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return defaults
        out = dict(defaults)
        for key in ("step1_voice", "step2_voice"):
            vid = sanitize_voice_id(data.get(key))
            if vid:
                out[key] = vid
        if data.get("step1_model") in ELEVENLABS_TTS_MODELS:
            out["step1_model"] = data["step1_model"]
        if data.get("step2_model") in ELEVENLABS_STS_MODELS:
            out["step2_model"] = data["step2_model"]
        if data.get("studio_timing") in TIMING_MODES:
            out["studio_timing"] = data["studio_timing"]
        if data.get("studio_sts") in DUB_STS_MODES:
            out["studio_sts"] = data["studio_sts"]
        return out
    except Exception:
        return defaults


def write_prefs(prefs: Dict[str, str]) -> None:
    """Persist the voice / model choices. Best-effort, never crashes."""
    try:
        clean = {
            "step1_voice": sanitize_voice_id(prefs.get("step1_voice")) or STEP1_VOICE_ID,
            "step2_voice": sanitize_voice_id(prefs.get("step2_voice")) or STEP2_VOICE_ID,
            "step1_model": (prefs.get("step1_model")
                            if prefs.get("step1_model") in ELEVENLABS_TTS_MODELS
                            else ELEVENLABS_TTS_MODEL),
            "step2_model": (prefs.get("step2_model")
                            if prefs.get("step2_model") in ELEVENLABS_STS_MODELS
                            else ELEVENLABS_STS_MODEL),
            "studio_timing": (prefs.get("studio_timing")
                              if prefs.get("studio_timing") in TIMING_MODES
                              else DEFAULT_TIMING),
            "studio_sts": (prefs.get("studio_sts")
                           if prefs.get("studio_sts") in DUB_STS_MODES
                           else DUB_DEFAULT_STS_MODE),
        }
        with open(PREFS_FILE, "w", encoding="utf-8") as f:
            json.dump(clean, f, indent=2)
    except Exception:
        pass


# ─── Favourite voices (pinned to the top of both dropdowns) ──────────────────

def read_fav_voice_meta() -> Dict[str, Dict[str, str]]:
    """Load favourites as a {voice_id: {"name", "label", "collection"}} map."""
    if not os.path.exists(FAV_VOICES_FILE):
        return {}
    try:
        with open(FAV_VOICES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        out: Dict[str, Dict[str, str]] = {}
        for vid, meta in data.items():
            if not VOICE_ID_RE.match(str(vid)):
                continue
            meta = meta if isinstance(meta, dict) else {}
            out[str(vid)] = {
                "name":       str(meta.get("name") or ""),
                "label":      str(meta.get("label") or ""),
                "collection": str(meta.get("collection") or ""),
            }
        return out
    except Exception:
        return {}


def write_fav_voice_meta(meta: Dict[str, Dict[str, str]]) -> None:
    """Persist favourites to fav_voices.json. Best-effort."""
    try:
        clean = {vid: {"name":       str(m.get("name") or ""),
                       "label":      str(m.get("label") or ""),
                       "collection": str(m.get("collection") or "")}
                 for vid, m in (meta or {}).items()
                 if VOICE_ID_RE.match(str(vid))}
        with open(FAV_VOICES_FILE, "w", encoding="utf-8") as f:
            json.dump(clean, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def read_fav_voices() -> set:
    """Set of favourite voice_ids, used for dropdown pinning + ★ decoration."""
    return set(read_fav_voice_meta().keys())
