"""Audio debriefs of tuning recommendations via ElevenLabs Text-to-Speech.

Owner: Person 2.  ElevenLabs is an EXTERNAL cloud service, so every text is
leak-checked (privacy/mask.py) before it can leave the host:
real table/column names, hidden values, emails, card numbers, our secret
key or the API key itself -> ValueError, nothing is sent.

Cache: audio is stored as <sha256>.mp3 in AUDIO_CACHE_DIR. The same text with
the same voice and model is never sent twice (saves free-tier credits).

Self-test:  .venv/Scripts/python -m privacy.tts
"""
import hashlib
import os
from pathlib import Path

import requests

from privacy import mask

# ============================================================
# TUNABLE SETTINGS
# ============================================================
ELEVENLABS_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"   # Voice used for debriefs (default: calm,
                                               # professional). Any voice id from your
                                               # ElevenLabs voice library works.
ELEVENLABS_MODEL_ID = "eleven_flash_v2_5"      # "eleven_flash_v2_5" = lowest latency and
                                               # cheapest; "eleven_turbo_v2_5" = a bit more
                                               # natural, slightly slower.
OUTPUT_FORMAT = "mp3_44100_128"                # Higher bitrate = better sound, bigger files.
MAX_CHARS = 1000                               # Longer texts are refused: each character
                                               # costs credits. Raise for longer debriefs.
REQUEST_TIMEOUT_S = 30                         # Give up on ElevenLabs after this many seconds.
AUDIO_CACHE_DIR = Path(__file__).with_name(".audio_cache")   # git-ignored (privacy/.gitignore)

# ============================================================
# FIXED SETTINGS
# ============================================================
API_URL = "https://api.elevenlabs.io/v1"
# Keys are read from the environment, never stored in code. Separate keys let
# TTS and STT credits/quotas be tracked independently; each falls back to the
# shared key when its own is not set.
API_KEY_ENV = "ELEVENLABS_API_KEY"             # shared fallback
TTS_API_KEY_ENV = "ELEVENLABS_TTS_API_KEY"     # text-to-speech calls only
STT_API_KEY_ENV = "ELEVENLABS_STT_API_KEY"     # speech-to-text calls only


def _key_status(env):
    """(key, where it came from) for one purpose; key is None if neither is set."""
    if os.getenv(env):
        return os.getenv(env), env
    if os.getenv(API_KEY_ENV):
        return os.getenv(API_KEY_ENV), f"{API_KEY_ENV} (fallback)"
    return None, "not set"


def _api_key():
    """Key for text-to-speech calls."""
    return _key_status(TTS_API_KEY_ENV)[0]


def stt_api_key():
    """Key for speech-to-text calls (for the STT module; never used for TTS)."""
    return _key_status(STT_API_KEY_ENV)[0]


def check_text(text, mapping=None):
    """Raise ValueError if text may not leave the host; otherwise return None.

    Uses the masker's free-text leak check (the debrief is prose), plus a
    check for our two secrets. The error never repeats the leaked text.
    """
    if not text or not text.strip():
        raise ValueError("TTS: empty text")
    if len(text) > MAX_CHARS:
        raise ValueError(f"TTS: text is {len(text)} chars, limit is MAX_CHARS={MAX_CHARS}")
    mapping = mapping or mask.build_mapping()
    leaks = mask.find_leaks(text, mapping, free_text=True)
    secrets_found = [name for name, s in (("masking key", mask.KEY_FILE.read_text().strip()),
                                          ("TTS API key", _api_key()),
                                          ("STT API key", stt_api_key())) if s and s in text]
    if leaks or secrets_found:
        raise ValueError(f"TTS blocked by the privacy leak check: {len(leaks)} real "
                         f"name/value token(s){', ' + ', '.join(secrets_found) if secrets_found else ''}. "
                         "Nothing was sent to ElevenLabs.")


def cache_path(text):
    """<sha256 of text + voice + model>.mp3 - a new voice or model never reuses old audio."""
    digest = hashlib.sha256(f"{ELEVENLABS_VOICE_ID}|{ELEVENLABS_MODEL_ID}|{text}".encode()).hexdigest()
    return AUDIO_CACHE_DIR / f"{digest}.mp3"


KIND_WORDS = {"index": "a new index", "partition": "partitioning the table by date",
              "rewrite": "rewriting the query"}


def debrief_text(result):
    """Spoken summary of a pipeline.analyze() result: numbers only, NO names.

    Masked codes (t_438.c_13b) sound like noise and real names may not leave
    the host, so the debrief says what kind of fix and how much it helps.
    Missing keys are simply skipped.
    """
    if not result.get("leak_check", False):
        return "The privacy check did not pass, so no recommendation is read out."
    r = (result.get("recommendations") or [{}])[0]
    parts = [f"Analysis complete. The best fix is {KIND_WORDS.get(r.get('kind'), 'a tuning change')}."]
    if result.get("improvement_pct") is not None:
        parts.append(f"Estimated cost drops by {round(result['improvement_pct'])} percent.")
    if result.get("time_before_ms") is not None and result.get("time_after_ms") is not None:
        parts.append(f"Measured time goes from {_num(result['time_before_ms'])} "
                     f"to {_num(result['time_after_ms'])} milliseconds.")
    if result.get("write_latency_ms") is not None:
        parts.append(f"Each insert becomes {_num(result['write_latency_ms'] * 1000)} microseconds slower.")
    if result.get("storage_bytes"):
        parts.append(f"It needs about {_num(result['storage_bytes'] / 1e6)} megabytes of storage.")
    parts.append("No raw values were exposed.")
    return " ".join(parts)


def _num(x):
    """Speakable number: 27.0 -> '27', 2.345 -> '2.3', 0.04 -> '0.04'."""
    return f"{x:.0f}" if abs(x) >= 10 else f"{x:.2g}" if abs(x) < 1 else f"{x:.1f}".rstrip("0").rstrip(".")


def speak_debrief(result, mapping=None):
    """Debrief audio for a pipeline result: debrief_text -> leak check -> cache -> ElevenLabs."""
    return speak(debrief_text(result), mapping)


def speak(text, mapping=None):
    """Return the path of an .mp3 of text. Order: leak check -> cache -> ElevenLabs."""
    check_text(text, mapping)                  # always first, even for cached audio
    path = cache_path(text)
    if path.exists():
        return path
    key = _api_key()
    if not key:
        raise RuntimeError(f"TTS: set {TTS_API_KEY_ENV} (or {API_KEY_ENV}) first")
    r = requests.post(f"{API_URL}/text-to-speech/{ELEVENLABS_VOICE_ID}",
                      params={"output_format": OUTPUT_FORMAT},
                      headers={"xi-api-key": key, "Content-Type": "application/json"},
                      json={"text": text, "model_id": ELEVENLABS_MODEL_ID},
                      timeout=REQUEST_TIMEOUT_S)
    if r.status_code != 200:
        raise RuntimeError(f"TTS: ElevenLabs returned HTTP {r.status_code}")   # body not echoed
    AUDIO_CACHE_DIR.mkdir(exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_bytes(r.content)
    tmp.replace(path)                          # never leave a half-written .mp3 in the cache
    return path


def connection_status():
    """HTTP status of the free /models endpoint with the TTS key (costs no credits).
    200 = key works. 401/403 = the key may not list models: either it is wrong,
    or it is valid but restricted to text-to-speech (TTS still works then)."""
    key = _api_key()
    if not key:
        return None
    return requests.get(f"{API_URL}/models", headers={"xi-api-key": key},
                        timeout=REQUEST_TIMEOUT_S).status_code


def _self_test():
    # Key status first, each purpose on its own (values are never printed).
    for purpose, env in (("TTS", TTS_API_KEY_ENV), ("STT", STT_API_KEY_ENV)):
        key, source = _key_status(env)
        print(f"{purpose} key: {'set' if key else 'MISSING'}  (from {source})")
    tts, stt = _api_key(), stt_api_key()
    if tts and stt:
        print("TTS and STT keys are", "the SAME key (shared quota)" if tts == stt else "separate (independent quotas)")
    print()
    m = mask.build_mapping()
    ok = True

    def report(name, passed, note=""):
        nonlocal ok
        ok &= passed is not False
        print(f"{name:44} {'PASS' if passed else ('SKIP' if passed is None else 'FAIL')}  {note}")

    clean = "The weekly report scans every row of t_438. An index on c_13b and c_303 cuts the cost by 95 percent."
    leaky = {"real table name": "The orders table needs an index on region_id.",
             "email": "Slow rows belong to diya.reddy1@example.com.",
             "card number": "Paid with card 4111 1111 1111 1111.",
             "masking key": "debug " + mask.KEY_FILE.read_text().strip()}
    try:
        check_text(clean, m)
        report("leak check: clean masked text passes", True)
    except ValueError as e:
        report("leak check: clean masked text passes", False, str(e))
    for name, text in leaky.items():
        try:
            check_text(text, m)
            report(f"leak check: {name} blocked", False, "NOT blocked")
        except ValueError:
            report(f"leak check: {name} blocked", True)

    # Cache: plant a fake cached file, speak() must return it without any API call.
    fake = "Cache self-test for t_438.c_13b."
    p = cache_path(fake)
    AUDIO_CACHE_DIR.mkdir(exist_ok=True)
    p.write_bytes(b"fake mp3")
    saved = {e: os.environ.pop(e, None) for e in (API_KEY_ENV, TTS_API_KEY_ENV)}   # no key -> an API call would fail
    try:
        report("cache: hit returns file without API call", speak(fake, m) == p)
    finally:
        os.environ.update({e: v for e, v in saved.items() if v is not None})
        p.unlink()
    report("cache: same text -> same file, new text -> new", cache_path(clean) == cache_path(clean)
           and cache_path(clean) != cache_path(clean + "."))

    if not _api_key():
        report("ElevenLabs connection", None, f"no {TTS_API_KEY_ENV} / {API_KEY_ENV} set")
        report("ElevenLabs audio generation", None, f"no {TTS_API_KEY_ENV} / {API_KEY_ENV} set")
    else:
        code = connection_status()
        # ElevenLabs answers 401 or 403 both for a wrong key AND for a valid key that is
        # restricted to text-to-speech; the generation test below is the real proof.
        note = {200: "key works"}.get(code, "key cannot list models (TTS-only key, or wrong key) "
                                            "- see the generation test")
        report("ElevenLabs connection (free /models call)",
               True if code == 200 else (None if code in (401, 403) else False), f"HTTP {code}: {note}")
        short = "Index on t_438 recommended."               # tiny text: few credits
        cached = cache_path(short).exists()
        first = speak(short, m)
        size = first.stat().st_size
        report("ElevenLabs audio generation", size > 1000,
               f"{size:,} bytes, " + ("FROM CACHE - key not used; delete the cache file to re-test"
                                      if cached else "new call with this key"))
        report("cache: second call reuses the file", speak(short, m) == first)
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if _self_test() else 1)
