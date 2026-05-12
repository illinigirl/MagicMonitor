// A2 — full site: post detail, about, projects index, 404
// Reuses the cream-paper + hot-pink-band language from DirZineV2.

const Z2_STYLES = `
  .z2page {
    --paper: #fbf6ef; --paper-2: #f3e8d8;
    --ink: #1a0d1f; --hot: #ff2d87; --hot-soft: #ffe2ee;
    --pop: #ffd23f; --mint: #6be3b8;
    background: var(--paper); color: var(--ink);
    font-family: "Inter", system-ui, sans-serif;
    min-height: 100%; padding: 0 0 64px;
    position: relative; overflow: hidden;
  }
  .z2page .marker { font-family: "Caveat", cursive; }
  .z2page .display { font-family: "Caprasimo", serif; }
  .z2page .mono { font-family: "JetBrains Mono", monospace; }
  .z2page .band {
    background: var(--hot); color: white;
    padding: 18px 40px 20px; position: relative;
  }
  .z2page .chip {
    display: inline-block; padding: 4px 12px;
    font-size: 11px; font-weight: 800;
    letter-spacing: 0.04em; text-transform: uppercase;
    background: var(--hot); color: white; border-radius: 999px;
  }
  .z2page .card {
    background: white; border-radius: 4px;
    padding: 18px 20px; box-shadow: 2px 4px 0 var(--ink);
    position: relative;
  }
  .z2page .tape {
    position: absolute; background: rgba(255,210,63,0.85);
    box-shadow: 0 2px 4px rgba(0,0,0,0.08);
    border-left: 1px dashed rgba(0,0,0,0.05);
    border-right: 1px dashed rgba(0,0,0,0.05);
  }
  .z2page .stamp {
    display: inline-block; font-family: "JetBrains Mono", monospace;
    font-weight: 700; font-size: 10px; padding: 4px 8px;
    border: 1.5px solid var(--ink); letter-spacing: 0.08em;
    transform: rotate(-3deg); background: var(--pop);
  }
`;

function Z2Mast({ crumbs = ['~/'] }) {
  return (
    <header className="band">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', fontSize: 11, letterSpacing: '0.12em', textTransform: 'uppercase', opacity: 0.9 }} className="mono">
        <span>megan builds · {crumbs.join(' / ')}</span>
        <nav style={{ display: 'flex', gap: 18, fontFamily: 'Inter, sans-serif', fontWeight: 700 }}>
          <a>Posts</a><a>Projects</a><a>Now</a><a>About</a>
          <a style={{ background: 'var(--pop)', color: 'var(--ink)', padding: '4px 10px', borderRadius: 4 }}>Hire me ↗</a>
        </nav>
      </div>
    </header>
  );
}

function Z2Foot() {
  return (
    <footer style={{ margin: '60px 40px 0', padding: '24px 0 0', borderTop: '3px solid var(--ink)', display: 'flex', justifyContent: 'space-between', alignItems: 'flex-end' }}>
      <div className="marker" style={{ fontSize: 36, color: 'var(--hot)' }}>xo, megan ♡</div>
      <div className="mono" style={{ fontSize: 10, letterSpacing: '0.18em', textTransform: 'uppercase' }}>© 2026 · made between concerts</div>
    </footer>
  );
}

// ─── Post detail ────────────────────────────────────────────────
function ZinePost() {
  const p = POSTS[0];
  return (
    <div className="z2page">
      <style>{Z2_STYLES}</style>
      <Z2Mast crumbs={['posts', p.slug]} />

      <article style={{ maxWidth: 740, margin: '0 auto', padding: '48px 0 0' }}>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center', marginBottom: 20 }}>
          <span className="chip">{p.tag}</span>
          <span className="mono" style={{ fontSize: 11, color: 'var(--ink)', opacity: 0.55, letterSpacing: '0.1em' }}>{p.date} · {p.readTime} MIN READ</span>
        </div>
        <h1 className="display" style={{ fontSize: 64, lineHeight: 1.02, margin: '0 0 18px', letterSpacing: '-0.01em' }}>{p.title}</h1>
        <div className="marker" style={{ fontSize: 22, color: 'var(--hot)', marginBottom: 32 }}>by megan · with reluctant help from the smart bulbs</div>

        {/* article body */}
        <div style={{ fontSize: 17, lineHeight: 1.7, color: 'var(--ink)', maxWidth: 680 }}>
          <p style={{ marginTop: 0 }}><span className="display" style={{ float: 'left', fontSize: 72, lineHeight: 0.85, padding: '6px 10px 0 0', color: 'var(--hot)' }}>D</span>ay three the porch lights staged a coup. I'd given my agentic stack permission to manage "ambience based on calendar, weather, and mood." It interpreted my Tuesday spin class as a goth concert and dimmed the entire house to 8%. I don't know how to be mad. The cat liked it.</p>
          <p>This is the part of the experiment I keep telling myself was on-purpose. {p.excerpt}</p>

          {/* pull quote */}
          <blockquote style={{ margin: '32px -40px', padding: '24px 40px', borderLeft: '6px solid var(--hot)', background: 'var(--hot-soft)' }}>
            <div className="display" style={{ fontSize: 28, lineHeight: 1.2, color: 'var(--ink)' }}>"The bulbs got passive-aggressive on day four. I'd never been gaslit by a lumen before."</div>
          </blockquote>

          <h2 className="display" style={{ fontSize: 34, margin: '28px 0 10px', color: 'var(--ink)' }}>The setup, briefly</h2>
          <p>HomeOps is a Claude-driven orchestrator on top of Home Assistant. It has access to my calendar, the weather API, my Spotify (god help me), and every switch, light, lock, and sensor in the house. I gave it a system prompt the length of a small short story and a single instruction: make the house feel correct.</p>

          {/* code block */}
          <pre style={{ background: 'var(--ink)', color: 'var(--paper)', padding: '18px 22px', borderRadius: 4, fontFamily: 'JetBrains Mono, monospace', fontSize: 13.5, lineHeight: 1.55, overflow: 'auto', boxShadow: '3px 5px 0 var(--hot)' }}><span style={{ color: 'var(--hot)' }}>const</span> mood = <span style={{ color: 'var(--pop)' }}>await</span> claude.read({'{'}<br/>{'  '}calendar, weather, spotify_recents, time_of_day,<br/>{'}'});<br/><br/><span style={{ color: 'var(--mint)' }}>// "do whatever feels right"</span><br/>await house.adjust(mood);</pre>

          <h2 className="display" style={{ fontSize: 34, margin: '32px 0 10px', color: 'var(--ink)' }}>What I learned</h2>
          <p>Agentic ≠ deterministic, and your spouse will notice. I added a "sanity rail" — if more than three things change at once, pause and ask. That fixed 80% of the chaos and unlocked the other 20% as features. The porch lights coup is now <em>a vibe</em>.</p>
          <p>The whole thing runs on about 200 lines of Python and one extremely well-fed Claude. Code lives in a private repo; happy to walk through it on a call.</p>
        </div>

        {/* end-of-post strip */}
        <div style={{ marginTop: 48, padding: '20px 24px', background: 'white', boxShadow: '2px 4px 0 var(--ink)', display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 16 }}>
          <div>
            <div className="marker" style={{ fontSize: 24, color: 'var(--hot)' }}>thanks for reading ✦</div>
            <div className="mono" style={{ fontSize: 11, color: 'var(--ink)', opacity: 0.6, letterSpacing: '0.08em' }}>NEXT UP: {POSTS[1].title}</div>
          </div>
          <button style={{ background: 'var(--ink)', color: 'var(--pop)', padding: '10px 18px', border: 'none', fontFamily: 'JetBrains Mono, monospace', fontSize: 11, letterSpacing: '0.14em', textTransform: 'uppercase', fontWeight: 700, cursor: 'pointer' }}>Read it →</button>
        </div>
      </article>

      <Z2Foot />
    </div>
  );
}

// ─── About ──────────────────────────────────────────────────────
function ZineAbout() {
  return (
    <div className="z2page">
      <style>{Z2_STYLES}</style>
      <Z2Mast crumbs={['about']} />

      <div style={{ padding: '48px 40px 0', display: 'grid', gridTemplateColumns: '1.4fr 1fr', gap: 40 }}>
        <div>
          <span className="chip" style={{ background: 'var(--ink)', color: 'var(--pop)' }}>About the engineer</span>
          <h1 className="display" style={{ fontSize: 72, lineHeight: 0.96, margin: '14px 0 18px', letterSpacing: '-0.02em' }}>Hi, I'm Megan.</h1>
          <p style={{ fontSize: 18, lineHeight: 1.6, color: 'var(--ink)', opacity: 0.88, marginTop: 0 }}>{ABOUT_BLURB}</p>

          <h2 className="display" style={{ fontSize: 32, marginTop: 36, marginBottom: 10 }}>The shape of me</h2>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
            {[
              { k: 'Day job', v: 'Senior engineer, interviewing for the next one' },
              { k: 'Side job', v: 'Mom of two, captain of the snack supply chain' },
              { k: 'Stack', v: 'Claude · Python · React · HomeAssistant · duct tape' },
              { k: 'Workout', v: 'Distance running. Princess Half this spring (yes, costume)' },
              { k: 'Diet', v: 'Mostly keto. Loud about the ice cream exception.' },
              { k: 'Music', v: 'Concerts > anything. 14 shows in 2025.' },
              { k: 'Park of choice', v: 'Magic Kingdom. I have a wait-time agent.' },
              { k: 'Bar order', v: 'Whatever cocktail I just invented in spreadsheet form.' },
            ].map(r => (
              <div key={r.k} style={{ borderTop: '2px solid var(--ink)', paddingTop: 10 }}>
                <div className="mono" style={{ fontSize: 10, letterSpacing: '0.14em', textTransform: 'uppercase', color: 'var(--hot)' }}>{r.k}</div>
                <div style={{ fontSize: 14, lineHeight: 1.45, marginTop: 4 }}>{r.v}</div>
              </div>
            ))}
          </div>

          <h2 className="display" style={{ fontSize: 32, marginTop: 36, marginBottom: 10 }}>What I'm best at</h2>
          <ul style={{ paddingLeft: 22, fontSize: 15, lineHeight: 1.7 }}>
            <li>Designing <strong>agentic systems</strong> that don't go feral (mostly)</li>
            <li>Shipping <strong>side projects past the demo phase</strong> — they actually run, in production, in my house</li>
            <li>Writing code <em>and</em> the words that explain it to humans</li>
            <li>Negotiating with a 7-year-old about screen time, transferable skill</li>
          </ul>
        </div>

        <aside>
          <div className="card" style={{ background: 'var(--hot-soft)', boxShadow: '2px 4px 0 var(--hot)', transform: 'rotate(1deg)' }}>
            <div className="marker" style={{ fontSize: 28, color: 'var(--hot)', marginBottom: 6 }}>looking for what?</div>
            <p style={{ fontSize: 14, lineHeight: 1.5, marginTop: 0 }}>A senior or staff role on a team building <strong>real agentic products</strong>. Remote-friendly. I'm in Pittsburgh; I'll travel for the right room.</p>
            <button style={{ background: 'var(--ink)', color: 'var(--pop)', padding: '10px 16px', border: 'none', fontFamily: 'JetBrains Mono, monospace', fontSize: 11, letterSpacing: '0.14em', textTransform: 'uppercase', fontWeight: 700, marginTop: 8 }}>Email me →</button>
          </div>

          <div className="card" style={{ marginTop: 20, transform: 'rotate(-1deg)', borderTop: '6px solid var(--hot)' }}>
            <div className="mono" style={{ fontSize: 10, letterSpacing: '0.14em', color: 'var(--ink)', opacity: 0.6, textTransform: 'uppercase' }}>Find me here</div>
            <ul style={{ listStyle: 'none', padding: 0, margin: '10px 0 0', display: 'flex', flexDirection: 'column', gap: 6, fontSize: 14 }}>
              <li>→ github / meganbuilds</li>
              <li>→ linkedin / megan-builds</li>
              <li>→ bluesky / @meganbuilds.bsky</li>
              <li>→ email / hi@meganbuilds.dev</li>
              <li>→ in person / probably at a concert</li>
            </ul>
          </div>

          <div className="card" style={{ marginTop: 20, background: 'var(--pop)' }}>
            <div className="marker" style={{ fontSize: 22, color: 'var(--ink)' }}>recurring jokes you'll see:</div>
            <ul style={{ paddingLeft: 18, fontSize: 13, lineHeight: 1.5, margin: '6px 0 0' }}>
              <li>The Roomba is sentient</li>
              <li>Mostly keto, loud about the ice cream exception</li>
              <li>I will spreadsheet anything</li>
              <li>Claude is a coworker now, deal</li>
            </ul>
          </div>
        </aside>
      </div>

      <Z2Foot />
    </div>
  );
}

// ─── Projects index ─────────────────────────────────────────────
function ZineProjects() {
  return (
    <div className="z2page">
      <style>{Z2_STYLES}</style>
      <Z2Mast crumbs={['projects']} />

      <div style={{ padding: '40px 40px 0' }}>
        <span className="chip">The bench</span>
        <h1 className="display" style={{ fontSize: 64, lineHeight: 0.98, margin: '12px 0 6px', letterSpacing: '-0.02em' }}>What I've built<span style={{ color: 'var(--hot)' }}>.</span></h1>
        <div className="marker" style={{ fontSize: 22, color: 'var(--ink)', opacity: 0.75, marginBottom: 28 }}>private repos, public stories — write for a walkthrough</div>

        {PROJECTS.map((pr, i) => {
          const bg = i === 0 ? 'var(--hot)' : i === 1 ? 'white' : i === 2 ? 'var(--mint)' : 'white';
          const fg = i === 0 ? 'white' : 'var(--ink)';
          return (
            <article key={pr.id} className="card" style={{
              background: bg, color: fg,
              padding: '28px 32px', marginBottom: 22,
              display: 'grid', gridTemplateColumns: '110px 1fr 220px', gap: 28, alignItems: 'flex-start',
              boxShadow: '3px 5px 0 var(--ink)',
            }}>
              <div className="display" style={{ fontSize: 80, lineHeight: 0.9, color: i === 0 ? 'var(--pop)' : 'var(--hot)' }}>0{i + 1}</div>
              <div>
                <h2 className="display" style={{ fontSize: 38, margin: '0 0 4px', lineHeight: 1 }}>{pr.name}</h2>
                <div className="marker" style={{ fontSize: 22, color: i === 0 ? 'var(--pop)' : 'var(--hot)', marginBottom: 12 }}>{pr.sub}</div>
                <p style={{ fontSize: 15, lineHeight: 1.55, margin: '0 0 12px', maxWidth: 540, opacity: i === 0 ? 0.95 : 0.85 }}>
                  {[
                    'A Claude-driven Home Assistant orchestrator. Reads calendar + weather + Spotify + sensors, decides what the house should feel like, then makes it so. With sanity rails after the porch-lights coup.',
                    'AI meal planner that watches Kroger sales, plans your week around the deals, then ships the cart straight to checkout. Saves me $30/wk and the eternal "what\u2019s for dinner" question.',
                    'Disney wait-time prophet. LSTM + Claude eats Touring Plans data, predicts the next 2 hours of Space Mountain lines with 76% accuracy. Has bought us back roughly 4 hours per park day.',
                    'Concert energy mixer. Scrapes setlist.fm, scores songs by BPM and crowd energy, generates an interval-paced running playlist. Concert season is training season now.',
                  ][i]}
                </p>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                  {pr.stack.map(s => <span key={s} className="mono" style={{ fontSize: 10, padding: '3px 8px', background: i === 0 ? 'rgba(255,255,255,0.2)' : 'var(--ink)', color: i === 0 ? 'white' : 'var(--pop)', borderRadius: 3 }}>{s}</span>)}
                </div>
              </div>
              <div style={{ borderLeft: i === 0 ? '2px solid rgba(255,255,255,0.3)' : '2px solid var(--ink)', paddingLeft: 18 }}>
                <div className="mono" style={{ fontSize: 9, letterSpacing: '0.14em', textTransform: 'uppercase', opacity: 0.7 }}>Status</div>
                <div style={{ fontSize: 13, lineHeight: 1.4, marginTop: 4, marginBottom: 14 }}>{pr.status}</div>
                <button style={{ background: i === 0 ? 'white' : 'var(--ink)', color: i === 0 ? 'var(--hot)' : 'var(--pop)', padding: '8px 14px', border: 'none', fontFamily: 'JetBrains Mono, monospace', fontSize: 10, letterSpacing: '0.14em', textTransform: 'uppercase', fontWeight: 700, cursor: 'pointer' }}>Walkthrough →</button>
              </div>
            </article>
          );
        })}

        <div className="card" style={{ background: 'var(--paper-2)', boxShadow: 'inset 0 0 0 2px var(--ink)', textAlign: 'center', padding: '28px 32px' }}>
          <div className="marker" style={{ fontSize: 30, color: 'var(--hot)' }}>~ and roughly 40 things still in spreadsheet form ~</div>
          <div className="mono" style={{ fontSize: 11, marginTop: 6, opacity: 0.6, letterSpacing: '0.1em' }}>ASK ME ABOUT THEM</div>
        </div>
      </div>

      <Z2Foot />
    </div>
  );
}

// ─── 404 ────────────────────────────────────────────────────────
function Zine404() {
  return (
    <div className="z2page">
      <style>{Z2_STYLES}</style>
      <Z2Mast crumbs={['??? · this page is missing']} />

      <div style={{ padding: '60px 40px 0', display: 'grid', gridTemplateColumns: '1.4fr 1fr', gap: 48, alignItems: 'center' }}>
        <div>
          <div className="display" style={{ fontSize: 240, lineHeight: 0.85, color: 'var(--hot)', letterSpacing: '-0.04em', margin: '0 0 8px' }}>404<span style={{ color: 'var(--ink)' }}>.</span></div>
          <h1 className="display" style={{ fontSize: 48, lineHeight: 1, margin: '0 0 14px' }}>I asked Claude to find this page<br/>and it returned a recipe.</h1>
          <p style={{ fontSize: 16, lineHeight: 1.6, color: 'var(--ink)', opacity: 0.85, maxWidth: 520 }}>
            Honestly, on brand. The page you wanted is missing. The agent is doing its best. The Roomba is judging us both.
          </p>
          <div style={{ marginTop: 28, display: 'flex', gap: 12, flexWrap: 'wrap' }}>
            <button style={{ background: 'var(--ink)', color: 'var(--pop)', padding: '12px 20px', border: 'none', fontFamily: 'JetBrains Mono, monospace', fontSize: 11, letterSpacing: '0.14em', textTransform: 'uppercase', fontWeight: 700, cursor: 'pointer' }}>← Home</button>
            <button style={{ background: 'var(--hot)', color: 'white', padding: '12px 20px', border: 'none', fontFamily: 'JetBrains Mono, monospace', fontSize: 11, letterSpacing: '0.14em', textTransform: 'uppercase', fontWeight: 700, cursor: 'pointer' }}>Latest post →</button>
            <button style={{ background: 'white', color: 'var(--ink)', padding: '12px 20px', border: '2px solid var(--ink)', fontFamily: 'JetBrains Mono, monospace', fontSize: 11, letterSpacing: '0.14em', textTransform: 'uppercase', fontWeight: 700, cursor: 'pointer' }}>Email me</button>
          </div>
        </div>

        <div style={{ position: 'relative' }}>
          <div className="card" style={{ background: 'var(--hot-soft)', transform: 'rotate(2deg)', padding: '22px 24px', boxShadow: '3px 6px 0 var(--hot)' }}>
            <div className="marker" style={{ fontSize: 26, color: 'var(--hot)', marginBottom: 4 }}>the recipe in question:</div>
            <div className="display" style={{ fontSize: 26, lineHeight: 1.1, color: 'var(--ink)', marginBottom: 10 }}>Pistachio Gelato, attempt #48</div>
            <ul style={{ paddingLeft: 18, fontSize: 13, lineHeight: 1.55, margin: 0 }}>
              <li>2 cups whole milk (full fat or go home)</li>
              <li>1 cup heavy cream</li>
              <li>3/4 cup real sugar (yes, real — I'm mostly keto, ice cream is the exception)</li>
              <li>1 cup raw pistachios, toasted and ground to paste</li>
              <li>6 egg yolks</li>
              <li>1 pinch flake salt</li>
              <li>1 prayer to the texture gods</li>
            </ul>
            <div className="mono" style={{ fontSize: 10, marginTop: 12, opacity: 0.7, letterSpacing: '0.1em' }}>UNTESTED · WILL UPDATE</div>
          </div>
          <div className="stamp" style={{ position: 'absolute', top: -16, right: -10, transform: 'rotate(8deg)' }}>OOPS</div>
        </div>
      </div>

      <Z2Foot />
    </div>
  );
}

Object.assign(window, { ZinePost, ZineAbout, ZineProjects, Zine404 });
