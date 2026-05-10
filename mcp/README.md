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

That installs the `mcp` SDK and `boto3`. The analytics tools work
fully offline (they read JSON files committed to the repo). Live
DDB tools (currently `get_ride_forecast`) require an active SSO
session — refresh with `aws sso login --profile watchtower` and
make sure Claude Desktop's MCP config sets `AWS_PROFILE=watchtower`
(see "Register with Claude Desktop" below).

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

Live-data tools (`get_ride_forecast`, `get_live_ride_status`,
`get_park_live_status`, `get_ride_downtime_today`) read from the
deployed DynamoDB table via the `watchtower` SSO profile. The
server picks that profile up automatically — `AWS_PROFILE` does
not need to be set in the MCP config.

(Earlier versions of this README suggested adding `"env":
{"AWS_PROFILE": "watchtower"}` to the magic-monitor block. Don't
bother — Claude Desktop currently rewrites the config file on
quit/launch and silently strips fields outside its known schema,
so the env block doesn't survive a restart. The server-side
default in `server.py` is the resilient fix instead. To point at
a different profile, override with `AWS_PROFILE=your-profile` in
your shell before launching Claude Desktop.)

Restart Claude Desktop after editing the config. The full tool
list should appear in the tools menu (wrench / 🔧 icon).

## Verify

In a new Claude Desktop conversation:

> Call hello_magic_monitor to make sure the Magic Monitor MCP server is reachable.

You should see Claude invoke the tool and return the greeting
string. To verify the live-DDB path:

> What's the wait-time forecast for Big Thunder Mountain right now?

Claude should call `get_ride_forecast`, return the latest forecast
snapshot, and (with luck) tell you when the wait peaks. To exercise
the other live-DDB tools:

> What's currently down at EPCOT?
>
> Is Space Mountain operating right now, and what's the wait?
>
> How many times has Test Track gone down today?
>
> I'm at Magic Kingdom and want to ride Pirates, Big Thunder, TRON,
> Haunted Mansion, and Space Mountain. What should I ride next?

If you see "AWS credentials expired" instead, run `aws sso login
--profile watchtower` and try again — Claude Desktop picks up the
refreshed SSO cache on the next tool call. If you don't see the tool show up at all, common causes:

- Forgot to restart Claude Desktop after editing the config.
- JSON syntax error in `claude_desktop_config.json` (trailing
  commas are common). Open in a JSON-validating editor.
- Wrong absolute path to `.venv/bin/python` or `server.py` —
  Claude Desktop launches the command literally and doesn't expand
  `~` or relative paths.

## Tools

Read-only by design. Analytics tools read static JSON snapshots
shipped with the repo. The live-data tool reads the deployed
DynamoDB table.

| Tool | Source | Purpose |
|---|---|---|
| `hello_magic_monitor` | — | Sanity check — returns a greeting + tool list |
| `get_park_heatmap` | snapshot | Wait-time heatmap cells for one park, optionally filtered to a day-of-week |
| `get_ride_analytics` | snapshot | Downtime %, hourly waits, peak/trough for one ride |
| `get_ride_dow_pattern` | snapshot | Per-(day-of-week, hour) wait + downtime cells for one ride |
| `get_ride_down_clusters` | snapshot | Contiguous DOWN runs for one ride; flap-style vs structural signal |
| `get_short_wait_baseline` | snapshot | Per-hour SHORT_WAIT alert thresholds for one ride |
| `get_ride_forecast` | DDB live | Latest themeparks.wiki forecast snapshot for one ride |
| `get_live_ride_status` | DDB live | Current live status of one ride (status, wait, LL availability) |
| `get_park_live_status` | DDB live | Current live status of every ride in one park, optionally filtered |
| `get_ride_downtime_today` | DDB live | Count of DOWN events for one ride during one park-day (today / N days back) |
| `get_planning_context` | mixed | One-shot trip-planner context: live status + forecast + DOWN history + lat/lon + park hours + weather, for a list of rides |
| `find_rides_matching` | snapshot | Filter and sort rides by predicates ("low downtime, high avg wait") |

Future tools (planned):

- `get_ride_forecast_history` — multiple poll-snapshots for a ride
  (Phase C: forecast-vs-actual accuracy analysis)

## Standalone debugging

```bash
.venv/bin/python server.py
```

The server blocks on stdio waiting for an MCP client; that's
expected. Use the MCP Inspector tool (`npx @modelcontextprotocol/inspector
.venv/bin/python server.py`) to exercise tools without setting up
a full client.
