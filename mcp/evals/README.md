# MCP Eval Suite

Behavioral tests for Claude's use of the Magic Monitor MCP tools.

The regular `pytest mcp/tests` suite tests pure-function logic. This
suite tests something different: **given specific tool responses,
does Claude make the right tool calls and produce reasonable plans?**
That's a behavioral question, not a deterministic one, and it deserves
its own infrastructure.

Each eval costs Anthropic API tokens (~$0.05–0.20 per case depending
on token usage). This suite is **NOT** run in CI on every PR. Run it
locally when you change MCP tool docstrings or want to verify Claude's
behavior on a class of prompts.

## Setup

The eval runner reads `ANTHROPIC_API_KEY` from `mcp/.env` (gitignored)
or from your shell environment.

```bash
# One-time: install eval-specific deps
pip install -r mcp/evals/requirements.txt

# Run all cases
cd mcp
pytest evals/ -v

# Run one case
pytest evals/ -v -k basic_mk_plan
```

## How a case works

Each YAML file in `cases/` defines:

1. **`prompt`** — what the user asks Claude.
2. **`tool_responses`** — canned responses for each MCP tool Claude
   might call. The runner returns these instead of executing the real
   tool. This isolates the eval to "is Claude's behavior good given
   this state?" — the actual tool implementations have their own unit
   tests.
3. **`assertions`** — list of behavioral checks. Each is a single-key
   dict mapping an assertion name (see `assertions.py`) to its config.

The runner loops Claude in the Anthropic Messages API tool-use cycle
until `stop_reason != "tool_use"`, capturing every tool call and the
final response text. Assertions then inspect the captured trace.

## Available assertions

| Name | What it checks |
|---|---|
| `tool_called` | Tool was called >= 1 time. |
| `tool_not_called` | Tool was not called at all. |
| `tool_called_with` | Tool was called with args matching constraints (`args_min_length`, `args_max_length`, `args_equal`). |
| `tool_called_in_order` | Listed tools appear in the trace in the given relative order (other tools may interleave). |
| `response_mentions` | Final response text contains the given string (or all strings if a list). Case-insensitive. |
| `response_does_not_mention` | Final response text does NOT contain the given string(s). |
| `stop_reason` | Final stop_reason matches expected (`end_turn`, `max_tokens`, etc.). |

## Adding a new case

1. Copy `cases/basic_mk_plan.yaml` as a starting template.
2. Set a unique `name`, write the `prompt`, define `tool_responses`
   for every tool you expect Claude to call.
3. Define `assertions` capturing the behaviors you care about.
4. Run `pytest evals/ -v -k <your-case-name>` to verify.

## Adding a new tool to the eval surface

When server.py adds a new `@mcp.tool()` that you want Claude to be
able to call in evals:

1. Add the Anthropic-format tool definition to `TOOLS` in
   `tool_schemas.py`.
2. Any existing case that wants Claude to use the new tool needs a
   `tool_responses` entry for it.

This is deliberate friction — exposing a tool to evals is a separate
decision from making it available to Claude Desktop / iOS in
production.

## Adding a new assertion type

1. Implement `_assert_<name>(spec, trace)` in `assertions.py`. Raise
   `AssertionError` on failure with a descriptive message.
2. Register it in `ASSERTION_REGISTRY`.
3. Use it in case YAML.

## Cost estimation

Each case = one Anthropic Messages API call sequence. Typical:

- 1–5 tool-use turns per case
- ~2K input tokens (system prompt + tool defs + canned responses)
- ~500 output tokens (Claude's reasoning + final response)
- Per-case cost at Opus 4.7 pricing: roughly $0.05–0.20

A full suite of 20 cases per run ≈ $1–4. Don't loop this on a cron.
