# Claude Desktop screenshot brief

Capturing fresh demo screenshots of the MCP agentic planner for the README. The web-app screenshots are already in `docs/screenshots/`; this brief covers the Claude Desktop side, which is the project's headline capability.

## Setup before capturing

1. **Refresh AWS SSO** so the live tools work:
   ```
   aws sso login --profile watchtower
   ```
2. **Restart Claude Desktop** to make sure it picked up the latest MCP server (17 tools as of 2026-05-10).
3. **Mac display**: bump font scale up one notch (System Settings → Displays → "Larger Text") so the screenshots are legible when embedded in the README at moderate zoom. Reset after capturing.
4. **Conversation hygiene**: start a fresh chat for each screenshot so prior context doesn't leak in.
5. **Crop**: tight crop on the conversation pane — exclude the sidebar / window chrome / nothing useful.

Save target: `docs/screenshots/mcp-<name>.png`. Add new entries to the README's "Demo" grid after capture (or send me the filenames and I'll wire them in).

---

## Required (the two that go in the README)

### `mcp-planning-tool-use.png`

**The agentic-coding demo image.** Shows Claude calling a tool, the tool returning data, and Claude reasoning over it conversationally.

**Query:**
> I'm at Magic Kingdom right now and have Pirates, Big Thunder, TRON, Haunted Mansion, and Space Mountain on my list. We've got about 4 hours left in the park. What should I ride next?

**What to capture:**
- The `get_planning_context` tool-use block (Claude Desktop renders these collapsed by default — expand it so the JSON args are visible)
- The first few lines of the tool's response (showtimes, weather, ride list — just enough to show it's pulling real data)
- Claude's narrative response below, with the ordered plan + cost-of-delay reasoning

If Claude also calls `get_user_plan_history` first (likely on a fresh restart with no plan history), that's fine — include it. Shows the feedback-loop check happening.

### `mcp-showtime-aware-plan.png`

**The showtime integration image.** Shows the planner treating a show as a fixed time-block and sequencing rides around it.

**Query:**
> Plan me a Hollywood Studios afternoon: Slinky Dog, Tower of Terror, and Rise of the Resistance. I also want to catch Fantasmic at 9pm.

**What to capture:**
- The `get_planning_context` call with `ride_names` arg visible
- The response showing the `showtimes` block (with Fantasmic specifically)
- Claude's plan with arrival-time math for Fantasmic ("arrive at the amphitheater by 7:30pm — that's 90 min before showtime, scaled up for today's crowd level…")

---

## Optional stretch — feedback loop in action

The calibration loop only kicks in after at least one recorded plan exists. If you want a screenshot showing it, seed a plan first:

### Seed step (one-time)

In a Claude Desktop conversation:
> Use record_plan to log this plan I just did at Magic Kingdom on 2026-05-09: Pirates (predicted wait 15), Big Thunder (predicted wait 45), Haunted Mansion (predicted wait 25). Then use record_plan_outcome with aggression_rating=not_aggressive_enough, timing_rating=extra_time, extra_time_minutes=40, free_text="finished an hour early, kids wanted snacks".

That writes one `USER#megan/PLAN#…` row in DDB with a real outcome attached.

### `mcp-feedback-loop.png` (optional)

Then start a **new conversation** and ask:
> Plan me a Magic Kingdom afternoon — Pirates, Big Thunder, Mansion, Space Mountain.

**Capture:**
- Claude calls `get_user_plan_history` at the start
- Claude either (a) asks "before we plan today, how did your other recent MK day go?" — referencing the seeded plan — OR (b) directly applies the calibration ("your last plan finished with 40 min of extra time, so I'm packing more in today")
- The plan that follows, showing the calibrated approach

### Cleanup after capture

Delete the seed row:
```python
# from mcp/ directory
.venv/bin/python -c "
import sys; sys.path.insert(0, '.')
from server import _ddb_table
t = _ddb_table()
# replace plan_id with the iso_ts you used
t.delete_item(Key={'PK': 'USER#megan', 'SK': 'PLAN#2026-05-09T...'})
"
```

Or just leave it — it auto-expires from the 1-year TTL.

---

## Format notes

- Use the macOS native screenshotter (Cmd+Shift+4, then drag) so we get PNG with retina resolution.
- Aspect ratio: the README's demo grid is a 2-col layout; landscape-ish (wider than tall) reads better there. Aim for ~1200-1600 px wide.
- If the tool-use JSON expansion makes the screenshot too tall, take two screenshots and stack them (or accept the height — anyone evaluating the agentic-coding work WANTS to see the tool args and response).
- Don't redact `user_id="megan"` — that's the actual single-user default; calling it out is honest about the auth model in PROJECT.md M9.

## After capture

Update the README "Demo" grid in the table around line 83:

```markdown
| ![MCP planning](docs/screenshots/mcp-planning-tool-use.png) | ![Showtime-aware plan](docs/screenshots/mcp-showtime-aware-plan.png) |
| **Agentic planner — tool use** — Claude calls `get_planning_context`, gets one round trip of live status + forecast + weather + showtimes, reasons over it conversationally with cost-of-delay sequencing | **Showtime-aware planning** — planner treats Fantasmic as a fixed time-block, walks backward from showtime to schedule rides + arrival buffer |
```
