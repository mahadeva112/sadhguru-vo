"""
The VO pipelines, with no UI in them.

Single speaker — run_pipeline():

    Step 1   script          → ElevenLabs TTS            (Sadhguru Hindi voice)
    Step 2   step-1 audio    → ElevenLabs speech-to-speech (step-2 voice)

The intermediate audio is handed straight from Step 1 to Step 2 with no manual
export in between.

Multiple speakers — run_dialogue():

    script → turns → per-turn render (each speaker's own recipe) → assemble

The unit changes from "the script" to "the turn". That is the whole difference,
and it is a necessary one: speech-to-speech is a whole-file operation with a
single target voice, so a conversation pushed through it in one piece comes back
with everyone speaking in the same voice. Each turn is rendered on its own with
its speaker's recipe, then the turns are laid back down on a timeline.

Both entry points share the same ElevenLabs calls, so a fix to chunking, error
handling or retry behaviour lands in both.
"""

import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .assembly import (AssemblyError, RenderedTurn, assemble, export,
                       load_clip, safe_filename, silence, soften_edges,
                       trim_silence)
from .cast import SpeakerRecipe, cast_for_speakers, validate_cast
from .config import (DIALOGUE_EDGE_FADE_MS, DIALOGUE_GAP_SAME_MS,
                     DIALOGUE_GAP_SWITCH_MS, DIALOGUE_MIN_STS_MS,
                     DIALOGUE_STS_PAD_MS, DIALOGUE_TARGET_DBFS,
                     DIALOGUE_TRIM_TURN_SILENCE, ELEVENLABS_STITCH_CHARS,
                     ELEVENLABS_STS_FORMATS,
                     ELEVENLABS_STS_MODEL, ELEVENLABS_STS_MODELS,
                     ELEVENLABS_TTS_FORMATS, ELEVENLABS_TTS_MODEL,
                     ELEVENLABS_TTS_MODELS, GEMINI_DEFAULT_MODEL, MODE_BOTH,
                     MODE_TTS, MODE_VC, MODES, VO_LANGUAGE)
from .elevenlabs_api import convert_voice, synthesize_tts
from .llm import run_dialogue_emotion, run_emotion_enrichment
from .prefs import sanitize_voice_id
from .script_parser import (Turn, merge_consecutive, normalize_speaker,
                            parse_script, speakers_in)

# MODE_* moved to config.py so cast.py can name a speaker's mode without
# importing this module (which imports cast). Re-imported above rather than
# redefined, so `from .pipeline import MODE_BOTH` keeps working for the GUI and
# the CLI.


def step1_target(out_path: str) -> str:
    """Where Step-1 audio lands when it is generated automatically: next to the
    final output, with a `_step1_sadhguru` suffix."""
    stem, _ = os.path.splitext(out_path)
    return f"{stem}_step1_sadhguru.mp3"


@dataclass
class VoRequest:
    """Everything one pipeline run needs."""
    mode: str = MODE_BOTH
    script: str = ""
    out_path: str = ""
    api_key: str = ""
    step1_voice: str = ""
    step2_voice: str = ""
    step1_model: str = ELEVENLABS_TTS_MODEL
    step2_model: str = ELEVENLABS_STS_MODEL
    emotion: bool = False
    keep_step1: bool = True
    # Existing audio to voice-change in MODE_VC. Ignored in the other modes.
    vc_input: str = ""
    language: str = VO_LANGUAGE
    llm_model: str = GEMINI_DEFAULT_MODEL
    write_chunk_files: bool = True


@dataclass
class VoResult:
    final_path: str = ""
    step1_path: str = ""
    step1_removed: bool = False
    steps: dict = field(default_factory=dict)


def validate_request(req: VoRequest) -> VoRequest:
    """
    Normalize and check a request, returning a cleaned copy.

    Raises ValueError with a user-readable message for anything the pipeline
    cannot proceed with, so the GUI and the CLI report identical problems.
    """
    if req.mode not in MODES:
        raise ValueError(f"Unknown mode {req.mode!r} — expected one of {', '.join(MODES)}.")
    if not req.api_key or not req.api_key.strip():
        raise ValueError("ElevenLabs API key is missing.")
    if not req.out_path:
        raise ValueError("No output path given.")

    script = (req.script or "").strip()
    if req.mode in (MODE_BOTH, MODE_TTS) and not script:
        raise ValueError("The script is empty — nothing for Step 1 to speak.")

    # Messages here surface in both the GUI and the CLI, so they name the value
    # that's wrong rather than a dropdown or a flag.
    v1 = sanitize_voice_id(req.step1_voice)
    v2 = sanitize_voice_id(req.step2_voice)
    if req.mode in (MODE_BOTH, MODE_TTS) and not v1:
        raise ValueError(
            f"Step-1 TTS voice is not a valid ElevenLabs voice ID "
            f"(got {req.step1_voice!r}). It must be the raw 20-character ID, "
            "not a display label.")
    if req.mode in (MODE_BOTH, MODE_VC) and not v2:
        raise ValueError(
            f"Step-2 target voice is not a valid ElevenLabs voice ID "
            f"(got {req.step2_voice!r}). It must be the raw 20-character ID, "
            "not a display label.")

    m1 = req.step1_model if req.step1_model in ELEVENLABS_TTS_MODELS else ELEVENLABS_TTS_MODEL
    m2 = req.step2_model if req.step2_model in ELEVENLABS_STS_MODELS else ELEVENLABS_STS_MODEL

    vc_input = req.vc_input
    if req.mode == MODE_VC:
        if not vc_input:
            raise ValueError("Step-2-only run needs an existing Step-1 audio file.")
        if not os.path.isfile(vc_input):
            raise ValueError(f"Step-1 audio file not found: {vc_input}")

    return VoRequest(
        mode=req.mode, script=script, out_path=req.out_path,
        api_key=req.api_key.strip(), step1_voice=v1, step2_voice=v2,
        step1_model=m1, step2_model=m2, emotion=bool(req.emotion),
        keep_step1=bool(req.keep_step1), vc_input=vc_input,
        language=req.language or VO_LANGUAGE, llm_model=req.llm_model,
        write_chunk_files=bool(req.write_chunk_files),
    )


def run_pipeline(req: VoRequest,
                 step_cb: Optional[Callable[[str, str], None]] = None) -> VoResult:
    """
    Run the pipeline. *req* must already have been through validate_request().

    *step_cb* is called as step_cb(tag, message) where tag is "1" or "2", so the
    caller can drive a per-step progress display. It is invoked from whatever
    thread run_pipeline runs on — the GUI marshals back to the Tk thread itself.

    Raises ValueError / RuntimeError from the underlying API calls on failure.
    """
    result = VoResult()

    def _emit(tag: str, msg: str) -> None:
        result.steps[tag] = msg
        if step_cb:
            step_cb(tag, msg)

    s1_path = step1_target(req.out_path)
    result.step1_path = s1_path

    # ── Step 1 — TTS ─────────────────────────────────────────────────────────
    if req.mode in (MODE_BOTH, MODE_TTS):
        text = req.script
        if req.emotion:
            text = run_emotion_enrichment(
                req.script, language=req.language, model=req.llm_model,
                status_cb=lambda m: _emit("1", m))
        os.makedirs(os.path.dirname(os.path.abspath(s1_path)), exist_ok=True)
        # No `formats=` here, and that is deliberate: this tab's output is pinned
        # to what it has always produced — MP3 chunks joined through pydub, and a
        # real MP3 out of Step 2. The PCM path that makes chunk joins silent is
        # opted into by the Dialogue pipeline only. See _render_turn().
        synthesize_tts(text, s1_path, api_key=req.api_key,
                       voice_id=req.step1_voice, model_id=req.step1_model,
                       status_cb=lambda m: _emit("1", m),
                       write_chunk_files=req.write_chunk_files)
        _emit("1", f"✔ {os.path.basename(s1_path)}")
        step2_input = s1_path
    else:
        _emit("1", "— skipped (using existing audio)")
        step2_input = req.vc_input
        result.step1_path = req.vc_input

    # ── Step 2 — speech to speech ────────────────────────────────────────────
    if req.mode in (MODE_BOTH, MODE_VC):
        os.makedirs(os.path.dirname(os.path.abspath(req.out_path)), exist_ok=True)
        convert_voice(step2_input, req.out_path, api_key=req.api_key,
                      voice_id=req.step2_voice, model_id=req.step2_model,
                      status_cb=lambda m: _emit("2", m))
        _emit("2", f"✔ {os.path.basename(req.out_path)}")
        result.final_path = req.out_path
        # Drop the intermediate only when this run is the one that made it.
        if req.mode == MODE_BOTH and not req.keep_step1:
            try:
                os.remove(s1_path)
                result.step1_removed = True
                _emit("1", "✔ generated (intermediate removed)")
            except OSError:
                pass
    else:
        _emit("2", "— skipped")
        result.final_path = s1_path

    return result


# ═════════════════════════════════════════════════════════════════════════════
#  Multi-speaker — the dialogue pipeline
# ═════════════════════════════════════════════════════════════════════════════

def turns_dir(out_path: str) -> str:
    """Where the per-turn clips live: a `_turns` folder beside the master.

    Kept by default rather than cleaned up. They are what makes re-rendering one
    changed line possible instead of re-rendering the whole conversation, and
    they are the first thing to listen to when one turn comes out wrong.
    """
    stem, _ = os.path.splitext(out_path)
    return f"{stem}_turns"


@dataclass
class DialogueRequest:
    """Everything one multi-speaker run needs."""
    script: str = ""
    out_path: str = ""
    api_key: str = ""
    cast: Dict[str, SpeakerRecipe] = field(default_factory=dict)
    emotion: bool = False
    language: str = VO_LANGUAGE
    llm_model: str = GEMINI_DEFAULT_MODEL
    # Back-to-back turns by one speaker render as a single clip: it sounds like
    # one continuous thought instead of two takes, and costs one call not two.
    merge_same_speaker: bool = True
    default_speaker: str = ""
    # Assembly
    gap_same_ms: int = DIALOGUE_GAP_SAME_MS
    gap_switch_ms: int = DIALOGUE_GAP_SWITCH_MS
    match_loudness: bool = True
    target_dbfs: float = DIALOGUE_TARGET_DBFS
    write_stems: bool = True
    write_manifest: bool = True
    # Short-turn speech-to-speech guard
    min_sts_ms: int = DIALOGUE_MIN_STS_MS
    sts_pad_ms: int = DIALOGUE_STS_PAD_MS
    # Turn edges — what stops a click at the end of every turn. See
    # assembly.soften_edges().
    edge_fade_ms: int = DIALOGUE_EDGE_FADE_MS
    trim_turn_silence: bool = DIALOGUE_TRIM_TURN_SILENCE
    # Debug artefacts. Off by default here: a 60-turn dialogue with the
    # single-speaker setting would litter the folder with 120+ chunk files.
    write_chunk_files: bool = False


@dataclass
class DialogueResult:
    master_path: str = ""
    manifest_path: str = ""
    stem_paths: Dict[str, str] = field(default_factory=dict)
    turn_dir: str = ""
    turns: List[Turn] = field(default_factory=list)
    rendered: List[RenderedTurn] = field(default_factory=list)
    gains: Dict[str, float] = field(default_factory=dict)
    duration_ms: int = 0
    notes: List[str] = field(default_factory=list)


def prepare_dialogue(req: DialogueRequest) -> Tuple[DialogueRequest, List[Turn]]:
    """
    Parse the script, fill in the cast, and check the whole thing can render.

    Everything checkable without spending an API call is checked here, and every
    problem is reported at once. A sixty-turn render that dies on turn forty-one
    because one speaker had no voice has already burnt forty turns of quota — so
    the gate is before the first call, not during.
    """
    if not req.api_key or not req.api_key.strip():
        raise ValueError("ElevenLabs API key is missing.")
    if not req.out_path:
        raise ValueError("No output path given.")

    turns = parse_script(req.script, default_speaker=req.default_speaker or None)
    if req.merge_same_speaker:
        turns = merge_consecutive(turns)

    names = speakers_in(turns)
    cast = cast_for_speakers(names, req.cast)

    problems = validate_cast(cast, names)
    if problems:
        raise ValueError("This cast cannot render the script:\n  - "
                         + "\n  - ".join(problems))

    clean = DialogueRequest(**dict(req.__dict__))
    clean.api_key = req.api_key.strip()
    clean.cast = cast
    clean.language = req.language or VO_LANGUAGE
    return clean, turns


def speech_context(items: Sequence[Tuple[str, str]], index: int,
                   current_speaker: str, before: bool,
                   max_chars: int = ELEVENLABS_STITCH_CHARS) -> str:
    """
    The words either side of items[index], as prosodic context.

    *items* is the whole run as (text, speaker) pairs. Walks outward from
    *index* — backwards when *before*, forwards otherwise — collecting
    neighbouring text while it belongs to the same speaker, until *max_chars*
    is spent.

    It accumulates rather than taking a single neighbour, and on the dub path
    that is most of the value: a pause-split chunk is ~40 characters, so one
    neighbour spends a tenth of the budget and leaves the model with barely a
    clause of run-up. Several chunks of it approach the paragraph of context
    the single-speaker tab gets for free by sending 1000 characters at a time.

    Stops at a speaker change, and that boundary is deliberate: across it the
    reset is correct, and lending the model the other speaker's words invites
    it to finish their sentence instead of starting its own.
    """
    key = normalize_speaker(current_speaker)
    step = -1 if before else 1
    parts: List[str] = []
    budget = max_chars
    i = index + step
    while 0 <= i < len(items) and budget > 0:
        text, speaker = items[i]
        if normalize_speaker(speaker) != key:
            break
        text = (text or "").strip()
        if text:
            parts.append(text)
            budget -= len(text) + 1
        i += step
    if not parts:
        return ""
    if before:
        # Collected nearest-first; restore reading order, then keep the tail —
        # the words immediately before the seam are the ones that matter.
        return " ".join(reversed(parts))[-max_chars:]
    return " ".join(parts)[:max_chars]


def _render_turn(turn: Turn, recipe: SpeakerRecipe, req: DialogueRequest,
                 turn_dir: str, say: Callable[[str], None],
                 previous_text: str = "",
                 next_text: str = "") -> RenderedTurn:
    """Render one turn to one audio file, through that speaker's recipe.

    *previous_text* / *next_text* are the neighbouring turns' words. They are
    not spoken — they tell the model where this turn sits in the flow, which is
    what stops a mid-thought turn being delivered as a self-contained sentence.
    """
    stem = safe_filename(turn.speaker) or "speaker"
    base = os.path.join(turn_dir, f"{turn.index + 1:03d}_{stem}")
    tts_path = f"{base}_tts.wav"

    # PCM here, unlike the single-speaker tab: a turn long enough to be chunked
    # would otherwise tick at each seam, and every clip on this path gets decoded
    # by the assembler anyway, so an MP3 generation in between is pure loss.
    synthesize_tts(turn.text, tts_path, api_key=req.api_key,
                   voice_id=recipe.step1_voice, model_id=recipe.step1_model,
                   status_cb=say, write_chunk_files=req.write_chunk_files,
                   voice_settings=recipe.voice_settings(),
                   formats=ELEVENLABS_TTS_FORMATS,
                   previous_text=previous_text, next_text=next_text)

    if not recipe.two_step:
        return RenderedTurn(turn=turn, path=tts_path, mode=MODE_TTS)

    # ── Short-turn guard ─────────────────────────────────────────────────────
    # Speech-to-speech needs material to work with; a one-second reply comes
    # back with audible artefacts. Short turns are padded with silence,
    # converted, then trimmed back by detecting where the speech actually is —
    # STS is only approximately length-preserving, so cutting a fixed number of
    # milliseconds would eventually clip a word.
    clip = load_clip(tts_path)
    padded_path = ""
    note = ""
    sts_input = tts_path
    if len(clip) < req.min_sts_ms:
        pad = silence(req.sts_pad_ms)
        padded_path = f"{base}_padded.wav"
        # Fade the edges before butting silence against them, so the two joins
        # are not themselves discontinuities that speech-to-speech then has to
        # interpret — it renders such a step as an audible tick.
        export(pad + soften_edges(clip, req.edge_fade_ms) + pad, padded_path)
        sts_input = padded_path
        note = f"short turn ({len(clip)} ms) padded {req.sts_pad_ms} ms for STS"
        say(f"Voice Changer: padding a {len(clip)} ms turn before conversion...")

    out_path = f"{base}.wav"
    convert_voice(sts_input, out_path, api_key=req.api_key,
                  voice_id=recipe.step2_voice, model_id=recipe.step2_model,
                  status_cb=say, formats=ELEVENLABS_STS_FORMATS)

    if padded_path:
        export(trim_silence(load_clip(out_path)), out_path)
        try:
            os.remove(padded_path)
        except OSError:
            pass

    return RenderedTurn(turn=turn, path=out_path, mode=MODE_BOTH, note=note)


# Dub Sync renders one chunk at a time through the same recipe machinery, so it
# needs this by a public name. Aliased rather than renamed: run_dialogue() and
# every existing caller keep the name they already use.
render_turn = _render_turn


def run_dialogue(req: DialogueRequest,
                 turns: Optional[List[Turn]] = None,
                 status_cb: Optional[Callable[[str], None]] = None,
                 turn_cb: Optional[Callable[[int, int, Turn, str], None]] = None
                 ) -> DialogueResult:
    """
    Render a multi-speaker script and assemble it.

    *req* must already have been through prepare_dialogue(), which also returns
    the parsed *turns* — pass them back in rather than re-parsing, so what gets
    rendered is exactly what was validated.

    *status_cb* receives overall progress; *turn_cb* receives
    (position, total, turn, message) for a per-turn display.

    Raises ValueError / RuntimeError from the underlying API calls, and
    AssemblyError if the rendered turns cannot be laid out.
    """
    result = DialogueResult()

    def say(msg: str) -> None:
        if status_cb:
            status_cb(msg)

    if turns is None:
        req, turns = prepare_dialogue(req)
    result.turns = list(turns)

    # ── Emotion pass — one call for the whole conversation ───────────────────
    if req.emotion:
        turns = run_dialogue_emotion(turns, cast=req.cast, language=req.language,
                                     model=req.llm_model, status_cb=say)
        result.turns = list(turns)

    turn_dir = turns_dir(req.out_path)
    os.makedirs(turn_dir, exist_ok=True)
    result.turn_dir = turn_dir

    total = len(turns)
    rendered: List[RenderedTurn] = []
    context_pairs = [(t.text, t.speaker) for t in turns]

    for position, turn in enumerate(turns, 1):
        recipe = req.cast[turn.key]
        label = f"[{position}/{total}] {turn.speaker}"

        def _say(msg: str, _label=label, _pos=position, _turn=turn) -> None:
            say(f"{_label}  {msg}")
            if turn_cb:
                turn_cb(_pos, total, _turn, msg)

        _say(f"{recipe.mode} - {turn.preview()}")
        # What surrounds this turn, so its delivery is conditioned on the flow
        # rather than generated in isolation. Same-speaker only, and it stops at
        # a speaker change — see speech_context().
        rendered.append(_render_turn(
            turn, recipe, req, turn_dir, _say,
            previous_text=speech_context(context_pairs, position - 1,
                                         turn.speaker, before=True),
            next_text=speech_context(context_pairs, position - 1,
                                     turn.speaker, before=False)))
        _say("done")

    result.rendered = rendered

    # ── Assembly ─────────────────────────────────────────────────────────────
    extra_gain = {key: r.gain_db for key, r in req.cast.items() if r.gain_db}
    asm = assemble(rendered, req.out_path,
                   write_stems=req.write_stems,
                   write_manifest_file=req.write_manifest,
                   match_loudness=req.match_loudness,
                   gap_same_ms=req.gap_same_ms,
                   gap_switch_ms=req.gap_switch_ms,
                   target_dbfs=req.target_dbfs,
                   extra_gain_db=extra_gain,
                   trim_turn_silence=req.trim_turn_silence,
                   edge_fade_ms=req.edge_fade_ms,
                   status_cb=say)

    result.master_path   = asm.master_path
    result.manifest_path = asm.manifest_path
    result.stem_paths    = asm.stem_paths or {}
    result.gains         = asm.gains or {}
    result.duration_ms   = asm.duration_ms
    result.notes         = [f"turn {r.turn.index + 1}: {r.note}"
                            for r in rendered if r.note]

    say(f"Done -> {os.path.basename(asm.master_path)} "
        f"({asm.duration_ms / 1000.0:.1f}s, {total} turns)")
    return result
