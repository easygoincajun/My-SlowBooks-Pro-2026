// Draw the dashboard's A/R Aging card from the data the server sent (argv[2],
// JSON) and print what the card says, one line per bar label / table row.
// Strip tags until nothing changes: one pass can leave a tag behind
// ('<scr<script>ipt>'), which is how CodeQL reads a single replace.
const stripTags = (s) => { let prev; s = String(s); do { prev = s; s = s.replace(/<[^>]*>/g, ''); } while (s !== prev); return s; };
const fs = require('fs'), vm = require('vm');
const ctx = {
  console,
  window: {},
  document: { addEventListener: () => {}, documentElement: { getAttribute: () => 'light' } },
  escapeHtml: (s) => String(s), T: (s) => s, Terms: { text: (s) => s },
  // utils.js's own formatCurrency
  formatCurrency: (n) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(n || 0),
  // utils.js's compactMoney, cssVar and figureTextOn: the figure a segment
  // carries (#265), in text that reads on it
  compactMoney: (n) => '$' + Math.round(n),
  cssVar: () => '#339966',
  figureTextOn: () => ({ fill: '#1a1a28', ratio: 4.82 }),
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync('app/static/js/dashboard.js', 'utf8') + '\nthis.D = DashboardPage;', ctx);
const html = ctx.D.renderers.ar_aging(JSON.parse(process.argv[2]));
const lines = [];
for (const m of html.matchAll(/<span><span aria-hidden="true"[^>]*><\/span>([^<]*)<\/span>/g)) lines.push(m[1].trim());
for (const m of html.matchAll(/<tr[^>]*>([\s\S]*?)<\/tr>/g)) {
  const cells = [...m[1].matchAll(/<td[^>]*>([\s\S]*?)<\/td>/g)].map((c) => stripTags(c[1]).trim());
  lines.push(cells.join(' | '));
}
if (!lines.length) lines.push(stripTags(html).trim());
console.log(lines.join('\n'));
