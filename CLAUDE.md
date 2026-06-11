# CLAUDE.md — Magic Monitor project guidance

Read this at session start. It captures the project-specific patterns,
conventions, and failure modes that aren't obvious from a single file
read but matter for every change.

## Project orientation

Magic Monitor (MM) is a serverless live-status dashboard for Walt Disney
World, plus an MCP-exposed agentic trip planner. Four codebases:

| Path | What | Stack |
|---|---|---|
| `web/` | Next.js dashboard, served live at magicmonitor.megillini.dev | Next.js 16, React 19, Tailwind 4, NextAuth + Cognito |
| `infra/` | AWS CDK infrastructure | CDK TypeScript |
| `infra/lambda/poller/` | Every-2-min poller Lambda | Python 3.12, boto3 |
| `mcp/` | MCP server exposed to Claude Desktop | Python, FastMCP, ~22 tools |

**AWS:** account `601669029997`, region `us-east-2`, profile `watchtower`.
Table `DisneyData` (single-table DynamoDB) — the poller writes, the
web app reads, the MCP server reads + writes. SSO refresh:
`aws sso login --profile watchtower`.

**Authoritative docs in repo:** `PROJECT.md` (roadmap + decisions),
`RUNBOOK.md` (operational), `TESTING.md` (testing strategy + failure
modes — read this if you're writing anything that touches DDB or
deployed surfaces).

## Failure modes to actively watch for

This section is THE most important part of this file. These are
categories of bug that have actually shipped in production or that
the codebase's shape makes likely. Surface them when relevant in any
design or testing discussion — don't wait to be asked.

### Silent regressions from data growth

**The category:** code whose correctness depends on an implicit
assumption about data shape or volume — table size, row distribution,
average request payload size — can silently start producing wrong
output when reality drifts past the assumption, WITHOUT any code change.
Code review and most unit tests don't catch this because nothing in
the code changed.

**Concrete case (2026-05-24):** `web/src/lib/dynamodb.ts`
`getParkRides()` did a single-page DDB Scan with a FilterExpression.
The original comment said "1 round-trip, well under 4KB" — true at
the time. Then M6-B Phase 1 (shipped 2026-05-17) started writing
WAIT# rows on every poll, and the table grew past 1MB. The first
scan page stopped containing any STATE rows, the function returned
`[]`, and the live park pages silently rendered "0 attractions" for
roughly 7 days before being caught accidentally.

**Always do when designing or reviewing:**

1. **For any DDB Scan with FilterExpression:** paginate via
   `ExclusiveStartKey` / `LastEvaluatedKey`, OR document a hard
   upper bound on table size with a written expiry condition
   ("valid while table fits in one Scan page (~1000 items); switch
   to GSI Query when X starts accumulating"). Treat the assumption
   as an expiring contract, not a permanent fact.
2. **For any code whose comment says "small enough to X without Y":**
   ask when that stops being true and what catches the cutover.
   If the answer is "nothing catches it," that's a bug waiting.
3. **For any test that uses a small fixture:** add at least one
   test with a larger fixture that exercises pagination /
   chunking / streaming behavior.
4. **For any user-facing read path:** prefer the runtime monitoring
   layer (a canary that asserts non-empty response) as a stop-loss,
   in addition to whatever pre-deploy tests you write.

**Three-layer defense to apply for this category:**

- **Code-time:** explicit comments that name when the assumption
  expires; review-time check for "scan + filter without pagination."
- **Test-time:** unit test with a mocked client returning paginated
  responses, asserting the function reads all pages.
- **Runtime:** synthetic canary against the live URL that asserts
  the page renders non-empty data. Catches the failure mode within
  the canary cadence even when nobody anticipated the trigger.

This pattern generalizes beyond DDB scans — applies to any cached
computation, any "small enough" data assumption, any "in practice
this returns 1 page" pattern. When in doubt, ask "what catches
this when the implicit assumption breaks?"

### Plausible-but-wrong AI output

The MCP planner's tool-use surface produces natural-language plans
that LOOK reasonable even when they're subtly wrong (silently dropped
constraints, ignored calibration data, wrong park, missing weather
consideration). Code-level tests don't catch behavioral drift; the
eval framework in `mcp/evals/` exists exactly for this category.

When adding a new tool, changing a docstring, or changing the
agentic planner's instructions, add an eval case that exercises the
new behavior. The 10 existing cases cover the core planning flow
(happy path, context-reading, personalization, calibration,
ambiguity/guardrails) plus the M5 multi-day surface (future trip
build, on-the-day activation, future-day lookup, single
future-dated record, trip deletion).

Don't change tool docstrings in `mcp/server.py` casually — they
are the contract Claude reads at runtime. Run the eval suite after
any docstring change.

## Debugging this project specifically

The general debugging methodology (root-cause-first, 3-fix-stop,
Megan's push-back signals) lives in `~/.claude/CLAUDE.md` and
applies everywhere. The project-specific addition:

**Magic Monitor is a multi-component system** — bugs commonly
cross boundaries (poller → DDB → web, MCP → boto3 → table,
canary → live URL → SSR). When a bug doesn't reproduce locally
or symptoms appear in one component but originate in another,
**add diagnostic logging at each boundary FIRST** to surface
where the chain actually breaks, then investigate that specific
layer. Don't guess which component is at fault — there are too
many layers for guesses to land.

Concrete pattern when a web page renders wrong data:

1. Log what the SSR query received from DDB (in `web/src/lib/dynamodb.ts`)
2. Check what the poller most recently wrote (CloudWatch logs for the poller Lambda)
3. Confirm the DDB table state directly (`aws dynamodb query --profile watchtower ...`)

This is how you'd catch a repeat of the silent `getParkRides`
regression in minutes instead of days.

## Project conventions

### DDB access

- **Poller (write side):** `infra/lambda/poller/db.py` is the helper
  layer. `record_wait_observation()`, `mark_status_change()`, etc.
  All writes include TTL.
- **Web app (read side):** `web/src/lib/dynamodb.ts`. Server-only.
  Reads via SSR Server Components or Route Handlers.
- **MCP (read + write):** `mcp/server.py` reads via boto3 directly.

### Tests

- **Poller:** `pytest` with an in-memory stub table (no moto).
  Pattern in `infra/lambda/poller/tests/test_db.py::_StubTable`.
  Run from project root: `python -m pytest infra/lambda/poller`.
- **MCP:** `pytest` on pure-function math + behavioral evals via
  Anthropic API in `mcp/evals/`. Run evals from `mcp/`:
  `.venv/bin/pytest evals/`. Evals cost real API tokens (~$0.05 per
  case, ~$0.30 for full suite).
- **Web:** `tsc --noEmit` typecheck + Vitest unit tests in CI.
  Scaffold added 2026-05-25 with the GSI cutover; first coverage is
  `web/src/lib/dynamodb.test.ts` (the `getParkRides` read path).
  Run locally: `cd web && pnpm test`. Mock the DDB client at the
  module level via `vi.mock("@aws-sdk/lib-dynamodb", ...)` —
  pattern in the existing test. No JSX/component tests yet; when
  the first one lands, set up jsdom in `vitest.config.ts`.
- **CI** runs three parallel jobs (python-tests, web-typecheck,
  cdk-synth) in `.github/workflows/test.yml`. The web-typecheck
  job also runs `pnpm test` after typecheck. Plus a separate
  canary workflow that runs hourly against the live site.

### Alert routing (added 2026-05-24)

`infra/lambda/poller/alert_routing.py` is the resolver pattern for
when multiple alert sources match the same user for the same event
(e.g., user has ride in favorites AND in today's active plan). The
resolver picks the highest-priority candidate per user via explicit
priority constants. When adding a new alert source, append candidates
with the right priority — don't introduce coordination via
`if user in other_set: continue` checks. See the file's docstring
for the design rationale.

## Things to NEVER do

- **Push without explicit ask.** Even minor doc changes wait for
  confirmation.
- **Reintroduce interview/hiring/portfolio framing into any
  public artifact** (PROJECT.md, README, RUNBOOK, mcp/README,
  docs/*). The repo is public and reads as engineering
  documentation for a working product, not as a portfolio piece.
  Architectural rationale is fine and valuable — just don't wrap
  it in interview framing.
- **Commit `PICKUP-*.md` files.** They're personal session-handoff
  context, gitignored. Don't suggest pushing them.
- **Add RAG to the MM wisdom doc.** Decision is locked: at 1M
  context + prompt caching, context injection is the right pattern
  for a small corpus.
- **Touch deployed customer surfaces (web pages, CDK deploys)
  without a deliberate verification path.** Run typecheck +
  local dev verify before pushing; trust Amplify auto-deploy to
  complete in ~3-5 min; verify production via curl after deploy.
- **Mock the database in poller integration tests.** Use the stub
  table pattern that already exists in `tests/test_db.py`.
- **Change MCP tool docstrings without running the eval suite.**

## When in doubt

- `PROJECT.md` priority list shows what's queued and in what order.
- `RUNBOOK.md` covers production operations + the M2-B Amplify
  lessons (required reading before any CDK change to the web app).
- `TESTING.md` covers what's tested, what's deliberately not, and
  failure modes the project explicitly watches for.
- Ask Megan rather than guessing on architectural calls. She
  architects; you write the code (per her global preferences).
