// Direction A2 — Zine, rebalanced
// Same scrapbook DNA. Hot pink is now the SIGNATURE, not the field:
// it owns the masthead, marker accents, tape, and footer — but the
// feature article sits on cream paper with a thick pink rule and a
// pink chip, so the color pops MORE because there's white space to
// pop against. "Now" card downgraded from neon-yellow to pink-on-cream.

function DirZineV2() {
  return (
    <div className="z2">
      <style>{`
        .z2 {
          --paper: #fbf6ef;
          --paper-2: #f3e8d8;
          --ink: #1a0d1f;
          --hot: #ff2d87;
          --hot-soft: #ffe2ee;
          --pop: #ffd23f;
          --mint: #6be3b8;
          --tape: rgba(255,210,63,0.85);
          background: var(--paper);
          color: var(--ink);
          font-family: "Inter", system-ui, sans-serif;
          padding: 0 0 64px;
          position: relative;
          overflow: hidden;
          min-height: 100%;
        }
        .z2 .marker { font-family: "Caveat", "Caprasimo", cursive; }
        .z2 .display { font-family: "Caprasimo", "Caveat", serif; }
        .z2 .mono { font-family: "JetBrains Mono", monospace; }
        .z2 .tape {
          position: absolute;
          background: var(--tape);
          box-shadow: 0 2px 4px rgba(0,0,0,0.08);
          border-left: 1px dashed rgba(0,0,0,0.05);
          border-right: 1px dashed rgba(0,0,0,0.05);
        }
        .z2 .card {
          background: white;
          border-radius: 4px;
          padding: 18px 20px;
          box-shadow: 2px 4px 0 var(--ink);
          position: relative;
        }
        .z2 .card.tilt-l { transform: rotate(-1.2deg); }
        .z2 .card.tilt-r { transform: rotate(0.9deg); }
        .z2 .chip {
          display: inline-block;
          padding: 4px 12px;
          font-size: 11px;
          font-weight: 800;
          letter-spacing: 0.04em;
          text-transform: uppercase;
          background: var(--hot);
          color: white;
          border-radius: 999px;
        }
        .z2 .stamp {
          display: inline-block;
          font-family: "JetBrains Mono", monospace;
          font-weight: 700;
          font-size: 10px;
          letter-spacing: 0.12em;
          padding: 4px 8px;
          border: 1.5px solid var(--ink);
          transform: rotate(-3deg);
          color: var(--ink);
          background: var(--pop);
        }
      `}</style>

      {/* Hot-pink masthead BAND — the signature lives here, contained */}
      <header style={{ background: 'var(--hot)', color: 'white', padding: '20px 40px 24px', position: 'relative' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', fontSize: 11, letterSpacing: '0.15em', textTransform: 'uppercase', opacity: 0.85, marginBottom: 10 }} className="mono">
          <span>Issue #14 · Spring '26 · Field notes</span>
          <span>mostly keto · always building</span>
        </div>
        <h1 className="display" style={{ fontSize: 104, lineHeight: 0.88, margin: 0, letterSpacing: '-0.02em' }}>
          megan builds<span style={{ color: 'var(--pop)' }}>.</span>
        </h1>
        <div className="marker" style={{ fontSize: 24, marginTop: 4, display: 'inline-block', transform: 'rotate(-1.5deg)' }}>~ a scrapbook of agentic chaos ~</div>

        {/* navigation */}
        <nav style={{ position: 'absolute', top: 22, right: 40, display: 'flex', gap: 18, fontFamily: 'Inter, sans-serif', fontSize: 12, letterSpacing: '0.08em', textTransform: 'uppercase', fontWeight: 700 }}>
          <a>Posts</a><a>Projects</a><a>Now</a><a>About</a><a style={{ background: 'var(--pop)', color: 'var(--ink)', padding: '4px 10px', borderRadius: 4 }}>Hire me ↗</a>
        </nav>

        {/* decorative scrawl */}
        <svg style={{ position: 'absolute', bottom: -14, right: 200, width: 110 }} viewBox="0 0 100 30" fill="none" stroke="var(--pop)" strokeWidth="2.5" strokeLinecap="round">
          <path d="M5 18 Q40 -4 80 14" />
          <path d="M70 8 L80 14 L72 22" />
        </svg>
      </header>

      {/* Body sits on cream — hot pink becomes accent */}
      <div style={{ padding: '40px 40px 0' }}>
        <section style={{ display: 'grid', gridTemplateColumns: '1.6fr 1fr', gap: 32 }}>
          {/* Feature — cream paper with pink top-rule and pink accents */}
          <article className="card" style={{ borderTop: '8px solid var(--hot)', padding: '24px 32px 28px', position: 'relative', boxShadow: '3px 6px 0 var(--ink)' }}>
            <div className="tape" style={{ top: -16, left: 60, width: 90, height: 26, transform: 'rotate(-4deg)' }} />
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14 }}>
              <span className="chip">Top of the pile</span>
              <span className="mono" style={{ fontSize: 10, color: 'var(--ink)', opacity: 0.55, letterSpacing: '0.1em' }}>{POSTS[0].tag.toUpperCase()} · {POSTS[0].date} · {POSTS[0].readTime} MIN</span>
            </div>
            <h2 className="display" style={{ fontSize: 60, lineHeight: 1, margin: '4px 0 14px', color: 'var(--ink)' }}>{POSTS[0].title}</h2>
            <p style={{ fontSize: 16, lineHeight: 1.55, color: 'var(--ink)', opacity: 0.85, margin: '0 0 16px', maxWidth: 600 }}>{POSTS[0].excerpt}</p>
            <div className="marker" style={{ fontSize: 22, color: 'var(--hot)' }}>read the whole mess →</div>
          </article>

          {/* Now — pink-on-cream not yellow */}
          <aside className="card tilt-r" style={{ background: 'var(--hot-soft)', boxShadow: '2px 4px 0 var(--hot)' }}>
            <div className="marker" style={{ fontSize: 30, color: 'var(--hot)', lineHeight: 1, marginBottom: 4 }}>right now</div>
            <div className="mono" style={{ fontSize: 10, color: 'var(--ink)', opacity: 0.5, marginBottom: 14, letterSpacing: '0.12em' }}>UPDATED THIS MORNING</div>
            <ul style={{ listStyle: 'none', padding: 0, margin: 0, display: 'flex', flexDirection: 'column', gap: 10 }}>
              {NOW.map((n, i) => (
                <li key={i} style={{ fontSize: 14, lineHeight: 1.4, display: 'flex', gap: 8 }}>
                  <span style={{ color: 'var(--hot)', fontWeight: 800 }}>✶</span>
                  <span>{n}</span>
                </li>
              ))}
            </ul>
          </aside>
        </section>

        {/* Recent — neutral cards, pink details only */}
        <section style={{ marginTop: 52 }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 14, marginBottom: 20, paddingBottom: 10, borderBottom: '3px solid var(--ink)' }}>
            <h3 className="display" style={{ fontSize: 36, margin: 0 }}>Recent dispatches</h3>
            <span className="marker" style={{ fontSize: 22, color: 'var(--hot)', transform: 'rotate(-2deg)', display: 'inline-block' }}>← scroll, friend</span>
            <span className="mono" style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--ink)', opacity: 0.5, letterSpacing: '0.1em' }}>{POSTS.length - 1} ENTRIES</span>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 22 }}>
            {POSTS.slice(1, 7).map((p, i) => (
              <article key={p.id} className={`card ${i % 2 ? 'tilt-l' : 'tilt-r'}`} style={{ position: 'relative', minHeight: 200, borderLeft: '4px solid var(--hot)' }}>
                {i === 0 && <div className="tape" style={{ top: -10, left: 24, width: 60, height: 18, transform: 'rotate(-8deg)' }} />}
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 10 }}>
                  <span className="chip" style={{ background: 'var(--ink)', color: 'var(--pop)', fontSize: 10 }}>{p.tag}</span>
                  <span className="mono" style={{ fontSize: 10, color: 'var(--ink)', opacity: 0.55 }}>{p.date}</span>
                </div>
                <h4 className="display" style={{ fontSize: 22, lineHeight: 1.05, margin: '0 0 10px' }}>{p.title}</h4>
                <p style={{ fontSize: 13, lineHeight: 1.5, color: 'var(--ink)', margin: 0, opacity: 0.78 }}>{p.excerpt}</p>
                <div className="mono" style={{ position: 'absolute', bottom: 16, right: 18, fontSize: 10, color: 'var(--hot)', fontWeight: 700 }}>{p.readTime} MIN →</div>
              </article>
            ))}
          </div>
        </section>

        {/* Bench — mint + cream + one pink card to anchor */}
        <section style={{ marginTop: 56, position: 'relative' }}>
          <h3 className="display" style={{ fontSize: 36, margin: '0 0 6px' }}>What's on the bench</h3>
          <div className="marker" style={{ fontSize: 18, color: 'var(--ink)', opacity: 0.7, marginBottom: 18 }}>private repos, public stories</div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 16 }}>
            {PROJECTS.map((pr, i) => {
              const bg = i === 0 ? 'var(--hot)' : i === 1 ? 'white' : i === 2 ? 'var(--mint)' : 'white';
              const fg = i === 0 ? 'white' : 'var(--ink)';
              return (
                <div key={pr.id} className="card" style={{ background: bg, color: fg, padding: 18, transform: `rotate(${i % 2 ? 1.2 : -0.8}deg)` }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
                    <h4 className="display" style={{ fontSize: 24, margin: 0 }}>{pr.name}</h4>
                    <span className="mono" style={{ fontSize: 9, opacity: 0.7 }}>0{i + 1}</span>
                  </div>
                  <div className="marker" style={{ fontSize: 18, marginTop: 2, marginBottom: 10, color: i === 0 ? 'var(--pop)' : 'var(--hot)' }}>{pr.sub}</div>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginBottom: 10 }}>
                    {pr.stack.map(s => <span key={s} className="mono" style={{ fontSize: 9, padding: '2px 6px', background: i === 0 ? 'rgba(255,255,255,0.2)' : 'var(--ink)', color: i === 0 ? 'white' : 'var(--pop)', borderRadius: 3 }}>{s}</span>)}
                  </div>
                  <p style={{ fontSize: 12, lineHeight: 1.4, margin: 0, opacity: i === 0 ? 0.95 : 0.85 }}>{pr.status}</p>
                </div>
              );
            })}
          </div>
        </section>

        <footer style={{ marginTop: 60, padding: '24px 0 0', borderTop: '3px solid var(--ink)', display: 'flex', justifyContent: 'space-between', alignItems: 'flex-end' }}>
          <div className="marker" style={{ fontSize: 36, color: 'var(--hot)' }}>xo, megan ♡</div>
          <div className="mono" style={{ fontSize: 10, letterSpacing: '0.18em', textTransform: 'uppercase' }}>© 2026 · made between concerts</div>
        </footer>
      </div>
    </div>
  );
}
window.DirZineV2 = DirZineV2;
