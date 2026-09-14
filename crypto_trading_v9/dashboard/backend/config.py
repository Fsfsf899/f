"""
إعدادات اللوحة وفحص الإقلاع — المرحلة الثانية.
================================================
الفلسفة: الإعداد غير الآمن **يمنع التشغيل** بدل أن يعمل بصمت.
اكتشاف الخلل عند الإقلاع أرخص بكثير من اكتشافه بعد التسريب.

⛔ لا يُقرأ أي مفتاح تداول هنا. أسماء متغيرات المحرك
(MAINNET_API_*, TESTNET_API_*, BINANCE_*) لا تظهر في هذا الملف
ولا في أي ملف داخل اللوحة — وهذا مُختبَر آلياً.
"""
import os
import re
import secrets
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# ── مستويات البيئة ──
LOCAL = 'local'
PRIVATE = 'private'
PRODUCTION = 'production'
TIERS = (LOCAL, PRIVATE, PRODUCTION)

MIN_SECRET_LEN = 32
WEAK_SECRETS = {
    'changeme', 'change-me', 'secret', 'secret-key', 'dev', 'development',
    'test', 'testing', 'password', 'default', 'please-change-me',
    'your-secret-key', 'insecure', 'dashboard', 'flask',
}
WEAK_PATTERN = re.compile(r'^(.)\1+$')      # حرف مكرر


def _env(name: str, default: str = '') -> str:
    return os.getenv(name, default).strip()


def _bool(name: str, default: bool = False) -> bool:
    v = _env(name).lower()
    if not v:
        return default
    return v in ('1', 'true', 'yes', 'on')


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


class ConfigError(RuntimeError):
    """إعداد غير آمن — يمنع الإقلاع."""


def secret_problems(secret: str) -> List[str]:
    """يُرجع قائمة العيوب. فارغة = المفتاح مقبول."""
    out: List[str] = []
    if not secret:
        return ['SECRET_KEY مفقود']
    if len(secret) < MIN_SECRET_LEN:
        out.append(f'SECRET_KEY أقصر من {MIN_SECRET_LEN} محرفاً ({len(secret)})')
    low = secret.lower()
    if low in WEAK_SECRETS or any(w == low for w in WEAK_SECRETS):
        out.append('SECRET_KEY قيمة معروفة/افتراضية')
    if WEAK_PATTERN.match(secret):
        out.append('SECRET_KEY حرف مكرر')
    if len(set(secret)) < 8:
        out.append('SECRET_KEY تنوّع محارف ضعيف')
    return out


@dataclass
class DashboardConfig:
    tier: str = field(default_factory=lambda: _env(
        'DASHBOARD_ENVIRONMENT_TIER', LOCAL).lower())
    data_env: str = field(default_factory=lambda: _env(
        'DASHBOARD_ENVIRONMENT', 'paper').lower())
    host: str = field(default_factory=lambda: _env(
        'DASHBOARD_API_HOST', '127.0.0.1'))
    port: int = field(default_factory=lambda: _int('DASHBOARD_API_PORT', 8080))
    secret_key: str = field(default_factory=lambda: _env('DASHBOARD_SECRET_KEY'))
    auth_required: Optional[bool] = field(default_factory=lambda: (
        None if not _env('DASHBOARD_AUTH_REQUIRED')
        else _bool('DASHBOARD_AUTH_REQUIRED')))
    debug: bool = field(default_factory=lambda: _bool('DASHBOARD_DEBUG'))
    https_enabled: bool = field(default_factory=lambda: _bool(
        'DASHBOARD_HTTPS_ENABLED'))
    trust_proxy_auth: bool = field(default_factory=lambda: _bool(
        'DASHBOARD_TRUST_PROXY_AUTH'))
    cors_raw: str = field(default_factory=lambda: _env('DASHBOARD_CORS_ORIGIN'))
    session_ttl_s: int = field(default_factory=lambda: _int(
        'DASHBOARD_SESSION_TTL_SECONDS', 3600))
    session_idle_s: int = field(default_factory=lambda: _int(
        'DASHBOARD_SESSION_IDLE_SECONDS', 900))
    rate_limit_rpm: int = field(default_factory=lambda: _int(
        'DASHBOARD_RATE_LIMIT', 240))
    db_url: str = field(default_factory=lambda: _env('DATABASE_READ_ONLY_URL'))
    users_raw: str = field(default_factory=lambda: _env('DASHBOARD_USERS'))
    secure_cookies: Optional[bool] = field(default_factory=lambda: (
        None if not _env('DASHBOARD_SECURE_COOKIES')
        else _bool('DASHBOARD_SECURE_COOKIES')))

    # ── مشتقات ──
    @property
    def is_local(self) -> bool:
        return self.tier == LOCAL

    @property
    def is_production(self) -> bool:
        return self.tier == PRODUCTION

    @property
    def binds_publicly(self) -> bool:
        from security import is_loopback
        return not is_loopback(self.host) and self.host != 'localhost'

    @property
    def cors_origins(self) -> List[str]:
        if not self.cors_raw:
            return []
        return [o.strip() for o in self.cors_raw.split(',') if o.strip()]

    @property
    def effective_auth_required(self) -> bool:
        """
        المصادقة إلزامية خارج local. في local اختيارية ما لم تُطلب.
        """
        if self.auth_required is not None:
            return self.auth_required
        return not self.is_local

    @property
    def effective_secure_cookies(self) -> bool:
        if self.secure_cookies is not None:
            return self.secure_cookies
        return self.https_enabled or self.is_production

    # ── فحص الإقلاع ──
    def validate(self, users_count: int = 0) -> Tuple[List[str], List[str]]:
        """
        يُرجع (أخطاء مانعة، تحذيرات).
        الأخطاء ترفع ConfigError في enforce().
        """
        errors: List[str] = []
        warns: List[str] = []

        if self.tier not in TIERS:
            errors.append(
                f"DASHBOARD_ENVIRONMENT_TIER='{self.tier}' غير معروف "
                f"(المسموح: {', '.join(TIERS)})")

        # ── المفتاح السري
        if self.tier in (PRIVATE, PRODUCTION):
            probs = secret_problems(self.secret_key)
            errors.extend(probs)
        elif self.secret_key:
            warns.extend(secret_problems(self.secret_key))

        # ── DEBUG
        if self.debug and self.tier in (PRIVATE, PRODUCTION):
            errors.append(f'DASHBOARD_DEBUG مفعّل في بيئة {self.tier}')

        # ── المصادقة
        if self.effective_auth_required and users_count == 0 \
                and not self.trust_proxy_auth:
            errors.append(
                'المصادقة مطلوبة لكن لا مستخدمين. اضبط DASHBOARD_USERS '
                'أو DASHBOARD_TRUST_PROXY_AUTH=1 خلف وكيل')

        if self.binds_publicly and not self.effective_auth_required:
            errors.append(
                f'الربط على {self.host} بلا مصادقة. اضبط '
                f'DASHBOARD_AUTH_REQUIRED=true أو ابقَ على 127.0.0.1')

        if self.binds_publicly and self.is_local:
            errors.append(
                f'tier=local لكن الربط على {self.host} — '
                f'استخدم private أو production')

        # ── CORS
        if '*' in self.cors_raw:
            if self.is_local:
                warns.append("CORS '*' في local — لا تستخدمه خارجها")
            else:
                errors.append(f"CORS '*' مرفوض في بيئة {self.tier}")
        for o in self.cors_origins:
            if o != '*' and not o.startswith(('http://', 'https://')):
                errors.append(f'Origin غير صالح: {o}')
            elif o.startswith('http://') and self.is_production:
                errors.append(f'Origin غير مشفَّر في production: {o}')

        # ── HTTPS
        if self.is_production and not self.https_enabled:
            errors.append(
                'production بلا HTTPS. اضبط DASHBOARD_HTTPS_ENABLED=1 '
                'بعد وضع TLS في الوكيل')
        if self.effective_auth_required and not self.effective_secure_cookies \
                and self.tier in (PRIVATE, PRODUCTION):
            warns.append('كوكي بلا Secure — فعّل HTTPS')

        # ── الجلسات
        if self.session_idle_s > self.session_ttl_s:
            warns.append('مهلة الخمول أطول من عمر الجلسة — بلا أثر')
        if self.is_production and self.session_ttl_s > 28800:
            warns.append('عمر جلسة طويل جداً في production')

        # ── المعدّل
        if self.rate_limit_rpm <= 0:
            errors.append('DASHBOARD_RATE_LIMIT يجب أن يكون موجباً')

        return errors, warns

    def enforce(self, users_count: int = 0) -> List[str]:
        errors, warns = self.validate(users_count)
        if errors:
            raise ConfigError(
                'إعداد غير آمن — الإقلاع مرفوض:\n  ⛔ '
                + '\n  ⛔ '.join(errors))
        return warns

    def safe_dict(self) -> dict:
        """بلا أسرار — صالح للطباعة والتشخيص."""
        return {
            'tier': self.tier, 'data_environment': self.data_env,
            'host': self.host, 'port': self.port,
            'auth_required': self.effective_auth_required,
            'debug': self.debug, 'https_enabled': self.https_enabled,
            'secure_cookies': self.effective_secure_cookies,
            'cors_origins': self.cors_origins,
            'session_ttl_s': self.session_ttl_s,
            'session_idle_s': self.session_idle_s,
            'rate_limit_rpm': self.rate_limit_rpm,
            'secret_key_set': bool(self.secret_key),
            'secret_key_len': len(self.secret_key),
        }


def generate_secret() -> str:
    return secrets.token_urlsafe(48)
