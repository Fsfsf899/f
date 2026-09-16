"""
فصل البيئات — المرحلة الثانية.
==============================
كل بيئة معزولة تماماً: endpoint، مفاتيح، قاعدة بيانات، سجل، قفل.

القاعدة الحاكمة: **لا fallback صامت.** بيئة testnet لا تنزلق إلى
mainnet، وpaper لا تنزلق إلى testnet. أي تعارض يوقف التشغيل فوراً.
"""
import hashlib
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, List

TESTNET_URL = 'https://testnet.binance.vision'
MAINNET_URL = 'https://api.binance.com'


class Env(str, Enum):
    SHADOW = 'shadow'       # يسجّل ما كان سيحدث، بلا تنفيذ
    MONITOR = 'monitor'     # يسجّل التوصيات، بلا أوامر
    PAPER = 'paper'         # تنفيذ ورقي محافظ
    TESTNET = 'testnet'     # أوامر حقيقية، أموال وهمية
    LIVE = 'live'           # 🔴 أموال حقيقية

    @property
    def sends_orders(self) -> bool:
        return self in (Env.PAPER, Env.TESTNET, Env.LIVE)

    @property
    def touches_exchange(self) -> bool:
        """يرسل أوامر إلى منصة خارجية فعلاً."""
        return self in (Env.TESTNET, Env.LIVE)

    @property
    def real_money(self) -> bool:
        return self is Env.LIVE


class EnvironmentError_(Exception):
    """تعارض بيئي — يوقف التشغيل."""


# متغيرات مستقلة لكل بيئة. لا تشارك إطلاقاً.
KEY_VARS: Dict[Env, tuple] = {
    Env.TESTNET: ('TESTNET_API_KEY', 'TESTNET_API_SECRET'),
    Env.LIVE: ('MAINNET_API_KEY', 'MAINNET_API_SECRET'),
}

ENDPOINTS: Dict[Env, Optional[str]] = {
    Env.SHADOW: None, Env.MONITOR: None, Env.PAPER: None,
    Env.TESTNET: TESTNET_URL, Env.LIVE: MAINNET_URL,
}

# بصمات معروفة لمفاتيح mainnet تُرفض في testnet والعكس
FINGERPRINT_KV = 'api_key_fingerprint'


def fingerprint(secret: str) -> str:
    """
    بصمة لا تكشف المفتاح. تُستخدم لكشف تبديل المفاتيح بين البيئات
    ولتوثيق أي مفتاح استُخدم في أي تشغيل.
    """
    if not secret:
        return 'none'
    return hashlib.sha256(secret.encode()).hexdigest()[:12]


@dataclass
class EnvConfig:
    env: Env
    endpoint: Optional[str]
    db_path: str
    log_path: str
    lock_name: str
    api_key: str = ''
    api_secret: str = ''
    symbol: str = 'BTCUSDT'
    interval: str = '4h'
    strategy_version: str = ''
    config_fingerprint: str = ''

    @property
    def key_fingerprint(self) -> str:
        return fingerprint(self.api_secret)

    @property
    def tag(self) -> str:
        """علامة تُطبع في كل سجل وتُخزَّن مع كل أمر."""
        return f'[{self.env.value.upper()}]'

    def safe_dict(self) -> Dict:
        """بلا أسرار — صالح للطباعة والتخزين."""
        return {
            'environment': self.env.value,
            'endpoint': self.endpoint or 'local',
            'api_key_fingerprint': self.key_fingerprint,
            'database': self.db_path,
            'log': self.log_path,
            'symbol': self.symbol,
            'interval': self.interval,
            'strategy_version': self.strategy_version,
            'config_fingerprint': self.config_fingerprint,
        }


def build(env_name: str, *, base_dir: str, symbol: str = 'BTCUSDT',
          interval: str = '4h', strategy_version: str = '',
          config_fingerprint: str = '',
          db_override: Optional[str] = None) -> EnvConfig:
    """
    يبني إعداد بيئة معزولة. يرمي عند أي تعارض.
    """
    try:
        env = Env(env_name)
    except ValueError:
        raise EnvironmentError_(f"بيئة غير معروفة: {env_name}")

    data_dir = os.path.join(base_dir, 'data', env.value)
    log_dir = os.path.join(base_dir, 'logs')
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(log_dir, exist_ok=True)

    # قاعدة مستقلة لكل بيئة — لا خلط بين ورقي وحقيقي
    db_path = db_override or os.path.join(data_dir, f'{env.value}.db')
    log_path = os.path.join(log_dir, f'{env.value}.log')

    key = secret = ''
    if env in KEY_VARS:
        kv, sv = KEY_VARS[env]
        key = os.getenv(kv, '').strip()
        secret = os.getenv(sv, '').strip()

    cfg = EnvConfig(
        env=env, endpoint=ENDPOINTS[env], db_path=db_path, log_path=log_path,
        lock_name=f'trader:{env.value}:{os.path.abspath(db_path)}',
        api_key=key, api_secret=secret, symbol=symbol, interval=interval,
        strategy_version=strategy_version, config_fingerprint=config_fingerprint)
    return cfg


def preflight(cfg: EnvConfig, *, allow_mainnet: Optional[bool] = None) -> List[str]:
    """
    فحص بدء التشغيل. يرمي EnvironmentError_ عند أي تعارض.
    يُرجع قائمة تحذيرات غير مانعة.
    """
    warnings: List[str] = []
    env = cfg.env

    # ── endpoint يطابق البيئة
    expected = ENDPOINTS[env]
    if cfg.endpoint != expected:
        raise EnvironmentError_(
            f"تعارض endpoint: البيئة {env.value} تتوقع {expected} "
            f"لكن المضبوط {cfg.endpoint}")

    if env is Env.TESTNET and cfg.endpoint == MAINNET_URL:
        raise EnvironmentError_("⛔ بيئة testnet تشير إلى Mainnet — إيقاف فوري")

    if env is Env.LIVE:
        from ..execution.binance_client import (mainnet_allowed,
                                                mainnet_block_reason)
        allowed = allow_mainnet if allow_mainnet is not None else mainnet_allowed()
        if not allowed:
            raise EnvironmentError_(
                f"⛔ بيئة live معطَّلة. {mainnet_block_reason()}")

    # ── المفاتيح
    if env.touches_exchange:
        if not cfg.api_key or not cfg.api_secret:
            kv, sv = KEY_VARS[env]
            sentinel = 'TESTNET_KEYS_MISSING' if env is Env.TESTNET else \
                f'{env.value.upper()}_KEYS_MISSING'
            raise EnvironmentError_(
                f"{sentinel}: مفاتيح {env.value} مفقودة. المطلوب "
                f"ضبط {kv} و {sv} في .env")
        # منع تبادل المفاتيح بين البيئات
        other = Env.LIVE if env is Env.TESTNET else Env.TESTNET
        okv, osv = KEY_VARS[other]
        other_secret = os.getenv(osv, '').strip()
        if other_secret and fingerprint(other_secret) == cfg.key_fingerprint:
            raise EnvironmentError_(
                f"⛔ مفتاح {env.value} مطابق لمفتاح {other.value} — "
                f"استخدم مفاتيح مستقلة")
    else:
        if cfg.api_key or cfg.api_secret:
            warnings.append(
                f"بيئة {env.value} لا تحتاج مفاتيح لكنها مضبوطة — ستُتجاهل")

    # ── القاعدة معزولة
    for other_env in Env:
        if other_env is env:
            continue
        other_db = os.path.join(os.path.dirname(os.path.dirname(cfg.db_path)),
                                other_env.value, f'{other_env.value}.db')
        if os.path.abspath(cfg.db_path) == os.path.abspath(other_db):
            raise EnvironmentError_(
                f"⛔ قاعدة {env.value} تتقاطع مع {other_env.value}")

    return warnings


def print_banner(cfg: EnvConfig, warnings: Optional[List[str]] = None) -> str:
    icons = {Env.SHADOW: '👁️ ظل (تسجيل فقط)', Env.MONITOR: '📝 مراقبة (بلا أوامر)',
             Env.PAPER: '📄 ورقي (محاكاة محافظة)', Env.TESTNET: '🧪 TESTNET',
             Env.LIVE: '🔴 أموال حقيقية'}
    d = cfg.safe_dict()
    lines = ['', '=' * 62, f"  {icons[cfg.env]}", '=' * 62]
    for k, v in d.items():
        lines.append(f"  {k:22s} {v}")
    for w in (warnings or []):
        lines.append(f"  ⚠️  {w}")
    lines.append('=' * 62)
    return '\n'.join(lines)
