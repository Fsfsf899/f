"""
الأطر الزمنية المتعددة — البند 11.
==================================
الخطر: استخدام شمعة 4h لم تُغلق بعد أثناء تحليل 15m.

الحل: التجميع يقبل الشمعة الأكبر فقط إذا اكتملت مجموعتها كاملة،
وتصبح متاحة اعتباراً من الشمعة الصغيرة التالية لإغلاقها.
"""
import numpy as np
from typing import Dict, Optional, Tuple
from ..data.types import OHLCV, INTERVAL_MS

HIERARCHY = ['5m', '15m', '1h', '4h', '1d']
ROLE = {'5m': 'entry_timing', '15m': 'short_structure',
        '1h': 'trend', '4h': 'regime', '1d': 'htf_bias'}


def aggregate(data: OHLCV, target: str) -> Tuple[OHLCV, np.ndarray]:
    """
    يجمّع إطاراً صغيراً إلى أكبر.
    يُرجع (بيانات الإطار الأكبر، خريطة available_from).

    available_from[i] = فهرس آخر شمعة كبيرة **مغلقة ومتاحة** عند
    الشمعة الصغيرة i، أو -1 إن لم تتوفر بعد.
    """
    src_ms = data.interval_ms
    tgt_ms = INTERVAL_MS[target]
    if tgt_ms < src_ms:
        raise ValueError("الإطار الهدف أصغر من المصدر")
    if tgt_ms % src_ms != 0:
        raise ValueError(f"{target} ليس مضاعفاً لـ {data.interval}")

    bucket = (data.open_time // tgt_ms) * tgt_ms
    uniq = np.unique(bucket)
    per_bucket = tgt_ms // src_ms

    ot, o, h, l, c, v = [], [], [], [], [], []
    complete_end: Dict[int, int] = {}   # bucket -> آخر فهرس صغير فيها

    for b in uniq:
        m = np.where(bucket == b)[0]
        if len(m) < per_bucket:
            continue                      # مجموعة ناقصة = شمعة لم تُغلق
        ot.append(int(b))
        o.append(float(data.open[m[0]]))
        h.append(float(data.high[m].max()))
        l.append(float(data.low[m].min()))
        c.append(float(data.close[m[-1]]))
        v.append(float(data.volume[m].sum()))
        complete_end[int(b)] = int(m[-1])

    htf = OHLCV(data.symbol, target,
                np.array(ot, dtype=np.int64), np.array(o), np.array(h),
                np.array(l), np.array(c), np.array(v),
                source=f"agg:{data.interval}->{target}", fetched_at=data.fetched_at)

    available = np.full(len(data), -1, dtype=int)
    if len(htf):
        ends = np.array([complete_end[int(b)] for b in htf.open_time])
        for j, e in enumerate(ends):
            # الشمعة الكبيرة j تصبح متاحة من الشمعة الصغيرة e+1
            if e + 1 < len(data):
                available[e + 1:] = j
    return htf, available


class MultiTimeframe:
    """
    يبني عرضاً متعدد الأطر من إطار أساسي واحد.
    كل قيمة أعلى تُقرأ عبر available_from — استحالة استخدام شمعة مفتوحة.
    """
    def __init__(self, base: OHLCV, targets=None):
        self.base = base
        self.frames: Dict[str, OHLCV] = {}
        self.avail: Dict[str, np.ndarray] = {}
        b_i = HIERARCHY.index(base.interval) if base.interval in HIERARCHY else -1
        for t in (targets or HIERARCHY):
            if t == base.interval or t not in INTERVAL_MS:
                continue
            if INTERVAL_MS[t] <= base.interval_ms:
                continue
            if INTERVAL_MS[t] % base.interval_ms != 0:
                continue
            try:
                f, a = aggregate(base, t)
                if len(f) >= 60:
                    self.frames[t] = f
                    self.avail[t] = a
            except ValueError:
                continue

    def index_at(self, tf: str, base_idx: int) -> int:
        """فهرس آخر شمعة مغلقة متاحة في الإطار tf عند الشمعة الأساسية base_idx."""
        if tf not in self.avail:
            return -1
        return int(self.avail[tf][base_idx])

    def bias_at(self, tf: str, base_idx: int, fast: int = 20, slow: int = 50) -> Dict:
        """انحياز الإطار الأعلى — من شموع مغلقة حصراً."""
        from ..indicators.engine import ema
        j = self.index_at(tf, base_idx)
        if j < slow:
            return {'tf': tf, 'bias': 'UNKNOWN', 'aligned_long': True, 'available': False}
        c = self.frames[tf].close[:j + 1]
        ef, es = ema(c, fast), ema(c, slow)
        if np.isnan(ef[-1]) or np.isnan(es[-1]) or es[-1] == 0:
            return {'tf': tf, 'bias': 'UNKNOWN', 'aligned_long': True, 'available': False}
        gap = (ef[-1] - es[-1]) / es[-1] * 100
        bias = 'NEUTRAL' if abs(gap) < 0.3 else ('BULLISH' if gap > 0 else 'BEARISH')
        return {'tf': tf, 'bias': bias, 'gap_pct': round(float(gap), 3),
                'aligned_long': bias in ('BULLISH', 'NEUTRAL'),
                'available': True, 'htf_index': j}
