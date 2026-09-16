"""
حالة السوق — البند 14.
كل العتبات من Config. لا رقم مكتوب داخل المنطق.
سببي بالكامل: نافذة تنتهي عند i.
"""
import numpy as np
from dataclasses import dataclass
from typing import Optional, Dict
from ..core.config import RegimeConfig

TRENDING_BULL = 'TRENDING_BULLISH'
TRENDING_BEAR = 'TRENDING_BEARISH'
RANGING       = 'RANGING'
HIGH_VOL      = 'HIGH_VOLATILITY'
LOW_VOL       = 'LOW_VOLATILITY'
TRANSITION    = 'TRANSITION'
UNKNOWN       = 'UNKNOWN'

LONG_FRIENDLY = {TRENDING_BULL}


@dataclass
class Regime:
    regime: str
    confidence: float
    long_friendly: bool
    slope_pct: float = 0.0
    efficiency: float = 0.0
    volatility_pct: float = 0.0
    adx: float = 0.0

    def to_dict(self) -> Dict:
        return {k: (round(v, 4) if isinstance(v, float) else v)
                for k, v in self.__dict__.items()}


# مضاعف معايرة التقلب حسب الإطار: تقلب شمعة 15د أصغر بطبيعته من 1د.
# بدون هذا، عتبة ثابتة تصنّف كل الأطر الكبيرة "تقلب منخفض".
INTERVAL_VOL_SCALE = {
    '1m': 0.30, '3m': 0.45, '5m': 0.55, '15m': 0.75, '30m': 0.85,
    '1h': 1.00, '2h': 1.20, '4h': 1.50, '6h': 1.70, '12h': 2.20, '1d': 3.00,
}


def detect_at(close: np.ndarray, idx: int, cfg: RegimeConfig,
              adx: Optional[np.ndarray] = None,
              interval: str = '1h') -> Regime:
    lb = cfg.lookback
    if idx < lb:
        return Regime(UNKNOWN, 0.0, False)

    w = close[idx - lb: idx + 1].astype(float)
    if np.any(~np.isfinite(w)) or w[0] <= 0:
        return Regime(UNKNOWN, 0.0, False)

    x = np.arange(len(w), dtype=float)
    slope = float(np.polyfit(x, w, 1)[0])
    slope_pct = slope * len(w) / w[0] * 100

    net = abs(w[-1] - w[0])
    total = float(np.sum(np.abs(np.diff(w)))) + 1e-12
    efficiency = net / total

    rets = np.diff(w) / w[:-1]
    vol_pct = float(np.std(rets) * 100)

    adx_val = float(adx[idx]) if (adx is not None and idx < len(adx)
                                  and np.isfinite(adx[idx])) else 0.0

    scale = INTERVAL_VOL_SCALE.get(interval, 1.0)
    vol_high = cfg.vol_high_pct * scale
    vol_low = cfg.vol_low_pct * scale

    # التقلب المفرط يلغي أي حكم اتجاهي — السعر عشوائي وليس متجهاً
    if vol_pct > vol_high:
        return Regime(HIGH_VOL, float(min(vol_pct / (vol_high * 2), 1.0)),
                      False, slope_pct, efficiency, vol_pct, adx_val)

    # الاتجاه يُفحص قبل "التقلب المنخفض": اتجاه نظيف هادئ هو أفضل
    # بيئة للتداول، ولا يصح تصنيفه ميتاً لمجرد هدوئه.
    # الاتجاه يتطلب ميلاً معتبراً (يحدد الوجود والاتجاه)، ثم تأكيداً
    # من أحد مقياسي النظافة: كفاءة الحركة أو ADX قوي.
    # اشتراطهما معاً كان ينقض اتجاهات واضحة (ADX>50) لفارق كفاءة ضئيل.
    slope_ok = abs(slope_pct) >= cfg.slope_trend_min_pct
    clean_ok = (efficiency >= cfg.efficiency_trend_min
                or adx_val >= cfg.adx_strong_min)
    trending = slope_ok and clean_ok and adx_val >= cfg.adx_trend_min
    if not trending and vol_pct < vol_low:
        return Regime(LOW_VOL, float(min(1.0 - vol_pct / max(vol_low, 1e-9), 1.0)),
                      False, slope_pct, efficiency, vol_pct, adx_val)

    if trending:
        r = TRENDING_BULL if slope_pct > 0 else TRENDING_BEAR
        conf = float(min(efficiency / max(cfg.efficiency_trend_min, 1e-9) * 0.5 +
                         min(adx_val / 40.0, 1.0) * 0.5, 1.0))
        return Regime(r, conf, r == TRENDING_BULL, slope_pct, efficiency, vol_pct, adx_val)

    if efficiency >= cfg.transition_efficiency:
        return Regime(TRANSITION, float(efficiency), False,
                      slope_pct, efficiency, vol_pct, adx_val)

    return Regime(RANGING, float(min(1.0 - efficiency, 1.0)), False,
                  slope_pct, efficiency, vol_pct, adx_val)


def detect_series(close: np.ndarray, cfg: RegimeConfig,
                  adx: Optional[np.ndarray] = None,
                  interval: str = '1h') -> np.ndarray:
    """سلسلة رموز الحالة لكل شمعة (للباكتست)."""
    return np.array([detect_at(close, i, cfg, adx, interval).regime
                     for i in range(len(close))], dtype=object)
