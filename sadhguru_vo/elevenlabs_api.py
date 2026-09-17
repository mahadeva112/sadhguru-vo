"""
ElevenLabs HTTP calls: key validation, voice catalogue, text-to-speech (Step 1)
and speech-to-speech (Step 2).

Uses urllib only — no SDK — so the app has no hard third-party dependency for
its core job.

Both steps take an optional `formats` ladder, and it is what decides which of two
behaviours runs. Pass nothing and the request is the original one: MP3, joined
through pydub in Step 1 and written verbatim in Step 2. Pass a ladder and the
request is PCM, which in Step 1 makes a multi-chunk render join without a tick
(chunks concatenate as bytes; see ELEVENLABS_TTS_FORMATS) and in Step 2 keeps a
lossy generation out of a chain that already runs TTS into speech-to-speech.

Only the Dialogue pipeline passes a ladder. The single-speaker tab does not, on
purpose — its output is pinned to what it has always produced.

On the PCM path neither step needs pydub or ffmpeg: the result is written with the
stdlib `wave` module. pydub is imported lazily and only for the MP3 join, where
decoding is unavoidable.

A ladder is walked if the account's tier refuses PCM, and the outcome is
remembered per key in _FORMAT_MEMO so a sixty-turn dialogue probes once rather
than sixty times.
"""

import json
import mimetypes
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import wave
from typing import Dict, List, Optional, Sequence

from .audio_backend import audio_segment
from .config import (DEFAULT_VOICE_SETTINGS, ELEVENLABS_CHUNK_CHARS,
                     ELEVENLABS_STITCH_CHARS, ELEVENLABS_STITCH_CONTEXT,
                     ELEVENLABS_STS_FALLBACKS, ELEVENLABS_STS_FORMATS,
                     ELEVENLABS_STS_MODEL, ELEVENLABS_STS_MODELS,
                     ELEVENLABS_TTS_FALLBACKS, ELEVENLABS_TTS_FORMATS,
                     ELEVENLABS_TTS_MODEL, ELEVENLABS_TTS_MODELS,
                     LANGUAGE_TOKENS, VO_LANGUAGE, _SSL_CTX)
from .prefs import api_key_fingerprint, sanitize_voice_id

# Voice catalogue cache keyed by (api-key fingerprint, language) so the list
# isn't re-fetched on every run. Cleared when the user hits "Reload Voices".
_EL_VOICE_CACHE: Dict[tuple, List[Dict[str, str]]] = {}

# Model catalogue cache keyed by api-key fingerprint. The value is
# {"tts": {model_id: name}, "sts": {model_id: name}}, or None when the fetch
# failed — a negative entry so a 200-chunk render probes once, not 200 times.
_EL_MODEL_CACHE: Dict[str, Optional[Dict[str, Dict[str, str]]]] = {}


def _lang_tokens(language: str) -> tuple:
    return LANGUAGE_TOKENS.get(language, ())


def strip_emotion_tags(text: str) -> str:
    """Strip ElevenLabs inline emotion/accent tags like [calm], [hindi accent],
    [pause] — including closers like [/fast]."""
    return re.sub(r'\[/?[\w\s]+\]', '', text).strip()


def _multipart_body(fields, files):
    boundary = "----ElevenLabsBoundary7MA4YWxkTrZu0gW"
    body = b""
    for name, value in fields:
        body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f"name=\"{name}\"\r\n\r\n{value}\r\n").encode()
    for name, filename, mime, data in files:
        body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f"name=\"{name}\"; filename=\"{filename}\"\r\n"
                 f"Content-Type: {mime}\r\n\r\n").encode()
        body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return body, boundary


# ═════════════════════════════════════════════════════════════════════════════
#  Key validation + voice catalogue
# ═════════════════════════════════════════════════════════════════════════════

def validate_api_key(api_key: str, timeout: float = 15.0) -> Dict[str, str]:
    """
    Verify an ElevenLabs key by hitting /v1/user.

    Returns {"ok": True, "tier": ..., "name": ...} on success; raises ValueError
    with a user-readable message on failure.
    """
    if not api_key or not api_key.strip():
        raise ValueError("API key is empty.")
    api_key = api_key.strip()
    req = urllib.request.Request(
        "https://api.elevenlabs.io/v1/user",
        method="GET",
        headers={"xi-api-key": api_key, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise ValueError("Invalid or expired ElevenLabs API key (401).") from None
        if e.code == 429:
            raise ValueError("ElevenLabs API rate limit hit (429). Try again shortly.") from None
        raise ValueError(f"ElevenLabs API error: HTTP {e.code}.") from None
    except urllib.error.URLError as e:
        raise ValueError(f"Network error reaching ElevenLabs: {e.reason}") from None
    except Exception as e:
        raise ValueError(f"Could not validate ElevenLabs key: {e}") from None

    sub = payload.get("subscription") or {}
    return {
        "ok": True,
        "tier": str(sub.get("tier", "")),
        "name": str(payload.get("first_name") or payload.get("xi_api_key") or "user"),
    }


def _voice_supports_language(voice: dict, lang_tokens: tuple) -> bool:
    """
    Heuristic: does this voice's metadata indicate support for the language
    identified by *lang_tokens*? Looks at labels.language / labels.languages /
    labels.accent, verified_languages (newer schema), the top-level language
    fields, and finally the name / description text.
    """
    if not isinstance(voice, dict):
        return False
    haystacks: List[str] = []

    labels = voice.get("labels") or {}
    if isinstance(labels, dict):
        for key in ("language", "languages", "accent"):
            v = labels.get(key)
            if isinstance(v, str):
                haystacks.append(v)
            elif isinstance(v, list):
                haystacks.extend(str(x) for x in v)

    verified = voice.get("verified_languages") or []
    if isinstance(verified, list):
        for entry in verified:
            if isinstance(entry, dict):
                for key in ("language", "code", "name", "locale"):
                    val = entry.get(key)
                    if isinstance(val, str):
                        haystacks.append(val)
            elif isinstance(entry, str):
                haystacks.append(entry)

    for key in ("language", "language_code", "locale", "name", "description"):
        v = voice.get(key)
        if isinstance(v, str):
            haystacks.append(v)

    blob = " ".join(haystacks).lower()
    if not blob:
        return False
    for token in lang_tokens:
        t = str(token).lower()
        # Word-boundary match for short codes, plain substring for full names.
        if len(t) <= 3:
            if re.search(rf"\b{re.escape(t)}\b", blob):
                return True
        elif t in blob:
            return True
    return False


def fetch_voices(api_key: str, language: str = VO_LANGUAGE,
                 force_refresh: bool = False,
                 timeout: float = 30.0) -> List[Dict[str, str]]:
    """
    Return EVERY voice on the account as [{"voice_id", "name", "label"}].

    Voices advertising support for *language* are sorted to the top and marked
    with a ✦ prefix, but the full list is returned — eleven_v3 auto-detects the
    language from the input text and works with any voice. Cached per
    (key fingerprint, language).
    """
    api_key = (api_key or "").strip()
    if not api_key:
        raise ValueError("API key is empty.")

    lang_tokens = _lang_tokens(language)
    cache_key   = (api_key_fingerprint(api_key), language)
    if not force_refresh and cache_key in _EL_VOICE_CACHE:
        return _EL_VOICE_CACHE[cache_key]

    req = urllib.request.Request(
        "https://api.elevenlabs.io/v1/voices",
        method="GET",
        headers={"xi-api-key": api_key, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise ValueError("Invalid or expired ElevenLabs API key (401).") from None
        if e.code == 429:
            raise ValueError("ElevenLabs API rate limit hit (429). Try again shortly.") from None
        raise ValueError(f"Could not fetch voices (HTTP {e.code}).") from None
    except urllib.error.URLError as e:
        raise ValueError(f"Network error fetching voices: {e.reason}") from None
    except Exception as e:
        raise ValueError(f"Could not fetch voices: {e}") from None

    voices = payload.get("voices") or []
    if not isinstance(voices, list):
        voices = []

    matched: List[Dict[str, str]] = []
    others:  List[Dict[str, str]] = []
    for v in voices:
        if not isinstance(v, dict):
            continue
        vid = v.get("voice_id") or v.get("voiceId") or ""
        if not vid:
            continue
        name     = v.get("name") or "Unnamed voice"
        labels   = v.get("labels") or {}
        category = str(v.get("category", "")).strip()
        accent   = ""
        if isinstance(labels, dict):
            accent = str(labels.get("accent") or labels.get("language") or "")

        meta_bits: List[str] = []
        if accent:
            meta_bits.append(accent)
        if category and category.lower() != "premade":
            meta_bits.append(category)
        meta = f"  [{' · '.join(meta_bits)}]" if meta_bits else ""

        is_match = _voice_supports_language(v, lang_tokens)
        entry = {
            "voice_id": vid,
            "name": str(name),
            "label": f"{'✦ ' if is_match else '  '}{name} — {vid[:8]}…{meta}",
        }
        (matched if is_match else others).append(entry)

    matched.sort(key=lambda e: e["name"].lower())
    others.sort(key=lambda e: e["name"].lower())
    all_voices = matched + others

    _EL_VOICE_CACHE[cache_key] = all_voices
    return all_voices


def clear_voice_cache(language: Optional[str] = None,
                      api_key: Optional[str] = None) -> None:
    """
    Clear the voice cache — everything, or just one language / key scope.

    The model catalogue for the same key scope goes with it: "Reload Voices" is
    the one gesture that means "re-ask this account what it has", and a key that
    was just upgraded (or swapped) must not keep answering from a stale list of
    models.
    """
    clear_model_cache(api_key)
    if language is None and api_key is None:
        _EL_VOICE_CACHE.clear()
        return
    fp = api_key_fingerprint(api_key) if api_key else None
    for key in list(_EL_VOICE_CACHE.keys()):
        key_fp, key_lang = key
        if (language is None or key_lang == language) and (fp is None or key_fp == fp):
            _EL_VOICE_CACHE.pop(key, None)


def voice_cache_get(api_key: str, language: str) -> Optional[List[Dict[str, str]]]:
    """Cached voice list for this key + language, or None if not cached yet."""
    return _EL_VOICE_CACHE.get((api_key_fingerprint(api_key), language))


# ═════════════════════════════════════════════════════════════════════════════
#  Model catalogue — what this key is actually allowed to call
# ═════════════════════════════════════════════════════════════════════════════

def fetch_models(api_key: str, force_refresh: bool = False,
                 timeout: float = 15.0) -> Optional[Dict[str, Dict[str, str]]]:
    """
    Ask /v1/models what this key can call, split by capability:

        {"tts": {model_id: display_name}, "sts": {model_id: display_name}}

    Returns None if the catalogue could not be fetched (no key, network down,
    401, malformed payload). None means "unknown", not "nothing available" —
    callers must fall back to the requested model rather than refuse to render.
    The result is cached per key, negative answers included.
    """
    if not api_key or not api_key.strip():
        return None
    fp = api_key_fingerprint(api_key)
    if not force_refresh and fp in _EL_MODEL_CACHE:
        return _EL_MODEL_CACHE[fp]

    req = urllib.request.Request(
        "https://api.elevenlabs.io/v1/models",
        method="GET",
        headers={"xi-api-key": api_key.strip(), "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        _EL_MODEL_CACHE[fp] = None
        return None

    if isinstance(payload, dict):
        payload = payload.get("models") or []
    if not isinstance(payload, list):
        _EL_MODEL_CACHE[fp] = None
        return None

    catalogue: Dict[str, Dict[str, str]] = {"tts": {}, "sts": {}}
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        mid = str(entry.get("model_id") or "").strip()
        if not mid:
            continue
        name = str(entry.get("name") or mid)
        if entry.get("can_do_text_to_speech"):
            catalogue["tts"][mid] = name
        if entry.get("can_do_voice_conversion"):
            catalogue["sts"][mid] = name

    # An empty catalogue is a shape we do not understand — treat it as unknown
    # so a render is never blocked by a parse that went sideways.
    if not catalogue["tts"] and not catalogue["sts"]:
        _EL_MODEL_CACHE[fp] = None
        return None

    _EL_MODEL_CACHE[fp] = catalogue
    return catalogue


def clear_model_cache(api_key: Optional[str] = None) -> None:
    """Forget the model catalogue — everything, or just one key's entry."""
    if api_key is None:
        _EL_MODEL_CACHE.clear()
        return
    _EL_MODEL_CACHE.pop(api_key_fingerprint(api_key), None)


def available_models(api_key: str, kind: str = "tts",
                     force_refresh: bool = False) -> Dict[str, str]:
    """
    Model ids this key can call for *kind* ("tts" or "sts"), mapped to the
    app's own labels where it has one and the API's name otherwise. Falls back
    to the hardcoded table when the catalogue is unknown.
    """
    labels = ELEVENLABS_TTS_MODELS if kind == "tts" else ELEVENLABS_STS_MODELS
    catalogue = fetch_models(api_key, force_refresh=force_refresh)
    if not catalogue:
        return dict(labels)
    return {mid: labels.get(mid, name)
            for mid, name in catalogue.get(kind, {}).items()}


def resolve_model(api_key: str, model_id: str, kind: str = "tts",
                  status_cb=None) -> str:
    """
    Return a model id this key is actually allowed to call for *kind*.

    *model_id* is returned untouched when the account has it, or when the
    catalogue could not be fetched (unknown ≠ invalid — a network blip must not
    silently change the model a render uses). Otherwise the first entry of the
    configured fallback chain that the account does have is substituted, and
    *status_cb* is told about the swap so it is visible in the log rather than
    surfacing later as an odd-sounding render.
    """
    default = ELEVENLABS_TTS_MODEL if kind == "tts" else ELEVENLABS_STS_MODEL
    model_id = (str(model_id or "").strip() or default)

    catalogue = fetch_models(api_key)
    if not catalogue:
        return model_id
    valid = catalogue.get(kind, {})
    if not valid or model_id in valid:
        return model_id

    chain = ELEVENLABS_TTS_FALLBACKS if kind == "tts" else ELEVENLABS_STS_FALLBACKS
    pick = next((m for m in chain if m in valid), None) or next(iter(valid))
    if status_cb:
        other = "sts" if kind == "tts" else "tts"
        job = ("text-to-speech" if kind == "tts" else "voice conversion")
        why = ("cannot do " + job if model_id in catalogue.get(other, {})
               else "is not available on this ElevenLabs account")
        status_cb(f"Model {model_id} {why} — using {pick} instead.")
    return pick


# ═════════════════════════════════════════════════════════════════════════════
#  Step 1 — text to speech
# ═════════════════════════════════════════════════════════════════════════════

def split_text_for_elevenlabs(text: str,
                              max_chars: int = ELEVENLABS_CHUNK_CHARS) -> list:
    """
    Split text into chunks of at most *max_chars* characters.

    Strategy, in priority order:
      1. Accumulate consecutive paragraphs into one chunk while the total stays
         under max_chars. Blank lines are ignored — they are NOT flush points,
         which stops several short paragraphs becoming several tiny API calls.
      2. A paragraph longer than max_chars is split at sentence boundaries.
      3. A sentence longer than max_chars is split at word boundaries.
    """
    if len(text) <= max_chars:
        return [text]

    def _fits(s: str) -> bool:
        return len(s) <= max_chars

    def _split_para(para: str) -> list:
        pieces, current = [], ""
        for sent in re.split(r'(?<=[.!?।])\s+', para):
            if not sent:
                continue
            candidate = (current + " " + sent).strip() if current else sent
            if _fits(candidate):
                current = candidate
            else:
                if current:
                    pieces.append(current)
                if _fits(sent):
                    current = sent
                else:
                    current = ""
                    for word in sent.split():
                        candidate = (current + " " + word).strip() if current else word
                        if _fits(candidate):
                            current = candidate
                        else:
                            if current:
                                pieces.append(current)
                            current = word
        if current:
            pieces.append(current)
        return pieces

    chunks, current = [], ""
    for para in text.split("\n"):
        if not para.strip():
            continue                     # skip blank lines — don't flush here
        candidate = (current + "\n" + para).strip() if current else para
        if _fits(candidate):
            current = candidate          # still under limit — keep accumulating
        else:
            if current:
                chunks.append(current)   # flush what we have
            if _fits(para):
                current = para           # start fresh with this paragraph
            else:
                sub = _split_para(para)  # paragraph itself is too long
                chunks.extend(sub[:-1])
                current = sub[-1] if sub else ""
    if current:
        chunks.append(current)
    chunks = chunks if chunks else [text]

    # Never split inside a v3 tag like "[hindi accent]" or "[calm]". If a chunk
    # ends with an unclosed "[", peel the trailing fragment off and prepend it
    # to the next chunk so the tag survives intact.
    if len(chunks) > 1:
        fixed = []
        for idx, ch in enumerate(chunks):
            if idx < len(chunks) - 1 and ch.count("[") > ch.count("]"):
                cut = ch.rfind("[")
                if cut > 0:
                    head, tail = ch[:cut].rstrip(), ch[cut:]
                    fixed.append(head)
                    chunks[idx + 1] = tail + " " + chunks[idx + 1].lstrip()
                    continue
            fixed.append(ch)
        chunks = fixed

    return chunks


def _mp3_bytes_to_segment(raw: bytes, work_dir: str = None):
    """
    Decode MP3 bytes into a pydub AudioSegment via a REAL temp file.

    Handing ffmpeg a pipe (io.BytesIO) makes it buffer the stream to its own
    temp file through the 'cache:pipe:0' protocol. On locked-down Windows setups
    (restricted %TEMP%, antivirus) that write fails with
    'ff_tempfile: Cannot open temporary file … Permission denied'. Writing a
    real, seekable input file in a known-writable directory (the output folder)
    sidesteps the cache path entirely.
    """
    AudioSegment = audio_segment()
    d = work_dir if (work_dir and os.path.isdir(work_dir)) else None
    fd, tmp = tempfile.mkstemp(suffix=".mp3", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        return AudioSegment.from_file(tmp, format="mp3")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# ─── Requested-format plumbing ───────────────────────────────────────────────

def pcm_rate(fmt: str) -> Optional[int]:
    """Sample rate for a PCM output_format, or None if *fmt* isn't PCM.

    The format name is the only thing that carries the rate: PCM comes back
    headerless, so nothing in the bytes themselves says how fast to play them.
    """
    m = re.fullmatch(r"pcm_(\d+)", str(fmt or "").strip())
    return int(m.group(1)) if m else None


def _write_wav(pcm: bytes, path: str, rate: int, channels: int = 1) -> str:
    """Write signed-16-bit PCM to a real WAV file using the stdlib.

    Deliberately not pydub: on the PCM path there is nothing to decode, so
    routing through pydub would mean an ffmpeg subprocess (and on locked-down
    Windows, the temp-file failure `_mp3_bytes_to_segment` exists to dodge) for
    no benefit.
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return path


def _strip_wav_header(raw: bytes) -> bytes:
    """Return just the samples if *raw* arrived as a RIFF/WAVE file.

    ElevenLabs offers both `pcm_*` and `wav_*` output formats, and documents
    neither as headered or headerless — the two families existing separately
    implies `pcm_*` is raw, but "implies" is not a guarantee to build on. If a
    header did come back, wrapping it in a second one would put 44 bytes of
    RIFF in the middle of the audio: a burst of noise at the very seam this all
    exists to remove. Detecting it is a few bytes of work, so it is not worth
    depending on the inference.

    Also means setting a `wav_*` format in the ladder keeps working.
    """
    if len(raw) < 12 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        return raw
    pos = 12
    while pos + 8 <= len(raw):
        chunk_id = raw[pos:pos + 4]
        size = int.from_bytes(raw[pos + 4:pos + 8], "little")
        body = pos + 8
        if chunk_id == b"data":
            # A streamed WAV often leaves the data size at 0 or 0xFFFFFFFF
            # because it wasn't known when the header went out; take the rest.
            if size and body + size <= len(raw):
                return raw[body:body + size]
            return raw[body:]
        pos = body + size + (size & 1)      # RIFF chunks are word-aligned
    return raw


def _align_pcm(raw: bytes, channels: int = 1) -> bytes:
    """Drop a trailing partial frame.

    A truncated response can leave an odd byte count; appending that to the next
    chunk would shift every following sample by one byte and turn the rest of
    the render into noise. Cheap insurance against a very loud failure.
    """
    stride = 2 * channels
    extra = len(raw) % stride
    return raw[:len(raw) - extra] if extra else raw


def _pcm_payload(raw: bytes) -> bytes:
    """Sample data ready to concatenate, whatever the API wrapped it in."""
    return _align_pcm(_strip_wav_header(raw))


class _ElevenHttpError(Exception):
    """An HTTP failure from ElevenLabs, with the body kept for inspection.

    Exists so format negotiation can look at *why* a request failed before
    deciding whether to step down the ladder or report it, which it cannot do
    once the failure has been flattened into a user-facing ValueError.
    """

    def __init__(self, code: int, body: str):
        super().__init__(f"HTTP {code}")
        self.code = code
        self.body = body or ""


# Formats an account has already refused, per endpoint and key. A dialogue makes
# one request per turn, so without this a tier-limited account would re-probe —
# and for Step 2, be billed for — the refused format on all sixty of them.
_FORMAT_MEMO: Dict[tuple, str] = {}


def _opening_format(endpoint: str, ladder: List[str], api_key: str) -> str:
    """Where to start on the ladder: wherever this key last settled."""
    return _FORMAT_MEMO.get((endpoint, api_key_fingerprint(api_key)), ladder[0])


def _remember_format(endpoint: str, api_key: str, fmt: str) -> None:
    _FORMAT_MEMO[(endpoint, api_key_fingerprint(api_key))] = fmt


def _step_down(endpoint: str, api_key: str, ladder: List[str],
               fmt: str, err: _ElevenHttpError) -> Optional[str]:
    """The next format to try, or None to give up and report *err*."""
    if not _rejects_format(err) or fmt == ladder[-1]:
        return None
    nxt = ladder[ladder.index(fmt) + 1]
    _remember_format(endpoint, api_key, nxt)
    return nxt


def _tts_error_message(err: _ElevenHttpError, voice_id: str) -> str:
    """Map a TTS HTTP failure to the message the user should see."""
    body = err.body[:500]
    if err.code == 401:
        return "ElevenLabs rejected the API key (401). Re-paste a valid key."
    if err.code == 404:
        return (f"ElevenLabs voice not found (404). voice_id={voice_id!r} is not "
                "on this account. Click 'Reload Voices' and pick a voice again.")
    if err.code == 422:
        return ("ElevenLabs rejected the request (422). The voice may not support "
                f"the target language. Details: {body}")
    if err.code == 429:
        return "ElevenLabs rate limit hit (429). Try again shortly."
    return f"ElevenLabs TTS error (HTTP {err.code}): {body}"


def _rejects_format(err: _ElevenHttpError) -> bool:
    """Whether this failure means "not that audio format on this account".

    Keyword-gated rather than assumed from the status code: a bad key also
    returns 401, and walking the whole format ladder on a bad key would spend
    three requests to arrive at the same message.
    """
    if err.code not in (400, 401, 403, 422):
        return False
    blob = err.body.lower()
    return any(t in blob for t in ("output_format", "output format", "tier",
                                   "subscription", "upgrade", "not allowed"))


def _rejects_stitching(err: _ElevenHttpError) -> bool:
    """Whether this failure blames the previous_text / next_text fields."""
    if err.code not in (400, 422):
        return False
    blob = err.body.lower()
    return "previous_text" in blob or "next_text" in blob


def _tts_post(voice_id: str, api_key: str, payload: dict,
              fmt: Optional[str]) -> bytes:
    """One TTS request. Returns the audio bytes, raises _ElevenHttpError on HTTP failure.

    *fmt* of None is the legacy request: no output_format at all, and the MP3
    Accept header the original app sent. The single-speaker pipeline uses it so
    that tab's requests stay exactly what they always were.
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    if fmt:
        url += f"?output_format={urllib.parse.quote(fmt)}"
    req = urllib.request.Request(
        url,
        data=body, method="POST",
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json; charset=utf-8",
            # When output_format decides what comes back, don't also assert a
            # MIME type here, or the two can disagree.
            "Accept": "*/*" if fmt else "audio/mpeg",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=180, context=_SSL_CTX) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise _ElevenHttpError(e.code, detail) from None
    except urllib.error.URLError as e:
        raise ValueError(f"Network error during ElevenLabs TTS: {e.reason}") from None


def _sts_error_message(err: _ElevenHttpError, voice_id: str) -> str:
    """Map a voice-changer HTTP failure to the message the user should see."""
    body = err.body[:500]
    if err.code == 401:
        return "ElevenLabs rejected the API key (401). Re-paste a valid key."
    if err.code == 404:
        return (f"ElevenLabs voice not found (404). voice_id={voice_id!r} is not on "
                "this account. Click 'Reload Voices' and pick a voice again.")
    if err.code == 413:
        return ("Audio file too large for the ElevenLabs voice changer (413). "
                "Split the file into smaller parts and retry.")
    if err.code == 422:
        return f"ElevenLabs rejected the request (422). Details: {body}"
    if err.code == 429:
        return "ElevenLabs rate limit hit (429). Try again shortly."
    return f"ElevenLabs voice changer error (HTTP {err.code}): {body}"


def _sts_post(voice_id: str, api_key: str, model_id: str, filename: str,
              mime: str, audio_data: bytes, fmt: Optional[str]) -> bytes:
    """One speech-to-speech request. Raises _ElevenHttpError on HTTP failure.

    As in _tts_post, *fmt* of None is the original request the single-speaker
    pipeline still makes.
    """
    body, boundary = _multipart_body(
        fields=[("model_id", model_id)],
        files=[("audio", filename, mime, audio_data)],
    )
    url = f"https://api.elevenlabs.io/v1/speech-to-speech/{voice_id}"
    if fmt:
        url += f"?output_format={urllib.parse.quote(fmt)}"
    req = urllib.request.Request(
        url,
        data=body, method="POST",
        headers={"xi-api-key": api_key,
                 "Content-Type": f"multipart/form-data; boundary={boundary}",
                 # When output_format decides what comes back, don't also assert
                 # a MIME type here, or the two can disagree.
                 "Accept": "*/*" if fmt else "audio/mpeg"},
    )
    try:
        with urllib.request.urlopen(req, timeout=600, context=_SSL_CTX) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise _ElevenHttpError(e.code, detail) from None
    except urllib.error.URLError as e:
        raise ValueError(f"Network error during ElevenLabs voice change: {e.reason}") from None


def synthesize_tts(text: str, output_path: str, api_key: str,
                   voice_id: str,
                   model_id: str = ELEVENLABS_TTS_MODEL,
                   status_cb=None,
                   write_chunk_files: bool = True,
                   voice_settings: Optional[dict] = None,
                   formats: Optional[Sequence[str]] = None,
                   previous_text: str = "",
                   next_text: str = "") -> str:
    """
    Render *text* to speech with ElevenLabs TTS and save it to *output_path*.

    Text is sent in ~ELEVENLABS_CHUNK_CHARS chunks and the returned audio is
    concatenated. When *write_chunk_files* is true a per-chunk audio file and a
    <output>_chunks.txt manifest are written next to the output, which is how the
    original app behaved and is useful for debugging a bad render.

    *formats* is the output_format ladder to negotiate, and it selects which of
    two join strategies runs:

      None (default)  the original request — MP3 per chunk, joined by decoding
                      through pydub. Multi-chunk renders tick at every seam for
                      the reasons in ELEVENLABS_TTS_FORMATS. This is what the
                      single-speaker tab uses, deliberately: that tab's output is
                      pinned to what it has always produced.
      a ladder        PCM, joined by appending bytes, so a multi-chunk render has
                      no audible seam. Pass ELEVENLABS_TTS_FORMATS. Also enables
                      the previous_text / next_text context.

    *voice_settings* overrides the Indic-tuned defaults. The single-speaker
    pipeline passes nothing and gets exactly what it always got; the dialogue
    pipeline passes each speaker's own settings, because an interviewer wants
    higher stability and less style than Sadhguru does.

    *previous_text* / *next_text* are what was said either side of *text* by
    callers that do their own chunking — one API call per dialogue turn or per
    dub chunk. Without them such a call has no way to know it is mid-sentence:
    the internal stitching below only ever sees the chunks *this* call made, so
    a caller rendering 225 four-second fragments got 225 isolated utterances,
    each opening at full energy and closing on a sentence-final fall. That is
    what makes a chunked render sound clipped and robotic next to the
    single-speaker tab, which sends ~1000-character blocks and gets a
    continuous contour for free. Context only — not spoken, not billed.

    Returns output_path.
    """
    if not api_key or not api_key.strip():
        raise ValueError("ElevenLabs API key is missing — paste it in the API Key box.")
    if not text or not text.strip():
        raise ValueError("TTS text is empty — nothing to synthesize.")

    api_key = api_key.strip()
    # Sanitize voice_id BEFORE it reaches the URL. A display label leaking
    # through here makes urllib raise "URL can't contain control characters".
    raw_voice_id = str(voice_id or "").strip()
    voice_id = sanitize_voice_id(raw_voice_id)
    if not voice_id:
        raise ValueError(
            f"Invalid ElevenLabs voice_id (received: {raw_voice_id!r}). "
            "Re-select a voice from the dropdown — the value must be the raw "
            "voice ID, not a display label.")
    model_id = (model_id or ELEVENLABS_TTS_MODEL).strip() or ELEVENLABS_TTS_MODEL
    # Swap in a model this key can actually call before anything downstream
    # branches on it — the tag-stripping test below is one such branch, so a
    # v3-to-v2 substitution has to happen first or the tags would be spoken.
    model_id = resolve_model(api_key, model_id, "tts", status_cb=status_cb)

    # Inline audio tags are an eleven_v3 feature. Older models would read them
    # aloud, so strip them for anything that isn't v3.
    if not model_id.startswith("eleven_v3"):
        stripped = strip_emotion_tags(text)
        if stripped.strip():
            text = stripped

    if status_cb:
        status_cb("TTS: Connecting to ElevenLabs…")

    chunks = split_text_for_elevenlabs(text)
    total  = len(chunks)

    # Indic-tuned voice settings. Lower stability + raised style give eleven_v3
    # room to act on the inline emotion / accent tags injected by the emotion
    # pass ([hindi accent], [calm], [slow], [pause]) so the delivery feels
    # reflective rather than flat.
    settings = dict(DEFAULT_VOICE_SETTINGS)
    if voice_settings:
        settings.update(voice_settings)

    out_base       = os.path.splitext(output_path)[0]
    chunk_log_path = out_base + "_chunks.txt"
    chunk_log_lines = [
        f"TTS Chunk Log — {os.path.basename(output_path)}",
        "Platform : ElevenLabs",
        f"Voice ID : {voice_id}",
        f"Model    : {model_id}",
        f"Settings : {json.dumps(settings, sort_keys=True)}",
        f"Total chunks: {total}",
        "",
    ]

    chunk_bytes_list = []

    # Negotiated on the first chunk and then fixed for the render — chunks at
    # two different sample rates would have to be resampled to join, which is
    # the decode-and-re-export path this is here to avoid.
    fmt_ladder = [f for f in (formats or ()) if f]
    fmt = _opening_format("tts", fmt_ladder, api_key) if fmt_ladder else None
    # Context is available if this call chunked the text itself (total > 1) *or*
    # the caller told us what surrounds it. The second case is the whole reason
    # `total > 1` is not the condition: a per-turn or per-chunk caller never
    # splits anything, so it would never have stitched at all.
    previous_text = (previous_text or "").strip()
    next_text     = (next_text or "").strip()
    stitch = (bool(fmt_ladder) and ELEVENLABS_STITCH_CONTEXT
              and (total > 1 or previous_text or next_text))

    for i, chunk in enumerate(chunks, 1):
        if status_cb:
            status_cb(f"TTS: ElevenLabs generating audio… chunk {i} of {total}"
                      if total > 1 else "TTS: ElevenLabs generating audio…")

        # NOTE: do NOT send `language_code` — eleven_v3 auto-detects the target
        # language from the input text, and passing it triggers HTTP 400
        # `unsupported_language` on multilingual models.
        payload = {
            "text": chunk,
            "model_id": model_id,
            "voice_settings": settings,
        }
        if stitch:
            # Tail of what precedes this chunk and head of what follows, so
            # intonation crosses the seam. Inside this call that is the
            # neighbouring chunk; at the two outer edges it is the caller's
            # context, which is what carries prosody across a per-turn or
            # per-chunk API boundary. Context only — not spoken, not billed.
            before = (chunks[i - 2] if i > 1 else previous_text)
            after  = (chunks[i] if i < total else next_text)
            if before:
                payload["previous_text"] = before[-ELEVENLABS_STITCH_CHARS:]
            if after:
                payload["next_text"] = after[:ELEVENLABS_STITCH_CHARS]

        while True:
            try:
                audio_bytes = _tts_post(voice_id, api_key, payload, fmt)
                break
            except _ElevenHttpError as err:
                # Step down the format ladder if this account can't serve the
                # one we asked for. Only while nothing has been rendered yet;
                # mid-render the sample rate is already committed.
                nxt = (_step_down("tts", api_key, fmt_ladder, fmt, err)
                       if fmt_ladder and not chunk_bytes_list else None)
                if nxt:
                    fmt = nxt
                    if status_cb:
                        status_cb(f"TTS: account can't serve that audio format — "
                                  f"falling back to {fmt}.")
                    continue
                # Older models predate request stitching. Losing the context
                # costs prosody across a seam, not the render.
                if _rejects_stitching(err) and stitch:
                    stitch = False
                    payload.pop("previous_text", None)
                    payload.pop("next_text", None)
                    if status_cb:
                        status_cb("TTS: model doesn't accept neighbouring-text "
                                  "context — continuing without it.")
                    continue
                raise ValueError(_tts_error_message(err, voice_id)) from None

        rate = pcm_rate(fmt)
        if rate:
            audio_bytes = _pcm_payload(audio_bytes)
        chunk_bytes_list.append(audio_bytes)

        chunk_note = ""
        if write_chunk_files:
            # On the PCM path these have to be .wav: raw PCM inside a .mp3 would
            # be undecodable, which defeats the point of a debug artefact. The
            # legacy path keeps the .mp3 files it always wrote.
            if rate:
                chunk_audio_path = f"{out_base}_chunk_{i:02d}.wav"
                _write_wav(audio_bytes, chunk_audio_path, rate)
            else:
                chunk_audio_path = f"{out_base}_chunk_{i:02d}.mp3"
                with open(chunk_audio_path, "wb") as cf:
                    cf.write(audio_bytes)
            chunk_note = os.path.basename(chunk_audio_path)

        chunk_log_lines += [
            f"=== CHUNK {i} of {total} ===",
            f"Characters : {len(chunk)}",
            f"Bytes (UTF-8): {len(chunk.encode('utf-8'))}",
            f"Audio saved : {chunk_note or '(not written)'}",
            "--- Text ---",
            chunk,
            "",
        ]

    if write_chunk_files:
        # Only on the negotiated path — the legacy manifest never had this line,
        # and the single-speaker tab's debug output should still match it.
        if fmt:
            chunk_log_lines.insert(5, f"Format   : {fmt}")
        try:
            with open(chunk_log_path, "w", encoding="utf-8") as lf:
                lf.write("\n".join(chunk_log_lines))
        except OSError:
            pass

    if status_cb:
        note = f" ({total} chunks joined)" if total > 1 else ""
        status_cb(f"TTS: Saving → {os.path.basename(output_path)}…{note}")

    # NOTE: the container written here is WAV (matching the original app), even
    # though the filename ends in .mp3 — the Step-2 upload and every player we
    # care about sniff the actual content, so this is deliberate rather than a
    # slip. See README ("Step-1 file format").
    rate = pcm_rate(fmt)
    if rate:
        # The gapless path. Concatenating PCM is exact: sample N of one chunk is
        # followed by sample 1 of the next with nothing invented in between, so
        # there is no seam to hide and no fade to apply.
        _write_wav(b"".join(chunk_bytes_list), output_path, rate)
        return output_path

    # MP3 fallback — decoding is unavoidable here, and so are the frame-edge
    # artefacts described in config.ELEVENLABS_TTS_FORMATS. Only reached on an
    # account that serves no PCM at all.
    try:
        AudioSegment = audio_segment()
        combined = AudioSegment.empty()
        for raw in chunk_bytes_list:
            combined += _mp3_bytes_to_segment(raw, os.path.dirname(os.path.abspath(output_path)))
        combined.export(output_path, format="wav")
    except ImportError:
        if status_cb:
            status_cb("TTS: Warning — pydub/ffmpeg not found; saving raw MP3 bytes.")
        with open(output_path, "wb") as f:
            for raw in chunk_bytes_list:
                f.write(raw)

    return output_path


# ═════════════════════════════════════════════════════════════════════════════
#  Step 2 — speech to speech (voice changer)
# ═════════════════════════════════════════════════════════════════════════════

def convert_voice(input_path: str, output_path: str, api_key: str,
                  voice_id: str,
                  model_id: str = ELEVENLABS_STS_MODEL,
                  status_cb=None,
                  formats: Optional[Sequence[str]] = None) -> str:
    """
    Re-render an existing audio file in a different voice via the ElevenLabs
    speech-to-speech API. Keeps the source performance (timing, emotion,
    delivery) and swaps only the voice. Returns output_path.

    *formats* is the output_format ladder to negotiate:

      None (default)  the original request — MP3, written out verbatim. The
                      single-speaker tab uses this, so its final file stays a
                      real MP3 of the size it always was.
      a ladder        PCM, wrapped in a WAV container, which keeps one lossy
                      generation out of a chain that already runs TTS into
                      speech-to-speech. Pass ELEVENLABS_STS_FORMATS. Note the
                      output extension is not honoured on this path — a `.mp3`
                      name gets WAV bytes.
    """
    if not api_key:
        raise ValueError("ElevenLabs API key is missing — paste it in the API Key box.")
    raw_voice_id = str(voice_id or "").strip()
    voice_id = sanitize_voice_id(raw_voice_id)
    if not voice_id:
        raise ValueError(
            f"No valid ElevenLabs voice selected (received: {raw_voice_id!r}). "
            "Paste a valid API key and pick a voice from the dropdown.")
    model_id = (model_id or ELEVENLABS_STS_MODEL).strip() or ELEVENLABS_STS_MODEL
    model_id = resolve_model(api_key, model_id, "sts", status_cb=status_cb)
    if not os.path.isfile(input_path):
        raise ValueError(f"Input audio file not found: {input_path}")

    if status_cb:
        status_cb("Voice Changer: uploading audio to ElevenLabs…")

    with open(input_path, "rb") as f:
        audio_data = f.read()
    mime, _ = mimetypes.guess_type(input_path)
    mime = mime or "audio/mpeg"

    fmt_ladder = [f for f in (formats or ()) if f]
    fmt = _opening_format("sts", fmt_ladder, api_key) if fmt_ladder else None
    filename = os.path.basename(input_path)

    while True:
        try:
            audio_bytes = _sts_post(voice_id, api_key, model_id, filename,
                                    mime, audio_data, fmt)
            break
        except _ElevenHttpError as err:
            nxt = (_step_down("sts", api_key, fmt_ladder, fmt, err)
                   if fmt_ladder else None)
            if nxt:
                fmt = nxt
                if status_cb:
                    status_cb("Voice Changer: account can't serve that audio "
                              f"format — falling back to {fmt}.")
                continue
            raise ValueError(_sts_error_message(err, voice_id)) from None

    if not audio_bytes:
        raise ValueError("ElevenLabs returned empty audio for the voice change request.")

    # PCM comes back headerless, so it has to be given a container before
    # anything can open it. WAV via the stdlib: nothing to decode, so no ffmpeg.
    rate = pcm_rate(fmt)
    if rate:
        _write_wav(_pcm_payload(audio_bytes), output_path, rate)
    else:
        with open(output_path, "wb") as f:
            f.write(audio_bytes)
    if status_cb:
        status_cb(f"Voice Changer: saved → {os.path.basename(output_path)}")
    return output_path
