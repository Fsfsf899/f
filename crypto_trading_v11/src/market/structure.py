"""
هيكل السوق — البند 13، مع إصلاح Look-Ahead (البند 2).
=====================================================
الفكرة المحورية: المحور (Swing) لا يُعرف لحظة حدوثه.

قمة عند الشمعة i لا تُؤكَّد إلا بعد مرور `confirmation_bars` شمعة
دون تجاوزها. لذلك:

    swing.timestamp              = وقت حدوث القمة فعلاً
    swing.confirmation_index     = أول شمعة يجوز فيها استخدام المعلومة

أي استخدام قبل confirmation_index = نظر للأمام.

المصفوفات المُرجعة (support[i], resistance[i]) مبنية حصراً على
المحاور المؤكَّدة حتى i.
"""
import numpy as np
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple
from ..core.config import StructureConfig


@dataclass
class Swing:
    index: int                  # موقع المحور
    confirmation_index: int     # أول شمعة يجوز استخدامه فيها
    price: float
    kind: str                   # 'high' | 'low'
    timestamp: int = 0
    confirmation_timestamp: int = 0
    strength: float = 0.0


@dataclass
class StructureEvent:
    index: int
    confirmation_index: int
    type: str                   # HH HL LH LL BREAKOUT BREAKDOWN RETEST FAILED_BREAKOUT
    price: float
    timestamp: int = 0
    confirmation_timestamp: int = 0
    strength: float = 0.0
    ref_price: Optional[float] = None


@dataclass
class Level:
    price: float
    touches: int
    strength: float
    last_touch_index: int


def find_swings(high: np.ndarray, low: np.ndarray, cfg: StructureConfig,
                open_time: Optional[np.ndarray] = None) -> List[Swing]:
    """
    محاور مؤكَّدة فقط.
    الشمعة i تُعتبر قمة إذا كانت أعلى من k شمعة قبلها و k شمعة بعدها.
    التأكيد يقع عند i+k — وهذا ما نسجّله في confirmation_index.
    """
    k = cfg.swing_lookback
    n = len(high)
    out: List[Swing] = []
    if n < 2 * k + 1:
        return out

    for i in range(k, n - k):
        win_h = high[i-k:i+k+1]
        win_l = low[i-k:i+k+1]
        conf = i + max(k, cfg.confirmation_bars)
        if conf >= n:
            continue
        ts = int(open_time[i]) if open_time is not None else 0
        cts = int(open_time[conf]) if open_time is not None else 0

        if high[i] == win_h.max() and np.sum(win_h == high[i]) == 1:
            rng = float(win_h.max() - win_l.min())
            out.append(Swing(i, conf, float(high[i]), 'high', ts, cts,
                             strength=float(rng / high[i] * 100) if high[i] else 0.0))
        if low[i] == win_l.min() and np.sum(win_l == low[i]) == 1:
            rng = float(win_h.max() - win_l.min())
            out.append(Swing(i, conf, float(low[i]), 'low', ts, cts,
                             strength=float(rng / low[i] * 100) if low[i] else 0.0))

    out.sort(key=lambda s: s.confirmation_index)
    return out


def classify_swings(swings: List[Swing], open_time: Optional[np.ndarray] = None
                    ) -> List[StructureEvent]:
    """HH / HL / LH / LL — يقارن كل محور بسابقه من نفس النوع."""
    events: List[StructureEvent] = []
    last_high: Optional[Swing] = None
    last_low: Optional[Swing] = None

    for s in sorted(swings, key=lambda x: x.index):
        if s.kind == 'high':
            if last_high is not None:
                t = 'HH' if s.price > last_high.price else 'LH'
                events.append(StructureEvent(
                    s.index, s.confirmation_index, t, s.price,
                    s.timestamp, s.confirmation_timestamp,
                    s.strength, last_high.price))
            last_high = s
        else:
            if last_low is not None:
                t = 'HL' if s.price > last_low.price else 'LL'
                events.append(StructureEvent(
                    s.index, s.confirmation_index, t, s.price,
                    s.timestamp, s.confirmation_timestamp,
                    s.strength, last_low.price))
            last_low = s

    events.sort(key=lambda e: e.confirmation_index)
    return events


def _cluster(prices: List[Tuple[float, int]], tol_pct: float,
             min_touches: int) -> List[Level]:
    """يجمّع الأسعار المتقاربة في مستوى واحد. كل لمسة تزيد القوة."""
    if not prices:
        return []
    ordered = sorted(prices, key=lambda x: x[0])
    clusters: List[List[Tuple[float, int]]] = [[ordered[0]]]
    for p, idx in ordered[1:]:
        ref = float(np.mean([q for q, _ in clusters[-1]]))
        if ref > 0 and abs(p - ref) / ref * 100 <= tol_pct:
            clusters[-1].append((p, idx))
        else:
            clusters.append([(p, idx)])

    out = []
    for cl in clusters:
        if len(cl) < min_touches:
            continue
        out.append(Level(price=float(np.mean([p for p, _ in cl])),
                         touches=len(cl),
                         strength=float(min(len(cl) / 4.0, 1.0)),
                         last_touch_index=max(i for _, i in cl)))
    return out


def causal_levels(high: np.ndarray, low: np.ndarray, cfg: StructureConfig,
                  open_time: Optional[np.ndarray] = None
                  ) -> Tuple[np.ndarray, np.ndarray, List[Swing]]:
    """
    ⭐ الإصلاح الأساسي للبند 2.

    يُرجع support[i] و resistance[i] بحيث تكون كل قيمة عند i مبنية
    فقط على المحاور المؤكَّدة قبل i أو عندها.

    النسخة القديمة كانت تحسب المستويات من نافذة كاملة وتضعها على كل
    التاريخ — وهذا تسريب صريح.
    """
    n = len(high)
    support = np.full(n, np.nan)
    resistance = np.full(n, np.nan)
    swings = find_swings(high, low, cfg, open_time)
    if not swings:
        return support, resistance, swings

    conf_idx = np.array([s.confirmation_index for s in swings])
    order = np.argsort(conf_idx, kind='stable')
    swings_sorted = [swings[i] for i in order]
    conf_sorted = conf_idx[order]

    ptr = 0
    highs: List[Tuple[float, int]] = []
    lows: List[Tuple[float, int]] = []

    for i in range(n):
        while ptr < len(swings_sorted) and conf_sorted[ptr] <= i:
            s = swings_sorted[ptr]
            (highs if s.kind == 'high' else lows).append((s.price, s.index))
            ptr += 1

        win_lo = i - cfg.sr_window
        h_win = [(p, idx) for p, idx in highs if idx >= win_lo]
        l_win = [(p, idx) for p, idx in lows if idx >= win_lo]

        res = _cluster(h_win, cfg.sr_tolerance_pct, cfg.sr_min_touches)
        sup = _cluster(l_win, cfg.sr_tolerance_pct, cfg.sr_min_touches)

        px = high[i]
        above = [L.price for L in res if L.price > px]
        below = [L.price for L in sup if L.price < low[i]]
        if above:
            resistance[i] = min(above)
        if below:
            support[i] = max(below)

    return support, resistance, swings


def levels_at(high: np.ndarray, low: np.ndarray, idx: int,
              cfg: StructureConfig, open_time: Optional[np.ndarray] = None
              ) -> Dict[str, List[Level]]:
    """
    المستويات المتاحة عند الشمعة idx — تستخدم بيانات [0..idx] فقط.
    مفيدة للتحليل اللحظي.
    """
    end = idx + 1
    swings = find_swings(high[:end], low[:end], cfg,
                         open_time[:end] if open_time is not None else None)
    usable = [s for s in swings if s.confirmation_index <= idx]
    win_lo = idx - cfg.sr_window
    res = _cluster([(s.price, s.index) for s in usable
                    if s.kind == 'high' and s.index >= win_lo],
                   cfg.sr_tolerance_pct, cfg.sr_min_touches)
    sup = _cluster([(s.price, s.index) for s in usable
                    if s.kind == 'low' and s.index >= win_lo],
                   cfg.sr_tolerance_pct, cfg.sr_min_touches)
    return {'resistances': sorted(res, key=lambda L: L.price),
            'supports': sorted(sup, key=lambda L: L.price)}


def detect_breaks(close: np.ndarray, high: np.ndarray, low: np.ndarray,
                  support: np.ndarray, resistance: np.ndarray,
                  cfg: StructureConfig,
                  open_time: Optional[np.ndarray] = None) -> List[StructureEvent]:
    """
    اختراق / كسر / إعادة اختبار / اختراق فاشل.
    كلها تُحسب من المستويات السببية فقط.
    """
    n = len(close)
    events: List[StructureEvent] = []
    buf = cfg.breakout_buffer_pct / 100
    tol = cfg.retest_tolerance_pct / 100
    pending: Optional[Dict] = None

    for i in range(1, n):
        ts = int(open_time[i]) if open_time is not None else 0
        r, s = resistance[i-1], support[i-1]

        if not np.isnan(r) and close[i] > r * (1 + buf) and close[i-1] <= r:
            events.append(StructureEvent(i, i, 'BREAKOUT', float(close[i]),
                                         ts, ts, 1.0, float(r)))
            pending = {'level': float(r), 'index': i, 'dir': 'up'}
        elif not np.isnan(s) and close[i] < s * (1 - buf) and close[i-1] >= s:
            events.append(StructureEvent(i, i, 'BREAKDOWN', float(close[i]),
                                         ts, ts, 1.0, float(s)))
            pending = {'level': float(s), 'index': i, 'dir': 'down'}

        if pending and i > pending['index']:
            lvl = pending['level']
            age = i - pending['index']
            if pending['dir'] == 'up':
                if close[i] < lvl * (1 - buf):
                    events.append(StructureEvent(i, i, 'FAILED_BREAKOUT',
                                                 float(close[i]), ts, ts, 1.0, lvl))
                    pending = None
                elif abs(low[i] - lvl) / lvl <= tol and close[i] > lvl:
                    events.append(StructureEvent(i, i, 'RETEST', float(close[i]),
                                                 ts, ts, 0.8, lvl))
                    pending = None
            else:
                if close[i] > lvl * (1 + buf):
                    events.append(StructureEvent(i, i, 'FAILED_BREAKOUT',
                                                 float(close[i]), ts, ts, 1.0, lvl))
                    pending = None
                elif abs(high[i] - lvl) / lvl <= tol and close[i] < lvl:
                    events.append(StructureEvent(i, i, 'RETEST', float(close[i]),
                                                 ts, ts, 0.8, lvl))
                    pending = None
            if pending and age > 20:
                pending = None

    return events


class StructureEngine:
    """واجهة موحّدة لهيكل السوق."""
    def __init__(self, cfg: Optional[StructureConfig] = None):
        self.cfg = cfg or StructureConfig()

    def compute(self, high, low, close, open_time=None) -> Dict:
        sup, res, swings = causal_levels(high, low, self.cfg, open_time)
        return {
            'support': sup, 'resistance': res, 'swings': swings,
            'swing_events': classify_swings(swings, open_time),
            'break_events': detect_breaks(close, high, low, sup, res,
                                          self.cfg, open_time),
        }
