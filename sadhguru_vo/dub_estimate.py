"""
Guessing how long a translation will take to say — before saying it.

This is the module the whole preview rests on: if it is wrong, the user either
spends credits on a dub that drifts or rewrites a translation that would have
been fine. So it is built around three admissions.

**It counts syllables, not characters.** Characters are a terrible unit across
scripts — "श्री" is three characters and one syllable, and comparing a
Devanagari chunk to a Latin one by length is comparing nothing at all. Syllables
are roughly isochronous within a language, which is what makes a per-language
rate meaningful in the first place.

**It calibrates against the recording it was given.** The source audio and the
source script together are a free, exact measurement of how fast this particular
speaker talks — no API call, no guessing. A speaker reading 30% slower than
average makes every default rate wrong by 30%, and that is by far the largest
error in the whole estimate. Measuring it removes it.

**It reports a band, not a number.** A single figure implies a precision this
cannot have on a first run. The band is wide until real renders have been
measured and narrows as they accumulate.

Nothing here calls ElevenLabs or touches the network. Running a preview costs
nothing, which is the point of having one.
"""

import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .config import (DUB_BAND_CALIBRATED, DUB_BAND_UNCALIBRATED,
                     DUB_CALIBRATION_FULL, DUB_DEFAULT_RATES,
                     DUB_FALLBACK_RATE, DUB_FIT_TOLERANCE, DUB_MAX_STRETCH,
                     DUB_MIN_STRETCH, DUB_PAUSE_FLOOR_MS, DUB_RATES_FILE,
                     SYNC_ELASTIC)
from .dub_align import Chunk

# Verdicts, worst last so the UI can sort by severity.
FIT     = "FITS"       # lands inside the slot, nothing to do
SHORT   = "SHORT"      # finishes early, padded with silence
TIGHT   = "TIGHT"      # overruns, but inside the transparent stretch range
OVER    = "OVER"       # overruns beyond what stretching can hide (elastic: informational)
REWRITE = "REWRITE"    # needs a shorter translation, not a faster one
EMPTY   = "EMPTY"      # no target text yet

VERDICT_ORDER = (FIT, SHORT, TIGHT, OVER, REWRITE, EMPTY)

# How far the measured source tempo may deviate from its language's baseline
# before it is treated as a mismatched script rather than an unusual speaker.
TEMPO_CLAMP_LOW  = 0.5
TEMPO_CLAMP_HIGH = 2.0

# Marks that ride on a base character rather than forming a syllable of their
# own: matras, nukta, anusvara, virama and friends.
_COMBINING = ("Mn", "Mc")

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_VOWEL_GROUP = re.compile(r"[aeiouy]+", re.IGNORECASE)

# The virama / halant / pulli of each Brahmic script — the one combining mark
# that removes a beat instead of riding on one, by fusing two consonants into a
# conjunct. Listed explicitly rather than matched by codepoint arithmetic: the
# low byte that all of these share is also carried by unrelated combining marks
# in other blocks, and subtracting a syllable for one of those would be a silent
# error in the estimate.
_VIRAMA = frozenset("्্੍્୍்్್്්")


# ═════════════════════════════════════════════════════════════════════════════
#  Counting
# ═════════════════════════════════════════════════════════════════════════════

def _is_indic(text: str) -> bool:
    """True when the text is mostly in a Brahmic script.

    Decided from the text rather than the language name, because the language
    dropdown says "Hindi" for a line that has been pasted in transliteration and
    the counting rule has to follow the characters actually present.
    """
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    indic = sum(1 for c in letters if 0x0900 <= ord(c) <= 0x0DFF)
    return indic * 2 > len(letters)


def _indic_units(text: str) -> float:
    """
    Syllable count for a Brahmic script.

    A syllable is a base consonant or independent vowel. Matras and other
    combining marks attach to one and do not add a beat, and a virama fuses two
    consonants into a single conjunct — so both are subtracted rather than
    counted. That is not phonetically exact, but it tracks spoken length far
    more closely than a character count, which is all the estimate needs.
    """
    base = 0
    viramas = 0
    for ch in text:
        if ch.isspace():
            continue
        # Combining marks are tested before punctuation, not after. `\w` is
        # defined by isalnum(), which is false for category Mn, so a matra or a
        # virama matches the [^\w\s] punctuation class — skip on that test first
        # and the virama is discarded before it can remove its beat, which makes
        # every conjunct count as two syllables instead of one.
        if ch in _VIRAMA:
            viramas += 1
            continue
        if unicodedata.category(ch) in _COMBINING:
            continue                     # matra, anusvara, nukta — rides a base
        if _PUNCT.match(ch):
            continue
        base += 1
    return float(max(0, base - viramas))


def _latin_units(text: str) -> float:
    """
    Syllable count for a Latin script, by vowel groups.

    The classic heuristic: each run of vowels is one syllable, minus a silent
    final "e", with every word worth at least one. Good to about 10% on running
    prose, which is well inside the band the estimate is reported with.
    """
    total = 0
    for word in re.findall(r"[A-Za-z']+", text):
        groups = len(_VOWEL_GROUP.findall(word))
        if len(word) > 2 and word.lower().endswith("e") and groups > 1:
            groups -= 1
        total += max(1, groups)
    # Digits and anything non-Latin that slipped through still take time to say.
    leftover = len(re.findall(r"\d", text))
    return float(total + leftover)


def speech_units(text: str) -> float:
    """Syllable-ish count for *text*, in whichever script it is written."""
    text = (text or "").strip()
    if not text:
        return 0.0
    if _is_indic(text):
        return _indic_units(text)
    if re.search(r"[A-Za-z]", text):
        return _latin_units(text)
    # Some other script — fall back to non-space, non-punctuation characters.
    return float(sum(1 for c in text if not c.isspace() and not _PUNCT.match(c)))


# ═════════════════════════════════════════════════════════════════════════════
#  Speaking-rate store
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class Rate:
    """A language's speaking rate and how much it is worth trusting."""
    language: str
    units_per_sec: float
    samples: int = 0
    source: str = "default"      # "default" | "calibrated" | "measured"
    clamped: bool = False        # measured tempo was implausible and got capped

    @property
    def ms_per_unit(self) -> float:
        return 1000.0 / max(0.5, self.units_per_sec)

    @property
    def band(self) -> float:
        """Half-width of the confidence band, as a fraction of the estimate."""
        if self.samples <= 0:
            return DUB_BAND_UNCALIBRATED
        t = min(1.0, self.samples / float(DUB_CALIBRATION_FULL))
        return DUB_BAND_UNCALIBRATED + t * (DUB_BAND_CALIBRATED - DUB_BAND_UNCALIBRATED)


def load_rates(path: str = DUB_RATES_FILE) -> Dict[str, Rate]:
    """Measured rates from disk, merged over the defaults.

    A corrupt or hand-edited file degrades to the defaults rather than stopping
    a preview — the numbers in it are an optimisation, not data the user would
    be upset to lose.
    """
    rates = {lang: Rate(lang, ups, 0, "default")
             for lang, ups in DUB_DEFAULT_RATES.items()}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            stored = json.load(fh)
        for lang, entry in (stored or {}).items():
            ups = float(entry.get("units_per_sec", 0) or 0)
            if ups <= 0:
                continue
            rates[lang] = Rate(lang, ups, int(entry.get("samples", 0) or 0), "measured")
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return rates


def save_rate(language: str, units_per_sec: float, samples: int,
              path: str = DUB_RATES_FILE) -> bool:
    """Persist one language's rate. Returns False if it could not be written."""
    try:
        stored = {}
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as fh:
                stored = json.load(fh) or {}
        stored[language] = {"units_per_sec": round(float(units_per_sec), 4),
                            "samples": int(samples)}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(stored, fh, ensure_ascii=False, indent=2, sort_keys=True)
        return True
    except (OSError, ValueError, TypeError):
        return False


def rate_for(language: str, rates: Optional[Dict[str, Rate]] = None) -> Rate:
    """The rate to use for *language*, defaulting sensibly for unknown ones."""
    rates = rates if rates is not None else load_rates()
    if language in rates:
        return rates[language]
    return Rate(language or "unknown", DUB_FALLBACK_RATE, 0, "default")


def measure_source_rates(chunks: Sequence[Chunk],
                         language: str) -> Dict[str, Rate]:
    """
    One measured rate per speaker, keyed by Chunk.key.

    Worth doing separately rather than averaging everyone together: in the
    material this app exists for, one speaker is deliberate and unhurried and
    the other is an interviewer asking brisk questions. A single blended rate
    is wrong for both — it makes the slow speaker's chunks look like they will
    overrun and the fast one's look like they will fall short, which is exactly
    backwards.

    A speaker with too little material to measure is left out; the caller falls
    back to the overall rate for them.
    """
    by_speaker: Dict[str, List[Chunk]] = {}
    for c in chunks:
        by_speaker.setdefault(c.key, []).append(c)

    out: Dict[str, Rate] = {}
    for key, items in by_speaker.items():
        rate = measure_source_rate(items, language)
        if rate is not None:
            out[key] = rate
    return out


def measure_source_rate(chunks: Sequence[Chunk], language: str) -> Optional[Rate]:
    """
    How fast the source speaker actually talks, from the recording itself.

    Free and exact: the source segments have measured durations and the script
    chunks have known syllable counts, so dividing one by the other gives this
    speaker's real rate with no API call and no guessing. Speaker tempo is the
    single largest error in a default-rate estimate, and this removes it.

    Chunks with no text are skipped rather than counted as zero syllables, which
    would drag the rate down toward nothing.
    """
    units = 0.0
    ms = 0
    used = 0
    for c in chunks:
        u = speech_units(c.source_text)
        if u <= 0 or c.duration_ms <= 0:
            continue
        units += u
        ms += c.duration_ms
        used += 1
    if used < 2 or ms <= 0 or units <= 0:
        return None
    return Rate(language, units / (ms / 1000.0), used, "calibrated")


def transfer_rate(source_measured: Optional[Rate],
                  source_language: str,
                  target_language: str,
                  rates: Optional[Dict[str, Rate]] = None) -> Rate:
    """
    Carry the source speaker's tempo across to the target language.

    A speaker reading 30% slower than average stays 30% slower when dubbed, and
    the target voice is being asked to match that delivery. So the target's
    intrinsic language rate is scaled by however far this speaker sits from the
    source language's own default — tempo transfers, the language's natural
    syllable rate does not.

    With no measurement to transfer, the target's stored or default rate is
    returned unchanged.
    """
    rates = rates if rates is not None else load_rates()
    target = rate_for(target_language, rates)
    if source_measured is None or source_measured.units_per_sec <= 0:
        return target

    baseline = rate_for(source_language, rates).units_per_sec
    if baseline <= 0:
        return target
    tempo = source_measured.units_per_sec / baseline
    # Clamped, but wide. The clamp is here to catch a script that does not match
    # its audio — that produces ratios of 5× or 0.1×, not 0.55×. Real articulation
    # rates run from roughly 2.5 to 7 syllables/sec, so a window any tighter
    # clips genuinely slow, deliberate speakers, which is precisely the delivery
    # this app exists to reproduce.
    clamped = not (TEMPO_CLAMP_LOW <= tempo <= TEMPO_CLAMP_HIGH)
    tempo = max(TEMPO_CLAMP_LOW, min(TEMPO_CLAMP_HIGH, tempo))
    return Rate(target.language, target.units_per_sec * tempo,
                target.samples, "calibrated", clamped)


# ═════════════════════════════════════════════════════════════════════════════
#  Per-chunk estimate
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class ChunkEstimate:
    """What one chunk is predicted to do, and what would have to be done to it."""
    index: int
    units: float = 0.0
    estimated_ms: int = 0
    low_ms: int = 0
    high_ms: int = 0
    source_ms: int = 0          # what the original speaker took
    usable_ms: int = 0          # room before the next chunk is due
    speed: float = 1.0          # playback factor needed (hard lock only)
    pad_ms: int = 0             # silence appended when it finishes early
    absorb_ms: int = 0          # pause eaten to buy room (hard lock only)
    dub_start_ms: int = 0       # where it actually starts in the dub
    dub_end_ms: int = 0
    drift_ms: int = 0           # dub start minus source start
    verdict: str = FIT

    @property
    def over_ms(self) -> int:
        """Milliseconds past the slot, before any fitting."""
        return max(0, self.estimated_ms - self.usable_ms)


@dataclass
class Preview:
    """A whole timeline's estimate. Costs nothing to produce."""
    mode: str = SYNC_ELASTIC
    estimates: List[ChunkEstimate] = field(default_factory=list)
    target_rate: Optional[Rate] = None
    source_rate: Optional[Rate] = None
    source_total_ms: int = 0
    dub_total_ms: int = 0
    final_drift_ms: int = 0
    max_drift_ms: int = 0
    counts: Dict[str, int] = field(default_factory=dict)
    speaker_rates: Dict[str, Rate] = field(default_factory=dict)
    # True when the chunks came from a script rather than a recording. Drift and
    # fit are undefined here; total length is the number that matters.
    planned: bool = False

    @property
    def needs_attention(self) -> int:
        return self.counts.get(REWRITE, 0) + self.counts.get(EMPTY, 0)

    def notes(self) -> List[str]:
        """Plain-language summary, worst news first."""
        out: List[str] = []
        if self.counts.get(EMPTY):
            out.append(f"⚠ {self.counts[EMPTY]} chunk(s) have no target text — "
                       f"those will render as silence.")
        if self.target_rate is not None and self.target_rate.clamped:
            out.append("⚠ The source script and the source audio imply an "
                       "implausible speaking rate — they are probably not the "
                       "same material, or the chunk boundaries are wrong. The "
                       "estimate below used a capped rate and should not be "
                       "trusted until that is fixed.")
        if self.counts.get(REWRITE):
            out.append(f"⚠ {self.counts[REWRITE]} chunk(s) are too long to fit even "
                       f"at {DUB_MAX_STRETCH:.2f}× — shorten the translation there. "
                       f"Stretching further would be audible.")
        if self.planned:
            # No recording, so no drift and no fit — the number worth having is
            # the total length, which otherwise costs a render to find out.
            out.append(f"Script only: positions come from the gap settings, so "
                       f"there is nothing to drift against. Estimated length "
                       f"{_mmss(self.dub_total_ms)}.")
        elif self.mode == SYNC_ELASTIC:
            out.append(f"Elastic: pauses reproduced exactly, no time-stretching. "
                       f"Dub ends {_signed(self.final_drift_ms)} against the source "
                       f"({_mmss(abs(self.final_drift_ms))}), peak drift "
                       f"{_mmss(self.max_drift_ms)}.")
        else:
            stretched = self.counts.get(TIGHT, 0)
            out.append(f"Hard lock: every chunk starts on its source timestamp, "
                       f"zero cumulative drift. {stretched} chunk(s) time-stretched, "
                       f"{self.counts.get(SHORT, 0)} padded with silence.")
        if self.speaker_rates:
            per = " · ".join(f"{k}: {r.units_per_sec:.2f}/s"
                             for k, r in sorted(self.speaker_rates.items()))
            out.append(f"Rate measured per speaker — {per}")
        elif self.target_rate:
            r = self.target_rate
            out.append(f"Rate: {r.units_per_sec:.2f} syllables/sec ({r.source}, "
                       f"±{r.band * 100:.0f}% band)"
                       + (f" · source measured at {self.source_rate.units_per_sec:.2f}"
                          if self.source_rate else ""))
        return out


def _signed(ms: int) -> str:
    return f"{'+' if ms >= 0 else '−'}{abs(ms) / 1000.0:.1f}s"


def _mmss(ms: int) -> str:
    ms = max(0, int(ms))
    minutes, rem = divmod(ms, 60_000)
    return f"{minutes}:{rem / 1000:04.1f}"


def estimate_chunk(chunk: Chunk, rate: Rate, mode: str,
                   dub_cursor_ms: int) -> ChunkEstimate:
    """
    Estimate one chunk against its slot.

    *dub_cursor_ms* is where the dub has actually reached — meaningful in
    elastic mode, where it walks away from the source timeline, and equal to the
    chunk's own start in hard lock.
    """
    units = speech_units(chunk.target_text)
    est = int(round(units * rate.ms_per_unit))
    band = rate.band

    e = ChunkEstimate(index=chunk.index,
                      units=units,
                      estimated_ms=est,
                      low_ms=int(round(est * (1.0 - band))),
                      high_ms=int(round(est * (1.0 + band))),
                      source_ms=chunk.duration_ms)

    if not chunk.target_text.strip():
        e.verdict = EMPTY
        e.usable_ms = chunk.duration_ms
        e.dub_start_ms = dub_cursor_ms
        e.dub_end_ms = dub_cursor_ms
        e.drift_ms = dub_cursor_ms - chunk.start_ms if chunk.pinned else 0
        return e

    if not chunk.pinned:
        # Planned from a script: there is no slot and no source duration, so
        # there is nothing to fit against and no drift to measure — a chunk
        # cannot be late for a timestamp nothing established. Every fit verdict
        # here would be an opinion about a number this path invented, so the
        # estimate reports the predicted length and stops there.
        e.usable_ms = est
        e.speed = 1.0
        e.dub_start_ms = dub_cursor_ms
        e.dub_end_ms = dub_cursor_ms + est
        e.drift_ms = 0
        e.verdict = FIT
        return e

    if mode == SYNC_ELASTIC:
        # Nothing is fitted: the chunk plays at its natural length and the pause
        # after it is reproduced exactly. The slot is reported for information —
        # it is what the source used — but it does not constrain anything.
        e.usable_ms = chunk.duration_ms
        e.speed = 1.0
        e.dub_start_ms = dub_cursor_ms
        e.dub_end_ms = dub_cursor_ms + est
        e.drift_ms = dub_cursor_ms - chunk.start_ms
        delta = est - chunk.duration_ms
        if abs(delta) <= max(120, chunk.duration_ms * DUB_FIT_TOLERANCE):
            e.verdict = FIT
        elif delta < 0:
            e.verdict = SHORT
        elif delta <= chunk.duration_ms * (DUB_MAX_STRETCH - 1.0):
            e.verdict = TIGHT
        else:
            e.verdict = OVER
        return e

    # ── Hard lock ────────────────────────────────────────────────────────────
    # Room = the speech slot plus whatever of the following pause can be eaten
    # without the pause ceasing to be one.
    absorbable = max(0, chunk.pause_after_ms - DUB_PAUSE_FLOOR_MS)
    usable = chunk.duration_ms + absorbable
    e.usable_ms = usable
    e.dub_start_ms = chunk.start_ms      # pinned, by definition of the mode
    e.drift_ms = 0

    if est <= chunk.duration_ms * (1.0 + DUB_FIT_TOLERANCE):
        # Fits the speech slot on its own. Pad if it finishes early so the next
        # chunk still starts where it should.
        e.speed = 1.0
        e.pad_ms = max(0, chunk.duration_ms - est)
        e.dub_end_ms = chunk.start_ms + est
        e.verdict = SHORT if est < chunk.duration_ms * (1.0 - DUB_FIT_TOLERANCE) else FIT
        if e.verdict == SHORT and est < chunk.duration_ms * DUB_MIN_STRETCH:
            # Far short — worth flagging, but padding is a real fix, not a bodge.
            e.verdict = SHORT
        return e

    if est <= usable:
        # Overruns the speech but fits by eating into the pause. No stretching.
        e.speed = 1.0
        e.absorb_ms = est - chunk.duration_ms
        e.dub_end_ms = chunk.start_ms + est
        e.verdict = TIGHT
        return e

    # Needs stretching on top of the whole absorbable pause.
    needed = est / float(max(1, usable))
    if needed <= DUB_MAX_STRETCH:
        e.speed = needed
        e.absorb_ms = absorbable
        e.dub_end_ms = chunk.start_ms + usable
        e.verdict = TIGHT
    else:
        e.speed = DUB_MAX_STRETCH
        e.absorb_ms = absorbable
        e.dub_end_ms = chunk.start_ms + int(round(est / DUB_MAX_STRETCH))
        e.verdict = REWRITE
    return e


def preview(chunks: Sequence[Chunk],
            target_language: str,
            source_language: str = "English",
            mode: str = SYNC_ELASTIC,
            lead_in_ms: int = 0,
            rates: Optional[Dict[str, Rate]] = None) -> Preview:
    """
    Estimate the whole timeline. No network, no credits, no audio.

    The source rate is measured from the chunks first and transferred to the
    target language, so the estimate reflects this speaker rather than an
    average one.
    """
    rates = rates if rates is not None else load_rates()
    measured = measure_source_rate(chunks, source_language)
    target_rate = transfer_rate(measured, source_language, target_language, rates)

    # Per-speaker tempo, where there is enough of each speaker to measure it.
    per_speaker = {
        key: transfer_rate(rate, source_language, target_language, rates)
        for key, rate in measure_source_rates(chunks, source_language).items()
    } if any(c.speaker for c in chunks) else {}

    out = Preview(mode=mode, target_rate=target_rate, source_rate=measured,
                  speaker_rates=per_speaker)
    if not chunks:
        return out

    # A planned timeline has no source timestamps to lock to, so hard lock has
    # nothing to pin against and the walk is the elastic one either way. Decided
    # from the chunks rather than from the caller's mode: the sync dropdown is
    # meaningless without a recording, and silently honouring it here would put
    # every chunk at start_ms=0.
    planned = not chunks[0].pinned
    walk = SYNC_ELASTIC if planned else mode
    out.planned = planned

    cursor = lead_in_ms
    estimates: List[ChunkEstimate] = []
    for c in chunks:
        e = estimate_chunk(c, per_speaker.get(c.key, target_rate), walk, cursor)
        estimates.append(e)
        if walk == SYNC_ELASTIC:
            # The gap after the chunk is reproduced verbatim — the measured
            # pause when there was a recording, the gap rule when there was
            # not — so the cursor advances by spoken length plus that gap.
            cursor = e.dub_end_ms + c.pause_after_ms
        else:
            cursor = c.start_ms + c.duration_ms + c.pause_after_ms

    last = chunks[-1]
    out.estimates = estimates
    out.dub_total_ms = cursor
    if planned:
        # Nothing to drift against. The useful number here is the total length,
        # which is the thing you would otherwise only learn by paying for it.
        out.source_total_ms = 0
        out.final_drift_ms = 0
        out.max_drift_ms = 0
    else:
        out.source_total_ms = last.end_ms + last.pause_after_ms
        out.final_drift_ms = cursor - out.source_total_ms
        out.max_drift_ms = max((abs(e.drift_ms) for e in estimates), default=0)
    out.counts = {v: sum(1 for e in estimates if e.verdict == v) for v in VERDICT_ORDER}
    out.counts = {k: v for k, v in out.counts.items() if v}
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  Feeding real renders back in
# ═════════════════════════════════════════════════════════════════════════════

def update_rate_from_render(language: str,
                            measurements: Sequence[tuple],
                            path: str = DUB_RATES_FILE) -> Optional[Rate]:
    """
    Fold a completed render's real durations into the stored rate.

    *measurements* is (units, actual_ms) per chunk, taken from audio that has
    actually been generated — the only unarguable data the estimator ever gets.
    Combined with the stored rate weighted by how many samples it already has,
    so one unusual render nudges the number instead of replacing it.

    Returns the new Rate, or None if there was nothing usable to learn from.
    """
    units = sum(u for u, ms in measurements if u > 0 and ms > 0)
    ms = sum(ms for u, ms in measurements if u > 0 and ms > 0)
    n = sum(1 for u, ms in measurements if u > 0 and ms > 0)
    if n < 2 or units <= 0 or ms <= 0:
        return None

    observed = units / (ms / 1000.0)
    current = rate_for(language, load_rates(path))
    prior = current.samples if current.source == "measured" else 0
    total = prior + n
    blended = (current.units_per_sec * prior + observed * n) / float(total)

    new = Rate(language, blended, min(total, DUB_CALIBRATION_FULL * 5), "measured")
    save_rate(language, new.units_per_sec, new.samples, path)
    return new
