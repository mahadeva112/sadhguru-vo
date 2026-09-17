"""
Turning a pile of rendered turns into something an editor can use.

Three outputs, not one:

    dialogue.wav            the mixed master
    stems/<SPEAKER>.wav     one full-length track per speaker, silent where
                            they aren't talking, sample-aligned to the master
    dialogue_manifest.csv   every turn with its in/out timecode and text

The stems and the manifest are the point. A single mixed file means any change
to balance or timing comes back to this app; stems plus timecodes mean the edit
happens in Premiere or Resolve where it belongs.

Everything is laid out by overlaying clips onto a silent bed at absolute
positions. That one code path covers both cases: script-mode turns get computed
positions (sequential, with a longer gap after a speaker change), and audio-in
turns get the positions carried over from the source recording — including
overlapping speech, which falls out for free rather than needing a second
implementation.
"""

import csv
import math
import os
from dataclasses import dataclass
from typing import Dict, List, Optional

from .audio_backend import audio_segment
from .config import (DIALOGUE_CHANNELS, DIALOGUE_EDGE_FADE_MS,
                     DIALOGUE_FRAME_RATE, DIALOGUE_GAP_SAME_MS,
                     DIALOGUE_GAP_SWITCH_MS, DIALOGUE_TARGET_DBFS,
                     DIALOGUE_TRIM_KEEP_MS, DIALOGUE_TRIM_THRESHOLD_DB,
                     DIALOGUE_TRIM_TURN_SILENCE)
from .script_parser import Turn, normalize_speaker

# How far a speaker may be pushed by the automatic loudness match. Beyond this
# the problem is the voice or the render, not the gain — silently applying
# +18 dB would just make a bad clip loud.
MAX_MATCH_GAIN_DB = 9.0

# Headroom left on the master. Mixing several normalised speakers can stack
# peaks; without this a busy passage clips.
MASTER_CEILING_DBFS = -1.0

# Silence left at the end of the master so a player doesn't cut the last word.
TAIL_PAD_MS = 400


class AssemblyError(RuntimeError):
    """Assembly could not run — almost always a missing pydub / ffmpeg."""


def _audio_segment():
    """Import pydub on demand with an actionable message if it isn't usable.

    Unlike Step 1, which can fall back to concatenating raw MP3 bytes, there is
    no degraded mode here: placing clips on a timeline requires decoding them.
    """
    try:
        return audio_segment()
    except ImportError as e:
        raise AssemblyError(f"Assembling a dialogue needs pydub and ffmpeg.\n{e}") from None


def timecode(ms: int) -> str:
    """Milliseconds → HH:MM:SS.mmm, the form editing software expects."""
    ms = max(0, int(ms))
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


@dataclass
class RenderedTurn:
    """A turn plus the audio file it rendered to."""
    turn: Turn
    path: str
    mode: str = ""
    note: str = ""          # e.g. "padded for STS", "cached"


@dataclass
class Placement:
    """A rendered turn at an absolute position on the timeline."""
    turn: Turn
    path: str
    start_ms: int
    duration_ms: int
    mode: str = ""
    note: str = ""

    @property
    def end_ms(self) -> int:
        return self.start_ms + self.duration_ms


def load_clip(path: str):
    """Decode a rendered clip, normalised to one common format.

    pydub refuses to overlay segments whose rate / channels / sample width
    differ, and Step-1 output is a WAV inside a .mp3 filename (see the README),
    so everything is coerced here rather than trusted.
    """
    AudioSegment = _audio_segment()
    if not path or not os.path.isfile(path):
        raise AssemblyError(f"Rendered clip is missing: {path}")
    try:
        seg = AudioSegment.from_file(path)
    except Exception as e:
        raise AssemblyError(f"Could not decode {os.path.basename(path)}: {e}") from None
    return (seg.set_frame_rate(DIALOGUE_FRAME_RATE)
               .set_channels(DIALOGUE_CHANNELS)
               .set_sample_width(2))


def prepare_clip(segment,
                 trim: bool = DIALOGUE_TRIM_TURN_SILENCE,
                 keep_ms: int = DIALOGUE_TRIM_KEEP_MS,
                 threshold_db: float = DIALOGUE_TRIM_THRESHOLD_DB,
                 fade_ms: int = DIALOGUE_EDGE_FADE_MS):
    """
    Get one turn's audio ready to be placed on the timeline.

    Trim the silence the clip arrived with, so the configured gap is the entire
    gap and pacing doesn't drift from turn to turn, then fade the edges to zero
    so the join to silence is silent. Order matters: trimming can expose a
    non-zero edge, so the fade has to come second.
    """
    if trim:
        segment = trim_silence(segment, keep_ms=keep_ms, threshold_db=threshold_db)
    return soften_edges(segment, fade_ms)


def build_timeline(rendered: List[RenderedTurn],
                   gap_same_ms: int = DIALOGUE_GAP_SAME_MS,
                   gap_switch_ms: int = DIALOGUE_GAP_SWITCH_MS,
                   clips: Optional[Dict[str, object]] = None) -> List[Placement]:
    """
    Work out where each rendered turn sits.

    A turn carrying start_ms (audio-in) is pinned there. Otherwise turns follow
    each other, with a longer gap after a speaker change than within one
    speaker's run — that difference is most of what makes a render sound like a
    conversation rather than one continuous read.
    """
    placements: List[Placement] = []
    cursor = 0
    prev_key = None

    for item in rendered:
        seg = (clips or {}).get(item.path) or load_clip(item.path)
        duration = len(seg)
        key = item.turn.key

        if item.turn.start_ms is not None:
            start = max(0, int(item.turn.start_ms))
        else:
            if prev_key is None:
                start = 0
            else:
                start = cursor + (gap_same_ms if key == prev_key else gap_switch_ms)

        placements.append(Placement(
            turn=item.turn, path=item.path, start_ms=start,
            duration_ms=duration, mode=item.mode, note=item.note))
        cursor = start + duration
        prev_key = key

    return placements


def speaker_gains(placements: List[Placement],
                  clips: Dict[str, object],
                  target_dbfs: float = DIALOGUE_TARGET_DBFS,
                  extra_gain_db: Optional[Dict[str, float]] = None
                  ) -> Dict[str, float]:
    """
    Gain per speaker that brings everyone to a common level.

    Measured across all of a speaker's speech at once rather than per clip: per
    clip would flatten the dynamics inside a performance, which is exactly the
    thing Step 1 was tuned to produce.
    """
    extra_gain_db = extra_gain_db or {}
    by_speaker: Dict[str, List[Placement]] = {}
    for p in placements:
        by_speaker.setdefault(p.turn.key, []).append(p)

    gains: Dict[str, float] = {}
    for key, items in by_speaker.items():
        # Energy-weighted mean level over the speaker's clips.
        total_ms = sum(p.duration_ms for p in items) or 1
        energy = 0.0
        for p in items:
            seg = clips[p.path]
            level = seg.dBFS
            if level == float("-inf"):
                continue                      # pure silence — contributes nothing
            energy += (10 ** (level / 10.0)) * p.duration_ms
        if energy <= 0:
            gains[key] = extra_gain_db.get(key, 0.0)
            continue
        mean_dbfs = 10 * math.log10(energy / total_ms)
        match = max(-MAX_MATCH_GAIN_DB,
                    min(MAX_MATCH_GAIN_DB, target_dbfs - mean_dbfs))
        gains[key] = match + extra_gain_db.get(key, 0.0)
    return gains


def silence(length_ms: int):
    """A silent segment in the common format, used as a timeline bed and as
    padding around a short turn before speech-to-speech."""
    AudioSegment = _audio_segment()
    return AudioSegment.silent(duration=max(0, length_ms),
                               frame_rate=DIALOGUE_FRAME_RATE
                               ).set_channels(DIALOGUE_CHANNELS
                               ).set_sample_width(2)


def _fade_len(edge, base_ms: int, max_ms: int) -> int:
    """How long a ramp this edge needs.

    A clip that fades out from near silence needs almost nothing. One cut off
    mid-vowel at a high level needs longer, or the ramp itself is heard as an
    abrupt stop. Scaled between the two rather than fixed, so an unusually loud
    truncation doesn't slip through with a ramp sized for a quiet one.
    """
    if len(edge) == 0:
        return base_ms
    level = edge.max_dBFS
    if level == float("-inf") or level <= -40.0:
        return base_ms
    frac = min(1.0, (level + 40.0) / 40.0)      # -40 dBFS → base, 0 dBFS → max
    return int(round(base_ms + frac * (max_ms - base_ms)))


def soften_edges(segment, fade_ms: int = DIALOGUE_EDGE_FADE_MS):
    """
    Fade a clip in and out so it starts and ends at zero.

    This is what stops the click at the end of every turn. TTS returns audio
    that stops the moment the last phoneme does — the waveform is still swinging
    when the file ends — so laying it on a silent timeline creates a
    single-sample step from that value down to zero, which is heard as a click.
    A ramp of a few milliseconds costs nothing audible on speech and removes it.

    Applied at both ends: the leading edge has the same problem, just quieter.
    """
    if fade_ms <= 0 or len(segment) == 0:
        return segment

    max_ms = fade_ms * 3
    probe = min(len(segment), max(1, max_ms))
    fade_in_ms = _fade_len(segment[:probe], fade_ms, max_ms)
    fade_out_ms = _fade_len(segment[-probe:], fade_ms, max_ms)

    # Never let the two ramps meet in the middle of a very short clip.
    cap = max(1, len(segment) // 3)
    return (segment
            .fade_in(int(min(fade_in_ms, cap)))
            .fade_out(int(min(fade_out_ms, cap))))


@dataclass
class TimedClip:
    """A clip plus where the *speech* actually sits inside it.

    The two are separated because a dub needs both and they pull opposite ways.
    The audio wants a generous margin: a spoken phrase ends in a release that
    decays over 100 ms or more, and cutting that off is heard as the recording
    being chopped. The timeline wants no margin at all: every extra millisecond
    of kept silence lands inside the following pause, and a pause measured from
    the source recording is the one thing a dub must reproduce exactly.

    Keeping the margin but measuring from `speech_ms` satisfies both. The
    retained release rings on into the gap — which is silence anyway, and which
    is exactly what a real voice does — while the next chunk is still scheduled
    from where the speaking actually stopped.
    """
    audio: object
    lead_ms: int = 0        # kept silence before speech starts
    speech_ms: int = 0      # speech onset → offset
    tail_ms: int = 0        # kept release after speech stops

    @property
    def total_ms(self) -> int:
        return len(self.audio)


def measure_speech(segment, threshold_db: float = -45.0) -> tuple:
    """(lead_ms, speech_ms) — where speech begins and how long it runs."""
    _audio_segment()
    from pydub.silence import detect_leading_silence
    lead = detect_leading_silence(segment, silence_threshold=threshold_db)
    if lead >= len(segment):
        return 0, 0                       # nothing but silence
    trail = detect_leading_silence(segment.reverse(), silence_threshold=threshold_db)
    return lead, max(0, len(segment) - lead - trail)


def timed_clip(segment,
               keep_head_ms: int,
               keep_tail_ms: int,
               threshold_db: float = -45.0,
               fade_ms: int = DIALOGUE_EDGE_FADE_MS) -> TimedClip:
    """
    Trim to the speech, keeping a margin at each end, and report where it sits.

    The head and tail margins are separate on purpose. A held-back head is a
    small courtesy — it stops a soft onset being clipped. A held-back tail is
    the difference between a phrase that finishes and one that is cut off, so it
    is the larger of the two by some way.
    """
    lead, speech = measure_speech(segment, threshold_db)
    if speech <= 0:
        return TimedClip(audio=soften_edges(segment, fade_ms), speech_ms=len(segment))

    start = max(0, lead - max(0, keep_head_ms))
    end = min(len(segment), lead + speech + max(0, keep_tail_ms))
    cut = segment[start:end]
    return TimedClip(audio=soften_edges(cut, fade_ms),
                     lead_ms=lead - start,
                     speech_ms=speech,
                     tail_ms=end - (lead + speech))


def trim_silence(segment, keep_ms: int = 60, threshold_db: float = -45.0):
    """
    Trim silence from both ends, keeping a short margin.

    Used to remove the padding put around a short turn before speech-to-speech.
    It detects where the speech actually is rather than cutting back a fixed
    number of milliseconds, because STS is only approximately length-preserving
    — cutting a fixed amount would eventually clip a word.
    """
    _audio_segment()                     # ensures ffmpeg is silenced first
    from pydub.silence import detect_leading_silence
    lead = detect_leading_silence(segment, silence_threshold=threshold_db)
    if lead >= len(segment):
        return segment                   # all silence — leave it alone
    trail = detect_leading_silence(segment.reverse(), silence_threshold=threshold_db)
    start = max(0, lead - keep_ms)
    end = min(len(segment), len(segment) - max(0, trail - keep_ms))
    return segment[start:end] if end > start else segment


def render_tracks(placements: List[Placement],
                  clips: Dict[str, object],
                  gains: Optional[Dict[str, float]] = None):
    """
    Build the master and the per-speaker stems in one pass.

    Returns (master, {speaker_key: (display_name, stem)}). Stems are the same
    length as the master, so dropping them all on a timeline at 00:00 in an
    editor reproduces the master exactly.
    """
    gains = gains or {}
    if not placements:
        raise AssemblyError("Nothing to assemble — no turns were rendered.")

    total = max(p.end_ms for p in placements) + TAIL_PAD_MS
    master = silence(total)
    stems: Dict[str, list] = {}

    for p in placements:
        seg = clips[p.path]
        gain = gains.get(p.turn.key, 0.0)
        if gain:
            seg = seg.apply_gain(gain)
        master = master.overlay(seg, position=p.start_ms)
        entry = stems.setdefault(p.turn.key, [p.turn.speaker, silence(total)])
        entry[1] = entry[1].overlay(seg, position=p.start_ms)

    # Overlapping or simply loud passages can stack past full scale; pull the
    # whole mix down rather than letting it clip. Stems get the same trim so
    # they still sum to the master.
    trim = 0.0
    if master.max_dBFS > MASTER_CEILING_DBFS:
        trim = MASTER_CEILING_DBFS - master.max_dBFS
        master = master.apply_gain(trim)

    out_stems = {}
    for key, (name, seg) in stems.items():
        out_stems[key] = (name, seg.apply_gain(trim) if trim else seg)
    return master, out_stems


def write_manifest(path: str, placements: List[Placement]) -> str:
    """
    Write the per-turn manifest.

    utf-8-sig because this gets opened in Excel, and without the BOM Excel
    renders Devanagari as mojibake — which makes the manifest useless to the
    people most likely to open it.
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["#", "speaker", "mode", "start", "end", "duration_s",
                    "start_ms", "end_ms", "chars", "text", "clip", "note"])
        for p in placements:
            w.writerow([
                p.turn.index + 1,
                p.turn.speaker,
                p.mode,
                timecode(p.start_ms),
                timecode(p.end_ms),
                f"{p.duration_ms / 1000.0:.3f}",
                p.start_ms,
                p.end_ms,
                len(p.turn.text),
                p.turn.text.replace("\n", " "),
                os.path.basename(p.path),
                p.note,
            ])
    return path


def export(segment, path: str) -> str:
    """Write a segment out, picking the container from the file extension."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    ext = (os.path.splitext(path)[1] or ".wav").lstrip(".").lower()
    fmt = {"mp3": "mp3", "wav": "wav", "m4a": "ipod", "ogg": "ogg"}.get(ext, "wav")
    kwargs = {"bitrate": "192k"} if fmt == "mp3" else {}
    segment.export(path, format=fmt, **kwargs)
    return path


@dataclass
class AssemblyResult:
    master_path: str = ""
    stem_paths: Dict[str, str] = None
    manifest_path: str = ""
    placements: List[Placement] = None
    gains: Dict[str, float] = None
    duration_ms: int = 0


def assemble(rendered: List[RenderedTurn],
             out_path: str,
             write_stems: bool = True,
             write_manifest_file: bool = True,
             match_loudness: bool = True,
             gap_same_ms: int = DIALOGUE_GAP_SAME_MS,
             gap_switch_ms: int = DIALOGUE_GAP_SWITCH_MS,
             target_dbfs: float = DIALOGUE_TARGET_DBFS,
             extra_gain_db: Optional[Dict[str, float]] = None,
             trim_turn_silence: bool = DIALOGUE_TRIM_TURN_SILENCE,
             edge_fade_ms: int = DIALOGUE_EDGE_FADE_MS,
             status_cb=None) -> AssemblyResult:
    """
    Lay the rendered turns out and write the master, stems and manifest.

    *out_path* names the master; the stems land in a `stems/` folder beside it
    and the manifest next to it with a `_manifest.csv` suffix.
    """
    if not rendered:
        raise AssemblyError("Nothing to assemble — no turns were rendered.")

    if status_cb:
        status_cb(f"Assembling {len(rendered)} turn(s)…")

    # Trimmed and faded before anything measures or places them, so the
    # durations in the timeline and the manifest are the durations that end up
    # in the master.
    clips = {item.path: prepare_clip(load_clip(item.path),
                                     trim=trim_turn_silence,
                                     fade_ms=edge_fade_ms)
             for item in rendered}
    placements = build_timeline(rendered, gap_same_ms, gap_switch_ms, clips)

    gains = (speaker_gains(placements, clips, target_dbfs, extra_gain_db)
             if match_loudness
             else dict(extra_gain_db or {}))

    if status_cb and match_loudness and gains:
        shown = ", ".join(f"{p:+.1f}dB" for p in
                          (gains[k] for k in sorted(gains)))
        status_cb(f"Loudness match: {shown}")

    master, stems = render_tracks(placements, clips, gains)

    result = AssemblyResult(stem_paths={}, placements=placements, gains=gains,
                            duration_ms=len(master))

    if status_cb:
        status_cb(f"Writing master → {os.path.basename(out_path)}")
    result.master_path = export(master, out_path)

    if write_stems:
        stem_dir = os.path.join(os.path.dirname(os.path.abspath(out_path)), "stems")
        for key, (name, seg) in stems.items():
            safe = safe_filename(name) or key or "speaker"
            result.stem_paths[key] = export(seg, os.path.join(stem_dir, f"{safe}.wav"))
        if status_cb:
            status_cb(f"Wrote {len(result.stem_paths)} stem(s) → stems/")

    if write_manifest_file:
        stem, _ = os.path.splitext(out_path)
        result.manifest_path = write_manifest(f"{stem}_manifest.csv", placements)

    return result


def safe_filename(name: str) -> str:
    """Speaker name → a filename Windows will accept, without flattening
    non-Latin names into nothing."""
    bad = '<>:"/\\|?*'
    cleaned = "".join("_" if (ch in bad or ord(ch) < 32) else ch
                      for ch in str(name or "")).strip(" .")
    return cleaned[:60]
