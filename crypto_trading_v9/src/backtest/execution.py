"""
تنفيذ الشمعة — البند 4.
=======================
لا اعتماد على Close وحده. الخروج يُكتشف من High/Low.

المعضلة المحورية: إذا لمست الشمعة الوقف والهدف معاً، ترتيب
الأحداث داخل الشمعة غير معروف من OHLC. لذلك سياسة صريحة:

  conservative : الوقف أولاً (الأسوأ للمتداول) — الافتراضي
  optimistic   : الهدف أولاً
  intrabar     : يُستنتج من شكل الشمعة، ويقع على الوقف عند الغموض

القاعدة: لا نفترض أفضل نتيجة للمستخدم أبداً.
"""
from dataclasses import dataclass
from typing import Optional, Tuple
import numpy as np

STOP_LOSS = 'STOP_LOSS'
TAKE_PROFIT = 'TAKE_PROFIT'
TRAILING_STOP = 'TRAILING_STOP'
BREAK_EVEN = 'BREAK_EVEN'
SIGNAL_EXIT = 'SIGNAL_EXIT'
TIME_EXIT = 'TIME_EXIT'
BACKTEST_END = 'BACKTEST_END'


@dataclass
class Bar:
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


def gap_adjust(level: float, bar: Bar, side: str) -> float:
    """
    فجوة السعر: إذا فتحت الشمعة متجاوزة المستوى، التنفيذ يقع
    عند الفتح لا عند المستوى. تجاهل هذا يُنتج أرباحاً وهمية.
    """
    if side == 'stop':
        return min(level, bar.open) if bar.open < level else level
    return max(level, bar.open) if bar.open > level else level


def resolve_long_exit(bar: Bar, stop: float, target: Optional[float],
                      policy: str = 'conservative',
                      allow_gap: bool = True) -> Optional[Tuple[str, float]]:
    """
    يُرجع (سبب الخروج، السعر) أو None.
    للمركز الطويل فقط (Spot).
    """
    hit_stop = bar.low <= stop
    hit_target = target is not None and bar.high >= target

    if not hit_stop and not hit_target:
        return None

    if hit_stop and not hit_target:
        px = gap_adjust(stop, bar, 'stop') if allow_gap else stop
        return (STOP_LOSS, px)

    if hit_target and not hit_stop:
        px = gap_adjust(target, bar, 'target') if allow_gap else target
        return (TAKE_PROFIT, px)

    # كلاهما في نفس الشمعة
    if policy == 'optimistic':
        px = gap_adjust(target, bar, 'target') if allow_gap else target
        return (TAKE_PROFIT, px)

    if policy == 'intrabar':
        # استدلال ضعيف من شكل الشمعة؛ عند الغموض نختار الوقف
        if bar.close > bar.open and (bar.open - bar.low) < (bar.high - bar.open) * 0.35:
            return (TAKE_PROFIT, gap_adjust(target, bar, 'target') if allow_gap else target)
        return (STOP_LOSS, gap_adjust(stop, bar, 'stop') if allow_gap else stop)

    # conservative (الافتراضي): الأسوأ
    px = gap_adjust(stop, bar, 'stop') if allow_gap else stop
    return (STOP_LOSS, px)
