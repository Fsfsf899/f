"""
جاتب جلب البيانات من Binance - بدون مكتبات خارجية
Binance Data Fetcher - Built-in urllib only

يستخدم فقط مكتبات Python المدمجة (urllib, json, datetime)
لا يحتاج ccxt أو pandas
"""

import urllib.request
import json
from datetime import datetime, timedelta
import time
import numpy as np


class BinanceDataFetcher:
    """
    جلب البيانات من Binance Spot Market API
    
    المزايا:
    - بدون مكتبات خارجية
    - محدود بـ 1200 طلب في الدقيقة
    - آمن وموثوق
    """
    
    BASE_URL = "https://api.binance.com/api/v3"
    
    @staticmethod
    def fetch_klines(
        symbol: str = 'BTCUSDT',
        interval: str = '15m',
        limit: int = 500,
        start_time: int = None
    ) -> list:
        """
        جلب شموع (OHLCV) من Binance
        
        Args:
            symbol: الرمز بدون / (BTCUSDT بدل BTC/USDT)
            interval: الفترة الزمنية (1m, 5m, 15m, 1h, 4h, 1d)
            limit: عدد الشموع (max 1000)
            start_time: وقت البداية بـ ميلي ثانية
        
        Returns:
            قائمة شموع [[timestamp, open, high, low, close, volume, ...], ...]
        """
        
        url = f"{BinanceDataFetcher.BASE_URL}/klines"
        
        params = {
            'symbol': symbol,
            'interval': interval,
            'limit': min(limit, 1000)  # Binance محدود بـ 1000
        }
        
        if start_time:
            params['startTime'] = start_time
        
        # بناء URL مع parameters
        param_str = '&'.join([f"{k}={v}" for k, v in params.items()])
        full_url = f"{url}?{param_str}"
        
        try:
            # جلب البيانات
            with urllib.request.urlopen(full_url, timeout=10) as response:
                data = json.loads(response.read().decode('utf-8'))
                return data
        
        except urllib.error.URLError as e:
            print(f"❌ خطأ الاتصال بـ Binance: {e}")
            return []
        except Exception as e:
            print(f"❌ خطأ: {e}")
            return []
    
    @staticmethod
    def fetch_historical_data(
        symbol: str = 'BTCUSDT',
        interval: str = '15m',
        days: int = 90
    ) -> dict:
        """
        جلب بيانات تاريخية (عدة أيام)
        
        بما أن حد البيانات 1000 شمعة، نحتاج جلب عدة مرات
        
        Args:
            symbol: الرمز
            interval: الفترة
            days: عدد الأيام الماضية
        
        Returns:
            Dictionary بـ {dates, open, high, low, close, volume}
        """
        
        # حساب عدد الشموع المطلوبة
        interval_minutes = BinanceDataFetcher._parse_interval(interval)
        total_candles = (days * 24 * 60) // interval_minutes
        
        print(f"📊 جاري جلب {total_candles} شمعة من Binance...")
        print(f"   الرمز: {symbol}")
        print(f"   الفترة: {interval}")
        print(f"   الأيام: {days}")
        print()
        
        all_candles = []
        
        # جلب في حلقات (كل مرة 1000 شمعة)
        batches = (total_candles // 1000) + 1
        
        for batch in range(batches):
            # احسب start_time للحلقة الحالية
            current_start = datetime.utcnow() - timedelta(days=days)
            current_start = current_start + timedelta(minutes=interval_minutes * (batch * 1000))
            start_ms = int(current_start.timestamp() * 1000)
            
            print(f"⏳ حلقة {batch + 1}/{batches}...")
            
            # جلب
            candles = BinanceDataFetcher.fetch_klines(
                symbol=symbol,
                interval=interval,
                limit=1000,
                start_time=start_ms
            )
            
            if not candles:
                print("❌ فشل جلب البيانات")
                break
            
            all_candles.extend(candles)
            
            # تأخير لتجنب Rate Limit
            if batch < batches - 1:
                time.sleep(0.1)
        
        print(f"✅ تم جلب {len(all_candles)} شمعة\n")
        
        # تحويل لنمط موحد
        dates = []
        open_prices = []
        high_prices = []
        low_prices = []
        close_prices = []
        volumes = []
        
        for candle in all_candles:
            timestamps = candle[0]
            open_p = float(candle[1])
            high_p = float(candle[2])
            low_p = float(candle[3])
            close_p = float(candle[4])
            volume = float(candle[7])  # Quote asset volume
            
            dates.append(datetime.fromtimestamp(timestamps / 1000))
            open_prices.append(open_p)
            high_prices.append(high_p)
            low_prices.append(low_p)
            close_prices.append(close_p)
            volumes.append(volume)
        
        return {
            'dates': np.array(dates),
            'open': np.array(open_prices),
            'high': np.array(high_prices),
            'low': np.array(low_prices),
            'close': np.array(close_prices),
            'volume': np.array(volumes)
        }
    
    @staticmethod
    def _parse_interval(interval: str) -> int:
        """تحويل الفترة الزمنية إلى دقائق"""
        mapping = {
            '1m': 1,
            '5m': 5,
            '15m': 15,
            '30m': 30,
            '1h': 60,
            '4h': 240,
            '1d': 1440
        }
        return mapping.get(interval, 15)


# ==================== اختبار ====================
if __name__ == "__main__":
    print("\n" + "="*70)
    print("🚀 اختبار جاتب جلب البيانات من Binance")
    print("="*70 + "\n")
    
    # جلب البيانات
    fetcher = BinanceDataFetcher()
    
    data = fetcher.fetch_historical_data(
        symbol='BTCUSDT',
        interval='15m',
        days=30  # 30 يوم من البيانات
    )
    
    if data and len(data['close']) > 0:
        print("✅ تم جلب البيانات بنجاح!\n")
        
        print("📊 معلومات البيانات:")
        print(f"   عدد الشموع: {len(data['close'])}")
        print(f"   الفترة: من {data['dates'][0]} إلى {data['dates'][-1]}")
        print(f"   النطاق السعري: ${data['close'].min():.2f} - ${data['close'].max():.2f}")
        print(f"   المتوسط: ${np.mean(data['close']):.2f}")
        
        print("\n📈 آخر 5 شموع:")
        print("-" * 70)
        for idx in range(-5, 0):
            i = len(data['close']) + idx
            print(f"[{i}] {data['dates'][i].strftime('%Y-%m-%d %H:%M')} | "
                  f"O:{data['open'][i]:.0f} H:{data['high'][i]:.0f} "
                  f"L:{data['low'][i]:.0f} C:{data['close'][i]:.0f} V:{data['volume'][i]:.0f}")
        
        print("\n✅ البيانات جاهزة للباكتستنق!")
    else:
        print("❌ فشل جلب البيانات")
