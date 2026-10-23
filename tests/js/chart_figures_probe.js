// The figures-on-the-marks plugin (#265), run against hand-built charts the
// way Chart.js would call it: bars (vertical, negative, one with no room
// above, a stacked segment), slices (one too thin), points on two lines
// (some colliding). Prints what was drawn where, as JSON.
const fs = require('fs'), vm = require('vm');
const ctx = {
  console, window: {}, getComputedStyle: () => ({ getPropertyValue: () => '' }),
  document: { documentElement: { getAttribute: () => process.argv[2] || 'light' }, addEventListener: () => {} },
  Intl, Number, Math, String, parseInt, parseFloat, Array,
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync('app/static/js/utils.js', 'utf8'), ctx);

function run(datasets, metas, area) {
  const drawn = [];
  let fill = null;
  const c = {
    ctx: {
      save() {}, restore() {}, set font(v) { this._font = v; }, get font() { return this._font; },
      set fillStyle(v) { fill = v; }, get fillStyle() { return fill; },
      textAlign: '', textBaseline: '',
      measureText: (t) => ({ width: t.length * 6 }),
      fillText: (t, x, y) => drawn.push({ text: t, x, y, fill }),
    },
    chartArea: area || { left: 0, top: 0, right: 400, bottom: 200 },
    data: { datasets },
    getDatasetMeta: (i) => metas[i],
  };
  ctx.chartFigures().afterDatasetsDraw(c);
  return drawn;
}

const out = {};
out.compact = ['1234', '12345', '3400000', '-850', '45.5', '999', '0', '12000000'].map((v) => ctx.compactMoney(v));

// vertical bars: 150 (room above), -40 (below), 9999 touching the top (inside), 0 (nothing)
const bars = { type: 'bar', data: [
  { x: 50, y: 120, base: 180, horizontal: false, options: { backgroundColor: '#00c48f' } },
  { x: 100, y: 180, base: 150, horizontal: false, options: { backgroundColor: '#ff6b6b' } },
  { x: 150, y: 3, base: 180, horizontal: false, options: { backgroundColor: '#1a3a5a' } },
  { x: 200, y: 180, base: 180, horizontal: false, options: { backgroundColor: '#00c48f' } },
] };
out.bars = run([{ data: [150, -40, 9999, 0] }], [bars]);

// a stacked horizontal bar: a wide segment carries its figure, a thin one does not
const stacked = { type: 'bar', data: [
  { x: 300, y: 50, base: 20, horizontal: true, options: { backgroundColor: '#fef3c7' } },
  { x: 318, y: 50, base: 300, horizontal: true, options: { backgroundColor: '#991b1b' } },
] };
out.stacked = run([{ stack: 'aging', data: [2800, 180] }], [stacked]);

// a stacked segment long enough but thinner than the text (a ~7px bar) carries nothing (NEW-61 / W-5)
const thin = { type: 'bar', data: [{ x: 300, y: 50, base: 20, height: 6, horizontal: true, options: { backgroundColor: '#fef3c7' } }] };
out.thin = run([{ stack: 'aging', data: [2800] }], [thin]);
// a plain horizontal bar with no room past its end and too thin to hold the text inside: nothing
const cramped = { type: 'bar', data: [{ x: 395, y: 50, base: 20, height: 6, horizontal: true, options: { backgroundColor: '#1a3a5a' } }] };
out.cramped = run([{ data: [9000] }], [cramped]);

// a plain horizontal bar: past its end
const hbar = { type: 'bar', data: [{ x: 120, y: 80, base: 20, horizontal: true, options: { backgroundColor: '#00c48f' } }] };
out.hbar = run([{ data: [640] }], [hbar]);

// slices: 70 / 27 / 3 per cent — the thin one is left off; dark slice, white text
const slices = { type: 'doughnut', data: [
  { tooltipPosition: () => ({ x: 100, y: 60 }), options: { backgroundColor: '#1a3a5a' } },
  { tooltipPosition: () => ({ x: 160, y: 140 }), options: { backgroundColor: '#fef3c7' } },
  { tooltipPosition: () => ({ x: 60, y: 140 }), options: { backgroundColor: '#991b1b' } },
] };
out.slices = run([{ data: [7000, 2700, 300] }], [slices]);

// a slice whose colour gives neither ink nor white 4.5:1 (the doughnut's red,
// NEW-75) carries no figure — the table has it; a dark slice beside it does
const lowc = { type: 'doughnut', data: [
  { tooltipPosition: () => ({ x: 100, y: 60 }), options: { backgroundColor: '#1a3a5a' } },
  { tooltipPosition: () => ({ x: 160, y: 140 }), options: { backgroundColor: '#e5484d' } },
] };
out.lowcontrast = run([{ data: [7000, 2700] }], [lowc]);
// the same colour on a stacked segment wide and thick enough: nothing inside it either
const lowbar = { type: 'bar', data: [{ x: 300, y: 50, base: 20, height: 40, horizontal: true, options: { backgroundColor: '#e5484d' } }] };
out.lowcontrast_bar = run([{ stack: 'aging', data: [2800] }], [lowbar]);

// two lines, three points each; the second line sits 4px under the first, so
// its labels (below) stay clear while labels that would overlap are left off
const line1 = { type: 'line', data: [{ x: 40, y: 100 }, { x: 60, y: 100 }, { x: 200, y: 100 }] };
const line2 = { type: 'line', data: [{ x: 40, y: 104 }, { x: 60, y: 104 }, { x: 200, y: 104 }] };
out.lines = run([{ data: [1000, 1000, 1000] }, { data: [2000, 2000, 2000] }], [line1, line2]);

// a line's first and last points sit on the area's edges, and the top one on
// its top (the month that is the axis maximum — NEW-81 / W-5): the label
// slides in from the edge, and the top point's goes below it
const edges = { type: 'line', data: [{ x: 0, y: 40 }, { x: 200, y: 0 }, { x: 400, y: 40 }] };
out.edges = run([{ data: [1000, 2500, 1000] }], [edges]);

// a steep rise into a last-point maximum with no room above it (W-6: the
// label under the peak sat on the line rising into it): above is off the
// chart, below is crossed, so the label goes beside the point, to its left;
// the middle point's label goes below, since its own line crosses the place
// above; the first point's stays above
const peak = { type: 'line', data: [{ x: 0, y: 290 }, { x: 200, y: 272 }, { x: 400, y: 32 }] };
out.peak = run([{ data: [290, 272, 32] }], [peak], { left: 0, top: 24, right: 400, bottom: 300 });
// a curve: the same points with bezier control points bulging the last
// segment up through the place beside the peak, so the label takes a corner
const bulge = { type: 'line', data: [
  { x: 0, y: 290 },
  { x: 200, y: 272, cp2x: 320, cp2y: 272 },
  { x: 400, y: 32, cp1x: 330, cp1y: 20 },
] };
out.bulge = run([{ data: [290, 272, 32] }], [bulge], { left: 0, top: 24, right: 400, bottom: 300 });

// a hidden dataset draws nothing
out.hidden = run([{ data: [5] }], [{ type: 'bar', hidden: true, data: [{ x: 10, y: 10, base: 100 }] }]);

console.log(JSON.stringify(out));
