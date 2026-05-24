"""
Eval runner — calls Claude with the MCP tool surface and routes Claude's
tool calls to canned responses from the YAML case.

The runner does NOT execute real MCP tool functions. Instead, each case
specifies what each tool should return when called. This isolates the
eval to "given these tool responses, does Claude behave well?" — which
is the right thing to test for an LLM behavioral eval. The MCP tools
themselves have unit tests in mcp/tests/.

Returns a Trace object that the assertion library inspects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import anthropic

from tool_schemas import TOOLS, get_tool_names

# Cap conversation length so a buggy case can't burn a $20 budget while
# Claude loops on missing tool data. 20 turns is well past any realistic
# planning conversation.
MAX_TURNS = 20

MODEL = "claude-opus-4-7"


@dataclass
class ToolCall:
    """One tool invocation by Claude during the eval."""

    name: str
    arguments: dict[str, Any]
    response: Any  # What we returned (the canned value)


@dataclass
class Trace:
    """Full record of what happened in an eval run.

    Inspected by assertions. Keep this structure stable — adding new
    fields is fine, removing/renaming breaks every case at once.
    """

    case_name: str
    final_text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    turns: int = 0
    stop_reason: str = ""
    raw_messages: list[dict[str, Any]] = field(default_factory=list)

    def tool_calls_for(self, name: str) -> list[ToolCall]:
        """Convenience accessor used by assertions."""
        return [tc for tc in self.tool_calls if tc.name == name]


def _resolve_tool_response(case: dict[str, Any], tool_name: str) -> Any:
    """Look up the canned response for a tool call in the case.

    If the case doesn't define a response for this tool, return a
    sentinel error payload. The eval will likely fail at assertion
    time — that's the point. Cases should be explicit about every
    tool Claude is expected to call.
    """
    responses = case.get("tool_responses", {})
    entry = responses.get(tool_name)
    if entry is None:
        return {
            "_eval_warning": (
                f"No canned response defined for tool '{tool_name}'. "
                "Either Claude called an unexpected tool, or the case "
                "is missing a tool_responses entry."
            )
        }
    # Cases can specify either `{response: ...}` (explicit) or the
    # response value directly. Accept both for ergonomics.
    if isinstance(entry, dict) and "response" in entry:
        return entry["response"]
    return entry


def run_case(client: anthropic.Anthropic, case: dict[str, Any]) -> Trace:
    """Run a single eval case end-to-end.

    Args:
        client: Anthropic SDK client.
        case: Parsed YAML case dict. Required keys: 'name', 'prompt'.
            Optional: 'tool_responses', 'system'.

    Returns:
        Trace capturing what Claude did.
    """
    trace = Trace(case_name=case["name"])

    # Validate that case tool_responses only reference tools we expose.
    # Catches typos before burning API tokens.
    known_tools = get_tool_names()
    for name in case.get("tool_responses", {}).keys():
        if name not in known_tools:
            raise ValueError(
                f"Case '{case['name']}' references unknown tool "
                f"'{name}'. Known tools: {sorted(known_tools)}"
            )

    messages: list[dict[str, Any]] = [
        {"role": "user", "content": case["prompt"]}
    ]

    system_prompt = case.get(
        "system",
        # Default system prompt mirrors the operating instructions Claude
        # Desktop/iOS receives when the MCP server is connected. Kept
        # deliberately minimal — the eval should reveal what Claude does
        # by default, not coach it into behaviors.
        "You are helping the user plan a Disney World trip. Use the "
        "tools available to gather context and propose plans. When the "
        "user accepts a plan, record it via record_plan.",
    )

    while trace.turns < MAX_TURNS:
        trace.turns += 1
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=system_prompt,
            tools=TOOLS,
            messages=messages,
        )

        # Append the assistant's response to the message history
        # (required by the API for tool-use continuation).
        messages.append({"role": "assistant", "content": response.content})

        trace.stop_reason = response.stop_reason or ""

        # Pull text content out for the final-text trace field. There
        # may be both text and tool_use blocks in the same response;
        # we capture text from every turn so partial reasoning is
        # visible to assertions.
        for block in response.content:
            if block.type == "text":
                # Concat text across turns. Final turn's text usually
                # dominates because that's the summary/recommendation.
                if trace.final_text:
                    trace.final_text += "\n"
                trace.final_text += block.text

        if response.stop_reason != "tool_use":
            # Claude is done — either end_turn (normal completion) or
            # max_tokens (truncation) or stop_sequence. Either way the
            # eval is over.
            break

        # Build tool_result blocks for every tool_use block in the
        # response. Per the Anthropic API spec these all go into a
        # single user-message turn.
        tool_results: list[dict[str, Any]] = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            canned = _resolve_tool_response(case, block.name)
            trace.tool_calls.append(
                ToolCall(
                    name=block.name,
                    arguments=dict(block.input) if block.input else {},
                    response=canned,
                )
            )
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": _format_tool_result_content(canned),
                }
            )

        messages.append({"role": "user", "content": tool_results})

    trace.raw_messages = messages
    return trace


def _format_tool_result_content(value: Any) -> str:
    """Serialize a canned tool response for the Messages API.

    Anthropic's tool_result content can be a string OR a list of blocks.
    We always use a string (JSON-serialized) because the MCP tool
    responses are dicts and Claude reads JSON fluently.
    """
    import json

    if isinstance(value, str):
        return value
    return json.dumps(value, default=str)
