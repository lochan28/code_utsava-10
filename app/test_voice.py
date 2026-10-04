"""Tests for the voice features: a spoken question in (STT), a spoken debrief out (TTS). No network:
privacy.stt.transcribe and privacy.tts.speak_debrief are mocked, and st.audio_input is faked.

Run:  .venv/Scripts/python -m app.test_voice      (from the project folder)
"""
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path

import requests
import streamlit as st
from streamlit.testing.v1 import AppTest

import pipeline
from app import demo_mode, llm, voice
from app import test_recommendations as tr
from app.test_approve import approve_result, page_with, rig_targets
from app.test_demo_mode import boom
from app.test_privacy import fresh_audit_log, patched
from app.test_recommendations import page_text
from app.test_restore import LAST, SENSITIVE, run_tripwire
from app.test_support import AI_STAND_INS
from privacy import stt, tts

QUESTION = "Why is the weekly sales report slow?"


class Clip:
    """What st.audio_input returns: an object with getvalue()."""
    def __init__(self, data):
        self.data = data

    def getvalue(self):
        return self.data


def recorder(texts):
    """A fake stt.transcribe: returns (or raises) the next item of `texts`, and remembers what it was given."""
    calls = []

    def transcribe(audio, filename="question.wav"):
        calls.append(audio)
        out = texts[min(len(calls) - 1, len(texts) - 1)]
        if isinstance(out, Exception):
            raise out
        return out
    return transcribe, calls


@contextmanager
def voice_page(transcribe, analyze, clip=b"clip", state=None, extra=(), real_widget=False):
    """The dashboard with a fake recording, a fake STT and a fake analyze(). Yields the AppTest, already run."""
    with ExitStack() as stack:
        stack.enter_context(fresh_audit_log())
        for obj, name, value in [(llm, "_call_ollama", lambda p: (_ for _ in ()).throw(llm.LLMUnavailable("off"))),
                                 (llm, "start_warmup", lambda: None), (pipeline, "analyze", analyze),
                                 (stt, "transcribe", transcribe), *extra,
                                 *([] if real_widget else [(st, "audio_input", lambda *a, **k: Clip(clip))])]:
            stack.enter_context(patched(obj, name, value))
        at = AppTest.from_file(tr.DASHBOARD, default_timeout=30)
        for k, v in (state or {}).items():
            at.session_state[k] = v
        at.run()
        assert not at.exception, [e.value for e in at.exception]
        yield at


# ---------------------------------------------------------------- the helpers
def test_each_recording_is_transcribed_once_even_though_the_page_reruns():
    fake, calls = recorder([QUESTION])
    state = {}
    with patched(stt, "transcribe", fake):
        assert voice.transcribe_once(b"clip-1", state) == (QUESTION, None)
        assert voice.transcribe_once(b"clip-1", state) == (None, None)       # a rerun: nothing happens
        assert voice.transcribe_once(b"clip-1", state) == (None, None)
        assert voice.transcribe_once(b"clip-2", state) == (QUESTION, None)   # a NEW recording is handled
    assert calls == [b"clip-1", b"clip-2"]


def test_stt_problems_give_a_note_and_never_raise():
    for problem in (RuntimeError("STT: set the key"), ValueError("STT: empty audio"),
                    requests.exceptions.ConnectionError("no network")):
        fake, _ = recorder([problem])
        state = {}
        with patched(stt, "transcribe", fake):
            assert voice.transcribe_once(b"x", state) == (None, voice.STT_UNAVAILABLE), problem
            assert voice.transcribe_once(b"x", state) == (None, voice.STT_UNAVAILABLE)   # the note stays; no retry loop
    fake, _ = recorder(["   "])
    with patched(stt, "transcribe", fake):
        assert voice.transcribe_once(b"y", {}) == (None, None)                            # an empty transcript asks nothing
    assert voice.transcribe_once(b"", {}) == (None, None)


def test_the_debrief_is_spoken_from_a_masked_view_without_the_unmasked_keys():
    got = []
    r = approve_result()
    r["deploy_rewrites"], r["explanation_unmasked"] = ["real rewrite"], "real names"
    with patched(tts, "speak_debrief", lambda result, mapping=None: got.append(result) or Path("x.mp3")):
        assert voice.debrief_audio(r) == ("x.mp3", None)
    sent = got[0]
    assert not set(voice.UNMASKED_KEYS) & set(sent), set(voice.UNMASKED_KEYS) & set(sent)
    assert sent["recommendations"] == r["recommendations"] and sent["leak_check"] == r["leak_check"]
    assert "deploy_scripts" in r and "real names" not in str(sent) and "real rewrite" not in str(sent)


def test_tts_problems_give_a_note_and_never_raise():
    for problem in (RuntimeError("TTS: HTTP 401"), ValueError("TTS blocked by the privacy leak check"),
                    requests.exceptions.Timeout("slow")):
        def broken(result, mapping=None, problem=problem):
            raise problem
        with patched(tts, "speak_debrief", broken):
            assert voice.debrief_audio(approve_result()) == (None, voice.TTS_UNAVAILABLE), problem


# ---------------------------------------------------------------- the page
def test_a_spoken_question_is_analysed_once_and_shown_as_the_admins_question():
    asked = []
    fake, calls = recorder([QUESTION])
    with voice_page(fake, lambda q: asked.append(q) or approve_result()) as at:
        at.run()
        at.run()                                            # more reruns: the same recording is not analysed again
        assert asked == [QUESTION] and calls == [b"clip"], (asked, calls)
        assert [t.value for t in at.text if t.value == QUESTION] == [QUESTION]


def test_a_failed_transcription_leaves_a_quiet_note_and_no_analysis():
    asked = []
    fake, _ = recorder([RuntimeError("STT: set the key")])
    with voice_page(fake, lambda q: asked.append(q)) as at:
        assert asked == [] and voice.STT_UNAVAILABLE in [c.value for c in at.caption]
        assert not at.error                                  # quiet: no red box


def test_the_page_says_where_the_recording_goes_and_has_the_recorder():
    fake, _ = recorder([RuntimeError("off")])
    with voice_page(fake, lambda q: None, real_widget=True) as at:
        assert any("ElevenLabs" in c.value and "customer data" in c.value for c in at.caption)
        assert at.get("audio_input"), "the recorder widget is missing"


def test_play_debrief_shows_an_audio_player_and_a_failure_falls_back_to_text():
    mp3 = Path(tempfile.mkdtemp()) / "debrief.mp3"
    mp3.write_bytes(b"ID3fake")
    with page_with(approve_result()) as at:
        assert not at.get("audio")                                     # nothing before the click
        with patched(tts, "speak_debrief", lambda result, mapping=None: mp3):
            [b for b in at.button if b.label == "Play debrief"][0].click().run()
        assert not at.exception and len(at.get("audio")) == 1
        at.run()
        assert len(at.get("audio")) == 1                               # it stays on later reruns
    with page_with(approve_result()) as at:
        def blocked(result, mapping=None):
            raise ValueError("TTS blocked by the privacy leak check")
        errors_before = [e.value for e in at.error]                    # (the sample has an unsafe-rewrite card: a red box)
        with patched(tts, "speak_debrief", blocked):
            [b for b in at.button if b.label == "Play debrief"][0].click().run()
        assert not at.exception and not at.get("audio") and [e.value for e in at.error] == errors_before
        assert voice.TTS_UNAVAILABLE in [c.value for c in at.caption]


def test_voice_works_in_demo_mode_and_touches_nothing_live():
    fake, _ = recorder([QUESTION])
    live = [(llm, "_post", boom), *rig_targets()]                      # sandbox, connections, Ollama HTTP: all explode
    with voice_page(fake, boom, state={demo_mode.DEMO_MODE_KEY: True}, extra=live) as at:
        assert demo_mode.LABEL in [i.value for i in at.info]
        assert "Weekly sales report by region" in page_text(at)


# ---------------------------------------------------------------- the leak tests
def test_a_debrief_made_from_a_real_analysis_has_no_real_name_or_value():
    to_ai, sandbox_saw, result_text, audit, deploy = run_tripwire(pipeline)    # a query full of real values
    spoken = tts.debrief_text(voice.masked_view(LAST["result"]))
    assert spoken and "Analysis complete" in spoken
    for value in SENSITIVE + ["customers", "orders", "name", "email"]:
        assert value.lower() not in spoken.lower(), value
    tts.check_text(spoken)                                                  # the real leak check passes, no network
    assert "Rahul" in deploy                                                # the real values do exist, elsewhere


def test_a_question_with_a_name_never_reaches_the_debrief():
    with ExitStack() as stack:
        stack.enter_context(fresh_audit_log())
        for obj, name, value in [(llm, "_call_ollama", lambda p: (_ for _ in ()).throw(llm.LLMUnavailable("off"))),
                                 (llm, "start_warmup", lambda: None), *AI_STAND_INS]:
            stack.enter_context(patched(obj, name, value))
        r = pipeline.analyze("why is Rahul Sharma's weekly sales report slow")
    spoken = tts.debrief_text(voice.masked_view(r))
    assert "rahul" not in spoken.lower() and "sharma" not in spoken.lower()
    tts.check_text(spoken)


def run_all():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            print(f"RUN   {name}")
            fn()
            print(f"PASS  {name}")
        except Exception as e:                       # noqa: BLE001 - report every failure
            failed += 1
            print(f"FAIL  {name}: {type(e).__name__}: {str(e)[:300]}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if run_all() else 0)
