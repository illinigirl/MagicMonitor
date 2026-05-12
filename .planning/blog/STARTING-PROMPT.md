# Starting prompt for the blog agent

Copy everything below the `---` line and paste it as your first
message to a fresh agent. The agent will then read the two
referenced files (`docs/aws-setup-brief.md` and
`.planning/blog/PROJECT.md`) and ask 1-2 clarifying questions
before starting.

---

I'm starting a new project under the same AWS account as my Magic
Monitor and Watchtower apps: a personal blog at
**blog.megillini.dev**. First post will showcase Magic Monitor; the
broader purpose is a long-form portfolio surface for writing about
engineering decisions.

Before you do anything, read these two files in the current working
directory:

1. **`docs/aws-setup-brief.md`** — covers my AWS account, region,
   profile (`watchtower`), sibling project context (Watchtower owns
   the shared Cognito pool + GitHub OIDC; Magic Monitor is a
   sibling), the five hard-won AWS lessons from deploying Magic
   Monitor (GitHub App install requirement, us-east-1 cert quirk,
   AWS SDK bundling, custom computeRole pitfall, trust-policy
   override), CDK conventions, Python 3.12 default, and my working
   preferences (architect-mode, options-with-tradeoffs, push back
   on scope creep, etc.). Read it cover to cover — it's ~300 lines
   and saves us re-discovering shared infrastructure.

2. **`.planning/blog/PROJECT.md`** — the locked stack decisions and
   scope for this specific project. Stack is Next.js 16 App Router
   + Tailwind 4 + MDX + static export, deployed via Amplify
   Hosting. Subdomain is blog.megillini.dev (NOT the apex).
   Authentication is none. Includes an explicit out-of-scope list
   (no tags, search, RSS, comments, newsletter, view counts, OG
   image generation, sitemap, dark mode, or CMS in v1) — please
   honor it. The first post is a Magic Monitor showcase (rough
   outline at the bottom of that file).

The blog repo will be NEW — separate from this Magic Monitor repo.
GitHub org is `illinigirl`. Create the repo locally first; we'll
push to GitHub once the scaffold builds and deploys cleanly.

**Your first task after reading those two files:** propose a
directory layout and the first concrete steps (e.g., "1. `pnpm
create next-app blog --typescript --tailwind --app`, 2. add MDX
plugin, 3. set up content/blog/, 4. configure static export...").
Don't start writing code yet — let me sign off on the plan first,
since I tend to like 2-3 options with tradeoffs presented before
committing.

A few open questions you may want to confirm with me before
starting (or propose defaults for me to approve):

1. **Repo name** — `megillini-blog`? `blog`? `megillini.dev`?
   I haven't decided.
2. **Color palette** — the project doc says "calmer than MM's
   castle pink, readability-first for long-form text." Propose 2-3
   options with a recommendation. I want visual cohesion with MM
   (same typography) but not visual identity overlap.
3. **Code highlighting theme** — `github-dark-default` is the
   default I locked in the project doc, but if you have a strong
   opinion on something else, say so.
4. **Markdown features** — beyond standard MDX, do we want
   anything fancy like callout boxes (info / warning / note),
   linked headings (anchor on hover), or table-of-contents
   sidebars on long posts? My lean is: keep it minimal in v1, add
   when a specific post needs it. But if you'd default to including
   one of these now, propose with a tradeoff.

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
