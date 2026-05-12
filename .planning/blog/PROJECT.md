# Blog at megillini.dev — project queue

Personal blog at `blog.megillini.dev`. First post showcases Magic
Monitor; subsequent posts cover engineering decisions, project
post-mortems, and whatever Megan finds worth writing about.

Status: **queued** (not yet started — handed off to a fresh agent
when ready to begin).

---

## Why this exists

- Adds a "writes about engineering choices" surface to the portfolio
  alongside the MM agentic demo and the Watchtower sibling project.
- Gives the Magic Monitor project a long-form narrative home — the
  README is portfolio-grade but doesn't capture the *decision*
  story (why MCP suite over embedded chat first, why pre-aggregated
  analytics over Athena, etc.).
- Forcing function for writing things up — turns conversation-with-Claude
  iteration into shareable artifacts.

---

## Stack decisions (locked)

| | Choice | Why |
|---|---|---|
| Framework | Next.js 16 App Router + Turbopack | Same family as MM; Megan already fluent with the stack |
| Rendering | **Static export (SSG)** | Content is build-time. No DDB, no auth, no SSR Lambda cost. Edge-cached out of the box on Amplify Hosting. |
| Styling | Tailwind 4 + `@tailwindcss/typography` | MM uses Tailwind 4; reuse the same primitives |
| Content | MDX via `@next/mdx` | Standard pattern. MDX lets posts embed React components when needed (callouts, custom code blocks) without becoming a full CMS |
| Code highlighting | `rehype-pretty-code` with `github-dark-default` | Battle-tested, no runtime cost, looks good with both light + dark themes |
| Typography | Fraunces (headings) + Inter (body) + JetBrains Mono (code) via `next/font/google` | **Shared visual lineage with MM** — reads as a family. Important for portfolio cohesion. |
| Palette | Calmer than MM's castle pink — readability-first for long-form text | MM is on-brand for Disney; the blog needs to read calmly across many posts. Pick a neutral palette with one accent color. |
| Deploy | AWS Amplify Hosting, us-east-2 | Same as MM. Let Amplify auto-issue the cert (do NOT pass `customCertificate`). |
| Domain | `blog.megillini.dev` | Subdomain, simpler DNS than apex |
| IaC | AWS CDK (TypeScript), single stack `BlogStack` | Same pattern as MM's `DisneyStack` |
| GitHub | New repo under `illinigirl` org | Megan's GitHub org |
| Auth | None (public read-only site) | Personal blog; no signup, no comments, no analytics in v1 |

---

## Page structure (v1 scope)

- `/` — Landing page: short intro paragraph + reverse-chronological
  list of posts (title + date + summary).
- `/blog` — Same list, slightly more detail per entry.
- `/blog/[slug]` — Full post.

That's it. No other pages in v1.

---

## Content model

Posts live in `content/blog/<slug>.mdx`. Frontmatter:

```yaml
---
title: "Post title"
date: "2026-MM-DD"
summary: "1-2 sentence summary for the index page"
---
```

Body is MDX — can include code blocks, headings, lists, links, and
embedded React components if Megan wires them up later.

Build-time resolution: at `next build`, the framework reads
`content/blog/*.mdx`, generates static pages for each, and prerenders
the index.

---

## Out of scope for v1 (resist scope creep)

The agent will be tempted to add these. Don't.

- **Tags / categories.** Add when there are 10+ posts and natural
  clusters emerge. Not before.
- **Search.** Static search needs a build-time index + client-side
  fuzzy matcher (FlexSearch, Pagefind). Worth it later; not v1.
- **RSS feed.** Real value but adds a build step. Add when there
  are subscribers asking for it.
- **Comments.** Either you self-host (Isso, Commento) or use a
  third party (Disqus, Giscus). Both pull focus from writing.
- **Newsletter signup.** Requires a third-party service (Buttondown,
  Substack). Add when the post backlog justifies it.
- **View counts.** Privacy-respecting analytics (Plausible, Fathom)
  cost money and add a third-party dep. Public blogs work fine
  without them.
- **OG image generation.** Per-post social-share cards via
  `@vercel/og` or similar. Nice to have for shared posts; not
  blocking v1.
- **Sitemap.** Trivial to add later when SEO matters; not v1.
- **Dark mode toggle.** Pick a single readable palette in v1.
  Theme-switching is a real engineering surface (CSS variables +
  user pref storage); not free.
- **CMS / admin UI.** Content is git; that's the feature. Headless
  CMS layers are an anti-pattern for a personal blog.

---

## First post: Magic Monitor showcase

The first post is the reason this project exists right now.

**Working title:** TBD (Megan picks). Candidates:
- "Building an Agentic Disney Trip Planner with MCP and Claude"
- "Magic Monitor: A WDW Dashboard That Learned to Plan Rides"
- "From Pi-fed snapshot to agentic loop: 30 days of building Magic Monitor"

**What it should cover (rough outline — Megan refines):**
1. The problem: every WDW family member wants live wait times +
   alerts when their planned rides go down.
2. The architecture choice: serverless single-table DynamoDB + a
   poller Lambda + Next.js SSR on Amplify. Cost: ~$0.30/mo.
3. The MCP pivot: turning MM's read model into 22 tools an MCP
   client can call conversationally. Demo: agentic trip-planner
   answering "I'm at MK with these 5 rides, what should I ride
   next?" using one `get_planning_context` call.
4. The cross-session feedback loop: `record_plan` /
   `mark_ride_complete` / `get_user_plan_history` with server-side
   calibration. The data plane does the math; the LLM narrates
   the lesson.
5. Real-world fixes: the BACK UP cooldown flap bug, the LL
   verification refusal rule, the today-only data caveat —
   examples of how usage shaped the system.
6. What's next: M6-B (live AWS data plane), M9 (embedded chat),
   M8 (calendar intelligence with party-day cohort filtering).

Target length: 1500-2500 words. Long enough to capture the
decisions, short enough to read in one sitting. Code snippets +
maybe 2-3 screenshots from the MM screenshot brief.

---

## Future post backlog (for after the blog ships)

- "The five AWS lessons from deploying Amplify Hosting in CDK"
  (the M2-B post-mortem — content already exists in RUNBOOK.md,
  just needs blog-shaping)
- "Why I picked MCP over an embedded chat first" (the agentic-
  architecture decision)
- "Pre-aggregated analytics vs streams→Athena: when each is right"
  (the M6 design call, with cost math)
- The Watchtower project itself — assuming Megan wants to write
  about its origin too

---

## Reference docs the agent should read first

- `docs/aws-setup-brief.md` — covers AWS account, region, profile,
  sibling project conventions, the five M2-B AWS lessons, Python
  3.12 default, Megan's working preferences. **Drop-in prompt for
  fresh agents — paste it before anything else.**
- `infra/lib/disney-stack.ts` — canonical reference for the CDK
  pattern (Amplify Hosting + custom domain + Cognito + GitHub
  OIDC role). The blog stack is simpler (no DDB, no Lambda, no
  Cognito), but the Amplify + domain pattern is identical.
- `web/next.config.ts` and `web/package.json` — reference for the
  Next.js 16 + Tailwind 4 + Turbopack baseline. Bump to MDX +
  static export for the blog.
- Magic Monitor's `README.md` and `PROJECT.md` — reference for
  tone and structure of project documentation if the agent ends
  up writing similar docs for the blog repo.
