"""
برنامج باكتستنق محسّن - جلب البيانات الحقيقية
Enhanced Backtesting - Real Data Loading

يحاول جلب البيانات من Binance (إذا كان الاتصال متاحاً)
أو استخدم بيانات CSV محفوظة
"""

import json
import os
from typing import Dict, List, Optional, Tuple
import numpy as np
from datetime import datetime, timedelta


# ==================== محاولة استيراد مكتبات اختيارية ====================
try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False
    print("⚠️  pandas ليس مثبت، سيتم استخدام قوائم Python بدلاً منها")

try:
    import ccxt
    HAS_CCXT = True
except ImportError:
    HAS_CCXT = False
    print("⚠️  ccxt ليس مثبت، لا يمكن جلب بيانات من Binance")


# ==================== شارك البيانات من السابق ====================
class IndicatorsCalculator:
    """حساب المؤشرات الفنية"""
    
    @staticmethod
    def calculate_ema(close: np.ndarray, period: int) -> np.ndarray:
        """حساب EMA"""
        ema = np.zeros_like(close, dtype=float)
        ema[0] = close[0]
        multiplier = 2 / (period + 1)
        
        for i in range(1, len(close)):
            ema[i] = close[i] * multiplier + ema[i-1] * (1 - multiplier)
        
        return ema
    
    @staticmethod
    def calculate_rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
        """حساب RSI"""
        deltas = np.diff(close)
        
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        
        avg_gain = np.zeros_like(close, dtype=float)
        avg_loss = np.zeros_like(close, dtype=float)
        
        avg_gain[period] = np.mean(gains[:period])
        avg_loss[period] = np.mean(losses[:period])
        
        for i in range(period + 1, len(close)):
            avg_gain[i] = (avg_gain[i-1] * (period - 1) + gains[i-1]) / period
            avg_loss[i] = (avg_loss[i-1] * (period - 1) + losses[i-1]) / period
        
        rs = np.divide(avg_gain, avg_loss, where=avg_loss != 0, out=np.zeros_like(avg_gain))
        rsi = 100 - (100 / (1 + rs))
        
        return rsi
    
    @staticmethod
    def calculate_macd(close: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """حساب MACD"""
        ema_fast = IndicatorsCalculator.calculate_ema(close, fast)
        ema_slow = IndicatorsCalculator.calculate_ema(close, slow)
        
        macd_line = ema_fast - ema_slow
        signal_line = IndicatorsCalculator.calculate_ema(macd_line, signal)
        histogram = macd_line - signal_line
        
        return macd_line, signal_line, histogram
    
    @staticmethod
    def calculate_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
        """حساب ATR"""
        tr1 = high - low
        tr2 = np.abs(high - np.roll(close, 1))
        tr3 = np.abs(low - np.roll(close, 1))
        
        tr = np.maximum(tr1, np.maximum(tr2, tr3))
        
        atr = np.zeros_like(tr, dtype=float)
        atr[period] = np.mean(tr[:period])
        
        for i in range(period + 1, len(tr)):
            atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period
        
        return atr


# ==================== جاتب جلب البيانات ====================
class DataLoader:
    """
    جلب البيانات من مصادر مختلفة:
    1. Binance (ccxt) - إذا كان مثبت
    2. ملف CSV - إذا كان موجود
    3. بيانات تجريبية - كخيار أخير
    """
    
    @staticmethod
    def try_load_from_binance(symbol: str = 'BTC/USDT', timeframe: str = '15m', days: int = 30) -> Optional[Dict]:
        """
        حاول جلب البيانات من Binance
        
        Returns:
            Dictionary بـ {dates, close, high, low, volume} أو None
        """
        if not HAS_CCXT:
            return None
        
        try:
            print("🔗 محاولة الاتصال بـ Binance...")
            exchange = ccxt.binance()
            
            # تحويل الأيام إلى ميلي ثانية
            since = int((datetime.utcnow() - timedelta(days=days)).timestamp() * 1000)
            
            # جلب البيانات
            candles = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=1000)
            
            print(f"✅ تم جلب {len(candles)} شمعة من Binance")
            
            # تحويل لنمط مشترك
            dates = []
            close = []
            high = []
            low = []
            volume = []
            
            for candle in candles:
                dates.append(candle[0])
                open_p = candle[1]
                high_p = candle[2]
                low_p = candle[3]
                close_p = candle[4]
                vol = candle[5]
                
                dates.append(datetime.fromtimestamp(candle[0] / 1000))
                close.append(close_p)
                high.append(high_p)
                low.append(low_p)
                volume.append(vol)
            
            return {
                'dates': np.array(dates),
                'close': np.array(close),
                'high': np.array(high),
                'low': np.array(low),
                'volume': np.array(volume)
            }
        
        except Exception as e:
            print(f"❌ فشل جلب البيانات من Binance: {e}")
            return None
    
    @staticmethod
    def load_from_csv(filename: str = 'btc_data.csv') -> Optional[Dict]:
        """
        حمّل البيانات من ملف CSV
        
        التنسيق المتوقع:
        timestamp, open, high, low, close, volume
        """
        if not os.path.exists(filename):
            return None
        
        try:
            if HAS_PANDAS:
                df = pd.read_csv(filename)
                return {
                    'dates': df['timestamp'].values,
                    'close': df['close'].values.astype(float),
                    'high': df['high'].values.astype(float),
                    'low': df['low'].values.astype(float),
                    'volume': df['volume'].values.astype(float)
                }
            else:
                # استخدم CSV reader من Python
                import csv
                dates = []
                close = []
                high = []
                low = []
                volume = []
                
                with open(filename) as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        dates.append(row['timestamp'])
                        close.append(float(row['close']))
                        high.append(float(row['high']))
                        low.append(float(row['low']))
                        volume.append(float(row['volume']))
                
                return {
                    'dates': np.array(dates),
                    'close': np.array(close),
                    'high': np.array(high),
                    'low': np.array(low),
                    'volume': np.array(volume)
                }
        
        except Exception as e:
            print(f"❌ خطأ قراءة CSV: {e}")
            return None
    
    @staticmethod
    def generate_synthetic_data(days: int = 30, timeframe_min: int = 15) -> Dict:
        """توليد بيانات تجريبية واقعية"""
        print("📊 توليد بيانات تجريبية...")
        
        candles = (days * 24 * 60) // timeframe_min
        np.random.seed(42)
        
        price = 50000
        close_prices = [price]
        high_prices = [price]
        low_prices = [price]
        volumes = []
        
        drift = 0.0001
        volatility = 0.008
        mean_reversion = 0.95
        
        for i in range(1, candles + 1):
            random_change = np.random.normal(drift, volatility)
            price = price * mean_reversion + close_prices[-1] * (1 - mean_reversion)
            price = price * (1 + random_change)
            
            daily_volatility = abs(np.random.normal(0, volatility * 2))
            high = price * (1 + daily_volatility)
            low = price * (1 - daily_volatility)
            
            close_prices.append(price)
            high_prices.append(high)
            low_prices.append(low)
            volumes.append(np.random.randint(1000, 10000))
        
        start_date = datetime.now() - timedelta(days=days)
        dates = [start_date + timedelta(minutes=timeframe_min*i) for i in range(len(close_prices))]
        
        min_len = min(len(close_prices), len(high_prices), len(low_prices), len(volumes))
        
        return {
            'dates': np.array(dates[:min_len]),
            'close': np.array(close_prices[:min_len]),
            'high': np.array(high_prices[:min_len]),
            'low': np.array(low_prices[:min_len]),
            'volume': np.array(volumes[:min_len])
        }
    
    @staticmethod
    def load_data(symbol: str = 'BTC/USDT', days: int = 30) -> Dict:
        """
        جرب جميع طرق التحميل بالترتيب
        """
        print("\n📥 جاري تحميل البيانات...\n")
        
        # 1. حاول Binance
        data = DataLoader.try_load_from_binance(symbol, timeframe='15m', days=days)
        if data:
            print(f"✅ تم تحميل البيانات من Binance\n")
            return data
        
        # 2. حاول CSV
        data = DataLoader.load_from_csv('btc_data.csv')
        if data:
            print(f"✅ تم تحميل البيانات من CSV\n")
            return data
        
        # 3. بيانات تجريبية
        print("⚠️  استخدام بيانات تجريبية (مو حقيقية)\n")
        return DataLoader.generate_synthetic_data(days=days)


# ==================== مساعد ====================
def print_instructions():
    """طبع التعليمات"""
    print("""
╔════════════════════════════════════════════════════════════════╗
║           إرشادات لتحسين نتائج الباكتستنق                    ║
╚════════════════════════════════════════════════════════════════╝

إذا كنت تريد:

1️⃣ تجربة ببيانات حقيقية من Binance:
   ثبّت ccxt:
   $ pip install --break-system-packages ccxt
   
   ثم شغل البرنامج مرة أخرى

2️⃣ تحميل بيانات من ملف CSV:
   ضع ملف CSV باسم 'btc_data.csv' بالنمط:
   timestamp, open, high, low, close, volume
   
   مثال من Yahoo Finance أو Investing.com

3️⃣ تعديل المعاملات (Parameters):
   عدّل هذه الأرقام في الكود:
   - ema_fast, ema_slow: فترات EMA
   - rsi_period: فترة RSI
   - rsi_min/max: حدود RSI للدخول
   - volume_threshold: حد Volume (الآن 1.2 = 120%)

4️⃣ اختبار أوضاع مختلفة:
   عدّل هذه القيم:
   - Scalping: ema_fast=9, ema_slow=21, rsi=7
   - Swing: ema_fast=20, ema_slow=50, rsi=14
   - LongTerm: ema_fast=50, ema_slow=200, rsi=14

════════════════════════════════════════════════════════════════════
    """)


# ==================== الرئيسي ====================
if __name__ == "__main__":
    print("\n" + "="*70)
    print("🚀 برنامج الباكتستنق المحسّن")
    print("="*70)
    
    # جلب البيانات
    data = DataLoader.load_data(symbol='BTC/USDT', days=90)
    
    print(f"📊 البيانات المحملة:")
    print(f"   عدد الشموع: {len(data['close'])}")
    print(f"   النطاق السعري: ${data['close'].min():.2f} - ${data['close'].max():.2f}")
    print()
    
    # حساب المؤشرات
    print("📈 حساب المؤشرات...")
    close = data['close']
    high = data['high']
    low = data['low']
    
    ema_fast = IndicatorsCalculator.calculate_ema(close, period=20)
    ema_slow = IndicatorsCalculator.calculate_ema(close, period=50)
    rsi = IndicatorsCalculator.calculate_rsi(close, period=14)
    macd, macd_signal, _ = IndicatorsCalculator.calculate_macd(close)
    atr = IndicatorsCalculator.calculate_atr(high, low, close, period=14)
    
    support = np.array([np.min(low[max(0, i-20):i+1]) if i >= 1 else low[i] for i in range(len(low))])
    resistance = np.array([np.max(high[max(0, i-20):i+1]) if i >= 1 else high[i] for i in range(len(high))])
    
    print("✅ تم حساب المؤشرات\n")
    
    # تحليل آخر 5 شموع
    print("📊 آخر 5 شموع:")
    print("-" * 70)
    for idx in range(-5, 0):
        i = len(close) + idx
        if i >= 0:
            print(f"[{i}] Close: ${close[i]:,.0f} | EMA: {ema_fast[i]:.0f}/{ema_slow[i]:.0f} | RSI: {rsi[i]:.1f} | ATR: {atr[i]:.0f}")
    
    print()
    print_instructions()
    
    print("\n" + "="*70)
    print("✅ البرنامج جاهز للاختبار!")
    print("="*70)
