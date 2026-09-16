/**
 * بيئة DOM مصغّرة لاختبار الواجهة بلا jsdom (غير متاح — npm محجوب).
 * تكفي لاختبار: البناء، التحويل، الحالات، الجداول، المخططات.
 */
class N {
  constructor(tag, ns) {
    this.tagName = (tag || '').toUpperCase(); this.ns = ns || null;
    this.children = []; this.attributes = {}; this.listeners = {};
    this._text = ''; this.className = ''; this.parentNode = null;
    this.style = {}; this.dataset = {};
  }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return this.attributes[k] ?? null; }
  removeAttribute(k) { delete this.attributes[k]; }
  addEventListener(t, f) { (this.listeners[t] ||= []).push(f); }
  removeEventListener() {}
  dispatch(t, ev = {}) { (this.listeners[t] || []).forEach((f) => f(ev)); }
  append(...ns) { for (const n of ns) {
    if (n === null || n === undefined) continue;
    const c = n instanceof N || n instanceof T ? n : new T(String(n));
    c.parentNode = this; this.children.push(c); } }
  replaceChildren(...ns) { this.children = []; this.append(...ns); }
  set innerHTML(v) { this._text = v; this.children = [new T(v)]; }
  get innerHTML() { return this._text; }
  get textContent() {
    return this.children.map((c) => c.textContent ?? '').join('');
  }
  querySelectorAll(sel) {
    const out = []; const want = sel.replace(/^[.#]/, '');
    const walk = (n) => {
      const cls = (n.className || '').split(/\s+/);
      const tag = (n.tagName || '').toLowerCase();
      if ((sel.startsWith('.') && cls.includes(want)) ||
          (!sel.startsWith('.') && !sel.startsWith('#') && tag === sel.toLowerCase()))
        out.push(n);
      (n.children || []).forEach((c) => c.children && walk(c));
    };
    walk(this); return out;
  }
  querySelector(s) { return this.querySelectorAll(s)[0] || null; }
  get classList() {
    const self = this;
    return {
      add: (c) => { const s = new Set((self.className || '').split(/\s+/).filter(Boolean)); s.add(c); self.className = [...s].join(' '); },
      remove: (c) => { const s = new Set((self.className || '').split(/\s+/).filter(Boolean)); s.delete(c); self.className = [...s].join(' '); },
      contains: (c) => (self.className || '').split(/\s+/).includes(c),
    };
  }
}
class T { constructor(t) { this.textContent = String(t); this.children = []; } }

const registry = new Map();
const doc = {
  createElement: (t) => new N(t),
  createElementNS: (ns, t) => new N(t, ns),
  createTextNode: (t) => new T(t),
  getElementById: (id) => registry.get(id) || null,
  addEventListener: () => {},
  body: new N('body'),
  hidden: false,
};
export function registerId(id, node) { registry.set(id, node); }
export function resetIds() { registry.clear(); }

globalThis.Node = N;
globalThis.document = doc;
globalThis.window = { addEventListener: () => {}, location: { hash: '' } };
globalThis.location = { hash: '#/dashboard' };
globalThis.Intl = globalThis.Intl;
globalThis.Blob = class { constructor(p) { this.parts = p; } };
globalThis.URL = globalThis.URL || {};
globalThis.URL.createObjectURL = () => 'blob:x';
globalThis.URL.revokeObjectURL = () => {};

export { N, T, doc };
