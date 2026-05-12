# Magic Monitor pickup prompt

Copy everything below the `---` line and paste it as your first
message to a fresh Claude Code session when resuming Magic Monitor
work. Keep this file updated when "where we are" or the priority
list shifts significantly.

Last updated: 2026-05-11 (end of session).

---

I'm picking up Magic Monitor. Project root: `/Users/meganschott/Documents/Pi/Disney/`

**Read these in order before doing anything:**

1. `PROJECT.md` — roadmap with the Done section reflecting current
   state. Top-level recent work is the 2026-05-11 entry.
2. `RUNBOOK.md` — operational layer. The "Hand-maintained data
   files" and "Plan-aware alert path" sections at the top capture
   recent operational realities; the M2-B lessons further down are
   required reading before touching Amplify Hosting in CDK.
3. `README.md` — portfolio artifact. The "Agentic planner with
   cross-session feedback loop" engineering judgment moment is the
   narrative anchor for the MCP suite.
4. `mcp/README.md` — 22 tools, design notes on the plan feedback
   loop + showtimes classifier dual-impl.

**Current state (as of 2026-05-11):**

- Repo is **public** on GitHub with MIT license.
- 22 MCP tools shipped. Agentic trip-planner with cross-session
  feedback loop + server-side calibration is the demo headline.
- Production poller fires plan-aware DOWN/UP alerts in addition to
  the favoriter-based ones. BACK UP cooldown bug fixed and deployed.
- Plan schema gained `completed_rides` + `dropped_rides` arrays;
  new `mark_ride_complete` tool captures actual_wait_min for
  calibration. `remove_ride_from_plan` modified to preserve entries
  in `dropped_rides`.
- Calendar awareness: `get_party_calendar` tool + `mll_tiers.json`
  and `party_calendar.json` hand-maintained data files. MNSSHP 2026
  dates verified; MVMCP + Jollywood pending Disney's announcement.
- Repo public-readiness sweep clean: MIT LICENSE, no secrets in
  tracked files, `.env.local` properly gitignored (never was
  committed).
- Dev tooling: `docs/aws-setup-brief.md` and
  `.planning/blog/STARTING-PROMPT.md` ready for spinning up new
  sibling projects.

**Refreshed priority for the next interview window** (also in
PROJECT.md → Demo-prep priority order):

1. **Capture Claude Desktop screenshots** — `docs/screenshot-brief.md`
   has the three target queries. Highest portfolio-return-per-
   minute remaining. Megan's manual work + ~2 min agent integration
   into README.
2. **Update MVMCP + Jollywood dates** when Disney publishes them
   (~10 min, mostly manual — paste dates into chat, agent updates
   `mcp/data/party_calendar.json` and flips `dates_status`).
3. **Weather-shift alerts** (~1-2 hr) — completes the agentic-loop
   story along a second axis. Poller already fetches weather inside
   `get_planning_context`; the alert path would compare a forecast
   shift against active plans + fire a Pushover.
4. **M6-B (live AWS data plane)** (~1.5-2 days) — the next major
   build. Pre- or post-interview depending on calendar window.
   Strong architecture-evolution narrative ("Pi-fed snapshot →
   MM-native collection, consumer interface unchanged, backfilled
   through the cutover").
5. **Magic Monitor showcase post for the blog** — separate project
   at `.planning/blog/`; the post is content-blocked on having
   more interesting features to write about, so it's natural to
   build the meaty stuff first.
6. **M9 (embedded agentic chat)** — post-interview.
7. **M5 (trip planning)** — personal-use polish, can slip.

**Pending environmental files** (none are blockers; you'll see
them in `git status`):
- `infra/lambda/poller/baselines.json` (regenerated, not editor-
  introduced)
- `web/next-env.d.ts` (Next.js auto-regen)
- `infra/pnpm-lock.yaml` (untracked, unclear origin)
- `web/src/app/api/debug/` (untracked diagnostic routes; per
  RUNBOOK Lesson 3 these are meant to be temporary)

**Constraint: demo-able at all times.** Every commit should leave
both magicmonitor.megillini.dev and the Claude Desktop MCP demo
working. Interview could land week 1 or week 3 — every pause point
should be portfolio-rich.

**Environment:**
- AWS profile: `watchtower` (SSO — `aws sso login --profile watchtower`)
- Account: 601669029997, Region: us-east-2
- Live URL: https://magicmonitor.megillini.dev
- GitHub: https://github.com/illinigirl/MagicMonitor (public, MIT)
- Amplify app id: d1ykat3qyev5c8
- MCP server: `mcp/server.py` registered in Claude Desktop config;
  defaults to AWS_PROFILE=watchtower internally
- See `docs/aws-setup-brief.md` for the full setup brief (also
  useful as a primer when spinning up sibling projects)

**My preferences (also captured in memory):**
- I architect; you write the code. Don't ask me to.
- Give me options with tradeoffs, not just one path.
- Push back if I'm scope-creeping. Hold the boundary.
- Clean, well-commented code. Comments explain "why" — hidden
  constraints, surprises, workarounds — not "what."
- I keep sessions running across days. Check the system date
  before saying "today."
- I have premium tokens; don't tell me to wrap up to save context.
- For frontend changes, start the dev server and test in a browser.
- For destructive AWS ops, confirm before executing.
- I catch data-quality issues sharply — investigate them
  concretely rather than deflect.
- Default to my actual style instead of asking the same question
  every session.

**First action:** Confirm you've read PROJECT.md, RUNBOOK.md,
README.md, and mcp/README.md, then propose what to start with based
on time available — weather-shift alerts (~1-2 hr), M6-B (~1.5-2
days), screenshot capture (mostly manual, ~5 min agent integration),
or a date update for MVMCP/Jollywood if Disney has announced them
since this prompt was written.
