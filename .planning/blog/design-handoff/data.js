// Shared content across all four design directions.

const POSTS = [
  {
    id: 'p1',
    title: 'I let Claude run my house for a week and somehow we\u2019re both still here',
    slug: 'claude-runs-the-house',
    date: 'Apr 28, 2026',
    readTime: 11,
    tag: 'Home Automation',
    excerpt: 'Day 3 the porch lights staged a coup. Day 5 my Roomba developed opinions about my running schedule. A field report from inside the loop.',
    hue: 332,
    kind: 'post',
  },
  {
    id: 'p2',
    title: 'Sub-agents, but make them deranged',
    slug: 'deranged-subagents',
    date: 'Apr 19, 2026',
    readTime: 7,
    tag: 'Agentic Coding',
    excerpt: 'In which I assign five Claudes to argue about my grocery list and accidentally invent peer review for dinner.',
    hue: 280,
    kind: 'post',
  },
  {
    id: 'p3',
    title: 'Pistachio gelato attempt #47: we have ascended',
    slug: 'pistachio-gelato-47',
    date: 'Apr 11, 2026',
    readTime: 5,
    tag: 'Life / Food',
    excerpt: 'Forty-six failures. One spreadsheet. I am mostly keto and this gelato is real cream, real sugar, and I will die on that hill. This batch did not break me.',
    hue: 12,
    kind: 'note',
  },
  {
    id: 'p4',
    title: 'I built a Disney wait-time predictor instead of doing my actual job',
    slug: 'park-whisperer',
    date: 'Mar 30, 2026',
    readTime: 9,
    tag: 'Side Quest',
    excerpt: 'Forecasting Space Mountain lines with a Claude-driven LSTM and the kind of confidence only a sleep-deprived parent can summon.',
    hue: 200,
    kind: 'project',
  },
  {
    id: 'p5',
    title: 'My Roomba has trust issues',
    slug: 'roomba-trust-issues',
    date: 'Mar 22, 2026',
    readTime: 3,
    tag: 'Home Automation',
    excerpt: 'It now sends me Slack messages before it starts cleaning. I am not the one who needs therapy here.',
    hue: 48,
    kind: 'note',
  },
  {
    id: 'p6',
    title: 'Setlist \u2192 Sweat: turning concert nights into intervals',
    slug: 'setlist-to-sweat',
    date: 'Mar 14, 2026',
    readTime: 8,
    tag: 'Project',
    excerpt: 'Scrape setlist.fm, score each song by BPM and crowd energy, hand the playlist to my running app. Concert season is training season now.',
    hue: 168,
    kind: 'project',
  },
  {
    id: 'p7',
    title: 'A cocktail spreadsheet, slowly evolving into an app',
    slug: 'cocktail-spreadsheet',
    date: 'Mar 02, 2026',
    readTime: 6,
    tag: 'Project',
    excerpt: 'I have invented 38 cocktails. Eleven of them are good. Three are dangerous. Claude is now my taste-tester, which feels morally complicated.',
    hue: 322,
    kind: 'project',
  },
  {
    id: 'p8',
    title: 'Notes from a feminine engineer in a beige industry',
    slug: 'beige-industry',
    date: 'Feb 18, 2026',
    readTime: 12,
    tag: 'Career',
    excerpt: 'I show up to standups in a sundress. My pull requests still merge. Some thoughts on taking up space without dimming the sparkle.',
    hue: 348,
    kind: 'post',
  },
];

const PROJECTS = [
  { id: 'pr1', name: 'HomeOps',        sub: 'Agentic Home Assistant',   stack: ['Claude', 'HA', 'Python'],    status: 'In production at my house, mostly compliant' },
  { id: 'pr2', name: 'Sous',           sub: 'Meal plan + Kroger cart',  stack: ['Claude', 'React', 'Kroger API'], status: 'Beta. Saves me $30/wk on groceries' },
  { id: 'pr3', name: 'Park Whisperer', sub: 'Disney line prophet',      stack: ['LSTM', 'Claude', 'Touring Plans'], status: 'Personal use \u2014 76% accurate, 100% smug' },
  { id: 'pr4', name: 'Setlist Sweat',  sub: 'Concert-to-workout mixer', stack: ['Setlist.fm', 'Spotify', 'Claude'], status: 'Used at 6 shows this year' },
];

const NOW = [
  'Building an agent that reads my Costco receipts to track real food cost',
  'Training for the Disney Princess Half (yes, in costume)',
  'Working on attempt #48 of full-sugar pistachio gelato',
  'Going to four concerts in May. Yes I have a spreadsheet.',
  'Interviewing. Hi, if you\u2019re reading this for that reason \ud83d\udc4b',
];

const ABOUT_BLURB = 'Megan. Engineer, mom, sarcastic, mostly keto, runs in costume on purpose. I build agentic software, automate my house past the point of reason, and ship side projects faster than I should. Currently interviewing \u2014 the projects here are real, just mostly running in my kitchen.';

Object.assign(window, { POSTS, PROJECTS, NOW, ABOUT_BLURB });
