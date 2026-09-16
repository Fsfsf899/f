"""
إعدادات التنفيذ الحي — المراحل 3 و4.
=====================================
Live معطَّل افتراضياً، ولا يُفعَّل بمتغير واحد.

مفاتيح كل بيئة مستقلة تماماً. مفتاح Mainnet في Paper أو Testnet
يُرفض، والعكس. الفحص ببصمة SHA-256 لا بالقيمة.
"""
import hashlib
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

TESTNET_URL = 'https://testnet.binance.vision'
MAINNET_URL = 'https://api.binance.com'


class Stage(str, Enum):
    SHADOW = 'shadow'
    MONITOR = 'monitor'
    PAPER = 'paper'
    TESTNET = 'testnet'
    LIVE_CANARY = 'live_canary'
    LIVE = 'live'

    @property
    def sends_orders(self) -> bool:
        return self in (Stage.PAPER, Stage.TESTNET,
                        Stage.LIVE_CANARY, Stage.LIVE)

    @property
    def real_money(self) -> bool:
        return self in (Stage.LIVE_CANARY, Stage.LIVE)

    @property
    def touches_exchange(self) -> bool:
        return self in (Stage.TESTNET, Stage.LIVE_CANARY, Stage.LIVE)


# متغيرات مستقلة لكل بيئة — لا تُشارك أبداً
STAGE_KEYS: Dict[Stage, Tuple[str, str]] = {
    Stage.TESTNET: ('TESTNET_API_KEY', 'TESTNET_API_SECRET'),
    Stage.LIVE_CANARY: ('MAINNET_API_KEY', 'MAINNET_API_SECRET'),
    Stage.LIVE: ('MAINNET_API_KEY', 'MAINNET_API_SECRET'),
}

STAGE_ENDPOINT: Dict[Stage, Optional[str]] = {
    Stage.SHADOW: None, Stage.MONITOR: None, Stage.PAPER: None,
    Stage.TESTNET: TESTNET_URL,
    Stage.LIVE_CANARY: MAINNET_URL, Stage.LIVE: MAINNET_URL,
}

# الترقية اليدوية فقط — لا انتقال تلقائي
STAGE_ORDER = [Stage.SHADOW, Stage.MONITOR, Stage.PAPER, Stage.TESTNET,
               Stage.LIVE_CANARY, Stage.LIVE]


class LiveConfigError(RuntimeError):
    """إعداد تنفيذ غير آمن — يمنع أي طلب للمنصة."""


def fingerprint(secret: str) -> str:
    return 'none' if not secret else hashlib.sha256(
        secret.encode()).hexdigest()[:12]


def _env(n: str, d: str = '') -> str:
    return os.getenv(n, d).strip()


def _bool(n: str) -> bool:
    return _env(n).lower() in ('1', 'true', 'yes')


def _f(n: str, d: float) -> float:
    try:
        return float(_env(n) or d)
    except ValueError:
        return d


def _i(n: str, d: int) -> int:
    try:
        return int(_env(n) or d)
    except ValueError:
        return d


@dataclass
class LiveLimits:
    """حدود صريحة قابلة للمراجعة — ليست توصية بحجم مناسب لك."""
    max_risk_per_trade_pct: float = field(
        default_factory=lambda: _f('LIVE_MAX_RISK_PER_TRADE_PCT', 0.25))
    max_daily_loss_pct: float = field(
        default_factory=lambda: _f('LIVE_MAX_DAILY_LOSS_PCT', 1.0))
    max_position_notional: float = field(
        default_factory=lambda: _f('LIVE_MAX_POSITION_NOTIONAL', 25.0))
    max_total_exposure_pct: float = field(
        default_factory=lambda: _f('LIVE_MAX_TOTAL_EXPOSURE_PCT', 10.0))
    max_cluster_exposure_pct: float = field(
        default_factory=lambda: _f('LIVE_MAX_CLUSTER_EXPOSURE_PCT', 10.0))
    max_open_positions: int = field(
        default_factory=lambda: _i('LIVE_MAX_OPEN_POSITIONS', 1))
    max_consecutive_losses: int = field(
        default_factory=lambda: _i('LIVE_MAX_CONSECUTIVE_LOSSES', 3))
    max_daily_trades: int = field(
        default_factory=lambda: _i('LIVE_MAX_DAILY_TRADES', 3))
    min_reserve_quote: float = field(
        default_factory=lambda: _f('LIVE_MIN_RESERVE_QUOTE', 0.0))
    max_signal_age_multiple: float = field(
        default_factory=lambda: _f('LIVE_MAX_SIGNAL_AGE_MULTIPLE', 2.0))

    def to_dict(self) -> Dict:
        return dict(self.__dict__)


@dataclass
class LiveConfig:
    stage: Stage = field(default_factory=lambda: Stage(
        _env('TRADING_ENVIRONMENT', 'paper').lower()
        if _env('TRADING_ENVIRONMENT', 'paper').lower()
        in [s.value for s in Stage] else 'paper'))
    live_enabled: bool = field(
        default_factory=lambda: _bool('LIVE_TRADING_ENABLED'))
    risk_accepted: bool = field(
        default_factory=lambda: _env('I_HAVE_REVIEWED_AND_ACCEPT_RISK') == 'yes')
    allowed_symbols: List[str] = field(default_factory=lambda: [
        s.strip().upper() for s in
        _env('LIVE_ALLOWED_SYMBOLS', 'BTCUSDT').split(',') if s.strip()])
    limits: LiveLimits = field(default_factory=LiveLimits)
    base_dir: str = field(default_factory=lambda: os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

    # ── المفاتيح ──
    @property
    def key_vars(self) -> Optional[Tuple[str, str]]:
        return STAGE_KEYS.get(self.stage)

    @property
    def api_key(self) -> str:
        kv = self.key_vars
        return _env(kv[0]) if kv else ''

    @property
    def api_secret(self) -> str:
        kv = self.key_vars
        return _env(kv[1]) if kv else ''

    @property
    def key_fingerprint(self) -> str:
        return fingerprint(self.api_secret)

    @property
    def endpoint(self) -> Optional[str]:
        return STAGE_ENDPOINT[self.stage]

    @property
    def db_path(self) -> str:
        d = os.path.join(self.base_dir, 'data', self.stage.value)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f'{self.stage.value}.db')

    @property
    def log_path(self) -> str:
        d = os.path.join(self.base_dir, 'logs')
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f'live_{self.stage.value}.log')

    def safe_dict(self) -> Dict:
        """بلا أسرار."""
        return {'stage': self.stage.value, 'endpoint': self.endpoint or 'local',
                'live_enabled': self.live_enabled,
                'risk_accepted': self.risk_accepted,
                'api_key_fingerprint': self.key_fingerprint,
                'allowed_symbols': self.allowed_symbols,
                'database': os.path.basename(self.db_path),
                'limits': self.limits.to_dict()}

    # ── فصل المفاتيح ──
    def key_separation_problems(self) -> List[str]:
        """
        يمنع خلط المفاتيح بين البيئات — بالبصمة لا بالقيمة.
        """
        out: List[str] = []
        tn = fingerprint(_env('TESTNET_API_SECRET'))
        mn = fingerprint(_env('MAINNET_API_SECRET'))

        if tn != 'none' and tn == mn:
            out.append('مفتاح Testnet مطابق لمفتاح Mainnet — افصلهما')

        if self.stage in (Stage.PAPER, Stage.SHADOW, Stage.MONITOR):
            if _env('MAINNET_API_KEY') or _env('MAINNET_API_SECRET'):
                out.append(
                    f'مفاتيح Mainnet مضبوطة في بيئة {self.stage.value} — '
                    f'أزلها من هذه البيئة')
        if self.stage is Stage.TESTNET and mn != 'none' and self.key_fingerprint == mn:
            out.append('مفتاح Mainnet مستخدم في Testnet')
        if self.stage.real_money and tn != 'none' and self.key_fingerprint == tn:
            out.append('مفتاح Testnet مستخدم في Live')
        return out


@dataclass
class GateCheck:
    name: str
    passed: bool
    detail: str = ''
    blocking: bool = True


@dataclass
class LiveActivation:
    allowed: bool
    checks: List[GateCheck] = field(default_factory=list)

    @property
    def failures(self) -> List[str]:
        return [c.name for c in self.checks if not c.passed and c.blocking]

    def to_dict(self) -> Dict:
        return {'allowed': self.allowed, 'failures': self.failures,
                'checks': [c.__dict__ for c in self.checks]}

    def render(self) -> str:
        L = ['', '=' * 64, '  بوابة تفعيل التنفيذ الحي', '=' * 64]
        for c in self.checks:
            L.append(f"  {'PASS' if c.passed else 'FAIL'}  "
                     f"{c.name:34s} {c.detail}")
        L += ['', f"  النتيجة: {'✅ مسموح' if self.allowed else '⛔ ممنوع'}"]
        if not self.allowed:
            L.append('  الحي لا يعمل حتى تُستوفى كل الشروط.')
        L.append('=' * 64)
        return '\n'.join(L)


def check_activation(cfg: LiveConfig, *,
                     account_info: Optional[Dict] = None,
                     paper_gate_passed: Optional[bool] = None,
                     testnet_gate_passed: Optional[bool] = None,
                     unresolved_intents: Optional[int] = None,
                     reconciliation_ok: Optional[bool] = None,
                     kill_switch_on: Optional[bool] = None,
                     db_healthy: Optional[bool] = None,
                     clock_offset_ms: Optional[int] = None
                     ) -> LiveActivation:
    """
    بوابة الحي — المرحلة الثالثة.
    غياب دليل ليس نجاحاً: `None` يُعامَل كفشل، لا كتخطٍّ.
    """
    ck: List[GateCheck] = []

    def add(name, ok, detail='', blocking=True):
        ck.append(GateCheck(name, bool(ok), str(detail), blocking))

    if not cfg.stage.real_money:
        add('STAGE_IS_LIVE', False,
            f'البيئة {cfg.stage.value} ليست حية — لا حاجة للبوابة')
        return LiveActivation(False, ck)

    # ── قفل المصدر (بوابة مستقلة عن البيئة)
    from ..execution.binance_client import (MAINNET_ENABLED_IN_SOURCE,
                                            mainnet_block_reason)
    add('MAINNET_ENABLED_IN_SOURCE', MAINNET_ENABLED_IN_SOURCE,
        mainnet_block_reason() or 'مفتوح في المصدر')

    add('LIVE_TRADING_ENABLED', cfg.live_enabled, str(cfg.live_enabled))
    add('RISK_ACCEPTED', cfg.risk_accepted,
        'I_HAVE_REVIEWED_AND_ACCEPT_RISK=yes مطلوب')
    add('API_KEY_PRESENT', bool(cfg.api_key and cfg.api_secret),
        f'fingerprint={cfg.key_fingerprint}')

    seps = cfg.key_separation_problems()
    add('KEY_SEPARATION', not seps, '; '.join(seps))

    add('ENDPOINT_MATCHES_STAGE', cfg.endpoint == MAINNET_URL,
        str(cfg.endpoint))

    if account_info is None:
        add('NO_WITHDRAWAL_PERMISSION', False, 'حالة الحساب غير مقروءة')
        add('CAN_TRADE', False, 'حالة الحساب غير مقروءة')
    else:
        add('NO_WITHDRAWAL_PERMISSION',
            account_info.get('canWithdraw') is False,
            f"canWithdraw={account_info.get('canWithdraw')}")
        add('CAN_TRADE', bool(account_info.get('canTrade')),
            f"canTrade={account_info.get('canTrade')}")
        add('IP_RESTRICTED', bool(account_info.get('ipRestrict')),
            'تقييد IP غير مفعّل', blocking=False)

    add('PAPER_GATE_PASSED', paper_gate_passed is True,
        'لم تُشغَّل' if paper_gate_passed is None else str(paper_gate_passed))
    add('TESTNET_GATE_PASSED', testnet_gate_passed is True,
        'لم تُشغَّل' if testnet_gate_passed is None else str(testnet_gate_passed))
    add('NO_UNRESOLVED_UNKNOWN', unresolved_intents == 0,
        'غير مقروء' if unresolved_intents is None else str(unresolved_intents))
    add('RECONCILIATION_OK', reconciliation_ok is True,
        'غير مقروءة' if reconciliation_ok is None else str(reconciliation_ok))
    add('KILL_SWITCH_OFF', kill_switch_on is False,
        'غير مقروء' if kill_switch_on is None else str(kill_switch_on))
    add('DATABASE_HEALTHY', db_healthy is True,
        'غير مقروءة' if db_healthy is None else str(db_healthy))
    add('CLOCK_SYNCHRONIZED',
        clock_offset_ms is not None and abs(clock_offset_ms) < 1000,
        'غير مزامن' if clock_offset_ms is None else f'{clock_offset_ms}ms')

    return LiveActivation(all(c.passed for c in ck if c.blocking), ck)
