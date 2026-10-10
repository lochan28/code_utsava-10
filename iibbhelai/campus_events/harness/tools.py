"""Tools the Gemma agent can call. Add a function with @tool and it is exposed.

Gemma reads each function's name, type hints and docstring to decide when to call it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import events  # noqa: E402

TOOLS = []


def tool(fn):
    """Register a function as an agent tool."""
    TOOLS.append(fn)
    return fn


def filter_events(city, radius_km=100, mode="any", event_type="any"):
    """Shared filter used by both the map tab and the agent.
    Returns (city_lat, city_lon, offline_matches_sorted_by_distance, online_matches)."""
    lat, lon = events.geocode(city)
    near, online = [], []
    for ev in events.load_events():
        if event_type != "any" and ev.get("type") != event_type:
            continue
        if ev["mode"] == "online":
            if mode in ("any", "online"):
                online.append(ev)
        elif mode in ("any", "offline") and lat is not None and ev.get("lat") is not None:
            d = events.haversine_km(lat, lon, ev["lat"], ev["lon"])
            if d <= radius_km:
                near.append(dict(ev, distance_km=round(d)))
    near.sort(key=lambda e: e["distance_km"])
    return lat, lon, near, online


@tool
def search_events(city: str, radius_km: int = 100, mode: str = "any", event_type: str = "any") -> list:
    """Find upcoming student events (hackathons, ideathons, webinars, workshops) near a city.

    Args:
        city: City name, e.g. "Raipur".
        radius_km: Only offline events within this many km of the city are returned.
        mode: "online", "offline" or "any".
        event_type: "hackathon", "ideathon", "webinar", "workshop" or "any".

    Returns a short list of events with id, title, city, distance_km, dates and mode.
    """
    lat, _, near, online = filter_events(city, radius_km, mode, event_type)
    if lat is None:
        return [{"error": f"Could not find the city '{city}'."}]
    keys = ("id", "title", "mode", "city", "distance_km", "start", "end", "register_url")
    return [{k: e.get(k) for k in keys} for e in (near + online)[:15]]


@tool
def get_event_details(event_id: str) -> dict:
    """Get the full record of one event (description, eligibility, requirements, links)
    using the id returned by search_events."""
    for ev in events.load_events():
        if ev["id"] == event_id:
            return ev
    return {"error": f"No event with id {event_id}"}


if __name__ == "__main__":
    res = search_events("Raipur", 200)
    print(len(res), "results within 200 km of Raipur:")
    for r in res:
        print(" -", r["title"], r["distance_km"], "km" if r["distance_km"] is not None else "(online)")
    print(get_event_details(res[0]["id"])["title"])
