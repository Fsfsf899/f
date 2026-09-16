"""
محرك الإشارات المحسّن الأقوى
Enhanced Signal Engine - Advanced Version

التحسينات:
1. مؤشرات إضافية (Bollinger Bands, Stochastic)
2. فلاتر ذكية (تقليل false signals)
3. تصنيف قوة الإشارة (1-5 نجوم)
4. نقاط دخول متعددة (Pyramiding)
5. إدارة رأس المال ديناميكية
"""

import numpy as np
from typing import Dict, List, Tuple, Optional


# ==================== مؤشرات إضافية ====================
class AdvancedIndicators:
    """مؤشرات متقدمة"""
    
    @staticmethod
    def bollinger_bands(prices: np.ndarray, period: int = 20, std_dev: float = 2.0) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Bollinger Bands - النطاقات العليا والدنيا"""
        sma = np.convolve(prices, np.ones(period)/period, mode='valid')
        sma = np.pad(sma, (period-1, 0), 'edge')
        
        std = np.zeros_like(prices, dtype=float)
        for i in range(period-1, len(prices)):
            std[i] = np.std(prices[i-period+1:i+1])
        
        upper = sma + (std_dev * std)
        lower = sma - (std_dev * std)
        
        return upper, sma, lower
    
    @staticmethod
    def stochastic(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> Tuple[np.ndarray, np.ndarray]:
        """Stochastic Oscillator - مؤشر موثوقية الاتجاه"""
        lowest = np.zeros_like(close, dtype=float)
        highest = np.zeros_like(close, dtype=float)
        
        for i in range(len(close)):
            if i >= period - 1:
                lowest[i] = np.min(low[i-period+1:i+1])
                highest[i] = np.max(high[i-period+1:i+1])
        
        k = np.zeros_like(close, dtype=float)
        for i in range(period-1, len(close)):
            if highest[i] - lowest[i] != 0:
                k[i] = ((close[i] - lowest[i]) / (highest[i] - lowest[i])) * 100
            else:
                k[i] = 50
        
        d = np.convolve(k, np.ones(3)/3, mode='valid')
        d = np.pad(d, (2, 0), 'edge')
        
        return k, d
    
    @staticmethod
    def adx(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
        """ADX - قوة الاتجاه (0-100)"""
        plus_dm = np.zeros_like(high, dtype=float)
        minus_dm = np.zeros_like(high, dtype=float)
        
        for i in range(1, len(high)):
            up = high[i] - high[i-1]
            down = low[i-1] - low[i]
            
            if up > down and up > 0:
                plus_dm[i] = up
            if down > up and down > 0:
                minus_dm[i] = down
        
        tr = np.maximum(
            high - low,
            np.maximum(
                np.abs(high - np.roll(close, 1)),
                np.abs(low - np.roll(close, 1))
            )
        )
        
        atr = np.zeros_like(tr, dtype=float)
        atr[period-1] = np.mean(tr[:period])
        for i in range(period, len(tr)):
            atr[i] = (atr[i-1] * (period-1) + tr[i]) / period
        
        plus_di = np.zeros_like(plus_dm, dtype=float)
        minus_di = np.zeros_like(minus_dm, dtype=float)
        
        for i in range(period-1, len(plus_dm)):
            if atr[i] != 0:
                plus_di[i] = (np.sum(plus_dm[i-period+1:i+1]) / atr[i]) * 100
                minus_di[i] = (np.sum(minus_dm[i-period+1:i+1]) / atr[i]) * 100
        
        di_diff = np.abs(plus_di - minus_di)
        di_sum = plus_di + minus_di + 0.0001
        dx = (di_diff / di_sum) * 100
        
        adx = np.zeros_like(dx, dtype=float)
        adx[period*2-1] = np.mean(dx[:period*2])
        for i in range(period*2, len(dx)):
            adx[i] = (adx[i-1] * (period-1) + dx[i]) / period
        
        return adx
    
    @staticmethod
    def obv(close: np.ndarray, volume: np.ndarray) -> np.ndarray:
        """On-Balance Volume - حجم التوازن"""
        obv = np.zeros_like(close, dtype=float)
        obv[0] = volume[0]
        
        for i in range(1, len(close)):
            if close[i] > close[i-1]:
                obv[i] = obv[i-1] + volume[i]
            elif close[i] < close[i-1]:
                obv[i] = obv[i-1] - volume[i]
            else:
                obv[i] = obv[i-1]
        
        return obv


# ==================== محرك الإشارات المحسّن ====================
class AdvancedSignalEngine:
    """
    محرك إشارات متقدم مع:
    - فلاتر ذكية
    - تصنيف الإشارة (1-5 نجوم)
    - نقاط دخول متعددة
    - إدارة رأس مال ديناميكية
    """
    
    @staticmethod
    def calculate_signal_strength(
        idx: int,
        close: np.ndarray,
        high: np.ndarray,
        low: np.ndarray,
        volume: np.ndarray,
        ema_fast: np.ndarray,
        ema_slow: np.ndarray,
        rsi: np.ndarray,
        macd: np.ndarray,
        macd_signal: np.ndarray,
        adx: np.ndarray,
        stoch_k: np.ndarray,
        stoch_d: np.ndarray,
        bb_upper: np.ndarray,
        bb_lower: np.ndarray,
        obv: np.ndarray
    ) -> Tuple[int, List[str], float]:
        """
        احسب قوة الإشارة (1-5 نجوم)
        
        Returns:
            (score, conditions_met, confidence)
        """
        
        if idx < 2:
            return 0, [], 0.0
        
        score = 0
        conditions = []
        
        # 1. EMA Crossover (أهم شرط)
        ema_cross = (ema_fast[idx-1] <= ema_slow[idx-1]) and (ema_fast[idx] > ema_slow[idx])
        if ema_cross:
            score += 2
            conditions.append("EMA Crossover")
        elif ema_fast[idx] > ema_slow[idx]:
            score += 1
            conditions.append("EMA Bullish")
        
        # 2. RSI (تأكيد الزخم)
        if 40 <= rsi[idx] <= 60:  # منطقة محايدة آمنة
            score += 1
            conditions.append("RSI Safe Zone")
        elif rsi[idx] < 30:  # منطقة البيع الزائد
            score += 1.5
            conditions.append("RSI Oversold (Strong)")
        
        # 3. MACD (تأكيد الاتجاه)
        if macd[idx] > macd_signal[idx]:
            score += 1
            conditions.append("MACD Bullish")
        
        # 4. ADX (قوة الاتجاه)
        if adx[idx] > 25:  # اتجاه قوي
            score += 1
            conditions.append("ADX Strong")
        elif adx[idx] > 20:  # اتجاه متوسط
            score += 0.5
            conditions.append("ADX Moderate")
        
        # 5. Stochastic (توقيت الدخول)
        if stoch_k[idx] < 20 and stoch_d[idx] < 20:  # نقطة ذهبية
            score += 1.5
            conditions.append("Stochastic Oversold (Strong)")
        elif stoch_k[idx-1] < stoch_d[idx-1] and stoch_k[idx] > stoch_d[idx]:  # crossover
            score += 1
            conditions.append("Stochastic Crossover")
        
        # 6. Bollinger Bands (نقطة دخول تقنية)
        if close[idx] < bb_lower[idx]:
            score += 1
            conditions.append("Near BB Lower")
        
        # 7. Volume (تأكيد الحركة)
        avg_vol = np.mean(volume[max(0, idx-20):idx])
        if volume[idx] > avg_vol * 1.3:
            score += 0.5
            conditions.append("Volume High")
        
        # 8. OBV (تأكيد المحترفين)
        if idx > 10:
            obv_slope = (obv[idx] - obv[idx-5]) / max(obv[idx-5], 1)
            if obv_slope > 0.05:
                score += 0.5
                conditions.append("OBV Rising")
        
        # حساب الثقة (Confidence)
        # كل شرط إضافي يزيد الثقة
        confidence = min(len(conditions) / 8, 1.0)  # max 100%
        
        return int(score * 2) % 6, conditions, confidence
    
    @staticmethod
    def check_advanced_long_entry(
        idx: int,
        close: np.ndarray,
        high: np.ndarray,
        low: np.ndarray,
        volume: np.ndarray,
        ema_fast: np.ndarray,
        ema_slow: np.ndarray,
        rsi: np.ndarray,
        macd: np.ndarray,
        macd_signal: np.ndarray,
        adx: np.ndarray,
        stoch_k: np.ndarray,
        stoch_d: np.ndarray,
        bb_upper: np.ndarray,
        bb_lower: np.ndarray,
        obv: np.ndarray,
        min_conditions: int = 4  # الحد الأدنى للشروط
    ) -> Tuple[bool, int, List[str], float]:
        """
        فحص الدخول LONG مع تصنيف قوة الإشارة
        
        Returns:
            (should_enter, stars, conditions, confidence)
        """
        
        stars, conditions, confidence = AdvancedSignalEngine.calculate_signal_strength(
            idx, close, high, low, volume,
            ema_fast, ema_slow, rsi,
            macd, macd_signal, adx,
            stoch_k, stoch_d,
            bb_upper, bb_lower, obv
        )
        
        # يجب توفر شروط معينة على الأقل
        should_enter = len(conditions) >= min_conditions
        
        return should_enter, stars, conditions, confidence
    
    @staticmethod
    def check_advanced_short_entry(
        idx: int,
        close: np.ndarray,
        high: np.ndarray,
        low: np.ndarray,
        volume: np.ndarray,
        ema_fast: np.ndarray,
        ema_slow: np.ndarray,
        rsi: np.ndarray,
        macd: np.ndarray,
        macd_signal: np.ndarray,
        adx: np.ndarray,
        stoch_k: np.ndarray,
        stoch_d: np.ndarray,
        bb_upper: np.ndarray,
        bb_lower: np.ndarray,
        obv: np.ndarray,
        min_conditions: int = 4
    ) -> Tuple[bool, int, List[str], float]:
        """فحص الدخول SHORT - معكوس"""
        
        if idx < 2:
            return False, 0, [], 0.0
        
        score = 0
        conditions = []
        
        # نفس الشروط لكن معكوسة
        ema_cross = (ema_fast[idx-1] >= ema_slow[idx-1]) and (ema_fast[idx] < ema_slow[idx])
        if ema_cross:
            score += 2
            conditions.append("EMA Crossover")
        elif ema_fast[idx] < ema_slow[idx]:
            score += 1
            conditions.append("EMA Bearish")
        
        if 40 <= rsi[idx] <= 60:
            score += 1
            conditions.append("RSI Safe Zone")
        elif rsi[idx] > 70:  # منطقة الشراء الزائد
            score += 1.5
            conditions.append("RSI Overbought (Strong)")
        
        if macd[idx] < macd_signal[idx]:
            score += 1
            conditions.append("MACD Bearish")
        
        if adx[idx] > 25:
            score += 1
            conditions.append("ADX Strong")
        elif adx[idx] > 20:
            score += 0.5
            conditions.append("ADX Moderate")
        
        if stoch_k[idx] > 80 and stoch_d[idx] > 80:
            score += 1.5
            conditions.append("Stochastic Overbought (Strong)")
        elif stoch_k[idx-1] > stoch_d[idx-1] and stoch_k[idx] < stoch_d[idx]:
            score += 1
            conditions.append("Stochastic Crossover")
        
        if close[idx] > bb_upper[idx]:
            score += 1
            conditions.append("Near BB Upper")
        
        avg_vol = np.mean(volume[max(0, idx-20):idx])
        if volume[idx] > avg_vol * 1.3:
            score += 0.5
            conditions.append("Volume High")
        
        if idx > 10:
            obv_slope = (obv[idx] - obv[idx-5]) / max(obv[idx-5], 1)
            if obv_slope < -0.05:
                score += 0.5
                conditions.append("OBV Falling")
        
        confidence = min(len(conditions) / 8, 1.0)
        should_enter = len(conditions) >= min_conditions
        
        return should_enter, int(score * 2) % 6, conditions, confidence


# ==================== إدارة رأس المال الديناميكية ====================
class DynamicPositionSizing:
    """
    إدارة حجم المركز ديناميكياً بناءً على:
    - قوة الإشارة
    - التقلب الحالي
    - الأداء الأخيرة
    """
    
    @staticmethod
    def calculate_position_size(
        account_balance: float,
        risk_percent: float,
        stop_loss_distance: float,
        signal_strength: int,
        recent_win_rate: float = 0.5
    ) -> float:
        """
        احسب حجم المركز الديناميكي
        
        Args:
            account_balance: رصيد الحساب
            risk_percent: نسبة المخاطرة (%)
            stop_loss_distance: مسافة الوقف من السعر
            signal_strength: قوة الإشارة (0-5)
            recent_win_rate: نسبة النجاح الأخيرة
        
        Returns:
            حجم المركز الموصى به
        """
        
        # الحساب الأساسي
        risk_amount = account_balance * (risk_percent / 100)
        base_position_size = risk_amount / stop_loss_distance
        
        # ضاعف حسب قوة الإشارة
        strength_multiplier = 1 + (signal_strength / 5 * 0.3)  # +30% أقصى
        
        # ضاعف حسب نسبة النجاح
        # إذا كان الأداء جيد، زد المخاطرة قليلاً
        if recent_win_rate > 0.6:
            wr_multiplier = 1.15  # +15%
        elif recent_win_rate > 0.5:
            wr_multiplier = 1.0
        elif recent_win_rate > 0.4:
            wr_multiplier = 0.85  # -15%
        else:
            wr_multiplier = 0.7  # -30%
        
        final_position_size = base_position_size * strength_multiplier * wr_multiplier
        
        return final_position_size
    
    @staticmethod
    def pyramiding(
        base_position: float,
        signal_strength: int,
        current_profit_percent: float
    ) -> Dict[str, float]:
        """
        نقاط دخول متعددة (Pyramiding)
        - دخول إضافي عند تأكيد الاتجاه
        """
        
        positions = {
            'initial': base_position,
            'pyramid_1': 0,
            'pyramid_2': 0
        }
        
        # إذا كانت الإشارة قوية جداً وحققنا ربح
        if signal_strength >= 4 and current_profit_percent > 1.0:
            # دخول إضافي 50% من الحجم الأساسي
            positions['pyramid_1'] = base_position * 0.5
            positions['pyramid_2'] = base_position * 0.25
        elif signal_strength >= 3 and current_profit_percent > 0.5:
            positions['pyramid_1'] = base_position * 0.3
        
        return positions


# ==================== نظام التصنيف ====================
def print_signal_with_strength(signal: Dict, stars: int, conditions: List[str], confidence: float):
    """طبع الإشارة مع تصنيفها"""
    
    star_display = "⭐" * stars + "☆" * (5 - stars)
    
    print(f"""
╔════════════════════════════════════════════════════════════════╗
║                     🚨 إشارة جديدة! 🚨                      ║
╚════════════════════════════════════════════════════════════════╝

قوة الإشارة: {star_display} ({stars}/5)
الثقة: {confidence*100:.1f}%
الاتجاه: {'🟢 LONG' if signal.get('direction') == 'long' else '🔴 SHORT'}

الشروط المتحققة ({len(conditions)}):
{chr(10).join(f'  ✓ {c}' for c in conditions)}

السعر:
  الدخول: ${signal.get('entry_price', 'N/A'):.2f}
  الهدف: ${signal.get('take_profit', 'N/A'):.2f}
  الوقف: ${signal.get('stop_loss', 'N/A'):.2f}
  النسبة: {(signal.get('take_profit', 0) - signal.get('entry_price', 0)) / (signal.get('entry_price', 1) - signal.get('stop_loss', 0)):.2f}:1

{'⚠️  تنبيه: إشارة متوسطة - راقب بعناية' if stars <= 2 else ''}
{'✅ إشارة جيدة - يمكن الدخول' if 3 <= stars <= 4 else ''}
{'🔥 إشارة قوية جداً - فرصة ذهبية!' if stars >= 5 else ''}
╚════════════════════════════════════════════════════════════════╝
""")


# ==================== مثال الاستخدام ====================
if __name__ == "__main__":
    print("\n" + "="*70)
    print("🚀 محرك الإشارات المحسّن الأقوى")
    print("="*70 + "\n")
    
    # بيانات تجريبية
    np.random.seed(42)
    prices = np.cumsum(np.random.normal(0, 100, 200)) + 50000
    
    print("✅ تم بناء محرك الإشارات المتقدم مع:")
    print("   • 8 مؤشرات فنية (EMA, RSI, MACD, ADX, Stochastic, BB, OBV)")
    print("   • تصنيف قوة الإشارة (1-5 نجوم)")
    print("   • نسبة ثقة (0-100%)")
    print("   • فلاتر ذكية لتقليل false signals")
    print("   • إدارة رأس مال ديناميكية")
    print("   • نقاط دخول متعددة (Pyramiding)")
    print("\n" + "="*70 + "\n")
