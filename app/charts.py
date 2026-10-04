"""Before/after bar charts (Altair is already installed: Streamlit depends on it).

ONE chart shows ONE unit. The cost chart and the time chart are separate charts with
separate axes, so planner cost units and milliseconds can never share a scale.

Self-test:  .venv/Scripts/python -m app.charts
"""
import altair as alt

# ============================================================
# TUNABLE SETTINGS (looks only)
# ============================================================
BEFORE_COLOR = "#8a8f98"    # gray: the "before" bar
# The "after" bar takes the colour of the target marker next to it:
STATUS_COLORS = {"met": "#2e9e5b",       # green: more than the 50% target
                 "below": "#e0a030",     # amber: better, but under the target
                 "none": "#8a8f98",      # gray:  no change
                 "worse": "#d64545",     # red:   it got worse
                 "unknown": "#8a8f98"}
CHART_HEIGHT = 220          # pixels
GNN_COLOR = "#8a8f98"       # gray: the GNN's other highly-rated steps
GNN_CHOSEN_COLOR = "#7a5af8"  # violet: the step the pipeline picked as the bottleneck
GNN_ROW_HEIGHT = 34         # pixels per bar in the GNN top-3 chart


def before_after_chart(before, after, y_title, status="unknown", label_format=",.0f"):
    """Two bars, 'before' and 'after'. `y_title` names the unit and is the axis title.
    `status` (met / below / none / worse / unknown) colours the 'after' bar.
    The numbers are put INSIDE the chart spec, so tests can read exactly what is plotted."""
    data = alt.Data(values=[{"when": "before", "value": float(before)},
                            {"when": "after", "value": float(after)}])
    base = alt.Chart(data).encode(
        x=alt.X("when:N", sort=["before", "after"], title=None, axis=alt.Axis(labelAngle=0)),
        y=alt.Y("value:Q", title=y_title, scale=alt.Scale(domainMin=0)))
    bars = base.mark_bar().encode(color=alt.Color(
        "when:N", legend=None,
        scale=alt.Scale(domain=["before", "after"],
                        range=[BEFORE_COLOR, STATUS_COLORS.get(status, STATUS_COLORS["unknown"])])))
    labels = base.mark_text(dy=-6).encode(text=alt.Text("value:Q", format=label_format))
    return (bars + labels).properties(height=CHART_HEIGHT)


def gnn_top3_chart(rows, chosen_id=None):
    """Small horizontal bars: the GNN's highest-rated plan steps. rows = [{"label", "score" (0-1), "node_id"}].
    The step that was picked as the bottleneck is violet. Scores are plotted as percent."""
    data = alt.Data(values=[{"step": r["label"], "score": round(float(r["score"]) * 100, 1),
                             "chosen": chosen_id is not None and r.get("node_id") == chosen_id} for r in rows])
    base = alt.Chart(data).encode(y=alt.Y("step:N", sort=None, title=None))
    bars = base.mark_bar().encode(
        x=alt.X("score:Q", title="share of the GNN's score (%)", scale=alt.Scale(domain=[0, 100])),
        color=alt.Color("chosen:N", legend=None,
                        scale=alt.Scale(domain=[True, False], range=[GNN_CHOSEN_COLOR, GNN_COLOR])))
    labels = base.mark_text(align="left", dx=4).encode(x=alt.X("score:Q"), text=alt.Text("score:Q", format=".0f"))
    return (bars + labels).properties(height=GNN_ROW_HEIGHT * len(rows) + 30)


if __name__ == "__main__":
    spec = before_after_chart(19437, 812, "planner cost units (estimate)").to_dict()
    print("y axis title:", spec["layer"][0]["encoding"]["y"]["title"])
    print("plotted values:", [d["value"] for d in spec["data"]["values"]])
