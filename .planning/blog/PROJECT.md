# Blog at megillini.dev — project queue

**Brand:** Megan Builds. Personal blog at `meganbuilds.megillini.dev`.
First post showcases Magic Monitor; broader purpose is a long-form
portfolio surface for writing about engineering decisions in a voice
that matches who Megan actually is.

The original plan was `blog.megillini.dev`, pivoted on deploy day
(2026-05-12) when a stale CloudFront alias claim on that subdomain —
left over from CDK delete/recreate cycles during the Amplify Hosting
bug rabbit hole — wouldn't clear after 45+ min and was producing a
misleading "Unable to assume IAM role" error. `meganbuilds.megillini.dev`
had clean history and landed first try. It also matches the brand
directly (the masthead literally says "megan builds"), so the swap
is a strict upgrade.

Status: **queued, design locked** (visual direction + tokens locked
via Claude Design — see `.planning/blog/design-handoff/`).
Implementation hasn't started.

---

## Why this exists

- Adds a "writes about engineering choices" surface to the portfolio
  alongside the MM agentic demo and the Watchtower sibling project.
- Gives Magic Monitor a long-form narrative home — the MM README is
  portfolio-grade but doesn't capture the *decision* story.
- Showcases who Megan is, not just what she ships — femininity is
  a differentiator, not a footnote.
- Forcing function for writing things up — turns conversation-with-
  Claude iteration into shareable artifacts.

---

## Voice & tone (locked from design handoff)

- **Casual, funny, sarcastic** — like talking to a friend who happens
  to ship things.
- **Confident but self-deprecating** — *"I let Claude run my house
  and lived to tell about it,"* not *"Exploring agentic orchestration
  patterns."*
- **Specific over generic** — *"the gelato is real cream, real
  sugar, and I will die on that hill"* beats *"I make ice cream."*
- **Femininity is a differentiator** — the hot pink is the point.
  Sundress at standup energy.
- **Recurring jokes** worth weaving in: the Roomba is sentient,
  mostly keto but loud about the ice cream exception (real cream,
  real sugar — no artificial sweeteners, ever), Claude is a
  coworker now, *"I will spreadsheet anything."*
- **Avoid:** corporate speak, "passionate about," "leveraging,"
  self-deprecation that reads as actually insecure.

Titles are full sentences with a joke. Excerpts deliver a punchline,
not a summary. Read times honest — short posts (3-5 min) flagged
as such; variety is part of the texture.

---

## Visual design (locked from Claude Design — `design-handoff/`)

**Direction: A2 — Zine, rebalanced.** Hot pink lives in the masthead
band and as a recurring accent (chips, rules, marker scrawls). Body
breathes on cream paper so the writing leads and the pink reads as
*signature*, not noise.

### Design tokens (in `design-handoff/tokens.css`)
- `--paper: #fbf6ef` (main background) + `--paper-2: #f3e8d8`
- `--ink: #1a0d1f` (primary text + structural lines)
- `--hot: #ff2d87` (signature hot pink) + `--hot-soft: #ffe2ee`
- `--pop: #ffd23f` (canary yellow accent)
- `--mint: #6be3b8` (tertiary accent)

### Typography
- **Display** — Caprasimo (decorative serif, riso vibe)
- **Marker** — Caveat (handwritten accent)
- **Body** — Inter (shared with Magic Monitor for visual lineage)
- **Mono** — JetBrains Mono (shared with MM)

### Signature aesthetic
- **Hard offset shadow** (`2px 4px 0 ink`) on cards — riso print
  signature, NOT soft drop shadows.
- **Slight rotation** (±1.2°, max 2°) on cards in grids for
  hand-pasted feel. Masthead, footer, primary-CTA cards stay at 0°.
- **Hover state:** shadow grows to `3px 6px 0 ink`, card translates
  -1px/-2px.

### Components (templates in `design-handoff/pages/`)
Masthead, Footer, Chip, Card, Stamp, Tape, Marker text, PullQuote,
CodeBlock, ProjectRow, PostCard. The JSX in `pages/home.jsx` and
`pages/templates.jsx` are the source of truth — port to whichever
framework gets picked.

---

## Locked decisions (no need to revisit)

| | Choice | Why |
|---|---|---|
| **Framework** | **Astro** | Built for content-heavy static sites; zero JS by default; first-class MDX; faster builds; the design handoff README recommends it and the technical fit is right. Trades MM-stack familiarity (~2 hours of Astro orientation cost) for a genuinely better tool for this job. |
| **v1 page scope** | **Core 4: /, /posts, /posts/[slug], /404** | Ship the design's reading surface fast with one good MM showcase post. Defer /projects + /projects/[slug] + /about + /now to v1.1 since those are content-blocked (need real bios + project blurbs beyond MM). The 404 is free brand voice — the "Claude returned a recipe" gag is a tone signature, costs nothing. |
| Content format | MDX (`@astrojs/mdx`) | Lets posts embed React components for callouts + code |
| Rendering | Static export (SSG) | Content is build-time; no SSR Lambda needed |
| Deploy | AWS Amplify Hosting, us-east-2 | Same as MM. Let Amplify auto-issue the cert. |
| Domain | `meganbuilds.megillini.dev` (subdomain, not apex) | Brand-exact, no apex-record gymnastics. Pivoted from `blog.megillini.dev` on deploy day after a stale CloudFront alias claim wouldn't clear — see RUNBOOK Lesson 7 / Lesson 8. |
| Auth | None (public read-only) | Personal blog; no signup, no comments, no view tracking |
| IaC | AWS CDK (TypeScript), single stack `BlogStack` | Same pattern as MM `DisneyStack` |
| GitHub | New repo under `illinigirl` | Megan's GitHub org |
| Code highlighting | `rehype-pretty-code` with the syntax palette from `tokens.css` (`.kw` hot pink, `.mod` pop yellow, `.com` mint green) | Matches the design — codeblock styling is a signature element |

---

## Content model

Posts live in `content/blog/<slug>.mdx` (Astro) or
`content/blog/<slug>.mdx` (Next.js — same shape). Frontmatter:

```yaml
---
title: "Full sentence title, ideally with a joke"
date: "2026-MM-DD"
readTime: 7
tag: "Agentic Coding"     # one of ~6 domain tags
excerpt: "Punchline, not a summary. Set up a moment, drop a line."
---
```

See `design-handoff/data.js` for the shape of sample posts —
particularly the title voice and excerpt punch.

---

## Out of scope for v1

The agent will be tempted to add these. Don't.

- **Tags / categories** as a navigation surface — single tag per
  post is fine in the schema; tag-filtered views land in v1.1.
- **Search.** Add when there are 10+ posts.
- **RSS feed.** Add when readers ask.
- **Comments.** No.
- **Newsletter signup.** No.
- **View counts.** No.
- **OG image generation.** Nice-to-have for shared posts, not v1.
- **Sitemap.** Trivial to add later when SEO matters.
- **Dark mode toggle.** The cream paper IS the brand. If added
  later, invert to ink background with cream text and keep hot
  pink + pop yellow unchanged.
- **CMS / admin UI.** Content is git.

---

## First post: Frame TV mood lighting (pivoted from MM showcase)

**Title (locked):** *"I asked Claude to fix a Samsung TV bug. Now my
house picks its own mood lighting."*

**File:** `src/content/blog/frame-tv-mood-lighting.mdx` (drafted
2026-05-12). Tag: `Home Automation`. Read time: ~9 min.

**Why this swap from the MM showcase:** Magic Monitor is still in
active development; an MM-first post would have to caveat itself.
Frame TV has the better "first thing I really built with Claude"
narrative arc and a chaos beat (Frame TV firmware staging coups on
*Bluey*) that fits the voice. The MM showcase moves to the future
post backlog and lands once MM is more settled.

**Frame TV post structure (drafted):**
1. The setup — husband to Mexico 2wk, kids 10/12, day job, new Pi,
   eight years of solo Python automation. Light reference to the
   2018/chemo origin story; flagged as separate future post.
2. The problem given up on — Frame TV's one shuffle option ("ALL"),
   tiny on-device storage, uploads erroring out.
3. The unstuck moment — Claude reads code, she clicks OK on the
   remote, it just works. Recurring "click OK on the remote" beat.
4. Collections — filename parser, mood/artist/movement/color tags.
5. The HEIC → JPEG pipeline (meta-joke: post screenshots converted
   by same code path).
6. Light show mode — color extraction → Hue + Govee dispatch.
7. Spotify drift — Sonos thumbnail auto-display.
8. Calendar card — Google Calendar between every 3rd piece, color-
   matched to prior art. Reminders overlay.
9. Chaos beat — Frame firmware lying about idle state, art mode
   coup mid-show.
10. The close — "ideas don't die in my notes anymore."

Photos: `src/content/blog/frame-art/{art,calendar,reminder}.jpg`,
converted from HEIC originals via `sips`.

---

## Future post backlog

- **"Go automate something"** — the 2018/chemo origin story for
  Megan's home automation. Her sister told her, during treatment,
  to go automate something when she got stressed. Eight years of
  solo Python came out of that. Touched lightly in the Frame TV
  post; deserves its own. *Sensitive material — Megan's call on
  voice/depth when she's ready to write it.*
- **What I do with the lights, generally** — the Hue/Govee
  automation surface beyond the Frame TV integration. The Frame
  post promised this as a follow-up.
- **Magic Monitor showcase** — deferred from first-post slot until
  MM is more settled. Original outline preserved below for when
  it's time:
  - The problem: every WDW family member wants live wait times
    + alerts when their planned rides go down.
  - Serverless architecture: single-table DDB + poller Lambda +
    Next.js SSR on Amplify. ~$0.30/mo joke.
  - The MCP pivot — 22 tools an MCP client can call. Demo: the
    agentic planner answering *"I'm at MK with these 5 rides,
    what should I ride next?"*
  - Cross-session feedback loop — system learns from her sister's
    actual trips. "I will spreadsheet anything" beat.
  - Real-world fixes — the BACK UP cooldown flap bug, the LL
    verification refusal rule, the today-only data caveat.
  - What's next: M6-B, M9 embedded chat, calendar intelligence.
- "The five AWS lessons from deploying Amplify Hosting in CDK"
  (the M2-B post-mortem — content already in `RUNBOOK.md`, just
  needs blog-shaping into Megan-voice)
- "Why I picked MCP over an embedded chat first" (the agentic-
  architecture decision)
- "Sub-agents, but make them deranged" — the sample post title is
  fine as-is, content TBD
- "Pre-aggregated analytics vs streams → Athena: when each is right"
  (the M6 design call, with cost math)
- Anything from `design-handoff/data.js` sample posts that captures
  a real Megan thing (the full-sugar pistachio gelato saga, Roomba
  opinions, etc.) — note: sample data still has stale "keto ice
  cream / erythritol" joke language that was corrected 2026-05-12.
  The real frame is *mostly keto, loud about the ice cream
  exception, ice cream is full-fat and full-sugar.*

---

## Reference docs the agent should read first

1. **`docs/aws-setup-brief.md`** — AWS account, region, profile,
   sibling project conventions, the five M2-B AWS lessons, Python
   3.12 default, Megan's working preferences. Drop-in prompt for
   fresh agents.
2. **`.planning/blog/design-handoff/README.md`** — voice/tone,
   component inventory, page structure, content guidelines.
3. **`.planning/blog/design-handoff/tokens.css`** — copy-paste-able
   design tokens. The canonical color/type/shape values.
4. **`.planning/blog/design-handoff/pages/home.jsx`** +
   **`templates.jsx`** — JSX source of truth for component
   structure. Port to chosen framework.
5. **`.planning/blog/design-handoff/data.js`** — sample post shape
   + voice reference. Don't ship as-is; use as voice anchor.
6. **`infra/lib/disney-stack.ts`** (in this repo, MM) — CDK pattern
   reference for the Amplify + custom domain + GitHub OIDC role.
   Blog stack is simpler (no DDB, no Lambda, no Cognito).
