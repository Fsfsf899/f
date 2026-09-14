/** مكوّنات عرض قابلة لإعادة الاستخدام. لا منطق تداول هنا إطلاقاً. */

/**
 * بناء عنصر. النصوص تُدرَج كـ TextNode حصراً — لا innerHTML إطلاقاً،
 * فبيانات القاعدة لا يمكن أن تُنفَّذ كـ HTML.
 */
export const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') n.className = v;
    // لا خيار html: — innerHTML مع بيانات القاعدة ناقل XSS.
    // كل نص يمر عبر createTextNode في حلقة الأبناء أدناه.
    else if (k.startsWith('on') && typeof v === 'function')
      n.addEventListener(k.slice(2).toLowerCase(), v);
    else n.setAttribute(k, v === true ? '' : String(v));
  }
  for (const c of kids.flat()) {
    if (c === null || c === undefined || c === false) continue;
    n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return n;
};

/* ── التنسيق: العرض فقط، لا تقريب داخلي ── */
export const NA = 'N/A';
export const isNil = (v) => v === null || v === undefined || Number.isNaN(v);

export function num(v, d = 2) {
  if (isNil(v)) return NA;
  const n = Number(v);
  if (!Number.isFinite(n)) return NA;
  return n.toLocaleString('en-US',
    { minimumFractionDigits: d, maximumFractionDigits: d });
}
export function money(v, d = 2) {
  if (isNil(v)) return NA;
  const n = Number(v);
  return (n >= 0 ? '+$' : '-$') + Math.abs(n).toLocaleString('en-US',
    { minimumFractionDigits: d, maximumFractionDigits: d });
}
export function price(v) {
  if (isNil(v)) return NA;
  const n = Math.abs(Number(v));
  const d = n >= 1000 ? 2 : n >= 1 ? 4 : n >= 0.01 ? 6 : 8;
  return '$' + Number(v).toLocaleString('en-US',
    { minimumFractionDigits: d, maximumFractionDigits: d });
}
export const pct = (v, d = 2) => isNil(v) ? NA : `${num(v, d)}%`;
export const prob = (v) => isNil(v) ? NA : `${num(Number(v) * 100, 1)}%`;

export function ts(ms, withTz = false) {
  if (isNil(ms)) return NA;
  const d = new Date(Number(ms));
  if (Number.isNaN(d.getTime())) return NA;
  const s = d.toLocaleString(undefined, {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
  return withTz ? `${s} (${tzName()})` : s;
}
export const tzName = () =>
  Intl.DateTimeFormat().resolvedOptions().timeZone || 'local';

export function dur(ms) {
  if (isNil(ms) || ms < 0) return NA;
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}ث`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}د`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}س ${m % 60}د`;
  return `${Math.floor(h / 24)}ي ${h % 24}س`;
}

export const sign = (v) => isNil(v) ? 'm' : Number(v) > 0 ? 'g' : Number(v) < 0 ? 'r' : 'm';

/* ── مكوّنات ── */
export const kpi = (label, value, cls = '', sub = '') =>
  el('div', { class: 'card kpi' },
    el('div', { class: 'k' }, label),
    el('div', { class: `v ${cls}` }, value),
    sub ? el('div', { class: 's' }, sub) : null);

export function badge(text, kind = 'unk') {
  return el('span', { class: `badge ${kind}`, title: text }, text);
}
export const signalBadge = (s) =>
  badge(s || 'UNKNOWN',
    s === 'BUY' ? 'buy' : s === 'WAIT' ? 'wait' : s === 'NO_TRADE' ? 'no' : 'unk');
export const statusBadge = (s) => badge(s || 'UNKNOWN',
  ({ ONLINE: 'on', OFFLINE: 'off', DEGRADED: 'deg', HALTED: 'off',
     STALE: 'stale', UNKNOWN: 'unk' })[s] || 'unk');
export const outcomeBadge = (o, pnl) => {
  if (!o) return badge('OPEN', 'unk');
  const w = Number(pnl) > 0;
  return badge(w ? 'WIN' : Number(pnl) < 0 ? 'LOSS' : 'BREAKEVEN',
    w ? 'win' : Number(pnl) < 0 ? 'loss' : 'unk');
};
export const dqBadge = (v) => {
  if (isNil(v)) return badge(NA, 'unk');
  const n = Number(v);
  return badge(`${num(n, 0)}%`, n >= 90 ? 'on' : n >= 75 ? 'deg' : 'off');
};

export const loading = (what = 'البيانات') =>
  el('div', { class: 'card', role: 'status', 'aria-live': 'polite' },
    el('div', { class: 'state' }, `جاري تحميل ${what}…`),
    ...[1, 2, 3, 4].map(() => el('div', { class: 'skel' })));

export const empty = (msg) =>
  el('div', { class: 'card' }, el('div', { class: 'state' },
    el('div', { class: 'big' }, msg), 'لا توجد بيانات لعرضها.'));

export const errorState = (e, onRetry) =>
  el('div', { class: 'card' }, el('div', { class: 'state err' },
    el('div', { class: 'big' }, e?.message || 'فشل التحميل'),
    e?.code ? el('div', {}, e.code) : null,
    onRetry ? el('div', { style: 'margin-top:12px' },
      el('button', { onClick: onRetry }, 'إعادة المحاولة')) : null));

export function table(cols, rows, { onRow, caption } = {}) {
  if (!rows || rows.length === 0) return empty('لا توجد سجلات');
  return el('div', { class: 'tw' },
    el('table', {},
      caption ? el('caption', { class: 'sr' }, caption) : null,
      el('thead', {}, el('tr', {},
        cols.map((c) => el('th', { scope: 'col' }, c.label)))),
      el('tbody', {}, rows.map((r, i) => {
        const tr = el('tr', { class: onRow ? 'click' : '',
          tabindex: onRow ? '0' : null,
          onClick: onRow ? () => onRow(r) : null,
          onKeydown: onRow ? (ev) => {
            if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); onRow(r); }
          } : null },
          cols.map((c) => {
            const v = c.render ? c.render(r, i) : r[c.key];
            return el('td', { class: c.num ? 'num' : '' },
              v instanceof Node ? v : (isNil(v) ? NA : String(v)));
          }));
        return tr;
      }))));
}

export function pager(total, limit, offset, onGo) {
  const page = Math.floor(offset / limit) + 1;
  const pages = Math.max(1, Math.ceil(total / limit));
  return el('div', { class: 'pager' },
    el('span', {}, `${total} سجل · صفحة ${page}/${pages}`),
    el('button', { disabled: offset <= 0,
      onClick: () => onGo(Math.max(0, offset - limit)) }, '‹ السابق'),
    el('button', { disabled: offset + limit >= total,
      onClick: () => onGo(offset + limit) }, 'التالي ›'));
}

export function field(label, value, cls = '') {
  return el('div', { class: 'row' },
    el('dt', {}, label),
    el('dd', { class: cls }, value instanceof Node ? value : (isNil(value) ? NA : String(value))));
}

export const details = (items) =>
  el('dl', { class: 'dl' }, items.filter(Boolean));

export function select(label, value, options, onChange) {
  return el('div', { class: 'fld' },
    el('label', { for: `f-${label}` }, label),
    el('select', { id: `f-${label}`, onChange: (e) => onChange(e.target.value) },
      options.map((o) => {
        const [v, t] = Array.isArray(o) ? o : [o, o];
        return el('option', { value: v, selected: String(v) === String(value ?? '') }, t);
      })));
}

export function csvButton(filename, cols, rows) {
  return el('button', { onClick: () => {
    const head = cols.map((c) => c.label).join(',');
    const body = rows.map((r) => cols.map((c) => {
      const v = c.raw ? c.raw(r) : r[c.key];
      const s = isNil(v) ? '' : String(v);
      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    }).join(',')).join('\n');
    const blob = new Blob([`\uFEFF${head}\n${body}`],
      { type: 'text/csv;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    a.click();
    URL.revokeObjectURL(a.href);
  } }, '⤓ CSV');
}
