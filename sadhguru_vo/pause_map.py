"""
Finding where the speaker actually stops.

Everything downstream in Dub Sync is built on the boundaries this module
returns, so it is deliberately the least clever part of the feature: measure the
recording, put the threshold where the recording says it belongs, and hand back
plain numbers.

The output is a PauseMap — the speech segments in order, each with the length of
the pause that follows it. That pause length is the load-bearing value: the dub
reproduces the source's rhythm by reproducing its pauses, so a pause measured
50 ms short is 50 ms of drift introduced before a single word is translated.

Detection runs on a mono 8 kHz copy of the source. Nothing here decodes the full
master more than once.
"""

import os
from dataclasses import dataclass, field
from typing import List

from .audio_backend import audio_segment
from .config import (DUB_ANALYSIS_RATE, DUB_FALLBACK_OFFSET_DB, DUB_FRAME_MS,
                     DUB_MIN_PAUSE_MS, DUB_MIN_RANGE_DB, DUB_MIN_SEGMENT_MS,
                     DUB_NOISE_MARGIN_DB, DUB_NOISE_PCT, DUB_SEEK_STEP_MS,
                     DUB_SPEECH_HEADROOM_DB, DUB_SPEECH_PCT)

# Level assigned to a frame of true digital silence. dBFS would be -inf, which
# poisons every percentile and mean it touches; this is far below any real noise
# floor and behaves like a number.
SILENT_DBFS = -120.0


class PauseMapError(RuntimeError):
    """The source audio could not be read or contained no detectable speech."""


@dataclass
class Segment:
    """One run of speech, and the silence that follows it."""
    index: int
    start_ms: int
    end_ms: int
    pause_after_ms: int = 0

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    @property
    def slot_ms(self) -> int:
        """Speech plus its trailing pause — the room a dubbed chunk can use
        before it starts eating into the next chunk's entry."""
        return self.duration_ms + self.pause_after_ms


@dataclass
class PauseMap:
    """Every segment in a source recording, plus how it was measured.

    The threshold and level fields are kept because the first question asked of
    a bad detection is always "what did it think silence was" — without them the
    only way to answer is to run it again with prints in it.
    """
    path: str
    total_ms: int
    segments: List[Segment] = field(default_factory=list)
    threshold_db: float = 0.0
    noise_db: float = 0.0
    speech_db: float = 0.0
    min_pause_ms: int = DUB_MIN_PAUSE_MS
    lead_in_ms: int = 0          # silence before the first word
    fallback: bool = False       # percentiles unusable; offset rule was used

    @property
    def count(self) -> int:
        return len(self.segments)

    @property
    def speech_ms(self) -> int:
        return sum(s.duration_ms for s in self.segments)

    @property
    def pause_ms(self) -> int:
        """Total silence between segments, excluding the lead-in."""
        return sum(s.pause_after_ms for s in self.segments)

    def summary(self) -> str:
        """One line for the status bar."""
        speech_pct = (100.0 * self.speech_ms / self.total_ms) if self.total_ms else 0.0
        return (f"{self.count} segments · {_mmss(self.total_ms)} total · "
                f"{speech_pct:.0f}% speech · threshold {self.threshold_db:.1f} dBFS"
                + (" (fallback)" if self.fallback else ""))


def _mmss(ms: int) -> str:
    """M:SS.s — short enough for a table cell, precise enough to trust."""
    ms = max(0, int(ms))
    minutes, rem = divmod(ms, 60_000)
    return f"{minutes}:{rem / 1000:04.1f}"


# ═════════════════════════════════════════════════════════════════════════════
#  Level profile
# ═════════════════════════════════════════════════════════════════════════════

def _dbfs(rms: int, max_amplitude: int) -> float:
    """RMS count → dBFS, with digital silence pinned to SILENT_DBFS."""
    if rms <= 0:
        return SILENT_DBFS
    import math
    return 20.0 * math.log10(rms / float(max_amplitude))


def level_profile(segment, frame_ms: int = DUB_FRAME_MS) -> List[float]:
    """
    Per-frame level of the whole recording, in dBFS.

    Computed over the raw sample bytes with audioop rather than by slicing the
    AudioSegment: a ten-minute file is 30,000 frames, and 30,000 AudioSegment
    objects cost seconds of wall clock and a lot of garbage for a number that is
    one C call away.
    """
    import audioop

    width = segment.sample_width
    channels = segment.channels
    rate = segment.frame_rate
    raw = segment.raw_data

    bytes_per_frame = width * channels
    step = max(1, int(rate * frame_ms / 1000.0)) * bytes_per_frame
    peak = float(2 ** (8 * width - 1))

    levels: List[float] = []
    for i in range(0, len(raw) - step + 1, step):
        levels.append(_dbfs(audioop.rms(raw[i:i + step], width), peak))
    return levels


def _percentile(sorted_values: List[float], pct: float) -> float:
    """Nearest-rank percentile. Input must already be sorted."""
    if not sorted_values:
        return SILENT_DBFS
    k = int(round((pct / 100.0) * (len(sorted_values) - 1)))
    return sorted_values[max(0, min(len(sorted_values) - 1, k))]


def estimate_threshold(levels: List[float]) -> tuple:
    """
    Where silence ends and speech begins, for this particular recording.

    Returns (threshold_db, noise_db, speech_db, used_fallback).

    The noise floor and the speech level are read off the low and high
    percentiles of the frame levels. The threshold then sits a margin above the
    floor — close enough to catch a real pause, far enough that room tone or a
    ventilation hum doesn't register as speech.

    Two things can go wrong, and both are handled rather than detected later as
    a nonsensical segment count:

      * floor and speech too close together — heavy compression, a noisy room,
        or a file that is almost entirely speech with no true silence in it. The
        percentiles carry no information, so a plain offset below speech level
        is used instead.
      * the margin would put the threshold on top of the speech itself, which
        would swallow every soft syllable. Clamped to stay below it.
    """
    if not levels:
        return SILENT_DBFS, SILENT_DBFS, SILENT_DBFS, True

    ordered = sorted(levels)
    noise = _percentile(ordered, DUB_NOISE_PCT)
    speech = _percentile(ordered, DUB_SPEECH_PCT)

    if speech - noise < DUB_MIN_RANGE_DB:
        return speech - DUB_FALLBACK_OFFSET_DB, noise, speech, True

    threshold = noise + DUB_NOISE_MARGIN_DB
    ceiling = speech - DUB_SPEECH_HEADROOM_DB
    return min(threshold, ceiling), noise, speech, False


# ═════════════════════════════════════════════════════════════════════════════
#  Segmentation
# ═════════════════════════════════════════════════════════════════════════════

def _absorb_short(ranges: List[List[int]], min_segment_ms: int) -> List[List[int]]:
    """
    Fold sub-threshold blips into the segment before them.

    A breath or a lip smack clears the level threshold for 80 ms and arrives
    looking like a chunk of speech. Dropping it outright would leave a hole in
    the timeline that no chunk covers, so it is absorbed into the previous
    segment instead — the audio stays accounted for, and it stops being
    something the user has to write a translation for.
    """
    if not ranges:
        return ranges

    out: List[List[int]] = [list(ranges[0])]
    for start, end in ranges[1:]:
        if end - start < min_segment_ms:
            out[-1][1] = end            # extend the previous segment over it
        else:
            out.append([start, end])

    # A short *first* segment has nothing before it to fold into, so it merges
    # forward instead. Only when there is something to merge with.
    if len(out) > 1 and out[0][1] - out[0][0] < min_segment_ms:
        out[1][0] = out[0][0]
        out.pop(0)
    return out


def _refine_edges(probe, ranges: List[List[int]], threshold_db: float,
                  total_ms: int) -> List[List[int]]:
    """
    Find the true first onset and last offset, ignoring min_pause_ms.

    min_pause_ms exists to stop one sentence shattering at every breath, so it
    is only meaningful *between* segments. Applied to the head of the file it
    does the opposite of its job: a 300 ms count-in is shorter than the minimum
    pause, so the scan never calls it silence and hands back a first segment
    that starts at zero. The dub then starts speaking 300 ms before the source
    does — an offset on the very first word, and one that no later chunk
    corrects because every chunk after it is measured from its own timestamp.

    There is no over-segmentation to protect against before the first word or
    after the last, so both edges are trimmed at the threshold alone.
    """
    if not ranges:
        return ranges
    from pydub.silence import detect_leading_silence

    lead = detect_leading_silence(probe, silence_threshold=threshold_db)
    if 0 < lead < total_ms:
        ranges[0][0] = max(ranges[0][0], min(lead, ranges[0][1] - 1))

    trail = detect_leading_silence(probe.reverse(), silence_threshold=threshold_db)
    if 0 < trail < total_ms:
        ranges[-1][1] = min(ranges[-1][1], max(total_ms - trail, ranges[-1][0] + 1))
    return ranges


def detect_segments(path: str,
                    min_pause_ms: int = DUB_MIN_PAUSE_MS,
                    min_segment_ms: int = DUB_MIN_SEGMENT_MS,
                    sensitivity_db: float = 0.0,
                    status_cb=None) -> PauseMap:
    """
    Read *path* and return its PauseMap.

    *sensitivity_db* shifts the measured threshold: negative catches quieter
    speech and so finds fewer, longer segments; positive splits more eagerly. It
    exists because "the recording says so" is right most of the time and the
    remaining times need a knob, not an argument.
    """
    if not path or not os.path.isfile(path):
        raise PauseMapError(f"Source audio not found: {path}")

    AudioSegment = audio_segment()      # also silences ffmpeg's console on Windows
    if status_cb:
        status_cb("Pause map: decoding source audio…")
    try:
        source = AudioSegment.from_file(path)
    except Exception as e:
        raise PauseMapError(
            f"Could not decode {os.path.basename(path)}: {e}\n"
            "ffmpeg needs to be on PATH — install it with "
            "`winget install Gyan.FFmpeg`.") from None

    total_ms = len(source)
    if total_ms <= 0:
        raise PauseMapError(f"{os.path.basename(path)} contains no audio.")

    # Analysis copy: mono so a speaker panned to one side isn't measured at half
    # level, 8 kHz because pause edges are wanted to ~10 ms and nothing finer.
    probe = (source.set_channels(1)
                   .set_frame_rate(DUB_ANALYSIS_RATE)
                   .set_sample_width(2))

    if status_cb:
        status_cb("Pause map: measuring levels…")
    levels = level_profile(probe)
    threshold, noise, speech, fallback = estimate_threshold(levels)
    threshold += sensitivity_db

    if status_cb:
        status_cb(f"Pause map: scanning at {threshold:.1f} dBFS…")
    from pydub.silence import detect_nonsilent
    ranges = detect_nonsilent(probe,
                              min_silence_len=max(1, int(min_pause_ms)),
                              silence_thresh=threshold,
                              seek_step=DUB_SEEK_STEP_MS)

    if not ranges:
        raise PauseMapError(
            f"No speech found in {os.path.basename(path)} at "
            f"{threshold:.1f} dBFS. If the recording is very quiet, lower the "
            "sensitivity; if it is silent, it is the wrong file.")

    ranges = _refine_edges(probe, [list(r) for r in ranges], threshold, total_ms)
    ranges = _absorb_short(ranges, min_segment_ms)

    segments: List[Segment] = []
    for i, (start, end) in enumerate(ranges):
        end = min(int(end), total_ms)
        nxt = int(ranges[i + 1][0]) if i + 1 < len(ranges) else total_ms
        segments.append(Segment(index=i,
                                start_ms=int(start),
                                end_ms=end,
                                pause_after_ms=max(0, nxt - end)))

    return PauseMap(path=path,
                    total_ms=total_ms,
                    segments=segments,
                    threshold_db=threshold,
                    noise_db=noise,
                    speech_db=speech,
                    min_pause_ms=int(min_pause_ms),
                    lead_in_ms=segments[0].start_ms,
                    fallback=fallback)


# ═════════════════════════════════════════════════════════════════════════════
#  Manual correction
# ═════════════════════════════════════════════════════════════════════════════
#  Detection gets the boundaries close; the person looking at the timeline gets
#  them right. These keep pause_after_ms consistent so nothing downstream has to
#  recompute it.

def _reindex(segments: List[Segment], total_ms: int) -> List[Segment]:
    """Renumber and recompute every trailing pause from the boundaries."""
    out: List[Segment] = []
    for i, s in enumerate(segments):
        nxt = segments[i + 1].start_ms if i + 1 < len(segments) else total_ms
        out.append(Segment(index=i, start_ms=s.start_ms, end_ms=s.end_ms,
                           pause_after_ms=max(0, nxt - s.end_ms)))
    return out


def merge_at(pmap: PauseMap, index: int) -> PauseMap:
    """Join segment *index* with the one after it, swallowing the pause between.

    Used when detection split one sentence at a breath.
    """
    segs = list(pmap.segments)
    if not 0 <= index < len(segs) - 1:
        return pmap
    merged = Segment(index=index,
                     start_ms=segs[index].start_ms,
                     end_ms=segs[index + 1].end_ms)
    segs[index:index + 2] = [merged]
    return _replace_segments(pmap, segs)


def split_at(pmap: PauseMap, index: int, at_ms: int) -> PauseMap:
    """Cut segment *index* in two at absolute position *at_ms*.

    Used when two sentences ran together under the pause threshold. The halves
    meet with no pause between them, which is the truth — there wasn't one.
    """
    segs = list(pmap.segments)
    if not 0 <= index < len(segs):
        return pmap
    s = segs[index]
    if not s.start_ms < at_ms < s.end_ms:
        return pmap
    segs[index:index + 1] = [
        Segment(index=index, start_ms=s.start_ms, end_ms=int(at_ms)),
        Segment(index=index + 1, start_ms=int(at_ms), end_ms=s.end_ms),
    ]
    return _replace_segments(pmap, segs)


def _replace_segments(pmap: PauseMap, segments: List[Segment]) -> PauseMap:
    """A copy of *pmap* carrying new segments, pauses recomputed."""
    return PauseMap(path=pmap.path,
                    total_ms=pmap.total_ms,
                    segments=_reindex(segments, pmap.total_ms),
                    threshold_db=pmap.threshold_db,
                    noise_db=pmap.noise_db,
                    speech_db=pmap.speech_db,
                    min_pause_ms=pmap.min_pause_ms,
                    lead_in_ms=segments[0].start_ms if segments else 0,
                    fallback=pmap.fallback)
