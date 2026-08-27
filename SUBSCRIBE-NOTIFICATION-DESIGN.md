# Subscribe-time notification — design note

**Status:** DESIGN, not built. Captured 2026-06-08.

## Problem / motivation

Two gaps, one feature:

1. **The late-subscriber gap.** Ride-based alert cooldowns are keyed
   **per ride, globally** (`RIDE#<id>/COOLDOWN#DOWN`, etc.) — one fire
   serves all users, then the ride is quiet for the cooldown window.
   Consequence: if you favorite a ride (or subscribe to a park) right
   *after* its alert fired, you get nothing until the next state
   change. You're subscribed but blind to current state.
2. **"Did it work?"** When a user flips on alerts they want immediate
   confirmation the notification channel actually works — not silence
   until the next real event.

## Core reframe: snapshot, not replay

The subscribe-time push is **"show me the current state of what I just
subscribed to,"** NOT "replay the alerts I missed." That decides what
qualifies:

- ✅ **State-type** alerts: currently **down**, currently **low wait**,
  storm currently **in the forecast** (for an activated plan).
- ❌ **Transition-type** alerts: "back up," "just went down" — these are
  events; replaying a 20-min-old transition is noise.

This framing also sidesteps the global-cooldown problem (see below).

## The design: instant confirmation + deferred digest

Split the two concerns by cost:

1. **Instant confirmation** (sent from the subscribe tier, synchronously):
   *"✅ Alerts on for <park/ride> — checking current status…"*
   Cheap: one fixed message, no state read, no resolver, no cooldown.
   Proves the Pushover pipe works the moment the user toggles.
2. **Deferred state digest** (poller, ≤2 min later): computes current
   state for the new subscriptions and sends *"X and Y are down right
   now; Z has a low wait."* Reuses the existing `notifier` + routing.

The confirmation answers *"did it work?"*; the digest answers *"what
should I know?"* a beat later. The "checking current status…" wording
makes the follow-up feel intentional, not like a second random buzz.

## Individual vs digest — hybrid

- **1 item** (single favorite that's down) → individual, normal alert
  format.
- **2+ items** → one digest push. Favoriting a whole park with 4 rides
  down must NOT fire 4 simultaneous buzzes — that's the firehose the
  cooldowns exist to prevent.

## Cooldown interaction — own per-user scope

- The welcome digest **reads** current state but is **independent of the
  global per-ride cooldown** (that cooldown's job is "don't re-fire the
  *event* for everyone"; the welcome is "show *this user* current
  state" — a different concern). It neither consumes nor respects it.
- Add a small **per-user welcome guard** (e.g. "welcome sent for this
  subscription in the last N min") so toggling subscribe/unsubscribe
  can't let a user spam themselves.

## Where it runs

- **Instant confirmation:** in the subscribe tier — **web app** for
  ride/park favorites, **MCP** for plan activation. Needs the Pushover
  **app token** available there (today it's poller-only — this is the
  one real cost: the credential now lives in a second place). The send
  itself is a thin `requests.post` to Pushover + the user's stored key;
  a small duplicated helper is cheaper than plumbing a shared module
  across the deploy boundary (same call as `weather.py`).
- **Deferred digest:** subscribe action writes a *"pending welcome for
  user X"* marker; the next poll computes state and sends via existing
  plumbing. Keeps the heavy logic + Pushover fanout in the poller.

## Reuse

`alert_routing.py` already has a **candidate → resolver** model. The
welcome digest is "run the resolver against **current state** for this
user's new subscriptions, then digest the survivors" — not new alerting
logic, and it inherits the existing dedup behavior.

## Edge cases

- **Park closed** → reuse park-hours gating; don't report "down" for a
  closed park.
- **Nothing actionable** → still send the plain confirmation (no state
  lines). Confirmation fires regardless of whether there's anything to
  report.
- **Imminent double-send** → the per-user welcome guard + "one-time
  snapshot" keeps the welcome from colliding with the next poll's real
  alert.
- **Plan activation** → "valid notifications" = storm currently in
  forecast + any plan ride currently down (plan-scoped snapshot).

## Open questions (decide at build time)

- Confirmation channel: Pushover push (proves the pipe) vs in-app toast
  only (free, but doesn't prove the notification pipe). Leaning push.
- Per-user welcome-guard window (N min?).
- Digest threshold (is 2+ the right cutoff for digest vs individual?).
- Confirm the user's Pushover key is readable from the web tier (poller
  uses it today — verify the access path before building).

## Cost gate

Build + deploy hits the cost gate — surface cost + get explicit okay
before deploying. (Design only at this stage.)
