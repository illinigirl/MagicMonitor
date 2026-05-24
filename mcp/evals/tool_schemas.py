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

v1 surface: the planning-flow tools. Add more as eval cases need them.
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
            "plan-aware disruption alerts."
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
                "user_id": {"type": "string"},
            },
            "required": ["park", "ride_sequence"],
        },
    },
]


def get_tool_names() -> set[str]:
    """Names of every tool exposed to Claude in the eval surface."""
    return {t["name"] for t in TOOLS}
