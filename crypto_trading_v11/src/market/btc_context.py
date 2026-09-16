"""
سياق BTC — البند 22.
عامل مخاطرة، لا دليل حتمي. يُستخدم لمنع التداول في ظروف سيئة،
ولا يُستخدم وحده لتوليد إشارة شراء.
"""
import numpy as np
from dataclasses import dataclass
from typing import Optional, Dict
from ..data.types import OHLCV
from ..core.config import RegimeConfig
from .regime import detect_at, TRENDING_BEAR, HIGH_VOL


@dataclass
class BTCContext:
    available: bool
    regime: str = 'UNKNOWN'
    trend_ok: bool = True
    volatility_pct: float = 0.0
    momentum_pct: float = 0.0
    risk_level: str = 'UNKNOWN'      # LOW | MEDIUM | HIGH | UNKNOWN
    note: str = ''

    def to_dict(self) -> Dict:
        return {k: (round(v, 4) if isinstance(v, float) else v)
                for k, v in self.__dict__.items()}


def evaluate(btc: Optional[OHLCV], at_time_ms: int,
             cfg: Optional[RegimeConfig] = None,
             adx: Optional[np.ndarray] = None) -> BTCContext:
    """
    يقيّم BTC عند لحظة زمنية.
    مهم: يستخدم آخر شمعة BTC **مغلقة قبل** at_time_ms — لا مطابقة تقريبية.
    """
    if btc is None or len(btc) < 60:
        return BTCContext(False, note='بيانات BTC غير متاحة')

    cfg = cfg or RegimeConfig()
    close_times = btc.open_time + btc.interval_ms - 1
    usable = np.where(close_times <= at_time_ms)[0]
    if len(usable) < cfg.lookback + 5:
        return BTCContext(False, note='شموع BTC غير كافية عند هذا الوقت')

    i = int(usable[-1])
    reg = detect_at(btc.close, i, cfg, adx)
    c = btc.close
    mom = float((c[i] - c[i - 20]) / c[i - 20] * 100) if i >= 20 else 0.0

    if reg.regime == TRENDING_BEAR or reg.volatility_pct > cfg.vol_high_pct:
        risk, ok = 'HIGH', False
    elif reg.regime == HIGH_VOL or mom < -5:
        risk, ok = 'MEDIUM', False
    else:
        risk, ok = 'LOW', True

    return BTCContext(True, reg.regime, ok, reg.volatility_pct, mom, risk)
