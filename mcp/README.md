# Magic Monitor MCP Server

Exposes Magic Monitor's analytics and (eventually) live ride data
as tools an MCP client can invoke conversationally. Speaks MCP over
stdio — clients launch the server as a subprocess.

Use cases:

- Ask Claude Desktop "what time should I avoid Magic Kingdom on
  Saturdays?" and have it call into MM's heatmap data.
- Wire MM into an agentic orchestration framework (LangChain, CrewAI,
  etc.) as a tool source.
- Programmatic access to MM's read model without standing up a
  REST API tier.

## Setup

```bash
cd mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

That installs the `mcp` SDK (Python). No AWS credentials needed for
the v1 hello-world; later tools that read live DDB data will require
an active SSO session under the `watchtower` profile.

## Register with Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`
and add (or merge into) the `mcpServers` block:

```json
{
  "mcpServers": {
    "magic-monitor": {
      "command": "/Users/meganschott/Documents/Pi/Disney/mcp/.venv/bin/python",
      "args": ["/Users/meganschott/Documents/Pi/Disney/mcp/server.py"]
    }
  }
}
```

Then restart Claude Desktop. The `hello_magic_monitor` tool should
appear in the tools list (look for the wrench / 🔧 icon in the
input bar).

## Verify

In a new Claude Desktop conversation:

> Call hello_magic_monitor to make sure the Magic Monitor MCP server is reachable.

You should see Claude invoke the tool and return the greeting
string. If you don't see the tool show up at all, common causes:

- Forgot to restart Claude Desktop after editing the config.
- JSON syntax error in `claude_desktop_config.json` (trailing
  commas are common). Open in a JSON-validating editor.
- Wrong absolute path to `.venv/bin/python` or `server.py` —
  Claude Desktop launches the command literally and doesn't expand
  `~` or relative paths.

## Tools (v1)

| Tool | Purpose |
|---|---|
| `hello_magic_monitor` | Sanity check — returns a greeting |

Later tools (planned):

- `get_live_ride_status` — current STATE rows from DynamoDB
- `get_park_heatmap` — analytics heatmap cells for one park
- `get_ride_analytics` — per-ride downtime %, hourly waits, peak/trough
- `get_short_wait_baseline` — the threshold the poller uses for SHORT_WAIT alerts
- `find_rides_matching` — filter rides by predicates ("low downtime, high avg wait")

## Standalone debugging

```bash
.venv/bin/python server.py
```

The server blocks on stdio waiting for an MCP client; that's
expected. Use the MCP Inspector tool (`npx @modelcontextprotocol/inspector
.venv/bin/python server.py`) to exercise tools without setting up
a full client.
