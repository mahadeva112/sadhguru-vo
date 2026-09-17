"""
Turns a speaker-labelled script into a list of Turns.

    SADHGURU: जीवन एक अवसर है।
    INTERVIEWER: But Sadhguru, how does one begin?
    SADHGURU: [contemplative] You begin by sitting still.

The Turn is the atomic unit of the whole multi-speaker path: it is what gets
rendered, what gets placed on the timeline, and what the manifest lists. The
single-speaker pipeline chunks a whole script by character count, which is
exactly what must NOT happen here — a chunk that spans two speakers gets
rendered in one continuous breath, in one voice.

A Turn can optionally carry start_ms / end_ms. Nothing in the script path sets
them, but the audio-in dubbing adapter will: it produces the same Turn list with
the original recording's timings attached, and the assembler places timed turns
at those positions instead of using default gaps. That is the whole reason the
fields exist here rather than being invented later.
"""

import re
from dataclasses import dataclass
from typing import List, Optional

# An optional "[00:12.500 - 00:15.200]" prefix, used by the audio-in adapter to
# carry a turn's position in the source recording.
_TIME_RE = re.compile(
    r"^\s*\[\s*(\d{1,2}:\d{1,2}(?::\d{1,2})?(?:[.,]\d{1,3})?)\s*"
    r"(?:-|–|—|-->|→)\s*"
    r"(\d{1,2}:\d{1,2}(?::\d{1,2})?(?:[.,]\d{1,3})?)\s*\]\s*(.*)$")

# A speaker label sits at the start of a line and is followed by a colon.
# Leading bullets and markdown bold are tolerated because scripts arrive pasted
# from Docs and WhatsApp. The colon may be ASCII or the fullwidth form.
_LABEL_RE = re.compile(
    r"^\s*(?:[-*>•]\s+)?(?:\*\*)?\s*([^:：]{1,40}?)\s*(?:\*\*)?\s*[:：]\s*(.*)$")

# Punctuation that means we are looking at a sentence containing a colon, not a
# speaker label. A full stop is deliberately NOT in here so "Dr. Rao:" works.
_NOT_IN_LABEL = set("।?!\"“”[]{}")


def normalize_speaker(name: str) -> str:
    """Canonical key for a speaker name — used to match a script label against a
    cast entry. Matching is case- and whitespace-insensitive so "SADHGURU",
    "Sadhguru" and "sadhguru " are one person."""
    return re.sub(r"\s+", " ", str(name or "").strip()).casefold()


def _word_looks_like_a_name(word: str) -> bool:
    """Could this word be part of someone's name?

    Names are capitalised ("Rao"), all-caps ("SADHGURU"), numbered ("Guest 2"),
    parenthesised stage directions ("(smiling)"), or written in a script with no
    letter case at all (Devanagari, Tamil, …). A plain lowercase word is prose.
    """
    if word.startswith("("):
        return True                      # stage direction — "(smiling)"
    w = word.strip("()[]{}.,;'\"-–—")
    if not w or w.isdigit():
        return True
    if w == w.upper() and w != w.lower():
        return True                      # ALL CAPS
    if not any(ch.islower() or ch.isupper() for ch in w):
        return True                      # caseless script — Devanagari, Tamil…
    return w[:1].isupper()


def _looks_like_label(candidate: str, known_keys: set) -> bool:
    """Is this text before a colon a speaker label rather than prose?

    Deliberately conservative: a false positive silently splits one person's
    line into two turns, which is much harder to spot in a finished render than
    a missed label is in the script. Without the name test, a continuation line
    reading "It is this: who are you?" would be read as a speaker called
    "It is this".

    A label already accepted earlier in the script is always accepted again, so
    a speaker written "SADHGURU:" once and "Sadhguru:" later stays one person.
    """
    c = candidate.strip()
    if not c or len(c) > 40:
        return False
    if normalize_speaker(c) in known_keys:
        return True
    words = c.split()
    if not words or len(words) > 4:
        return False
    if any(ch in _NOT_IN_LABEL for ch in c):
        return False
    if not any(ch.isalpha() for ch in c):
        return False
    return all(_word_looks_like_a_name(w) for w in words)


def _parse_timestamp(ts: str) -> Optional[int]:
    """"MM:SS.mmm" or "HH:MM:SS.mmm" → milliseconds. None if unparseable."""
    try:
        head, _, frac = ts.replace(",", ".").partition(".")
        parts = [int(p) for p in head.split(":")]
        if len(parts) == 2:
            hours, minutes, seconds = 0, parts[0], parts[1]
        elif len(parts) == 3:
            hours, minutes, seconds = parts
        else:
            return None
        ms = (hours * 3600 + minutes * 60 + seconds) * 1000
        if frac:
            ms += int(frac.ljust(3, "0")[:3])
        return ms
    except (ValueError, TypeError):
        return None


@dataclass
class Turn:
    """One speaker saying one thing. Renders to exactly one audio clip."""
    index: int
    speaker: str
    text: str
    start_ms: Optional[int] = None      # set by the audio-in adapter only
    end_ms: Optional[int] = None

    @property
    def key(self) -> str:
        return normalize_speaker(self.speaker)

    def preview(self, width: int = 48) -> str:
        """Short one-line form for progress rows and error messages."""
        flat = re.sub(r"\s+", " ", self.text).strip()
        return flat if len(flat) <= width else flat[:width - 1] + "…"


class ScriptFormatError(ValueError):
    """The script cannot be read as a dialogue. Message names the line."""


def parse_script(text: str, default_speaker: Optional[str] = None) -> List[Turn]:
    """
    Parse a speaker-labelled script into Turns, in script order.

    Lines without a label continue the current turn, so a speaker's paragraph
    can wrap across as many lines as it likes.

    *default_speaker* names who says any text appearing before the first label.
    Without it, such text raises rather than being silently attributed to
    whoever happens to speak first.
    """
    if not text or not text.strip():
        raise ScriptFormatError("The script is empty.")

    turns: List[Turn] = []
    buf: List[str] = []
    speaker: Optional[str] = None
    pending_start: Optional[int] = None
    pending_end: Optional[int] = None
    known_keys: set = set()
    if default_speaker:
        known_keys.add(normalize_speaker(default_speaker))

    def _flush():
        nonlocal buf, speaker, pending_start, pending_end
        if speaker is None:
            buf = []
            return
        body = "\n".join(buf).strip()
        buf = []
        if not body:
            # A label with nothing under it is a stray line, not a silent turn.
            speaker, pending_start, pending_end = None, None, None
            return
        body = re.sub(r"\n{3,}", "\n\n", body)
        turns.append(Turn(index=len(turns), speaker=speaker, text=body,
                          start_ms=pending_start, end_ms=pending_end))
        speaker, pending_start, pending_end = None, None, None

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if not line.strip():
            if speaker is not None:
                buf.append("")          # paragraph break inside a turn
            continue

        start_ms = end_ms = None
        tm = _TIME_RE.match(line)
        if tm:
            start_ms = _parse_timestamp(tm.group(1))
            end_ms   = _parse_timestamp(tm.group(2))
            line     = tm.group(3)
            if not line.strip():
                continue

        m = _LABEL_RE.match(line)
        if m and _looks_like_label(m.group(1), known_keys):
            _flush()
            speaker = re.sub(r"\s+", " ", m.group(1).strip())
            known_keys.add(normalize_speaker(speaker))
            pending_start, pending_end = start_ms, end_ms
            rest = m.group(2).strip()
            if rest:
                buf.append(rest)
            continue

        if speaker is None:
            if default_speaker:
                speaker = default_speaker
                pending_start, pending_end = start_ms, end_ms
            else:
                raise ScriptFormatError(
                    f"Line {lineno} has no speaker: {line.strip()[:60]!r}\n"
                    "Every line must start with a speaker label, like:\n"
                    "    SADHGURU: जीवन एक अवसर है।\n"
                    "    INTERVIEWER: How does one begin?")
        buf.append(line.strip())

    _flush()

    if not turns:
        raise ScriptFormatError(
            "No speaker turns found. Write the script as `NAME: text`, one "
            "speaker per line.")
    return turns


def split_label(line: str, known_keys: Optional[set] = None) -> tuple:
    """
    Split one line into (speaker, text). Speaker is "" when there is no label.

    The same detection parse_script() uses, exposed so Dub Sync can read speaker
    labels off a line at a time without going through turn-merging — it needs one
    unit per detected pause, not one per turn. Keeping both callers on this
    function is the point: a label form that works in the Dialogue tab and not in
    Dub Sync would be a maddening thing to debug.

    *known_keys* carries speakers already seen, which is what lets a label be
    recognised on its second appearance even where the name alone looks like
    prose. Callers that parse a whole script should thread one set through.
    """
    line = (line or "").strip()
    if not line:
        return "", ""
    known_keys = known_keys if known_keys is not None else set()

    tm = _TIME_RE.match(line)
    if tm:
        line = tm.group(3).strip()

    m = _LABEL_RE.match(line)
    if m and _looks_like_label(m.group(1), known_keys):
        speaker = re.sub(r"\s+", " ", m.group(1).strip())
        known_keys.add(normalize_speaker(speaker))
        return speaker, m.group(2).strip()
    return "", line


def speakers_in(turns: List[Turn]) -> List[str]:
    """Distinct speakers in order of first appearance, in their first-seen
    spelling — that is the spelling the cast table shows."""
    seen, out = set(), []
    for t in turns:
        if t.key not in seen:
            seen.add(t.key)
            out.append(t.speaker)
    return out


def merge_consecutive(turns: List[Turn]) -> List[Turn]:
    """
    Join back-to-back turns by the same speaker into one.

    Two renders of the same person separated by nothing sound like two takes
    spliced together, and cost two API calls. Turns carrying timestamps are
    left alone — merging them would throw away the timing the audio-in adapter
    went to the trouble of extracting.
    """
    out: List[Turn] = []
    for t in turns:
        prev = out[-1] if out else None
        if (prev is not None and prev.key == t.key
                and prev.start_ms is None and t.start_ms is None):
            prev.text = f"{prev.text}\n{t.text}".strip()
            continue
        out.append(Turn(index=len(out), speaker=t.speaker, text=t.text,
                        start_ms=t.start_ms, end_ms=t.end_ms))
    return out


def format_script(turns: List[Turn]) -> str:
    """Render Turns back to `NAME: text` form.

    Round-trips through parse_script, which is what lets the emotion pass send
    the whole conversation to the LLM in one call and read the tagged result
    back as turns.
    """
    blocks = []
    for t in turns:
        body = t.text.replace("\n", "\n    ")
        blocks.append(f"{t.speaker}: {body}")
    return "\n\n".join(blocks)
