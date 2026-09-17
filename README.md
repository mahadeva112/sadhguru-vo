# Sadhguru VO

![Python](https://img.shields.io/badge/python-3.13%2B-3776AB?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/platform-Windows-0078D4?logo=windows&logoColor=white)
![TTS](https://img.shields.io/badge/TTS-ElevenLabs-black)
![GUI](https://img.shields.io/badge/GUI-Tkinter-FFD43B)

Standalone ElevenLabs voice-over app, with three tabs.

**①② Sadhguru VO** — the two-step pipeline, one speaker:

```
Step 1   script          →  ElevenLabs TTS               (Sadhguru Hindi Fast)
Step 2   step-1 audio    →  ElevenLabs speech-to-speech  (step-2 voice)
```

The intermediate audio is handed straight from Step 1 to Step 2 — nothing has to
be exported and re-imported by hand.

**⇄ Dialogue** — many speakers:

```
script  →  turns  →  per-turn render (each speaker's own recipe)  →  assemble
```

Each speaker gets their own voice **and their own number of steps**, so a cloned
voice runs the full two steps while a stock voice runs one. Output is a mixed
master, per-speaker stems and a timecoded manifest. See
[Dialogue](#dialogue--multiple-speakers).

**⏱ Dub Sync** — dubbing an existing recording:

```
source audio  →  pause map  →  chunks  →  estimate  →  (approve)  →  render
```

The pauses in the source recording become the chunk boundaries; both scripts are
split across them; and a local estimate says whether each translation will fit
its slot **before anything is generated**. Label the script and it dubs a
conversation: each speaker renders through their own cast entry, so Sadhguru
runs the full two steps while an interviewer runs one. See
[Dub Sync](#dub-sync--dubbing-an-existing-recording).

Lifted out of the `bulk-video-processing` project's "Sadhguru VO" tab into a
fully independent app: its own virtual environment, its own config files, its own
API keys, and **zero imports from the original project** (which is untouched).

---

## Install

```bash
setup_windows.bat
```

That creates `.venv`, installs `requirements.txt`, checks for `ffmpeg`, and
creates the pinnable `Sadhguru VO.lnk` shortcut.

Then give the app its voices — these are studio-specific, so the repo ships
only examples:

```bash
copy voices.example.json voices.local.json
copy cast.example.json cast.json
```

`voices.local.json` holds the two pinned voice IDs the "↺" reset buttons snap
back to; `cast.json` holds the Dialogue tab's per-speaker recipes. Both are
gitignored. Without them the app still runs — you just pick every voice from
the dropdown instead of starting from a pinned default.

You also need **ffmpeg** on `PATH`. pydub uses it to join multi-chunk
single-speaker renders, and the Dialogue tab needs it regardless — laying turns on
a timeline means decoding them.

```bash
winget install Gyan.FFmpeg
```

---

## Launch

| What | How |
|---|---|
| GUI | `Sadhguru VO.bat`, or double-click `Sadhguru VO.lnk` |
| GUI, console attached (for tracebacks) | `"Sadhguru VO.bat" --debug` |
| CLI | `.venv\Scripts\python.exe cli.py --help` |

### Pin to the taskbar

Right-click **`Sadhguru VO.lnk`** → *Show more options* → **Pin to taskbar**.
(On Windows 11 you can also just drag the `.lnk` onto the taskbar. A copy was
placed on your Desktop too.)

Windows will not pin a `.bat` directly, which is why the `.lnk` exists. It targets
`.venv\Scripts\pythonw.exe` so no console window ever appears, and it carries
`assets\sadhguru_vo.ico` as its icon. The app also sets an explicit Windows
AppUserModelID, so the taskbar button keeps that icon instead of being grouped
under a generic Python entry.

If you move or rename the folder, re-run `make_shortcut.ps1` — the shortcut
stores absolute paths.

---

## CLI

Voices, models and the API key all default to whatever the GUI last saved, so a
normal run needs only `--script` and `--out`:

```bash
python cli.py --script script.txt --out "D:\out\Sadhguru_VO.mp3"
```

```bash
# inline text, with the emotion-tag pass
python cli.py --text "जीवन एक अवसर है।" --out out.mp3 --emotion

# Step 1 only (TTS), or Step 2 only (voice change on existing audio)
python cli.py --mode tts --script script.txt --out out.mp3
python cli.py --mode vc  --vc-input step1.mp3 --out out.mp3

# read the script from stdin
type script.txt | python cli.py --script - --out out.mp3

# what voices does this account have?
python cli.py --list-voices
python cli.py --list-voices --refresh          # bypass the cache

# override and persist new defaults
python cli.py --script s.txt --out o.mp3 \
    --step1-voice <id> --step2-voice <id> --save-prefs
```

`--quiet` suppresses progress and prints only the final path, which makes the CLI
easy to call from another script. Exit codes: `0` ok, `1` runtime/API failure,
`2` bad arguments, `130` interrupted.

### Dialogue on the command line

```bash
# who is in this script, and is the cast ready? (no API key needed)
python cli.py --dialogue --script dialogue.txt --speakers
```

```bash
# write a starter cast.json to fill in (no API key needed)
python cli.py --dialogue --script dialogue.txt --init-cast

# render it
python cli.py --dialogue --script dialogue.txt --out "D:\out\Dialogue.wav"

# a different cast file, and no stems
python cli.py --dialogue --script dialogue.txt --out out.wav \
    --cast project_cast.json --no-stems

# wider gaps, and the emotion pass
python cli.py --dialogue --script dialogue.txt --out out.wav \
    --gap-same 400 --gap-switch 900 --emotion
```

`--speakers` and `--init-cast` only read the script, so neither needs a
validated API key. `--speakers` exits `2` when the cast still has gaps, which
makes it usable as a pre-flight check in a batch script.

---

## Dialogue — multiple speakers

The two-step pipeline cannot render a conversation as it stands, and the reason
is worth knowing: **speech-to-speech is a whole-file operation with a single
target voice.** Push a four-person dialogue through Step 2 and all four people
come back speaking in the same voice. The chunker makes it worse — it splits on
character count, so it will happily put the end of one speaker's line and the
start of another's into the same request and render them in one breath.

So the Dialogue tab changes the unit of work from *the script* to *the turn*.
Each turn is rendered on its own, with its speaker's own recipe, and the turns
are then laid back down on a timeline.

### Script format

Plain text, one speaker label per line:

```
SADHGURU: जीवन एक अवसर है। इसे व्यर्थ मत गँवाइए।
INTERVIEWER: But Sadhguru, how does one actually begin?
SADHGURU: [contemplative] You begin by sitting still.
```

- A line with no label continues the previous speaker's turn.
- Labels are matched case-insensitively, so `SADHGURU:` and `Sadhguru:` are one
  person.
- Names in any script work — `सद्गुरु:` is a valid label.
- A line like `It is this: who are you?` is **not** read as a speaker called
  "It is this". The parser requires a label to look like a name, precisely
  because a false positive silently splits one line into two turns and that is
  far harder to spot in a finished render than a missing label is in the script.

### The cast

`cast.json` holds one recipe per speaker. The field that matters most is `mode`:

| `mode` | What runs | Use for |
|---|---|---|
| `both` | TTS → speech-to-speech | a speaker with a cloned target voice |
| `tts` | TTS only | a speaker using a stock voice as-is |

Because it is per speaker, a cast can mix the two freely — which is what makes a
Sadhguru-plus-interviewer conversation cost roughly half of what treating
everybody as two-step would.

```json
{
  "SADHGURU": {
    "mode": "both",
    "step1_voice": "YOUR_TTS_VOICE_ID",
    "step2_voice": "YOUR_STS_VOICE_ID",
    "style": "reflective, unhurried, contemplative"
  },
  "INTERVIEWER": {
    "mode": "tts",
    "step1_voice": "YOUR_INTERVIEWER_VOICE_ID",
    "stability": 0.6,
    "style_exaggeration": 0.15,
    "style": "curious, warm, brisk"
  }
}
```

A speaker whose name contains "sadhguru" is given the pinned two-step recipe
automatically. Everyone else starts with **no voice chosen** — deliberately, so
the check below forces a real decision rather than quietly rendering an
interviewer in Sadhguru's voice.

`style` is never sent to ElevenLabs. It goes to the emotion pass, so an
interviewer's question gets tagged as curious instead of contemplative.

### Nothing is rendered until the whole cast is valid

Every check that can run without an API call runs before the first one, and all
problems are reported together. A sixty-turn render that dies on turn forty-one
because one speaker had no voice has already burnt forty turns of quota.

### Output

For `--out D:\out\Dialogue.wav`:

| Path | What |
|---|---|
| `Dialogue.wav` | the mixed master |
| `stems/SADHGURU.wav`, `stems/INTERVIEWER.wav` | full-length per-speaker tracks, silent elsewhere, sample-aligned to the master |
| `Dialogue_manifest.csv` | every turn with in/out timecode, duration and text |
| `Dialogue_turns/` | the individual turn clips, and each 2-step speaker's pre-conversion `_tts.wav` |

The stems and the manifest are the point. One mixed file means every change to
balance or timing comes back to this app; stems plus timecodes mean the edit
happens in Premiere or Resolve. The stems sum back to the master exactly, so
dropping them all at 00:00 reproduces it.

`Dialogue_turns/` is kept rather than cleaned up — it is what lets you re-render
one changed line instead of the whole conversation, and it is the first place to
listen when a single turn comes out wrong.

### Things it does on your behalf

- **Back-to-back turns by one speaker are merged** before rendering. Two clips
  spliced together sound like two takes; one clip sounds like one thought, and
  costs one API call instead of two.
- **Short turns are padded before Step 2.** Speech-to-speech needs material to
  work with — a one-second "हाँ।" comes back with audible artefacts. Such turns
  are padded with silence, converted, then trimmed back by detecting where the
  speech actually is. STS is only *approximately* length-preserving, so cutting
  back a fixed number of milliseconds would eventually clip a word. Every time
  this fires it is reported as a note.
- **Every turn's edges are faded to zero**, which is what stops the click at the
  end of each turn. ElevenLabs TTS hands back audio that ends the instant the
  last phoneme does, with the waveform still well away from zero — on the test
  render one turn ended at amplitude `0.0146`. Laid straight onto a silent
  timeline that is a single-sample step down to zero, and a step is heard as a
  click. A ~12 ms ramp removes it (measured: worst boundary discontinuity
  `0.00876` → `0.00009`) and is inaudible on speech. The ramp lengthens
  automatically when a clip is cut off at a high level, since a loud truncation
  needs longer to stay inaudible. Speech-to-speech output does not have this
  problem, which is why it was only audible after one-step speakers.
- **Silence already on a clip is trimmed** before the gap is applied, so the
  configured gap is the *whole* gap. Without it a turn's spacing is the gap plus
  whatever trailing silence that particular render happened to include, and the
  pacing drifts from turn to turn.
- **Speakers are loudness-matched** to a common level, measured across all of a
  speaker's speech at once rather than per clip (per clip would flatten the
  dynamics Step 1 was tuned to produce). Capped at ±9 dB — beyond that the
  problem is the render, not the gain.
- **The gap after a speaker change is longer** than the gap within one speaker's
  run. That difference is most of what makes the result sound like turn-taking
  rather than one continuous read.

### Emotion tags in a dialogue

The pass runs **once for the whole conversation**, not once per turn. It is far
cheaper, and more importantly the model sees the surrounding turns, so it can
tag a reply as a reply. Each speaker's `style` note steers their own turns.

The response is checked structurally: same number of turns, same speakers, same
order. Any mismatch and the untagged script is used instead. A model that
quietly drops or reorders a turn would otherwise ship a conversation that says
something different from the one that was approved.

### Known limits

- **No overlapping speech.** Turn-taking only. The assembler already places
  clips at absolute positions and handles overlap, so the groundwork is there —
  what is missing is a way to say when someone interrupts.
- **Turns render cold.** Each turn is its own TTS request, so prosody does not
  carry across a turn boundary the way it does inside one.
- **Rendering is serial.** A sixty-turn dialogue with two two-step speakers is
  around 180 API calls, one after another.

---

## Dub Sync — dubbing an existing recording

Re-voicing a recording into another language, keeping the original's rhythm.
Everything up to the Generate button runs locally and costs nothing.

### What it does

1. **Pause detection** on the source audio. The silence threshold is measured
   from the recording (noise floor and speech level, read off percentiles of the
   frame levels) rather than fixed — a fixed floor set for a studio read makes a
   quiet phone recording come back as one unbroken segment.
2. **Chunks** — each detected run of speech with its start, end, and the pause
   that follows it. That trailing pause is the load-bearing number: the dub
   reproduces the source's rhythm by reproducing its pauses.
3. **Script split** across those chunks. If a script has exactly as many
   non-empty lines as there are segments they are used one to one, which is
   exact; otherwise the split is proportional to each chunk's speech duration
   and snapped to the nearest sentence end, comma, or word break.
4. **Estimate** — per chunk, how long the translation will take to say, from a
   syllable count and a per-language speaking rate. Shown as a band, not a
   single number.
5. **Timeline** — source and dub on one time axis, pause gaps visible, coloured
   by verdict, with the running drift.
6. **Generate**, only after you have looked at all of the above.

### Multiple speakers

Write the source script with speaker labels — the same `NAME: text` form the
Dialogue tab uses — and Dub Sync dubs a conversation:

```
SADHGURU: The mind is not the problem.
INTERVIEWER: Then what is?
SADHGURU: You have taken it to be yourself.
```

A cast table appears, sharing `cast.json` with the Dialogue tab: one cast, two
places to see it, and an edit in either shows up in both. Each speaker gets
their own voice **and their own number of steps**, so **Sadhguru renders
two-step** — TTS in the Sadhguru voice, then speech-to-speech onto the target
voice — while an interviewer renders one-step. That is the same `render_turn()`
the Dialogue tab calls, including its short-turn padding guard, which matters
more here: dubbing produces short chunks constantly, one per pause.

Three things follow from knowing who is speaking:

- **Speaking rate is measured per speaker.** A deliberate speaker and a brisk
  interviewer have genuinely different tempos, and one blended rate is wrong for
  both — it makes the slow speaker look like they will overrun and the fast one
  look like they will fall short, which is backwards.
- **No chunk straddles a speaker change.** When the script has more turns than
  lines to spare, segments are allocated to each speaker's run *first* and the
  text split within the run. A speaker change is exactly where a real pause is,
  so a chunk spanning one would be the single place the dub is guaranteed wrong.
- **The cast is validated before the first call.** A run that dies on chunk
  forty because one speaker had no voice has already spent thirty-nine chunks.

The confirmation dialog counts **API calls**, not chunks — a two-step speaker
costs two calls per chunk.

An unlabelled script behaves exactly as it did before: no cast table, one voice,
one step.

### Sync modes

| Mode | What it does |
|---|---|
| **Elastic — preserve pauses** *(default)* | Every pause reproduced at its source length. Chunks run their natural length; nothing is time-stretched. The dub drifts against the source, and the preview shows by how much. |
| **Hard lock** | Each chunk pinned to its source timestamp. Overrun is bought back by stretching within a transparent range, then by eating into the following pause down to a floor. Zero cumulative drift. |

Switching between them re-runs the preview instantly and costs nothing, so both
can be compared before committing.

### Verdicts

| | |
|---|---|
| **FITS** | lands inside its slot |
| **SHORT** | finishes early — padded with silence |
| **TIGHT** | overruns, absorbed by the pause or a small stretch |
| **OVER** | runs past its slot (elastic mode — this is drift) |
| **REWRITE** | too long to fit without audible stretching — shorten the translation |
| **EMPTY** | no target text; renders as silence |

### Why sync does not come from the speed parameter

ElevenLabs' `speed` is a hint: nothing guarantees the returned audio is any
particular length, and `eleven_v3` — this app's default model — does not accept
it at all. A timeline built on requested speed is built on a number nobody
promised.

So the render measures instead. Each chunk is generated, trimmed, and
**measured**, and only then fitted: clip is 2,310 ms, slot is 2,000 ms, therefore
1.155×, applied with ffmpeg's `atempo`, which changes duration without moving
pitch. `atempo` is transparent on speech to roughly ±15%; past that it is
audible, so past that the render refuses to stretch and lets the chunk run long
instead. Those chunks were flagged **REWRITE** in the preview, where fixing them
costs nothing.

### The estimate calibrates itself

Two things stop the first preview being a pure guess:

- The source audio and the source script together are a free, exact measurement
  of how fast **this** speaker talks. Speaker tempo is the largest single error
  in a default rate, and measuring it removes it. That tempo transfers to the
  target language.
- Every completed render feeds its real per-chunk durations back into
  `dub_rates.json`, blended with what is already stored. The confidence band
  narrows as samples accumulate.

### Corrections

Detection gets the boundaries close; you get them right.

- **⇊** in the table merges a chunk with the next one — for when a sentence was
  split at a breath.
- **Right-click a SRC block** on the timeline splits it there — for when two
  sentences ran together under the pause threshold.
- **Editing any translation** re-estimates and re-draws immediately.

### Output

`<name>.mp3` plus `<name>_manifest.csv`, which carries **both** timelines side by
side — where the source said it, where the dub says it, the drift, the speaker,
how many steps that chunk ran, the speed applied and the verdict. The question
asked of a dub in an edit suite is always "how far has this slipped by here",
and one set of timecodes cannot answer it.

With more than one speaker the mix also gets a per-speaker loudness match,
measured across all of each speaker's chunks at once. Two voices from two
ElevenLabs models routinely land several dB apart, and in a dub that difference
lands on the same words every time. A single-speaker dub is left alone —
normalising one voice would only move the dub's level away from the source's.

### Known limits

- **The script must be pasted.** There is no ASR, so nothing checks that the
  script actually matches the audio. If the implied speaking rate comes out
  implausible the preview says so, but it cannot align for you.
- **Chunk boundaries are text-proportional, not acoustic.** One line per pause
  in the pasted script turns the guess into a certainty.
- **Speakers come from the script's labels, not from the audio.** There is no
  diarization, so who is speaking is whatever the script says. If detection
  finds fewer pauses than the script has turns, speakers cannot be assigned
  reliably and the preview says so rather than guessing.
- **Rendering is serial**, one call per non-empty chunk, two for a two-step
  speaker.

---

## Configuration

All config lives as plain files next to the launcher, so it is easy to see and
edit. None of it is shared with the original project.

| File | What |
|---|---|
| `api.txt` | ElevenLabs API key. Written automatically whenever a pasted key validates. |
| `voices.local.json` | The two pinned default voice IDs + display names. Copy from `voices.example.json`. |
| `sadhguru_vo_prefs.json` | The two voice IDs and the two model IDs. Saved on every change. |
| `cast.json` | Dialogue tab: one voice recipe per speaker. Saved on every change; override with `--cast FILE`. Copy from `cast.example.json`. |
| `fav_voices.json` | Starred voices — pinned to the top of both dropdowns with a ★. |
| `llm_settings.json` | Provider + credentials for the optional emotion pass. |
| `prompts/Step4_Emotion_Prompt_<Language>.txt` | The emotion-pass prompt, one per language. |
| `error_log.txt` | Startup failures only. Created on demand. |

### The emotion pass (optional)

Ticking **Emotion tags** (or passing `--emotion`) runs the script through an LLM
first, which injects ElevenLabs v3 inline audio tags (`[hindi accent]`, `[calm]`,
`[contemplative]`, `[slow]`, `[pause]`) so the delivery is reflective rather than
flat. Words and punctuation are preserved verbatim — only tags are added.

Three providers, selected by `provider` in `llm_settings.json`:

- `OpenAI-compatible (Base URL)` — any `/v1/chat/completions` endpoint. Needs no
  extra packages; this is the configured default.
- `Gemini API key` — needs `google-genai`.
- `Vertex AI (JSON file)` — needs `google-genai` plus a service-account JSON
  (`vertex_json`, or `vertex_key.json` next to the app).

The pass is **best-effort**: any failure (missing prompt, bad credentials,
network error, empty response) logs a note and falls back to the original text,
so Step 1 is never blocked.

Only `eleven_v3` understands the inline tags. On any other TTS model the tags are
stripped before sending, so they are never read aloud.

---

## Output files

For `--out D:\x\Sadhguru_VO.mp3` a two-step run writes:

| File | What |
|---|---|
| `Sadhguru_VO.mp3` | The final voice-changed audio. |
| `Sadhguru_VO_step1_sadhguru.mp3` | The Step-1 TTS audio (kept by default; untick *Keep Step-1 audio* or pass `--no-keep-step1` to delete it). |
| `Sadhguru_VO_step1_sadhguru_chunk_NN.mp3` | One file per API chunk (`.wav` on the Dialogue tab's PCM path). |
| `Sadhguru_VO_step1_sadhguru_chunks.txt` | Manifest: chunk text, sizes, voice and model used. |

Pass `--no-chunk-files` to skip the per-chunk files and the manifest.

### File format, and why the chunk joins tick

**The single-speaker tab is unchanged, on purpose.** Its Step-1 file is named
`.mp3` and contains WAV bytes (carried over from the original app), its final file
is a real MP3, and its chunks are still joined by decoding MP3 through pydub — with
the seam artefacts described below. That tab's output is pinned to what it has
always produced, so nothing here applies to it.

**The Dialogue tab opts into PCM**, and gets seamless joins as a result. The
switch is a `formats=` argument on `synthesize_tts()` / `convert_voice()`
(`ELEVENLABS_TTS_FORMATS`, `ELEVENLABS_STS_FORMATS`); pass nothing and you get the
original request. `pipeline.run_pipeline()` passes nothing; `pipeline._render_turn()`
passes the ladders.

Why it matters for **Step 1**: MP3 is a block format. Every file carries encoder
priming samples at the front
and zero padding to fill its last 1152-sample frame, and its first and last
blocks decode wrong because the overlap-add has no neighbouring block to work
with. Butt two decoded MP3s together and every seam gets a few ms of dead air
plus a few ms of malformed waveform — audible as a tick or a "tape splice", and
**not** fixable with a fade, because the artefact sits inside the audio rather
than at the boundary. A streamed MP3 usually has no LAME/Xing gapless header
either, so ffmpeg cannot know how much priming to trim.

Raw PCM has no frames, no priming and no padding. Joining two chunks is a byte
append, the seam is absent rather than merely quiet, and nothing has to be
decoded — the WAV is written with the standard library, so the PCM path needs
neither pydub nor ffmpeg.

In practice a Dialogue turn is usually shorter than `ELEVENLABS_CHUNK_CHARS` and
so arrives in one chunk with no seam at all; this matters for the occasional long
turn. It is the single-speaker tab, splitting a whole script, that produces the
most seams — and that tab is deliberately left with them.

**Why `pcm_*` and not `wav_*`.** ElevenLabs offers both, and `wav_44100` would be
the obvious-looking choice since the output is a WAV anyway. It is the wrong one
for Step 1: each chunk would arrive as a complete file with its own 44-byte RIFF
header and its own (wrong) length field, so concatenating them would bury a
header inside the audio at every seam — a burst of noise where there used to be a
tick. Headerless is exactly the property that makes chunks concatenable. Asking
for WAV would not dodge the tier gate either: `wav_44100` needs Pro just as
`pcm_44100` does.

The docs don't actually state whether `pcm_*` is headerless — the two families
existing separately implies it, but `_strip_wav_header()` checks for a RIFF
header and unwraps it rather than trusting the inference, so both response shapes
produce identical audio and a `wav_*` format in the ladder also works.

In **Step 2** there is only ever one request per turn, so there was never a seam
to fix. The reason there is different: the chain already runs TTS into
speech-to-speech, and asking for MP3 added a third lossy generation on the way
out. On the Dialogue path it also cost an MP3 decode per turn, since the assembler
decodes every clip anyway to lay it on the timeline.

Three supporting details:

- **`pcm_44100` needs a paid ElevenLabs tier.** A rejection on format grounds
  steps down the ladder to `pcm_24000` (still gapless, and 12 kHz of bandwidth is
  plenty for speech), then to MP3 so a render never fails outright. The status
  line says when it steps down, and the chunk manifest records which format was
  used.
- **The outcome is remembered per API key** (`_FORMAT_MEMO`). A sixty-turn
  dialogue makes a request per turn, so without this a tier-limited account would
  re-probe the refused format sixty times — and for Step 2, be billed for each
  attempt.
- **Each Step-1 chunk is sent the text on either side of it** (`previous_text` /
  `next_text`), so intonation carries across a seam instead of every chunk
  closing on a sentence-final fall and reopening at full energy. These are
  context, not billed characters. A model that rejects the fields is retried
  without them.

Fall all the way down the ladder to MP3 — or pass no ladder at all, as the
single-speaker tab does — and Step 1 joins through pydub with the frame-edge
artefacts intact. If pydub or ffmpeg is also missing, the code concatenates the raw
MP3 bytes and says so in the status line. Step 2 on MP3 writes what the API
returned, verbatim.

---

## Layout

```
Sadhguru VO/
├── app.py                  GUI entry point
├── cli.py                  CLI entry point
├── Sadhguru VO.bat         launcher (--debug keeps the console)
├── Sadhguru VO.lnk         pinnable shortcut → pythonw.exe app.py
├── make_shortcut.ps1       (re)creates the shortcut
├── setup_windows.bat       creates .venv + installs deps + makes the shortcut
├── requirements.txt
├── sadhguru_vo/
│   ├── config.py           constants, paths, theme palette
│   ├── prefs.py            API key, voice/model choices, favourites
│   ├── elevenlabs_api.py   the ElevenLabs HTTP calls
│   ├── audio_backend.py    the only pydub import; keeps ffmpeg windowless
│   ├── llm.py              the optional emotion pass (3 providers)
│   ├── script_parser.py    speaker-labelled script → Turn[]
│   ├── cast.py             one voice recipe per speaker
│   ├── assembly.py         timeline, master, stems, manifest
│   ├── pause_map.py        source audio → timed segments + pauses
│   ├── dub_align.py        pause map + scripts → chunks (merge / split)
│   ├── dub_estimate.py     syllable counting, speaking rates, the preview
│   ├── dub_render.py       render → measure → fit → place
│   ├── pipeline.py         both runs — no UI in either
│   ├── gui.py              Tkinter window, three tabs
│   ├── gui_dub.py          the Dub Sync tab, mixed into the window
│   └── cli.py              argparse front-end
├── assets/
│   ├── sadhguru_vo.ico     taskbar / window icon
│   └── make_icon.py        regenerates the .ico (needs Pillow; build-time only)
└── prompts/
    └── Step4_Emotion_Prompt_<Language>.txt
```

GUI and CLI are both thin shells over `pipeline.run_pipeline()` and
`pipeline.run_dialogue()`, so they validate input identically and cannot drift
apart in behaviour. Both pipelines call the same ElevenLabs functions, so a fix
to chunking, error handling or retries lands in both.

`Turn` carries optional `start_ms` / `end_ms`, and the assembler pins a turn
there when they are set instead of computing a position. Nothing in the script
path sets them — they exist so an audio-in dubbing adapter (diarize →
transcribe → translate → timed `Turn[]`) can reuse the renderer and the
assembler unchanged, including overlapping speech.

Dub Sync is the first user of that timed path. It supplies the timings from
pause detection rather than diarization, so it needs no ASR and adds no heavy
dependencies — `pause_map.py` uses the pydub and ffmpeg that were already
required. The Dub Sync tab lives in its own module and is mixed into
`SadhguruVOApp`, so `gui.py` gains an import, a base class and a tab rather
than several hundred lines through the middle of the two tabs already in
production use.

Multi-speaker dubbing reuses rather than reimplements: `pipeline.render_turn()`
(aliased from `_render_turn`, which `run_dialogue` still calls by its old name)
renders one chunk through one `SpeakerRecipe`, so the two-step path, the
short-turn speech-to-speech guard and the cast validation are the same code in
both tabs. `_make_cast_row()` takes an optional parent frame — defaulted, so the
Dialogue tab is unchanged — and both tables edit the same `cast.json`.

---

## Dependencies

The ElevenLabs calls use only the standard library (`urllib`), so the dependency
list is short:

- **pydub** — cuts and joins audio. Needs ffmpeg on `PATH`.
- **audioop-lts** — pydub imports the stdlib `audioop` module, which was
  **removed in Python 3.13**. Without this, `import pydub` fails outright on
  3.13+. This machine runs Python 3.14.
- **google-genai** — only needed for the Vertex / Gemini emotion providers.

Tkinter ships with python.org builds. If it is missing, the app says so on
startup instead of failing with a stack trace.

## Notes

- **ffmpeg never opens a window.** pydub shells out to ffmpeg/ffprobe for every
  decode and export and passes no creation flags, so on Windows each call opens
  a console window for as long as it runs — under `pythonw.exe` that reads as a
  black window blinking over the app. A real four-turn dialogue makes 12 such
  calls (3 per turn), so a sixty-turn one would blink about 180 times while it
  cut and joined. `audio_backend.py` is the only module that imports pydub, and
  it forces `CREATE_NO_WINDOW` on before handing `AudioSegment` back. Both of
  pydub's call styles need covering — `pydub/utils.py` does
  `from subprocess import Popen` (a bound name) while `pydub/audio_segment.py`
  does `import subprocess` (an attribute lookup) — so one patches the name and
  the other patches the module reference. Best-effort: an unrecognised pydub
  layout means visible flashes, never a failed render.
- TLS certificate verification is **disabled** for outbound calls, carried over
  from the original app: the corporate networks this runs on terminate TLS with
  a root that isn't in Python's trust store, and the calls fail outright with
  verification on. Only `api.elevenlabs.io` and your configured LLM endpoint are
  contacted. See `_SSL_CTX` in `sadhguru_vo/config.py`.
- Voice lists are cached per (API key, language) for the session. Hit
  **↻ Reload Voices** (or `--refresh`) after adding a voice in ElevenLabs.
- `api.txt`, `llm_settings.json` and `vertex_key.json` hold credentials and are
  in `.gitignore`. Don't commit them.
- `voices.local.json`, `cast.json` and `fav_voices.json` name the voices a
  given studio uses. They're gitignored too — commit `voices.example.json` /
  `cast.example.json` instead when the schema changes.
