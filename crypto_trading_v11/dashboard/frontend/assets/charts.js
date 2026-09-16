/**
 * مخططات SVG بلا مكتبات خارجية.
 * لا يُرسم مخطط ببيانات غير كافية — يُعرض "INSUFFICIENT DATA" بدلاً منه.
 */
import { el, empty, num, isNil } from './ui.js';

const NS = 'http://www.w3.org/2000/svg';
const s = (tag, attrs = {}, ...kids) => {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined) continue;
    n.setAttribute(k, String(v));
  }
  for (const c of kids.flat()) {
    if (c === null || c === undefined) continue;
    n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return n;
};

const MIN_POINTS = 2;

export function insufficient(msg = 'INSUFFICIENT DATA') {
  return el('div', { class: 'state' },
    el('div', { class: 'big' }, msg),
    'لا تُرسم مخططات ببيانات غير كافية — الرسم المضلِّل أسوأ من غيابه.');
}

function frame(w, h, pad) {
  return { w, h, pad, iw: w - pad.l - pad.r, ih: h - pad.t - pad.b };
}

function axes(f, yMin, yMax, xLabels, desc) {
  const g = [];
  const ticks = 4;
  for (let i = 0; i <= ticks; i++) {
    const y = f.pad.t + (f.ih * i) / ticks;
    const val = yMax - ((yMax - yMin) * i) / ticks;
    g.push(s('line', { x1: f.pad.l, y1: y, x2: f.w - f.pad.r, y2: y,
      stroke: '#242b3a', 'stroke-width': 1 }));
    g.push(s('text', { x: f.pad.l - 6, y: y + 4, 'text-anchor': 'end',
      fill: '#8b93a7', 'font-size': 10, 'font-family': 'monospace' },
      num(val, Math.abs(yMax - yMin) < 10 ? 2 : 0)));
  }
  if (xLabels?.length) {
    const step = Math.max(1, Math.ceil(xLabels.length / 6));
    xLabels.forEach((lb, i) => {
      if (i % step !== 0) return;
      const x = f.pad.l + (f.iw * i) / Math.max(1, xLabels.length - 1);
      g.push(s('text', { x, y: f.h - 6, 'text-anchor': 'middle',
        fill: '#5d6579', 'font-size': 9, 'font-family': 'monospace' }, lb));
    });
  }
  return g;
}

function svgWrap(f, children, desc) {
  return s('svg', { class: 'chart', viewBox: `0 0 ${f.w} ${f.h}`,
    role: 'img', 'aria-label': desc, preserveAspectRatio: 'none' },
    s('title', {}, desc), children);
}

/** خط — منحنى الحقوق */
export function lineChart(points, { color = '#58a6ff', height = 210,
  label = 'منحنى', fill = true } = {}) {
  const vals = (points || []).map((p) => Number(p.y)).filter(Number.isFinite);
  if (vals.length < MIN_POINTS) return insufficient();
  const f = frame(760, height, { t: 12, r: 12, b: 24, l: 52 });
  let mn = Math.min(...vals), mx = Math.max(...vals);
  if (mn === mx) { mn -= 1; mx += 1; }
  const pad = (mx - mn) * 0.08; mn -= pad; mx += pad;
  const X = (i) => f.pad.l + (f.iw * i) / Math.max(1, vals.length - 1);
  const Y = (v) => f.pad.t + f.ih - ((v - mn) / (mx - mn)) * f.ih;
  const d = vals.map((v, i) => `${i ? 'L' : 'M'}${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(' ');
  const area = `${d} L${X(vals.length - 1).toFixed(1)},${f.pad.t + f.ih} L${f.pad.l},${f.pad.t + f.ih} Z`;
  const desc = `${label}: ${vals.length} نقطة، من ${num(vals[0], 2)} إلى ${num(vals[vals.length - 1], 2)}`;
  return svgWrap(f, [
    ...axes(f, mn, mx, (points || []).map((p) => p.x || '')),
    fill ? s('path', { d: area, fill: color, 'fill-opacity': .08 }) : null,
    s('path', { d, fill: 'none', stroke: color, 'stroke-width': 2,
      'stroke-linejoin': 'round' }),
  ], desc);
}

/** أعمدة — PnL يومي/توزيع */
export function barChart(items, { height = 200, label = 'أعمدة',
  posColor = '#3fb950', negColor = '#f85149' } = {}) {
  const vals = (items || []).map((i) => Number(i.y)).filter(Number.isFinite);
  if (vals.length < 1) return insufficient();
  const f = frame(760, height, { t: 12, r: 12, b: 24, l: 52 });
  let mn = Math.min(0, ...vals), mx = Math.max(0, ...vals);
  if (mn === mx) mx = mn + 1;
  const Y = (v) => f.pad.t + f.ih - ((v - mn) / (mx - mn)) * f.ih;
  const bw = Math.max(2, (f.iw / vals.length) * 0.7);
  const zero = Y(0);
  const bars = vals.map((v, i) => {
    const x = f.pad.l + (f.iw * (i + 0.5)) / vals.length - bw / 2;
    const y = Math.min(Y(v), zero);
    const h = Math.max(1, Math.abs(Y(v) - zero));
    return s('rect', { x, y, width: bw, height: h,
      fill: v >= 0 ? posColor : negColor, 'fill-opacity': .85,
      role: 'presentation' },
      s('title', {}, `${items[i].x || i}: ${num(v, 4)}`));
  });
  const total = vals.reduce((a, b) => a + b, 0);
  return svgWrap(f, [
    ...axes(f, mn, mx, (items || []).map((i) => i.x || '')),
    s('line', { x1: f.pad.l, y1: zero, x2: f.w - f.pad.r, y2: zero,
      stroke: '#3a4256', 'stroke-width': 1 }),
    bars,
  ], `${label}: ${vals.length} عمود، المجموع ${num(total, 2)}`);
}

/** منطقة — منحنى التراجع */
export function drawdownChart(points, { height = 170 } = {}) {
  const vals = (points || []).map((p) => Number(p.y)).filter(Number.isFinite);
  if (vals.length < MIN_POINTS) return insufficient();
  const f = frame(760, height, { t: 12, r: 12, b: 24, l: 52 });
  const mx = Math.max(...vals, 1);
  const X = (i) => f.pad.l + (f.iw * i) / Math.max(1, vals.length - 1);
  const Y = (v) => f.pad.t + (v / mx) * f.ih;
  const d = vals.map((v, i) => `${i ? 'L' : 'M'}${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(' ');
  return svgWrap(f, [
    ...axes(f, mx, 0, (points || []).map((p) => p.x || '')),
    s('path', { d: `${d} L${X(vals.length - 1)},${f.pad.t} L${f.pad.l},${f.pad.t} Z`,
      fill: '#f85149', 'fill-opacity': .12 }),
    s('path', { d, fill: 'none', stroke: '#f85149', 'stroke-width': 1.8 }),
  ], `منحنى التراجع: أقصى ${num(mx, 2)}%`);
}

/** منحنى المعايرة — المتوقَّع مقابل الفعلي */
export function calibrationChart(buckets, { height = 300 } = {}) {
  if (!buckets || buckets.length < 2) return insufficient('CALIBRATION INSUFFICIENT');
  const f = frame(560, height, { t: 14, r: 14, b: 34, l: 48 });
  const X = (v) => f.pad.l + v * f.iw;
  const Y = (v) => f.pad.t + f.ih - v * f.ih;
  const pts = buckets.map((b) => ({ p: Number(b.predicted), a: Number(b.actual), n: b.n, l: b.bucket }))
    .filter((b) => Number.isFinite(b.p) && Number.isFinite(b.a));
  if (pts.length < 2) return insufficient('CALIBRATION INSUFFICIENT');
  const line = pts.map((b, i) => `${i ? 'L' : 'M'}${X(b.p).toFixed(1)},${Y(b.a).toFixed(1)}`).join(' ');
  const grid = [];
  for (let i = 0; i <= 4; i++) {
    const v = i / 4;
    grid.push(s('line', { x1: f.pad.l, y1: Y(v), x2: f.w - f.pad.r, y2: Y(v),
      stroke: '#242b3a' }));
    grid.push(s('text', { x: f.pad.l - 6, y: Y(v) + 4, 'text-anchor': 'end',
      fill: '#8b93a7', 'font-size': 10 }, `${(v * 100).toFixed(0)}%`));
    grid.push(s('text', { x: X(v), y: f.h - 8, 'text-anchor': 'middle',
      fill: '#5d6579', 'font-size': 10 }, `${(v * 100).toFixed(0)}%`));
  }
  const gap = pts.reduce((a, b) => a + Math.abs(b.a - b.p), 0) / pts.length;
  return svgWrap(f, [
    grid,
    s('line', { x1: X(0), y1: Y(0), x2: X(1), y2: Y(1),
      stroke: '#5d6579', 'stroke-dasharray': '5 4', 'stroke-width': 1.4 }),
    s('path', { d: line, fill: 'none', stroke: '#a371f7', 'stroke-width': 2.2 }),
    pts.map((b) => s('circle', { cx: X(b.p), cy: Y(b.a),
      r: Math.min(9, 3 + Math.sqrt(b.n)), fill: '#a371f7', 'fill-opacity': .85 },
      s('title', {}, `${b.l} · n=${b.n} · متوقَّع ${(b.p * 100).toFixed(1)}% · فعلي ${(b.a * 100).toFixed(1)}%`))),
    s('text', { x: f.pad.l + 6, y: f.pad.t + 14, fill: '#5d6579',
      'font-size': 10 }, 'الخط المتقطع = معايرة مثالية'),
  ], `منحنى المعايرة: ${pts.length} شريحة، متوسط الانحراف ${(gap * 100).toFixed(1)}%`);
}
