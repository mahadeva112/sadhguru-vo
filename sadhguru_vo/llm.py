"""
The optional emotion-tag pass.

Before Step 1 the script can be run through an LLM that injects ElevenLabs v3
inline audio tags ([hindi accent], [calm], [contemplative], [slow], [pause], …)
so the delivery sounds reflective rather than flat. Words and punctuation are
preserved verbatim — only tags are added.

Three providers are supported, selected in llm_settings.json:
  1. Vertex AI          — service-account JSON file
  2. Gemini API         — plain Google AI Studio API key
  3. OpenAI-compatible  — any /v1/chat/completions endpoint (LiteLLM proxy,
                          OpenRouter, vLLM, …) via base URL + optional key

Only the OpenAI-compatible provider works with no third-party packages
installed; the two Google providers need `google-genai`.

The whole pass is best-effort: any failure (missing prompt, no credentials,
network error) returns the original text so Step 1 is never blocked.
"""

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from typing import Dict, Optional, Tuple

from .config import (GEMINI_DEFAULT_MODEL, LLM_PROVIDER_GEMINI,
                     LLM_PROVIDER_OPENAI, LLM_PROVIDER_VERTEX, LLM_PROVIDERS,
                     LLM_SETTINGS_FILE, PROMPTS_DIR, VO_LANGUAGE, APP_DIR)

try:
    from google import genai
    from google.genai import types as genai_types
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False

_LLM_SETTINGS_DEFAULTS: Dict[str, str] = {
    "provider":        LLM_PROVIDER_OPENAI,
    "vertex_json":     "",              # blank → <app>/vertex_key.json
    "gemini_api_key":  "",
    "openai_base_url": "",
    "openai_api_key":  "",
    "openai_model":    "",
    "prompt_caching":  "1",
}
_LLM_SETTINGS: Dict[str, str] = dict(_LLM_SETTINGS_DEFAULTS)


def load_llm_settings() -> None:
    """Load llm_settings.json (if present) over the defaults."""
    global _LLM_SETTINGS
    _LLM_SETTINGS = dict(_LLM_SETTINGS_DEFAULTS)
    try:
        with open(LLM_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k in _LLM_SETTINGS_DEFAULTS:
                if k in data and isinstance(data[k], str):
                    _LLM_SETTINGS[k] = data[k]
        if _LLM_SETTINGS["provider"] not in LLM_PROVIDERS:
            _LLM_SETTINGS["provider"] = LLM_PROVIDER_OPENAI
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[LLM] Could not read {LLM_SETTINGS_FILE}: {e} — using defaults.")


def save_llm_settings() -> None:
    """Persist the current settings. Raises on a genuine write failure so the
    settings dialog can surface it."""
    with open(LLM_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(_LLM_SETTINGS, f, indent=2)


def get_llm_settings() -> Dict[str, str]:
    return _LLM_SETTINGS


def llm_provider_label() -> str:
    """Short human-readable description of the active provider, for the UI."""
    s = get_llm_settings()
    p = s.get("provider", LLM_PROVIDER_OPENAI)
    if p == LLM_PROVIDER_OPENAI:
        model = s.get("openai_model") or "(model not set)"
        return f"{model} via {s.get('openai_base_url') or '(base URL not set)'}"
    return f"{GEMINI_DEFAULT_MODEL} via {p}"


def _get_vertex_project() -> str:
    key_file = (get_llm_settings().get("vertex_json") or "").strip() \
               or os.path.join(APP_DIR, "vertex_key.json")
    if not os.path.exists(key_file):
        raise FileNotFoundError(f"Vertex service-account JSON not found at {key_file}")
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = key_file
    with open(key_file, "r", encoding="utf-8") as f:
        key_data = json.load(f)
    project_id = key_data.get("project_id")
    if not project_id:
        raise ValueError(f"{os.path.basename(key_file)} is missing 'project_id'.")
    return project_id


def _make_genai_client():
    """google-genai Client for the Vertex or Gemini-API-key providers."""
    if not GENAI_AVAILABLE:
        raise ImportError("google-genai not installed. Run: pip install google-genai")
    s = get_llm_settings()
    if s.get("provider") == LLM_PROVIDER_GEMINI:
        api_key = (s.get("gemini_api_key") or "").strip()
        if not api_key:
            raise ValueError("Gemini API key is empty — set it in LLM Settings.")
        return genai.Client(api_key=api_key)
    return genai.Client(vertexai=True, project=_get_vertex_project(),
                        location="us-central1")


def _openai_chat(prompt: str, model: str, timeout: float = 900.0) -> str:
    """Single-turn /v1/chat/completions call against the configured base URL."""
    s = get_llm_settings()
    base = (s.get("openai_base_url") or "").strip().rstrip("/")
    if not base:
        raise ValueError("Base URL is empty — set it in LLM Settings.")
    url = (base + "/chat/completions") if base.endswith("/v1") \
          else (base + "/v1/chat/completions")
    model = (model or "").strip()
    if not model:
        raise ValueError("Model name is empty — set it in LLM Settings.")
    headers = {"Content-Type": "application/json"}
    api_key = (s.get("openai_api_key") or "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        raise RuntimeError(f"LLM endpoint returned HTTP {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot reach LLM endpoint {url}: {e.reason}") from e
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise ValueError(f"Unexpected response from {url}: {str(data)[:400]}")


# Prompt-cache registry: (model, sha1-of-prefix) → Gemini cache name.
# None marks a prefix as uncacheable (too small / rejected) so we stop retrying.
# Caches live server-side with a 1-hour TTL and are recreated transparently.
_GENAI_CACHE_REGISTRY: Dict[Tuple[str, str], Optional[str]] = {}


def _genai_cached_generate(client, model: str, static_prefix: Optional[str],
                           dynamic: str, use_cache: bool) -> str:
    """generate_content with explicit Gemini prompt caching for the static
    prompt prefix. Any cache failure falls back to a plain inline call."""
    inline = (static_prefix or "") + dynamic
    if not (use_cache and static_prefix):
        return client.models.generate_content(model=model, contents=inline).text

    key = (model, hashlib.sha1(static_prefix.encode("utf-8")).hexdigest())
    cache_name = _GENAI_CACHE_REGISTRY.get(key, "")
    if cache_name is None:                       # known-uncacheable prefix
        return client.models.generate_content(model=model, contents=inline).text
    if not cache_name:
        try:
            cache = client.caches.create(
                model=model,
                config=genai_types.CreateCachedContentConfig(
                    contents=[static_prefix], ttl="3600s"))
            cache_name = cache.name
            _GENAI_CACHE_REGISTRY[key] = cache_name
        except Exception:
            # Prefix below the model's cache minimum, or caching unsupported —
            # remember that and never retry for this prefix.
            _GENAI_CACHE_REGISTRY[key] = None
            return client.models.generate_content(model=model, contents=inline).text
    try:
        return client.models.generate_content(
            model=model, contents=dynamic,
            config=genai_types.GenerateContentConfig(cached_content=cache_name)
        ).text
    except Exception:
        # Cache likely expired — forget it so the next call recreates it.
        _GENAI_CACHE_REGISTRY.pop(key, None)
        return client.models.generate_content(model=model, contents=inline).text


def llm_generate(prompt: str, model: str = GEMINI_DEFAULT_MODEL,
                 static_prefix: Optional[str] = None) -> str:
    """Provider-agnostic text generation.

    *static_prefix* is the reusable part (the prompt file); *prompt* is the
    per-request part. Splitting them enables prompt caching: explicit Gemini
    context caching on the Vertex / Gemini-key providers, and implicit
    server-side prefix caching on OpenAI-compatible endpoints — which also
    relies on the static prefix coming first in the request."""
    s = get_llm_settings()
    if s.get("provider") == LLM_PROVIDER_OPENAI:
        return _openai_chat((static_prefix or "") + prompt,
                            (s.get("openai_model") or "").strip() or model)
    client = _make_genai_client()
    use_cache = s.get("prompt_caching", "1") == "1"
    return _genai_cached_generate(client, model, static_prefix, prompt, use_cache)


def validate_llm_config() -> None:
    """Raise with a user-readable message if the active provider is unusable."""
    s = get_llm_settings()
    p = s.get("provider", LLM_PROVIDER_OPENAI)
    if p == LLM_PROVIDER_OPENAI:
        if not (s.get("openai_base_url") or "").strip():
            raise ValueError("OpenAI-compatible base URL is empty — open LLM Settings.")
        if not (s.get("openai_model") or "").strip():
            raise ValueError("Model name is empty — open LLM Settings.")
        return
    if not GENAI_AVAILABLE:
        raise ImportError("google-genai not installed. Run: pip install google-genai")
    if p == LLM_PROVIDER_GEMINI:
        if not (s.get("gemini_api_key") or "").strip():
            raise ValueError("Gemini API key is empty — open LLM Settings.")
        return
    _get_vertex_project()


def _strip_code_fence(text: str) -> str:
    """Strip ```lang ... ``` fences the model sometimes wraps its output in."""
    if not text:
        return text
    m = re.match(r"^\s*```[a-zA-Z0-9_-]*\s*\n(.*?)\n```\s*$", text, re.DOTALL)
    return m.group(1) if m else text


def _load_emotion_prompt(language: str) -> str:
    """Load prompts/Step4_Emotion_Prompt_<Language>.txt."""
    fname = f"Step4_Emotion_Prompt_{language}.txt"
    path  = os.path.join(PROMPTS_DIR, fname)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Prompt file not found: prompts/{fname}")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def run_emotion_enrichment(text: str,
                           language: str = VO_LANGUAGE,
                           model: str = GEMINI_DEFAULT_MODEL,
                           status_cb=None) -> str:
    """
    Inject ElevenLabs v3 emotion / accent tags into the script.

    Best-effort: on ANY failure (missing prompt, no credentials, network error,
    empty response) the original text is returned so Step 1 is never blocked.
    """
    if not text or not text.strip():
        return text
    try:
        if status_cb:
            status_cb(f"Emotion: enrichment pass ({language})…")
        prompt   = _load_emotion_prompt(language)
        enriched = llm_generate(f"\n\n{text}", model, static_prefix=prompt) or ""
        enriched = _strip_code_fence(enriched).strip()
        if not enriched:
            if status_cb:
                status_cb("Emotion: returned empty — using original text.")
            return text
        if status_cb:
            status_cb("Emotion: enrichment ✓")
        return enriched
    except Exception as e:
        if status_cb:
            status_cb(f"Emotion: skipped ({e}). Using original text.")
        return text


# ═════════════════════════════════════════════════════════════════════════════
#  Dialogue emotion pass
# ═════════════════════════════════════════════════════════════════════════════

_DIALOGUE_INSTRUCTIONS = """
────────────────────────────────────────────────────────────────────────────
ADDITIONAL RULES FOR THIS REQUEST — THIS IS A CONVERSATION, NOT A MONOLOGUE
────────────────────────────────────────────────────────────────────────────
The script below has several speakers. Every line is written as

    SPEAKER: text

Tag each speaker in their OWN manner, using the delivery notes below. Do not
give every speaker the same reflective, unhurried delivery — a question asked
by an interviewer and an answer given by Sadhguru should not sound alike.

DELIVERY NOTES
{styles}

You may use the surrounding turns as context — that is why you are being given
the whole conversation at once — but tag each turn for the speaker who says it.

OUTPUT FORMAT — follow exactly:
  • Return the SAME number of turns, in the SAME order, with the SAME speaker
    labels, spelled exactly as they appear below.
  • One turn per block, written as `SPEAKER: text`, blocks separated by a blank
    line.
  • Keep every word and every punctuation mark of the original. Add audio tags
    only. Do not translate, summarise, reorder, merge, split, or drop a turn.
  • No preamble, no commentary, no code fences — just the turns.

SCRIPT
{script}
"""


class DialogueEmotionError(RuntimeError):
    """The dialogue emotion pass produced something unusable."""


def run_dialogue_emotion(turns, cast=None,
                         language: str = VO_LANGUAGE,
                         model: str = GEMINI_DEFAULT_MODEL,
                         status_cb=None):
    """
    Tag a whole conversation in one LLM call, per speaker.

    One call rather than one per turn, for two reasons. It is far cheaper, and
    more importantly the model sees the surrounding turns — so it can tag a
    reply as a reply. Each speaker's `style` note from the cast steers their own
    turns, which is what stops the interviewer inheriting Sadhguru's pacing.

    Returns a new list of Turns with tags added. Best-effort in exactly the same
    way as the single-speaker pass: on ANY failure — bad credentials, network
    error, or a response whose turn structure does not match what was sent — the
    ORIGINAL turns come back untouched and the reason is reported through
    *status_cb*. A render must never be blocked, and a render must never be
    silently re-ordered by a model that decided to be helpful.
    """
    from .script_parser import Turn, format_script, normalize_speaker, parse_script

    if not turns:
        return turns

    def _note(msg: str) -> None:
        if status_cb:
            status_cb(msg)

    original = list(turns)
    try:
        _note(f"Emotion: tagging {len(original)} turn(s) in one pass ({language})…")
        static_prefix = _load_emotion_prompt(language)

        speakers, seen = [], set()
        for t in original:
            if t.key not in seen:
                seen.add(t.key)
                speakers.append(t.speaker)

        lines = []
        for name in speakers:
            recipe = (cast or {}).get(normalize_speaker(name))
            style = (getattr(recipe, "style", "") or "").strip()
            lines.append(f"  • {name}: {style or 'natural, neutral delivery'}")

        dynamic = _DIALOGUE_INSTRUCTIONS.format(
            styles="\n".join(lines), script=format_script(original))

        raw = llm_generate(dynamic, model, static_prefix=static_prefix) or ""
        raw = _strip_code_fence(raw).strip()
        if not raw:
            raise DialogueEmotionError("the model returned nothing")

        tagged = parse_script(raw)

        # Structural checks. A model that drops or reorders a turn would
        # silently ship a conversation that says something different from the
        # approved script, so this is a hard gate rather than a warning.
        if len(tagged) != len(original):
            raise DialogueEmotionError(
                f"got {len(tagged)} turns back, sent {len(original)}")
        for i, (was, now) in enumerate(zip(original, tagged), 1):
            if was.key != now.key:
                raise DialogueEmotionError(
                    f"turn {i} came back as {now.speaker!r}, sent {was.speaker!r}")

        out = [Turn(index=was.index, speaker=was.speaker, text=now.text,
                    start_ms=was.start_ms, end_ms=was.end_ms)
               for was, now in zip(original, tagged)]
        _note(f"Emotion: tagged {len(out)} turn(s) ✓")
        return out

    except Exception as e:
        _note(f"Emotion: skipped ({e}). Using the untagged script.")
        return original


load_llm_settings()
