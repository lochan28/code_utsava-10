"""Voice for the dashboard: a spoken question in, a spoken debrief out. Online (ElevenLabs, via privacy/).

Both directions FAIL SOFT: any problem (no key, HTTP error, blocked by the leak check, no network) gives a
plain-text note and the page carries on with text. Nothing here raises to the page.

Privacy:
- the recording is sent to ElevenLabs as raw audio (the team's decision; it cannot be leak-checked), so the
  page tells the admin not to dictate customer data. Only the transcript enters analyze(), where it is
  handled exactly like a typed question;
- the debrief is spoken from a MASKED view of the result: the un-masked keys (deploy_*, explanation_unmasked)
  are removed first, so they can never be read by anything below, now or after a future change there.
"""
import hashlib

from privacy import stt, tts

UNMASKED_KEYS = ("deploy_sql", "deploy_scripts", "deploy_rewrites", "explanation_unmasked")
STT_UNAVAILABLE = "Voice input isn't available right now. Type the question instead."
TTS_UNAVAILABLE = "Audio isn't available right now. The text above has the same information."
# RuntimeError = no key or an HTTP error; ValueError = blocked by the leak check (or empty / too big);
# OSError = no network (requests' exceptions are OSErrors).
SOFT_FAILURES = (RuntimeError, ValueError, OSError)


def masked_view(result):
    """The result without the keys that hold real names or values."""
    return {k: v for k, v in result.items() if k not in UNMASKED_KEYS}


def transcribe_once(data, state):
    """(text or None, note or None) for one recording. Each distinct recording is handled ONCE: the page
    reruns on every click and the audio widget keeps its value, so `state` (a dict-like, e.g.
    st.session_state) remembers the hash of the last one handled."""
    if not data:
        return None, None
    digest = hashlib.sha256(data).hexdigest()
    if state.get("voice_done") == digest:
        return None, state.get("voice_note")
    state["voice_done"] = digest
    try:
        text = stt.transcribe(data)
    except SOFT_FAILURES:
        state["voice_note"] = STT_UNAVAILABLE
        return None, STT_UNAVAILABLE
    state["voice_note"] = None
    text = (text or "").strip()
    return (text or None), None


def debrief_audio(result):
    """(path of an .mp3 or None, note or None) for the spoken debrief of one analysis."""
    try:
        return str(tts.speak_debrief(masked_view(result))), None
    except SOFT_FAILURES:
        return None, TTS_UNAVAILABLE
