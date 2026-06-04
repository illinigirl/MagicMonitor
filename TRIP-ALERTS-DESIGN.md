# Design: shared-trip alerts — attendees + global mute

**Status:** proposed, from a design discussion with Megan 2026-06-03.
Local working doc — NOT committed (same convention as the other design
docs). Distinct from `TRIP-VISIBILITY-DESIGN.md` (dashboard + calendar);
this is about *who gets pinged* for a shared trip.

---

## Problem (verified in the poller)

Plan-aware alerts (a ride in an active plan transitions DOWN/UP, or a
weather shift) fan out to **the plan's `user_id` → that user's Pushover
key** (`index.py` `get_user_key(user_id)` → `db.get_user_profile`). Every
shared-trip `PLAN#` row is `user_id="megan"`, so **only Megan gets the
shared trip's plan alerts.** Jim and Michele — on the same family trip —
get nothing from it; they'd only be alerted via their own per-user
park-favorite subscriptions (generic, not plan-aware, self-curated).

Two wants fell out of the discussion: (A) let a trip say who's on it so
they get the trip's alerts, and (B) a master mute that overrides
everything. They share one implementation seam — a single per-recipient
gate in the poller's send path — so they're designed together.

## Step 0 — verify the base fires at all (do FIRST)

The shared trip partition is `USER#megan` (a friendly id), but Pushover
keys live under `USER#<cognito-sub>/PROFILE`. So `get_user_profile("megan")`
only resolves if a `USER#megan/PROFILE` row with a `pushover_user_key`
exists. **Confirm the current shared-trip alert path actually delivers to
Megan today** before building on it — if it doesn't, that's the real
first bug, and attendees would just be inheriting a broken base.

---

## Part 1 — Per-trip attendees (Model A)

Let a trip carry the family members on it; fan the trip's plan-aware
alerts out to each attendee's Pushover key (deduped — the per-user dedup
already exists).

**Data:** `attendees` on the `TRIP#` header — a list of family-member
identifiers. Store them as the **Cognito sub** (the key to the profile),
or a friendly id resolved via a sub↔name map. (Today's `MCP_SUB_USER_MAP`
is sub→name only and has just Megan + Jim — it'd need the reverse
direction + Michele's sub.)

**Write side:** an `attendees` param on `create_trip` / `update_trip`, or
a dedicated `set_trip_attendees(trip_id, members)`. Shared-partition write
like the other trip tools.

**Poller fanout:** for an active plan, resolve recipients from its
trip's attendees (not just `plan.user_id`) → each attendee's profile →
key → send (deduped). Today's single-recipient path becomes
attendee-list-driven.

### Issues with A (must address, by severity)

1. **Silent "no Pushover key" failure (biggest).** An attendee with no
   `pushover_user_key` is silently uncovered — adding them *looks* like
   coverage but isn't. Needs a **coverage surface**: when setting
   attendees (and on `/trips`), show ✓ will-be-alerted vs ⚠ no-key.
   Michele in particular must sign in to `/me` once and add a key.
2. **Identity plumbing.** friendly-id ↔ sub ↔ `USER#<sub>/PROFILE` ↔
   key. Needs a complete, reversible family map (add Michele) and ties to
   Step 0's `USER#megan/PROFILE` question.
3. **Per-trip vs per-day granularity.** Per-trip attendance over-alerts
   when the family splits (Jim pinged for the MK day he's skipping).
   Per-day is precise but more to manage. **v1: per-trip** (simpler);
   per-day is a later refinement.
4. **Cooldown re-key.** The weather cooldown is keyed `(plan.user_id,
   plan_id)`. With multiple recipients it MUST become per-`(attendee,
   plan)`, or one person's cooldown suppresses everyone's.
5. **Consent.** Adding Jim opts his phone in without his action — fine
   for family, but note it. See the variant below.

### Consent-flipped variant (alternative to attendee lists)
Instead of someone setting attendees on the shared trip, each family
member toggles **"alert me about the shared trip"** in their own `/me`
(a flag under `USER#<their-sub>`). The poller fans out to whoever opted
in (and has a key). Dodges issues 1 + 5 (they self-select and know their
key works) at the cost of no central control. Trade central convenience
for self-consent + simpler coverage.

---

## Part 2 — Global mute (overrides everything)

A per-user master off-switch: when set, **no alert of any kind** reaches
that user — favorites, plan-disruption, weather, all of it.

- **One gate.** Add `should_send_to(user_id)` in the poller that checks
  *both* "has a Pushover key" *and* "not muted." Every fanout
  (favorites, plan, weather) routes through it — the same seam attendees
  plug into. Muted → skip, full stop.
- **Self-controlled.** A field under the user's own `USER#<sub>/PROFILE`.
  Nobody mutes anyone else (the clean inverse of the attendee-consent
  worry).
- **Time-boxed, not just a toggle.** Prefer a `muted_until` timestamp
  (snooze 2h / mute today / until tomorrow AM) with an explicit
  indefinite option. The plain on/off toggle's failure mode is
  "muted Tuesday, forgot, missed Saturday's whole trip" — auto-expiry
  makes the default failure "alerts come back."
- **Visible.** `/me` shows "🔇 Muted (until 8am)" loudly so it's never a
  silent surprise.
- **Composes with attendees.** Mute is the *final per-recipient gate* —
  even an attendee on an active trip is suppressed by their own mute. So
  each person gets individual quiet without touching the shared trip.

**Cost: low** — a `muted_until` field + the one send-gate check.
Independently valuable (doesn't depend on attendees), so it can **ship
first** as a standalone quality-of-life win and establish the
`should_send_to` seam that attendees then reuse.

---

## Sequencing

1. **Step 0 — verify** shared-trip alerts deliver to Megan today
   (`USER#megan/PROFILE` + key). Fix if broken.
2. **Mute** — small, independent, establishes the `should_send_to` gate.
3. **Attendees** — the bigger build (data + write tool + fanout + cooldown
   re-key + coverage surface), reusing the gate from step 2.

## Open decisions
1. Attendee granularity — **per-trip** (v1) vs per-day?
2. Attendee model — central attendee list vs consent-flipped self-opt-in?
3. Mute shape — time-boxed (recommended) vs plain toggle vs both?
4. Coverage surface — where it lives (the attendee-setting flow, `/trips`,
   `/me`).
5. Identity map — extend `MCP_SUB_USER_MAP` to include Michele + a
   reverse lookup; confirm where each family member's Pushover key lives.
