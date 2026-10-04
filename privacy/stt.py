"""Speech-to-text for the admin's spoken question, via ElevenLabs.

Owner: Person 2.  Team decision (option a): the admin's recorded question is
sent to ElevenLabs as raw audio. It is the admin's own words, not database
data. The transcript is then treated exactly like a typed question: the
pipeline masks everything before any AI step. Audio cannot be masked before
sending, so admins should not dictate customer data.

Cache: transcripts are stored as <sha256>.txt in the git-ignored audio cache,
so the same recording is never sent twice.

Self-test:  .venv/Scripts/python -m privacy.stt
"""
import hashlib
from pathlib import Path

import requests

from privacy import tts

# ============================================================
# TUNABLE SETTINGS
# ============================================================
STT_MODEL_ID = "scribe_v1"     # ElevenLabs speech-to-text model.
LANGUAGE_CODE = "en"           # Hint for the spoken language; None = auto-detect
                               # (slightly slower, handles mixed languages).
MAX_AUDIO_BYTES = 5_000_000    # Refuse bigger recordings (~5 min of mp3): long
                               # audio costs more credits. Raise if needed.
REQUEST_TIMEOUT_S = 60         # Give up on ElevenLabs after this many seconds.

# ============================================================
# FIXED SETTINGS
# ============================================================
AUDIO_CACHE_DIR = tts.AUDIO_CACHE_DIR
API_URL = tts.API_URL


def cache_path(audio):
    """<sha256 of audio + model + language>.txt"""
    digest = hashlib.sha256(f"{STT_MODEL_ID}|{LANGUAGE_CODE}|".encode() + audio).hexdigest()
    return AUDIO_CACHE_DIR / f"{digest}.txt"


def transcribe(audio, filename="question.wav"):
    """Return the text spoken in audio (bytes or a file path). Order: cache -> ElevenLabs."""
    if isinstance(audio, (str, Path)):
        filename, audio = Path(audio).name, Path(audio).read_bytes()
    if not audio:
        raise ValueError("STT: empty audio")
    if len(audio) > MAX_AUDIO_BYTES:
        raise ValueError(f"STT: audio is {len(audio):,} bytes, limit is MAX_AUDIO_BYTES={MAX_AUDIO_BYTES:,}")
    path = cache_path(audio)
    if path.exists():
        return path.read_text(encoding="utf-8")
    key = tts.stt_api_key()
    if not key:
        raise RuntimeError(f"STT: set {tts.STT_API_KEY_ENV} (or {tts.API_KEY_ENV}) first")
    data = {"model_id": STT_MODEL_ID}
    if LANGUAGE_CODE:
        data["language_code"] = LANGUAGE_CODE
    r = requests.post(f"{API_URL}/speech-to-text", headers={"xi-api-key": key},
                      data=data, files={"file": (filename, audio)}, timeout=REQUEST_TIMEOUT_S)
    if r.status_code != 200:
        raise RuntimeError(f"STT: ElevenLabs returned HTTP {r.status_code}")   # body not echoed
    text = r.json().get("text", "").strip()
    AUDIO_CACHE_DIR.mkdir(exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return text


def _self_test():
    ok = True

    def report(name, passed, note=""):
        nonlocal ok
        ok &= passed is not False
        print(f"{name:42} {'PASS' if passed else ('SKIP' if passed is None else 'FAIL')}  {note}")

    key, source = tts._key_status(tts.STT_API_KEY_ENV)
    print(f"STT key: {'set' if key else 'MISSING'}  (from {source})\n")

    for name, audio in (("empty audio refused", b""), ("oversized audio refused", b"0" * (MAX_AUDIO_BYTES + 1))):
        try:
            transcribe(audio); report(name, False, "not refused")
        except ValueError:
            report(name, True)

    fake = b"fake recording for the cache test"          # planted transcript: no API call allowed
    p = cache_path(fake)
    AUDIO_CACHE_DIR.mkdir(exist_ok=True)
    p.write_text("cached question", encoding="utf-8")
    try:
        report("cache: hit returns text without API call", transcribe(fake) == "cached question")
    finally:
        p.unlink()

    # Live test: transcribe the clip the TTS self-test produced (already on disk).
    clip = tts.cache_path("Index on t_438 recommended.")
    if not key:
        report("ElevenLabs transcription", None, f"no {tts.STT_API_KEY_ENV} set")
    elif not clip.exists():
        report("ElevenLabs transcription", None, "run python -m privacy.tts first (makes the test clip)")
    else:
        cached = cache_path(clip.read_bytes()).exists()
        text = transcribe(clip)
        report("ElevenLabs transcription", "index" in text.lower() and "recommend" in text.lower(),
               f"heard {text!r}" + (" (from cache)" if cached else " (new call)"))
        report("cache: second call reuses the transcript", transcribe(clip) == text)
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if _self_test() else 1)
