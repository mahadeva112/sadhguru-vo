"""
The cast: one voice recipe per speaker.

A dialogue is not "one script, one voice pair" — it is a set of people, each of
whom needs their own answer to "how do I get rendered?". That answer is a
SpeakerRecipe, and the important field on it is `mode`:

    mode="both"   TTS, then speech-to-speech onto a cloned target voice.
                  This is the Sadhguru treatment: Step 1 supplies the
                  performance, Step 2 supplies the timbre.
    mode="tts"    TTS only. Right for any speaker using a stock voice as-is —
                  half the API calls, half the latency, and no cloned voice
                  needed.

Because mode is per speaker, a cast can freely mix the two, which is what makes
a Sadhguru-plus-interviewer conversation cheap rather than twice the price of a
monologue.

Recipes live in a JSON file (cast.json by default). Reading is forgiving —
anything missing or malformed falls back to a default rather than refusing to
load — but validate_cast() before a run is strict, so problems surface before
any API credits are spent rather than halfway through a 60-turn render.
"""

import json
import os
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from .config import (CHUNKED_VOICE_SETTINGS, ELEVENLABS_STS_MODEL,
                     ELEVENLABS_STS_MODELS, ELEVENLABS_TTS_MODEL,
                     ELEVENLABS_TTS_MODELS, MODE_BOTH, MODE_TTS,
                     STEP1_VOICE_ID, STEP2_VOICE_ID)
from .prefs import sanitize_voice_id
from .script_parser import normalize_speaker

# Only these two make sense per speaker. MODE_VC is a whole-file operation on
# audio that already exists, which is the single-speaker tab's job.
CAST_MODES = (MODE_BOTH, MODE_TTS)

CAST_MODE_LABELS = {
    MODE_BOTH: "2-step (TTS → voice change)",
    MODE_TTS:  "1-step (TTS only)",
}

# Name fragments that mean "this speaker is Sadhguru", so a freshly parsed
# script comes back with his pinned two-step recipe already filled in and the
# operator only has to choose voices for everybody else.
_SADHGURU_TOKENS = ("sadhguru", "sadguru", "सद्गुरु", "isha")


def _clamp(value, low: float, high: float, fallback: float) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return fallback


@dataclass
class SpeakerRecipe:
    """How one speaker gets rendered."""
    speaker: str
    mode: str = MODE_TTS
    step1_voice: str = ""
    step2_voice: str = ""
    step1_model: str = ELEVENLABS_TTS_MODEL
    step2_model: str = ELEVENLABS_STS_MODEL
    # Plain-English delivery note. Not sent to ElevenLabs — it goes to the
    # emotion pass so the LLM tags an interviewer's question as curious rather
    # than giving everyone Sadhguru's contemplative pacing.
    style: str = ""
    # Chunked defaults, not the single-speaker tab's: a recipe only ever renders
    # through the Dialogue or Studio path, one call per turn or per chunk.
    stability: float = field(default=CHUNKED_VOICE_SETTINGS["stability"])
    similarity_boost: float = field(default=CHUNKED_VOICE_SETTINGS["similarity_boost"])
    style_exaggeration: float = field(default=CHUNKED_VOICE_SETTINGS["style"])
    speaker_boost: bool = field(default=CHUNKED_VOICE_SETTINGS["use_speaker_boost"])
    # Manual trim applied to this speaker's audio in the mix, on top of the
    # automatic loudness match.
    gain_db: float = 0.0

    @property
    def key(self) -> str:
        return normalize_speaker(self.speaker)

    @property
    def two_step(self) -> bool:
        return self.mode == MODE_BOTH

    def voice_settings(self) -> dict:
        """The voice_settings block for the ElevenLabs TTS call."""
        return {
            "stability": self.stability,
            "similarity_boost": self.similarity_boost,
            "style": self.style_exaggeration,
            "use_speaker_boost": bool(self.speaker_boost),
        }

    def to_dict(self) -> dict:
        return {
            "speaker": self.speaker,
            "mode": self.mode,
            "step1_voice": self.step1_voice,
            "step2_voice": self.step2_voice,
            "step1_model": self.step1_model,
            "step2_model": self.step2_model,
            "style": self.style,
            "stability": self.stability,
            "similarity_boost": self.similarity_boost,
            "style_exaggeration": self.style_exaggeration,
            "speaker_boost": bool(self.speaker_boost),
            "gain_db": self.gain_db,
        }

    @classmethod
    def from_dict(cls, speaker: str, data: dict) -> "SpeakerRecipe":
        """Build a recipe from JSON, repairing anything malformed.

        Forgiving on purpose: a hand-edited cast.json with one bad field should
        still open in the UI so the operator can see and fix it, rather than
        failing to load with a parse error. validate_cast() is where a run is
        actually blocked.
        """
        data = data if isinstance(data, dict) else {}
        mode = str(data.get("mode") or "").strip().lower()
        if mode not in CAST_MODES:
            mode = MODE_BOTH if data.get("step2_voice") else MODE_TTS
        m1 = data.get("step1_model")
        m2 = data.get("step2_model")
        return cls(
            speaker=str(data.get("speaker") or speaker),
            mode=mode,
            step1_voice=sanitize_voice_id(data.get("step1_voice")),
            step2_voice=sanitize_voice_id(data.get("step2_voice")),
            step1_model=m1 if m1 in ELEVENLABS_TTS_MODELS else ELEVENLABS_TTS_MODEL,
            step2_model=m2 if m2 in ELEVENLABS_STS_MODELS else ELEVENLABS_STS_MODEL,
            style=str(data.get("style") or ""),
            stability=_clamp(data.get("stability"), 0.0, 1.0,
                             CHUNKED_VOICE_SETTINGS["stability"]),
            similarity_boost=_clamp(data.get("similarity_boost"), 0.0, 1.0,
                                    CHUNKED_VOICE_SETTINGS["similarity_boost"]),
            style_exaggeration=_clamp(data.get("style_exaggeration"), 0.0, 1.0,
                                      CHUNKED_VOICE_SETTINGS["style"]),
            speaker_boost=bool(data.get("speaker_boost", True)),
            gain_db=_clamp(data.get("gain_db"), -24.0, 24.0, 0.0),
        )


def is_sadhguru(speaker: str) -> bool:
    """Does this label name Sadhguru? Used only to pick a sensible default."""
    key = normalize_speaker(speaker)
    return any(tok in key for tok in _SADHGURU_TOKENS)


def default_recipe(speaker: str) -> SpeakerRecipe:
    """
    A first-guess recipe for a speaker the cast has never seen.

    Sadhguru gets the pinned two-step treatment, unchanged from the
    single-speaker tab. Everyone else starts as one-step TTS with no voice
    chosen — deliberately blank, so validate_cast() forces a real decision
    instead of quietly rendering an interviewer in Sadhguru's voice.
    """
    if is_sadhguru(speaker):
        return SpeakerRecipe(
            speaker=speaker, mode=MODE_BOTH,
            step1_voice=STEP1_VOICE_ID, step2_voice=STEP2_VOICE_ID,
            style="reflective, unhurried, contemplative")
    return SpeakerRecipe(speaker=speaker, mode=MODE_TTS, style="")


# ─── Persistence ─────────────────────────────────────────────────────────────

def read_cast(path: str) -> Dict[str, SpeakerRecipe]:
    """Load a cast file into a {normalized speaker: recipe} map. A missing or
    unreadable file is an empty cast, not an error — the tab fills it in from
    the script."""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    # Tolerate both the flat {"SADHGURU": {...}} form and a wrapped
    # {"speakers": {...}} form, since hand-written files show up as both.
    if isinstance(data.get("speakers"), dict):
        data = data["speakers"]
    out: Dict[str, SpeakerRecipe] = {}
    for name, entry in data.items():
        if not isinstance(name, str) or not name.strip():
            continue
        recipe = SpeakerRecipe.from_dict(name, entry)
        out[recipe.key] = recipe
    return out


def write_cast(path: str, cast: Dict[str, SpeakerRecipe]) -> None:
    """Persist a cast. Best-effort, matching how prefs.py treats a locked or
    read-only folder — a failed save must never take the app down mid-session."""
    try:
        payload = {r.speaker: r.to_dict() for r in cast.values()}
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def cast_for_speakers(speakers: Iterable[str],
                      existing: Optional[Dict[str, SpeakerRecipe]] = None
                      ) -> Dict[str, SpeakerRecipe]:
    """
    Build the cast for one script: keep what the operator already configured,
    add a default for anyone new, and drop nobody.

    Ordered by first appearance in the script, which is the order the cast table
    shows. Speakers held over from a previous script are kept at the end so
    their voice choices survive a script edit that temporarily removes them.
    """
    existing = existing or {}
    out: Dict[str, SpeakerRecipe] = {}
    for name in speakers:
        key = normalize_speaker(name)
        if not key:
            continue
        if key in existing:
            recipe = existing[key]
            recipe.speaker = name        # follow the script's current spelling
            out[key] = recipe
        else:
            out[key] = default_recipe(name)
    for key, recipe in existing.items():
        out.setdefault(key, recipe)
    return out


# ─── Validation ──────────────────────────────────────────────────────────────

def validate_cast(cast: Dict[str, SpeakerRecipe],
                  speakers: Iterable[str]) -> List[str]:
    """
    Check the cast can render this script. Returns a list of human-readable
    problems — empty means good to go.

    Returns rather than raises so the UI can show every problem at once. A
    60-turn render that dies on turn 41 because one speaker had no voice has
    already burnt 40 turns of quota.
    """
    problems: List[str] = []
    for name in speakers:
        key = normalize_speaker(name)
        recipe = cast.get(key)
        if recipe is None:
            problems.append(f"{name}: no voice configured.")
            continue
        if recipe.mode not in CAST_MODES:
            problems.append(
                f"{name}: unknown mode {recipe.mode!r} — expected "
                f"{' or '.join(CAST_MODES)}.")
        # Phrased so the same string reads correctly in the cast table, in a
        # message box, and on a terminal — each surface adds its own hint.
        if not sanitize_voice_id(recipe.step1_voice):
            problems.append(f"{name}: no TTS voice chosen (step1_voice).")
        if recipe.two_step and not sanitize_voice_id(recipe.step2_voice):
            problems.append(
                f"{name}: set to 2-step but has no target voice (step2_voice) — "
                f"choose one, or switch to {CAST_MODE_LABELS[MODE_TTS]}.")
        if recipe.step1_model not in ELEVENLABS_TTS_MODELS:
            problems.append(f"{name}: unknown TTS model {recipe.step1_model!r}.")
        if recipe.two_step and recipe.step2_model not in ELEVENLABS_STS_MODELS:
            problems.append(f"{name}: unknown STS model {recipe.step2_model!r}.")
    return problems


def cast_summary(cast: Dict[str, SpeakerRecipe],
                 speakers: Iterable[str]) -> str:
    """One-line description of the cast, for status bars and CLI output."""
    names = list(speakers)
    two = sum(1 for n in names
              if (cast.get(normalize_speaker(n)) or default_recipe(n)).two_step)
    one = len(names) - two
    bits = []
    if two:
        bits.append(f"{two} × 2-step")
    if one:
        bits.append(f"{one} × 1-step")
    return f"{len(names)} speaker(s): " + (", ".join(bits) or "none configured")
