"""Shared tool implementations + helpers for the Magic Monitor MCP servers.

Extracted verbatim from server.py (the canonical stdio source) so the
stdio (server.py) and HTTP (server_http.py) servers stop duplicating this
logic. Both import these names; existing call sites are unchanged.

Environment-specific accessors (the DDB table handle, the analytics
snapshot, and the attraction-locations map) differ per host — stdio reads
local files + an SSO profile; HTTP fetches S3 + uses the Lambda role — so
each host injects them via configure() at import. The moved helpers call
_ddb_table() / _snapshot() / _locations() by name, which delegate here.
"""

from __future__ import annotations

import json
import os
import re as _re
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

# ─── Host-injected resource accessors ──────────────────────────────
# stdio + HTTP differ in how they reach DDB / snapshot / locations.
# Each host calls configure() once at import, after defining its own.
_RESOURCE_HOOKS: dict[str, Any] = {}


def configure(*, ddb_table, snapshot, locations) -> None:
    """Install the host server's environment-specific accessors."""
    _RESOURCE_HOOKS["ddb_table"] = ddb_table
    _RESOURCE_HOOKS["snapshot"] = snapshot
    _RESOURCE_HOOKS["locations"] = locations


def _ddb_table():
    return _RESOURCE_HOOKS["ddb_table"]()


def _snapshot():
    return _RESOURCE_HOOKS["snapshot"]()


def _locations():
    return _RESOURCE_HOOKS["locations"]()



# ─── Calibration scoring constants (used by _compute_calibration_summary) ─
# Aggression rating numeric scale. Negative = plan didn't fit (too
# aggressive), positive = plan ran short (not aggressive enough).
# Average across plans gives a -1..+1 calibration knob.
_AGGRESSION_SCORES = {
    "too_aggressive": -1.0,
    "about_right": 0.0,
    "not_aggressive_enough": 1.0,
}

# Canonical rating enums — the single source of truth for both write-side
# validation (record_plan_outcome) and read-side aggregation. An off-enum
# value stored verbatim is silently dropped by the aggregator, so writes
# must reject anything not in these sets.
_AGGRESSION_VALUES = frozenset(_AGGRESSION_SCORES)
_TIMING_VALUES = frozenset({"ran_over", "on_time", "extra_time"})

# Sample-size thresholds for per-ride / per-show bias confidence.
# Below 3 samples a derived average is essentially noise; treat as
# directional only or ignore entirely.
_BIAS_CONFIDENCE_HIGH = 5

_BIAS_CONFIDENCE_MEDIUM = 3

# Magnitude threshold (minutes) below which a wait-time delta is
# treated as "predicted accurately" rather than biased. Calibration
# noise on a 2-min poll cadence + user recall imprecision means
# ±5 min is functionally indistinguishable from zero.
_BIAS_NEUTRAL_MINUTES = 5


# ─── Extracted constants + helpers + get_planning_context ──────────

# Park-day boundary in Eastern time. Mirrors tools/aggregate-analytics.py
# so the live downtime tool agrees with the historical heatmap and
# down-cluster aggregations: a 1am Friday breakdown counts as
# "Thursday's park day," not Friday's. Disney World's parks span ET
# year-round; no per-park override needed.
_PARK_DAY_BOUNDARY_HOUR = 4


_EASTERN = ZoneInfo("America/New_York")


# HIST# row TTL in the poller — must track the poller's
# HISTORY_RETENTION_DAYS (set to 1825 / 5yr in disney-stack.ts for the
# analytics aggregator; see DATA-GROWTH-MODEL.md). Tools that look further
# back than this return empty, so we reject it explicitly. Was stalely 90
# here while the poller kept 1825, needlessly capping get_ride_downtime_today
# (reconciled 2026-07-01).
_HIST_RETENTION_DAYS = 1825


# Walt Disney World coordinates (entrance plaza). Used to fetch a
# single weather forecast that's representative of all four parks —
# they're all within ~6km of each other so weather doesn't vary
# meaningfully between them at the planning resolution we care about.
_WDW_LAT = 28.3852


_WDW_LON = -81.5639


# SQLite-style day-of-week (Sun=0..Sat=6) — matches the heatmap data.
_DOW_NAMES = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]


_DOW_INDEX = {name: i for i, name in enumerate(_DOW_NAMES)}


_PARK_KEYS = {"magic_kingdom", "epcot", "hollywood_studios", "animal_kingdom"}


def _convert_decimals(obj: Any) -> Any:
    """Recursively convert boto3 Decimals to int/float for JSON-friendly output.

    DynamoDB returns numbers as `decimal.Decimal` to preserve precision.
    MCP tool returns get serialized to JSON for the client, and JSON
    has no Decimal type — without this conversion the MCP runtime fails
    or surfaces opaque type errors. We convert back to int when the value
    is whole (matches our write shape) and float otherwise.
    """
    from decimal import Decimal
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    if isinstance(obj, dict):
        return {k: _convert_decimals(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_convert_decimals(v) for v in obj]
    if isinstance(obj, set):
        # DynamoDB String Sets (e.g. alert_subscribers) come back as Python
        # sets, which aren't JSON-serializable — a raw row in a tool return
        # would crash the MCP runtime. Sorted list keeps output stable.
        return sorted(_convert_decimals(v) for v in obj)
    return obj


def _floats_to_decimals(obj: Any) -> Any:
    """Recursively convert Python floats to Decimal for DDB writes.

    boto3's resource interface refuses native floats with
    "Float types are not supported. Use Decimal types instead."
    The reverse of _convert_decimals, used on the write side. NaN /
    inf would raise inside Decimal(); we don't expect those in plan
    feedback data so we let the exception propagate as a clear
    error rather than silently coercing.
    """
    from decimal import Decimal
    if isinstance(obj, float):
        # str() round-trips the value cleanly; Decimal(float) would
        # introduce binary-float artifacts like Decimal('1.1200000...').
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _floats_to_decimals(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_floats_to_decimals(v) for v in obj]
    return obj


def _normalize_park(park: str) -> str:
    """Accept 'Magic Kingdom' / 'magic_kingdom' / 'MK' / 'mk', return canonical key.

    Raises ValueError on unknown input so the MCP client surfaces a
    clear message instead of an empty result.
    """
    p = park.strip().lower().replace(" ", "_").replace("-", "_")
    if p in _PARK_KEYS:
        return p
    aliases = {
        "mk": "magic_kingdom",
        "magickingdom": "magic_kingdom",
        "ep": "epcot",
        "hs": "hollywood_studios",
        "dhs": "hollywood_studios",
        "hollywoodstudios": "hollywood_studios",
        "ak": "animal_kingdom",
        "animalkingdom": "animal_kingdom",
    }
    if p.replace("_", "") in aliases:
        return aliases[p.replace("_", "")]
    raise ValueError(
        f"Unknown park '{park}'. Use one of: "
        f"{', '.join(sorted(_PARK_KEYS))} (or aliases like MK, EP, HS, AK)."
    )


def _find_ride(ride_name: str) -> dict[str, Any]:
    """Resolve a free-text ride name to a snapshot record (substring match).

    Returns the first ride whose name contains the query (case-
    insensitive). Raises ValueError if nothing matches — better to
    fail loudly than to silently match a wrong ride.
    """
    q = ride_name.strip().lower()
    if not q:
        raise ValueError("ride_name cannot be empty")
    rides = _snapshot()["rides"]
    for r in rides:
        if q in r["ride_name"].lower():
            return r
    raise ValueError(
        f"No ride matching '{ride_name}'. "
        f"Use find_rides_matching to list rides by criteria."
    )


def _aws_error_payload(e: Exception) -> dict[str, Any] | None:
    """If `e` is a recognizable AWS-auth failure, return a friendly
    error dict for the tool to surface. Otherwise return None so the
    caller can re-raise or wrap with its own context.

    Centralized so all live-DDB tools surface the same `aws sso login`
    hint instead of opaque tracebacks. Personal-dev SSO expiry is by
    far the most common failure mode here.
    """
    msg = str(e)
    # Token expiry (SSO refresh case) — friendly path:
    if "Token has expired" in msg or "ExpiredToken" in msg:
        return {
            "error": "AWS credentials expired",
            "error_hint": "Run `aws sso login --profile watchtower` and retry.",
        }
    # Invalid creds (different from expiry) — usually means boto3 picked
    # up the wrong profile entirely, e.g. a stale [default] in
    # ~/.aws/credentials when AWS_PROFILE wasn't set in the MCP env.
    # DynamoDB surfaces this as UnrecognizedClientException; STS as
    # InvalidClientTokenId; same root cause.
    if "InvalidClientTokenId" in msg or "UnrecognizedClientException" in msg:
        return {
            "error": "AWS credentials not recognized",
            "error_hint": (
                "boto3 is hitting AWS with credentials that aren't valid "
                "for this account. Most likely the MCP env doesn't set "
                "AWS_PROFILE=watchtower and boto3 is falling back to a "
                "stale [default] profile. Add "
                '`\"env\": {\"AWS_PROFILE\": \"watchtower\"}` '
                "to the magic-monitor block in claude_desktop_config.json "
                "and restart Claude Desktop."
            ),
        }
    return None


def _park_day_window_utc(days_back: int) -> tuple[datetime, datetime, str]:
    """Return [start, end_inclusive] UTC datetimes covering one park-day.

    Park-days run 4am ET to 4am ET (next calendar day). A 1am Friday
    breakdown belongs to Thursday's park-day — matches the historical
    analytics convention in tools/aggregate-analytics.py exactly, so
    "today's down count" stays consistent with the live heatmap that
    the model can also pull via get_park_heatmap.

    `end_inclusive` is shifted back by 1 microsecond from the next
    park-day's start so DynamoDB BETWEEN over HIST# SKs is a clean
    half-open interval — a transition recorded at exactly the next
    park-day's 4am-ET-rendered-as-UTC won't double-count.
    """
    now_et = datetime.now(_EASTERN)
    # Anchor on the CURRENT park-day, not the calendar day. Before the 4am
    # boundary we're still inside the previous calendar day's park-day (a
    # 2am poll belongs to yesterday's 4am→4am window). Without this shift,
    # days_back=0 between midnight and 4am ET builds a window that is
    # entirely in the future, the HIST# BETWEEN query returns nothing, and
    # get_ride_downtime_today reports "0 down today" after an evening of
    # breakdowns. Matches tools/aggregate-analytics.py _park_day_iso.
    if now_et.hour < _PARK_DAY_BOUNDARY_HOUR:
        now_et -= timedelta(days=1)
    target_date = (now_et - timedelta(days=days_back)).date()
    start_et = datetime.combine(
        target_date, time(_PARK_DAY_BOUNDARY_HOUR, 0), tzinfo=_EASTERN
    )
    end_et = start_et + timedelta(days=1) - timedelta(microseconds=1)
    return (
        start_et.astimezone(timezone.utc),
        end_et.astimezone(timezone.utc),
        target_date.isoformat(),
    )


_PARK_KEY_SK_INDEX = "park_key-SK-index"


def _park_state_rows_via_gsi(table, park_key: str) -> list[dict]:
    """All STATE rows for one park, via the park_key-SK-index GSI — a
    partition Query (`park_key=:p AND SK="STATE"`), NOT a full-table Scan.

    O(rides-in-park), independent of table size. The live read tools used
    Scan+FilterExpression, which pages the entire multi-GB table to return
    ~30 STATE rows (~20s and climbing toward the 30s API Gateway cap by
    mid-2026). Same index + fix the web getParkRides path took on
    2026-05-25.
    """
    items: list[dict] = []
    kwargs = {
        "IndexName": _PARK_KEY_SK_INDEX,
        "KeyConditionExpression": "park_key = :p AND SK = :sk",
        "ExpressionAttributeValues": {":p": park_key, ":sk": "STATE"},
    }
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return items
        kwargs["ExclusiveStartKey"] = lek


def _all_park_state_rows_via_gsi(table) -> list[dict]:
    """Every ride's STATE row across the parks (~150 total) via one small
    GSI partition Query per park — replaces a full-table Scan for the
    by-name resolvers (park unknown). sorted() so the "first name match
    wins" resolution is deterministic across runs (PARK_KEYS is a set)."""
    items: list[dict] = []
    for pk in sorted(_PARK_KEYS):
        items.extend(_park_state_rows_via_gsi(table, pk))
    return items


def _fetch_park_currently_down(table, park_key: str) -> list[dict] | None:
    """Return every DOWN ride in the park with its down-since timing.

    Used by the planner to detect weather-vs-mechanical patterns: a
    single outdoor ride DOWN during a storm might be coincidence, but
    multiple outdoor rides simultaneously DOWN within a similar window
    is essentially proof of weather causation. Claude classifies each
    ride as outdoor/indoor from general knowledge; we just surface
    the raw "what's broken right now" picture.

    Scope is the whole park, not just the user's wishlist, so the
    planner sees the broader pattern even when the wishlist itself
    is a small subset. Returns None on DDB failure (planner degrades
    gracefully rather than blocking the whole call).
    """
    if table is None:
        return None
    try:
        rows = _park_state_rows_via_gsi(table, park_key)
    except Exception:
        return None

    # The GSI Query returns this park's STATE rows (~30); filter to DOWN
    # in Python rather than a FilterExpression — the set is tiny.
    items = _convert_decimals([r for r in rows if r.get("status") == "DOWN"])
    out: list[dict] = []
    for item in items:
        rid = item.get("ride_id")
        entry: dict[str, Any] = {
            "ride_name": item.get("name"),
            "ride_id": rid,
            "last_seen": item.get("last_seen"),
        }
        # DOWN_SINCE gives "how long" — critical for the concurrent-
        # within-X-min detection. One extra GetItem per DOWN ride;
        # typical DOWN count is <5 so this is cheap.
        try:
            ds_resp = table.get_item(
                Key={"PK": f"RIDE#{rid}", "SK": "DOWN_SINCE"}
            )
            ds = ds_resp.get("Item")
            if ds and ds.get("down_since"):
                entry["down_since"] = ds["down_since"]
                try:
                    down_dt = datetime.fromisoformat(ds["down_since"])
                    elapsed = datetime.now(timezone.utc) - down_dt
                    entry["down_duration_mins"] = round(
                        elapsed.total_seconds() / 60, 1
                    )
                except ValueError:
                    pass
        except Exception:
            pass
        out.append(entry)
    return out


def _fetch_park_hours_today(park_key: str) -> dict[str, Any] | None:
    """Fetch today's open/close window for a park from themeparks.wiki.

    Mirrors the poller's fetch_park_hours logic so the planning tool
    sees the same hours the alert filter uses. Returns a dict with
    open + close ISO timestamps (with timezone), or None on failure
    (the planner can degrade gracefully if hours aren't available).
    """
    park_ids = {
        "magic_kingdom":     "75ea578a-adc8-4116-a54d-dccb60765ef9",
        "epcot":             "47f90d2c-e191-4239-a466-5892ef59a88b",
        "hollywood_studios": "288747d1-8b4f-4a64-867e-ea7c9b27bad8",
        "animal_kingdom":    "1c84a229-8862-4648-9c71-378ddd2c7693",
    }
    park_id = park_ids.get(park_key)
    if not park_id:
        return None

    import requests
    today_local = datetime.now(_EASTERN).date().isoformat()
    url = f"https://api.themeparks.wiki/v1/entity/{park_id}/schedule"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return None

    open_dt: datetime | None = None
    close_dt: datetime | None = None
    for entry in data.get("schedule", []):
        if entry.get("date") != today_local:
            continue
        if entry.get("type") not in ("OPERATING", "EXTRA_HOURS"):
            continue
        try:
            o = datetime.fromisoformat(entry["openingTime"])
            c = datetime.fromisoformat(entry["closingTime"])
        except (KeyError, ValueError):
            continue
        if open_dt is None or o < open_dt:
            open_dt = o
        if close_dt is None or c > close_dt:
            close_dt = c

    if open_dt is None or close_dt is None:
        return None
    now_et = datetime.now(_EASTERN)
    minutes_to_close = round((close_dt - now_et).total_seconds() / 60)
    return {
        "open": open_dt.isoformat(),
        "close": close_dt.isoformat(),
        "minutes_until_close": minutes_to_close,
    }


def _fetch_weather_forecast() -> dict[str, Any] | None:
    """Fetch current conditions + 6-hour forecast from Open-Meteo.

    No API key, no signup, no rate-limit headaches. Single request
    returns current state + hourly forecast for our fixed lat/lon.
    Returns None on failure — the planner falls back to "weather
    unknown, plan accordingly" rather than blocking the whole tool.

    Schema returned to the planner: a flat dict with the few fields
    Claude actually uses for ride selection (precipitation chance,
    weather code, temperature). The full Open-Meteo response is
    much richer; we trim it here so the model doesn't waste tokens
    parsing fields it won't use.
    """
    import requests
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={_WDW_LAT}&longitude={_WDW_LON}"
        "&current=temperature_2m,precipitation,weather_code"
        "&hourly=temperature_2m,precipitation_probability,weather_code"
        "&temperature_unit=fahrenheit&precipitation_unit=inch"
        "&forecast_hours=6&timezone=America/New_York"
    )
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return None

    current = data.get("current") or {}
    hourly = data.get("hourly") or {}
    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])
    precip = hourly.get("precipitation_probability", [])
    codes = hourly.get("weather_code", [])
    next_hours = [
        {
            "time": t,
            "temp_f": temps[i] if i < len(temps) else None,
            "precipitation_chance_pct": precip[i] if i < len(precip) else None,
            "weather_code": codes[i] if i < len(codes) else None,
        }
        for i, t in enumerate(times)
    ]
    return {
        "current": {
            "temp_f": current.get("temperature_2m"),
            "precipitation_inches": current.get("precipitation"),
            "weather_code": current.get("weather_code"),
        },
        "next_6h": next_hours,
        "weather_code_legend": (
            "Open-Meteo WMO codes: 0=clear, 1-3=mainly clear/partly cloudy/"
            "overcast, 45/48=fog, 51-67=drizzle/rain (light to heavy), "
            "71-77=snow, 80-82=rain showers, 95=thunderstorm, "
            "96/99=thunderstorm with hail. >=80 means rides may close "
            "(Disney pauses outdoor rides for lightning); 51-67 means "
            "ride continues but the queue/seats get wet."
        ),
    }


_SHOW_PARK_IDS = {
    "magic_kingdom":     "75ea578a-adc8-4116-a54d-dccb60765ef9",
    "epcot":             "47f90d2c-e191-4239-a466-5892ef59a88b",
    "hollywood_studios": "288747d1-8b4f-4a64-867e-ea7c9b27bad8",
    "animal_kingdom":    "1c84a229-8862-4648-9c71-378ddd2c7693",
}


# Six-bucket category model mirrors web/src/lib/showtimes.ts. The
# planner uses the headliner subset (spectacular/parade/stage) as
# soft time constraints and treats music/atmosphere/character_meet
# as ambient (mention only if the user asks).
_SHOW_HEADLINER_CATEGORIES = ("spectacular", "parade", "stage")


_NAMED_ACT_OVERRIDES = [
    # Stage shows whose API names lack "live on stage" / "musical"
    (_re.compile(r"mickey's magical friendship faire"),                "stage"),
    (_re.compile(r"celebraci[oó]n encanto"),                           "stage"),
    (_re.compile(r"feathered friends in flight"),                      "stage"),
    # The "Spectacular!" in the title trips SPECTACULAR_RX before the
    # stage regex's "epic stunt" can match — but Indy is a midday
    # stunt show that runs ~5x/day, not a nighttime fireworks finale.
    # Override forces the right bucket so the planner doesn't apply
    # post-finale-crowd reasoning to a 4pm performance.
    (_re.compile(r"indiana jones.*epic stunt"),                        "stage"),
    # Christmas-season stage show at EPCOT (Festival of the Holidays).
    # 50-piece orchestra + celebrity narrator reading the Christmas
    # story — a real planning anchor in November/December.
    (_re.compile(r"candlelight processional"),                         "stage"),
    # Live-music sets at World Showcase / AK pavilions where the API
    # name describes the venue, not the act
    (_re.compile(r"viva mexico"),                                      "music"),
    (_re.compile(r"entertainment at canada mill stage"),               "music"),
    (_re.compile(r"entertainment at germany gazebo"),                  "music"),
    # EPCOT festival concert series. Garden Rocks (Flower & Garden,
    # Mar-Jul) and Eat to the Beat (Food & Wine, Aug-Nov) usually
    # contain the word "Concert" in the API name and would match the
    # music keyword regex on their own — these overrides are defense
    # in depth for years where the branding drops "Concert" from the
    # title. Disney on Broadway (also F&W) sometimes appears without
    # any music keyword at all, so it actually needs the override.
    (_re.compile(r"eat to the beat"),                                  "music"),
    (_re.compile(r"garden rocks"),                                     "music"),
    (_re.compile(r"disney on broadway"),                               "music"),
    # Up character moment, not an atmosphere band
    (_re.compile(r"adventures with kevin"),                            "character_meet"),
]


_SPECTACULAR_RX = _re.compile(
    r"\b(fireworks|spectacular|enchantment|happily ever after|luminous|"
    r"fantasmic|wonderful world of animation|disney movie magic|"
    r"tree of life awakenings|disney starlight|symphony of us)\b"
)


_PARADE_RX = _re.compile(r"\b(parade|cavalcade)\b")


_STAGE_RX = _re.compile(
    r"\b(live on stage|sing-?along|musical adventure|musical celebration|"
    r"festival of the lion king|finding nemo|epic stunt|frozen sing|"
    r"first order searches|disney villains|big blue|beauty and the beast)\b"
)


_MUSIC_RX = _re.compile(
    r"\b(band|philharmonic|drum|drummers|drummer|pianist|musician|concert|"
    r"mariachi|marimba|voices of|jammitors|dapper dans|beats and strings|"
    r"kora tinga|rhythmics|swingin|matsuriza)\b"
)


def _classify_show(name: str) -> str:
    """Bucket a SHOW entity by name into one of six categories.

    Mirrors web/src/lib/showtimes.ts `classifyShow`. Anything unmatched
    falls through to "atmosphere" — wrong-but-safe (still surfaced,
    never invisible).
    """
    n = name.lower()
    for pattern, category in _NAMED_ACT_OVERRIDES:
        if pattern.search(n):
            return category
    if n.startswith("meet "):
        return "character_meet"
    if _SPECTACULAR_RX.search(n):
        return "spectacular"
    if _PARADE_RX.search(n):
        return "parade"
    if _STAGE_RX.search(n):
        return "stage"
    if _MUSIC_RX.search(n):
        return "music"
    return "atmosphere"


def _fetch_park_showtimes(park_key: str) -> list[dict[str, Any]] | None:
    """Fetch today's SHOW entities for a park from themeparks.wiki.

    Mirrors web/src/lib/showtimes-server.ts `getParkShowtimes`. Filters
    to entities of type SHOW with at least one performance starting
    today (in park-local time, America/New_York), classifies each by
    name, and returns a flat list sorted by next-upcoming start time.

    Returns None on fetch failure — callers should degrade gracefully
    (showtimes are nice-to-have for the planner, not load-bearing).

    No caching: the Open-Meteo / themeparks.wiki helpers used by
    get_planning_context are also uncached. Showtime payloads change
    rarely intra-day (the API publishes the day's schedule once and
    the rare "Fantasmic cancelled tonight" status doesn't show up here
    anyway — that goes via the live status of the SHOW entity which
    we're not surfacing yet).
    """
    park_id = _SHOW_PARK_IDS.get(park_key)
    if not park_id:
        return None

    import requests
    url = f"https://api.themeparks.wiki/v1/entity/{park_id}/live"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return None

    today_local = datetime.now(_EASTERN).date().isoformat()
    shows: list[dict[str, Any]] = []

    for item in data.get("liveData", []) or []:
        if item.get("entityType") != "SHOW":
            continue
        # Filter the multi-day showtimes array to today's performances
        # only. Slice the date prefix off the ISO string instead of
        # parsing — themeparks.wiki always returns ISO with TZ offset
        # in park-local time so the YYYY-MM-DD prefix IS the park day.
        todays = []
        for s in item.get("showtimes", []) or []:
            start = s.get("startTime")
            end = s.get("endTime")
            if not start or not end:
                continue
            if start[:10] != today_local:
                continue
            todays.append({"start": start, "end": end})
        if not todays:
            # Skip shows the API knows about but isn't running today —
            # multi-day festival entries, after-hours-only spectaculars
            # when there's no after-hours event, etc.
            continue
        todays.sort(key=lambda x: x["start"])
        shows.append({
            "id": item.get("id"),
            "name": item.get("name"),
            "category": _classify_show(item.get("name", "")),
            "showtimes": todays,
        })

    # Sort by next-upcoming start time so "what's happening soon" reads
    # naturally. Shows whose performances are all in the past sort to
    # the bottom by name.
    now_iso = datetime.now(_EASTERN).isoformat()
    def _sort_key(show: dict[str, Any]) -> tuple[int, str]:
        for t in show["showtimes"]:
            if t["start"] > now_iso:
                return (0, t["start"])
        return (1, (show.get("name") or "").lower())
    shows.sort(key=_sort_key)
    return shows


def _next_upcoming_showtime(
    show: dict[str, Any], now_iso: str
) -> dict[str, str] | None:
    """First performance of `show` starting after `now_iso`, or None."""
    for t in show.get("showtimes", []) or []:
        if t["start"] > now_iso:
            return t
    return None


def _compute_load_vs_forecast(
    rides_out: list[dict],
) -> dict[str, Any] | None:
    """Compare each operating ride's current wait to today's forecast
    for the current ET hour. Aggregate into a park-level "today is
    running X% above/below forecast" signal.

    This is the always-on, point-in-time version of Phase C. Full
    forecast accuracy analytics (per-ride bias, time-of-day error,
    statistical confidence intervals) needs the per-poll wait history
    the C → B upgrade introduces. What we can do TODAY from existing
    data: snapshot ratio at the current moment.

    Aggregation is `sum(actual) / sum(predicted)` across sampled rides,
    which is equivalent to a wait-weighted mean of per-ride ratios.
    That weighting matters — a ride with predicted_wait=5 reporting
    actual_wait=20 is a 4x ratio but on tiny numbers (noise). A ride
    with predicted=60 reporting actual=75 is a 1.25x ratio on real
    minutes. Weighting by predicted wait pulls the signal toward the
    high-traffic rides that actually drive the user's experience.

    Excludes:
    - DOWN rides (no comparable forecast)
    - Rides with predicted wait <10 min (noise floor — small
      denominators produce flap in the ratio)
    - Rides missing either current STATE wait or a forecast entry
      for the current hour

    Returns None if no rides survive the exclusions (planner falls
    back to the raw forecast).
    """
    now_et = datetime.now(_EASTERN)
    current_hour = now_et.hour
    today_iso = now_et.date().isoformat()

    per_ride: list[dict[str, Any]] = []
    total_actual = 0
    total_predicted = 0

    for r in rides_out:
        if r.get("status") != "OPERATING":
            continue
        actual = r.get("wait_mins")
        forecast = r.get("forecast")
        if not actual or not forecast:
            continue
        # Find the forecast entry for the current ET hour today.
        # Upstream times are like "2026-05-10T17:00:00-04:00" — match
        # by date AND hour so we don't accidentally pick a previous
        # day's same-hour entry on overnight queries.
        predicted: int | None = None
        for entry in forecast:
            try:
                t = datetime.fromisoformat(entry["time"])
            except (KeyError, ValueError):
                continue
            t_et = t.astimezone(_EASTERN)
            if (
                t_et.date().isoformat() == today_iso
                and t_et.hour == current_hour
            ):
                predicted = entry.get("wait_mins")
                break
        if predicted is None or predicted < 10:
            continue
        ratio = round(actual / predicted, 2)
        per_ride.append({
            "ride_name": r["ride_name"],
            "actual_wait_mins": actual,
            "predicted_wait_this_hour": predicted,
            "ratio": ratio,
        })
        total_actual += actual
        total_predicted += predicted

    if not per_ride or total_predicted == 0:
        return None

    park_ratio = round(total_actual / total_predicted, 2)
    n = len(per_ride)

    # Confidence + interpretation. We don't have enough data to
    # compute formal confidence intervals (one snapshot per ride),
    # so this is sample-size + magnitude based.
    if n < 3:
        confidence = "low"
        interp = (
            f"Only {n} ride(s) sampled — treat the ratio as "
            f"directional only, don't lean on it heavily."
        )
    elif abs(park_ratio - 1.0) < 0.10:
        confidence = "high"
        interp = (
            f"Today running close to forecast ({int(park_ratio * 100)}% "
            f"of predicted, {n} rides sampled). Use forecast values as-is."
        )
    elif park_ratio > 1.0:
        pct = int((park_ratio - 1.0) * 100)
        confidence = "high" if n >= 5 else "medium"
        interp = (
            f"Today running ~{pct}% ABOVE forecast across {n} rides — "
            f"crowds heavier than predicted. Scale forecast peak values "
            f"by ~{park_ratio:.2f} when reasoning about cost-of-delay."
        )
    else:
        pct = int((1.0 - park_ratio) * 100)
        confidence = "high" if n >= 5 else "medium"
        interp = (
            f"Today running ~{pct}% BELOW forecast across {n} rides — "
            f"crowds lighter than predicted. Scale forecast peak values "
            f"by ~{park_ratio:.2f} when reasoning about cost-of-delay."
        )

    return {
        "park_load_ratio": park_ratio,
        "rides_sampled": n,
        "confidence": confidence,
        "interpretation": interp,
        "per_ride": per_ride,
    }


def _forecast_peak_in_window(
    forecast: list[dict], hours_ahead: int = 3
) -> dict[str, Any] | None:
    """Find the peak forecasted wait in the next N hours from now.

    Replaces the old full-horizon slope metric, which was misleading
    for non-monotonic forecasts: Pirates of the Caribbean humps
    10→40 (afternoon peak)→10 over the day, and a (last-first)/hours
    slope reports 0.0 — masking the real peak that an evening
    planner would walk into. Forward-looking peak captures the
    cost-of-delay signal that matters: "if you defer this ride,
    here's the worst wait you'd hit and how soon."

    Returns:
        Dict with peak_wait_mins, minutes_until_peak (from now), and
        peak_at (ISO timestamp). None if the forecast has no
        forward-looking entries with valid waits.
    """
    if not forecast:
        return None
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc + timedelta(hours=hours_ahead)

    peak_wait: int | None = None
    peak_time: datetime | None = None
    for entry in forecast:
        try:
            t = datetime.fromisoformat(entry["time"])
        except (KeyError, ValueError):
            continue
        # Compare in UTC so timezone-aware datetimes from upstream
        # (with `-04:00` offsets) compare cleanly.
        t_utc = t.astimezone(timezone.utc)
        if t_utc < now_utc or t_utc > cutoff:
            continue
        wait = entry.get("wait_mins")
        if not isinstance(wait, (int, float)):
            continue
        if peak_wait is None or wait > peak_wait:
            peak_wait = wait
            peak_time = t

    if peak_wait is None or peak_time is None:
        return None
    minutes_until = round(
        (peak_time.astimezone(timezone.utc) - now_utc).total_seconds() / 60, 1
    )
    return {
        "peak_wait_mins": peak_wait,
        "minutes_until_peak": minutes_until,
        "peak_at": peak_time.isoformat(),
    }


def get_planning_context(
    park: str, ride_names: list[str]
) -> dict[str, Any]:
    """One-shot planner context: live status + forecast + DOWN history
    + location + park hours + weather, all for a list of rides.

    Use this when the user is planning what to ride next. Replaces
    5-10 separate calls to get_live_ride_status / get_ride_forecast /
    etc. Single round trip; consistent timestamp across all rides.

    HOW TO USE THE RESPONSE WHEN ORDERING RIDES:

    **CRITICAL DATA CAVEAT — read this first.** Every live field in
    this response (`status`, `wait_mins`, `current_ll_offer`,
    `forecast`, `weather`, `today_vs_forecast`, `currently_down_in_park`,
    `showtimes`) reflects RIGHT NOW, TODAY. Nothing here predicts
    tomorrow or any future date. If the user is asking about a
    future-day plan ("we're going Saturday", "tomorrow's our EPCOT
    day"), this data still helps for typical-pattern reasoning
    (historical analytics, drop patterns, baselines), but DO NOT
    treat live values as predictions for that date — and DO NOT
    suggest specific actions that depend on availability data we
    don't have for that date (see the LL section below for the most
    common version of this trap). Building a future day or whole trip
    ahead of time IS supported, though — persist it dormant and
    activate it on the day; see section 0d.

    0a. **Before planning, check for unrecorded prior plans + calibrate
       against the user's track record.** Call get_user_plan_history
       (defaults to user_id="megan" for this single-user setup) BEFORE
       laying out today's plan. Two things to do with what you get back:

       - **Pending feedback prompt.** If a plan has outcome_recorded=false
         AND stale_for_recall=false (planned 1-14 days ago), ask once:
         "Before we plan today — how did your <park> day on <planned_for_date>
         go? Was it about right, did you run over, or have extra time?"
         Then call record_plan_outcome with what they say. If they reply
         "don't really remember," still call record_plan_outcome with
         free_text="user couldn't recall" so the row stops generating
         prompts. For plans where stale_for_recall=true, don't ask —
         briefly acknowledge ("I see you also planned at MK on the 1st,
         too long ago to ask about") and move on.

       - **Calibration via the pre-computed summary.** Read
         `calibration_summary` from the response (server-side
         aggregation; you don't need to derive anything from raw plans).
         Apply each bucket according to its `confidence` label and the
         summary's `usage_hint`:

           - **aggression + timing aggregates** — if either has an
             interpretation that's actionable (i.e., not the "balanced"
             / "current calibration is working" wording), surface it
             ONCE up front in your response: "Your last N plans
             finished with extra time, so I'm packing more in today."
             Don't restate per ride. Same convention as the section
             1.5 today_vs_forecast calibration mention.
           - **per_ride_prediction_bias entries** — apply by
             confidence:
               · high (n>=5): quote directly to the user when
                 relevant ("Big Thunder runs ~15 min longer than
                 forecast for you, so I'm scheduling 60 min for it
                 instead of 45")
               · medium (n=3-4): apply silently to your prediction
                 (don't mention; sample size doesn't justify a callout)
               · low (n<3): IGNORE. The signal is too noisy.
           - **per_show_arrival_bias entries** — same confidence
             rules. A "user arrives later than recommended" signal
             with high confidence may justify trimming arrival
             buffers; with low confidence, ignore.

       If `calibration_summary` is null, this is the first recorded
       trip — proceed with baselines and don't fabricate calibration.

    0b. **Check for after-hours parties on the planning date.** Call
       `get_party_calendar(date=<plan_date>)` early — before laying
       out the order — for any Magic Kingdom OR Hollywood Studios
       plan. Three parties currently tracked:
         - **MNSSHP** (Mickey's Not So Scary Halloween Party) — MK,
           Aug-Nov
         - **MVMCP** (Mickey's Very Merry Christmas Party) — MK,
           mid-Nov through late Dec
         - **Jollywood Nights** — Hollywood Studios, mid-Nov through
           mid-Dec, ~6-10 nights total
       Parties affect the plan in two big ways:

       - **Park closes early (typically 6pm) for non-party guests
         on party days.** If the user is planning MK on a party
         date AND hasn't mentioned a party ticket: surface this
         FIRST, before sequencing. "MK closes for non-party guests
         at 6pm on this date — your plan needs to fit in the
         9am-6pm window, or move to another park for the evening."
         Don't bury this in a footnote; it's a hard ceiling on the
         day's length.
       - **Daytime crowds run lighter than typical on party days**
         (locals avoid the early close). When `is_party_day_on_target_date`
         is true, treat predicted ride waits as if today_vs_forecast
         park_load_ratio were ~0.80-0.85. If you ALSO have a real
         load_ratio signal from the live data, combine them — don't
         double-count, just use the lower value.
       - **Evening party hours are the sweet spot for guests WITH
         party tickets.** Capped attendance, shorter waits than
         typical evening waits. Tell the user this if they're
         heading into the party.
       - **December non-party days are some of the highest-crowd
         days of the year.** If the user is planning a December
         MK day, the get_party_calendar response tells you which
         dates are party days nearby — and the surrounding
         non-party days are NOT lighter, they're the opposite
         (holiday tourists piling in). Worth flagging when the
         user has flexibility on which day to go.

       Hedge the language when the tool's `dates_status` is
       `estimated_from_typical_pattern_for_<year>` (the file hasn't
       been verified against Disney's published calendar yet): say
       "this LOOKS like it'll be a party day based on typical
       Disney scheduling — worth double-checking the official
       calendar before booking." Only assert confidently when
       `dates_status` is `verified_from_disney_calendar`.

       Skip the call for EPCOT or Animal Kingdom plans — neither
       park hosts after-hours parties. Always call for MK and HS.

       **When dates_status is "pending_disney_announcement"** (the
       Jollywood Nights case until Disney publishes the schedule):
       the dates array is empty, so the tool naturally returns no
       matches. But if the user is planning HS for Nov-Dec, mention
       that Jollywood Nights could fall during their trip dates and
       you can't verify yet — suggest they check the official
       schedule before locking in evening plans for those months.

    0c. **Fetch the living wisdom + preferences docs from Google Drive
       before planning.** Two Google Docs in the user's Drive carry
       context that's deliberately kept editable outside the codebase:

       - **"Disney Wisdom"** — global operational tactics the user has
         learned and wants applied to every plan: LL strategy
         (SLL vs MLL distinction, the burner-ride trick, scan-in
         timing windows), park-mechanics gotchas (SLL scan-in doesn't
         unlock tier-1 bookings), annual-passholder workarounds.
         Shared across all family members.
       - **"Disney Planner Preferences"** — per-person personal
         preferences. Sectioned by name (e.g., `## Megan`,
         `## Mark`, `## Karen`). Read the section matching the
         intended user — default to Megan (matches `user_id="megan"`);
         if the prompt names someone else ("plan for my husband"),
         switch to their section. If it says "for the family" or
         "for all of us," read every section and synthesize.

       **How to fetch.** Use the user's Google Drive MCP tools
       (typically prefixed `mcp__claude_ai_Google_Drive__` or similar
       depending on their setup). The flow is:
         1. `search_files` with `title contains 'Disney Wisdom'` →
            note the file id from the result.
         2. `read_file_content` with that id → get the doc text.
         3. Repeat for `'Disney Planner Preferences'`.

       **Fail-soft.** If the Drive MCP isn't loaded, the search
       returns no matches, or the read fails, proceed without that
       doc's content. The docstring already carries the load-bearing
       planning rules; the docs are enhancement, not blocking. Note
       in your response that you couldn't access the doc if it would
       have mattered (e.g., user asks about LL strategy and you
       couldn't fetch wisdom).

       **Precedence hierarchy when sources conflict** (apply highest
       authority first, fall through to lower tiers when silent):

       1. **Park reality** — wisdom-doc facts about how Disney's
          systems actually work, plus hard physical constraints (park
          hours, ride heights, ride-down state). These describe the
          world, not anyone's preferences. Non-negotiable. If the
          prompt or preferences appear to demand the impossible (e.g.,
          "use the SLL scan to unlock my MLL tier-1 booking"), surface
          the real constraint politely and offer the closest viable
          alternative.
       2. **Current prompt** — what the user just typed wins for
          today's intent. "Plan for 4 hours" trumps a preferences
          entry saying they typically do 6-hour days. Can selectively
          override docstring assumptions if the user reasons through
          it ("I know this is aggressive, I'm willing to skip lunch").
       3. **Preferences doc (per-person section)** — standing personal
          tendencies. Apply as defaults when the prompt is silent.
          "Skip spinning rides" applies unless today's prompt says
          "I want to ride Mad Tea Party for once."
       4. **Wisdom doc tactics** — operational strategies like the
          burner-ride trick or LL pre-booking sequencing. Apply when
          they fit the situation. The prompt or preferences can opt
          out ("I don't want to bother with the burner trick today").
       5. **Planner framework (this docstring)** — the cost-of-delay
          math, today_vs_forecast scaling, sequencing approach, and
          tool-routing rules. Base method. Everything above tunes it
          but rarely overturns it entirely.

       When you detect a conflict you can't auto-resolve (e.g.,
       preferences say "no thrill rides," prompt says "plan TRON"),
       go with the higher-authority source and briefly mention the
       conflict you noticed so the user can confirm. Don't silently
       drop either signal.

    0d. **Multi-day trips: build the days ahead, activate each on its
       day.** Magic Monitor supports laying out a FUTURE trip in advance
       and only switching on live monitoring once each day arrives.
       Three moments to recognize:

       - **Session start — is a trip already in flight?** Alongside the
         get_user_plan_history check in 0a, call get_upcoming_trip once.
         If it returns a trip (today is on or before its end_date), lead
         with it: "You've got your <name> trip, <start>–<end> — want to
         keep building it, or is today one of its days?" Skip the prompt
         only when the user's message already makes the day's intent
         obvious. And when the user just wants to SEE a specific day —
         "what's the plan for the 14th?" — call get_plan_for_day(date=...)
         and show the stored plan. That's a read-only lookup, NOT the
         on-the-day activation flow below: don't activate a future day,
         and don't narrate this call's live waits as that date's.
       - **Building ahead (a date that isn't today).** When the user
         wants a day they're not at yet ("plan our June 23–25 trip",
         "rough out next Saturday's MK day"), PERSIST it dormant instead
         of narrating this call's live data as that date's reality: use
         create_trip for several days at once, or record_plan with
         planned_for_date for a single future day. Dormant plans fire NO
         disruption alerts (see section 7). Lean on this call's data only
         for typical-pattern reasoning (analytics, drop patterns,
         baselines), and say so — "waits below are typical for that day,
         not a live read."
       - **On the trip day — re-evaluate, THEN activate.** When the user
         asks "what's my plan today?" on a day that was pre-built, pull
         the stored plan with get_plan_for_day, then re-check it against
         THIS call's live data for that park (what's DOWN now, today's
         real forecast + weather, confirmed hours). Walk the user through
         any changes; once they accept, call activate_plan with the
         re-evaluated ride_sequence + a resolved plan_window. That flip is
         what starts disruption monitoring — a dormant plan stays silent
         until you activate it. Don't activate a future day early (it
         would fire alerts for rides nobody's near yet) — activate on the
         day, after re-evaluation.

    0. **Discover hard constraints first.** If the user gives you a
       multi-ride list without mentioning any of these, ASK ONCE
       before laying out the order — they materially change the plan
       and users often forget to volunteer them:
       - Table-service dining reservations (fixed start time, ~60-90
         min hold; missing the slot loses the reservation entirely)
       - Genie+ / Multi-Pass Lightning Lane reservations (1-hour
         return window per ride; standby wait effectively drops to
         ~10-15 min for that ride during its window)
       - Individual Lightning Lane / ILL (paid per-ride for top-tier
         attractions like TRON or Guardians; same 1-hour shape)
       - Virtual queue boarding groups (TRON, Tiana's, etc. — return
         window is set when the boarding group is called). NOTE:
         Guardians of the Galaxy: Cosmic Rewind is NO LONGER
         virtual-queue-only — it now runs a standard STANDBY queue (plus
         paid ILL). Older training data says VQ-only; that's stale. Treat
         it as a normal standby ride: it has a live wait time, can be
         sequenced like any other ride, and does NOT require a boarding
         group.
       - Shows worth planning around. The top-level `showtimes` field
         lists today's headliner spectaculars / parades / stage shows
         that haven't started yet. If any look marquee (Happily Ever
         After, Festival of Fantasy, Fantasmic, Festival of the Lion
         King, etc.) and the user hasn't said one way or the other,
         ASK ONCE: "I see X at <time> and Y at <time> running tonight
         — want to fit either in?" Treat any selected show as a
         ~20-45 min fixed hold (see section 5.5 for the full mechanics).
       Skip the question if the user already mentioned them. Treat
       each as a hard slot in the schedule and sequence other rides
       around it. When the user has an LL/ILL for a ride, note that
       you're skipping the standby line entirely for that ride.

       **Lightning Lane scheduling mechanics (practical detail
       Claude might not naturally know):**
       - **LL window grace — three layers, use the right one.**
         The 1-hour window has both stated and unofficial buffers:
           - **5 min before window opens** — Disney consistently
             allows early entry. Always safe to plan against.
           - **15 min after window closes** — stated policy. Always
             safe to plan against.
           - **Up to ~2 hours after window closes** — widely tested
             and consistently honored at the tap point, but NOT
             stated Disney policy. Enforcement varies by day, park,
             and CM. Treat this as crisis-mode capacity, not a
             default planning buffer.
         **Default policy:** use 5-min-early + 15-min-late as the
         "always reliable" buffer that doesn't need flagging.
         Mention the assumption ONCE per itinerary: "Plan assumes
         typical Disney grace on LL windows (~5 min early / ~15 min
         late). If grace tightens on a given day, fall back to the
         nominal windows." Don't re-flag for every individual ride.
         **For the 15-45 min late zone:** mention the extension
         where it's load-bearing for the plan ("running ~30 min late
         to this slot — still within informal grace") but don't
         re-flag per ride.
         **For the 45 min - 2 hr late zone:** explicitly warn the
         user that they're leaning on informal-informal grace that
         Disney doesn't publish: "This pushes your 8:30 LL window
         to ~10:25 arrival — widely tested but unofficial. If a CM
         enforces strictly on this day, that slot won't be honored.
         Reserve this for situations where strictness would cost
         you the marquee ride entirely; otherwise sequence earlier."
         Switch to conservative-only mode (no grace at all) only
         when the user explicitly asks.
       - **Pre-arrival booking — tier mechanics + the 3-ride
         allocation.** Multi-Pass / Genie+ lets guests pre-book up
         to 3 LL rides before arriving at the park. The allocation
         rule depends on park:
           - **Magic Kingdom, EPCOT, Hollywood Studios:** rides are
             split into Tier 1 (the marquees) and Tier 2 (everything
             else). Of the 3 pre-bookings: exactly **1 Tier 1 + 2
             Tier 2**. You cannot pre-book 3 Tier 1 rides.
           - **Animal Kingdom:** no tiers — any 3 rides.
         **Call `get_mll_tiers(park)` for the authoritative current
         tier snapshot** rather than relying on the examples below.
         The tool returns the full Tier 1 / Tier 2 lists (or the
         no-tiers AK note), plus an `updated_at` date so you can
         tell the user how fresh the data is. The examples below
         are illustrative; the tool is the source of truth.

         Tier 1 examples (Disney revises these periodically; verify
         in the user's app if uncertain):
           - MK: TRON Lightcycle/Run (also an ILL), Seven Dwarfs
             Mine Train, Jungle Cruise, Peter Pan's Flight, Big
             Thunder, Space Mountain, Tiana's Bayou Adventure
           - EPCOT: Test Track, Frozen Ever After, Remy's Ratatouille
             Adventure, Guardians of the Galaxy: Cosmic Rewind (also
             an ILL)
           - HS: Slinky Dog Dash, Mickey & Minnie's Runaway Railway,
             Star Tours, Toy Story Mania, Rise of the Resistance
             (also an ILL)
         ILL (paid per-ride) rides like TRON, Cosmic Rewind, and
         Rise of the Resistance are sometimes listed under Tier 1
         in the booking UI but they're a SEPARATE product —
         purchasing ILL does NOT count against the 3-ride MLL pre-
         book allocation. A guest can hold 3 MLL pre-bookings AND
         however many ILLs they buy.
         When recommending pre-arrival picks for MK/EPCOT/HS:
         honor the 1+2 split. Pick the user's highest-value Tier 1
         (highest typical wait + most-wanted) and two strong Tier 2
         picks. Don't suggest 2 Tier 1 + 1 Tier 2 — they cannot
         book that combination.
       - **Day-of booking unlocks — scan into MLL to refill the
         queue.** Once the guest scans into an MLL pre-booking at
         the tap point, the LL system unlocks the next booking:
         they can book ONE additional MLL ride **from any tier**
         (the 1-Tier-1 restriction lifts entirely after the first
         MLL scan). This loops — each subsequent MLL scan unlocks
         another. Two constraints:
           - **Scanning into an SLL (paid ILL) ride does NOT
             unlock more MLL bookings or lift tier restrictions.**
             Riding Cosmic Rewind, TRON Lightcycle, or Rise of the
             Resistance via the ILL purchase doesn't help the MLL
             cadence at all.
           - **Sequence MLL pre-bookings BEFORE any SLL/ILL when
             possible.** If a guest has both MLL pre-bookings and
             ILL purchases for the same morning, route them to the
             MLL FIRST so the unlock fires immediately and they can
             start building toward booking #4, #5, etc. Burning the
             ILL first wastes the morning window where you could be
             accumulating MLL slots.
         When planning a guest's day with active MLL pre-bookings:
         the recommended next LL (see the recommendation engine
         below) typically targets right after the first scan, when
         the tier restriction has lifted and the strongest available
         Tier 1 wait becomes bookable.
       - The LL line itself is NOT zero-wait. Plan ~10-15 min for
         the LL queue (can hit 20-25 min during peak hours). Total
         time from "tap in" to "off the ride" is usually ~25-30 min.
       - Factor in walking time to reach the LL window. If the user
         is across the park (>500m by the lat/lon data) when the
         window opens, build in the walk — don't have them book-end
         the window with travel time on both sides.
       - If two LL windows overlap, the user has to pick one. Flag
         the conflict and ask which is the priority.
       - **When you have to push past the end of one LL window,
         prefer pushing the Individual Lightning Lane (ILL, paid
         per-ride) over the Multi-Pass / Genie+ (bundled).** Empirical
         pattern: ILL CMs tend to be more lenient about late
         arrivals than Multi-Pass CMs — paid-per-ride creates a
         "we charged you $20, we're not turning you away" dynamic,
         and ILL queues are typically shorter so a late guest is
         less disruptive. Multi-Pass enforcement is more variable
         and often stricter. So when sequencing forces a late
         arrival on one of two competing LLs, route on-time to the
         Multi-Pass and apply the late grace to the ILL. Same logic
         when only one of two LLs is reachable in time without
         skipping a planned ride: be on-time to the Multi-Pass,
         late to the ILL. (User-stated experience as of 2026-05-10
         — if Disney standardizes enforcement across products this
         rule may need revisiting.)
       - **Recommend the next LL to book when the user has Multi-Pass
         active.** Multi-Pass users can hold one LL at a time and
         book the next once the current one is used (or tapped in).
         When the user mentions they have Multi-Pass / Genie+ active
         — or is asking for a plan after a long park stretch where
         it's reasonable to assume — proactively suggest which ride
         to book next. Don't wait for them to ask.

         Selection criteria, in order:

         1. **Filter:** rides on the user's wishlist that are
            (a) currently OPERATING, (b) have a `current_ll_offer`
            with a `return_start` falling within the user's planned
            remaining park time, (c) haven't already been done today
            (ask if uncertain — Claude has no built-in "done" signal
            beyond what the user reports).

         2. **Score each candidate by time saved:**
              ll_value = current wait_mins - 15 min LL queue
            Higher is better — an LL on a 60-min standby saves ~45
            min; an LL on a 25-min standby only saves ~10. If the
            current wait is below ~25 min, the LL is rarely worth
            burning on that ride.

         3. **Tiebreakers (apply when top picks are within ~10 min
            of each other on time saved):**
            - **Plan compatibility:** does the LL return window fall
              during a stretch the user would already be in that
              land? Bonus if yes (e.g., Big Thunder LL returning at
              3 PM while the user planned to be in Frontierland
              anyway).
            - **Proximity:** walking distance from the user's current
              location or their next planned ride. Use lat/lon and
              haversine. Closer = bonus.
            - **Cost-of-delay survivor:** if the ride's
              `forecast_peak_next_3h_mins` is much higher than its
              current wait, the LL locks in today's lower value
              against the predicted peak.
            - **Down-risk avoidance:** rides with high
              `downtime_pct` in their historical analytics carry
              more risk if you commit an LL slot to them. Slight
              negative weight.

         4. **Recommend top 1-2 with explicit reasoning.** Don't
            just name the ride; show the math: "I'd book Big Thunder
            next — it's showing 65 min standby and its LL is
            returning at 3:15 PM. That's ~50 min saved vs standby,
            and you'd be in Frontierland for Pirates around that
            time anyway. Second choice would be Space Mountain (40
            min saved, less convenient return window)."

         **How to incorporate into the plan you propose:**

         - Lay out the plan ASSUMING the recommended LL gets booked.
           Mark that ride's slot in the sequence with an explicit
           "assuming Big Thunder LL at 3:15 PM" note rather than
           hiding the assumption. Predicted wait for that ride
           drops to ~15 min (LL queue) for plan-time purposes.
         - When you call record_plan to persist the plan, encode
           the assumption in the ride_sequence entry — e.g.,
           {"ride_name": "Big Thunder", "predicted_wait_min": 15,
           "position": 3, "notes": "assumes Multi-Pass LL booked
           at 3:15 PM"} — so the feedback loop can later compare
           predicted-vs-actual if the booking diverged.
         - Tell the user upfront that the plan is contingent on
           the booking: "If you book something else when the window
           opens, tell me what you got and I'll re-sequence the
           rest of the day. I'm assuming Big Thunder at 3:15 here
           — if Disney offers you a different time slot or you
           pick another ride, that's the trigger for a quick
           replan."

         **When the user reports back what they actually booked:**

         - If they booked the recommendation: nothing to do. Plan
           continues as written; the predicted_wait_min on that
           ride stays at LL-queue-time.
         - If they booked something different: prefer to PATCH the
           existing plan in place rather than creating a fresh one.
           See "Mid-trip plan adjustments" below for the
           remove_ride_from_plan / add_ride_to_plan flow.

       - **Mid-trip plan adjustments — use the right tool per
         situation.** Four tools mutate the active PLAN# row in
         place (no new row, no outcome recorded; the plan stays
         in-flight). Choose the one that matches the user's
         actual signal:

           - `mark_ride_complete(plan_id, ride_id, ride_name,
             actual_wait_min?, notes?)` — user RODE the ride. Use
             this whenever the user reports finishing a ride
             ("we just got off Pirates", "Big Thunder was 35 min,
             not bad"). Moves the ride from ride_sequence into
             completed_rides with a completed_at timestamp. Pass
             actual_wait_min when the user mentions a wait — that's
             the strongest signal for the calibration loop's
             per-ride prediction bias. **Prefer this to
             remove_ride_from_plan whenever the user actually
             rode the thing.**
           - `remove_ride_from_plan(plan_id, ride_id, ride_name,
             reason?)` — user SKIPPED the ride. Use this only when
             the user explicitly abandons a ride from their day
             ("we're not doing Space Mountain, wait's too long",
             "let's drop Mansion, we ran out of time"). Moves the
             ride into dropped_rides with a dropped_at timestamp
             + optional reason. NEGATIVE signal for the calibration
             loop — contributes to "plan was too aggressive"
             pattern. Calling this for a ride the user actually
             rode undercounts completions in their history.
           - `add_ride_to_plan(plan_id, ride_id, ride_name,
             predicted_wait_min?, position?, notes?)` — user added a
             SPONTANEOUS ride that wasn't in the original ("we're
             grabbing Pirates while we're nearby"). Adds to
             ride_sequence; poller starts watching within ~2 min.
           - `add_ride_to_plan` + `mark_ride_complete` —
             retroactively log a ride the user did that wasn't
             planned. Add it first (so it's recorded with predicted
             wait if you remember what it was), then mark complete
             with the actual wait.

         **Common in-trip narrative flow** Claude should reach for:
           - "we just finished Pirates, 12 min wait" → mark_ride_complete
           - "we're skipping Space Mountain" → remove_ride_from_plan
           - "we're grabbing Carousel too" → add_ride_to_plan
           - "I booked TRON instead of the Big Thunder LL you
             recommended" → remove_ride_from_plan(Big Thunder,
             reason="swapped LL") + add_ride_to_plan(TRON, notes=
             "actual ILL booked")
           - "Big Thunder went down, we'll come back later" → leave
             it alone — the existing plan-disruption alert covers
             this; the ride is still in ride_sequence and the user
             may still ride it once it's back.

         **When to use add/remove vs. a full replan:**
           - **Use add/remove** for small in-the-moment changes:
             user swaps one LL booking, decides to skip a single
             ride, adds a spontaneous one. Preserves the plan
             history including predictions for unchanged rides
             (matters for the feedback loop's calibration data).
           - **Use full replan** (record_plan_outcome on the prior
             plan + fresh get_planning_context + new record_plan)
             when the day fundamentally shifts: weather pivots
             everything indoor, user changes parks mid-day, half
             the wishlist gets dropped. Anything that changes 3+
             items in one go is a fresh plan.

         **Sequence for the most common case — user swaps an LL
         booking:** `remove_ride_from_plan` (the ride whose LL you
         recommended but they didn't get) → `add_ride_to_plan`
         (the ride they actually got, with notes="actual LL booked
         instead of recommended X"). Both calls return cleanly; the
         poller catches the change on its next 2-min cycle.

       - **Suggest modifying LLs ONLY when the data supports it.**
         Disney Genie+ / Multi-Pass / ILL reservations can be
         modified through the app, but the planner only has visibility
         into one signal: each ride's `current_ll_offer`, which is
         the GLOBAL next-available LL return time being offered RIGHT
         NOW for TODAY (not user-specific — same number Disney shows
         anyone booking fresh). Disney's API does NOT expose a full
         inventory map, future-day inventory, or per-slot availability.

         **Hard refusal rule:** never suggest moving an LL to a
         specific time without a concrete signal supporting it. The
         three valid scenarios:

         **(A) Today, target ≥ current_ll_offer.return_start** —
         the only "we have data" case. Phrase with appropriate
         hedging: "The 7-8 PM range looks LIKELY available — current
         next-available is 6:30 PM, so anything ≥ 6:30 should be
         bookable. Specific times within that range can still be
         sold out though, so check the app to confirm 7-8 PM
         specifically before committing." Do NOT phrase as "is
         open" or "is available" — that overstates what we know.

         **(B) Today, target < current_ll_offer.return_start** —
         the target is gone. Tell the user honestly: "7-8 PM appears
         to be sold out — earliest available now is 8:30 PM. Your
         5-6 PM is still the best slot you have access to."

         **(C) `current_ll_offer` is missing OR the user is planning
         a future date** — REFUSE to suggest specific times. We have
         no data. Do this instead:
           - Acknowledge the gap honestly: "I can't see tomorrow's
             LL inventory — Disney doesn't publish that, and the live
             data here is today-only."
           - Reference `ll_drop_pattern.top_drop_hours_et` as TYPICAL
             refresh windows (NOT availability claims):
             "Big Thunder's LL slot most commonly refreshes around
             11 AM, 2 PM, and 5 PM ET — when you're booking, those
             are the historically best times to check the app for an
             earlier slot." Pure pattern-of-life data, no claim
             about specific dates.
           - For tomorrow specifically, also note the booking timing:
             Multi-Pass opens at 7am day-of for most guests; Deluxe/
             DVC guests can book Multi-Pass 7 days ahead and ILL up
             to 7 days ahead. The user should react to actual
             availability when the booking window opens, not to any
             plan we propose.

         **When to proactively trigger this reasoning:** when the
         current LL window creates an awkward fit for TODAY
         (requires backtracking, conflicts with dining, forces a
         worse ride order, pushes against park close). Don't suggest
         a swap if the current slot is fine. Don't suggest a swap
         AT ALL for a future-date plan — wait until they're at the
         park and ask in real time.

         **`ll_drop_pattern` field reference (historical only):**
         `drops_per_active_day` tells you how frequently this ride's
         LL refreshes (8+ per day is "very active," <1 is "rare").
         `typical_shift_minutes` tells you how big a refresh usually
         is — if it's 30 min, the swap window opens half an hour
         when refreshes happen; if it's 4 hours, the slot can jump
         dramatically. These describe the ride's typical refresh
         behavior, not today's specific inventory state.

    1. **Cost-of-delay rule** (most important). The fields you want:
       `forecast_peak_next_3h_mins` (worst forecasted wait in the next
       3 hours) and `forecast_minutes_until_peak` (how soon that peak
       hits). For each ride, marginal cost of deferring it ≈
       max(0, forecast_at_deferred_time - current_wait_mins). Order
       by DESCENDING cost-of-delay, NOT by ascending current wait.
       Show your math: "If I do TRON first (~80 min round trip),
       Pirates' wait at +80 min would be ~40 (its peak hits in 60
       min and holds), so deferring Pirates costs +30 min. If I do
       Pirates first (~25 min), TRON's wait at +25 min is still ~85
       (flat over the next 3h), so deferring TRON costs ~0. Pirates
       first." The full `forecast` array is also returned so you can
       look up exact future-wait values at specific times when needed.

    1.5. **Today-vs-forecast correction.** The top-level
       `today_vs_forecast` field compares each operating ride's
       CURRENT wait to what today's forecast predicted for the
       current ET hour, aggregated park-wide. If `park_load_ratio`
       is materially different from 1.0 (>10% off), today's actual
       crowd is heavier or lighter than the forecast model expected.
       When reasoning about cost-of-delay, scale forecast peak
       values by this ratio. Example: ratio=1.23, forecast says
       Pirates peaks at 40 in 60 min → expected actual peak is
       ~49 min. Note the calibration ONCE in your response ("Today
       is running 23% above forecast — I've adjusted the peak
       estimates accordingly") rather than re-mentioning it per
       ride. Confidence levels: `low` (<3 rides sampled, treat as
       directional only); `medium`/`high` (5+ rides, reliable
       enough to scale by). If confidence is low or the field is
       absent, use forecast values as-is.

    2. **DOWN-state rides.** Two different causes, two different
       return-time models — diagnose before predicting.

       a) **Weather-caused downtime (outdoor rides during a storm).**
          Diagnostic checklist, in order of confidence:

          - **Strongest signal: multiple simultaneous outdoor downs.**
            Check `currently_down_in_park`. If 3+ outdoor rides went
            DOWN within ~20 min of each other AND it's currently
            storming (weather.current.weather_code 95/96/99 or high
            precip), weather causation is near-certain. Single-ride
            DOWN during a storm could be coincidence; concurrent
            outdoor downs is essentially proof.
          - **Medium signal: one outdoor ride DOWN, recent down_since,
            storm now.** Likely weather, but acknowledge uncertainty
            ("could be weather or coincident mechanical").
          - **Weak signal: outdoor ride DOWN for hours, storm just
            arrived.** Cause is probably mechanical-then-weather-
            prolongs; reopening still waits for storm clear regardless.

          When weather causation applies, the historical cluster
          median does NOT — that data mostly captures mechanical
          breakdowns. Return-time prediction follows Disney's lightning
          rule: outdoor rides resume ~30 min after the last lightning
          strike. Use weather.next_6h to find when the storm clears
          (weather_code drops back to <80) and add ~30 min.

          Example: "Big Thunder, TRON, and Splash all went DOWN
          within 15 minutes of each other and it's currently
          thunderstorming — this is a park-wide weather closure, not
          mechanical. Forecast shows storms clearing by 5:45 PM, so
          expect all three back around 6:15 PM. Indoor rides are
          unaffected — do Mansion or Pirates during the closure."

          Indoor rides DOWN during the same storm are still mechanical
          (the rain isn't why they're broken) — apply cluster math.

       b) **Mechanical downtime (everything else).** Use
          `down_duration_mins`, `typical_down_cluster_mins`, and
          `cluster_progress_pct` as before. Near 0% = early, plan
          ~typical more min; near 100% = could come back any minute.

       c) **Pre-closing rule — DOWN rides late in the park day.** If
          a ride transitions to DOWN within the last ~30-45 minutes
          of park close (`park_hours.minutes_until_close <= 45` at
          the moment it went down, derivable from `down_since` +
          park_hours), **assume it won't reopen for the day** and
          treat it as gone in the plan. Disney's late-day operational
          posture favors keeping a ride down over a hurried restart
          for a few more minutes of cycles. Communicate this to the
          user as "given how late this went down, plan as if it's
          out for the night — if it does come back, that's a bonus."

          Exception: rides on a known short-recovery pattern (Pirates,
          Mansion, Carousel-style mechanical resets typically back
          within 15 min — check `typical_down_cluster_mins` for the
          specific ride; if the historical median is <20 min, the
          pre-closing rule is weaker because the ride genuinely can
          come back fast).

          Pre-closing rule does NOT apply to weather-caused downtime
          — that's already handled by 2a (the storm-clearing-time
          model). Apply the pre-closing rule only to mechanical-
          downtime cases (2b).

       d) **Time-of-day calibration for return predictions.** The
          historical `typical_down_cluster_mins` value is a single
          all-time average across hours. Real downtime patterns
          vary by hour-of-day: a ride going DOWN at 11 AM has a
          very different expected recovery profile than one going
          DOWN at 9 PM. When the predicted return time is decision-
          load-bearing for the plan (e.g., user is deciding whether
          to wait or sequence around it), call
          `get_ride_dow_pattern(ride_name)` and look at the cell
          for the current (day-of-week, hour). The per-hour
          downtime pattern gives a tighter prediction than the
          all-time median.

          Apply the hour-adjusted estimate visibly when the
          difference vs the all-time median is meaningful (>15 min
          or >30% relative). Quote both numbers to the user:
          "Historically this ride averages 45 min outages, but for
          the 8 PM hour specifically it's averaged 75 min on
          previous Saturdays — given that, I'd sequence around it."

          When the per-hour sample is thin (`get_ride_dow_pattern`
          reports `n` below ~5 for the cell), fall back to the
          all-time median and don't make a confidence claim. Same
          confidence-by-sample-size convention as the calibration
          loop in `get_user_plan_history`.

       NEVER infer return time from `current_ll_offer.return_start` —
       Lightning Lane offers are unrelated to operational status.

       Note for users: the planner doesn't currently store historical
       weather, so we can't perfectly correlate "when the ride went
       down" with "when the storm started." Use current weather +
       down_since timing as the heuristic and acknowledge the
       uncertainty when relevant ("if the storm started before this
       ride went down, weather is the likely cause").

    3. **Proximity grouping.** Each ride has `location.lat/lon`. Use
       haversine distance to identify clusters (rides within ~250m
       are in adjacent lands). Other things equal, prefer back-to-back
       rides in the same cluster to reduce walking.

    4. **Feasibility check — warn the user if the wishlist won't fit.**
       BEFORE presenting the order, sanity-check that the plan fits
       in the time available. Estimate per ride:
         time_per_ride ≈ current_wait_mins + 10 min ride duration
                         + walking time to next ride
       Walking: ~5 min for adjacent lands (<300m), ~10 min for cross-
       park hops (>500m, e.g. Adventureland to Tomorrowland). Use the
       lat/lon distances to be specific.
       Total budget = `park_hours.minutes_until_close` minus dining
       hold(s) minus reservation/LL window buffers minus a 30-min
       safety margin (bathroom, longer-than-forecast queues, etc.).
       If total_estimated > total_budget, **flag it explicitly and
       generate 2-3 alternate full plans** rather than asking the
       user "which to drop." Each alternate should be a complete
       ordered itinerary so the user can compare lived experiences,
       not just lists of dropped rides. Format each as a short
       labeled bundle:

         **Plan A — "Skip TRON, do everything else"**
         1. Pirates (10 min, 6:00 PM)
         2. Big Thunder (60 min, 6:30 PM)
         3. Haunted Mansion (recovers ~6:30, do at 7:00)
         4. Space Mountain (last, recovers ~8:30)
         Tradeoff: skips the marquee coaster but fits 4 rides
         comfortably.

         **Plan B — "TRON-focused: 3 rides including TRON"**
         1. TRON (65 min, NOW)
         2. Big Thunder (next door after TRON, 60 min ~7:30)
         3. Space Mountain (last, recovers ~8:30)
         Tradeoff: skips both Mansion and Pirates to lock in TRON
         + late-night Space Mountain.

         **Plan C — "Adventureland cluster: 3 quick rides nearby"**
         1. Pirates (10 min)
         2. Haunted Mansion (when it recovers ~6:30)
         3. Big Thunder (adjacent to Mansion)
         Tradeoff: tightest walking, drops the Tomorrowland rides
         but you finish with energy left for fireworks/dining.

       Pick alternatives that highlight DIFFERENT tradeoffs — drop
       one big ride vs drop several small ones; cluster by proximity
       vs spread across the park; drop based on uncertainty (DOWN
       rides) vs drop based on cost. 2-3 plans is the right number,
       not 5. Then ask "Which plan fits your priorities?" so the
       user picks intent rather than reverse-engineering a list of
       skipped rides.

    5. **Meal/break windows.** If the user mentions wanting to eat or
       take a break in a specific time window ("quick-service dinner
       between 5-7pm" / "let's stop for snacks around 3"), treat it
       as a fixed ~30-45 min hold in the schedule and sequence rides
       to put them in the right area when the window starts. Use your
       general knowledge of WDW dining locations (Pecos Bill's in
       Frontierland, Cosmic Ray's in Tomorrowland near Space Mountain,
       Pinocchio Village Haus in Fantasyland, Columbia Harbour House
       in Liberty Square near Haunted Mansion, etc.) to suggest a
       specific spot near whichever ride you'd be at when the window
       opens. Acknowledge this is a general-knowledge suggestion, not
       a live-data lookup — wait times and operating status of
       restaurants aren't currently in MM's data.

    5.5. **Showtimes the user wants to catch.** The top-level `showtimes`
       field is a headliner-only subset of today's entertainment lineup
       (spectacular / parade / stage), filtered to performances that
       haven't started yet. Each entry has `category`, `name`, and
       `remaining_today` — a list of {start, end} ISO timestamps for
       this show's upcoming performances.

       Treat selected shows as fixed time-blocks. Walk backward from
       the start time when sequencing: the previous ride must finish
       (wait + ride duration + walk to viewing spot + early-arrival
       buffer) before showtime. If the math doesn't work, swap a
       different ride into the slot or warn the user the show is at
       risk.

       **Crowd-scale the arrival recommendations.** The arrival times
       below are baselines for an average-crowd day. The same
       `today_vs_forecast.park_load_ratio` signal that scales ride
       waits in section 1.5 applies to show arrival times — popular
       viewing spots fill up proportionally to the park's actual
       crowd level, not its forecast. Apply roughly:
         - ratio > 1.20 (heavy) → multiply baseline arrival by ~1.4-1.5x
           (60-90 min Fantasmic baseline becomes 90-130 min)
         - 1.05 ≤ ratio ≤ 1.20 (slightly heavy) → multiply by ~1.2x
         - 0.85 ≤ ratio ≤ 1.05 (typical) → use baseline as-is
         - ratio < 0.85 (light) → multiply by ~0.7-0.8x
       If `today_vs_forecast` is None or `confidence` is "low", use
       baselines as-is and acknowledge the uncertainty ("hard to gauge
       crowds today — these are typical-day arrival times, add 30 min
       on top if the park feels packed when you arrive"). Mention the
       scaling ONCE per response (same convention as section 1.5
       calibration) rather than per-show.

       Weather is a secondary modifier: if `weather.next_6h` shows
       rain clearing right before showtime, expect the venue to fill
       faster than usual once the rain stops (people who were waiting
       it out indoors all converge at once). For Fantasmic
       specifically, heavy rain can cancel the show — don't promise
       a 90-min hold if the forecast looks thunderstorm-y.

       **Spectaculars (fireworks / projection finales)** — ~15-30 min
       performance. These typically gate park-departure timing: most
       guests leave shortly after, so the post-finale ~30 min is the
       worst time to queue (mass exit crowds). Don't put a long-wait
       ride immediately after a spectacular. Per-park notes:

       - **Happily Ever After (Magic Kingdom):** ~18 min, projection
         mapping on the castle. The iconic spot is the Hub directly
         in front of the castle (best castle view, gets crowded
         60-90 min before). Main Street curbs are the popular
         alternative — arrive 30-45 min early for a clear sightline.
         The Tomorrowland bridge is an off-angle alternative with
         less crowd if the user is okay with a side view. After the
         finale, Main Street becomes a slow-moving river of exits
         for ~30-45 min. If the user has a Tomorrowland ride after,
         that's fine — the queues there clear quickly. Avoid
         scheduling a Frontierland or Adventureland ride post-finale
         unless they're willing to fight the exit crowd to get there.
       - **Disney Starlight: Dream the Night Away (Magic Kingdom):**
         ~20 min, a newer nighttime parade-spectacular hybrid that
         winds the parade route. Same viewing-spot logic as Festival
         of Fantasy below applies, with the added consideration that
         this is a nighttime show — Frontierland start position has
         the LEAST castle ambient light, Main Street near the castle
         has the most. Often runs a second performance ~2 hours
         after the first on busy nights.
       - **Luminous The Symphony of Us (EPCOT):** ~17 min, fireworks
         and barges on World Showcase Lagoon. Best viewing is
         anywhere along the lagoon perimeter — the show is designed
         to look good from all sides. Showcase Plaza (front of WS)
         is the densest viewing area. Japan, Italy, and UK pavilions
         are popular alternative spots. International Gateway side
         (between France and UK) usually has more breathing room and
         is the closer exit if leaving via Boardwalk hotels. Arrive
         30-45 min early for a clear lagoon sightline at the popular
         spots; 15 min for the lesser-trafficked sides.
       - **Fantasmic! (Hollywood Studios):** ~30 min, in the 6,900-
         seat Hollywood Hills Amphitheater (uniquely sit-down — the
         only WDW spectacular with assigned seating). Arrive 60-90
         min early for a good seat, 30 min for standing room.
         Dining packages with a few HS table-service restaurants
         include reserved seating. After Fantasmic, the amphitheater
         empties onto a single narrow path — expect 20-30 min just
         to clear the venue, longer to reach the front of the park.
       - **Disney Movie Magic + Wonderful World of Animation
         (Hollywood Studios):** projected on the Chinese Theatre at
         the end of Hollywood Blvd. Stand anywhere along Hollywood
         Blvd facing the theater. Less of a planning anchor than
         Fantasmic — these run back-to-back at the front of the park
         and are easy to walk up to 10-15 min before.
       - **Animal Kingdom:** no traditional nighttime spectacular
         currently. Tree of Life Awakenings is a 5-min projection
         that loops every ~10 min after dark — ambient, no planning
         needed.

       **Parades** — ~12-15 min for the parade itself, but the parade
       route is closed off ~30-45 min total (crowd assembly + parade
       + dispersal). Currently only Magic Kingdom has daytime parades:

       - **Disney Festival of Fantasy Parade (Magic Kingdom):** the
         marquee parade. ~12 min performance. Route: starts at
         Frontierland (the steps near Tiana's Bayou Adventure / Big
         Thunder), winds through Liberty Square, past Cinderella
         Castle, down Main Street USA, ends at Town Square. Common
         viewing spots and tradeoffs:
           - **Frontierland (start)** — lightest crowd, easy to leave
             toward Big Thunder / Splash / Pirates afterward. Good
             pick if the user's next ride is in Frontierland or
             Adventureland — they're already there. Arrive 15-20 min
             early.
           - **Liberty Square bridge / in front of Haunted Mansion** —
             same route side as Frontierland. Easy walk to Mansion or
             back to Frontierland after. ~15-20 min early.
           - **Hub in front of castle** — best castle backdrop and
             photo opportunity, most crowded. 30-45 min early.
           - **Main Street curb** — flat, easy viewing, lots of curb
             space. Arrive 20-30 min early for prime spots, 10 min
             for back-row standing.
           - **Town Square (end)** — parade ends here, easy exit to
             park entrance if leaving after. 10-15 min early.
         The route blocks crossing through Liberty Square / past the
         castle / down Main Street for the full ~30-45 min window.
         If the user wants a ride on the OPPOSITE side of the route
         from the parade (e.g., they're at Town Square and want to
         get to Frontierland), sequence that ride BEFORE the parade
         or wait until ~15 min after it ends. Use the ride lat/lon
         to find a viewing spot near the user's next ride: if their
         next ride is Big Thunder, suggest the Frontierland start;
         if it's Haunted Mansion, suggest the Liberty Square bridge.
       - **Disney Adventure Friends Cavalcades:** mini-parades that
         run multiple times throughout the day at MK (often AK too).
         ~5 min each, much shorter route than FoF. Less of a
         planning anchor — easy to catch incidentally if the user is
         on Main Street when one passes. Don't reorganize the day
         around these unless the user specifically asks.
       - **EPCOT, Hollywood Studios, Animal Kingdom:** no traditional
         daytime parade currently running. Don't suggest parade
         viewing for those parks even if `remaining_today` is empty
         (the absence of parade entries is the signal — don't
         hallucinate a parade).

       **Stage shows** — typically 25-35 min for the performance plus
       15-20 min queueing in (these are indoor-theater shows with set
       seatings). Treat as a ~45-min hold. Examples:
       - **Festival of the Lion King (Animal Kingdom):** in-the-round
         theater, ~30 min, very popular — arrive 20 min early.
       - **Indiana Jones Epic Stunt Spectacular (Hollywood Studios):**
         ~30 min outdoor stunt show, 5x daily. Easy walk-up; arrive
         10-15 min early.
       - **Festival of Fantasy / Mickey's Magical Friendship Faire
         on the Castle Forecourt Stage (Magic Kingdom):** ~20 min,
         outdoor castle stage. Sightlines from in front of the
         castle are fine; arrive at start time.
       - **Beauty and the Beast Live on Stage (Hollywood Studios):**
         ~25 min, outdoor amphitheater. Arrive 15 min early.

       The web app at `/parks/<park>/today` shows the full lineup
       (including atmosphere acts and character meets) — mention it
       as the place to browse what else is running if the user wants
       more detail than the headliner list here.

    6. **Weather + heat.** Outdoor rides (coasters like Big Thunder,
       TRON, Splash, Slinky Dog; water rides like Pirates, Splash,
       Kali) close for lightning (weather_code 95/96/99). Heavy rain
       (weather_code 80+ or precipitation_chance > 70%) means outdoor
       rides become miserable even when they stay open. Florida
       afternoon heat is its own factor — outdoor queues with no
       shade are uncomfortable above ~85°F and brutal above ~90°F.
       Use general Disney knowledge to classify each ride as
       indoor / outdoor / partially-covered. Then sequence:
       - Imminent thunderstorms in `weather.next_6h` → push outdoor
         rides to the clear hours, do indoor rides during the storm.
       - Hot now (>~88°F) but cooler later in the forecast → defer
         outdoor rides to the cooler window, do indoor rides now.
       - Currently cooler than later → do outdoor rides now while
         comfortable, save indoor for when heat peaks.
       - Always-comfortable day → ignore temperature, decide on
         cost-of-delay + proximity alone.

    6.5. **Water rides are the hot-day exception** — and have their
       own scheduling caveats. The two significant soak rides at WDW
       are Tiana's Bayou Adventure (Magic Kingdom, formerly Splash
       Mountain) and Kali River Rapids (Animal Kingdom). Both
       genuinely soak you — Kali is the more aggressive of the two
       (drenched, not damp).

       **Heat + sun = optimal conditions for these, not the time to
       avoid them.** Invert the normal "defer outdoor rides when
       hot" logic for water rides specifically: treat them as
       INDOOR-equivalent (or better) during the hot-afternoon
       window. The whole appeal is the cooldown.

       But two constraints that matter for sequencing:

       - **Don't schedule a water ride immediately before any
         extended indoor AC stop.** Table-service dining, indoor
         stage shows (Festival of the Lion King at AK, Beauty and
         the Beast Live at HS, Carousel of Progress at MK), or
         long indoor queues with strong AC will make a soaked
         guest genuinely miserable for 30+ minutes. Allow
         ~30-60 min in sun/warm air to dry before the next indoor
         stop, OR sequence another outdoor activity (a coaster, a
         walking break, an outdoor quick-service stop) in between
         to bridge the dry-out window.
       - **Don't schedule water rides when it's cool.** Below
         ~70°F, getting soaked is unpleasant; below ~60°F (rare in
         FL but possible Dec-Feb mornings), actively avoid. Check
         `weather.current.temp_f` before recommending.

       When a user has a water ride on their list:
         1. Find a hot-afternoon slot (warmer than 80°F ideally)
         2. Verify what's scheduled immediately after — if it's
            indoor dining or an indoor show, push the water ride
            earlier or insert a 30-60 min outdoor buffer
         3. If the day is cool throughout, mention it to the user
            and ask if they still want to ride — don't silently
            include a soak ride on a 65°F day

    7. **After the user accepts a plan, persist it for the feedback
       loop.** Once the user signals acceptance ("let's do that",
       "starting with Pirates", "sounds good"), call record_plan with
       a compact snapshot:
         - park
         - ride_sequence: ordered list of {ride_name,
           predicted_wait_min, position} from the plan you just laid
           out (use today_vs_forecast-adjusted predictions, not raw
           forecast values)
         - show_selections: any shows being fitted in (with
           performance_start + your predicted_arrival_min)
         - context: small dict like {park_load_ratio:
           today_vs_forecast.park_load_ratio, weather_summary:
           "<temp>F, <conditions>"}
         - notes: any user-stated constraints ("dining at 6pm",
           "skipping water rides")
       record_plan returns a plan_id; mention it briefly to the user
       so they can reference it later if needed ("logged as plan
       2026-05-10T18:00; you can give feedback next time we plan").

       DO NOT call record_plan for plans the user didn't accept
       (alternates they asked about and rejected, hypothetical "what
       if" planning, or pure information queries with no commitment).
       The point is to capture plans the user actually intends to
       follow.

       **Future days + multi-day trips.** Everything above is the
       same-day path — record_plan defaults to today and auto-activates,
       so the poller watches it immediately. To build a day the user
       ISN'T at yet, pass planned_for_date to record_plan, or use
       create_trip to mint a whole trip (one dormant day per date) in a
       single call. Dormant rows fire no alerts and survive until just
       past their trip day. On the trip day, after you re-evaluate
       against fresh live conditions (section 0d) and the user accepts,
       call activate_plan to turn on monitoring. Don't pre-activate
       future days, and don't record this session's live waits as if
       they were a future date's.

       Then LATER in the same conversation, if the user signals
       end-of-trip ("we're heading out", "thanks, that worked",
       "we're done") OR reports outcomes incrementally throughout
       the day, call record_plan_outcome with the same plan_id and
       whatever feedback you've gathered. If they don't, the next
       planning session will pick it up via the section 0a check.

    Args:
        park: Park key or human name. Accepts 'magic_kingdom',
            'Magic Kingdom', 'MK', etc.
        ride_names: List of ride names (substring match, case
            insensitive). Each name resolved against the historical
            snapshot. Unresolved names appear in `unresolved` instead
            of being silently dropped.

    Returns:
        Dict with park, current_time_et, park_hours (open/close/
        minutes_until_close), weather (current + 6h forecast),
        showtimes (headliner-only, remaining-today performances; None
        if the showtimes API fetch failed), rides (list with full
        per-ride context), and unresolved (list of ride_names that
        couldn't be matched). On AWS auth failure for the live-data
        portion, falls back to per-ride error blocks rather than
        dropping the whole call.
    """
    park_key = _normalize_park(park)
    now_et = datetime.now(_EASTERN)

    # Resolve every ride name first (offline lookup against the
    # snapshot). Unmatched names go into `unresolved` so the model
    # can flag them to the user instead of silently planning around
    # the rides it could resolve.
    resolved: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for name in ride_names:
        try:
            r = _find_ride(name)
        except ValueError:
            unresolved.append(name)
            continue
        resolved.append(r)

    table = None
    table_error: dict[str, Any] | None = None
    try:
        table = _ddb_table()
    except Exception as e:
        err = _aws_error_payload(e)
        table_error = err if err is not None else {
            "error": "DDB connection failed",
            "error_message": str(e),
        }

    locations = _locations()
    rides_out: list[dict[str, Any]] = []

    for ride in resolved:
        rid = ride["ride_id"]
        out: dict[str, Any] = {
            "ride_name": ride["ride_name"],
            "ride_id": rid,
            "park_key": ride.get("park_key"),
        }
        # Static location lookup — cheap, always fill it in if we have it
        loc = locations.get(rid)
        if loc:
            out["location"] = {"lat": loc["lat"], "lon": loc["lon"]}

        # LL drop pattern (from the historical snapshot) — used by the
        # planner to suggest when to check the app for a better slot.
        # Surface a compact summary; full histograms available via
        # get_ride_ll_drops if Claude wants the breakdown.
        ll_drops_total = ride.get("ll_drops_total")
        if ll_drops_total:
            drop_hours = ride.get("ll_drop_hours") or []
            top_hours = sorted(drop_hours, key=lambda x: -x["count"])[:3]
            out["ll_drop_pattern"] = {
                "drops_per_active_day": ride.get("ll_drops_per_active_day"),
                "typical_shift_minutes": ride.get("ll_typical_shift_mins"),
                "top_drop_hours_et": [h["hour"] for h in top_hours],
                "sample_size_days": ride.get("ll_active_days"),
            }

        if table_error is not None or table is None:
            out.update(table_error or {"error": "DDB unavailable"})
            rides_out.append(out)
            continue

        # 1. STATE row (current status, wait, LL, last_seen, last_forecast_at)
        try:
            state_resp = table.get_item(
                Key={"PK": f"RIDE#{rid}", "SK": "STATE"}
            )
            state = _convert_decimals(state_resp.get("Item")) if state_resp.get("Item") else None
        except Exception as e:
            err = _aws_error_payload(e)
            out.update(err or {"error": "STATE read failed", "error_message": str(e)})
            rides_out.append(out)
            continue

        if not state:
            out["live_state_available"] = False
            rides_out.append(out)
            continue

        out["status"] = state.get("status")
        out["wait_mins"] = state.get("wait_mins")
        out["current_ll_offer"] = state.get("ll")
        out["last_seen"] = state.get("last_seen")
        out["last_forecast_at"] = state.get("last_forecast_at")

        # 2. DOWN enrichment (only when status=DOWN)
        if state.get("status") == "DOWN":
            try:
                ds_resp = table.get_item(
                    Key={"PK": f"RIDE#{rid}", "SK": "DOWN_SINCE"}
                )
                ds_item = ds_resp.get("Item")
                if ds_item and ds_item.get("down_since"):
                    out["down_since"] = ds_item["down_since"]
                    try:
                        down_dt = datetime.fromisoformat(ds_item["down_since"])
                        elapsed = datetime.now(timezone.utc) - down_dt
                        out["down_duration_mins"] = round(
                            elapsed.total_seconds() / 60, 1
                        )
                    except ValueError:
                        pass
            except Exception:
                pass

            clusters = ride.get("down_clusters", [])
            durations = sorted(
                c["duration_minutes"] for c in clusters
                if isinstance(c.get("duration_minutes"), (int, float))
            )
            if durations:
                mid = len(durations) // 2
                if len(durations) % 2 == 1:
                    median = durations[mid]
                else:
                    median = (durations[mid - 1] + durations[mid]) / 2
                out["typical_down_cluster_mins"] = round(median, 1)
                out["historical_cluster_count"] = len(durations)
                cur = out.get("down_duration_mins")
                if cur is not None and median > 0:
                    out["cluster_progress_pct"] = round(100.0 * cur / median, 1)

        # 3. Latest forecast snapshot
        try:
            f_resp = table.query(
                KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
                ExpressionAttributeValues={
                    ":pk": f"RIDE#{rid}",
                    ":sk": "FORECAST#",
                },
                ScanIndexForward=False,
                Limit=1,
            )
            f_items = f_resp.get("Items", [])
            if f_items:
                f_item = _convert_decimals(f_items[0])
                forecast = f_item.get("forecast", []) or []
                out["forecast_polled_at"] = f_item.get("polled_at")
                out["forecast"] = forecast
                # Peak-in-next-3h is the cost-of-delay signal that
                # matters for planning. The old full-horizon slope
                # was misleading for hump-shaped curves like Pirates
                # of the Caribbean (afternoon peak → drops by close).
                peak = _forecast_peak_in_window(forecast, hours_ahead=3)
                if peak is not None:
                    out["forecast_peak_next_3h_mins"] = peak["peak_wait_mins"]
                    out["forecast_minutes_until_peak"] = peak["minutes_until_peak"]
                    out["forecast_peak_at"] = peak["peak_at"]
            else:
                # Common when ride is currently DOWN — themeparks.wiki
                # stops forecasting DOWN rides. Note that explicitly
                # rather than leaving the field absent ambiguously.
                out["forecast"] = None
                out["forecast_unavailable_reason"] = (
                    "No forecast row in DDB. Usually means the upstream "
                    "API isn't predicting this ride right now (most often "
                    "because it's DOWN, walk-up, or no-queue)."
                )
        except Exception:
            out["forecast"] = None

        rides_out.append(out)

    # Headliner-only showtimes with remaining-today performances. We
    # filter aggressively here (vs. exposing the full park lineup via
    # get_park_showtimes) because get_planning_context is already
    # token-heavy and atmosphere acts / character meets aren't
    # planner-relevant. The model can fall back to get_park_showtimes
    # if it needs the rest.
    now_iso = now_et.isoformat()
    all_shows = _fetch_park_showtimes(park_key)
    headliner_showtimes: list[dict[str, Any]] | None
    if all_shows is None:
        headliner_showtimes = None
    else:
        headliner_showtimes = []
        for show in all_shows:
            if show["category"] not in _SHOW_HEADLINER_CATEGORIES:
                continue
            remaining = [t for t in show["showtimes"] if t["start"] > now_iso]
            if not remaining:
                continue
            headliner_showtimes.append({
                "name": show["name"],
                "category": show["category"],
                "remaining_today": remaining,
            })

    return {
        "park": park_key,
        "current_time_et": now_et.isoformat(),
        "park_hours": _fetch_park_hours_today(park_key),
        "weather": _fetch_weather_forecast(),
        "today_vs_forecast": _compute_load_vs_forecast(rides_out),
        "currently_down_in_park": _fetch_park_currently_down(table, park_key),
        "showtimes": headliner_showtimes,
        "rides": rides_out,
        "unresolved": unresolved,
    }


# Eager-write rows that never get a confirming outcome should expire
# fast — they represent plans the user browsed past or asked about
# hypothetically. 24h is enough that a same-day "we're done" cue can
# still find the row.
_PLAN_PENDING_TTL_SECS = 24 * 60 * 60


# Outcome-confirmed rows live longer so the calibration history is
# meaningful. 365 days lets per-user trends accumulate across a year
# of trips.
_PLAN_RECORDED_TTL_SECS = 365 * 24 * 60 * 60


# Staleness threshold for prompting. Plans older than this are too
# stale for the user to recall details; Claude should skip asking
# about them rather than nag.
_PLAN_STALENESS_DAYS = 14


# Multi-day trip planner (M5). A not-yet-recorded plan expires this many
# days past its trip day — so a FUTURE plan survives until just after the
# day it's for, while a same-day plan still auto-cleans if no outcome is
# ever recorded. Replaces the old fixed "now + 24h" pending TTL, which
# would have deleted a future trip plan the day after it was built.
_PLAN_PENDING_BUFFER_DAYS = 2


# Trip header rows (TRIP#<trip_id>) outlive their last day by this much.
_TRIP_BUFFER_DAYS = 3


def _epoch_now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _coerce_plan_id_to_sk(plan_id: str) -> str:
    """plan_id is the ISO timestamp suffix; SK is `PLAN#<ts>`.

    Accept either form ('PLAN#2026-05-10T18:00:00+00:00' or
    '2026-05-10T18:00:00+00:00') so Claude can pass back whatever it
    received without having to remember the prefix convention.
    """
    if plan_id.startswith("PLAN#"):
        return plan_id
    return f"PLAN#{plan_id}"


def _today_et_date_iso() -> str:
    """Calendar date in Eastern time, ISO (YYYY-MM-DD).

    Matches the `planned_for_date` convention record_plan has always
    used — the human "what day is the trip," a plain ET calendar date,
    NOT the 4am park-day shift the heatmap/downtime tools apply.
    """
    return datetime.now(_EASTERN).date().isoformat()


def _plan_pending_ttl(planned_for_date: str) -> int:
    """TTL epoch for a not-yet-recorded plan: a couple days past its trip
    day. Future plans survive until just after the day; same-day plans
    still auto-clean if never outcomed. Falls back to the old 24h window
    if the date can't be parsed.
    """
    try:
        d = datetime.fromisoformat(planned_for_date).date()
    except ValueError:
        return _epoch_now() + _PLAN_PENDING_TTL_SECS
    # Expire at the park-day boundary (4am ET) `buffer + 1` days later.
    expiry_et = datetime.combine(
        d + timedelta(days=_PLAN_PENDING_BUFFER_DAYS + 1),
        time(_PARK_DAY_BOUNDARY_HOUR),
        tzinfo=_EASTERN,
    )
    return int(expiry_et.astimezone(timezone.utc).timestamp())


def _build_plan_item(
    *,
    user_id: str,
    park_key: str,
    ride_sequence: list[dict[str, Any]],
    planned_for_date: str,
    plan_ts: str,
    show_selections: list[dict[str, Any]] | None = None,
    context: dict[str, Any] | None = None,
    notes: str | None = None,
    trip_id: str | None = None,
    plan_window: dict[str, Any] | None = None,
    active: bool = False,
    activated_at: str | None = None,
    created_by: str | None = None,
    alert_subscribers: set[str] | None = None,
) -> dict[str, Any]:
    """Assemble a PLAN# row. Shared by record_plan (single day, often
    same-day + active) and create_trip (one dormant row per trip day).

    Multi-day fields (M5):
      - `planned_for_date`: the day the plan is FOR (settable; future-capable).
      - `trip_id`: groups day-plans into one trip (None for a standalone plan).
      - `active`: gates poller disruption alerts. A dormant future plan
        MUST stay active=false until activated on its day, or it would
        fire alerts for rides weeks ahead.
      - `plan_window`: optional {open, close} ET times; the poller only
        alerts inside this window once set (resolved at activation).
      - `created_by`: attribution label (friendly user id). Defaults to
        user_id. In the shared-trip model multiple people write to one
        partition, so we stamp who recorded each row.
      - `alert_subscribers` (2026-07-03): DDB String Set of ADDITIONAL
        alert recipients (ids with a USER#<id>/PROFILE row — Cognito subs
        for family members). The partition owner is IMPLICIT and always
        alerted; absent attribute = owner-only, exactly the pre-feature
        behavior (no migration). Omitted when empty (DDB rejects empty
        sets). Mutated only via atomic ADD/DELETE (see
        set_plan_alert_subscription) so web + MCP edits can't race.
    """
    item = {
        "PK": f"USER#{user_id}",
        "SK": f"PLAN#{plan_ts}",
        "park_key": park_key,
        "planned_at": plan_ts,
        "planned_for_date": planned_for_date,
        "trip_id": trip_id,
        "ride_sequence": ride_sequence,
        # As rides get done / abandoned during execution they move OUT of
        # ride_sequence and INTO one of these two arrays (poller only
        # watches ride_sequence).
        "completed_rides": [],
        "dropped_rides": [],
        "show_selections": show_selections or [],
        "context": context or {},
        "notes": notes,
        "plan_window": plan_window,
        "active": active,
        "activated_at": activated_at,
        "created_by": created_by or user_id,
        "outcome_recorded": False,
        "ttl": _plan_pending_ttl(planned_for_date),
    }
    if alert_subscribers:
        item["alert_subscribers"] = set(alert_subscribers)
    return item


def _resolve_alert_member(
    table, member: str, friendly_to_sub: dict[str, str] | None = None
) -> tuple[str | None, bool]:
    """Resolve a member label to the profile id the poller alerts on.

    Tries `member` as-given (a Cognito sub, or a legacy friendly id with
    its own profile row), then via a friendly-name→sub map (available on
    the HTTP transport from MCP_SUB_USER_MAP). The poller looks up
    Pushover keys at USER#<id>/PROFILE, so an id only "works" if that row
    exists — family members create it by signing into the dashboard and
    saving /me once.

    Returns (resolved_id, has_pushover_key); (None, False) when no
    profile row exists under any candidate id.
    """
    candidates = [member]
    if friendly_to_sub:
        mapped = friendly_to_sub.get(member.strip().lower())
        if mapped:
            candidates.append(mapped)
    for cand in candidates:
        row = table.get_item(
            Key={"PK": f"USER#{cand}", "SK": "PROFILE"}
        ).get("Item")
        if row:
            return cand, bool(row.get("pushover_user_key"))
    return None, False


def _apply_alert_subscription(
    table, user_id: str, member_id: str, subscribed: bool,
    plan_rows: list[dict],
) -> list[str]:
    """Atomically ADD/DELETE `member_id` in each plan row's
    alert_subscribers String Set.

    Set-level ADD/DELETE (not read-modify-write) so a concurrent MCP plan
    edit or web toggle can't lose the change — and it never touches the
    attributes the plan-edit tools rewrite. DELETE removing the last
    member removes the attribute entirely, which reads back as
    owner-only (the default). Returns the affected planned_for_dates.
    """
    op = "ADD" if subscribed else "DELETE"
    updated: list[str] = []
    for r in plan_rows:
        table.update_item(
            Key={"PK": f"USER#{user_id}", "SK": r["SK"]},
            UpdateExpression=f"{op} alert_subscribers :m",
            ExpressionAttributeValues={":m": {member_id}},
            ConditionExpression="attribute_exists(PK)",
        )
        updated.append(r.get("planned_for_date") or r["SK"])
    return updated


def _bias_confidence(n: int) -> str:
    if n >= _BIAS_CONFIDENCE_HIGH:
        return "high"
    if n >= _BIAS_CONFIDENCE_MEDIUM:
        return "medium"
    return "low"


def _compute_calibration_summary(
    recorded_plans: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Aggregate recorded plans into per-user calibration signals.

    Server-side derivation so the LLM gets pre-computed numbers +
    confidence labels rather than having to eyeball raw plan rows.
    Same design pattern as `_compute_load_vs_forecast` for the live
    today_vs_forecast signal — pre-computation in the data plane,
    interpretation strings in the response so Claude has ready
    phrasing.

    Returns None if no plans have outcomes recorded.

    Output shape:
        {
          n_recorded_plans: int,
          aggression: {avg_score, interpretation, n_samples} | null,
          timing: {distribution, avg_extra_time_minutes,
                   interpretation, n_samples} | null,
          per_ride_prediction_bias: [
            {ride_name, n_samples, avg_delta_min, confidence,
             interpretation},
            ...
          ],
          per_show_arrival_bias: [
            {show_name, n_samples, avg_delta_min, confidence,
             interpretation},
            ...
          ],
        }
    """
    plans = [p for p in recorded_plans if p.get("outcome_recorded")]
    if not plans:
        return None

    # ── Aggression aggregate ──
    agg_scores = [
        _AGGRESSION_SCORES[p["aggression_rating"]]
        for p in plans
        if p.get("aggression_rating") in _AGGRESSION_SCORES
    ]
    if agg_scores:
        avg_agg = sum(agg_scores) / len(agg_scores)
        if avg_agg < -0.3:
            agg_interp = (
                "User's recent plans tend to run too aggressive — they "
                "didn't fit. Be more conservative today: longer buffers, "
                "fewer rides, or both."
            )
        elif avg_agg > 0.3:
            agg_interp = (
                "User's recent plans tend to finish with time to spare. "
                "Pack more in today — shorter buffers, add a ride, or "
                "fit a show that wouldn't normally make the cut."
            )
        else:
            agg_interp = (
                "User's recent plans have been balanced — predicted "
                "aggression has been about right. Use baseline buffers."
            )
        aggression = {
            "avg_score": round(avg_agg, 2),
            "interpretation": agg_interp,
            "n_samples": len(agg_scores),
        }
    else:
        aggression = None

    # ── Timing aggregate ──
    timing_buckets = {k: 0 for k in ("ran_over", "on_time", "extra_time")}
    extra_times: list[float] = []
    for p in plans:
        t = p.get("timing_rating")
        if t in timing_buckets:
            timing_buckets[t] += 1
        if t == "extra_time" and p.get("extra_time_minutes") is not None:
            extra_times.append(float(p["extra_time_minutes"]))
    n_timing = sum(timing_buckets.values())
    if n_timing > 0:
        avg_extra = (
            round(sum(extra_times) / len(extra_times), 1)
            if extra_times else None
        )
        # Interpretation pulls the dominant bucket, with an extra-time
        # magnitude callout when meaningful.
        share_extra = timing_buckets["extra_time"] / n_timing
        share_over = timing_buckets["ran_over"] / n_timing
        if share_extra >= 0.6:
            extra_blurb = (
                f" (averaging ~{avg_extra:.0f} min spare on those days)"
                if avg_extra else ""
            )
            timing_interp = (
                f"User finishes with extra time on {timing_buckets['extra_time']}/"
                f"{n_timing} recent plans{extra_blurb} — pack today's plan "
                f"more aggressively."
            )
        elif share_over >= 0.5:
            timing_interp = (
                f"User runs over time on {timing_buckets['ran_over']}/"
                f"{n_timing} recent plans — be more conservative today, "
                f"cut a ride or extend buffers."
            )
        elif timing_buckets["on_time"] / n_timing >= 0.5:
            timing_interp = (
                f"User finishes on time on {timing_buckets['on_time']}/"
                f"{n_timing} recent plans — current calibration is working."
            )
        else:
            timing_interp = (
                f"Mixed timing pattern across {n_timing} recent plans "
                f"(ran_over={timing_buckets['ran_over']}, "
                f"on_time={timing_buckets['on_time']}, "
                f"extra_time={timing_buckets['extra_time']}) — no clear "
                f"adjustment. Use baselines."
            )
        timing = {
            "distribution": timing_buckets,
            "avg_extra_time_minutes": avg_extra,
            "interpretation": timing_interp,
            "n_samples": n_timing,
        }
    else:
        timing = None

    # ── Per-ride prediction bias ──
    # Two sources of "ride was done with actual wait Y":
    #  1. completed_rides entries (mid-trip mark_ride_complete) carry
    #     predicted_wait_min on the same entry alongside actual_wait_min.
    #     This is the primary, most accurate source — captured within
    #     minutes of the actual ride.
    #  2. per_item_feedback (end-of-day record_plan_outcome) keyed by
    #     ride_name with actual_wait_min in the value dict. Predictions
    #     for those rides live in ride_sequence (if they weren't moved
    #     to completed_rides) or in completed_rides (if they were).
    #     Recall-based, less accurate, but still useful when the user
    #     reports a day's outcomes after the fact.
    # We union both — same ride showing up in both paths contributes
    # one delta each, but that's intentional: independent observations
    # of the same ride still strengthen the signal.
    ride_deltas: dict[str, list[float]] = {}
    show_deltas: dict[str, list[float]] = {}
    for p in plans:
        # Path 1: completed_rides (self-contained — predicted + actual
        # on the same entry).
        for r in (p.get("completed_rides") or []):
            predicted = r.get("predicted_wait_min")
            actual = r.get("actual_wait_min")
            ride_name = r.get("ride_name")
            if predicted is None or actual is None or not ride_name:
                continue
            try:
                delta = float(actual) - float(predicted)
            except (TypeError, ValueError):
                continue
            ride_deltas.setdefault(ride_name, []).append(delta)

        # Path 2: per_item_feedback against ride_sequence + completed_rides
        # predictions (legacy path; covers end-of-day recall).
        feedback = p.get("per_item_feedback") or {}
        if not isinstance(feedback, dict):
            continue
        ride_predictions: dict[str, Any] = {}
        for r in (p.get("ride_sequence") or []) + (p.get("completed_rides") or []):
            name = r.get("ride_name")
            pred = r.get("predicted_wait_min")
            if name and pred is not None:
                ride_predictions[name] = pred
        for ride_name, predicted in ride_predictions.items():
            fb = feedback.get(ride_name)
            if not isinstance(fb, dict):
                continue
            actual = fb.get("actual_wait_min")
            if actual is None:
                continue
            try:
                delta = float(actual) - float(predicted)
            except (TypeError, ValueError):
                continue
            ride_deltas.setdefault(ride_name, []).append(delta)

        # Show arrival bias — same shape, different fields. Only sourced
        # from per_item_feedback for now (no mid-trip mark_show_attended
        # tool yet).
        show_predictions = {
            s["show_name"]: s.get("predicted_arrival_min")
            for s in (p.get("show_selections") or [])
            if s.get("show_name") and s.get("predicted_arrival_min") is not None
        }
        for show_name, predicted in show_predictions.items():
            fb = feedback.get(show_name)
            if not isinstance(fb, dict):
                continue
            actual = fb.get("arrived_with_min")
            if actual is None:
                continue
            try:
                delta = float(actual) - float(predicted)
            except (TypeError, ValueError):
                continue
            show_deltas.setdefault(show_name, []).append(delta)

    def _bias_entries(deltas: dict[str, list[float]], item_label: str, kind: str) -> list[dict[str, Any]]:
        """kind = 'ride_wait' or 'show_arrival' — picks interpretation phrasing."""
        out = []
        for name, ds in deltas.items():
            n = len(ds)
            avg = round(sum(ds) / n, 1)
            confidence = _bias_confidence(n)
            if abs(avg) <= _BIAS_NEUTRAL_MINUTES:
                interp = f"Predictions for {name} have been roughly accurate ({avg:+.0f} min avg)."
            elif kind == "ride_wait":
                if avg > 0:
                    interp = (
                        f"{name} tends to wait LONGER than predicted "
                        f"(+{avg:.0f} min avg, {confidence} confidence "
                        f"on n={n}). Scale this ride's prediction up by "
                        f"~{avg:.0f} min for this user."
                    )
                else:
                    interp = (
                        f"{name} tends to wait SHORTER than predicted "
                        f"({avg:.0f} min avg, {confidence} confidence "
                        f"on n={n}). Scale this ride's prediction down "
                        f"by ~{abs(avg):.0f} min for this user."
                    )
            else:  # show_arrival
                if avg > 0:
                    interp = (
                        f"User arrives at {name} LATER than recommended "
                        f"(+{avg:.0f} min avg). Either they cut it close "
                        f"(no problem if they got a fine spot) or your "
                        f"recommendation was more conservative than needed."
                    )
                else:
                    interp = (
                        f"User arrives at {name} EARLIER than recommended "
                        f"({avg:.0f} min avg) — your arrival recommendation "
                        f"may be padding too much for this user."
                    )
            out.append({
                f"{item_label}_name": name,
                "n_samples": n,
                "avg_delta_min": avg,
                "confidence": confidence,
                "interpretation": interp,
            })
        # Sort by sample size desc — most reliable signals first
        out.sort(key=lambda x: -x["n_samples"])
        return out

    return {
        "n_recorded_plans": len(plans),
        "aggression": aggression,
        "timing": timing,
        "per_ride_prediction_bias": _bias_entries(ride_deltas, "ride", "ride_wait"),
        "per_show_arrival_bias": _bias_entries(show_deltas, "show", "show_arrival"),
        "usage_hint": (
            "Apply aggression + timing interpretations as ONE upfront "
            "calibration note in your response (same convention as "
            "today_vs_forecast in section 1.5). Apply per-ride / "
            "per-show bias entries selectively: high-confidence biases "
            "(n>=5) can be quoted directly to the user; medium (n=3-4) "
            "should be applied silently to predictions; low (n<3) "
            "should be ignored — the signal is too noisy to be useful."
        ),
    }


def parse_ll_time(time_str: str, date_iso: str) -> str | None:
    """Turn a held-LL time into a full ET ISO timestamp comparable to the
    poller's return_start.

    Accepts a full ISO ("2026-07-03T15:00:00-04:00"), a 24h "HH:MM", or a
    12h "3:00 PM" / "3pm" — combined with `date_iso` (the plan's day) in
    America/New_York. Returns None if unparseable.
    """
    s = (time_str or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            return dt.isoformat()
    except ValueError:
        pass
    m = _re.match(r"^(\d{1,2}):(\d{2})\s*([AaPp][Mm])?$", s)
    m2 = _re.match(r"^(\d{1,2})\s*([AaPp][Mm])$", s)
    if m:
        hh, mm, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    elif m2:
        hh, mm, ap = int(m2.group(1)), 0, m2.group(2).lower()
    else:
        return None
    if ap == "pm" and hh != 12:
        hh += 12
    elif ap == "am" and hh == 12:
        hh = 0
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    try:
        day = datetime.fromisoformat(date_iso).date()
    except ValueError:
        return None
    return datetime.combine(day, time(hh, mm), tzinfo=_EASTERN).isoformat()


def resolve_ll_holds(
    ll_holds: dict[str, str] | None,
    ride_sequence: list[dict[str, Any]],
    date_iso: str,
) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    """Resolve a record_plan-style ll_holds map ({ride name or id: return
    time in any parse_ll_time format}) against the plan's own
    ride_sequence into the stored shape ({ride_id: full ET ISO}).

    Returns (resolved, None) on success or (None, error_payload) on the
    first bad entry — FAIL LOUD, never drop silently: a hold that
    vanishes without an error is exactly the 2026-07-04 bug (LLs written
    to free-text notes, invisible to the trip page and alert engine).
    """
    if not ll_holds:
        return None, None
    by_id = {r.get("ride_id"): r for r in ride_sequence if r.get("ride_id")}
    resolved: dict[str, str] = {}
    for key, raw_time in ll_holds.items():
        q = (key or "").strip().lower()
        match = by_id.get(key) or next(
            (
                r for r in ride_sequence
                if (r.get("ride_name") or "").lower() == q
                or (q and q in (r.get("ride_name") or "").lower())
            ),
            None,
        )
        if not match or not match.get("ride_id"):
            return None, {
                "error": "Held-LL ride not in plan",
                "error_message": (
                    f"ll_holds entry '{key}' doesn't match any ride in "
                    "ride_sequence (by ride_id or name). Fix the plan or "
                    "the hold — nothing was saved."
                ),
            }
        iso = parse_ll_time(raw_time, date_iso)
        if iso is None:
            return None, {
                "error": "Invalid held-LL return time",
                "error_message": (
                    f"Could not parse ll_holds['{key}'] = {raw_time!r}. "
                    "Use '3:00 PM', '15:00', or a full ISO timestamp — "
                    "nothing was saved."
                ),
            }
        resolved[match["ride_id"]] = iso
    return resolved, None


def normalize_ride_targets(
    ride_sequence: list[dict[str, Any]],
    date_iso: str,
) -> dict[str, Any] | None:
    """Normalize optional per-ride `target_time` fields IN PLACE to full
    ET ISO timestamps (any parse_ll_time form accepted). Also validates
    the optional `ll_planned` flag is boolean-ish. Returns an error
    payload on the first bad value, else None. FAIL LOUD — a target time
    that silently vanishes is the same bug class as dropped ll_holds.
    """
    for r in ride_sequence:
        raw = r.get("target_time")
        if raw:
            iso = parse_ll_time(str(raw), date_iso)
            if iso is None:
                return {
                    "error": "Invalid ride target_time",
                    "error_message": (
                        f"Could not parse target_time {raw!r} on "
                        f"'{r.get('ride_name') or r.get('ride_id')}'. Use "
                        "'10:00 AM', '14:30', or full ISO — nothing was saved."
                    ),
                }
            r["target_time"] = iso
        if "ll_planned" in r and not isinstance(r["ll_planned"], bool):
            r["ll_planned"] = bool(r["ll_planned"])
    return None


def resolve_reservations(
    reservations: list[dict[str, Any]] | None,
    date_iso: str,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    """Normalize a record_plan `reservations` list ({name, time, ...})
    into stored shape with full ET ISO times. Returns (resolved, None)
    or (None, error) on the first bad entry — FAIL LOUD, same rationale
    as resolve_ll_holds.
    """
    if not reservations:
        return None, None
    out: list[dict[str, Any]] = []
    for res in reservations:
        name = (res.get("name") or "").strip()
        if not name:
            return None, {
                "error": "Invalid reservation",
                "error_message": "Each reservation needs a name — nothing was saved.",
            }
        iso = parse_ll_time(str(res.get("time") or ""), date_iso)
        if iso is None:
            return None, {
                "error": "Invalid reservation time",
                "error_message": (
                    f"Could not parse time {res.get('time')!r} for "
                    f"'{name}'. Use '12:30 PM', '18:00', or full ISO — "
                    "nothing was saved."
                ),
            }
        entry = {"name": name, "time": iso}
        for extra in ("type", "notes", "confirmation", "party_size"):
            if res.get(extra) is not None:
                entry[extra] = res[extra]
        out.append(entry)
    out.sort(key=lambda e: e["time"])
    return out, None


def apply_held_ll(table, user_id, ride_id, return_iso, plan_rows):
    """Set (return_iso) or clear (None) a held Lightning Lane for `ride_id`
    on each matching plan row's ll_holds map. Atomic per-key map update —
    ll_holds is ensured first so `SET ll_holds.#r` can't fail on a missing
    map; no ride_sequence surgery, so it can't race a plan edit. Returns
    the affected planned_for_dates.
    """
    updated = []
    for r in plan_rows:
        key = {"PK": f"USER#{user_id}", "SK": r["SK"]}
        if return_iso:
            table.update_item(
                Key=key,
                UpdateExpression="SET ll_holds = if_not_exists(ll_holds, :empty)",
                ExpressionAttributeValues={":empty": {}},
                ConditionExpression="attribute_exists(PK)",
            )
            table.update_item(
                Key=key,
                UpdateExpression="SET ll_holds.#r = :t",
                ExpressionAttributeNames={"#r": ride_id},
                ExpressionAttributeValues={":t": return_iso},
            )
        else:
            table.update_item(
                Key=key,
                UpdateExpression="REMOVE ll_holds.#r",
                ExpressionAttributeNames={"#r": ride_id},
            )
        updated.append(r.get("planned_for_date") or r["SK"])
    return updated


def split_dropped_rides(
    plan: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a plan's ride_sequence into (still-planned, dropped-via-replan)
    using the plan's dropped_ride_ids.

    dropped_ride_ids is the atomic set the /replan approve flow writes when
    the family drops a disrupted ride from the phone (no Claude app). The
    poller already excludes those rides from its watch set; this keeps the
    MCP planner's view in sync so Claude re-plans around the SAME effective
    sequence — not a stale one that still lists a ride the family dropped.
    """
    dropped_ids = set(plan.get("dropped_ride_ids") or [])
    # Rides marked done from /replan also leave the remaining sequence
    # (the plan's completed_rides list is the richer, calibration path).
    done_ids = set(plan.get("completed_ride_ids") or [])
    seq = plan.get("ride_sequence") or []
    still = [
        r for r in seq
        if r.get("ride_id") not in dropped_ids and r.get("ride_id") not in done_ids
    ]
    dropped = [r for r in seq if r.get("ride_id") in dropped_ids]
    # Honor a Claude-applied re-order (plan_order, set by the /replan
    # "Ask Claude" apply): listed rides first, in that order; unlisted
    # rides keep their original position after. Keeps the planner's view
    # consistent with what the family sees on the page.
    order = plan.get("plan_order") or []
    if order:
        rank = {rid: i for i, rid in enumerate(order)}
        still.sort(key=lambda r: rank.get(r.get("ride_id"), len(order) + 1))
    return still, dropped


def _pop_ride_from_sequence(
    ride_sequence: list[dict[str, Any]],
    ride_id: str,
    ride_name: str,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Helper for mark_ride_complete / remove_ride_from_plan.

    Find a ride in ride_sequence (ride_id match first, ride_name
    case-insensitive fallback), return (new_sequence_without_match,
    popped_entry_or_None). If nothing matches, returns the original
    sequence unchanged and None.
    """
    name_lc = (ride_name or "").lower()
    popped: dict[str, Any] | None = None
    new_seq: list[dict[str, Any]] = []
    for r in ride_sequence:
        if popped is None and (
            r.get("ride_id") == ride_id
            or (r.get("ride_name") or "").lower() == name_lc
        ):
            popped = r
            continue
        new_seq.append(r)
    return new_seq, popped
