/**
 * طبقة الوصول للـ API — قراءة فقط.
 * كاش مركزي يمنع تكرار الطلبات لنفس البيانات من مكوّنات مختلفة.
 * لا يوجد أي دالة POST/PUT/DELETE في هذا الملف — بالتصميم.
 */
const cache = new Map();
const inflight = new Map();

export const TTL = { live: 5000, historical: 60000, health: 10000 };

export class ApiError extends Error {
  constructor(msg, status, code) {
    super(msg); this.status = status; this.code = code;
  }
}

function key(path, params) {
  const q = new URLSearchParams(
    Object.entries(params || {}).filter(([, v]) => v !== undefined && v !== null && v !== '')
  ).toString();
  return q ? `${path}?${q}` : path;
}

/** آخر meta مستلَمة — تُستخدم للافتة التحذيرات وحالة الحداثة. */
export const lastMeta = { value: null };

export async function get(path, params, ttl = 0) {
  const k = key(path, params);
  const now = Date.now();
  if (ttl > 0) {
    const hit = cache.get(k);
    if (hit && now - hit.t < ttl) return hit.v;
  }
  if (inflight.has(k)) return inflight.get(k);

  const p = (async () => {
    let res;
    try {
      res = await fetch(`/api${k}`, {
        method: 'GET',
        headers: { Accept: 'application/json' },
      });
    } catch (e) {
      throw new ApiError('ENGINE CONNECTION LOST', 0, 'NETWORK');
    }
    let body = null;
    try { body = await res.json(); } catch { /* قد يكون فارغاً */ }
    if (!res.ok || body?.ok === false) {
      const code = body?.error?.code || body?.error || `HTTP_${res.status}`;
      const msg = {
        DATABASE_UNAVAILABLE: 'DATABASE UNAVAILABLE',
        READ_ONLY_VIOLATION: 'READ-ONLY VIOLATION',
        SCHEMA_MISMATCH: 'SCHEMA MISMATCH',
        RATE_LIMITED: 'RATE LIMITED — تجاوزت حد الطلبات',
        AUTH_REQUIRED: 'AUTH REQUIRED',
        INVALID_CREDENTIALS: 'بيانات دخول غير صحيحة',
        LOGIN_LOCKED: 'محاولات كثيرة — حاول لاحقاً',
        NOT_FOUND: 'NOT FOUND',
      }[code] || body?.error?.message || `HTTP ${res.status}`;
      throw new ApiError(msg, res.status, code);
    }
    if (body?.meta) lastMeta.value = body.meta;
    if (ttl > 0) cache.set(k, { t: now, v: body });
    return body;
  })().finally(() => inflight.delete(k));

  inflight.set(k, p);
  return p;
}

/** المصادقة — جلسات عرض فقط، لا صلة لها ببيانات المحرك. */
export const auth = {
  session: () => get('/auth/session'),
  async login(username, password) {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    const body = await res.json().catch(() => null);
    if (!res.ok || body?.ok === false) {
      throw new ApiError(
        body?.error?.message || 'فشل الدخول', res.status,
        body?.error?.code || 'LOGIN_FAILED');
    }
    clearCache();
    return body;
  },
  async logout() {
    await fetch('/api/auth/logout', { method: 'POST' }).catch(() => {});
    clearCache();
  },
};

export const api = {
  accessLog: (p) => get('/access-log', p, TTL.health),
  meta:            ()   => get('/meta', null, TTL.historical),
  health:          ()   => get('/health', null, TTL.health),
  dashboard:       ()   => get('/dashboard', null, TTL.live),
  market:          ()   => get('/market', null, TTL.live),
  recommendations: (p)  => get('/recommendations', p, TTL.live),
  recommendation:  (id) => get(`/recommendations/${id}`, null, TTL.historical),
  positions:       (p)  => get('/positions', p, TTL.live),
  position:        (id) => get(`/positions/${id}`, null, TTL.live),
  trades:          (p)  => get('/trades', p, TTL.historical),
  performance:     (p)  => get('/performance', p, TTL.historical),
  accuracy:        ()   => get('/accuracy', null, TTL.historical),
  audit:           (p)  => get('/audit', p, TTL.health),
  equity:          ()   => get('/equity', null, TTL.historical),
  dataQuality:     (p)  => get('/data-quality', p, TTL.historical),
  settings:        ()   => get('/settings', null, TTL.historical),
};

export function clearCache(prefix) {
  if (!prefix) return cache.clear();
  for (const k of [...cache.keys()]) if (k.startsWith(prefix)) cache.delete(k);
}
