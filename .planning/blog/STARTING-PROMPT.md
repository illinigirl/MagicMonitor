# Starting prompt for the blog agent

Copy everything below the `---` line and paste it as your first
message to a fresh agent. The agent will then read the three
referenced reference paths (AWS context, project decisions, and
design handoff) and come back with a proposed plan + the two open
decisions before writing any code.

---

I'm starting a new project under the same AWS account as my Magic
Monitor and Watchtower apps: a personal blog called **Megan Builds**
at **blog.megillini.dev**. First post will showcase Magic Monitor;
the broader purpose is a long-form portfolio surface for writing
about engineering decisions in a voice that matches who I actually
am — casual, sarcastic, feminine-as-differentiator, with recurring
jokes about my Roomba, erythritol, and Claude as a coworker.

Before you do anything, read these three reference paths in the
current working directory:

1. **`docs/aws-setup-brief.md`** — AWS account, region, profile
   (`watchtower`), sibling project context (Watchtower owns the
   shared Cognito pool + GitHub OIDC; Magic Monitor is a sibling),
   the five hard-won AWS lessons from deploying Magic Monitor
   (GitHub App install requirement, us-east-1 cert quirk, AWS SDK
   bundling, custom computeRole pitfall, trust-policy override),
   CDK conventions, Python 3.12 default, and my working preferences
   (architect-mode, options-with-tradeoffs, push back on scope
   creep). Read cover to cover.

2. **`.planning/blog/PROJECT.md`** — locked decisions and scope for
   this specific project. Brand is "Megan Builds." Visual direction
   is "Zine, rebalanced" — hot-pink-as-signature on cream paper,
   riso-print aesthetic. Voice is locked. Out-of-scope list is
   explicit (no tags-as-nav, search, RSS, comments, newsletter,
   view counts, OG image gen, sitemap, dark mode, or CMS in v1).
   Includes the Magic Monitor showcase post outline. Note the two
   OPEN decisions at the top — Astro vs Next.js, and v1 page
   scope. I want your recommendation on both as part of the plan
   you propose.

3. **`.planning/blog/design-handoff/`** — complete design package
   from Claude Design. Includes:
   - `README.md` — voice/tone guide, component inventory, page
     structure, content guidelines
   - `tokens.css` — copy-paste-able design tokens (colors, fonts,
     spacing, motion, atom classes like `.chip` / `.card` /
     `.stamp` / `.pullquote` / `.codeblock`)
   - `pages/home.jsx` + `pages/templates.jsx` — JSX source of
     truth for component structure. Port these to whichever
     framework gets picked.
   - `data.js` — sample post + project shape with voice
     reference. DO NOT ship the sample content as-is; use as a
     voice anchor for replacement content I'll write.
   - `preview.html` — design preview reference

   **The design is locked at the visual / component / tokens level.**
   You implement what's in this folder; you don't redesign.

The blog repo will be NEW — separate from this Magic Monitor repo.
GitHub org is `illinigirl`. Create the repo locally first; we'll
push to GitHub once the scaffold builds and deploys cleanly.

**Your first task after reading those three reference paths:**

Come back with:
1. **A recommendation on Astro vs Next.js 16** with the tradeoff
   (the project doc has my lean — Astro — but I want to hear yours
   independent of mine).
2. **A recommendation on v1 page scope** — minimum 3 pages, or the
   design's full set, or a middle ground (my lean is core 4: /,
   /posts, /posts/[slug], /404). Tell me what you'd ship in v1
   vs defer to v1.1.
3. **A concrete first-steps plan** — repo init, dependency install,
   tokens.css integration, MDX wiring, first sample post, dev
   server up. Numbered, ~5-7 steps.
4. **A proposed repo name.** I haven't decided. Options I've
   floated: `megan-builds`, `blog`, `megillini-blog`. I want a
   recommendation with reasoning.

DO NOT start writing code or running shell commands until I sign
off on the plan. I architect, you build — and I want to see the
two big decisions resolved before tooling commits start landing.

Note on my working style (also in the AWS brief):
- I architect; you write the code. Don't ask me to write it
  myself.
- Surface tradeoffs but default to my actual style instead of
  asking the same question every session.
- Push back if I'm scope-creeping. Hold the boundary.
- For destructive AWS ops, confirm before executing.
- I keep sessions running across days — check the system date
  before saying "today did X."
- I have premium tokens; don't tell me to wrap up to save context.

Ready when you are.
