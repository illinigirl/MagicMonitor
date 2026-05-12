# Megan Builds — Design Handoff

A personal blog for a sarcastic, feminine, agentic-coding-obsessed engineer who is currently interviewing. Used to showcase private side-projects without making them public.

**Direction:** A2 — Zine, rebalanced. Hot pink lives in the masthead band and as a recurring accent (chips, rules, marker scrawls). The body breathes on cream paper so the writing leads and the pink reads as *signature*, not noise.

---

## Voice & tone

- **Casual, funny, sarcastic** — like talking to a friend who happens to ship things.
- **Confident but self-deprecating** — "I let Claude run my house and lived to tell about it," not "Exploring agentic orchestration patterns."
- **Specific over generic** — "the gelato is real cream, real sugar, and I will die on that hill" beats "I make ice cream."
- **Femininity is a differentiator, not a footnote** — the hot pink is the point. Sundress at standup energy.
- **Recurring jokes** to weave in: the Roomba is sentient, mostly keto but loud about the ice cream exception (real cream, real sugar — no artificial sweeteners, ever), Claude is a coworker now, "I will spreadsheet anything."

Avoid: corporate speak, "passionate about," "leveraging," self-deprecation that reads as actually insecure.

---

## Design tokens (`tokens.css`)

```css
:root {
  /* Color */
  --paper:      #fbf6ef;   /* main background */
  --paper-2:    #f3e8d8;   /* sectioned/secondary surfaces */
  --ink:        #1a0d1f;   /* primary text + structural lines */
  --hot:        #ff2d87;   /* signature hot pink — masthead, accents */
  --hot-soft:   #ffe2ee;   /* tinted pink for callout cards */
  --pop:        #ffd23f;   /* canary yellow — secondary pop, tape */
  --mint:       #6be3b8;   /* tertiary accent for cards */

  /* Type */
  --font-display: "Caprasimo", serif;        /* display / titles */
  --font-marker:  "Caveat", cursive;         /* handwritten accent */
  --font-body:    "Inter", system-ui, sans-serif;
  --font-mono:    "JetBrains Mono", monospace;

  /* Shape */
  --radius:     4px;
  --shadow:     2px 4px 0 var(--ink);        /* the signature hard shadow */
  --shadow-lg:  3px 6px 0 var(--ink);
  --rule:       3px solid var(--ink);
}
```

Load fonts:

```html
<link href="https://fonts.googleapis.com/css2?family=Caprasimo&family=Caveat:wght@400;700&family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
```

---

## Component inventory

| Component | Use |
|---|---|
| `<Masthead>` | Hot-pink top band. Logo, issue meta, primary nav, "Hire me" CTA in pop yellow. **Used on every page.** |
| `<Footer>` | Cream, with marker-script "xo, megan" and mono copyright row. **Every page.** |
| `<Chip>` | Hot-pink pill, white text, uppercase. For tags and post categories. Inverse variant: ink bg + pop fg. |
| `<Card>` | White card with hard offset shadow (`2px 4px 0 ink`). Often slightly rotated (-1.2° or +0.9°). |
| `<Stamp>` | Mono text in a tilted bordered box on pop-yellow ground. For status flags ("OOPS", "MOSTLY KETO"). |
| `<Tape>` | Pop-yellow rectangle, slight rotation, positioned absolutely over cards. Decorative only. |
| `<Marker text>` | Inline Caveat script in hot pink. Subheads, doodled callouts, signature flourishes. |
| `<PullQuote>` | Hot-soft background, 6px hot-pink left rule, Caprasimo display text. |
| `<CodeBlock>` | Ink background, paper-cream text, hot-pink keywords, pop-yellow modifiers, mint-green comments. Casts a hot-pink hard shadow. |
| `<ProjectRow>` | 3-col grid: huge numeral + title/body + status sidebar. First row uses hot-pink fill; others alternate white/mint. |
| `<PostCard>` | Card with hot-pink left-rule, chip on top, mono date right, display title, body excerpt, mono "X MIN →" in bottom-right corner. |

### Rotation rules

Cards in grids alternate ±1° rotation for hand-pasted feel. **Never rotate more than 2°** — readability dies past that. The masthead, footer, and any card containing a primary CTA stay at 0°.

### Shadow rule

The hard offset shadow (`2px 4px 0 ink`) is the structural signature — like a riso print. Don't replace with soft drop shadows. Hover state: shadow grows to `3px 6px 0 ink` and card translates -1px / -2px.

---

## Page structure

```
/                    → homepage (latest post hero + recent dispatches + bench preview + now)
/posts               → archive (all dispatches, filterable by tag)
/posts/[slug]        → reading view (drop cap, pull quotes, code blocks, related)
/projects            → projects index (4 detailed project rows)
/projects/[slug]     → individual project case study (optional, post format is fine for most)
/about               → bio + "shape of me" grid + how to reach + recurring jokes card
/now                 → could be a card on homepage, or its own page
/404                 → "I asked Claude to find this page and it returned a recipe"
```

### Homepage anatomy (top to bottom)

1. **Masthead band** (hot pink, full bleed)
2. **Hero feature** — latest post on cream card with top hot-pink rule, plus "Now" sidebar on hot-soft tilted card
3. **Recent dispatches** — 3-col grid of post cards with hot-pink left-rule
4. **The bench** — projects preview (4 cards, one filled hot pink as anchor)
5. **Footer**

### Post detail anatomy

- Masthead with breadcrumb (`posts / [slug]`)
- 740px max-width article column on cream
- Chip + mono meta line above title
- Display title (~64px), marker-script byline in hot pink
- Body in 17px Inter, 1.7 line height, drop cap on first paragraph in Caprasimo+hot
- Pull quotes break out of column to ±40px margin
- Code blocks with the syntax color palette above
- End-of-post strip with "next up" + CTA

---

## Content guidelines

Sample posts and projects live in `data.js`. Replace with real content but keep:

- **Titles are full sentences with a joke** — "Sub-agents, but make them deranged" not "Multi-agent architecture patterns"
- **Excerpts deliver a punchline, not a summary** — set up a moment, drop a line
- **Tags are domain (Home Automation, Agentic Coding, Side Quest) — keep ~6 of them, no more**
- **Read times are honest** — short posts (3–5 min) should be marked as such; the variety is part of the texture

---

## What lives where (this handoff package)

```
megan-builds-handoff/
├── README.md              ← you are here
├── tokens.css             ← copy/paste design tokens
├── data.js                ← sample posts + projects
├── pages/
│   ├── home.jsx           ← homepage component (A2)
│   ├── post.jsx           ← post detail template
│   ├── about.jsx          ← about page
│   ├── projects.jsx       ← projects index
│   └── 404.jsx            ← the recipe gag
└── lib/
    └── components.jsx     ← Masthead, Footer, Chip, Card, Stamp, etc.
```

The full interactive prototype lives one folder up at `megan-builds/MeganBuilds.html` — pan/zoom canvas with all directions and the chosen A2 full site. Treat that as the visual source of truth.

---

## Notes for the build

- **Next.js or Astro.** Astro is probably the right call for a blog of this size — content in MDX, ship fast, no JS for static pages.
- **MDX for posts** so code blocks and pull quotes can be authored inline.
- **Images:** if/when added, treat them like polaroids — white border, 0.5° rotation, hard offset shadow. Don't let photography break the riso-print language.
- **Dark mode:** not designed. The cream paper *is* the brand. If you must add it later, invert to ink background with cream text and keep hot pink + pop yellow unchanged.
- **Print stylesheet:** worth adding. Megan reads things on paper.

— end of handoff —
