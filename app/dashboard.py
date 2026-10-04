"""Streamlit dashboard.  Owner: Person 4.  STATUS: Step 4b (chat, badges, masked query
and plan, bottleneck card, recommendations with honesty badges).  4c adds charts and
4d the Approve script.

EVERYTHING on this page is MASKED data. This file never calls the decoder (unmask):
the un-masked version belongs to the Approve step only.

Run:  .venv/Scripts/streamlit run app/dashboard.py
"""
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so "import pipeline" works
import pipeline  # noqa: E402
from app import approve, charts, demo_mode, llm, sample_data, tradeoff, voice  # noqa: E402
from app.ui_helpers import (  # noqa: E402
    ai_inputs_line, bottleneck_items, bottleneck_node_id, confidence_counts, confidence_strip,
    evidence_items, gnn_top3_rows, match_line, md_safe, novelty_badge, plan_tree_text, sort_recommendations,
    second_opinion_line, tier, verified_badge)

# ============================================================
# TUNABLE SETTINGS
# ============================================================
BADGE_REFRESH = "3s"   # How often the "LLM status" badge re-checks on its own, so it turns
                       # green when the model finishes loading. Lower = snappier, a little
                       # more work in the background.
PAGE_TITLE = "AI Database Tuner"

st.set_page_config(page_title=PAGE_TITLE, page_icon=":material/database:", layout="wide")

# Demo mode: answers come from RECORDED real analyses (app/demo_mode.py). No Ollama, no database,
# no sandbox. The switch only sets st.session_state["demo_mode"]; everything below reads it.
with st.sidebar:
    st.toggle("Demo mode (recorded results)", key=demo_mode.DEMO_MODE_KEY, value=False,
              help="Answers the demo questions from stored real runs. Uses no database and no Ollama.")
demo_on = bool(st.session_state.get(demo_mode.DEMO_MODE_KEY))
if not demo_on:
    llm.start_warmup()   # load the model in the background at app start; only the first call acts


# ------------------------------------------------------------------
# Header: LLM status badge (refreshes itself)
# ------------------------------------------------------------------
@st.fragment(run_every=BADGE_REFRESH)
def llm_badge():
    s = llm.llm_status()
    color = {"live LLM": "green", "warming up": "orange"}.get(s["status"], "gray")
    st.badge(f"LLM: {s['status']} · {s['model']}", icon=":material/smart_toy:", color=color,
             help=s["reason"])
    if s["status"] != "live LLM":
        st.caption(s["reason"])


head_left, head_right = st.columns([3, 1], vertical_alignment="center")
head_left.title(PAGE_TITLE)
head_left.caption("Finds slow queries and suggests fixes. The AI only ever sees masked data.")
with head_right:
    if demo_on:
        st.badge("Demo mode · recorded results", icon=":material/movie:", color="violet",
                 help="No database and no Ollama are used.")
    else:
        llm_badge()


MASKED_NOTE = ("Masked view: table and column names are hashed and values are replaced. "
               "The un-masked version is only shown in the Approve step.")


# ------------------------------------------------------------------
# Bottleneck card
# ------------------------------------------------------------------
def render_bottleneck(b):
    items = bottleneck_items(b)           # only the keys that exist; never crashes
    with st.container(border=True):
        st.markdown("**Bottleneck** · the step that costs the most")
        if not items:
            st.caption("No bottleneck details available.")
            return
        d = dict(items)
        st.markdown("#### " + md_safe(d.get("step", "Plan step") + (f" on {d['table']}" if "table" in d else "")))
        facts = [f"{k}: **{md_safe(v)}**" for k, v in items if k not in ("step", "table", "why", "found by")]
        if facts:
            st.markdown("  ·  ".join(facts))
        if "found by" in d:
            st.badge(f"found by: {d['found by']}", icon=":material/search:",
                     color="violet" if "GNN" in d["found by"] else "gray")
        opinion = second_opinion_line(b)
        if opinion:
            st.badge(opinion[0], icon=":material/psychology:", color=opinion[1])
        if "why" in d:
            st.markdown(md_safe(d["why"]))
        rows = gnn_top3_rows(b)
        if rows:
            st.caption("The GNN's highest-rated steps (violet = the one picked)")
            st.altair_chart(charts.gnn_top3_chart(rows, bottleneck_node_id(b)), width="stretch")


# ------------------------------------------------------------------
# Recommendations: one bordered row each, with honesty badges
# ------------------------------------------------------------------
REC_COLUMNS = [1.3, 3, 3, 2.6]      # type | fix | reason | evidence


def render_rec(rec, top):
    t = tier(rec)
    with st.container(border=True):
        if t == "unsafe":             # loud, and impossible to read as a good fix
            warning = rec.get("warning") if isinstance(rec.get("warning"), str) else \
                "This rewrite returns different rows than the original query. Do not deploy."
            st.error(f"**UNSAFE: results differ.** {md_safe(warning)}", icon=":material/dangerous:")
        c_type, c_fix, c_reason, c_ev = st.columns(REC_COLUMNS)
        with c_type:
            st.markdown(f"**{md_safe(str(rec.get('kind') or '?').upper())}**")
            label, color = verified_badge(rec)
            st.badge(label, color=color)
            nb = novelty_badge(rec)
            if nb:
                st.badge(nb[0], color=nb[1])
            if rec.get("combined"):
                st.badge("combined fix", icon=":material/layers:", color="gray")
            if top:
                st.badge("top pick", icon=":material/star:", color="green")
        with c_fix:
            action = rec.get("action")
            if isinstance(action, str) and action.strip() and rec.get("combined"):
                for number, step in enumerate(action.split("; "), 1):      # a combined fix: show BOTH steps
                    st.caption(f"Step {number} of 2" if number <= 2 else f"Step {number}")
                    st.code(step, language="sql")
            elif isinstance(action, str) and action.strip():
                st.code(action, language="sql")
            if isinstance(rec.get("rewrite_sql"), str) and rec["rewrite_sql"].strip():     # timed together with the fix
                rule = rec.get("rewrite_rule") if isinstance(rec.get("rewrite_rule"), str) else ""
                st.caption("Also rewrite the query" + (f" ({md_safe(rule)})" if rule else "") + ":")
                st.code(rec["rewrite_sql"], language="sql")
            else:
                st.caption("No fix text (the masked SQL could not be analysed).")
        with c_reason:
            reason = rec.get("reason")
            st.markdown(md_safe(reason) if isinstance(reason, str) and reason else "No reason given.")
        with c_ev:
            items = evidence_items(rec)
            if items:
                st.markdown("\n".join(f"- {md_safe(k)}: **{md_safe(v)}**" for k, v in items))
            else:
                st.warning("No evidence attached", icon=":material/report:")   # never hidden
        notes = [(label, rec[key]) for label, key in (("Summary", "summary"), ("Why not the alternative", "why_not"))
                 if isinstance(rec.get(key), str) and rec[key]]
        if notes:
            with st.expander("Summary and why not the alternatives"):
                for label, text in notes:
                    st.markdown(f"**{label}:** {md_safe(text)}")


def render_recommendations(recs, ai_inputs):
    st.markdown("#### Recommendations")
    st.caption(MASKED_NOTE)
    ranked = sort_recommendations(recs)   # measured, simulated, rule-only, unverified, unsafe last
    if not ranked:
        st.info("No recommendations were returned for this query.")
        return
    st.markdown("**Confidence:** " + confidence_strip(confidence_counts(ranked)))
    line = ai_inputs_line(ai_inputs)      # status words only; anything else is left out
    if line:
        st.caption(line)
    for col, text in zip(st.columns(REC_COLUMNS), ["Type", "Fix (masked SQL)", "Reason", "Evidence"]):
        col.caption(text)
    for i, rec in enumerate(ranked):
        render_rec(rec, top=(i == 0 and tier(rec) != "unsafe"))
    if all(tier(r) == "unsafe" for r in ranked):
        st.error("No safe fix was found: every suggested rewrite returned different results.")


# ------------------------------------------------------------------
# Before / after: charts, the 50% target, and the price of the fix
# ------------------------------------------------------------------
def target_marker(status, worse_text):
    """The PS target (more than 50% improvement): green check if met, amber if not, red if worse."""
    target = f"{tradeoff.TARGET_PCT:.0f}%"
    if status == "met":
        st.success(f"Meets the target: more than {target} improvement.", icon=":material/check_circle:")
    elif status == "below":
        st.warning(f"Below the {target} target.", icon=":material/info:")
    elif status == "none":
        st.warning("No improvement.", icon=":material/info:")
    elif status == "worse":
        st.error(worse_text, icon=":material/trending_down:")
    else:
        st.info("Improvement could not be worked out.")


def render_cost_panel(c, time=None):
    """The planner's estimate. If a MEASURED time exists, the 50% target marker is NOT shown here (the
    measured headline carries it) and the bars are gray; if the measurement says the opposite, a neutral
    amber note says to trust it."""
    with st.container(border=True):
        st.markdown("**Planner cost** · the planner's estimate, not a stopwatch")
        if c is None:
            st.info(f"Cost: {tradeoff.NOT_MEASURED}. No before/after cost numbers for the top pick.")
            return
        opposite = tradeoff.disagree(c, time)
        st.metric("Estimated cost improvement", tradeoff.fmt_pct(c["pct"]))
        if opposite:
            st.warning("The planner's estimate points the other way from the measured time. Trust the "
                       "measured time: planner estimates are often unreliable, especially for rewrites.",
                       icon=":material/info:")
        elif time is None:                 # a measured time exists -> it alone says met / below / worse
            target_marker(c["status"], "WORSE: this fix is estimated to make the query MORE expensive.")
        st.altair_chart(charts.before_after_chart(c["before"], c["after"], "planner cost units (estimate)",
                                                  status="none" if (opposite or time is not None) else c["status"]),
                        width="stretch")
        st.caption(f"Source: {c['source']}")


def render_time_panel(t):
    with st.container(border=True):
        st.markdown("**Execution time** · measured on a sandbox copy")
        if t is None:
            st.info("Not measured, cost estimate only.")      # never an empty chart
            return
        st.metric("Measured time improvement", tradeoff.fmt_pct(t["pct"]))
        target_marker(t["status"], "WORSE: this fix made the query SLOWER when measured.")
        st.altair_chart(charts.before_after_chart(t["before"], t["after"], "measured milliseconds",
                                                  status=t["status"], label_format=",.1f"),
                        width="stretch")
        st.caption(f"{tradeoff.fmt_ms(t['before'])} → {tradeoff.fmt_ms(t['after'])} · Source: {t['source']}")
        if t.get("note"):
            st.caption(md_safe(t["note"]))


def render_price_cards(n):
    """What the fix costs: write latency and storage, each with its source. A missing number
    is shown as 'not measured', never as 0."""
    st.markdown("#### What the fix costs")
    c_write, c_storage = st.columns(2)
    with c_write, st.container(border=True):
        if n["write_note"]:
            st.metric("Write latency", "none")
            st.caption(n["write_note"])
        else:
            st.metric("Write latency", tradeoff.fmt_write(n["write"]))
            st.caption(f"Source: {tradeoff.SOURCE_MEASURED} on a sandbox copy" if tradeoff.ok(n["write"])
                       else f"Source: {tradeoff.SOURCE_NONE}")
    with c_storage, st.container(border=True):
        if n["storage_note"]:
            st.metric("Extra storage", "none")
            st.caption(n["storage_note"])
        else:
            st.metric("Extra storage", tradeoff.fmt_storage(n["storage"]))
            st.caption(f"Source: {tradeoff.SOURCE_MEASURED} on a sandbox copy" if tradeoff.ok(n["storage"])
                       else f"Source: {tradeoff.SOURCE_NONE}")


def render_tradeoff(n):
    """The price next to the benefit, for the top pick, in one small view."""
    kind = str(n["top"].get("kind") or "?").upper() if n["top"] else "?"
    gain = []
    measured_first = n["time"] is not None            # the measurement is the headline: show only it
    for part, lower, higher, none_text in (
            (n["cost"], "lower estimated cost", "HIGHER estimated cost (worse)", "no change in estimated cost"),
            (n["time"], "faster (measured)", "SLOWER (measured, worse)", "no change in measured time")):
        if measured_first and part is n["cost"]:
            continue
        if part and tradeoff.ok(part["pct"]):
            p = part["pct"]
            gain.append(f"{p:.1f}% {lower}" if p > 0 else none_text if p == 0
                        else f":red[{abs(p):.1f}% {higher}]")
    write = n["write_note"] or tradeoff.fmt_write(n["write"])
    storage = n["storage_note"] or tradeoff.fmt_storage(n["storage"])
    with st.container(border=True):
        st.markdown(f"**Trade-off for the top pick ({md_safe(kind)})**: what you gain and what you pay")
        g, w, s = st.columns(3)
        g.markdown("**You gain**  \n" + ("  \n".join(gain) if gain else tradeoff.NOT_MEASURED))
        w.markdown("**You pay in writes**  \n" + md_safe(write))
        s.markdown("**You pay in storage**  \n" + md_safe(storage))


def render_before_after(r):
    n = tradeoff.resolve(r)
    st.markdown("#### Before and after")
    if n["no_safe"]:        # no celebration: only the warning
        st.error("No improvement is shown: every suggested fix returns different results.",
                 icon=":material/dangerous:")
        return
    if n["time"] is not None:
        # A MEASURED time is the headline result, and the 50% target is checked against it.
        # The planner's estimate moves into a collapsed expander: it can disagree with the stopwatch.
        render_time_panel(n["time"])
        with st.expander("Planner estimate (can disagree with measured time)"):
            render_cost_panel(n["cost"], n["time"])
    else:                                     # nothing measured: cost estimate only, as before
        left, right = st.columns(2)
        with left:
            render_cost_panel(n["cost"])
        with right:
            render_time_panel(n["time"])
    render_price_cards(n)
    render_tradeoff(n)
    if n["rejected"]:                         # neutral note: the AI already dropped these, nothing to act on
        st.info("**Dropped by the AI** (they returned different results in the sandbox):" + chr(10)
                + chr(10).join(f"- {md_safe(x)}" for x in n["rejected"]), icon=":material/info:")


# ------------------------------------------------------------------
# The Approve step: the ONLY place the un-masked SQL appears
# ------------------------------------------------------------------
def render_approve(r, key):
    """A button that SHOWS the script for the top pick. It never runs anything: no database
    call, no sandbox call, no decoder call. Before the click the un-masked SQL is not on the
    page at all; after it, it appears only inside the script below."""
    st.markdown("#### Approve")
    top = tradeoff.top_recommendation(r.get("recommendations"))
    if top is None:
        st.caption("No safe fix to approve.")
        return
    sql = approve.sql_for(r, top)                  # text only; nothing is rendered from it yet
    if sql is None:
        st.caption("No script is available for this fix.")
        return
    st.caption("Approving only SHOWS the script, for you to review. It does not run anything.")
    if st.button("Approve and show the script", key=f"approve_{key}", type="primary"):
        st.session_state.approved.add(key)
    if key in st.session_state.approved:
        st.warning("NOT EXECUTED, review before running. Nothing has been applied to your database.",
                   icon=":material/gpp_maybe:")
        script = approve.build_script(top.get("kind"), sql, verified_badge(top)[0],
                                      approve.rewrite_for(r, top), top.get("rewrite_rule"))
        st.code(script, language="sql")            # has its own copy button
        st.download_button("Download as .sql", data=script, file_name="approved_fix.sql",
                           mime="text/plain", key=f"download_{key}")
        if r.get("explanation_unmasked"):          # real names: shown here and nowhere else
            st.markdown("**Explanation with your real names**")
            st.text(r["explanation_unmasked"])     # plain text: nothing in it can be read as markup


# ------------------------------------------------------------------
# The spoken debrief: a button; any problem falls back to the text above, quietly
# ------------------------------------------------------------------
def render_debrief(r, key):
    if st.button("Play debrief", key=f"debrief_{key}", icon=":material/volume_up:"):
        with st.spinner("Preparing the audio..."):
            st.session_state.debriefs[key] = voice.debrief_audio(r)
    path, note = st.session_state.debriefs.get(key, (None, None))
    if path:
        st.audio(path)
    elif note:
        st.caption(note)


# ------------------------------------------------------------------
# One analysis, shown inside an assistant chat message
# ------------------------------------------------------------------
def render_result(r, key=None, approvable=False):
    if r.get("match"):
        st.caption(match_line(r["match"]))

    if r.get("leak_check"):
        st.badge("0 raw values exposed", icon=":material/verified_user:", color="green",
                 help="Everything the AI saw or wrote passed the leak check.")
    else:
        st.badge("LEAK DETECTED", icon=":material/gpp_bad:", color="red")
    st.caption(MASKED_NOTE)

    st.markdown("**Masked query** (what the AI sees instead of your real SQL)")
    st.code(r.get("masked_query") or "(none)", language="sql")
    render_bottleneck(r.get("bottleneck"))
    with st.expander("Masked execution plan"):
        st.code(plan_tree_text(r.get("masked_plan"), highlight=bottleneck_node_id(r.get("bottleneck"))),
                language="text")
        st.json(r.get("masked_plan") or {}, expanded=False)
    render_recommendations(r.get("recommendations"), r.get("ai_inputs"))
    render_before_after(r)

    # Un-masked SQL is deliberately NOT shown here: only render_approve may show it, after a click.
    if r.get("explanation"):
        st.markdown("#### Explanation")
        st.markdown(md_safe(r["explanation"]))
    if approvable:
        render_debrief(r, key)
        render_approve(r, key)


# ------------------------------------------------------------------
# Chat
# ------------------------------------------------------------------
if "approved" not in st.session_state:
    st.session_state.approved = set()     # which analyses the admin has approved (only decides what to SHOW)
if "debriefs" not in st.session_state:
    st.session_state.debriefs = {}    # analysis number -> (audio path or None, note or None)
if "history" not in st.session_state:
    st.session_state.history = []     # [{"question", "result" or None, "error" or None}]

# Ask by voice. The recording goes to ElevenLabs as raw audio; only its transcript enters the pipeline.
voice_question = None
with st.sidebar:
    st.markdown("**Ask by voice**")
    clip = st.audio_input("Record a question", key="voice_clip", label_visibility="collapsed")
    st.caption("The recording is sent to ElevenLabs to be turned into text. Don't dictate customer data.")
    if clip is not None:
        with st.spinner("Turning your recording into text..."):
            voice_question, voice_note = voice.transcribe_once(clip.getvalue(), st.session_state)
        if voice_note:
            st.caption(voice_note)

question = st.chat_input("Ask about a slow report, e.g. 'Why is the weekly sales report slow?' "
                         "or paste a SELECT") or voice_question
if question:
    entry = {"question": question, "result": None, "error": None}
    with st.spinner("Analysing: masking, finding the bottleneck, simulating and timing fixes... "
                    "this can take up to a minute."):
        try:
            entry["result"] = (demo_mode.answer if demo_on else pipeline.analyze)(question)
        except ValueError as e:    # rejected SQL, or PrivacyError (a subclass of ValueError)
            entry["error"] = str(e)
    st.session_state.history.append(entry)

# Sample data is for looking at the layout only. It is OFF by default, and it can NEVER show
# while Demo mode is on (Demo mode is for the real demo: no made-up numbers allowed).
# The Demo mode switch is at the top of the page. While it is on, the sample switch is HIDDEN.
if not demo_on:
    with st.sidebar:
        st.toggle("Show sample result (every badge type)", key="show_sample", value=False,
                  help="Made-up data for checking the layout. Not a real analysis.")

if not st.session_state.history:
    st.info("Ask a question to begin, for example: *Why is the weekly sales report slow?*")

if st.session_state.get("show_sample") and not demo_on:      # the guard: checked at render time
    with st.sidebar:
        variant = st.radio("Sample variant", sample_data.VARIANTS, key="sample_variant")
    with st.chat_message("assistant"):
        st.warning("SAMPLE DATA: made-up numbers that show every badge type. Not a real analysis.",
                   icon=":material/science:")
        render_result(sample_data.demo_result(variant))

for position, entry in enumerate(st.session_state.history):
    with st.chat_message("user"):
        st.text(entry["question"])      # the admin's own words: shown only to the admin
    with st.chat_message("assistant"):
        if entry["error"]:
            st.error(entry["error"])
        else:
            if entry["result"].get("demo"):
                st.info(demo_mode.LABEL, icon=":material/movie:")
            render_result(entry["result"], key=position, approvable=True)
