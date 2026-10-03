"""Tests for the real Ollama call in app/llm.py, against a FAKE local server.

Run:  .venv/Scripts/python -m app.test_llm_http      (from the project folder)

No real Ollama is needed: a tiny HTTP server on 127.0.0.1 pretends to be Ollama's
/api/generate. It can answer normally, answer slowly, answer with a leaked value,
return an error, or be switched off entirely.
"""
import json
import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app import llm
from app.test_privacy import checker, everything_logged, fresh_audit_log, patched, sample

GOOD = "The table is scanned fully; add the index."


class FakeOllama:
    """Pretends to be Ollama. Set .mode and .delay between requests."""

    def __init__(self):
        self.mode, self.delay, self.requests = "normal", 0.0, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"path": self.path, "body": body})
                time.sleep(outer.delay)
                code, payload = outer.answer()
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                try:
                    self.send_response(code)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:          # the client already gave up (timeout test)
                    pass

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def answer(self):
        def ok(text):
            return 200, {"response": text, "eval_count": 40, "eval_duration": 2_000_000_000}
        return {
            "normal": lambda: ok(GOOD),
            "leaky": lambda: ok("Look at the orders table for Rahul."),
            "fenced": lambda: ok("```sql\nCREATE INDEX ON t_a3f (c_91b);\n```\n" + GOOD),
            "code only": lambda: ok("```sql\nSELECT 1\n```"),
            "no model": lambda: (404, {"error": "model not found, try pulling it first"}),
            "garbage": lambda: (200, b"this is not json"),
        }[self.mode]()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@contextmanager
def fake_ollama(mode="normal", delay=0.0):
    srv = FakeOllama()
    srv.mode, srv.delay = mode, delay
    llm._reset_state()
    try:
        with patched(llm, "OLLAMA_URL", srv.url), fresh_audit_log() as path:
            yield srv, path
    finally:
        srv.close()


def closed_port_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                       # nothing listens here any more
    return f"http://127.0.0.1:{port}"


# ---------------------------------------------------------------- (a) normal reply
def test_a_normal_reply_and_exact_request():
    with fake_ollama("normal") as (srv, path):
        out = llm.explain(sample(), checker)
        entry = llm.get_audit_log()[-1]
        req = srv.requests[0]
        print(f"      source={out['source']} stats={out['stats']} latency={out['latency_ms']}ms "
              f"status={llm.llm_status()['status']}")
        print(f"      request options: {req['body']['options']} keep_alive={req['body']['keep_alive']} "
              f"stream={req['body']['stream']} model={req['body']['model']}")
        assert (out["source"], out["text"], out["reply_leak_ok"]) == ("ollama", GOOD, True)
        assert out["stats"] == {"tokens": 40, "tokens_per_s": 20.0}
        assert req["path"] == "/api/generate"
        b = req["body"]
        assert (b["keep_alive"], b["stream"], b["model"]) == (-1, False, llm.MODEL)
        assert b["options"] == {"temperature": llm.TEMPERATURE, "num_predict": llm.MAX_REPLY_TOKENS,
                                "num_ctx": llm.NUM_CTX}
        assert b["prompt"] == entry["prompt"], "what was sent differs from what was logged"
        assert entry["sent"] is True and entry["event"] == "llm_call"
        assert llm.llm_status()["status"] == "live LLM"


# ---------------------------------------------------------------- (b) slow reply times out
def test_b_slow_reply_times_out_and_falls_back():
    with fake_ollama("normal", delay=3.0) as (srv, path), patched(llm, "TIMEOUT_S", 0.5):
        t0 = time.perf_counter()
        out = llm.explain(sample(), checker)
        took = time.perf_counter() - t0
        print(f"      gave up after {took:.2f}s, reason: {out['fallback_reason']}")
        assert out["source"] == "template" and "no reply within" in out["fallback_reason"]
        assert 0.4 <= took < 2.5, took
        assert llm.get_audit_log()[-1]["sent"] is True       # it did reach the server
        assert llm.llm_status()["status"] == "template fallback"


# ---------------------------------------------------------------- (c) leaky reply is blocked
def test_c_leaky_reply_is_blocked_and_not_stored():
    with fake_ollama("leaky") as (srv, path):
        out = llm.explain(sample(), checker)
        entry = llm.get_audit_log()[-1]
        print(f"      source={out['source']} reply_leak_ok={out['reply_leak_ok']} audit reply={entry['reply']}")
        assert (out["source"], out["reply_leak_ok"]) == ("template", False)
        assert checker(out["text"]) and entry["reply"].startswith("[BLOCKED")
        assert "rahul" not in everything_logged(path)
        assert llm.llm_status()["status"] == "live LLM"       # the LLM works; its answer was refused


# ---------------------------------------------------------------- (d) server down
def test_d_server_down_falls_back_fast():
    with fake_ollama("normal") as (srv, path), patched(llm, "OLLAMA_URL", closed_port_url()):
        t0 = time.perf_counter()
        out = llm.explain(sample(), checker)
        took = time.perf_counter() - t0
        print(f"      took {took:.2f}s, reason: {out['fallback_reason']}")
        assert out["source"] == "template" and "not running" in out["fallback_reason"]
        assert llm.get_audit_log()[-1]["sent"] is False
        assert took < 3
        st = llm.llm_status()
        assert st["status"] == "template fallback" and "not running" in st["reason"]


# ---------------------------------------------------------------- more failure modes
def test_e_model_not_downloaded_says_how_to_fix_it():
    with fake_ollama("no model") as (srv, path):
        out = llm.explain(sample(), checker)
        assert out["source"] == "template" and f"ollama pull {llm.MODEL}" in out["fallback_reason"]


def test_f_unreadable_reply_falls_back():
    with fake_ollama("garbage") as (srv, path):
        out = llm.explain(sample(), checker)
        assert out["source"] == "template" and "could not be read" in out["fallback_reason"]


def test_g_code_blocks_are_removed_before_display():
    with fake_ollama("fenced") as (srv, path):
        out = llm.explain(sample(), checker)
        print(f"      shown to admin: {out['text']!r}")
        assert out["source"] == "ollama" and out["text"] == GOOD
        assert "CREATE INDEX" not in out["text"] and "```" not in out["text"]
    with fake_ollama("code only") as (srv, path):
        out = llm.explain(sample(), checker)
        assert out["source"] == "template" and "empty after removing code" in out["fallback_reason"]
    assert llm.clean_reply("a ```sql\nDROP x") == "a"           # unclosed fence is removed too


# ---------------------------------------------------------------- warm-up and status
def test_h_status_starts_unchecked():
    llm._reset_state()
    st = llm.llm_status()
    assert st["status"] == "template fallback" and st["reason"] == "not checked yet" and st["model"] == llm.MODEL


def test_i_warmup_runs_in_background_and_only_once():
    with fake_ollama("normal", delay=0.6) as (srv, path):
        t0 = time.perf_counter()
        llm.start_warmup()
        llm.start_warmup()                                       # second call must do nothing
        returned_in = time.perf_counter() - t0
        assert returned_in < 0.3, f"start_warmup blocked for {returned_in:.2f}s"
        assert llm.llm_status()["status"] == "warming up"
        deadline = time.time() + 5
        while llm.llm_status()["status"] == "warming up" and time.time() < deadline:
            time.sleep(0.05)
        st = llm.llm_status()
        print(f"      returned in {returned_in * 1000:.0f}ms, then status -> {st['status']} ({st['reason']})")
        assert st["status"] == "live LLM"
        assert len(srv.requests) == 1, "warm-up ran more than once"
        body = srv.requests[0]["body"]
        assert body["prompt"] == llm.WARMUP_PROMPT and body["keep_alive"] == -1
        assert body["options"]["num_ctx"] == llm.NUM_CTX          # same as real calls: no model reload
        ev = [e for e in llm.get_audit_log() if e["event"] == "warmup"]
        assert len(ev) == 1 and ev[0]["sent"] is True and ev[0]["prompt"] == llm.WARMUP_PROMPT


def test_j_warmup_with_no_ollama_reports_fallback():
    with fake_ollama("normal") as (srv, path), patched(llm, "OLLAMA_URL", closed_port_url()):
        llm.start_warmup()
        deadline = time.time() + 5
        while llm.llm_status()["status"] == "warming up" and time.time() < deadline:
            time.sleep(0.05)
        st = llm.llm_status()
        print(f"      status -> {st['status']} ({st['reason']})")
        assert st["status"] == "template fallback" and "not running" in st["reason"]


def test_k_model_comes_from_environment_variable():
    import importlib
    import os
    old = os.environ.get("OLLAMA_MODEL")
    try:
        os.environ["OLLAMA_MODEL"] = "qwen2.5:3b"
        assert importlib.reload(llm).MODEL == "qwen2.5:3b"
        os.environ.pop("OLLAMA_MODEL")
        assert importlib.reload(llm).MODEL == "qwen2.5:7b"
    finally:
        if old is None:
            os.environ.pop("OLLAMA_MODEL", None)
        else:
            os.environ["OLLAMA_MODEL"] = old
        importlib.reload(llm)


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            print(f"RUN   {name}")
            fn()
            print(f"PASS  {name}")
        except Exception as e:                       # noqa: BLE001 - report every failure
            failed += 1
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
