"""
Generating the dub, once the preview has been approved.

The preview works from an estimate. This does not. Every decision here is made
from the duration of audio that has actually come back from ElevenLabs:

    render → trim → **measure** → fit → place

That ordering is the whole reason the dub stays in sync. A speed value sent up
with the request is a hint — the model is not obliged to return audio of any
particular length, and `eleven_v3` does not accept the parameter at all — so a
timeline built on requested speed is built on a number nobody guaranteed.
Measuring first turns it into arithmetic: the clip is 2,310 ms, the slot is
2,000 ms, therefore 1.155×. Nothing to trust.

Fitting is done with ffmpeg's `atempo`, which changes duration without moving
pitch. It is transparent on speech to roughly ±15%; past that it is audible, so
past that this refuses to stretch and lets the chunk run long instead. A chunk
that needs more was flagged REWRITE in the preview, where a shorter translation
costs nothing — silently mangling it at render time would spend credits to
produce something unusable.

Clips are trimmed before they are placed. ElevenLabs returns a variable amount
of silence on each end, and left on, that silence adds to the pause the source
recording actually had, so the rhythm the feature exists to reproduce drifts
chunk by chunk.
"""

import csv
import os
from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, Optional, Sequence

from .assembly import (AssemblyError, Placement, export, load_clip,
                       prepare_clip, silence, soften_edges, speaker_gains,
                       timecode, timed_clip)
from .audio_backend import run_ffmpeg
from .elevenlabs_api import convert_voice
from .cast import SpeakerRecipe
from .config import (CHUNKED_VOICE_SETTINGS, DIALOGUE_CHANNELS,
                     DIALOGUE_FRAME_RATE, DIALOGUE_MIN_STS_MS,
                     DIALOGUE_STS_PAD_MS, DIALOGUE_TARGET_DBFS,
                     DIALOGUE_TRIM_KEEP_MS, DIALOGUE_TRIM_THRESHOLD_DB,
                     DUB_DEFAULT_STS_MODE, DUB_KEEP_HEAD_MS, DUB_KEEP_TAIL_MS,
                     DUB_MAX_STRETCH, DUB_MIN_STRETCH,
                     DUB_PAUSE_FLOOR_MS, DUB_RELEASE_FADE_MS,
                     DUB_SPEECH_THRESHOLD_DB, DUB_STS_MAX_UPLOAD_MB,
                     DUB_STS_MIN_BLOCK_MS, DUB_STS_PER_CHUNK, DUB_STS_WHOLE,
                     DUB_TRIM_KEEP_MS, DUB_TRIM_THRESHOLD_DB,
                     ELEVENLABS_STS_FORMATS, ELEVENLABS_TTS_MODEL, MODE_BOTH,
                     MODE_TTS, SYNC_ELASTIC, SYNC_LOCK)
from .dub_align import Chunk
from .dub_estimate import (EMPTY, FIT, REWRITE, SHORT, TIGHT, speech_units,
                           update_rate_from_render)
from .pipeline import DialogueRequest, render_turn, speech_context
from .script_parser import Turn, normalize_speaker

# Headroom on the assembled dub, matching the dialogue master.
MASTER_CEILING_DBFS = -1.0
TAIL_PAD_MS = 400

# atempo handles 0.5–2.0 in one pass. The fitting range is far inside that, so a
# chunk never needs the filter chained — but the bound is asserted rather than
# assumed, because a chained atempo silently doubles the artefacts.
_ATEMPO_MIN, _ATEMPO_MAX = 0.5, 2.0


class DubRenderError(RuntimeError):
    """The dub could not be produced. Message is meant for the status bar."""


@dataclass
class RenderedChunk:
    """One chunk after it has actually been generated and measured."""
    index: int
    path: str = ""
    raw_ms: int = 0             # as ElevenLabs returned it
    trimmed_ms: int = 0         # speech only, margins excluded
    final_ms: int = 0           # speech only, after fitting
    # Margin kept around the speech so the phrase can open and release
    # naturally. Held separately from final_ms because the timeline is
    # scheduled from the speech and these two hang off either side of it.
    lead_ms: int = 0
    tail_ms: int = 0
    speed: float = 1.0          # atempo factor actually applied
    start_ms: int = 0           # position in the dub
    drift_ms: int = 0           # against the source segment's own start
    units: float = 0.0
    verdict: str = FIT
    note: str = ""
    speaker: str = ""
    mode: str = ""              # the recipe's mode: which steps this chunk ran

    @property
    def end_ms(self) -> int:
        return self.start_ms + self.final_ms


@dataclass
class DubResult:
    """Everything a completed render produced."""
    output_path: str = ""
    manifest_path: str = ""
    chunks: List[RenderedChunk] = field(default_factory=list)
    total_ms: int = 0
    source_total_ms: int = 0
    final_drift_ms: int = 0
    max_drift_ms: int = 0
    stretched: int = 0
    unfitted: int = 0           # chunks that hit the stretch limit and ran long
    skipped: int = 0            # chunks with no target text
    rate_updated: Optional[object] = None

    two_step: int = 0           # chunks that ran TTS + speech-to-speech
    speakers: List[str] = field(default_factory=list)
    planned: bool = False       # rendered from a script, not from a recording
    notes: List[str] = field(default_factory=list)

    # Whole-mix voice change, when that mode ran.
    sts_mode: str = DUB_STS_PER_CHUNK
    sts_blocks: int = 0         # passes it took (1 = the whole mix in one go)
    sts_shift_ms: int = 0       # worst block length change the converter made

    def summary(self) -> str:
        parts = [f"{len(self.chunks)} chunks",
                 f"{self.total_ms / 1000.0:.1f}s"]
        if len(self.speakers) > 1:
            parts.append(f"{len(self.speakers)} speakers")
        if self.sts_mode == DUB_STS_WHOLE and self.sts_blocks:
            parts.append("voice changed once over the mix"
                         if self.sts_blocks == 1
                         else f"voice changed in {self.sts_blocks} blocks")
        elif self.two_step:
            parts.append(f"{self.two_step} two-step")
        if self.stretched:
            parts.append(f"{self.stretched} fitted")
        if self.unfitted:
            parts.append(f"⚠ {self.unfitted} over limit")
        if self.skipped:
            parts.append(f"{self.skipped} empty")
        if not self.planned:
            parts.append(f"drift {self.final_drift_ms:+d} ms")
        return " · ".join(parts)


# ═════════════════════════════════════════════════════════════════════════════
#  Time fitting
# ═════════════════════════════════════════════════════════════════════════════

def time_fit(in_path: str, out_path: str, factor: float) -> bool:
    """
    Re-time *in_path* by *factor* without moving its pitch.

    factor > 1 plays faster (shorter). Returns False and leaves *out_path*
    alone if ffmpeg refused — callers fall back to the unfitted clip rather
    than failing the whole render over one chunk.
    """
    if not _ATEMPO_MIN <= factor <= _ATEMPO_MAX:
        raise DubRenderError(
            f"atempo factor {factor:.3f} is outside the single-pass range "
            f"[{_ATEMPO_MIN}, {_ATEMPO_MAX}] — this should have been clamped "
            "to the transparent fitting range before reaching here.")
    ok, err = run_ffmpeg(["-i", in_path,
                          "-filter:a", f"atempo={factor:.6f}",
                          "-ar", str(DIALOGUE_FRAME_RATE),
                          "-ac", str(DIALOGUE_CHANNELS),
                          out_path])
    return ok and os.path.isfile(out_path)


def fit_factor(measured_ms: int, target_ms: int) -> tuple:
    """
    The atempo factor that puts *measured_ms* into *target_ms*, and whether it
    had to be clamped.

    Returns (factor, clamped). A factor inside the tolerance band comes back as
    exactly 1.0 so the clip is passed through untouched — running atempo at
    1.002× is a decode and re-encode for no audible gain.
    """
    if measured_ms <= 0 or target_ms <= 0:
        return 1.0, False
    factor = measured_ms / float(target_ms)
    if DUB_MIN_STRETCH <= factor <= DUB_MAX_STRETCH:
        # Inside the transparent range. Skip trivial adjustments entirely.
        return (1.0, False) if 0.99 <= factor <= 1.01 else (factor, False)
    clamped = max(DUB_MIN_STRETCH, min(DUB_MAX_STRETCH, factor))
    return clamped, True


# ═════════════════════════════════════════════════════════════════════════════
#  Placement
# ═════════════════════════════════════════════════════════════════════════════

def _recipe_for(chunk: Chunk,
                cast: Optional[Dict[str, SpeakerRecipe]],
                fallback_voice: str,
                fallback_model: str,
                fallback_settings: Optional[dict]) -> SpeakerRecipe:
    """
    This chunk's recipe: the cast entry for its speaker, or the tab's single
    voice when the script carried no labels.

    The fallback is what makes the multi-speaker path a superset of the
    single-speaker one rather than a replacement for it — an unlabelled script
    still renders, through a one-step recipe built from the tab's own voice.
    """
    if chunk.speaker and cast:
        recipe = cast.get(chunk.key)
        if recipe is not None:
            return recipe
    # Chunked defaults — an unlabelled script still renders one call per chunk,
    # so it wants the same steadier setting a cast speaker gets, not the
    # single-speaker tab's.
    settings = fallback_settings or CHUNKED_VOICE_SETTINGS
    return SpeakerRecipe(
        speaker=chunk.speaker or "",
        mode=MODE_TTS,
        step1_voice=fallback_voice,
        step1_model=fallback_model,
        stability=settings.get("stability", CHUNKED_VOICE_SETTINGS["stability"]),
        similarity_boost=settings.get("similarity_boost",
                                      CHUNKED_VOICE_SETTINGS["similarity_boost"]),
        style_exaggeration=settings.get("style", CHUNKED_VOICE_SETTINGS["style"]),
        speaker_boost=settings.get("use_speaker_boost", True))


def _render_one(chunk: Chunk, recipe: SpeakerRecipe, api_key: str,
                out_path: str, min_sts_ms: int, sts_pad_ms: int,
                write_chunk_files: bool,
                previous_text: str = "", next_text: str = "") -> str:
    """
    Render one chunk through its recipe, and return the audio path.

    Delegates to pipeline.render_turn — the same code the Dialogue tab uses —
    so a two-step speaker gets TTS followed by speech-to-speech, including the
    short-turn padding guard that stops a one-second reply coming back full of
    artefacts. Dubbing produces short chunks constantly, one per pause, so that
    guard matters more here than it does on a script-in dialogue.

    *previous_text* / *next_text* matter more here than anywhere else in the
    app. A pause map cuts on silence, not on sentence ends, so most chunks are
    mid-sentence fragments — and a fragment rendered with no idea what precedes
    or follows it is spoken as a complete sentence, full stop and all. That is
    the difference between this tab and the single-speaker one, which sends the
    script in ~1000-character blocks and never has the problem.
    """
    turn = Turn(index=chunk.index, speaker=recipe.speaker or "speaker",
                text=chunk.target_text)
    req = DialogueRequest(api_key=api_key,
                          out_path=out_path,
                          min_sts_ms=min_sts_ms,
                          sts_pad_ms=sts_pad_ms,
                          write_chunk_files=write_chunk_files)
    rendered = render_turn(turn, recipe, req, os.path.dirname(out_path) or ".",
                           lambda _m: None,
                           previous_text=previous_text, next_text=next_text)
    return rendered.path


@dataclass
class WholeStsPlan:
    """Whether this dub can have its voice changed in one pass, and onto what."""
    ok: bool = False
    voice: str = ""
    model: str = ""
    reason: str = ""


def plan_whole_sts(chunks: Sequence[Chunk],
                   cast: Optional[Dict[str, SpeakerRecipe]],
                   fallback_voice: str,
                   fallback_model: str,
                   fallback_settings: Optional[dict] = None) -> WholeStsPlan:
    """
    Can the whole mix go through speech-to-speech in one pass?

    Only when every chunk that actually says something is two-step onto the
    *same* target voice and model. Both halves matter:

      - A one-step chunk in the mix would be converted too. That speaker was
        chosen precisely because their stock voice is right as-is, and a
        whole-file pass would overwrite it with somebody else's.
      - Two different target voices cannot both come out of one pass. The API
        converts a file onto one voice; the second speaker would simply be lost.

    Checked here, before a single credit is spent, so the answer is "this dub
    cannot use that mode, and here is why" rather than a bill for a render that
    came back in the wrong voice.
    """
    voice = model = ""
    saying = 0
    for chunk in chunks:
        if not chunk.target_text.strip():
            continue          # silence is converted by nobody
        saying += 1
        recipe = _recipe_for(chunk, cast, fallback_voice, fallback_model,
                             fallback_settings)
        if not recipe.two_step:
            who = chunk.speaker or "the target voice"
            return WholeStsPlan(
                reason=f"{who} is set to 1-step (TTS only). A single pass over "
                       f"the finished mix would change that voice too, so this "
                       f"dub converts per chunk instead.")
        if not voice:
            voice, model = recipe.step2_voice, recipe.step2_model
        elif (recipe.step2_voice, recipe.step2_model) != (voice, model):
            return WholeStsPlan(
                reason="This dub has more than one target voice. Speech-to-"
                       "speech converts a file onto a single voice, so one pass "
                       "would put every speaker in the same one — converting "
                       "per chunk instead.")
    if not saying:
        return WholeStsPlan(reason="Nothing to convert — every chunk is empty.")
    if not voice:
        return WholeStsPlan(reason="No target voice on the two-step recipe.")
    return WholeStsPlan(ok=True, voice=voice, model=model)


def _block_budget_ms(segment) -> int:
    """How many ms of *segment* fit in one upload, from its actual frame size."""
    bytes_per_ms = max(1, segment.frame_rate * segment.frame_width *
                       segment.channels // 1000)
    return max(DUB_STS_MIN_BLOCK_MS,
               int(DUB_STS_MAX_UPLOAD_MB * 1024 * 1024 // bytes_per_ms))


def sts_blocks(rendered: Sequence["RenderedChunk"],
               budget_ms: int) -> List[tuple]:
    """
    Group placed chunks into (start_ms, end_ms) blocks no longer than *budget_ms*.

    A block always ends on a chunk boundary, which on this timeline is inside a
    pause — so the cut lands in silence and the seam is inaudible. A single
    chunk longer than the budget still gets its own block rather than being cut
    mid-word; the upload limit is the API's problem to report, not a reason to
    slice a sentence.
    """
    spoken = [rc for rc in rendered if rc.final_ms > 0]
    if not spoken:
        return []
    blocks: List[tuple] = []
    start = spoken[0].start_ms
    end = spoken[0].end_ms
    for rc in spoken[1:]:
        if rc.end_ms - start > budget_ms:
            blocks.append((start, end))
            start, end = rc.start_ms, rc.end_ms
        else:
            end = max(end, rc.end_ms)
    blocks.append((start, end))
    return blocks


def convert_whole(master, rendered: Sequence["RenderedChunk"],
                  plan: WholeStsPlan, api_key: str, work_dir: str,
                  on_status: Optional[Callable[[str], None]] = None,
                  should_cancel: Optional[Callable[[], bool]] = None):
    """
    Voice-change the assembled mix, in as few passes as the upload limit allows.

    Returns (converted_master, block_count, max_block_delta_ms).

    Each converted block is laid back down at the timestamp it was cut from,
    not appended to the one before it. Speech-to-speech is only approximately
    length-preserving, and appending would let each block's small error
    accumulate into the next — twenty blocks off by 200 ms each is four seconds
    of drift by the end. Re-placing bounds the error inside the block it came
    from, so a dub cannot walk away from its own timeline.
    """
    budget = _block_budget_ms(master)
    blocks = sts_blocks(rendered, budget)
    if not blocks:
        return master, 0, 0

    converted: List[tuple] = []          # (start_ms, clip)
    worst = 0
    for i, (start, end) in enumerate(blocks, 1):
        if should_cancel and should_cancel():
            raise DubRenderError(
                f"Cancelled during the voice change, after {i - 1} of "
                f"{len(blocks)} block(s).")
        if on_status:
            on_status(f"Voice change: block {i} of {len(blocks)} "
                      f"({(end - start) / 1000.0:.0f}s)…")

        piece = master[start:end]
        src = os.path.join(work_dir, f"sts_block_{i:03d}_in.wav")
        dst = os.path.join(work_dir, f"sts_block_{i:03d}.wav")
        # Fade the block's own edges before upload. The cut lands in silence, so
        # this is cheap insurance rather than a fix — but a converter handed a
        # step discontinuity will happily render it as a tick.
        export(soften_edges(piece), src)
        try:
            convert_voice(src, dst, api_key=api_key, voice_id=plan.voice,
                          model_id=plan.model,
                          status_cb=(lambda m: on_status(f"  {m}"))
                          if on_status else None,
                          formats=ELEVENLABS_STS_FORMATS)
        except Exception as e:
            raise DubRenderError(
                f"The voice change failed on block {i} of {len(blocks)} "
                f"({timecode(start)}–{timecode(end)}):\n{e}") from None

        clip = soften_edges(load_clip(dst))
        worst = max(worst, abs(len(clip) - len(piece)))
        converted.append((start, clip))

    # Size the bed only once every block's real length is known. A block the
    # converter handed back longer than its slot must not be clipped by a
    # timeline that was measured before the conversion happened.
    total = max(len(master), max(s + len(c) for s, c in converted))
    out = silence(total)
    for start, clip in converted:
        out = out.overlay(clip, position=start)
    return out, len(blocks), worst


def _plan_slot(chunk: Chunk, mode: str) -> int:
    """
    How much room this chunk has before the next one is due.

    Hard lock only. The slot is the chunk's own speech plus whatever of the
    following pause can be given up while the pause still reads as a pause —
    below the floor a listener hears two sentences run together, which is a
    different kind of desync from the one being fixed.
    """
    if mode != SYNC_LOCK:
        return 0
    return chunk.duration_ms + max(0, chunk.pause_after_ms - DUB_PAUSE_FLOOR_MS)


# ═════════════════════════════════════════════════════════════════════════════
#  The render
# ═════════════════════════════════════════════════════════════════════════════

def render_dub(chunks: Sequence[Chunk],
               output_path: str,
               api_key: str,
               voice_id: str,
               target_language: str,
               model_id: str = ELEVENLABS_TTS_MODEL,
               mode: str = SYNC_ELASTIC,
               lead_in_ms: int = 0,
               source_total_ms: int = 0,
               voice_settings: Optional[dict] = None,
               cast: Optional[Dict[str, SpeakerRecipe]] = None,
               min_sts_ms: int = DIALOGUE_MIN_STS_MS,
               sts_pad_ms: int = DIALOGUE_STS_PAD_MS,
               sts_mode: str = DUB_DEFAULT_STS_MODE,
               match_loudness: bool = True,
               work_dir: Optional[str] = None,
               keep_chunks: bool = True,
               write_manifest_file: bool = True,
               learn_rate: bool = True,
               on_status: Optional[Callable[[str], None]] = None,
               on_chunk: Optional[Callable[[int, int, "RenderedChunk"], None]] = None,
               should_cancel: Optional[Callable[[], bool]] = None) -> DubResult:
    """
    Generate every chunk and lay them out on the source recording's rhythm.

    This is the only function in Dub Sync that spends credits. It is called
    after the preview has been looked at, and it renders exactly the chunks the
    preview described.

    With a *cast*, each chunk goes through its speaker's own recipe — which is
    how a two-step speaker gets TTS followed by speech-to-speech while a one-step
    speaker gets TTS alone. Without one, every chunk renders one-step through
    *voice_id*, so an unlabelled script behaves exactly as it did before speakers
    existed. A two-step chunk costs two API calls, not one.
    """
    if not chunks:
        raise DubRenderError("Nothing to render — the chunk list is empty.")
    if not api_key or not api_key.strip():
        raise DubRenderError("ElevenLabs API key is missing.")
    if not voice_id:
        raise DubRenderError("No target voice selected.")

    out_base = os.path.splitext(output_path)[0]
    work_dir = work_dir or (out_base + "_chunks")
    if keep_chunks:
        os.makedirs(work_dir, exist_ok=True)

    result = DubResult(output_path=output_path,
                       source_total_ms=source_total_ms)
    rendered: List[RenderedChunk] = []
    clips: Dict[int, object] = {}
    measurements: List[tuple] = []

    cursor = lead_in_ms          # where the dub has actually reached
    total = len(chunks)
    context_pairs = [(c.target_text, c.speaker) for c in chunks]

    # ── Where does the voice change happen? ──────────────────────────────────
    # Decided before the first credit is spent, because it changes what each
    # chunk call is: with one pass over the finished mix, every chunk renders
    # TTS-only and the conversion happens once at the end.
    whole_plan = WholeStsPlan()
    if sts_mode == DUB_STS_WHOLE:
        whole_plan = plan_whole_sts(chunks, cast, voice_id, model_id,
                                    voice_settings)
        if not whole_plan.ok:
            result.notes.append(
                f"Voice change ran per chunk — {whole_plan.reason}")
            if on_status:
                on_status(f"Voice change per chunk: {whole_plan.reason}")
    use_whole = whole_plan.ok

    # A planned timeline has no source timestamps, so hard lock has nothing to
    # pin against and the walk is the elastic one whatever the caller asked for.
    planned = not chunks[0].pinned
    if planned:
        mode = SYNC_ELASTIC

    # Trim settings follow the timing source, and the difference matters.
    #
    # A measured pause has to come back exactly, so a dub trims to zero margin.
    # A *configured* gap does not: the Dialogue path has always left 50 ms at
    # each edge, which means its real gap has always been the setting plus
    # 100 ms — and those gap values were tuned by ear with that included. Trim
    # a planned render to zero and every gap in it tightens by 100 ms against
    # what the same script has produced for months.
    #
    # So the planned path keeps the Dialogue numbers. Being consistent with the
    # output people already have beats being consistent with the other mode.
    trim_keep = DUB_TRIM_KEEP_MS if not planned else DIALOGUE_TRIM_KEEP_MS
    trim_thresh = (DUB_TRIM_THRESHOLD_DB if not planned
                   else DIALOGUE_TRIM_THRESHOLD_DB)

    # Margins are the same in both timing modes. They used to differ only
    # because kept silence lengthened the gap, and it no longer does — the
    # timeline is scheduled from speech_ms either way. A phrase releases the
    # same way whether its gap was measured or configured.
    keep_head = DUB_KEEP_HEAD_MS
    keep_tail = DUB_KEEP_TAIL_MS
    speech_thresh = DUB_SPEECH_THRESHOLD_DB
    release_fade = DUB_RELEASE_FADE_MS

    for n, chunk in enumerate(chunks, 1):
        if should_cancel and should_cancel():
            raise DubRenderError(f"Cancelled after {n - 1} of {total} chunks.")

        rc = RenderedChunk(index=chunk.index,
                           units=speech_units(chunk.target_text))

        if not chunk.target_text.strip():
            # Nothing to say. In elastic the source's own timing for this
            # segment is reproduced as silence so the following chunks keep
            # their spacing; in hard lock the next chunk is pinned anyway.
            rc.verdict = EMPTY
            rc.start_ms = chunk.start_ms if mode == SYNC_LOCK else cursor
            rc.note = "no target text — rendered as silence"
            result.skipped += 1
            rendered.append(rc)
            if mode == SYNC_ELASTIC:
                cursor += chunk.duration_ms + chunk.pause_after_ms
            if on_chunk:
                on_chunk(n, total, rc)
            continue

        recipe = _recipe_for(chunk, cast, voice_id, model_id, voice_settings)
        if use_whole:
            # Step 2 has been lifted out of the chunk loop. Rendering it here as
            # well would convert every chunk twice — once alone and once inside
            # the mix — which is both a doubled bill and a doubled generation.
            recipe = replace(recipe, mode=MODE_TTS)
        steps = "2-step" if recipe.two_step else "1-step"
        who = f"{chunk.speaker} · " if chunk.speaker else ""
        if on_status:
            on_status(f"Dub: chunk {n} of {total} — {who}{steps} — generating…")

        raw_path = os.path.join(work_dir if keep_chunks else os.path.dirname(out_base),
                                f"dub_{chunk.index:04d}.wav")
        # The chunks either side, as context only — as many as the character
        # budget allows, since one pause-split chunk is far too little run-up.
        # Same-speaker only; across a speaker change the fresh start is correct.
        # See pipeline.speech_context.
        try:
            raw_path = _render_one(
                chunk, recipe, api_key, raw_path,
                min_sts_ms=min_sts_ms, sts_pad_ms=sts_pad_ms,
                write_chunk_files=False,
                previous_text=speech_context(context_pairs, n - 1,
                                             chunk.speaker, before=True),
                next_text=speech_context(context_pairs, n - 1,
                                         chunk.speaker, before=False))
        except Exception as e:
            raise DubRenderError(
                f"Chunk {chunk.index + 1} of {total}"
                + (f" ({chunk.speaker})" if chunk.speaker else "")
                + f" failed to generate:\n{e}\n"
                f"Text: {chunk.target_text[:80]}") from None
        rc.speaker = chunk.speaker
        rc.mode = recipe.mode

        # Measure where the speech is, and keep a margin around it. The margin
        # is what stops every chunk ending like a splice — a phrase releases
        # over ~100 ms and cutting that off is audible on every sentence — and
        # measuring is what stops the margin costing anything: the timeline
        # below is driven by `speech_ms`, so the kept release rings on into a
        # pause that was silent anyway rather than lengthening it.
        raw = load_clip(raw_path)
        tc = timed_clip(raw,
                        keep_head_ms=keep_head, keep_tail_ms=keep_tail,
                        threshold_db=speech_thresh, fade_ms=release_fade)
        clip = tc.audio
        rc.path = raw_path
        rc.raw_ms = len(raw)
        rc.lead_ms = tc.lead_ms
        rc.tail_ms = tc.tail_ms
        rc.trimmed_ms = tc.speech_ms          # speech only — the timeline unit
        rc.final_ms = rc.trimmed_ms

        if mode == SYNC_LOCK:
            slot = _plan_slot(chunk, mode)
            # Only ever speed a chunk up. A clip shorter than its slot is left
            # alone and the remainder becomes silence: slowing speech down to
            # fill a gap is audible as dragging, and the next chunk starts on
            # its own timestamp either way, so the stretch buys nothing.
            factor, clamped = (fit_factor(rc.trimmed_ms, slot)
                               if rc.trimmed_ms > slot else (1.0, False))
            if factor != 1.0:
                # Stretch the *trimmed* clip, not the file as it arrived. The
                # factor was computed from the trimmed length, so feeding
                # atempo the padded original stretches silence that is about to
                # be thrown away and lands the fitted clip off its slot.
                trim_path = os.path.splitext(raw_path)[0] + "_trim.wav"
                fitted_path = os.path.splitext(raw_path)[0] + "_fit.wav"
                clip.export(trim_path, format="wav")
                if time_fit(trim_path, fitted_path, factor):
                    # Already trimmed — only the edges need softening again,
                    # since atempo can leave a non-zero sample at each end.
                    fitted = prepare_clip(load_clip(fitted_path), trim=False,
                                          fade_ms=release_fade)
                    clip = fitted
                    rc.path = fitted_path
                    rc.speed = factor
                    # atempo scaled the whole clip, margins included, so the
                    # lead and tail move with it. Rescale rather than re-detect:
                    # the ratio is exact and a second detection pass on stretched
                    # audio can land a millisecond or two off.
                    rc.lead_ms = int(round(rc.lead_ms / factor))
                    rc.tail_ms = int(round(rc.tail_ms / factor))
                    rc.final_ms = max(0, len(fitted) - rc.lead_ms - rc.tail_ms)
                    result.stretched += 1
                else:
                    rc.note = "time-fit failed; placed at natural length"
            if clamped:
                # Wanted more than the transparent range allows. The clip is
                # left as long as it is rather than mangled — the preview
                # already said this one needs a shorter translation.
                rc.verdict = REWRITE
                rc.note = (f"needs {rc.trimmed_ms / max(1, slot):.2f}× — past the "
                           f"{DUB_MAX_STRETCH:.2f}× limit; runs "
                           f"{rc.final_ms - slot:+d} ms long")
                result.unfitted += 1
            elif rc.speed != 1.0:
                rc.verdict = TIGHT
            elif rc.final_ms < chunk.duration_ms:
                rc.verdict = SHORT
            rc.start_ms = chunk.start_ms          # pinned, by definition
        else:
            rc.start_ms = cursor
            rc.verdict = FIT if abs(rc.final_ms - chunk.duration_ms) <= 150 else (
                SHORT if rc.final_ms < chunk.duration_ms else TIGHT)

        # Drift is distance from where the source put this line. A planned
        # chunk has no such position, so there is no drift to report — and
        # subtracting its placeholder start_ms of 0 would report the chunk's
        # own timestamp as if it were an error.
        rc.drift_ms = (rc.start_ms - chunk.start_ms) if chunk.pinned else 0
        clips[chunk.index] = clip
        measurements.append((rc.units, rc.final_ms))
        rendered.append(rc)

        if mode == SYNC_ELASTIC:
            # The pause is reproduced verbatim — that is the mode's promise.
            cursor = rc.start_ms + rc.final_ms + chunk.pause_after_ms

        if on_chunk:
            on_chunk(n, total, rc)

    if not clips:
        raise DubRenderError("No chunk produced any audio — every translation "
                             "was empty.")

    # ── Assemble ─────────────────────────────────────────────────────────────
    if on_status:
        on_status("Dub: assembling timeline…")

    # The bed has to cover the kept release too, not just the speech.
    total_ms = max((rc.end_ms + rc.tail_ms for rc in rendered if rc.final_ms),
                   default=0) + TAIL_PAD_MS
    master = silence(total_ms)

    # Per-speaker loudness match, measured across all of a speaker's chunks at
    # once. Two voices from two different ElevenLabs models routinely land
    # several dB apart, and in a dub that difference lands on the same words
    # every time the interviewer speaks. Skipped entirely for one speaker —
    # there is nothing to match against, and normalising a single voice would
    # move the dub's level away from the source's for no reason.
    gains: Dict[str, float] = {}
    if match_loudness and len({rc.speaker for rc in rendered if rc.final_ms}) > 1:
        placements = [
            Placement(turn=Turn(index=rc.index, speaker=rc.speaker, text=""),
                      path=rc.path, start_ms=rc.start_ms,
                      duration_ms=rc.final_ms)
            for rc in rendered if rc.index in clips
        ]
        gains = speaker_gains(placements,
                              {p.path: clips[p.turn.index] for p in placements},
                              target_dbfs=DIALOGUE_TARGET_DBFS,
                              extra_gain_db={r.key: r.gain_db
                                             for r in (cast or {}).values()})

    for rc in rendered:
        clip = clips.get(rc.index)
        if clip is None:
            continue
        gain = gains.get(normalize_speaker(rc.speaker), 0.0)
        if gain:
            clip = clip.apply_gain(gain)
        # start_ms is where the *speech* belongs, so the clip goes down that
        # much earlier — its kept lead-in is silence and lands in the previous
        # gap. Without this the margin would push every word late by its own
        # width, which is the drift the zero margin existed to prevent.
        master = master.overlay(clip, position=max(0, rc.start_ms - rc.lead_ms))

    # ── The voice change, once, over the finished mix ────────────────────────
    # This is the whole point of the mode: speech-to-speech carries a
    # performance across, and here it is handed minutes of continuous delivery
    # instead of a four-second fragment with nothing in it to carry.
    if use_whole:
        pre_ms = len(master)
        master, blocks_used, shift = convert_whole(
            master, rendered, whole_plan, api_key,
            work_dir if keep_chunks else (os.path.dirname(out_base) or "."),
            on_status=on_status, should_cancel=should_cancel)
        result.sts_mode = DUB_STS_WHOLE
        result.sts_blocks = blocks_used
        result.sts_shift_ms = shift
        result.two_step = sum(1 for rc in rendered if rc.final_ms)
        if shift:
            # Honest rather than silent: the converter is only approximately
            # length-preserving, and in hard lock that is a real deviation from
            # the timestamps the mode promises to hit.
            result.notes.append(
                f"The voice change altered block length by up to {shift} ms; "
                f"each block was re-placed at its own timestamp, so the error "
                f"does not accumulate.")
        if len(master) != pre_ms:
            result.notes.append(
                f"Mix length after the voice change: {len(master) - pre_ms:+d} ms.")

    if master.max_dBFS > MASTER_CEILING_DBFS:
        master = master.apply_gain(MASTER_CEILING_DBFS - master.max_dBFS)

    if on_status:
        on_status(f"Dub: writing {os.path.basename(output_path)}…")
    try:
        fmt = (os.path.splitext(output_path)[1].lstrip(".") or "wav").lower()
        master.export(output_path, format="mp3" if fmt == "mp3" else fmt)
    except Exception as e:
        raise AssemblyError(f"Could not write {output_path}: {e}") from None

    result.chunks = rendered
    if not use_whole:
        # In whole-mix mode every chunk rendered TTS-only and the conversion
        # happened once at the end, so a per-chunk count of MODE_BOTH would
        # report zero for a dub that was in fact voice-changed throughout.
        result.two_step = sum(1 for rc in rendered if rc.mode == MODE_BOTH)
    seen: Dict[str, str] = {}
    for rc in rendered:
        if rc.speaker and normalize_speaker(rc.speaker) not in seen:
            seen[normalize_speaker(rc.speaker)] = rc.speaker
    result.speakers = list(seen.values())
    result.total_ms = len(master)
    result.planned = planned
    result.final_drift_ms = (0 if planned or not source_total_ms
                             else result.total_ms - source_total_ms)
    result.max_drift_ms = max((abs(rc.drift_ms) for rc in rendered), default=0)

    if write_manifest_file:
        result.manifest_path = write_dub_manifest(out_base + "_manifest.csv",
                                                  chunks, rendered, mode)

    # The one unarguable measurement the estimator ever gets: real audio, real
    # durations. Folded back so the next preview for this language is closer.
    if learn_rate and measurements:
        result.rate_updated = update_rate_from_render(target_language, measurements)

    return result


# ═════════════════════════════════════════════════════════════════════════════
#  Manifest
# ═════════════════════════════════════════════════════════════════════════════

def write_dub_manifest(path: str,
                       chunks: Sequence[Chunk],
                       rendered: Sequence[RenderedChunk],
                       mode: str) -> str:
    """
    Write the per-chunk manifest.

    Carries both timelines side by side — where the source said it and where the
    dub says it — because the question asked of a dub in an edit suite is always
    "how far has this slipped by here", and answering it from a single set of
    timecodes means doing the subtraction by hand.

    utf-8-sig for the same reason as the dialogue manifest: without the BOM,
    Excel renders Devanagari as mojibake and the file is useless to the people
    most likely to open it.
    """
    by_index = {rc.index: rc for rc in rendered}
    try:
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["#", "speaker", "steps", "source_in", "source_out",
                        "pause_after_ms", "dub_in", "dub_out", "drift_ms",
                        "speed", "verdict", "note", "source_text", "target_text"])
            for c in chunks:
                rc = by_index.get(c.index)
                w.writerow([
                    c.index + 1,
                    c.speaker,
                    rc.mode if rc else "",
                    timecode(c.start_ms), timecode(c.end_ms), c.pause_after_ms,
                    timecode(rc.start_ms) if rc else "",
                    timecode(rc.end_ms) if rc else "",
                    rc.drift_ms if rc else "",
                    f"{rc.speed:.3f}" if rc else "",
                    rc.verdict if rc else "",
                    rc.note if rc else "",
                    c.source_text, c.target_text,
                ])
    except OSError as e:
        raise AssemblyError(f"Could not write the manifest: {e}") from None
    return path
