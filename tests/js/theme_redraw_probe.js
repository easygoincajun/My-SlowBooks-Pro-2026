// Evaluate dashboard.js with stubs and prove a theme toggle redraws the trend in the new theme's ink.
const fs = require('fs'), vm = require('vm');
const listeners = {}; let theme = 'light'; const made = []; let destroyed = 0;
const canvas = {};
const ctx = {
  console, setTimeout: (f) => f(),
  document: {
    documentElement: { getAttribute: () => theme },
    getElementById: (id) => (id === 'chart-bs-trend' ? canvas : null),
    addEventListener: (n, f) => { (listeners[n] = listeners[n] || []).push(f); },
  },
  getComputedStyle: () => ({ getPropertyValue: () => (theme === 'dark' ? ' #4a7fb5' : ' #336699') }),
  Chart: function (c, cfg) { made.push(cfg); this.destroy = () => { destroyed++; }; },
  window: {}, T: (s) => s, formatCurrency: (n) => String(n), escapeHtml: (s) => s, App: {}, API: {}, $: () => null,
  // utils.js's chartColor: the theme's --chart-* colour
  chartColor: (name) => (theme === 'dark' ? 'dark-' : 'light-') + name,
  // utils.js's chartFigures: the figures-on-the-marks plugin (#265)
  chartFigures: () => ({ id: 'sbFigures' }),
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync('app/static/js/dashboard.js', 'utf8') + '\nthis.DashboardPage = DashboardPage;', ctx);
const D = ctx.DashboardPage;
D._data = { balance_sheet_trend: { months: [{ month: 'Sep', year: 2026, assets: 1, liabilities: 2, equity: -1 }] } };
D._renderBsTrendChart();
theme = 'dark';
(listeners['slowbooks:themechange'] || []).forEach(f => f());
const ink = (c) => c.options.scales.x.ticks.color, line = (c) => c.data.datasets[0].borderColor;
console.log('charts made:', made.length, '| destroyed before redraw:', destroyed);
console.log('light render  axis', ink(made[0]), 'assets line', line(made[0]));
console.log('after toggle  axis', ink(made[1]), 'assets line', line(made[1]));
console.log('listener registered:', (listeners['slowbooks:themechange'] || []).length === 1);
