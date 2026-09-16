"""
عقد الإشارة — المرحلة الأولى.
==============================
`TradeSignal` **مجمَّد** (frozen). سبب التجميد ليس أناقة: إشارة قابلة
للتعديل بعد إنتاجها تعني أن ما يُنفَّذ قد يخالف ما قرره المحرك.

مصدر الحقيقة الوحيد: `src.signals.engine.SignalEngine`.
لا يُقبل قاموس من HTTP ولا JSON ولا ملف يدوي — فقط كائن أنتجه المحرك
ويحمل `payload_hash` يطابق محتواه.
"""
import hashlib
import json
import math
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Optional, Tuple, List

BUY = 'BUY'
LONG = 'LONG'
ACTION_ENTER = 'ENTER'


def _finite(v) -> bool:
    if v is None:
        return True
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return math.isfinite(f)


@dataclass(frozen=True)
class TradeSignal:
    signal_id: str
    strategy_version: str
    environment: str
    symbol: str
    timeframe: str
    candle_close_ts: int
    generated_at: int
    side: str
    action: str
    entry_reference_price: Optional[float]
    stop_loss: Optional[float]
    take_profit: Optional[float]
    risk_reward: Optional[float]
    calibrated_probability: Optional[float]
    probability_source: Optional[str]
    score: Optional[float]
    no_trade_reasons: Tuple[str, ...]
    data_quality: Dict[str, Any]
    payload_hash: str
    config_fingerprint: str = ''
    market_regime: str = 'UNKNOWN'

    # ── الهوية ──
    @staticmethod
    def build_signal_id(symbol: str, timeframe: str, candle_close_ts: int,
                        strategy_version: str, config_fingerprint: str) -> str:
        """
        معرّف حتمي: نفس الشمعة + نفس النسخة + نفس الإعدادات ⇒ نفس المعرّف.
        هذا ما يجعل إعادة التقييم لا تُنتج صفقة ثانية.
        """
        seed = (f'{symbol}|{timeframe}|{candle_close_ts}|'
                f'{strategy_version}|{config_fingerprint}')
        return 'sig_' + hashlib.sha256(seed.encode()).hexdigest()[:24]

    def canonical_payload(self) -> str:
        d = asdict(self)
        d.pop('payload_hash', None)
        d['no_trade_reasons'] = list(self.no_trade_reasons)
        return json.dumps(d, sort_keys=True, ensure_ascii=False,
                          separators=(',', ':'), default=str)

    def compute_hash(self) -> str:
        return hashlib.sha256(self.canonical_payload().encode()).hexdigest()

    def hash_matches(self) -> bool:
        return bool(self.payload_hash) and self.payload_hash == self.compute_hash()

    def to_dict(self) -> Dict:
        d = asdict(self)
        d['no_trade_reasons'] = list(self.no_trade_reasons)
        return d


@dataclass
class SignalRejection:
    code: str
    detail: str = ''


ACCEPT_CHECKS = (
    'SIGNAL_ID_PRESENT', 'STRATEGY_VERSION_PRESENT', 'PAYLOAD_HASH_VALID',
    'ENVIRONMENT_MATCH', 'CANDLE_CLOSED', 'DATA_FRESH', 'NO_NAN_OR_INF',
    'NO_TRADE_REASONS_EMPTY', 'SYMBOL_ALLOWED', 'SIDE_ALLOWED',
    'ACTION_ALLOWED', 'STOP_LOSS_PRESENT', 'STOP_LOSS_SANE',
    'ENTRY_PRICE_PRESENT', 'RISK_REWARD_SANE', 'DATA_QUALITY_OK',
)


def validate_signal(sig: TradeSignal, *, environment: str,
                    allowed_symbols: List[str],
                    allowed_sides: Tuple[str, ...] = (BUY, LONG),
                    now_ms: Optional[int] = None,
                    interval_ms: Optional[int] = None,
                    max_age_multiple: float = 2.0,
                    min_data_quality: float = 0.80,
                    max_stop_distance_pct: float = 10.0,
                    min_stop_distance_pct: float = 0.10
                    ) -> Tuple[bool, List[Dict]]:
    """
    شروط قبول الإشارة. يُرجع (مقبولة، نتيجة كل فحص).
    أي فشل ⇒ لا أمر، والسبب يُسجَّل.
    """
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    out: List[Dict] = []

    def chk(name: str, ok: bool, detail: str = ''):
        out.append({'check': name, 'passed': bool(ok), 'detail': detail})

    chk('SIGNAL_ID_PRESENT', bool(sig.signal_id and
                                  sig.signal_id.startswith('sig_')),
        sig.signal_id or '')
    chk('STRATEGY_VERSION_PRESENT', bool(sig.strategy_version),
        sig.strategy_version or '')
    chk('PAYLOAD_HASH_VALID', sig.hash_matches(),
        'البصمة لا تطابق المحتوى' if not sig.hash_matches() else '')
    chk('ENVIRONMENT_MATCH', sig.environment == environment,
        f'{sig.environment} != {environment}')

    closed = bool(sig.candle_close_ts) and sig.candle_close_ts <= now_ms
    chk('CANDLE_CLOSED', closed,
        f'close_ts={sig.candle_close_ts} now={now_ms}')

    if interval_ms:
        age = now_ms - sig.candle_close_ts
        chk('DATA_FRESH', 0 <= age <= interval_ms * max_age_multiple,
            f'عمر {age}ms')
    else:
        chk('DATA_FRESH', True, 'لا إطار زمني — تخطٍ')

    numeric = (sig.entry_reference_price, sig.stop_loss, sig.take_profit,
               sig.risk_reward, sig.calibrated_probability, sig.score)
    chk('NO_NAN_OR_INF', all(_finite(v) for v in numeric))

    chk('NO_TRADE_REASONS_EMPTY', len(sig.no_trade_reasons) == 0,
        ','.join(sig.no_trade_reasons[:4]))
    chk('SYMBOL_ALLOWED', sig.symbol in allowed_symbols, sig.symbol)
    chk('SIDE_ALLOWED', sig.side.upper() in allowed_sides, sig.side)
    chk('ACTION_ALLOWED', sig.action.upper() == ACTION_ENTER, sig.action)

    chk('ENTRY_PRICE_PRESENT',
        sig.entry_reference_price is not None and sig.entry_reference_price > 0)
    chk('STOP_LOSS_PRESENT', sig.stop_loss is not None and sig.stop_loss > 0)

    sane = False
    detail = 'مفقود'
    if (sig.stop_loss and sig.entry_reference_price
            and sig.entry_reference_price > 0):
        dist = (sig.entry_reference_price - sig.stop_loss) / sig.entry_reference_price * 100
        sane = (sig.stop_loss < sig.entry_reference_price
                and min_stop_distance_pct <= dist <= max_stop_distance_pct)
        detail = f'{dist:.3f}%'
    chk('STOP_LOSS_SANE', sane, detail)

    chk('RISK_REWARD_SANE',
        sig.risk_reward is None or (_finite(sig.risk_reward)
                                    and sig.risk_reward > 0),
        str(sig.risk_reward))

    dq = sig.data_quality or {}
    score = dq.get('score')
    chk('DATA_QUALITY_OK',
        score is not None and _finite(score) and float(score) >= min_data_quality,
        f'{score}')

    return all(c['passed'] for c in out), out


def from_engine_signal(engine_sig, *, environment: str,
                       config_fingerprint: str,
                       interval_ms: int,
                       data_quality: Optional[Dict] = None) -> TradeSignal:
    """
    المحوّل الوحيد المسموح: من `src.signals.engine.Signal` إلى `TradeSignal`.

    لا توجد دالة تبني TradeSignal من قاموس خارجي — بالتصميم. أي مسار
    آخر لإنشاء إشارة يعني إمكانية حقن صفقة لم يقرّرها المحرك.
    """
    close_ts = int(engine_sig.timestamp) + interval_ms - 1
    sid = TradeSignal.build_signal_id(
        engine_sig.symbol, engine_sig.interval, close_ts,
        engine_sig.strategy_version, config_fingerprint)

    base = TradeSignal(
        signal_id=sid,
        strategy_version=engine_sig.strategy_version,
        environment=environment,
        symbol=engine_sig.symbol,
        timeframe=engine_sig.interval,
        candle_close_ts=close_ts,
        generated_at=int(time.time() * 1000),
        side=LONG,                       # Spot: شراء فقط
        action=(ACTION_ENTER if engine_sig.decision == BUY else 'NONE'),
        entry_reference_price=engine_sig.entry,
        stop_loss=engine_sig.stop_loss,
        take_profit=engine_sig.take_profit,
        risk_reward=engine_sig.risk_reward,
        calibrated_probability=engine_sig.calibrated_probability,
        probability_source=engine_sig.probability_source,
        score=engine_sig.score,
        no_trade_reasons=tuple(engine_sig.reasons or ()),
        data_quality=(data_quality or {'score': engine_sig.data_quality}),
        payload_hash='',
        config_fingerprint=config_fingerprint,
        market_regime=engine_sig.regime,
    )
    # البصمة تُحسب بعد اكتمال الحقول ثم تُثبَّت
    return TradeSignal(**{**base.to_dict(),
                          'no_trade_reasons': base.no_trade_reasons,
                          'payload_hash': base.compute_hash()})
