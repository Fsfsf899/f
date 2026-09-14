"""
تحليل هيكل السوق - Market Structure Analysis
=============================================
الوحدات:
1. Support/Resistance Detection  (مستويات الدعم والمقاومة)
2. Price Action Patterns         (أنماط حركة السعر)
3. Volume Profile                (ملف الحجم / POC)
4. Market Regime Detection       (تحديد حالة السوق)
5. Higher Timeframe Confirmation (تأكيد الإطار الأكبر)

كل وحدة مستقلة - تقدر تستخدم واحدة أو الكل.
لا تحتاج مكتبات خارجية عدا numpy.
"""

import numpy as np
from typing import Dict, List, Tuple, Optional


# ═══════════════════════════════════════════════════════════
# 1. مستويات الدعم والمقاومة
# ═══════════════════════════════════════════════════════════
class SupportResistance:
    """
    كشف مستويات الدعم/المقاومة بطريقة Swing Points
    (أدق من الطرق البسيطة لأنها تعتمد على قمم وقيعان حقيقية)
    """

    @staticmethod
    def find_swing_points(high: np.ndarray, low: np.ndarray,
                          lookback: int = 5) -> Tuple[List[int], List[int]]:
        """
        إيجاد القمم والقيعان المحورية.
        قمة = أعلى من `lookback` شمعة يمين ويسار.
        """
        swing_highs, swing_lows = [], []
        n = len(high)

        for i in range(lookback, n - lookback):
            window_h = high[i - lookback:i + lookback + 1]
            window_l = low[i - lookback:i + lookback + 1]

            if high[i] == np.max(window_h):
                swing_highs.append(i)
            if low[i] == np.min(window_l):
                swing_lows.append(i)

        return swing_highs, swing_lows

    @staticmethod
    def cluster_levels(prices: List[float], tolerance_pct: float = 0.5) -> List[Dict]:
        """
        تجميع الأسعار المتقاربة في مستوى واحد.
        كل ما زاد عدد اللمسات = المستوى أقوى.
        """
        if not prices:
            return []

        sorted_p = sorted(prices)
        clusters = []
        current = [sorted_p[0]]

        for p in sorted_p[1:]:
            if abs(p - np.mean(current)) / np.mean(current) * 100 <= tolerance_pct:
                current.append(p)
            else:
                clusters.append(current)
                current = [p]
        clusters.append(current)

        return [
            {'level': float(np.mean(c)), 'touches': len(c),
             'strength': min(len(c) / 3, 1.0)}
            for c in clusters
        ]

    @staticmethod
    def get_levels(high: np.ndarray, low: np.ndarray,
                   lookback: int = 5, tolerance_pct: float = 0.5) -> Dict:
        """المستويات الكاملة: مقاومات + دعوم"""
        sh, sl = SupportResistance.find_swing_points(high, low, lookback)

        resistances = SupportResistance.cluster_levels(
            [float(high[i]) for i in sh], tolerance_pct)
        supports = SupportResistance.cluster_levels(
            [float(low[i]) for i in sl], tolerance_pct)

        return {'resistances': resistances, 'supports': supports}

    @staticmethod
    def distance_to_nearest(price: float, levels: List[Dict]) -> Optional[Dict]:
        """أقرب مستوى للسعر الحالي + المسافة بالنسبة المئوية"""
        if not levels:
            return None
        nearest = min(levels, key=lambda L: abs(L['level'] - price))
        return {
            'level': nearest['level'],
            'distance_pct': abs(nearest['level'] - price) / price * 100,
            'strength': nearest['strength'],
            'above': nearest['level'] > price
        }


# ═══════════════════════════════════════════════════════════
# 2. أنماط حركة السعر (Price Action)
# ═══════════════════════════════════════════════════════════
class PriceAction:
    """
    أنماط الشموع - تأكيد إضافي للإشارة.
    ملاحظة: أنماط الشموع وحدها ضعيفة، لكنها ممتازة كـ فلتر تأكيد.
    """

    @staticmethod
    def candle_anatomy(o: float, h: float, l: float, c: float) -> Dict:
        """تشريح الشمعة"""
        body = abs(c - o)
        total = h - l if h != l else 0.0001
        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l

        return {
            'body_pct': body / total,
            'upper_wick_pct': upper_wick / total,
            'lower_wick_pct': lower_wick / total,
            'bullish': c > o,
            'range': total
        }

    @staticmethod
    def detect(idx: int, o: np.ndarray, h: np.ndarray,
               l: np.ndarray, c: np.ndarray) -> List[str]:
        """كشف الأنماط عند شمعة معينة"""
        if idx < 2:
            return []

        patterns = []
        cur = PriceAction.candle_anatomy(o[idx], h[idx], l[idx], c[idx])
        prev = PriceAction.candle_anatomy(o[idx-1], h[idx-1], l[idx-1], c[idx-1])

        # Hammer (مطرقة) - انعكاس صعودي
        if (cur['lower_wick_pct'] > 0.6 and cur['body_pct'] < 0.3
                and cur['upper_wick_pct'] < 0.1):
            patterns.append('HAMMER_BULL')

        # Shooting Star (نجمة ساقطة) - انعكاس هبوطي
        if (cur['upper_wick_pct'] > 0.6 and cur['body_pct'] < 0.3
                and cur['lower_wick_pct'] < 0.1):
            patterns.append('SHOOTING_STAR_BEAR')

        # Bullish Engulfing (ابتلاع صعودي)
        if (cur['bullish'] and not prev['bullish']
                and c[idx] > o[idx-1] and o[idx] < c[idx-1]):
            patterns.append('ENGULFING_BULL')

        # Bearish Engulfing (ابتلاع هبوطي)
        if (not cur['bullish'] and prev['bullish']
                and c[idx] < o[idx-1] and o[idx] > c[idx-1]):
            patterns.append('ENGULFING_BEAR')

        # Doji (تردد)
        if cur['body_pct'] < 0.1:
            patterns.append('DOJI_NEUTRAL')

        # Marubozu (شمعة قوية بلا ظلال)
        if cur['body_pct'] > 0.9:
            patterns.append('MARUBOZU_BULL' if cur['bullish'] else 'MARUBOZU_BEAR')

        return patterns

    @staticmethod
    def bias(patterns: List[str]) -> int:
        """
        تحويل الأنماط إلى انحياز رقمي:
        +1 صعودي / -1 هبوطي / 0 محايد
        """
        score = 0
        for p in patterns:
            if p.endswith('_BULL'):
                score += 1
            elif p.endswith('_BEAR'):
                score -= 1
        return int(np.sign(score))


# ═══════════════════════════════════════════════════════════
# 3. ملف الحجم (Volume Profile)
# ═══════════════════════════════════════════════════════════
class VolumeProfile:
    """
    توزيع الحجم على مستويات السعر.
    POC = Point of Control = السعر الأكثر تداولاً (مغناطيس سعري)
    Value Area = المنطقة التي تحوي 70% من الحجم
    """

    @staticmethod
    def build(close: np.ndarray, volume: np.ndarray, bins: int = 30) -> Dict:
        if len(close) == 0:
            return {}

        lo, hi = float(np.min(close)), float(np.max(close))
        if hi == lo:
            return {'poc': lo, 'vah': lo, 'val': lo, 'bins': []}

        edges = np.linspace(lo, hi, bins + 1)
        vol_at_price = np.zeros(bins)

        for price, vol in zip(close, volume):
            b = min(int((price - lo) / (hi - lo) * bins), bins - 1)
            vol_at_price[b] += vol

        poc_idx = int(np.argmax(vol_at_price))
        poc = float((edges[poc_idx] + edges[poc_idx + 1]) / 2)

        # Value Area: توسّع من POC حتى نغطي 70% من الحجم
        total = vol_at_price.sum()
        target = total * 0.70
        acc = vol_at_price[poc_idx]
        lo_i = hi_i = poc_idx

        while acc < target and (lo_i > 0 or hi_i < bins - 1):
            down = vol_at_price[lo_i - 1] if lo_i > 0 else -1
            up = vol_at_price[hi_i + 1] if hi_i < bins - 1 else -1
            if up >= down:
                hi_i += 1
                acc += max(up, 0)
            else:
                lo_i -= 1
                acc += max(down, 0)

        return {
            'poc': poc,
            'vah': float(edges[hi_i + 1]),   # Value Area High
            'val': float(edges[lo_i]),        # Value Area Low
            'bins': vol_at_price.tolist()
        }

    @staticmethod
    def position(price: float, profile: Dict) -> str:
        """أين السعر بالنسبة لمنطقة القيمة؟"""
        if not profile:
            return 'UNKNOWN'
        if price > profile['vah']:
            return 'ABOVE_VALUE'    # ممتد لأعلى - حذر من الارتداد
        if price < profile['val']:
            return 'BELOW_VALUE'    # ممتد لأسفل - فرصة شراء محتملة
        return 'IN_VALUE'           # داخل القيمة - نطاق جانبي


# ═══════════════════════════════════════════════════════════
# 4. تحديد حالة السوق (Market Regime)
# ═══════════════════════════════════════════════════════════
class MarketRegime:
    """
    ⭐ أهم وحدة في الملف كله.

    السبب: نفس الاستراتيجية تربح في اتجاه وتخسر في نطاق جانبي.
    معرفة "حالة السوق" تمنعك من التداول في الوقت الخطأ.
    """

    TRENDING_UP = 'TRENDING_UP'
    TRENDING_DOWN = 'TRENDING_DOWN'
    RANGING = 'RANGING'
    VOLATILE = 'VOLATILE'

    @staticmethod
    def detect(close: np.ndarray, high: np.ndarray, low: np.ndarray,
               idx: int, lookback: int = 50) -> Dict:
        if idx < lookback:
            return {'regime': 'UNKNOWN', 'confidence': 0.0, 'tradeable': False}

        window = close[idx - lookback:idx + 1]
        wh = high[idx - lookback:idx + 1]
        wl = low[idx - lookback:idx + 1]

        # 1) ميل خط الاتجاه (انحدار خطي مطبّع)
        x = np.arange(len(window))
        slope = np.polyfit(x, window, 1)[0]
        slope_pct = (slope * len(window)) / window[0] * 100

        # 2) كفاءة الحركة (Efficiency Ratio):
        #    مسافة صافية ÷ مسافة كلية. قريب من 1 = اتجاه نظيف
        net_move = abs(window[-1] - window[0])
        total_move = np.sum(np.abs(np.diff(window))) + 1e-9
        efficiency = net_move / total_move

        # 3) التقلب المطبّع
        volatility = float(np.std(np.diff(window) / window[:-1]) * 100)

        # 4) نطاق التذبذب
        range_pct = (np.max(wh) - np.min(wl)) / np.mean(window) * 100

        # القرار
        if efficiency > 0.35 and abs(slope_pct) > 2:
            regime = (MarketRegime.TRENDING_UP if slope_pct > 0
                      else MarketRegime.TRENDING_DOWN)
            confidence = min(efficiency * 2, 1.0)
            tradeable = True
        elif volatility > 3.0:
            regime = MarketRegime.VOLATILE
            confidence = min(volatility / 5, 1.0)
            tradeable = False       # ⛔ لا تتداول في تقلب عشوائي
        else:
            regime = MarketRegime.RANGING
            confidence = 1.0 - efficiency
            tradeable = False       # ⛔ استراتيجية الاتجاه تفشل هنا

        return {
            'regime': regime,
            'confidence': round(float(confidence), 3),
            'tradeable': tradeable,
            'slope_pct': round(float(slope_pct), 2),
            'efficiency': round(float(efficiency), 3),
            'volatility': round(volatility, 2),
            'range_pct': round(float(range_pct), 2)
        }


# ═══════════════════════════════════════════════════════════
# 5. تأكيد الإطار الزمني الأكبر
# ═══════════════════════════════════════════════════════════
class HigherTimeframe:
    """
    قاعدة ذهبية: لا تتداول ضد الإطار الأكبر.
    مثال: إشارة شراء على 15m + اتجاه هابط على 1h = تجاهلها.
    """

    @staticmethod
    def resample(data: np.ndarray, factor: int, mode: str = 'last') -> np.ndarray:
        """تحويل الإطار الصغير لإطار أكبر (مثلاً 15m → 1h بـ factor=4)"""
        n = len(data) // factor * factor
        reshaped = data[:n].reshape(-1, factor)
        if mode == 'last':
            return reshaped[:, -1]
        if mode == 'max':
            return reshaped.max(axis=1)
        if mode == 'min':
            return reshaped.min(axis=1)
        if mode == 'sum':
            return reshaped.sum(axis=1)
        return reshaped[:, -1]

    @staticmethod
    def _ema(prices: np.ndarray, period: int) -> np.ndarray:
        ema = np.zeros_like(prices, dtype=float)
        ema[0] = prices[0]
        k = 2 / (period + 1)
        for i in range(1, len(prices)):
            ema[i] = prices[i] * k + ema[i-1] * (1 - k)
        return ema

    @staticmethod
    def get_bias(close: np.ndarray, idx: int, factor: int = 4) -> Dict:
        """انحياز الإطار الأكبر عند نقطة معينة"""
        htf_idx = idx // factor
        if htf_idx < 55:
            return {'bias': 'NEUTRAL', 'aligned_long': True, 'aligned_short': True}

        htf_close = HigherTimeframe.resample(close[:idx + 1], factor, 'last')
        if len(htf_close) < 55:
            return {'bias': 'NEUTRAL', 'aligned_long': True, 'aligned_short': True}

        ema_f = HigherTimeframe._ema(htf_close, 20)
        ema_s = HigherTimeframe._ema(htf_close, 50)

        bull = ema_f[-1] > ema_s[-1]
        gap_pct = abs(ema_f[-1] - ema_s[-1]) / ema_s[-1] * 100

        if gap_pct < 0.3:
            bias = 'NEUTRAL'
        else:
            bias = 'BULLISH' if bull else 'BEARISH'

        return {
            'bias': bias,
            'gap_pct': round(float(gap_pct), 3),
            'aligned_long': bias in ('BULLISH', 'NEUTRAL'),
            'aligned_short': bias in ('BEARISH', 'NEUTRAL')
        }


if __name__ == '__main__':
    print("✅ market_structure.py — 5 وحدات جاهزة:")
    print("   1. SupportResistance   — مستويات الدعم/المقاومة")
    print("   2. PriceAction         — أنماط الشموع")
    print("   3. VolumeProfile       — POC ومنطقة القيمة")
    print("   4. MarketRegime        ⭐ الأهم — حالة السوق")
    print("   5. HigherTimeframe     — تأكيد الإطار الأكبر")
