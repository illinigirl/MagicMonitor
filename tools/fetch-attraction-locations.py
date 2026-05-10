"""
Snapshot lat/lon for every WDW attraction we track in the analytics layer.

Themeparks.wiki returns location data on the entity GET endpoint
(`/entity/<id>` → `location: {latitude, longitude, ...}`). Locations
are static — Space Mountain doesn't move — so we fetch once and ship
a JSON snapshot in the repo. Re-run this script when new attractions
open or when entity IDs change (rare but happens during refurbs).

Output: `web/src/data/attraction-locations.json` shaped as:
    {
      "ride_id": {"name": "...", "park_key": "...", "lat": ..., "lon": ...},
      ...
    }

Used by the MCP `get_planning_context` tool to give Claude proximity
context for trip-planning ("Pirates and Haunted Mansion are both ~200m
from the hub"), without ever needing user GPS.
"""

import json
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_PATH = ROOT / "web" / "src" / "data" / "analytics-snapshot.json"
OUTPUT_PATH = ROOT / "web" / "src" / "data" / "attraction-locations.json"

BASE_URL = "https://api.themeparks.wiki/v1"
# Be polite to themeparks.wiki — they're a free service. ~10/s max
# is fine for an ~88-ride run; takes ~10 sec end-to-end.
SLEEP_BETWEEN = 0.1


def main() -> int:
    if not SNAPSHOT_PATH.exists():
        print(f"ERROR: {SNAPSHOT_PATH} not found — run aggregate-analytics.py first.")
        return 1

    snapshot = json.loads(SNAPSHOT_PATH.read_text())
    rides = snapshot.get("rides", [])
    print(f"Found {len(rides)} rides in analytics snapshot.")

    out: dict[str, dict] = {}
    missing: list[str] = []

    for i, ride in enumerate(rides, start=1):
        rid = ride["ride_id"]
        name = ride["ride_name"]
        park_key = ride.get("park_key")

        try:
            resp = requests.get(f"{BASE_URL}/entity/{rid}", timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  [{i}/{len(rides)}] {name}: fetch failed — {e}")
            missing.append(name)
            time.sleep(SLEEP_BETWEEN)
            continue

        loc = data.get("location") or {}
        lat = loc.get("latitude")
        lon = loc.get("longitude")
        if lat is None or lon is None:
            # Some entities don't have coordinates (e.g. rides removed
            # from the live feed but still in our historical snapshot).
            # Capture the omission rather than silently skipping.
            print(f"  [{i}/{len(rides)}] {name}: no location in entity response")
            missing.append(name)
            time.sleep(SLEEP_BETWEEN)
            continue

        out[rid] = {
            "name": name,
            "park_key": park_key,
            "lat": lat,
            "lon": lon,
        }
        if i % 20 == 0:
            print(f"  [{i}/{len(rides)}] {name}: {lat:.4f}, {lon:.4f}")
        time.sleep(SLEEP_BETWEEN)

    OUTPUT_PATH.write_text(json.dumps(out, indent=2))
    print()
    print(f"Wrote {len(out)} ride locations to {OUTPUT_PATH}")
    if missing:
        print(f"Missing locations for {len(missing)} rides:")
        for n in missing:
            print(f"  - {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
