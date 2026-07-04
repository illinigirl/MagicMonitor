"""
Anthropic tool definitions mirroring a subset of MCP server.py tools.

These are NOT the canonical MCP tool schemas — those live in server.py
via @mcp.tool() decorators. These are a parallel set of Anthropic-API
tool definitions used by the eval runner to expose tools to Claude
via the Messages API's tool-use feature.

Why a parallel set instead of importing from the MCP server:
  - The MCP SDK's tool registry isn't designed to be re-exported as
    Anthropic Messages API schemas. The shapes are similar but not
    identical (MCP uses JSON-RPC; Anthropic uses inline JSON schema).
  - Keeping eval tool surface explicit makes it obvious which tools
    Claude has access to in a given eval — important for reproducibility
    of behavioral assertions.
  - Adding new tools to the eval surface is a deliberate act, not an
    accidental side effect of adding @mcp.tool() somewhere.

When server.py changes a tool signature, this file may need updating.
That's intentional friction: a tool-shape change deserves an explicit
eval-surface review.

v1 surface: the planning-flow tools (get_user_plan_history,
get_planning_context, find_rides_matching, record_plan). Extended for
the M5 multi-day trip planner with create_trip, get_plan_for_day,
get_upcoming_trip, activate_plan, and the future-dated record_plan
params (planned_for_date / trip_id / plan_window / active). Add more as
eval cases need them.
"""

from __future__ import annotations

from typing import Any

# Each tool is the Anthropic Messages-API tool dict shape:
#   {"name": str, "description": str, "input_schema": JSONSchema}
TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_user_plan_history",
        "description": (
            "Recent plans for a user, with outcomes if recorded, plus a "
            "pre-computed calibration_summary derived from the recorded "
            "ones. Call this at the start of every planning session to "
            "check for unrecorded prior plans and to calibrate today's "
            "plan against the user's past predictions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "Defaults to 'megan'. Single-user setup.",
                },
                "limit": {"type": "integer", "default": 10},
                "include_unrecorded_only": {"type": "boolean", "default": False},
                "include_calibration": {"type": "boolean", "default": True},
            },
            "required": [],
        },
    },
    {
        "name": "get_planning_context",
        "description": (
            "One-shot planner context: live status + forecast + DOWN "
            "history + location + park hours + weather, all for a list of "
            "rides. Use this when the user is planning what to ride next. "
            "Single round trip with a consistent timestamp."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "park": {
                    "type": "string",
                    "description": "Park key (mk, epcot, hs, ak) or human name.",
                },
                "ride_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "List of ride names to include in the context. "
                        "Substring match is OK; the server resolves to "
                        "canonical ride records."
                    ),
                },
            },
            "required": ["park", "ride_names"],
        },
    },
    {
        "name": "find_rides_matching",
        "description": (
            "Filter and sort rides across the analytics snapshot. Use to "
            "answer 'what's the most reliable ride at MK?' or 'which "
            "rides have low downtime but high waits?' Helpful for "
            "discovery when the user names a constraint rather than "
            "specific rides."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "park": {"type": "string"},
                "max_downtime_pct": {"type": "number"},
                "min_downtime_pct": {"type": "number"},
                "min_avg_wait": {"type": "integer"},
                "max_avg_wait": {"type": "integer"},
                "sort_by": {
                    "type": "string",
                    "enum": [
                        "downtime_pct",
                        "avg_wait",
                        "max_wait",
                        "total_polls",
                        "ride_name",
                    ],
                    "default": "downtime_pct",
                },
                "sort_desc": {"type": "boolean", "default": True},
                "limit": {"type": "integer", "default": 20},
            },
            "required": [],
        },
    },
    {
        "name": "record_plan",
        "description": (
            "Persist a plan the user just accepted. Call this AFTER the "
            "user signals acceptance (e.g. 'let's do that', 'sounds "
            "good'). Don't call for hypothetical plans or one-off "
            "questions. The poller uses the recorded plan to fire "
            "plan-aware disruption alerts. By default the plan is for "
            "TODAY and auto-activates (monitoring starts immediately). "
            "To pre-build a day the user isn't at yet, pass "
            "planned_for_date — that row stays DORMANT (no alerts) until "
            "activate_plan flips it on its day. For a whole multi-day "
            "trip at once, prefer create_trip."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "park": {
                    "type": "string",
                    "description": "Park key or human name.",
                },
                "ride_sequence": {
                    "type": "array",
                    "description": (
                        "Ordered list of rides in the plan. Each entry "
                        "should include ride_name, ride_id, "
                        "predicted_wait_min, and position."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "ride_name": {"type": "string"},
                            "ride_id": {"type": "string"},
                            "predicted_wait_min": {"type": ["integer", "null"]},
                            "position": {"type": "integer"},
                        },
                        "required": ["ride_name", "position"],
                    },
                },
                "show_selections": {
                    "type": "array",
                    "items": {"type": "object"},
                },
                "context": {"type": "object"},
                "notes": {"type": "string"},
                "planned_for_date": {
                    "type": "string",
                    "description": (
                        "ISO date (YYYY-MM-DD) the plan is FOR. Defaults "
                        "to today. Pass a future date to pre-build a day "
                        "of an upcoming trip — that row stays dormant "
                        "(no alerts) until activated on the day."
                    ),
                },
                "trip_id": {
                    "type": "string",
                    "description": (
                        "Optional. Groups this day into a multi-day trip "
                        "(the id create_trip minted). Omit for a "
                        "standalone plan."
                    ),
                },
                "plan_window": {
                    "type": "object",
                    "description": (
                        "Optional {open, close} ET window. Once set + "
                        "activated, alerts only fire inside it. Usually "
                        "resolved at activation."
                    ),
                },
                "active": {
                    "type": "boolean",
                    "description": (
                        "Override the dormant/active default. Leave unset "
                        "for normal behavior: same-day plans auto-activate, "
                        "future-dated plans stay dormant."
                    ),
                },
                "ll_holds": {
                    "type": "object",
                    "description": (
                        "Lightning Lanes the party ALREADY HOLDS "
                        "(pre-booked MLL/ILL), as {ride name or ride_id: "
                        "return time like '10:00 AM'}. If the plan "
                        "mentions a booked LL it MUST go here (or via "
                        "set_held_ll after) — LL times written only into "
                        "notes are invisible to the trip page and the "
                        "alert engine. Only actually-booked LLs; "
                        "aspirational 'grab later' ones stay out."
                    ),
                    "additionalProperties": {"type": "string"},
                },
                "user_id": {"type": "string"},
            },
            "required": ["park", "ride_sequence"],
        },
    },
    {
        "name": "create_trip",
        "description": (
            "Pre-build a whole multi-day trip in one call: a trip header "
            "plus one DORMANT day-plan per date. Use this when the user "
            "wants to lay out an upcoming trip ahead of time ('plan our "
            "June 23-25 trip: MK, then EPCOT, then HS'). Each day is "
            "written dormant — NO disruption alerts — until the user "
            "activates it on the day via activate_plan (which "
            "re-evaluates that day against live conditions first). For a "
            "single same-day plan use record_plan instead. Do NOT "
            "activate days here and do NOT treat any live data as a "
            "prediction for the future dates."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Human label for the trip ('June 2026 family trip').",
                },
                "days": {
                    "type": "array",
                    "description": (
                        "Ordered list, one entry per trip day. ride_sequence "
                        "and the other per-day fields are optional and can be "
                        "filled in later per day via record_plan with the "
                        "trip_id."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "date": {"type": "string"},
                            "park": {"type": "string"},
                            "ride_sequence": {"type": "array", "items": {"type": "object"}},
                            "show_selections": {"type": "array", "items": {"type": "object"}},
                            "plan_window": {"type": "object"},
                            "notes": {"type": "string"},
                        },
                        "required": ["date", "park"],
                    },
                },
                "user_id": {"type": "string"},
            },
            "required": ["name", "days"],
        },
    },
    {
        "name": "get_plan_for_day",
        "description": (
            "Return the plan recorded for a specific day (default today). "
            "Use on a trip day ('what's my plan today?') to pull up that "
            "day's plan so you can re-evaluate it against live conditions "
            "and activate it, or mid-day to see what's left. Prefers the "
            "active plan for the date, else the most recently recorded one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "ISO date (YYYY-MM-DD). Defaults to today (ET).",
                },
                "user_id": {"type": "string"},
            },
            "required": [],
        },
    },
    {
        "name": "get_upcoming_trip",
        "description": (
            "Return the soonest upcoming (or in-progress) trip and its "
            "days. Call at the start of a session to see whether the user "
            "has a trip coming up ('you've got your June 23-25 trip — want "
            "to keep building it?'). Returns the nearest trip whose "
            "end_date >= today, with each day's park and whether that "
            "day's plan is active or still dormant."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
            },
            "required": [],
        },
    },
    {
        "name": "activate_plan",
        "description": (
            "Activate a day's plan: turn on live disruption monitoring "
            "AFTER re-evaluating it against live conditions. The "
            "activation step in the multi-day flow — on the trip day, "
            "once you've pulled the plan up (get_plan_for_day), re-checked "
            "it against get_planning_context (what's DOWN now, today's "
            "real forecast/weather/hours), and the user accepts the "
            "adjusted plan, call this. It flips the plan ACTIVE (the "
            "poller then fires disruption alerts for its rides — a dormant "
            "plan fires NOTHING until activated) and stores the "
            "re-evaluated ride_sequence + resolved plan_window. Same-day "
            "plans from record_plan are already active; use this for "
            "future/dormant plans on their day. Don't activate a future "
            "day early."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "plan_id": {
                    "type": "string",
                    "description": (
                        "The plan to activate. If omitted, the plan for "
                        "`date` (default today) is looked up."
                    ),
                },
                "date": {
                    "type": "string",
                    "description": (
                        "ISO date to look the plan up by, if plan_id isn't "
                        "given. Defaults to today (ET)."
                    ),
                },
                "ride_sequence": {
                    "type": "array",
                    "description": (
                        "The accepted, live-re-evaluated ride order — "
                        "replaces the stored sequence."
                    ),
                    "items": {"type": "object"},
                },
                "plan_window": {
                    "type": "object",
                    "description": (
                        "{open, close} ET window resolved to concrete times "
                        "(e.g. close -> the park's actual close). Alerts "
                        "fire only inside it once set."
                    ),
                },
                "user_id": {"type": "string"},
            },
            "required": [],
        },
    },
    {
        "name": "delete_trip",
        "description": (
            "Delete a whole trip — its header AND every day plan under it, "
            "in one cascade. Use when the user cancels/scraps a trip. To "
            "drop just one day, use delete_plan. Guardrail: REFUSES if any "
            "day has a recorded outcome (calibration history) unless "
            "force=True — surface the refusal and confirm with the user "
            "before retrying with force. Destructive: only call when the "
            "user clearly wants the trip gone."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "trip_id": {"type": "string"},
                "force": {
                    "type": "boolean",
                    "description": "Delete even if some days have recorded outcomes.",
                },
                "user_id": {"type": "string"},
            },
            "required": ["trip_id"],
        },
    },
    {
        "name": "delete_plan",
        "description": (
            "Delete a single day's plan (drop one day from a trip, or "
            "remove a standalone plan). For a whole trip use delete_trip. "
            "Guardrail: refuses if the plan has a recorded outcome unless "
            "force=True. Destructive: only when the user wants the day gone."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "string"},
                "force": {"type": "boolean"},
                "user_id": {"type": "string"},
            },
            "required": ["plan_id"],
        },
    },
    {
        "name": "update_trip",
        "description": (
            "Rename a trip (sets the trip header's name). A trip's days + "
            "dates are derived from its day plans, so change those by "
            "adding/removing days (record_plan / delete_plan), not here — "
            "this only updates the human label."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "trip_id": {"type": "string"},
                "name": {"type": "string"},
                "user_id": {"type": "string"},
            },
            "required": ["trip_id", "name"],
        },
    },
]


def get_tool_names() -> set[str]:
    """Names of every tool exposed to Claude in the eval surface."""
    return {t["name"] for t in TOOLS}
