"""
Matching a pasted script to a detected pause map.

The audio says where the pauses are. The script says what is being said. This
module lines the two up and produces the Chunk list everything else works on.

There is no way to do this perfectly without listening to the audio, and this
module deliberately does not pretend otherwise. It does three things in order of
decreasing confidence:

  1. If the script has exactly as many non-empty lines as there are segments,
     use them one to one. This is exact, and it is worth telling the user about,
     because reformatting a script into one line per pause turns a guess into a
     certainty.
  2. Otherwise split proportionally — each chunk gets a share of the text sized
     to its share of the speech — and snap every cut to the nearest real
     boundary, preferring a sentence end over a comma over a bare space.
  3. Whatever is left the user fixes by hand, so merge and split are first-class
     operations here rather than an afterthought.

Text edits survive merge and split, which is why chunks (not the pause map) are
the editable state.
"""

import re
from dataclasses import dataclass, field, replace
from typing import List, Optional, Sequence, Tuple

from .config import DIALOGUE_GAP_SAME_MS, DIALOGUE_GAP_SWITCH_MS
from .pause_map import PauseMap
from .script_parser import normalize_speaker, split_label

# Cut-point preference. Lower is better, and the ranks are ordered by how likely
# a speaker is to have paused there.
RANK_LINE     = 0    # an explicit line break in the pasted script
RANK_SENTENCE = 1    # । ॥ . ? ! …
RANK_CLAUSE   = 2    # , ; : — –
RANK_WORD     = 3    # any whitespace

# How far a cut may be dragged to reach a better boundary, as a multiple of the
# average chunk length. A cut that has to travel further than this is not
# improving the alignment, it is moving a whole clause into the wrong chunk.
RANK_COST = 0.25

SENTENCE_END = "।॥.?!…"
CLAUSE_END   = ",;:—–"

_LINE_BREAK = re.compile(r"\n+")
_WS         = re.compile(r"\s")


@dataclass
class Chunk:
    """One segment of the timeline: when it happens, who says it, and what.

    start_ms/end_ms/pause_after_ms are the only timing truth in the feature —
    everything the estimator and the renderer do is measured against them.
    Where they came from is *pinned*:

      pinned=True   measured from a source recording. The render must put the
                    chunk at start_ms, because something in the world already
                    happened there.
      pinned=False  planned from a script. start_ms is a prediction made from
                    estimated durations and the gap rule — good enough to draw a
                    timeline and quote a total length, but the render lets the
                    real clip lengths decide, exactly as the Dialogue path
                    always has.

    The distinction is honoured all the way down: `assembly.build_timeline`
    already pins a turn carrying start_ms and computes a position for one that
    does not, so a planned chunk simply arrives there without it.
    """
    index: int
    start_ms: int
    end_ms: int
    pause_after_ms: int
    source_text: str = ""
    target_text: str = ""
    # Who is speaking. Empty for a single-speaker dub; set from the script's
    # labels when there are any, and then used to pick this chunk's voice and
    # its number of steps out of the cast.
    speaker: str = ""
    # Defaults to True so every existing caller — all of which build chunks from
    # a pause map — keeps its current meaning without being touched.
    pinned: bool = True

    @property
    def key(self) -> str:
        """Cast lookup key — matches SpeakerRecipe.key."""
        return normalize_speaker(self.speaker)

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    @property
    def slot_ms(self) -> int:
        """Speech plus trailing pause: the room available before the next chunk
        is due to start."""
        return self.duration_ms + self.pause_after_ms


# ═════════════════════════════════════════════════════════════════════════════
#  Boundary finding
# ═════════════════════════════════════════════════════════════════════════════

def _candidates(text: str) -> List[Tuple[int, int]]:
    """
    Every position the text may be cut at, with how good a cut it is.

    A position is the index of the first character of the *next* chunk, so
    trailing punctuation and the whitespace after it stay with the chunk they
    belong to.
    """
    found: dict = {}

    def offer(pos: int, rank: int):
        if 0 < pos < len(text) and rank < found.get(pos, 99):
            found[pos] = rank

    for m in _LINE_BREAK.finditer(text):
        offer(m.end(), RANK_LINE)

    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in SENTENCE_END or ch in CLAUSE_END:
            rank = RANK_SENTENCE if ch in SENTENCE_END else RANK_CLAUSE
            # Skip any run of punctuation ("?!", "...") then the whitespace.
            j = i + 1
            while j < n and (text[j] in SENTENCE_END or text[j] in CLAUSE_END):
                j += 1
            k = j
            while k < n and _WS.match(text[k]):
                k += 1
            if k > j or j >= n:          # must be followed by space or be the end
                offer(k, rank)
            i = k if k > i else i + 1
            continue
        if _WS.match(ch):
            j = i
            while j < n and _WS.match(text[j]):
                j += 1
            offer(j, RANK_WORD)
            i = j
            continue
        i += 1

    return sorted(found.items())


def _choose_cuts(text: str, weights: Sequence[float]) -> List[int]:
    """
    Pick len(weights) - 1 cut positions splitting *text* by *weights*.

    Each cut goes where the proportional share says it should, pulled to the
    nearest real boundary by a cost that trades distance against boundary
    quality. Cuts are chosen left to right and forced to increase, so a chunk is
    never handed text that belongs to one before it.
    """
    n_parts = len(weights)
    if n_parts <= 1:
        return []

    total_w = float(sum(weights)) or 1.0
    length = len(text)
    avg = length / n_parts
    penalty = RANK_COST * avg

    cands = _candidates(text)
    cuts: List[int] = []
    prev = 0
    running = 0.0

    for i in range(n_parts - 1):
        running += weights[i]
        desired = (running / total_w) * length

        # Only boundaries after the previous cut, and leaving room for the
        # chunks still to come.
        room = n_parts - i - 1
        usable = [(p, r) for p, r in cands if prev < p <= length - room]
        if not usable:
            # No boundary left — fall back to a hard character cut so the chunk
            # count still matches the segment count. The UI shows this as a
            # chunk the user needs to look at.
            cuts.append(min(max(prev + 1, int(round(desired))), length - room))
            prev = cuts[-1]
            continue

        best = min(usable, key=lambda pr: abs(pr[0] - desired) + pr[1] * penalty)
        cuts.append(best[0])
        prev = best[0]

    return cuts


def split_text(text: str, weights: Sequence[float]) -> List[str]:
    """Split *text* into len(weights) pieces sized by *weights*."""
    text = (text or "").strip()
    n_parts = len(weights)
    if n_parts <= 0:
        return []
    if not text:
        return [""] * n_parts
    if n_parts == 1:
        return [text]

    cuts = _choose_cuts(text, weights)
    pieces: List[str] = []
    prev = 0
    for cut in cuts:
        pieces.append(text[prev:cut].strip())
        prev = cut
    pieces.append(text[prev:].strip())
    return pieces


def _lines(text: str) -> List[str]:
    """Non-empty lines of a pasted script, whitespace normalised."""
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


# ═════════════════════════════════════════════════════════════════════════════
#  Speaker labels
# ═════════════════════════════════════════════════════════════════════════════

def labelled_lines(text: str, default_speaker: str = "") -> List[Tuple[str, str]]:
    """
    Non-empty lines as (speaker, text).

    An unlabelled line continues whoever spoke last, the same rule parse_script
    uses, so a speaker's paragraph can wrap without repeating the label. Lines
    before any label go to *default_speaker*, or to "" when there is none.
    """
    known: set = set()
    if default_speaker:
        known.add(normalize_speaker(default_speaker))
    current = default_speaker
    out: List[Tuple[str, str]] = []
    for line in _lines(text):
        speaker, body = split_label(line, known)
        if speaker:
            current = speaker
        if body:
            out.append((current or "", body))
    return out


def has_labels(text: str) -> bool:
    """True when the script carries speaker labels at all."""
    known: set = set()
    return any(split_label(line, known)[0] for line in _lines(text))


def plan_from_script(source_text: str,
                     target_text: str = "",
                     gap_same_ms: int = DIALOGUE_GAP_SAME_MS,
                     gap_switch_ms: int = DIALOGUE_GAP_SWITCH_MS,
                     default_speaker: str = "",
                     merge_same_speaker: bool = True
                     ) -> Tuple[List[Chunk], AlignReport]:
    """
    Build the chunk list from a script alone — no recording, no pause map.

    This is the other half of `align()`. Both produce the same thing; they
    differ only in where the gap after each chunk comes from. Here it comes from
    the rule the Dialogue path has always used: a longer gap after a speaker
    change than within one speaker's run, which is most of what makes a rendered
    script sound like turn-taking rather than one continuous read.

    Chunks come back **unpinned**, with zero-length placeholder timings. Real
    positions are not knowable yet — they depend on how long each line turns out
    to take — so the estimator predicts them for the timeline and the renderer
    lets the actual clip lengths decide. Writing a guess into start_ms and then
    pinning to it would be the one way to make this path drift.

    *target_text* is optional: with no translation this is a plain
    same-language render, and the source text is what gets spoken.
    """
    lines = labelled_lines(source_text, default_speaker)
    if merge_same_speaker:
        lines = _runs(lines)

    report = AlignReport(source_lines=len(_lines(source_text)),
                         target_lines=len(_lines(target_text)))

    if not lines:
        report.source_mode = "empty"
        return [], report

    report.source_mode = "lines"
    report.segment_count = len(lines)

    # Target text is matched line for line when it lines up, and split across
    # the source chunks by length when it does not — the same two rules align()
    # uses, so a pasted translation behaves identically in both modes.
    target_lines = labelled_lines(target_text, default_speaker)
    if merge_same_speaker:
        target_lines = _runs(target_lines)

    if not target_text.strip():
        # No translation means this is a same-language render, not a silent one.
        # The renderer speaks target_text, so with nothing else to say the
        # source text *is* the target — leaving it blank would mark every chunk
        # EMPTY and produce a file of pure silence, which is the single most
        # common way to use this mode.
        target_parts = [body for _s, body in lines]
        report.target_mode = "same-language"
    elif len(target_lines) == len(lines):
        target_parts = [body for _s, body in target_lines]
        report.target_mode = "lines"
    else:
        weights = [max(1, len(body)) for _s, body in lines]
        target_parts = split_text(" ".join(b for _s, b in target_lines), weights)
        report.target_mode = "proportional"

    chunks: List[Chunk] = []
    for i, (speaker, body) in enumerate(lines):
        if i + 1 < len(lines):
            same = normalize_speaker(lines[i + 1][0]) == normalize_speaker(speaker)
            pause = gap_same_ms if same else gap_switch_ms
        else:
            pause = 0
        chunks.append(Chunk(index=i, start_ms=0, end_ms=0,
                            pause_after_ms=max(0, int(pause)),
                            source_text=body,
                            target_text=target_parts[i],
                            speaker=speaker,
                            pinned=False))

    report.speakers = speakers_in_chunks(chunks)
    report.empty_source = sum(1 for c in chunks if not c.source_text.strip())
    report.empty_target = sum(1 for c in chunks if not c.target_text.strip())
    return chunks, report


def speakers_in_script(text: str, default_speaker: str = "") -> List[str]:
    """
    Distinct speakers named by a script, in order of first appearance.

    Cheap enough to run on every keystroke, which is the point: the cast table
    has to appear as soon as a script names somebody, not after the operator
    finds the button that builds it. Chunking the script to find that out would
    throw away any per-chunk edits already made.
    """
    seen: dict = {}
    for speaker, _body in labelled_lines(text, default_speaker):
        key = normalize_speaker(speaker)
        if key and key not in seen:
            seen[key] = speaker
    return list(seen.values())


def speakers_in_chunks(chunks: Sequence[Chunk]) -> List[str]:
    """Distinct speakers in order of first appearance, in their first spelling —
    which is the spelling the cast table shows."""
    seen: dict = {}
    for c in chunks:
        if c.speaker and c.key not in seen:
            seen[c.key] = c.speaker
    return list(seen.values())


def _runs(lines: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """Collapse consecutive lines by one speaker into (speaker, joined text)."""
    runs: List[Tuple[str, str]] = []
    for speaker, body in lines:
        if runs and normalize_speaker(runs[-1][0]) == normalize_speaker(speaker):
            runs[-1] = (runs[-1][0], runs[-1][1] + " " + body)
        else:
            runs.append((speaker, body))
    return runs


def _allocate(runs: List[Tuple[str, str]], n_segments: int) -> List[int]:
    """
    How many segments each speaker run gets, by largest remainder.

    Splitting the whole script at once and then asking who owns each chunk would
    let one chunk straddle a speaker change — and a speaker change is exactly
    where a real pause is, so that chunk would be the one place the dub is
    guaranteed to be wrong. Allocating segments to runs first makes straddling
    impossible: every chunk belongs to one person by construction.

    Every run gets at least one segment, so nobody's lines vanish.
    """
    total_chars = sum(max(1, len(text)) for _s, text in runs) or 1
    exact = [max(1, len(text)) / total_chars * n_segments for _s, text in runs]
    counts = [max(1, int(x)) for x in exact]

    # Largest-remainder settle-up, then trim overshoot from the biggest holders.
    while sum(counts) < n_segments:
        i = max(range(len(counts)), key=lambda k: exact[k] - counts[k])
        counts[i] += 1
    while sum(counts) > n_segments:
        i = max(range(len(counts)),
                key=lambda k: (counts[k] - exact[k], counts[k]))
        if counts[i] <= 1:
            break                       # cannot take the last segment off anyone
        counts[i] -= 1
    return counts


def _split_by_runs(lines: List[Tuple[str, str]],
                   weights: Sequence[float]) -> Optional[List[Tuple[str, str]]]:
    """
    Split a labelled script across len(weights) segments, speaker by speaker.

    Returns None when there are more speaker runs than segments — detection
    found fewer pauses than there are turns, which no split can fix and which
    the caller reports rather than papering over.
    """
    runs = _runs(lines)
    n = len(weights)
    if not runs or len(runs) > n:
        return None

    counts = _allocate(runs, n)
    out: List[Tuple[str, str]] = []
    pos = 0
    for (speaker, text), count in zip(runs, counts):
        share = list(weights[pos:pos + count]) or [1.0]
        for piece in split_text(text, share):
            out.append((speaker, piece))
        pos += count

    # Allocation is settled against n, but guard the arithmetic anyway: a short
    # list here would silently drop the tail of the script.
    if len(out) != n:
        return None
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  Alignment
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class AlignReport:
    """How the split was arrived at — shown in the UI so the user knows whether
    to trust it or start fixing boundaries."""
    source_mode: str = ""        # "lines" | "by-speaker" | "proportional" | "empty"
    target_mode: str = ""
    segment_count: int = 0
    source_lines: int = 0
    target_lines: int = 0
    empty_source: int = 0
    empty_target: int = 0
    speakers: List[str] = field(default_factory=list)
    too_many_turns: bool = False   # more speaker runs than detected pauses

    def notes(self) -> List[str]:
        out: List[str] = []
        if self.speakers:
            out.append(f"Speakers: {', '.join(self.speakers)} — each renders "
                       f"through its own cast recipe.")
        if self.too_many_turns:
            out.append(f"⚠ The script has more speaker turns than the audio has "
                       f"pauses ({self.segment_count} detected). Lower the minimum "
                       f"pause, or raise sensitivity, so every turn boundary is "
                       f"found — speakers cannot be assigned reliably until then.")
        if self.source_mode == "lines":
            out.append(f"Source split by line — {self.segment_count} lines matched "
                       f"{self.segment_count} segments exactly.")
        elif self.source_mode == "by-speaker":
            out.append(f"Source split per speaker run across {self.segment_count} "
                       f"segments, so no chunk straddles a speaker change.")
        elif self.source_mode == "proportional":
            out.append(f"Source split proportionally — {self.source_lines} lines vs "
                       f"{self.segment_count} segments. Check the boundaries; "
                       f"reformatting the script to one line per pause makes this exact.")
        if self.target_mode == "same-language":
            out.append("No translation pasted — this renders the source script "
                       "as-is, in the source language.")
        elif self.target_mode == "lines":
            out.append(f"Target split by line — {self.segment_count} lines matched.")
        elif self.target_mode == "proportional":
            out.append(f"Target split proportionally — {self.target_lines} lines vs "
                       f"{self.segment_count} segments.")
        if self.empty_source:
            out.append(f"⚠ {self.empty_source} chunk(s) got no source text — the script "
                       f"has fewer usable breaks than the audio has pauses. Merge those "
                       f"segments or paste a longer script.")
        if self.empty_target:
            out.append(f"⚠ {self.empty_target} chunk(s) have no target text yet.")
        return out


def _split_side(text: str, weights: Sequence[float], n: int,
                default_speaker: str) -> Tuple[List[str], List[str], str]:
    """
    Split one script across *n* segments. Returns (texts, speakers, mode).

    Three routes, in order of how much they can be trusted:

      lines         one labelled line per segment — exact, nothing inferred
      by-speaker    labelled, but the counts differ: allocate segments to each
                    speaker's run and split within the run
      proportional  no labels at all — the single-speaker case
    """
    if not text.strip():
        return [""] * n, [default_speaker] * n, "empty"

    lines = labelled_lines(text, default_speaker)
    if lines and len(lines) == n:
        return ([body for _s, body in lines],
                [speaker or default_speaker for speaker, _b in lines],
                "lines")

    if has_labels(text):
        by_run = _split_by_runs(lines, weights)
        if by_run is not None:
            return ([body for _s, body in by_run],
                    [speaker or default_speaker for speaker, _b in by_run],
                    "by-speaker")
        # More turns than pauses. Fall through to a flat split rather than
        # failing: the report says the speakers are unreliable, and the user can
        # still see the chunks and fix the detection.
        flat = " ".join(body for _s, body in lines)
        return (split_text(flat, weights), [default_speaker] * n, "proportional")

    plain_lines = _lines(text)
    if len(plain_lines) == n:
        return plain_lines, [default_speaker] * n, "lines"
    return split_text(text, weights), [default_speaker] * n, "proportional"


def align(pmap: PauseMap,
          source_text: str = "",
          target_text: str = "",
          default_speaker: str = "") -> Tuple[List[Chunk], AlignReport]:
    """
    Build the chunk list for *pmap* from the pasted scripts.

    Source text is split against the segments' *speech* durations — the pauses
    are not being spoken, so counting them would hand a long silence a share of
    the words. Target text is then split against the source chunks' character
    counts rather than against durations again, so the translation tracks the
    text it is a translation of.

    When the source script carries speaker labels each chunk records who is
    speaking, and the split is done a speaker run at a time so no chunk can
    straddle a change of speaker. The translation inherits the source chunk's
    speaker unless it is labelled itself — a translator is not required to
    reproduce the labels, and where they do, theirs win.
    """
    segments = pmap.segments
    report = AlignReport(segment_count=len(segments),
                         source_lines=len(_lines(source_text)),
                         target_lines=len(_lines(target_text)))
    if not segments:
        return [], report

    n = len(segments)
    weights = [max(1, s.duration_ms) for s in segments]

    source_parts, source_speakers, report.source_mode = _split_side(
        source_text, weights, n, default_speaker)

    if target_text.strip():
        # Weight by the source split so chunk 3 of the translation covers chunk
        # 3 of the script. Falls back to duration when the source is empty.
        tgt_weights = [max(1, len(p)) for p in source_parts]
        if not any(len(p) for p in source_parts):
            tgt_weights = weights
        target_parts, target_speakers, report.target_mode = _split_side(
            target_text, tgt_weights, n, default_speaker)
    else:
        target_parts = [""] * n
        target_speakers = [""] * n
        report.target_mode = "empty"

    if has_labels(source_text) and report.source_mode == "proportional":
        report.too_many_turns = True

    chunks = []
    for i, s in enumerate(segments):
        speaker = source_speakers[i] or target_speakers[i] or default_speaker
        chunks.append(Chunk(index=i,
                            start_ms=s.start_ms,
                            end_ms=s.end_ms,
                            pause_after_ms=s.pause_after_ms,
                            source_text=source_parts[i],
                            target_text=target_parts[i],
                            speaker=speaker))

    report.speakers = speakers_in_chunks(chunks)
    report.empty_source = sum(1 for c in chunks if not c.source_text.strip())
    report.empty_target = sum(1 for c in chunks if not c.target_text.strip())
    return chunks, report


# ═════════════════════════════════════════════════════════════════════════════
#  Manual correction
# ═════════════════════════════════════════════════════════════════════════════

def _renumber(chunks: List[Chunk], total_ms: Optional[int] = None) -> List[Chunk]:
    """Renumber and recompute every trailing pause from the boundaries.

    The last chunk keeps whatever trailing pause it had unless *total_ms* is
    given, because the tail silence is a property of the recording rather than
    of the chunk before it.
    """
    out: List[Chunk] = []
    for i, c in enumerate(chunks):
        if i + 1 < len(chunks):
            pause = max(0, chunks[i + 1].start_ms - c.end_ms)
        elif total_ms is not None:
            pause = max(0, total_ms - c.end_ms)
        else:
            pause = c.pause_after_ms
        out.append(replace(c, index=i, pause_after_ms=pause))
    return out


def merge(chunks: List[Chunk], index: int) -> List[Chunk]:
    """
    Join chunk *index* with the one after it.

    The pause between them disappears into the merged chunk's speech, which is
    correct: the speaker did pause there, and the merged chunk's slot has to
    include that silence or the timeline loses time.
    """
    if not 0 <= index < len(chunks) - 1:
        return chunks
    a, b = chunks[index], chunks[index + 1]
    joined = Chunk(index=index,
                   start_ms=a.start_ms,
                   end_ms=b.end_ms,
                   pause_after_ms=b.pause_after_ms,
                   source_text=" ".join(t for t in (a.source_text, b.source_text) if t),
                   target_text=" ".join(t for t in (a.target_text, b.target_text) if t),
                   # The first speaker keeps the merged chunk. Merging across a
                   # speaker change is the user overriding detection, and one
                   # clip can only be rendered in one voice.
                   speaker=a.speaker or b.speaker)
    out = list(chunks)
    out[index:index + 2] = [joined]
    return _renumber(out)


def split(chunks: List[Chunk], index: int, at_ms: int,
          source_cut: Optional[int] = None,
          target_cut: Optional[int] = None) -> List[Chunk]:
    """
    Cut chunk *index* in two at absolute time *at_ms*.

    The halves meet with no pause between them, because there wasn't one — the
    split is being made inside a run of continuous speech. Text is divided at
    *source_cut* / *target_cut* character offsets when given, otherwise
    proportionally to where the time cut fell.
    """
    if not 0 <= index < len(chunks):
        return chunks
    c = chunks[index]
    if not c.start_ms < at_ms < c.end_ms:
        return chunks

    frac = (at_ms - c.start_ms) / float(c.duration_ms or 1)

    def halve(text: str, cut: Optional[int]) -> Tuple[str, str]:
        text = text.strip()
        if not text:
            return "", ""
        if cut is None:
            parts = split_text(text, [max(frac, 0.01), max(1.0 - frac, 0.01)])
            return parts[0], parts[1]
        cut = max(0, min(len(text), cut))
        return text[:cut].strip(), text[cut:].strip()

    src_a, src_b = halve(c.source_text, source_cut)
    tgt_a, tgt_b = halve(c.target_text, target_cut)

    out = list(chunks)
    out[index:index + 1] = [
        Chunk(index=index, start_ms=c.start_ms, end_ms=int(at_ms),
              pause_after_ms=0, source_text=src_a, target_text=tgt_a,
              speaker=c.speaker),
        Chunk(index=index + 1, start_ms=int(at_ms), end_ms=c.end_ms,
              pause_after_ms=c.pause_after_ms, source_text=src_b, target_text=tgt_b,
              speaker=c.speaker),
    ]
    return _renumber(out)


def rebalance_target(chunks: List[Chunk], target_text: str) -> List[Chunk]:
    """
    Re-split a freshly pasted translation across the current chunk boundaries.

    Used when the timings have been corrected by hand and the translation needs
    redistributing over them without losing the timing work.
    """
    if not chunks:
        return chunks
    lines = _lines(target_text)
    if len(lines) == len(chunks):
        parts = lines
    else:
        weights = [max(1, len(c.source_text) or c.duration_ms) for c in chunks]
        parts = split_text(target_text, weights)
    return [replace(c, target_text=parts[i]) for i, c in enumerate(chunks)]
