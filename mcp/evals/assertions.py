"""
Assertion library for eval cases.

Each assertion is a pure function: (case, trace) -> None. On failure
it raises AssertionError with a descriptive message. The runner test
harness collects assertions per case and runs them after run_case
returns.

YAML cases express assertions as a list of single-key dicts mapping
assertion-name to its config:

    assertions:
      - tool_called: get_planning_context
      - tool_called_with:
          tool: record_plan
          args_min_length: { ride_sequence: 5 }
      - response_mentions: "Magic Kingdom"

Adding a new assertion = adding a new ASSERTION_REGISTRY entry +
implementing the corresponding `_assert_<name>` function.
"""

from __future__ import annotations

from typing import Any, Callable

from runner import Trace


def _assert_tool_called(spec: Any, trace: Trace) -> None:
    """spec = tool name (str). Passes if the tool was called >= 1 time."""
    name = spec if isinstance(spec, str) else spec.get("tool")
    if not name:
        raise ValueError("tool_called: missing tool name")
    matches = trace.tool_calls_for(name)
    if not matches:
        called = sorted({tc.name for tc in trace.tool_calls})
        raise AssertionError(
            f"Expected Claude to call '{name}', but it called: {called or '(no tools)'}"
        )


def _assert_tool_not_called(spec: Any, trace: Trace) -> None:
    """spec = tool name (str). Passes if the tool was NOT called."""
    name = spec if isinstance(spec, str) else spec.get("tool")
    if not name:
        raise ValueError("tool_not_called: missing tool name")
    matches = trace.tool_calls_for(name)
    if matches:
        raise AssertionError(
            f"Expected Claude NOT to call '{name}', but it was called "
            f"{len(matches)} time(s)"
        )


def _assert_tool_called_with(spec: dict[str, Any], trace: Trace) -> None:
    """Check that a tool was called and arguments meet structural rules.

    spec = {
        "tool": "<name>",
        "args_min_length": { "<arg>": <int> },  # optional
        "args_max_length": { "<arg>": <int> },  # optional
        "args_equal": { "<arg>": <value> },     # optional
    }
    """
    name = spec.get("tool")
    if not name:
        raise ValueError("tool_called_with: missing 'tool' key")
    matches = trace.tool_calls_for(name)
    if not matches:
        raise AssertionError(f"Tool '{name}' was never called")

    min_lens: dict[str, int] = spec.get("args_min_length", {}) or {}
    max_lens: dict[str, int] = spec.get("args_max_length", {}) or {}
    equals: dict[str, Any] = spec.get("args_equal", {}) or {}

    # Find at least one call that satisfies every constraint.
    last_failure: str | None = None
    for tc in matches:
        try:
            for arg_name, min_len in min_lens.items():
                val = tc.arguments.get(arg_name)
                actual = len(val) if hasattr(val, "__len__") else 0
                if actual < min_len:
                    raise AssertionError(
                        f"call to '{name}' had {arg_name}=len({actual}), "
                        f"expected >= {min_len}"
                    )
            for arg_name, max_len in max_lens.items():
                val = tc.arguments.get(arg_name)
                actual = len(val) if hasattr(val, "__len__") else 0
                if actual > max_len:
                    raise AssertionError(
                        f"call to '{name}' had {arg_name}=len({actual}), "
                        f"expected <= {max_len}"
                    )
            for arg_name, expected_val in equals.items():
                actual_val = tc.arguments.get(arg_name)
                if actual_val != expected_val:
                    raise AssertionError(
                        f"call to '{name}' had {arg_name}={actual_val!r}, "
                        f"expected {expected_val!r}"
                    )
            return  # this call satisfied everything
        except AssertionError as e:
            last_failure = str(e)

    raise AssertionError(
        f"No call to '{name}' satisfied all constraints. "
        f"Last failure: {last_failure}"
    )


def _assert_response_mentions(spec: Any, trace: Trace) -> None:
    """spec = string or list of strings. ALL must appear in final_text."""
    needles = [spec] if isinstance(spec, str) else list(spec)
    text = trace.final_text
    missing = [n for n in needles if n.lower() not in text.lower()]
    if missing:
        raise AssertionError(
            f"Expected response to mention {missing!r}. "
            f"Response text: {text[:300]}..."
        )


def _assert_response_does_not_mention(spec: Any, trace: Trace) -> None:
    """spec = string or list. NONE may appear in final_text."""
    needles = [spec] if isinstance(spec, str) else list(spec)
    text = trace.final_text.lower()
    present = [n for n in needles if n.lower() in text]
    if present:
        raise AssertionError(
            f"Expected response NOT to mention {present!r}, but it did. "
            f"Response text: {trace.final_text[:300]}..."
        )


def _assert_tool_called_in_order(spec: list[str], trace: Trace) -> None:
    """spec = list of tool names. Each must appear in trace in order.

    Doesn't require adjacency — other tools may interleave. Just that
    the listed tools appear in the given relative order.
    """
    expected_seq = list(spec)
    seq_iter = iter(expected_seq)
    target = next(seq_iter, None)
    for tc in trace.tool_calls:
        if target is None:
            break
        if tc.name == target:
            target = next(seq_iter, None)
    if target is not None:
        called = [tc.name for tc in trace.tool_calls]
        raise AssertionError(
            f"Expected tool order {expected_seq}, never reached '{target}'. "
            f"Actual sequence: {called}"
        )


def _assert_stop_reason(spec: str, trace: Trace) -> None:
    """spec = expected stop_reason ('end_turn', 'max_tokens', etc.)."""
    if trace.stop_reason != spec:
        raise AssertionError(
            f"Expected stop_reason={spec!r}, got {trace.stop_reason!r}"
        )


# Registry maps YAML assertion names to implementation functions.
# When adding a new assertion: implement _assert_X, register here, done.
ASSERTION_REGISTRY: dict[str, Callable[[Any, Trace], None]] = {
    "tool_called": _assert_tool_called,
    "tool_not_called": _assert_tool_not_called,
    "tool_called_with": _assert_tool_called_with,
    "tool_called_in_order": _assert_tool_called_in_order,
    "response_mentions": _assert_response_mentions,
    "response_does_not_mention": _assert_response_does_not_mention,
    "stop_reason": _assert_stop_reason,
}


def run_assertions(case: dict[str, Any], trace: Trace) -> None:
    """Iterate the case's assertions list and run each.

    Each list entry is a single-key dict mapping assertion-name → spec.
    Raises AssertionError on first failure (pytest reports it).
    """
    for entry in case.get("assertions", []):
        if not isinstance(entry, dict) or len(entry) != 1:
            raise ValueError(
                f"Each assertion must be a single-key dict, got: {entry!r}"
            )
        (name, spec), = entry.items()
        impl = ASSERTION_REGISTRY.get(name)
        if impl is None:
            raise ValueError(
                f"Unknown assertion '{name}'. Known: "
                f"{sorted(ASSERTION_REGISTRY)}"
            )
        impl(spec, trace)
