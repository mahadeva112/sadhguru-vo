"""
Command-line front-end over the same pipelines the GUI drives.

Single speaker:

    python cli.py --script script.txt --out out.mp3
    python cli.py --text "…" --out out.mp3 --emotion
    python cli.py --mode tts --script script.txt --out out.mp3
    python cli.py --mode vc  --vc-input step1.mp3 --out out.mp3

Multiple speakers:

    python cli.py --dialogue --script dialogue.txt --out Dialogue.wav
    python cli.py --dialogue --script dialogue.txt --speakers      # who's in it
    python cli.py --dialogue --script dialogue.txt --init-cast     # starter cast
"""

import argparse
import json
import os
import sys
from typing import List, Optional

from .assembly import AssemblyError, timecode
from .cast import (CAST_MODES, cast_for_speakers, read_cast, validate_cast,
                   write_cast)
from .config import (APP_NAME, APP_VERSION, CAST_FILE, DIALOGUE_GAP_SAME_MS,
                     DIALOGUE_GAP_SWITCH_MS, ELEVENLABS_STS_MODEL,
                     ELEVENLABS_STS_MODELS, ELEVENLABS_TTS_MODEL,
                     ELEVENLABS_TTS_MODELS, GEMINI_DEFAULT_MODEL,
                     STEP1_VOICE_NAME, STEP2_VOICE_NAME, VO_LANGUAGE)
from .elevenlabs_api import clear_voice_cache, fetch_voices, validate_api_key
from .llm import llm_provider_label
from .pipeline import (MODE_BOTH, MODE_TTS, MODE_VC, MODES, DialogueRequest,
                       VoRequest, prepare_dialogue, run_dialogue, run_pipeline,
                       step1_target, validate_request)
from .prefs import (get_api_key, read_prefs, sanitize_voice_id,
                    set_runtime_api_key, write_api_key_file, write_prefs)
from .script_parser import (ScriptFormatError, normalize_speaker, parse_script,
                            speakers_in)


def _configure_console() -> None:
    """Let stdout/stderr carry the ✔ ✦ → characters this CLI prints.

    A Windows console defaults to a legacy code page, and piping to another tool
    reports one too. Without this, a finished render dies with UnicodeEncodeError
    while printing its own success message — the work is done, but the exit code
    says it failed.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def _build_parser() -> argparse.ArgumentParser:
    prefs = read_prefs()
    p = argparse.ArgumentParser(
        prog="sadhguru-vo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=f"{APP_NAME} {APP_VERSION} — two-step ElevenLabs VO pipeline.\n"
                    f"  Step 1  script       → TTS            ({STEP1_VOICE_NAME})\n"
                    f"  Step 2  step-1 audio → speech-to-speech ({STEP2_VOICE_NAME})",
        epilog="Voice IDs, models and the API key default to whatever the GUI last "
               "saved, so most runs need only --script and --out.")

    src = p.add_mutually_exclusive_group()
    src.add_argument("--script", metavar="FILE",
                     help=f"path to a UTF-8 text file holding the {VO_LANGUAGE} script "
                          "(use - to read stdin)")
    src.add_argument("--text", metavar="STR", help="the script inline")

    p.add_argument("--out", "-o", metavar="FILE",
                   help="final output audio path (required unless --list-voices)")
    p.add_argument("--mode", choices=list(MODES), default=MODE_BOTH,
                   help=f"which steps to run (default: {MODE_BOTH})")
    p.add_argument("--vc-input", metavar="FILE",
                   help="existing audio to voice-change; only used with --mode vc "
                        "(defaults to the auto-named Step-1 file next to --out)")

    p.add_argument("--step1-voice", default=prefs["step1_voice"], metavar="ID",
                   help=f"Step-1 TTS voice id (default: {prefs['step1_voice']})")
    p.add_argument("--step2-voice", default=prefs["step2_voice"], metavar="ID",
                   help=f"Step-2 target voice id (default: {prefs['step2_voice']})")
    p.add_argument("--step1-model", default=prefs["step1_model"],
                   choices=list(ELEVENLABS_TTS_MODELS),
                   help=f"ElevenLabs TTS model (default: {prefs['step1_model']})")
    p.add_argument("--step2-model", default=prefs["step2_model"],
                   choices=list(ELEVENLABS_STS_MODELS),
                   help=f"ElevenLabs STS model (default: {prefs['step2_model']})")

    p.add_argument("--emotion", action="store_true",
                   help="run the LLM emotion-tag pass before Step 1 "
                        f"(v3 audio tags, {VO_LANGUAGE})")
    p.add_argument("--language", default=VO_LANGUAGE, metavar="NAME",
                   help=f"language for the emotion pass (default: {VO_LANGUAGE})")
    p.add_argument("--llm-model", default=GEMINI_DEFAULT_MODEL, metavar="NAME",
                   help="model for the emotion pass on the Gemini/Vertex providers")

    p.add_argument("--no-keep-step1", dest="keep_step1", action="store_false",
                   help="delete the Step-1 intermediate after a successful "
                        "two-step run (kept by default)")
    # Tri-state on purpose: left alone (None) the right answer differs by path.
    # One script produces a handful of debug files and they are worth having; a
    # sixty-turn dialogue would produce a hundred and twenty and bury the output.
    p.add_argument("--no-chunk-files", dest="write_chunk_files",
                   action="store_false", default=None,
                   help="skip the per-chunk MP3s and the _chunks.txt manifest")
    p.add_argument("--chunk-files", dest="write_chunk_files",
                   action="store_true", default=None,
                   help="write the per-chunk debug files "
                        "(default: on for a single script, off for --dialogue)")
    p.set_defaults(keep_step1=True)

    p.add_argument("--api-key", metavar="KEY",
                   help="ElevenLabs API key (default: api.txt next to the app)")
    p.add_argument("--save-api-key", action="store_true",
                   help="write --api-key to api.txt for future runs")
    p.add_argument("--save-prefs", action="store_true",
                   help="persist the given voices/models as the new defaults")

    # ── Multi-speaker ────────────────────────────────────────────────────────
    d = p.add_argument_group(
        "multi-speaker",
        "Render a script with speaker labels (`NAME: text`). Each speaker gets "
        "their own voice and their own number of steps, from the cast file.")
    d.add_argument("--dialogue", action="store_true",
                   help="treat the script as a multi-speaker dialogue")
    d.add_argument("--cast", metavar="FILE", default=CAST_FILE,
                   help=f"cast JSON: voices per speaker (default: {CAST_FILE})")
    d.add_argument("--speakers", action="store_true",
                   help="list the speakers in the script and exit")
    d.add_argument("--init-cast", action="store_true",
                   help="write a starter cast file for this script and exit")
    d.add_argument("--no-stems", dest="write_stems", action="store_false",
                   help="skip the per-speaker stem tracks")
    d.add_argument("--no-manifest", dest="write_manifest", action="store_false",
                   help="skip the timecoded CSV manifest")
    d.add_argument("--no-loudness", dest="match_loudness", action="store_false",
                   help="skip the per-speaker loudness match")
    d.add_argument("--no-merge", dest="merge_same_speaker", action="store_false",
                   help="render back-to-back turns by one speaker separately")
    d.add_argument("--gap-same", type=int, default=DIALOGUE_GAP_SAME_MS,
                   metavar="MS",
                   help=f"silence between turns by the same speaker "
                        f"(default: {DIALOGUE_GAP_SAME_MS})")
    d.add_argument("--gap-switch", type=int, default=DIALOGUE_GAP_SWITCH_MS,
                   metavar="MS",
                   help=f"silence after a speaker change "
                        f"(default: {DIALOGUE_GAP_SWITCH_MS})")
    p.set_defaults(write_stems=True, write_manifest=True, match_loudness=True,
                   merge_same_speaker=True)

    p.add_argument("--list-voices", action="store_true",
                   help="print every voice on the account and exit")
    p.add_argument("--refresh", action="store_true",
                   help="bypass the voice cache when listing")
    p.add_argument("--quiet", "-q", action="store_true",
                   help="only print the final output path")
    p.add_argument("--version", action="version",
                   version=f"{APP_NAME} {APP_VERSION}")
    return p


def _read_script(args) -> str:
    if args.text is not None:
        return args.text
    if not args.script:
        return ""
    if args.script == "-":
        return sys.stdin.read()
    if not os.path.isfile(args.script):
        raise ValueError(f"Script file not found: {args.script}")
    with open(args.script, "r", encoding="utf-8") as f:
        return f.read()


def _dialogue_main(args, parser, api_key: str, say) -> int:
    """The --dialogue path: parse, cast, render, assemble."""
    try:
        script = _read_script(args)
    except ValueError as e:
        parser.error(str(e))
        return 2
    if not script.strip():
        parser.error("--dialogue needs a script (--script FILE or --text STR).")

    try:
        turns_preview = parse_script(script)
    except ScriptFormatError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    names = speakers_in(turns_preview)

    # ── --speakers: who is in this script? ───────────────────────────────────
    if args.speakers:
        existing = read_cast(args.cast)
        cast = cast_for_speakers(names, existing)
        counts = {}
        for t in turns_preview:
            counts[t.key] = counts.get(t.key, 0) + 1
        say(f"{len(turns_preview)} turn(s), {len(names)} speaker(s) "
            f"in {args.script or '<inline text>'}:")
        for name in names:
            r = cast[normalize_speaker(name)]
            v1 = r.step1_voice or "(no voice chosen)"
            v2 = r.step2_voice or "—"
            print(f"  {name:<20} {counts.get(r.key, 0):>3} turn(s)  "
                  f"{r.mode:<5} v1={v1}  v2={v2}")
        problems = validate_cast(cast, names)
        if problems:
            say("")
            say("Not ready to render:")
            for p in problems:
                say(f"  - {p}")
            say(f"Edit {args.cast} (or use the Dialogue tab), then re-run.")
            return 2
        return 0

    # ── --init-cast: write a starter file to fill in ─────────────────────────
    if args.init_cast:
        cast = cast_for_speakers(names, read_cast(args.cast))
        write_cast(args.cast, cast)
        if not os.path.exists(args.cast):
            print(f"error: could not write {args.cast}", file=sys.stderr)
            return 1
        say(f"✔ Wrote cast for {len(names)} speaker(s) → {args.cast}")
        say("  Fill in each speaker's step1_voice (and step2_voice for 2-step "
            "speakers), then re-run without --init-cast.")
        print(args.cast)
        return 0

    if not args.out:
        parser.error("--out is required (the assembled master).")

    try:
        req, turns = prepare_dialogue(DialogueRequest(
            script=script, out_path=os.path.abspath(args.out), api_key=api_key,
            cast=read_cast(args.cast),
            emotion=bool(args.emotion),
            merge_same_speaker=bool(args.merge_same_speaker),
            match_loudness=bool(args.match_loudness),
            write_stems=bool(args.write_stems),
            write_manifest=bool(args.write_manifest),
            gap_same_ms=args.gap_same, gap_switch_ms=args.gap_switch,
            language=args.language, llm_model=args.llm_model,
            # Unset → off here, so a long dialogue doesn't bury its own output.
            write_chunk_files=bool(args.write_chunk_files)))
    except (ValueError, ScriptFormatError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    say(f"{APP_NAME} {APP_VERSION} — dialogue, {len(turns)} turn(s)")
    for name in speakers_in(turns):
        r = req.cast[normalize_speaker(name)]
        say(f"  {name:<20} {r.mode:<5} v1={r.step1_voice}"
            + (f"  v2={r.step2_voice}" if r.two_step else ""))
    if req.emotion:
        say(f"  Emotion pass via {llm_provider_label()} (one call, whole script)")

    try:
        result = run_dialogue(req, turns,
                              status_cb=(None if args.quiet
                                         else lambda m: print(f"  {m}", flush=True)))
    except KeyboardInterrupt:
        print("\naborted", file=sys.stderr)
        return 130
    except (ValueError, AssemblyError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    say("")
    say(f"  duration : {timecode(result.duration_ms)}")
    say(f"  turns    : {result.turn_dir}")
    for _key, path in sorted(result.stem_paths.items()):
        say(f"  stem     : {path}")
    if result.manifest_path:
        say(f"  manifest : {result.manifest_path}")
    for note in result.notes:
        say(f"  note     : {note}")
    say("✔ Done")
    print(result.master_path)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    _configure_console()
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.api_key:
        set_runtime_api_key(args.api_key)

    def say(msg: str) -> None:
        if not args.quiet:
            print(msg)

    # These two only read the script, so they must not demand an API key.
    if args.dialogue and (args.speakers or args.init_cast):
        return _dialogue_main(args, parser, "", say)

    try:
        api_key = get_api_key()
    except Exception as e:
        parser.error(str(e))
        return 2   # unreachable — parser.error exits

    if args.save_api_key:
        if not args.api_key:
            parser.error("--save-api-key needs --api-key.")
        validate_api_key(api_key)
        write_api_key_file(api_key)
        say("✔ API key validated and saved to api.txt")

    # ── --list-voices ────────────────────────────────────────────────────────
    if args.list_voices:
        try:
            if args.refresh:
                clear_voice_cache(language=args.language, api_key=api_key)
            voices = fetch_voices(api_key, args.language,
                                  force_refresh=bool(args.refresh))
        except Exception as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        say(f"{len(voices)} voice(s) on this account "
            f"(✦ = advertises {args.language} support):")
        for v in voices:
            print(f"  {v['voice_id']}  {v['label']}")
        return 0

    # ── --dialogue: the multi-speaker path ───────────────────────────────────
    if args.dialogue:
        return _dialogue_main(args, parser, api_key, say)

    # ── validate + run ───────────────────────────────────────────────────────
    if not args.out:
        parser.error("--out is required (or use --list-voices).")

    try:
        script = _read_script(args)
    except ValueError as e:
        parser.error(str(e))
        return 2

    vc_input = args.vc_input or ""
    if args.mode == MODE_VC and not vc_input:
        auto = step1_target(args.out)
        if os.path.isfile(auto):
            vc_input = auto
            say(f"Using existing Step-1 audio: {auto}")

    try:
        req = validate_request(VoRequest(
            mode=args.mode, script=script, out_path=os.path.abspath(args.out),
            api_key=api_key,
            step1_voice=args.step1_voice, step2_voice=args.step2_voice,
            step1_model=args.step1_model, step2_model=args.step2_model,
            emotion=bool(args.emotion), keep_step1=bool(args.keep_step1),
            vc_input=vc_input, language=args.language,
            llm_model=args.llm_model,
            # Unset → on here, matching how the single-script path always behaved.
            write_chunk_files=(True if args.write_chunk_files is None
                               else bool(args.write_chunk_files))))
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.save_prefs:
        prefs = read_prefs()
        prefs.update({"step1_voice": req.step1_voice,
                      "step2_voice": req.step2_voice,
                      "step1_model": req.step1_model,
                      "step2_model": req.step2_model})
        write_prefs(prefs)
        say("✔ Saved these voices/models as the new defaults")

    say(f"{APP_NAME} {APP_VERSION} — mode: {req.mode}")
    if req.mode in (MODE_BOTH, MODE_TTS):
        say(f"  Step 1  TTS  voice={req.step1_voice}  model={req.step1_model}")
    if req.mode in (MODE_BOTH, MODE_VC):
        say(f"  Step 2  STS  voice={req.step2_voice}  model={req.step2_model}")
    if req.emotion:
        say(f"  Emotion pass via {llm_provider_label()}")

    def _step_cb(tag: str, msg: str) -> None:
        if not args.quiet:
            print(f"  [S{tag}] {msg}", flush=True)

    try:
        result = run_pipeline(req, step_cb=_step_cb)
    except KeyboardInterrupt:
        print("\naborted", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if req.mode in (MODE_BOTH, MODE_TTS) and not result.step1_removed:
        say(f"  Step-1 audio : {result.step1_path}")
    say("✔ Done")
    print(result.final_path)
    return 0


if __name__ == "__main__":       # python -m sadhguru_vo.cli
    raise SystemExit(main())
