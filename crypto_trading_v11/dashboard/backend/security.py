"""
طبقة الأمان — مصادقة، تحديد معدّل، سجل وصول.
==============================================
⚠️ هذه الطبقة تحمي **القراءة** فقط. لا تمنح أي صلاحية تداول ولا
تستطيع منحها — لا يوجد في اللوحة مسار كتابة أصلاً.

المصادقة اختيارية على 127.0.0.1 وإلزامية على أي عنوان آخر.
"""
import hashlib
import hmac
import ipaddress
import os
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple, Deque

# ── إعدادات ──
SESSION_TTL_S = int(os.getenv('DASHBOARD_SESSION_TTL', '3600'))
LOGIN_MAX_ATTEMPTS = int(os.getenv('DASHBOARD_LOGIN_MAX_ATTEMPTS', '5'))
LOGIN_WINDOW_S = int(os.getenv('DASHBOARD_LOGIN_WINDOW', '300'))
LOGIN_LOCKOUT_S = int(os.getenv('DASHBOARD_LOGIN_LOCKOUT', '900'))
RATE_LIMIT_RPM = int(os.getenv('DASHBOARD_RATE_LIMIT_RPM', '240'))
PBKDF2_ROUNDS = 200_000

LOCAL_NETS = [ipaddress.ip_network('127.0.0.0/8'),
              ipaddress.ip_network('::1/128')]


def is_loopback(addr: Optional[str]) -> bool:
    if not addr:
        return False
    try:
        ip = ipaddress.ip_address(addr.split('%')[0])
    except ValueError:
        return False
    return any(ip in n for n in LOCAL_NETS)


def anonymize_ip(addr: Optional[str]) -> str:
    """
    إخفاء جزئي للعنوان — يكفي للتشخيص ولا يعرّف الشخص.
    IPv4: آخر octet يُصفَّر. IPv6: آخر 80 بت.
    """
    if not addr:
        return 'unknown'
    try:
        ip = ipaddress.ip_address(addr.split('%')[0])
    except ValueError:
        return 'invalid'
    if ip.version == 4:
        p = str(ip).split('.')
        return '.'.join(p[:3] + ['0'])
    return ':'.join(str(ip.exploded).split(':')[:3]) + '::'


# ── كلمات المرور ──
def hash_password(password: str, salt: Optional[str] = None) -> str:
    """PBKDF2-HMAC-SHA256. لا تُخزَّن كلمة المرور نصاً أبداً."""
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode(),
                             bytes.fromhex(salt), PBKDF2_ROUNDS)
    return f'pbkdf2_sha256${PBKDF2_ROUNDS}${salt}${dk.hex()}'


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt, digest = stored.split('$')
        if algo != 'pbkdf2_sha256':
            return False
        dk = hashlib.pbkdf2_hmac('sha256', password.encode(),
                                 bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(dk.hex(), digest)   # مقارنة ثابتة الزمن
    except Exception:
        return False


def load_users() -> Dict[str, str]:
    """
    المستخدمون من متغير البيئة بصيغة `user:hash,user2:hash2`.
    لا كلمات مرور نصية — الهاش فقط.
    """
    raw = os.getenv('DASHBOARD_USERS', '').strip()
    users: Dict[str, str] = {}
    for part in raw.split(','):
        part = part.strip()
        if not part or ':' not in part:
            continue
        name, _, h = part.partition(':')
        if h.startswith('pbkdf2_sha256$'):
            users[name.strip()] = h.strip()
    return users


# ── الجلسات ──
@dataclass
class Session:
    token: str
    user: str
    created: float
    last_seen: float

    def age(self) -> float:
        return time.time() - self.created

    def idle(self) -> float:
        return time.time() - self.last_seen

    def expired(self, ttl: int = SESSION_TTL_S,
                idle_max: Optional[int] = None) -> Tuple[bool, str]:
        """يُرجع (منتهية، السبب). الخمول والعمر الأقصى شرطان مستقلان."""
        if self.age() > ttl:
            return True, 'MAX_AGE'
        if idle_max is not None and self.idle() > idle_max:
            return True, 'IDLE_TIMEOUT'
        return False, ''


class SessionStore:
    """
    جلسات في الذاكرة. إعادة التشغيل تُبطلها كلها — مقصود.

    شرطان مستقلان للانتهاء:
      • العمر الأقصى: لا تعيش الجلسة أكثر من ttl مهما كان النشاط
      • الخمول: تنتهي بعد idle_max بلا استخدام حتى لو لم يبلغ ttl
    الثاني يحمي شاشة تُركت مفتوحة.
    """

    def __init__(self, ttl: int = SESSION_TTL_S,
                 idle_s: Optional[int] = None):
        self.ttl = ttl
        self.idle_s = idle_s
        self._s: Dict[str, Session] = {}
        self._lock = threading.RLock()
        self.last_expiry_reason: Optional[str] = None

    def create(self, user: str) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._s[token] = Session(token, user, now, now)
        return token

    def rotate(self, old_token: Optional[str], user: str) -> str:
        """
        معرّف جديد بعد الدخول وإبطال القديم — يمنع تثبيت الجلسة
        (session fixation): رمز زرعه مهاجم قبل الدخول لا يصير صالحاً.
        """
        self.destroy(old_token)
        return self.create(user)

    def get(self, token: Optional[str]) -> Optional[Session]:
        if not token:
            return None
        with self._lock:
            s = self._s.get(token)
            if s is None:
                return None
            gone, why = s.expired(self.ttl, self.idle_s)
            if gone:
                self._s.pop(token, None)
                self.last_expiry_reason = why
                return None
            s.last_seen = time.time()
            return s

    def destroy_user(self, user: str) -> int:
        """إبطال كل جلسات مستخدم."""
        with self._lock:
            toks = [t for t, s in self._s.items() if s.user == user]
            for t in toks:
                self._s.pop(t, None)
            return len(toks)

    def destroy(self, token: Optional[str]):
        if token:
            with self._lock:
                self._s.pop(token, None)

    def purge(self) -> int:
        with self._lock:
            dead = [t for t, s in self._s.items()
                    if s.expired(self.ttl, self.idle_s)[0]]
            for t in dead:
                self._s.pop(t, None)
            return len(dead)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._s)


# ── تحديد المعدّل ──
# حدود منفصلة لكل مجموعة مسارات — الثقيل ليس كالخفيف
ENDPOINT_LIMITS = {
    'login': 10,          # محاولات الدخول
    'heavy': 30,          # performance / accuracy — استعلامات تجميع
    'list': 120,          # recommendations / trades / audit
    'light': 300,         # meta / health / dashboard
}


def limit_group(path: str) -> str:
    if path.startswith('/api/auth/'):
        return 'login'
    if any(path.startswith(p) for p in
           ('/api/performance', '/api/accuracy', '/api/equity')):
        return 'heavy'
    if any(path.startswith(p) for p in
           ('/api/recommendations', '/api/trades', '/api/audit',
            '/api/positions', '/api/access-log')):
        return 'list'
    return 'light'


class RateLimiter:
    """
    نافذة منزلقة لكل مفتاح. تمنع إرهاق قاعدة المحرك.

    ⚠️ في الذاكرة — لا يعمل عبر عدة عمّال. مع gunicorn متعدد العمّال
    استخدم تحديد معدّل في الوكيل العكسي أو Redis. موثَّق في DEPLOYMENT.
    """

    def __init__(self, limit: int, window_s: float = 60.0):
        self.limit = limit
        self.window = window_s
        self._hits: Dict[str, Deque[float]] = {}
        self._lock = threading.RLock()

    def check(self, key: str, limit: Optional[int] = None
              ) -> Tuple[bool, int, float, int]:
        """يُرجع (مسموح، المتبقي، ثوانٍ حتى إعادة الضبط، الحد المطبَّق)."""
        lim = limit if limit is not None else self.limit
        now = time.monotonic()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= lim:
                return False, 0, round(self.window - (now - q[0]), 1), lim
            q.append(now)
            return True, lim - len(q), round(self.window - (now - q[0]), 1), lim

    def reset(self, key: Optional[str] = None):
        with self._lock:
            if key:
                self._hits.pop(key, None)
            else:
                self._hits.clear()


class LoginGuard:
    """
    قفل محاولات الدخول — المرحلة الرابعة.

    حدّان مستقلان:
      • لكل IP  — يمنع مهاجماً واحداً من تجربة عدة حسابات
      • لكل اسم مستخدم — يمنع توزيع التخمين على عدة عناوين

    وتأخير تدريجي بعد كل فشل يجعل التخمين مكلفاً قبل بلوغ القفل.
    لا يكشف وجود المستخدم إطلاقاً — نفس الرد ونفس الرمز لكل فشل.
    """

    def __init__(self, max_attempts: int = LOGIN_MAX_ATTEMPTS,
                 window_s: int = LOGIN_WINDOW_S,
                 lockout_s: int = LOGIN_LOCKOUT_S,
                 user_max_attempts: Optional[int] = None,
                 base_delay_s: float = 0.25, max_delay_s: float = 4.0):
        self.max = max_attempts
        self.window = window_s
        self.lockout = lockout_s
        self.user_max = user_max_attempts or (max_attempts * 2)
        self.base_delay = base_delay_s
        self.max_delay = max_delay_s
        self._fails: Dict[str, Deque[float]] = {}
        self._locked: Dict[str, float] = {}
        self._lock = threading.RLock()

    @staticmethod
    def ip_key(ip: str) -> str:
        return f'ip:{ip}'

    @staticmethod
    def user_key(user: str) -> str:
        return f'user:{(user or "").lower()[:64]}'

    def delay_for(self, key: str) -> float:
        """تأخير أسّي محدود بسقف — يبطّئ التخمين بلا تعليق الخادم."""
        with self._lock:
            n = len(self._fails.get(key, ()))
        if n <= 0:
            return 0.0
        return min(self.base_delay * (2 ** (n - 1)), self.max_delay)

    def check(self, ip: str, user: str) -> Tuple[bool, float, str]:
        """يُرجع (مسموح، ثوانٍ للتأخير/الانتظار، السبب)."""
        for key, label in ((self.ip_key(ip), 'IP'),
                           (self.user_key(user), 'USER')):
            locked, retry = self.locked(key)
            if locked:
                return False, retry, label
        delay = max(self.delay_for(self.ip_key(ip)),
                    self.delay_for(self.user_key(user)))
        return True, delay, ''

    def record_attempt_failure(self, ip: str, user: str):
        self.record_failure(self.ip_key(ip), limit=self.max)
        self.record_failure(self.user_key(user), limit=self.user_max)

    def record_attempt_success(self, ip: str, user: str):
        self.record_success(self.ip_key(ip))
        self.record_success(self.user_key(user))

    def locked(self, key: str) -> Tuple[bool, float]:
        with self._lock:
            until = self._locked.get(key)
            if until is None:
                return False, 0.0
            if time.monotonic() >= until:
                self._locked.pop(key, None)
                self._fails.pop(key, None)
                return False, 0.0
            return True, round(until - time.monotonic(), 1)

    def record_failure(self, key: str, limit: Optional[int] = None):
        limit = limit if limit is not None else self.max
        now = time.monotonic()
        with self._lock:
            q = self._fails.setdefault(key, deque())
            while q and now - q[0] > self.window:
                q.popleft()
            q.append(now)
            if len(q) >= limit:
                self._locked[key] = now + self.lockout

    def record_success(self, key: str):
        with self._lock:
            self._fails.pop(key, None)
            self._locked.pop(key, None)

    def reset(self):
        with self._lock:
            self._fails.clear()
            self._locked.clear()


# ── سجل الوصول ──
REDACT_HEADERS = {'authorization', 'cookie', 'set-cookie', 'x-api-key',
                  'x-mbx-apikey', 'proxy-authorization'}


@dataclass
class AccessEvent:
    ts: float
    user: str
    method: str
    path: str
    status: int
    duration_ms: float
    ip_anon: str
    environment: str

    def to_dict(self) -> Dict:
        return {'ts': int(self.ts * 1000), 'user': self.user,
                'method': self.method, 'path': self.path,
                'status': self.status,
                'duration_ms': round(self.duration_ms, 2),
                'ip': self.ip_anon, 'environment': self.environment}


class AccessLog:
    """
    سجل دائري في الذاكرة. لا يسجّل أسراراً ولا كوكيز ولا رؤوس مصادقة.
    """

    def __init__(self, maxlen: int = 500):
        self._q: Deque[AccessEvent] = deque(maxlen=maxlen)
        self._lock = threading.RLock()

    def record(self, **kw):
        with self._lock:
            self._q.append(AccessEvent(**kw))

    def recent(self, limit: int = 100):
        with self._lock:
            return [e.to_dict() for e in list(self._q)[-limit:]][::-1]

    def clear(self):
        with self._lock:
            self._q.clear()


# ── سياسة الوصول ──
@dataclass
class AccessPolicy:
    """
    القاعدة: بلا مصادقة ⇒ loopback فقط.
    الربط على عنوان غير محلي بلا مستخدمين ولا ثقة بوكيل = رفض التشغيل.
    """
    users: Dict[str, str] = field(default_factory=load_users)
    trust_proxy_auth: bool = field(
        default_factory=lambda: os.getenv('DASHBOARD_TRUST_PROXY_AUTH', '')
        .lower() in ('1', 'true', 'yes'))
    host: str = field(default_factory=lambda: os.getenv(
        'DASHBOARD_API_HOST', '127.0.0.1'))

    @property
    def auth_required(self) -> bool:
        return bool(self.users)

    @property
    def binds_publicly(self) -> bool:
        return not is_loopback(self.host) and self.host not in ('localhost',)

    def startup_check(self) -> Tuple[bool, str]:
        """يمنع تشغيلاً غير آمن بدل اكتشافه لاحقاً."""
        if not self.binds_publicly:
            return True, 'loopback — المصادقة اختيارية'
        if self.users:
            return True, f'ربط عام مع {len(self.users)} مستخدم'
        if self.trust_proxy_auth:
            return True, 'ربط عام خلف وكيل موثوق (المصادقة خارجية)'
        return False, (
            f'⛔ الربط على {self.host} بلا مصادقة. اضبط DASHBOARD_USERS '
            f'أو DASHBOARD_TRUST_PROXY_AUTH=1 خلف وكيل، أو ابقَ على 127.0.0.1')

    def allows_anonymous(self, remote_addr: Optional[str]) -> bool:
        if self.trust_proxy_auth:
            return True
        if not self.users:
            return is_loopback(remote_addr)
        return False


def parse_cors_origins() -> Tuple[list, Optional[str]]:
    """
    Origins مسموحة. `*` مرفوض دائماً — البيانات مالية.
    """
    raw = os.getenv('DASHBOARD_CORS_ORIGIN', '').strip()
    if not raw:
        return [], None
    if '*' in raw:
        return [], "CORS '*' مرفوض — بيانات تداول تتطلب Origins محددة"
    origins = [o.strip() for o in raw.split(',') if o.strip()]
    bad = [o for o in origins if not o.startswith(('http://', 'https://'))]
    if bad:
        return [], f'Origins غير صالحة: {bad}'
    return origins, None
