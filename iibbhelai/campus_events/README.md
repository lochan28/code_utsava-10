# 🎓 CampusEvents — built with Gemma 4

CampusEvents finds upcoming student events (hackathons, ideathons, webinars, workshops)
from **Unstop** and **Devpost**, filters them by distance from your city, and shows them on a map.
It is built with **Gemma 4 (`gemma-4-26b-a4b-it`)**, which:
- summarizes Unstop event pages into short description / eligibility / requirements,
- reads a photo of an **event poster** and adds the event to the map,
- powers a chat **agent with tools** ("hackathons within 200 km of Raipur").

## Run
```
pip install -r requirements.txt
copy .env.example .env        # then put your GEMINI_API_KEY in .env
python events.py              # fetch fresh events (optional)
python app.py                 # open the printed http://127.0.0.1:7860 link
```
If the internet or an API is down, the app uses the saved `events.json` (offline demo mode).
Events titled "Sample: ..." are placeholder data for that offline mode.

## Files
| File | What it does |
|---|---|
| `events.py` | fetch + normalize + geocode (cached in `geocache.json`) + save `events.json` |
| `gemma.py` | small Gemma 4 API wrapper |
| `harness/tools.py` | `@tool` functions for the agent: `search_events`, `get_event_details` |
| `app.py` | Gradio UI: Events Map tab (folium map, online table, poster upload) + Agent tab |
| `SKILL.md` | agent skill description |

## Screenshots
_TODO: add screenshots of the map, a popup, and a poster upload._
