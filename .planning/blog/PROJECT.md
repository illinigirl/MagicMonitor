# Blog at megillini.dev — project queue

**Brand:** Megan Builds. Personal blog at `blog.megillini.dev`. First
post showcases Magic Monitor; broader purpose is a long-form portfolio
surface for writing about engineering decisions in a voice that
matches who Megan actually is.

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
- **Specific over generic** — *"erythritol crystallization is a
  personal enemy"* beats *"I tried different sweeteners."*
- **Femininity is a differentiator** — the hot pink is the point.
  Sundress at standup energy.
- **Recurring jokes** worth weaving in: the Roomba is sentient,
  erythritol is the enemy, Claude is a coworker now, *"I will
  spreadsheet anything."*
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
| Domain | `blog.megillini.dev` (subdomain, not apex) | Simpler DNS, no apex-record gymnastics |
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

## First post: Magic Monitor showcase

**Working title (in Megan's voice — sample, she'll tune):**
*"I built a Disney wait-time predictor instead of doing my actual
job — and then taught Claude to plan rides for my sister"*

Note `design-handoff/data.js` already has a sample
`park-whisperer` post entry with similar voice — use that as the
tone reference.

**What it should cover (rough outline):**
1. The problem: every WDW family member wants live wait times +
   alerts when their planned rides go down.
2. The architecture choice: serverless single-table DynamoDB + a
   poller Lambda + Next.js SSR on Amplify. Cost: ~$0.30/mo. Joke
   about how this is somehow LESS than her phone bill.
3. The MCP pivot: turning MM's read model into 22 tools an MCP
   client can call conversationally. Demo: the agentic planner
   answering *"I'm at MK with these 5 rides, what should I ride
   next?"* — show a screenshot of the actual exchange.
4. The cross-session feedback loop: the system learns from her
   sister's actual trips. Joke about how she'll spreadsheet
   anything, including her family's ride preferences.
5. Real-world fixes — the BACK UP cooldown flap bug shipped while
   testing, the LL verification refusal rule, the today-only data
   caveat. Stories of how usage shaped the system.
6. What's next: M6-B (live AWS data plane), M9 (embedded chat for
   non-Claude-Desktop users like her sister), the calendar
   intelligence build.

Target length: 1500-2500 words. Code snippets + 2-3 MM screenshots.

---

## Future post backlog

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
  a real Megan thing (keto ice cream, Roomba opinions, etc.)

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
