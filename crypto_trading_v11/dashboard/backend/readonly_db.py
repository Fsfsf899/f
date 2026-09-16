"""
طبقة قراءة فقط — الضمان التقني الأساسي.
========================================
Dashboard لا يستطيع تغيير مصدر الحقيقة. ثلاث طبقات مستقلة:

  1. اتصال SQLite بوضع `mode=ro` عبر URI — يرفضه المحرك نفسه
  2. `PRAGMA query_only=ON` — طبقة ثانية داخل الجلسة
  3. فحص نصي للاستعلام قبل التنفيذ — يرفض أي كلمة كتابة

حتى لو اخترقت طبقة، تبقى اثنتان. ولا يملك Dashboard مفاتيح تداول
إطلاقاً — لا تُقرأ ولا تُمرَّر.
"""
import os
import re
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

WRITE_TOKENS = re.compile(
    r'\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE|'
    r'ATTACH|DETACH|VACUUM|REINDEX|PRAGMA\s+\w+\s*=)\b', re.IGNORECASE)


class ReadOnlyViolation(Exception):
    """محاولة كتابة من طبقة العرض."""


class DatabaseUnavailable(Exception):
    """القاعدة غير متاحة — تُعرض كحالة، لا تُخفى."""


EXPECTED_SCHEMA_VERSION = 6
MAX_ROWS = 5000
QUERY_TIMEOUT_MS = 4000

# الجداول التي تعتمد عليها اللوحة. نقص أيٍّ منها ⇒ DEGRADED لا انهيار.
# `sizing_plans` عمداً **خارج** هذه المجموعة: قاعدة أُنشئت بمخطط v6
# لكن لم يُحسَب فيها أي خطة بعد قاعدة صالحة تماماً، ولا يجوز وسمها
# DEGRADED لذلك. غياب الخطط يُعالَج في طبقة الاستعلام برد
# NO_PLAN_YET صريح.
REQUIRED_TABLES = {'signals', 'recommendations', 'positions', 'orders',
                   'fills', 'risk_events', 'system_events', 'kv'}


class SchemaMismatch(Exception):
    """نسخة مخطط مختلفة — الأرقام قد تكون خاطئة."""


class ReadOnlyDB:
    def __init__(self, path: str, timeout: float = 5.0,
                 cache_ttl: float = 3.0, max_rows: int = MAX_ROWS,
                 query_timeout_ms: int = QUERY_TIMEOUT_MS):
        self.path = os.path.abspath(path)
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self.max_rows = max_rows
        self.query_timeout_ms = query_timeout_ms
        self._lock = threading.RLock()
        self._cache: Dict[str, Tuple[float, Any]] = {}
        self._schema: Optional[Dict] = None

    @property
    def exists(self) -> bool:
        return os.path.exists(self.path)

    def _connect(self) -> sqlite3.Connection:
        if not self.exists:
            raise DatabaseUnavailable(f'قاعدة غير موجودة: {self.path}')
        uri = f'file:{self.path}?mode=ro'
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=self.timeout)
        except sqlite3.Error as e:
            raise DatabaseUnavailable(str(e)[:200])
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')     # طبقة ثانية
        # مقاطعة الاستعلامات الثقيلة — اللوحة يجب ألا تُبطئ المحرك
        deadline = time.monotonic() + self.query_timeout_ms / 1000.0
        conn.set_progress_handler(
            lambda: 1 if time.monotonic() > deadline else 0, 2000)
        return conn

    @staticmethod
    def _guard(sql: str):
        s = sql.strip()
        if not re.match(r'^\s*(SELECT|WITH)\b', s, re.IGNORECASE):
            raise ReadOnlyViolation(f'يُسمح بـ SELECT فقط: {s[:60]}')
        if WRITE_TOKENS.search(s):
            raise ReadOnlyViolation(f'كلمة كتابة في الاستعلام: {s[:60]}')

    def query(self, sql: str, params: Tuple = ()) -> List[Dict]:
        self._guard(sql)                          # طبقة ثالثة
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(sql, params)
                rows = cur.fetchmany(self.max_rows + 1)
                if len(rows) > self.max_rows:
                    raise DatabaseUnavailable(
                        f'الاستعلام تجاوز {self.max_rows} صفاً — ضيّق الفلاتر')
                return [dict(r) for r in rows]
            except sqlite3.OperationalError as e:
                msg = str(e)
                if 'readonly' in msg.lower() or 'attempt to write' in msg.lower():
                    raise ReadOnlyViolation(msg[:200])
                if 'no such table' in msg.lower():
                    return []
                raise DatabaseUnavailable(msg[:200])
            finally:
                conn.close()

    def one(self, sql: str, params: Tuple = ()) -> Optional[Dict]:
        r = self.query(sql, params)
        return r[0] if r else None

    def scalar(self, sql: str, params: Tuple = (), default=None):
        r = self.one(sql, params)
        return list(r.values())[0] if r else default

    def cached(self, key: str, sql: str, params: Tuple = (),
               ttl: Optional[float] = None) -> List[Dict]:
        """كاش قصير — يمنع إرهاق القاعدة بالتحديث كل بضع ثوانٍ."""
        ttl = self.cache_ttl if ttl is None else ttl
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
        val = self.query(sql, params)
        with self._lock:
            self._cache[key] = (now, val)
        return val

    def invalidate(self):
        with self._lock:
            self._cache.clear()

    def tables(self) -> List[str]:
        try:
            return [r['name'] for r in self.query(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        except DatabaseUnavailable:
            return []

    def kv(self, key: str, default=None):
        try:
            r = self.one('SELECT v FROM kv WHERE k=?', (key,))
        except (DatabaseUnavailable, ReadOnlyViolation):
            return default
        if not r:
            return default
        import json
        try:
            return json.loads(r['v'])
        except Exception:
            return default

    def schema(self) -> Dict:
        """
        نسخة المخطط والجداول الموجودة. تُقرأ مرة وتُخزَّن.
        اختلاف النسخة يُبلَّغ ولا يُخفى — الأرقام قد تكون خاطئة.
        """
        if self._schema is not None:
            return self._schema
        try:
            conn = self._connect()
        except DatabaseUnavailable as e:
            return {'status': 'UNAVAILABLE', 'error': str(e)[:200],
                    'version': None, 'expected': EXPECTED_SCHEMA_VERSION,
                    'missing_tables': sorted(REQUIRED_TABLES)}
        try:
            ver = int(conn.execute('PRAGMA user_version').fetchone()[0])
            names = {r['name'] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        missing = sorted(REQUIRED_TABLES - names)
        if ver != EXPECTED_SCHEMA_VERSION:
            status = 'SCHEMA_MISMATCH'
        elif missing:
            status = 'DEGRADED'
        else:
            status = 'OK'
        self._schema = {'status': status, 'version': ver,
                        'expected': EXPECTED_SCHEMA_VERSION,
                        'missing_tables': missing, 'error': None}
        return self._schema

    def health(self) -> Dict:
        t0 = time.monotonic()
        try:
            self.query('SELECT 1')
        except Exception as e:
            return {'status': 'OFFLINE', 'latency_ms': None,
                    'error': str(e)[:200], 'path': os.path.basename(self.path),
                    'schema': self.schema()}
        sch = self.schema()
        status = ('ONLINE' if sch['status'] == 'OK'
                  else 'DEGRADED')      # مخطط ناقص أو مختلف ⇒ DEGRADED
        return {'status': status,
                'latency_ms': round((time.monotonic() - t0) * 1000, 2),
                'error': (None if sch['status'] == 'OK'
                          else f"schema {sch['status']}"),
                'path': os.path.basename(self.path),
                'schema': sch}
