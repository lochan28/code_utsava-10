---
name: campus-events
description: Find upcoming student hackathons, ideathons, webinars and workshops near a city, filtered by distance, and show their details.
---
# CampusEvents agent skill
Tools (in `harness/tools.py`):
- `search_events(city, radius_km=100, mode="any", event_type="any")` - nearby + online events.
- `get_event_details(event_id)` - full record of one event.

Use `search_events` first, then `get_event_details` for any event the user asks about.
Always give the register link.
