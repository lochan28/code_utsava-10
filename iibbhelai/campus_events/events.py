"""Data layer: fetch events from Unstop + Devpost, normalize, geocode, save.

Run directly to refresh events.json:  python events.py
"""
import hashlib
import json
import math
import os
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))

# ---- Tunable values --------------------------------------------------------
GEMMA_SUMMARY_LIMIT = 15   # how many Unstop events get a Gemma summary (more = slower, more API calls)
HTTP_TIMEOUT = 15          # seconds to wait for each API before giving up

# ---- Fixed values ----------------------------------------------------------
EVENTS_FILE = os.path.join(HERE, "events.json")
GEOCACHE_FILE = os.path.join(HERE, "geocache.json")
SUMMARY_CACHE_FILE = os.path.join(HERE, "summary_cache.json")
UNSTOP_URL = ("https://unstop.com/api/public/opportunity/search-result"
              "?opportunity=hackathons&oppstatus=open&per_page=30")
DEVPOST_URL = "https://devpost.com/api/hackathons?status[]=upcoming"
HEADERS = {"User-Agent": "Mozilla/5.0 (campus-events student project)"}


# ---------- small helpers ----------
def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def make_id(source, key):
    return source + "-" + hashlib.md5(str(key).encode()).hexdigest()[:8]


def haversine_km(lat1, lon1, lat2, lon2):
    """Straight-line distance between two points on Earth, in km."""
    r = 6371
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ---------- geocoding (cached, 1 request/sec) ----------
_geocache = load_json(GEOCACHE_FILE, {})
_last_geocode = 0.0


def geocode(place):
    """City name -> (lat, lon) or (None, None). Cached in geocache.json."""
    if not place:
        return None, None
    key = place.strip().lower()
    if key in _geocache:
        return tuple(_geocache[key]) if _geocache[key] else (None, None)
    global _last_geocode
    try:
        from geopy.geocoders import Nominatim
        wait = 1.0 - (time.time() - _last_geocode)   # Nominatim rule: max 1 req/sec
        if wait > 0:
            time.sleep(wait)
        _last_geocode = time.time()
        loc = Nominatim(user_agent="campus-events").geocode(place, timeout=10)
        _geocache[key] = [loc.latitude, loc.longitude] if loc else None
    except Exception as e:
        print("geocode failed for", place, "->", e)
        return None, None          # don't cache network errors
    save_json(GEOCACHE_FILE, _geocache)
    return tuple(_geocache[key]) if _geocache[key] else (None, None)


# ---------- Unstop ----------
def fetch_unstop():
    r = requests.get(UNSTOP_URL, headers=HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    items = r.json()["data"]["data"]
    events = []
    for it in items:
        reg = it.get("regnRequirements") or {}
        if reg.get("reg_status") == "FINISHED":
            continue
        addr = it.get("address_with_country_logo") or {}
        mode = "offline" if (it.get("region") or "").lower() == "offline" else "online"
        city = ", ".join(x for x in [addr.get("city"), addr.get("state")] if x)
        team = f"Team size {reg.get('min_team_size', '?')}-{reg.get('max_team_size', '?')}"
        events.append({
            "id": make_id("unstop", it.get("id") or it.get("title")),
            "title": it.get("title", ""),
            "organiser": (it.get("organisation") or {}).get("name", ""),
            "type": "hackathon",
            "mode": mode,
            "city": city if mode == "offline" else "",
            "start": reg.get("start_regn_dt", ""),
            "end": reg.get("end_regn_dt", ""),
            "description": "",
            "eligibility": "",
            "requirements": team,
            "register_url": "https://unstop.com/" + (it.get("seo_url") or "").lstrip("/"),
            "poster_url": it.get("logoUrl2") or "",
            "source": "unstop",
            "details_html": it.get("details") or "",
        })
    return events


# ---------- Devpost ----------
def fetch_devpost():
    r = requests.get(DEVPOST_URL, headers=HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    events = []
    for it in r.json().get("hackathons", []):
        loc = (it.get("displayed_location") or {}).get("location", "")
        mode = "online" if loc.lower() in ("online", "") else "offline"
        thumb = it.get("thumbnail_url") or ""
        if thumb.startswith("//"):
            thumb = "https:" + thumb
        themes = ", ".join(t.get("name", "") for t in it.get("themes", []))
        events.append({
            "id": make_id("devpost", it.get("id") or it.get("url")),
            "title": it.get("title", ""),
            "organiser": it.get("organization_name", ""),
            "type": "hackathon",
            "mode": mode,
            "city": loc if mode == "offline" else "",
            "start": it.get("submission_period_dates", ""),
            "end": "",
            "description": f"Themes: {themes}" if themes else "",
            "eligibility": "",
            "requirements": "",
            "register_url": it.get("url", ""),
            "poster_url": thumb,
            "source": "devpost",
        })
    return events


# ---------- Gemma summaries of Unstop HTML (cached) ----------
def summarize_details(events):
    """Ask Gemma to turn the first N Unstop 'details' HTML into short fields."""
    cache = load_json(SUMMARY_CACHE_FILE, {})
    done = 0
    for ev in events:
        html = ev.pop("details_html", "")
        if not html or done >= GEMMA_SUMMARY_LIMIT:
            continue
        done += 1
        if ev["id"] not in cache:
            try:
                from bs4 import BeautifulSoup
                import gemma
                text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)[:4000]
                prompt = ("From this event description, return ONLY JSON with keys "
                          '"description" (1-2 sentences), "eligibility" (short), '
                          '"requirements" (short).\n\n' + text)
                cache[ev["id"]] = gemma.parse_json(gemma.ask([prompt]))
                save_json(SUMMARY_CACHE_FILE, cache)
            except Exception as e:
                print("Gemma summary skipped for", ev["title"], "->", e)
                continue
        for k in ("description", "eligibility", "requirements"):
            if cache[ev["id"]].get(k):
                ev[k] = str(cache[ev["id"]][k])
    for ev in events:            # drop leftover raw HTML
        ev.pop("details_html", None)
    return events


# ---------- main entry points ----------
def add_coords(events):
    for ev in events:
        if ev.get("lat") is None:
            ev["lat"], ev["lon"] = geocode(ev["city"]) if ev["mode"] == "offline" else (None, None)
    return events


def refresh_events():
    """Fetch live data; on any failure, fall back to the saved events.json."""
    try:
        events = fetch_unstop() + fetch_devpost()
    except Exception as e:
        print("Live fetch failed, using saved events.json ->", e)
        return load_events()
    events = add_coords(summarize_details(events))
    # keep events added from posters across refreshes
    events += [e for e in load_events() if e.get("source") == "poster"]
    save_json(EVENTS_FILE, events)
    return events


def load_events():
    return load_json(EVENTS_FILE, [])


def add_event(ev):
    """Append one event (e.g. from a poster) to events.json and return it."""
    events = load_events()
    events.append(ev)
    save_json(EVENTS_FILE, events)
    return ev


if __name__ == "__main__":
    evs = refresh_events()
    print(len(evs), "events,", sum(1 for e in evs if e.get("lat")), "with map coordinates")
    for e in evs[:5]:
        print("-", e["title"], "|", e["mode"], "|", e["city"] or "online")
