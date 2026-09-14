"""
طبقة جلب البيانات - المرحلة الأولى
Data Fetcher Layer - Phase 1

تجلب البيانات من منصات التداول (Binance/Bybit) أو تحمل من ملفات CSV تاريخية
"""

import ccxt
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import time
import sqlite3
import json
import os

# إنشاء مجلد المشروع إذا مو موجود
os.makedirs('data', exist_ok=True)
os.makedirs('db', exist_ok=True)

# ==================== إعدادات أساسية ====================
EXCHANGE_NAME = 'binance'  # أو 'bybit'
SYMBOL = 'BTC/USDT'  # العملة المراد تداولها
TIMEFRAME = '15m'  # الفترة الزمنية (1m, 5m, 15m, 1h, 4h, 1d)

# معاملات للأوضاع المختلفة
MODES = {
    'scalping': {
        'timeframe': '5m',
        'ema_fast': 9,
        'ema_slow': 21,
        'rsi_period': 7,
        'atr_period': 7,
        'risk_reward': 1.5,
        'candles_needed': 200  # كم شمعة تاريخية نحتاج
    },
    'swing': {
        'timeframe': '15m',
        'ema_fast': 20,
        'ema_slow': 50,
        'rsi_period': 14,
        'atr_period': 14,
        'risk_reward': 2.0,
        'candles_needed': 300
    },
    'longterm': {
        'timeframe': '1h',  # يمكن 4h أو 1d
        'ema_fast': 50,
        'ema_slow': 200,
        'rsi_period': 14,
        'atr_period': 14,
        'risk_reward': 3.0,
        'candles_needed': 500
    }
}

# ==================== فئة جلب البيانات ====================
class CryptoDataFetcher:
    """
    تجلب البيانات من منصات التداول
    
    مثال:
        fetcher = CryptoDataFetcher('binance', 'BTC/USDT', '15m')
        # جلب بيانات تاريخية
        df = fetcher.fetch_historical_data(days=90)
        # حفظها في CSV
        fetcher.save_to_csv(df, 'btc_historical')
        # جلب أحدث شمعة
        latest_candle = fetcher.fetch_latest_candle()
    """
    
    def __init__(self, exchange_name='binance', symbol='BTC/USDT', timeframe='15m'):
        self.exchange_name = exchange_name.lower()
        self.symbol = symbol
        self.timeframe = timeframe
        
        # إنشاء اتصال بالمنصة
        try:
            if self.exchange_name == 'binance':
                self.exchange = ccxt.binance()
            elif self.exchange_name == 'bybit':
                self.exchange = ccxt.bybit()
            else:
                raise ValueError(f"المنصة {exchange_name} غير مدعومة")
            
            print(f"✅ تم الاتصال بـ {self.exchange_name}")
        except Exception as e:
            print(f"❌ خطأ الاتصال: {e}")
            self.exchange = None
    
    def fetch_historical_data(self, days=90, limit=1000):
        """
        جلب البيانات التاريخية
        
        Args:
            days: عدد الأيام الماضية (تقريبي)
            limit: أقصى عدد شموع لكل طلب (ccxt محدود بـ 1000)
        
        Returns:
            DataFrame بـ OHLCV (Open, High, Low, Close, Volume)
        """
        if not self.exchange:
            print("❌ لا يوجد اتصال بالمنصة")
            return None
        
        try:
            print(f"📊 جاري جلب {days} أيام من البيانات للـ {self.symbol}...")
            
            # تحويل الفترة الزمنية إلى ملي ثانية
            timeframe_ms = self._timeframe_to_milliseconds(self.timeframe)
            
            # حساب الوقت البداية
            since = int((datetime.utcnow() - timedelta(days=days)).timestamp() * 1000)
            
            ohlcv_data = []
            current_since = since
            
            # جلب البيانات في حلقة (لأن ccxt محدود بـ 1000 شمعة لكل طلب)
            while True:
                candles = self.exchange.fetch_ohlcv(
                    self.symbol,
                    timeframe=self.timeframe,
                    since=current_since,
                    limit=limit
                )
                
                if not candles:
                    break
                
                ohlcv_data.extend(candles)
                
                # آخر شمعة = البداية للطلب الجديد
                current_since = candles[-1][0] + timeframe_ms
                
                # تأخير لتجنب حد معدل الطلبات
                time.sleep(0.1)
            
            # تحويل إلى DataFrame
            df = pd.DataFrame(
                ohlcv_data,
                columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
            )
            
            # تحويل timestamp إلى datetime
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            
            print(f"✅ تم جلب {len(df)} شمعة")
            return df
        
        except Exception as e:
            print(f"❌ خطأ جلب البيانات: {e}")
            return None
    
    def fetch_latest_candle(self):
        """
        جلب أحدث شمعة (بيانات حية)
        
        Returns:
            dictionary بـ {timestamp, open, high, low, close, volume}
        """
        if not self.exchange:
            return None
        
        try:
            candles = self.exchange.fetch_ohlcv(
                self.symbol,
                timeframe=self.timeframe,
                limit=1
            )
            
            if candles:
                latest = candles[0]
                return {
                    'timestamp': pd.to_datetime(latest[0], unit='ms'),
                    'open': latest[1],
                    'high': latest[2],
                    'low': latest[3],
                    'close': latest[4],
                    'volume': latest[5]
                }
        except Exception as e:
            print(f"❌ خطأ جلب آخر شمعة: {e}")
        
        return None
    
    def save_to_csv(self, df, filename):
        """
        حفظ البيانات في ملف CSV
        
        Args:
            df: DataFrame البيانات
            filename: اسم الملف (بدون .csv)
        """
        if df is None or df.empty:
            print("⚠️  لا توجد بيانات للحفظ")
            return
        
        filepath = f'data/{filename}.csv'
        df.to_csv(filepath)
        print(f"✅ تم الحفظ في: {filepath}")
    
    def load_from_csv(self, filename):
        """
        تحميل البيانات من ملف CSV
        
        Args:
            filename: اسم الملف (بدون .csv)
        
        Returns:
            DataFrame
        """
        filepath = f'data/{filename}.csv'
        try:
            df = pd.read_csv(filepath, index_col='timestamp', parse_dates=True)
            print(f"✅ تم التحميل من: {filepath}")
            return df
        except FileNotFoundError:
            print(f"❌ الملف غير موجود: {filepath}")
            return None
    
    def _timeframe_to_milliseconds(self, timeframe):
        """تحويل الفترة الزمنية (5m, 1h, إلخ) إلى ملي ثانية"""
        multipliers = {
            'm': 60 * 1000,
            'h': 60 * 60 * 1000,
            'd': 24 * 60 * 60 * 1000
        }
        
        number = int(timeframe[:-1])
        unit = timeframe[-1]
        
        return number * multipliers.get(unit, 60000)


# ==================== قاعدة بيانات ====================
class DataDB:
    """
    قاعدة بيانات SQLite لحفظ البيانات
    الجداول:
    - candles: الشموع التاريخية
    - signals: الإشارات (دخول/خروج)
    - daily_risk: المخاطر اليومية
    """
    
    def __init__(self, db_name='crypto_trading.db'):
        self.db_path = f'db/{db_name}'
        self.init_database()
    
    def init_database(self):
        """إنشاء الجداول إذا كانت غير موجودة"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # جدول الشموع
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS candles (
                id INTEGER PRIMARY KEY,
                timestamp TEXT UNIQUE,
                symbol TEXT,
                timeframe TEXT,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL
            )
        ''')
        
        # جدول الإشارات
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY,
                timestamp TEXT,
                symbol TEXT,
                mode TEXT,
                direction TEXT,  -- long/short
                entry_price REAL,
                stop_loss REAL,
                take_profit REAL,
                indicators_json TEXT,  -- JSON بقيم المؤشرات وقت الإشارة
                status TEXT,  -- open/closed
                exit_price REAL,
                exit_time TEXT,
                result REAL,  -- الربح/الخسارة
                r_multiple REAL  -- كم Risk/Reward تحقق
            )
        ''')
        
        # جدول المخاطر اليومية
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS daily_risk (
                id INTEGER PRIMARY KEY,
                date TEXT UNIQUE,
                starting_balance REAL,
                current_pnl REAL,
                trading_halted INTEGER  -- 0=no, 1=yes
            )
        ''')
        
        conn.commit()
        conn.close()
        print(f"✅ قاعدة البيانات جاهزة: {self.db_path}")
    
    def insert_candles(self, df, symbol, timeframe):
        """حفظ الشموع في قاعدة البيانات"""
        conn = sqlite3.connect(self.db_path)
        
        for idx, row in df.iterrows():
            try:
                conn.execute('''
                    INSERT OR IGNORE INTO candles 
                    (timestamp, symbol, timeframe, open, high, low, close, volume)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    str(idx), symbol, timeframe,
                    row['open'], row['high'], row['low'], row['close'], row['volume']
                ))
            except Exception as e:
                print(f"⚠️  خطأ إدراج شمعة: {e}")
        
        conn.commit()
        conn.close()
        print(f"✅ تم حفظ {len(df)} شمعة في قاعدة البيانات")
    
    def get_candles(self, symbol, timeframe, limit=500):
        """جلب آخر N شمعة من قاعدة البيانات"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT timestamp, open, high, low, close, volume
            FROM candles
            WHERE symbol = ? AND timeframe = ?
            ORDER BY timestamp DESC
            LIMIT ?
        ''', (symbol, timeframe, limit))
        
        rows = cursor.fetchall()
        conn.close()
        
        if rows:
            df = pd.DataFrame(
                rows[::-1],  # عكس الترتيب (من الأقدم للأحدث)
                columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
            )
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            df.set_index('timestamp', inplace=True)
            return df
        
        return pd.DataFrame()


# ==================== اختبار ====================
if __name__ == "__main__":
    print("🚀 اختبار طبقة جلب البيانات\n")
    
    # المحتاج: إنستول المكتبات أولاً
    print("📌 تأكد من تنصيب: pip install ccxt pandas numpy")
    print()
    
    # إنشاء fetcher
    fetcher = CryptoDataFetcher(
        exchange_name='binance',
        symbol='BTC/USDT',
        timeframe='15m'
    )
    
    print("\n" + "="*50)
    print("اختبار 1: جلب بيانات تاريخية")
    print("="*50)
    
    # جلب 7 أيام فقط (عشان أسرع)
    df = fetcher.fetch_historical_data(days=7, limit=100)
    
    if df is not None:
        print("\n📊 معلومات البيانات:")
        print(f"عدد الشموع: {len(df)}")
        print(f"الفترة: {df.index[0]} إلى {df.index[-1]}")
        print(f"\nأول 3 شموع:")
        print(df.head(3))
        print(f"\nآخر 3 شموع:")
        print(df.tail(3))
        
        # حفظ في CSV
        fetcher.save_to_csv(df, 'btc_7days')
        
        # حفظ في قاعدة البيانات
        db = DataDB()
        db.insert_candles(df, 'BTC/USDT', '15m')
        
        # تحميل من قاعدة البيانات
        df_from_db = db.get_candles('BTC/USDT', '15m', limit=10)
        print(f"\n✅ آخر 10 شموع من قاعدة البيانات:")
        print(df_from_db)
    
    print("\n" + "="*50)
    print("اختبار 2: جلب أحدث شمعة (حي)")
    print("="*50)
    
    latest = fetcher.fetch_latest_candle()
    if latest:
        print(f"\n⏰ آخر شمعة (الحية):")
        print(f"الوقت: {latest['timestamp']}")
        print(f"السعر: {latest['close']} USDT")
        print(f"الحجم: {latest['volume']}")
