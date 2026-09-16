"""
التحقق من جودة البيانات — البند 9.
يفحص: نقص، تكرار، OHLC غير صالح، ترتيب، طوابع مستقبلية، بيانات بايتة.
لا يخترع بيانات ولا يملأ الفجوات صامتاً — يبلّغ فقط.
"""
import time
import numpy as np
from typing import Optional
from .types import OHLCV, DataQuality
from ..core.config import DataConfig


def validate(data: OHLCV, cfg: Optional[DataConfig] = None,
             now_ms: Optional[int] = None) -> DataQuality:
    cfg = cfg or DataConfig()
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    n = len(data)
    issues = []

    if n == 0:
        return DataQuality(0.0, 0, 0, 0, 0, 0, ["لا توجد بيانات"])

    ot = data.open_time
    step = data.interval_ms

    # ── الترتيب الزمني
    diffs = np.diff(ot)
    out_of_order = int(np.sum(diffs <= 0))
    if out_of_order:
        issues.append(f"{out_of_order} شمعة خارج الترتيب الزمني")

    # ── التكرار
    duplicates = int(n - len(np.unique(ot)))
    if duplicates:
        issues.append(f"{duplicates} شمعة مكررة")

    # ── الشموع المفقودة
    if n > 1:
        span = int(ot[-1] - ot[0])
        expected = span // step + 1
        missing = max(0, expected - n)
    else:
        expected, missing = 1, 0
    gap_ratio = missing / max(expected, 1)
    if missing:
        issues.append(f"{missing} شمعة مفقودة ({gap_ratio*100:.2f}%)")

    # ── صلاحية OHLC
    o, h, l, c, v = data.open, data.high, data.low, data.close, data.volume
    bad = (
        (h < l) | (h < o) | (h < c) | (l > o) | (l > c) |
        (o <= 0) | (h <= 0) | (l <= 0) | (c <= 0) |
        ~np.isfinite(o) | ~np.isfinite(h) | ~np.isfinite(l) | ~np.isfinite(c)
    )
    invalid = int(np.sum(bad))
    if invalid:
        issues.append(f"{invalid} شمعة بقيم OHLC غير منطقية")

    neg_vol = int(np.sum((v < 0) | ~np.isfinite(v)))
    if neg_vol:
        issues.append(f"{neg_vol} شمعة بحجم غير صالح")

    # ── طوابع مستقبلية: شمعة لم تغلق بعد
    close_times = ot + step - 1
    future = int(np.sum(close_times > now_ms))
    if future:
        issues.append(f"{future} شمعة لم تُغلق بعد — يجب استبعادها")

    # ── الطزاجة
    age = int(now_ms - close_times[-1]) if n else None
    max_age = step * cfg.max_staleness_multiple
    if age is not None and age > max_age:
        issues.append(f"البيانات بايتة: آخر إغلاق قبل {age/1000/60:.1f} دقيقة")

    # ── عدد كافٍ
    if n < cfg.min_bars_required:
        issues.append(f"شموع قليلة: {n} (المطلوب {cfg.min_bars_required})")

    # ── المكوّنات
    completeness = float(np.clip(1.0 - gap_ratio / max(cfg.max_gap_ratio, 1e-9), 0, 1))
    if n < cfg.min_bars_required:
        completeness *= n / cfg.min_bars_required

    freshness = 1.0 if age is None else float(np.clip(1.0 - max(0, age - step) / max(max_age, 1), 0, 1))
    integrity = float(np.clip(1.0 - (duplicates + out_of_order + future) / max(n, 1) * 10, 0, 1))
    validity = float(np.clip(1.0 - (invalid + neg_vol) / max(n, 1) * 10, 0, 1))

    score = float(0.30*completeness + 0.25*freshness + 0.20*integrity + 0.25*validity)

    # سقف صارم للمشاكل الحرجة: لا يجوز أن تمر بيانات معطوبة بدرجة عالية.
    # OHLC غير منطقي أو شمعة غير مغلقة أو ترتيب مكسور = خلل بنيوي لا يُعوَّض
    # بجودة بقية المؤشرات.
    if invalid > 0 or future > 0 or out_of_order > 0:
        score = min(score, 0.55)
    if n < cfg.min_bars_required:
        score = min(score, 0.60)
    if age is not None and age > max_age:
        score = min(score, 0.50)

    return DataQuality(
        score=round(score, 4), completeness=round(completeness, 4),
        freshness=round(freshness, 4), integrity=round(integrity, 4),
        validity=round(validity, 4), n_bars=n, issues=issues,
        missing_bars=missing, duplicate_bars=duplicates,
        invalid_ohlc=invalid, out_of_order=out_of_order,
        future_bars=future, age_ms=age)


def drop_unclosed(data: OHLCV, now_ms: Optional[int] = None) -> OHLCV:
    """
    يحذف أي شمعة لم تُغلق بعد.
    البند 9: الإشارة تُبنى على آخر شمعة **مغلقة** فقط.
    """
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    ct = data.open_time + data.interval_ms - 1
    keep = ct <= now_ms
    if keep.all():
        return data
    idx = np.where(keep)[0]
    end = int(idx[-1]) + 1 if len(idx) else 0
    return data.slice(0, end)


def dedupe_and_sort(data: OHLCV) -> OHLCV:
    """يزيل التكرار ويرتّب زمنياً. لا يملأ فجوات."""
    _, uniq = np.unique(data.open_time, return_index=True)
    order = uniq[np.argsort(data.open_time[uniq])]
    if len(order) == len(data) and np.all(np.diff(data.open_time) > 0):
        return data
    return OHLCV(data.symbol, data.interval, data.open_time[order],
                 data.open[order], data.high[order], data.low[order],
                 data.close[order], data.volume[order],
                 data.source, data.fetched_at)
