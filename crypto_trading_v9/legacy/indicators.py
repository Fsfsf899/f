"""
حساب المؤشرات الفنية - المرحلة الثانية
Indicators Calculator - Phase 2

يحسب: EMA (المتوسط المتحرك الأسي)، RSI (قوة الزخم)، MACD (تأكيد الاتجاه)، ATR (نطاق التذبذب)
"""

import numpy as np
import pandas as pd
from typing import Dict, Tuple, Optional


class IndicatorsCalculator:
    """
    حسب المؤشرات الفنية من بيانات الأسعار
    
    المؤشرات:
    1. EMA (Exponential Moving Average) — متوسط متحرك أسي
       → يعطي وزن أكبر للأسعار الحديثة
       → لو EMA السريع (9) فوق EMA البطيء (21) = اتجاه صاعد محتمل
    
    2. RSI (Relative Strength Index) — مؤشر قوة النسبية
       → يقيس قوة الزخم (0-100)
       → > 70 = مشترى بقوة (قد ينخفض)
       → < 30 = مبيع بقوة (قد يرتفع)
    
    3. MACD (Moving Average Convergence Divergence) — تقارب/تباعد المتوسطات
       → يؤكد الاتجاه والزخم
       → MACD فوق signal line = اتجاه صاعد
    
    4. ATR (Average True Range) — متوسط النطاق الحقيقي
       → يقيس التقلبات
       → يُستخدم لتحديد وقف الخسارة والأرباح المستهدفة
    """
    
    @staticmethod
    def calculate_ema(df: pd.DataFrame, period: int, column: str = 'close') -> pd.Series:
        """
        حساب المتوسط المتحرك الأسي (EMA)
        
        Formula: EMA = Close * K + EMA(Previous) * (1 - K)
                 حيث K = 2 / (Period + 1)
        
        Args:
            df: DataFrame بيانات الشموع
            period: الفترة الزمنية (9 لـ fast، 21/50/200 لـ slow)
            column: العمود المراد حسابه (عادة 'close')
        
        Returns:
            Series بقيم EMA
        
        مثال:
            ema_9 = IndicatorsCalculator.calculate_ema(df, 9)
            ema_21 = IndicatorsCalculator.calculate_ema(df, 21)
        """
        return df[column].ewm(span=period, adjust=False).mean()
    
    @staticmethod
    def calculate_rsi(df: pd.DataFrame, period: int = 14, column: str = 'close') -> pd.Series:
        """
        حساب مؤشر قوة النسبية (RSI)
        
        خطوات:
        1. احسب الفرق بين السعر الحالي والسعر السابق (gains و losses)
        2. احسب متوسط الأرباح والخسائر
        3. RS = Average Gain / Average Loss
        4. RSI = 100 - (100 / (1 + RS))
        
        النتيجة بين 0-100:
        - > 70: overbought (قد ينخفض)
        - < 30: oversold (قد يرتفع)
        - 40-60: محايد
        
        Args:
            df: DataFrame البيانات
            period: الفترة (عادة 14)
            column: العمود (عادة 'close')
        
        Returns:
            Series بقيم RSI
        """
        close = df[column]
        
        # الفروقات
        delta = close.diff()
        
        # gains = الأرباح (الفروقات الموجبة)
        gains = delta.where(delta > 0, 0)
        # losses = الخسائر (الفروقات السالبة، بقيمة موجبة)
        losses = -delta.where(delta < 0, 0)
        
        # المتوسطات
        avg_gain = gains.rolling(window=period, min_periods=period).mean()
        avg_loss = losses.rolling(window=period, min_periods=period).mean()
        
        # تجنب القسمة على صفر
        avg_loss = avg_loss.replace(0, 0.0001)
        
        # حساب RS ثم RSI
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        return rsi
    
    @staticmethod
    def calculate_macd(
        df: pd.DataFrame,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
        column: str = 'close'
    ) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """
        حساب MACD (Moving Average Convergence Divergence)
        
        المكونات:
        1. MACD Line = EMA(12) - EMA(26)
        2. Signal Line = EMA(9) للـ MACD Line
        3. Histogram = MACD - Signal (الفرق)
        
        الإشارات:
        - MACD فوق Signal Line = صعود
        - MACD تحت Signal Line = هبوط
        - Histogram موجب/سالب = اتجاه/قوة
        
        Args:
            df: DataFrame البيانات
            fast: فترة EMA السريع (عادة 12)
            slow: فترة EMA البطيء (عادة 26)
            signal: فترة EMA للإشارة (عادة 9)
            column: العمود (عادة 'close')
        
        Returns:
            Tuple من (macd_line, signal_line, histogram)
        """
        ema_fast = df[column].ewm(span=fast, adjust=False).mean()
        ema_slow = df[column].ewm(span=slow, adjust=False).mean()
        
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        histogram = macd_line - signal_line
        
        return macd_line, signal_line, histogram
    
    @staticmethod
    def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
        """
        حساب متوسط النطاق الحقيقي (ATR)
        
        النطاق الحقيقي (True Range) هو أقصى قيمة من:
        1. High - Low
        2. |High - Close(prev)|
        3. |Low - Close(prev)|
        
        ATR = متوسط متحرك للنطاق الحقيقي
        
        الاستخدام:
        - تحديد وقف الخسارة = Entry - (1.5 × ATR) للشراء
        - تحديد الهدف = Entry + (Risk/Reward × ATR)
        - كلما زاد ATR = السوق أكثر تقلباً
        
        Args:
            df: DataFrame البيانات (يجب يحتوي على high, low, close)
            period: الفترة (عادة 14)
        
        Returns:
            Series بقيم ATR
        """
        high = df['high']
        low = df['low']
        close = df['close']
        
        # حساب النطاق الحقيقي
        tr1 = high - low
        tr2 = abs(high - close.shift())
        tr3 = abs(low - close.shift())
        
        true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        
        # ATR = متوسط متحرك للنطاق الحقيقي
        atr = true_range.rolling(window=period).mean()
        
        return atr
    
    @staticmethod
    def calculate_support_resistance(df: pd.DataFrame, lookback: int = 20) -> Tuple[float, float]:
        """
        حساب مستويات الدعم والمقاومة (بسيط)
        
        الدعم = أقل سعر في آخر N شمعة
        المقاومة = أعلى سعر في آخر N شمعة
        
        ملاحظة: هذا حساب بسيط جداً
        الحسابات المتقدمة تستخدم swing highs/lows أو Pivot Points
        
        Args:
            df: DataFrame البيانات
            lookback: عدد الشموع الماضية
        
        Returns:
            Tuple من (support, resistance)
        """
        recent = df.tail(lookback)
        support = recent['low'].min()
        resistance = recent['high'].max()
        
        return support, resistance
    
    @staticmethod
    def get_all_indicators(
        df: pd.DataFrame,
        ema_fast: int = 9,
        ema_slow: int = 21,
        rsi_period: int = 14,
        atr_period: int = 14,
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9
    ) -> pd.DataFrame:
        """
        حساب جميع المؤشرات معاً وإرجاعها في DataFrame واحد
        
        هذي الدالة الرئيسية اللي تستخدمها في الكود الرئيسي
        
        Args:
            df: DataFrame البيانات الأساسية (open, high, low, close, volume)
            ema_fast: فترة EMA السريع
            ema_slow: فترة EMA البطيء
            rsi_period: فترة RSI
            atr_period: فترة ATR
            macd_fast/slow/signal: فترات MACD
        
        Returns:
            DataFrame بجميع المؤشرات
        
        مثال:
            df_with_indicators = IndicatorsCalculator.get_all_indicators(
                df,
                ema_fast=9,
                ema_slow=21
            )
            print(df_with_indicators[['close', 'ema_9', 'ema_21', 'rsi', 'atr']])
        """
        df_copy = df.copy()
        
        # EMA
        df_copy['ema_fast'] = IndicatorsCalculator.calculate_ema(df_copy, ema_fast)
        df_copy['ema_slow'] = IndicatorsCalculator.calculate_ema(df_copy, ema_slow)
        
        # RSI
        df_copy['rsi'] = IndicatorsCalculator.calculate_rsi(df_copy, rsi_period)
        
        # MACD
        macd_line, signal_line, histogram = IndicatorsCalculator.calculate_macd(
            df_copy,
            fast=macd_fast,
            slow=macd_slow,
            signal=macd_signal
        )
        df_copy['macd'] = macd_line
        df_copy['macd_signal'] = signal_line
        df_copy['macd_histogram'] = histogram
        
        # ATR
        df_copy['atr'] = IndicatorsCalculator.calculate_atr(df_copy, atr_period)
        
        # Support/Resistance
        support, resistance = IndicatorsCalculator.calculate_support_resistance(df_copy)
        df_copy['support'] = support
        df_copy['resistance'] = resistance
        
        return df_copy


# ==================== اختبار المؤشرات ====================
if __name__ == "__main__":
    print("🚀 اختبار حساب المؤشرات\n")
    
    # إنشاء بيانات تجريبية
    print("📌 إنشاء بيانات تجريبية...")
    dates = pd.date_range('2024-01-01', periods=100, freq='1H')
    
    # أسعار عشوائية (محاكاة)
    np.random.seed(42)
    close = 50000 + np.cumsum(np.random.randn(100) * 100)
    high = close + np.abs(np.random.randn(100) * 50)
    low = close - np.abs(np.random.randn(100) * 50)
    volume = np.random.randint(1000, 10000, 100)
    
    df = pd.DataFrame({
        'open': close * 0.99,
        'high': high,
        'low': low,
        'close': close,
        'volume': volume
    }, index=dates)
    
    print(f"✅ تم إنشاء {len(df)} شمعة\n")
    
    # حساب المؤشرات
    print("="*60)
    print("حساب جميع المؤشرات...")
    print("="*60)
    
    df_indicators = IndicatorsCalculator.get_all_indicators(
        df,
        ema_fast=9,
        ema_slow=21,
        rsi_period=14,
        atr_period=14
    )
    
    # عرض النتائج
    print("\n📊 آخر 5 شموع مع المؤشرات:\n")
    display_cols = ['close', 'ema_fast', 'ema_slow', 'rsi', 'atr', 'macd', 'macd_signal']
    print(df_indicators[display_cols].tail())
    
    # تحليل آخر شمعة
    print("\n" + "="*60)
    print("تحليل آخر شمعة")
    print("="*60)
    
    last = df_indicators.iloc[-1]
    
    print(f"\n💰 السعر: {last['close']:.2f}")
    print(f"\n📈 EMA السريع (9): {last['ema_fast']:.2f}")
    print(f"📉 EMA البطيء (21): {last['ema_slow']:.2f}")
    
    if last['ema_fast'] > last['ema_slow']:
        print("   ✅ اتجاه صاعد محتمل (السريع فوق البطيء)")
    else:
        print("   ⚠️  اتجاه هابط محتمل (السريع تحت البطيء)")
    
    print(f"\n💪 RSI: {last['rsi']:.2f}")
    if last['rsi'] > 70:
        print("   ⚠️  مشترى بقوة (overbought) — قد ينخفض")
    elif last['rsi'] < 30:
        print("   ✅ مبيع بقوة (oversold) — قد يرتفع")
    else:
        print("   ➡️  محايد")
    
    print(f"\n🎯 MACD: {last['macd']:.6f}")
    print(f"📍 Signal Line: {last['macd_signal']:.6f}")
    if last['macd'] > last['macd_signal']:
        print("   ✅ MACD فوق Signal — زخم صاعد")
    else:
        print("   ⚠️  MACD تحت Signal — زخم هابط")
    
    print(f"\n📏 ATR (التقلبات): {last['atr']:.2f}")
    print(f"   وقف الخسارة المقترح: {last['close'] - (1.5 * last['atr']):.2f}")
    print(f"   الهدف المقترح (1:2): {last['close'] + (2 * 1.5 * last['atr']):.2f}")
    
    print(f"\n🛡️  الدعم: {last['support']:.2f}")
    print(f"🎪 المقاومة: {last['resistance']:.2f}")
    
    print("\n✅ انتهى الاختبار بنجاح!")
