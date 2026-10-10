"""CampusEvents Gradio app.  Run:  python app.py"""
import html
import json

import folium
import gradio as gr

import events
import gemma
from harness.tools import TOOLS, filter_events

# ---- Tunable values --------------------------------------------------------
DEFAULT_CITY = "Raipur"
DEFAULT_RADIUS = 300      # km shown when the app opens


# ---------------- Events Map tab ----------------
def popup_html(ev):
    e = {k: html.escape(str(ev.get(k) or "")) for k in ev}
    img = f'<img src="{e["poster_url"]}" width="200"><br>' if ev.get("poster_url") else ""
    return (f'{img}<b>{e["title"]}</b><br>{e["city"]} ({ev.get("distance_km", "?")} km)<br>'
            f'{e["start"]} → {e["end"]}<br><i>{e["description"]}</i><br>'
            f'<b>Eligibility:</b> {e["eligibility"]}<br><b>Requirements:</b> {e["requirements"]}<br>'
            f'<a href="{e["register_url"]}" target="_blank">Register</a>')


def build_map(city, radius, mode):
    lat, lon, near, online = filter_events(city, radius, mode)
    if lat is None:
        return f"<p style='color:red'>Could not find city '{html.escape(city)}'.</p>", []
    m = folium.Map(location=[lat, lon], zoom_start=6)
    folium.Marker([lat, lon], tooltip=city, icon=folium.Icon(color="red", icon="home")).add_to(m)
    folium.Circle([lat, lon], radius=radius * 1000, fill=False).add_to(m)
    for ev in near:
        folium.Marker([ev["lat"], ev["lon"]], tooltip=ev["title"],
                      popup=folium.Popup(popup_html(ev), max_width=260)).add_to(m)
    rows = [[e["title"], e["type"], e["start"], e["end"], e["register_url"]] for e in online]
    header = f"<p><b>{len(near)}</b> offline events within {radius} km, <b>{len(online)}</b> online.</p>"
    return header + m._repr_html_(), rows


def refresh(city, radius, mode):
    events.refresh_events()           # live fetch, falls back to events.json
    return build_map(city, radius, mode)


# ---------------- Poster upload ----------------
POSTER_PROMPT = """Read this event poster. Return ONLY a JSON object with these keys:
"title", "type" (one of hackathon, ideathon, webinar, workshop), "date" (YYYY-MM-DD if possible),
"mode" (online or offline), "city", "eligibility", "requirements", "register_link".
Use "" for anything not on the poster."""


def add_poster(image_path, city, radius, mode):
    if not image_path:
        return "⚠️ Please upload a poster image first.", *build_map(city, radius, mode)
    try:
        from PIL import Image
        reply = gemma.ask([Image.open(image_path), POSTER_PROMPT])
        data = gemma.parse_json(reply)
    except Exception as e:
        return f"❌ Could not read poster: {e}", *build_map(city, radius, mode)
    if not str(data.get("title", "")).strip():
        return f"❌ Gemma found no title on the poster. Raw output:\n{json.dumps(data, indent=2)}", \
            *build_map(city, radius, mode)
    ev_mode = "online" if str(data.get("mode", "")).lower() == "online" else "offline"
    ev = {
        "id": events.make_id("poster", data["title"] + str(data.get("date"))),
        "title": data["title"], "organiser": "", "type": data.get("type") or "hackathon",
        "mode": ev_mode, "city": data.get("city", "") if ev_mode == "offline" else "",
        "start": data.get("date", ""), "end": "", "description": "Added from a poster.",
        "eligibility": data.get("eligibility", ""), "requirements": data.get("requirements", ""),
        "register_url": data.get("register_link", ""), "poster_url": "", "source": "poster",
    }
    events.add_coords([ev])
    events.add_event(ev)
    warn = "" if ev["lat"] or ev_mode == "online" else " (⚠️ city not found, so no map marker)"
    return f"✅ Added: {ev['title']} — {ev['city'] or 'online'}{warn}\n{json.dumps(data, indent=2)}", \
        *build_map(city, radius, mode)


# ---------------- Agent with tools tab ----------------
def agent_chat(message, history):
    try:
        prompt = ("You help students find events. Use the tools to answer. "
                  "List event titles, cities, distances, dates and register links.\n\nUser: " + message)
        return gemma.ask([prompt], tools=TOOLS)   # google-genai runs the tool calls automatically
    except Exception as e:
        return f"Error: {e}"


with gr.Blocks(title="CampusEvents") as demo:
    gr.Markdown("# 🎓 CampusEvents — powered by Gemma 4")
    with gr.Tab("Events Map"):
        with gr.Row():
            city = gr.Textbox(DEFAULT_CITY, label="City")
            radius = gr.Slider(10, 2000, DEFAULT_RADIUS, step=10, label="Radius (km)")
            mode = gr.Dropdown(["any", "offline", "online"], value="any", label="Mode")
            btn = gr.Button("Refresh", variant="primary")
        map_html = gr.HTML()
        online_table = gr.Dataframe(headers=["Online event", "Type", "Start", "End", "Register"],
                                    label="Online events")
        gr.Markdown("### 📸 Add an event from a poster")
        with gr.Row():
            poster = gr.Image(type="filepath", label="Poster")
            status = gr.Textbox(label="Result", lines=8)
        add_btn = gr.Button("Read poster with Gemma & add")

        inputs = [city, radius, mode]
        btn.click(refresh, inputs, [map_html, online_table])
        for c in inputs:
            c.change(build_map, inputs, [map_html, online_table])
        add_btn.click(add_poster, [poster] + inputs, [status, map_html, online_table])
        demo.load(build_map, inputs, [map_html, online_table])
    with gr.Tab("Agent with tools"):
        gr.ChatInterface(agent_chat, examples=["hackathons within 200 km of Raipur"])

if __name__ == "__main__":
    demo.launch()
