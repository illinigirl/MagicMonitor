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
>
> What entertainment is running at EPCOT today?
>
> I'm at Hollywood Studios with Slinky Dog, Tower of Terror, and
> Rise of the Resistance on my list — but I also want to catch
> Fantasmic. How should I order things?
>
> [Day 1] Plan me a Magic Kingdom day — Pirates, Big Thunder, Haunted
> Mansion, Space Mountain, plus Happily Ever After.
>
> [Day 2, new conversation, same MCP server still loaded]
> Let's plan EPCOT today: Test Track, Cosmic Rewind, Soarin', and
> Spaceship Earth.
>
> [Claude should call get_user_plan_history first, see the
> unrecorded MK plan from yesterday, ask "before we plan today, how
> did your MK day go?", then use whatever you say to calibrate the
> EPCOT plan. After EPCOT is laid out and you accept, Claude calls
> record_plan to log it for the same loop.]

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
| `get_park_showtimes` | themeparks.wiki | Today's full entertainment lineup for one park — spectaculars, parades, stage shows, music, atmosphere, character meets — each classified and sorted by next-upcoming start time |
| `get_planning_context` | mixed | One-shot trip-planner context: live status + forecast + DOWN history + lat/lon + park hours + weather + headliner showtimes, for a list of rides |
| `find_rides_matching` | snapshot | Filter and sort rides by predicates ("low downtime, high avg wait") |
| `record_plan` | DDB write | Persist an accepted plan + the planner's predictions for the cross-session feedback loop. 24h TTL until outcome is recorded, then 1 year |
| `record_plan_outcome` | DDB write | Log how a recorded plan actually went — aggression rating, timing rating, per-ride/show actuals, free-text notes |
| `get_user_plan_history` | DDB live | Recent plans for a user with outcomes if recorded, plus a server-computed `calibration_summary` (aggression avg, timing distribution, per-ride / per-show prediction bias with sample sizes + confidence labels). The planner uses this to calibrate today's plan against the user's track record without having to derive aggregates from raw rows |
| `mark_ride_complete` | DDB write | User RODE the ride. Moves it from `ride_sequence` into `completed_rides` with optional `actual_wait_min`. Stops plan-disruption alerts AND captures actuals for the calibration loop. Prefer this to `remove_ride_from_plan` whenever the user actually rode the thing |
| `remove_ride_from_plan` | DDB write | User SKIPPED the ride. Moves it from `ride_sequence` into `dropped_rides` with optional `reason`. Negative signal for calibration ("plan was too aggressive"). Don't call this for rides the user actually rode — undercounts their completions |
| `add_ride_to_plan` | DDB write | Adds a spontaneous ride to `ride_sequence`. Poller starts watching within ~2 min |
| `get_mll_tiers` | static JSON | Current Multi-Pass tier rosters per park (Tier 1 / Tier 2 for MK/EPCOT/HS; no-tiers + LL-eligible list for AK). Used when the planner reasons about pre-arrival bookings (3-ride allocation, 1 Tier 1 + 2 Tier 2 at the tiered parks). Hand-maintained data file at `mcp/data/mll_tiers.json`; `updated_at` field on the response tells callers how fresh the snapshot is |

Future tools (planned):

- `get_ride_forecast_history` — multiple poll-snapshots for a ride
  (Phase C: forecast-vs-actual accuracy analysis)

### Plan feedback loop — design notes

`record_plan` / `record_plan_outcome` / `get_user_plan_history` form a
cross-session feedback loop that lets the agentic planner learn from
the user's actual outcomes.

**Lifecycle:**

1. User asks for a plan; Claude lays it out via `get_planning_context`.
2. User accepts ("let's do that, starting with Pirates"). Claude calls
   `record_plan` with the ride sequence, any shows, and a small
   context snapshot. DDB row is written with `outcome_recorded=false`
   and a 24h TTL.
3. Either same-conversation (user says "we're done", "thanks, that
   worked") OR at the start of a future planning session (Claude calls
   `get_user_plan_history` and sees the pending plan), Claude calls
   `record_plan_outcome` with the user's feedback. TTL extends to 1
   year and the row becomes calibration data for future plans.
4. Stale plans (planned > 14 days ago, never recorded) get flagged
   `stale_for_recall=true` so Claude stops asking about them.

**Why eager-write at plan-time vs. lazy-write at outcome-time:**

We write the row when the plan is made (not when feedback arrives) so
we capture the planner's predictions at the time — predicted wait per
ride, predicted arrival per show, the today_vs_forecast ratio Claude
saw. This is what makes "system learns from outcomes" rigorous: we
can compare the predictions Claude actually made against actuals later.
Lazy-write would lose the original predictions because the user only
remembers what happened, not what was forecast.

The 24h TTL on unrecorded rows limits cruft from plans the user
browsed past and never followed.

**Server-side calibration aggregation (the "system learns" loop):**

`get_user_plan_history` doesn't just return raw plan rows — it also
returns a pre-computed `calibration_summary` block derived from the
recorded outcomes. Same shape as the live `today_vs_forecast` block
in `get_planning_context`: pre-computed numbers, sample sizes, plus
ready-made interpretation strings the LLM can paraphrase. Buckets:

- `aggression`: avg score on a -1..+1 scale (-1 = too aggressive,
  +1 = not aggressive enough), plus an interpretation like *"User's
  recent plans tend to finish with time to spare. Pack more in
  today."*
- `timing`: distribution of `ran_over` / `on_time` / `extra_time`
  outcomes, plus avg `extra_time_minutes` when meaningful, plus
  interpretation like *"User finishes with extra time on 3/5 recent
  plans (averaging ~45 min spare on those days) — pack today's plan
  more aggressively."*
- `per_ride_prediction_bias`: per-ride avg(actual − predicted) wait
  delta, sample size, and a confidence label (`high` for n≥5,
  `medium` for n=3-4, `low` for n<3). Interpretations are
  ride-specific and direction-aware: *"Big Thunder tends to wait
  LONGER than predicted (+17 min avg, high confidence on n=5).
  Scale this ride's prediction up by ~17 min for this user."*
- `per_show_arrival_bias`: same pattern for show arrival timing
  (predicted arrival window vs. actual `arrived_with_min`).

The `usage_hint` field at the bottom of the summary tells the LLM
how to apply each bucket: surface aggression + timing as one upfront
calibration note (same convention as today_vs_forecast); apply
high-confidence per-item biases visibly to the user; apply
medium-confidence biases silently to predictions; ignore
low-confidence biases entirely (sample too small to be useful).

This is the design intent: the data plane does the math, the LLM
narrates the lesson. Server-side aggregation keeps the calibration
rigorous and version-controlled; LLM-side narration keeps the
conversational feel of the agentic loop.

**Auth model — single-user by design:**

MCP doesn't carry the calling client's authenticated user identity to
the server. For this family-use deployment, all three feedback tools
default `user_id="megan"` and write under `PK = USER#megan`. To plan
for someone else, pass `user_id` explicitly. Multi-user MCP is out of
scope for v1; the same data shape extends cleanly when MCP gains a
way to carry the auth subject from the calling client.

**Pushover-driven proactive feedback collection** (a Pushover ping at
park-close-time with one-tap "great / fine / ran-over" buttons that
deep-link to the web app) is intentionally NOT in v1. The cheap
cue-based + next-session triggers should cover most real usage; the
proactive loop is a follow-on to build only if data sparsity proves
the feedback gap is real.

### Showtimes classifier — note on the dual TS/Python implementation

`get_park_showtimes` (and the headliner subset embedded in
`get_planning_context`) reuses the same six-bucket classifier the
web app uses at `/parks/<park>/today` — but the classifier itself
is a verbatim Python port of the TS implementation in
`web/src/lib/showtimes.ts`. Two copies of the regex is the
deliberate trade-off:

- Same product judgment expressed in two languages — interview-
  legible "here's how I'd express the same heuristic across stacks."
- Drift cost is low: shows turn over maybe twice a year, and a "keep
  in sync" comment at the top of both files makes the link explicit.
- Failure mode is "MCP planner misclassifies show X for one cycle
  until both files are updated," never silently broken data.

Alternatives considered: shipping a static JSON snapshot built by a
TS classifier run, or shrinking the Python side to a four-bucket
classifier (planner-relevant categories only). Both add complexity
or asymmetry for marginal benefit at this scale.

## Standalone debugging

```bash
.venv/bin/python server.py
```

The server blocks on stdio waiting for an MCP client; that's
expected. Use the MCP Inspector tool (`npx @modelcontextprotocol/inspector
.venv/bin/python server.py`) to exercise tools without setting up
a full client.
