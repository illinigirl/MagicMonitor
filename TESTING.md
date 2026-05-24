# Testing Strategy

This document captures what's tested in Magic Monitor, what's
deliberately not, and the rationale for both. It doubles as a
planning artifact — testing strategy is a design decision worth
making explicitly rather than implicitly.

## What's tested

Tests live in two suites, mirroring the two Python codebases:

### Poller Lambda — `infra/lambda/poller/tests/`

Run from the repo root: `pytest infra/lambda/poller`

| Module under test | What's verified |
|---|---|
| `weather.detect_storm_shift` | Storm-shift detection logic that gates the plan-weather-shift alert: false-positive cases (storm already in prior forecast → no re-alert), false-negative cases (storm newly appears → alert fires), edge cases (None prior, storm clearing). |
| `weather.format_storm_window` | Human-readable phrase used in Pushover bodies — non-empty, includes clock time, handles malformed input. |
| `db` cooldown helpers | DOWN / BACK_UP / LOW_WAIT / weather-shift cooldowns set and read symmetrically; cooldown keys don't collide across types or across (user, plan) tuples. Stubs `_table` with an in-memory dict; no real DDB required. |
| `db.put_weather_snapshot` / `get_prior_weather_snapshot` | Snapshot round-trips intact for the storm-shift detector's prior-state input. |

### MCP server — `mcp/tests/`

Run from the repo root: `pytest mcp`

| Function under test | What's verified |
|---|---|
| `_compute_calibration_summary` | The cross-session feedback loop's pre-computation: aggression averaging + threshold interpretation, timing distribution counts, per-ride bias from `completed_rides` (mid-trip mark_ride_complete path), per-ride bias from `per_item_feedback` (end-of-day recall path), confidence labels at the 5 / 3 / <3 sample-size thresholds, edge cases (no outcomes, missing fields). |
| `_compute_load_vs_forecast` | The live "today is running X% above/below forecast" signal: wait-weighted ratio math, exclusion of DOWN rides + low-predicted rides (noise filter) + rides without forecasts, confidence labels at the n>=5 / n>=3 / n<3 thresholds, per-ride breakdown included in the response. |

## Design philosophy

**The pattern worth naming:** the things tested here are the
"data plane does the math, LLM narrates the lesson" functions —
pure pre-computation that the LLM consumes via `get_planning_context`
or `get_user_plan_history`. They're the most consequential code in
the system because the agentic planner trusts their output without
re-deriving it. They're also the easiest to test rigorously because
they're pure functions.

The alert-side helpers (`detect_storm_shift`, cooldown helpers) are
tested for the same reason: they gate user-visible Pushover pings.
A false positive sends a real Pushover at 3am; a false negative
misses a real plan-disrupting event. Testing the logic is cheaper
than the alternative.

## What's deliberately not tested

Trade-offs are explicit:

| What's not tested | Why |
|---|---|
| **End-to-end Lambda invocation** | Requires real AWS resources (DDB, SSM, Pushover credentials). Out of scope for CI. Verified post-deploy via `aws lambda invoke` (see RUNBOOK). |
| **MCP tool routing / FastMCP framework** | Framework integration code; tested by the framework upstream. Manual smoke-test via Claude Desktop after each deploy. |
| **Web app (Next.js Server Components, route handlers)** | TypeScript side has its own type-checking surface; behavioral tests deferred. The web app's primary verification path is the smoke test in RUNBOOK (`curl` six paths, expect 200). |
| **themeparks.wiki / Open-Meteo fetchers** | External APIs — mocking them tests the mock, not the integration. Real-world validation through production usage. |
| **Pushover delivery** | External API. Production usage is the validation; the notifier `_send` function is straightforward and would mostly test `requests.post` behavior. |
| **CDK stack synth** | `cdk diff` before every deploy serves as the regression check. The CDK constructs are short enough that a unit test would add little signal. |

## CI

GitHub Actions runs three parallel jobs on every push to main and
on PRs. The workflow is at `.github/workflows/test.yml`. A green
badge in the README signals current test state.

| Job | What it does | Why |
|---|---|---|
| `python-tests` | pytest both Python suites (poller + MCP) | Pure-function logic that the LLM trusts without re-deriving. The bar from the original test strategy. |
| `web-typecheck` | `tsc --noEmit` against the Next.js app | Catches type drift before it ships. Cheap to run, catches a real class of regressions. |
| `cdk-synth` | builds the CDK TS + `cdk synth` | Verifies the infrastructure stacks still compile to valid CloudFormation. Catches CDK regressions before deploy. |

The three jobs run in parallel (independent failures, faster
overall feedback). Each does its own setup so one job's failure
doesn't block the others' logs from being readable.

### Known follow-ups in CI

- **ESLint is NOT in CI.** The current `eslint.config.mjs` (default
  scaffolded by Next.js 16) fails at config load with a circular
  structure error when run under ESLint 9 via the `@eslint/eslintrc`
  FlatCompat shim. Fixing requires either pinning ESLint to 8.x,
  updating `eslint-config-next`, or moving to a hand-rolled flat
  config. Deferred until lint becomes load-bearing for a workflow.
- **No web behavioral tests yet.** See "Future work" below.

## Local development

### Poller tests

```bash
cd infra/lambda/poller
pip install -r requirements.txt pytest
pytest
```

### MCP server tests

The MCP server uses a dedicated virtualenv at `mcp/.venv/` because
the MCP SDK is the largest dep. Activate that venv for tests:

```bash
cd mcp
.venv/bin/python -m pytest
```

Or install pytest into the existing system Python alongside the
MCP SDK and run from anywhere:

```bash
python -m pip install -r mcp/requirements.txt pytest
cd mcp && pytest
```

## Adding tests

When you ship a new pure-function piece of logic (calibration step,
alert trigger, classifier, aggregation), add a test for it. The bar
held in the existing suites: **every pre-computation function the
LLM trusts without re-deriving needs a test that pins its math.**

Tests for cooldown / dedup helpers should stub the DDB table — the
pattern is in `tests/test_db.py`. Tests for pure-function logic
should be plain unit tests with literal fixture dicts — the pattern
is in `tests/test_weather.py` and `tests/test_calibration.py`.

## Future work

| Task | Why |
|---|---|
| **Integration tests against `moto` or DDB local** | Closer to production fidelity for the DDB-touching code paths. Today's stub-table tests verify logic, not the boto3 wire protocol. |
| **TypeScript tests for `web/`** | The web app currently relies on `tsc --noEmit` for type checking. Behavioral tests via Playwright or Vitest would add real coverage. |
| **MCP tool integration tests** | End-to-end exercise of each tool via the MCP Inspector, comparing tool output schemas across releases. Catches breaking changes before Claude Desktop notices. |
| **Storybook / visual regression on the analytics page** | The hour×day-of-week heatmap is the most visually complex component. Visual regression would catch unintentional palette / layout changes. |

These are queued for post-portfolio-validation work; current
coverage is sufficient for a single-contributor project at this
scale where production smoke tests handle the integration layer.
