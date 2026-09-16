"""
برنامج باكتستنق مستقل - لا يحتاج مكتبات خارجية
Standalone Backtesting Script - Self-contained

يستخدم بيانات تجريبية واقعية (محاكاة حركة السعر الحقيقية)
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import json


# ==================== حساب المؤشرات ====================
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


# ==================== محرك الإشارات ====================
class SignalEngine:
    """فحص شروط الدخول والخروج"""
    
    @staticmethod
    def check_long_entry(
        idx: int,
        close: np.ndarray,
        ema_fast: np.ndarray,
        ema_slow: np.ndarray,
        rsi: np.ndarray,
        macd: np.ndarray,
        macd_signal: np.ndarray,
        volume: np.ndarray,
        support: np.ndarray
    ) -> bool:
        """فحص شروط الدخول LONG"""
        if idx < 2:
            return False
        
        # 1. EMA Crossover
        ema_cross = ema_fast[idx-1] <= ema_slow[idx-1] and ema_fast[idx] > ema_slow[idx]
        
        # 2. RSI بين 40-60
        rsi_ok = 40 <= rsi[idx] <= 60
        
        # 3. MACD Crossover
        macd_cross = macd[idx-1] <= macd_signal[idx-1] and macd[idx] > macd_signal[idx]
        
        # 4. Volume >= 120%
        avg_vol = np.mean(volume[max(0, idx-20):idx])
        vol_ok = volume[idx] >= avg_vol * 1.2
        
        # 5. فوق الدعم
        support_ok = close[idx] > support[idx]
        
        return ema_cross and rsi_ok and macd_cross and vol_ok and support_ok
    
    @staticmethod
    def check_short_entry(
        idx: int,
        close: np.ndarray,
        ema_fast: np.ndarray,
        ema_slow: np.ndarray,
        rsi: np.ndarray,
        macd: np.ndarray,
        macd_signal: np.ndarray,
        volume: np.ndarray,
        resistance: np.ndarray
    ) -> bool:
        """فحص شروط الدخول SHORT"""
        if idx < 2:
            return False
        
        # 1. EMA Crossover معكوس
        ema_cross = ema_fast[idx-1] >= ema_slow[idx-1] and ema_fast[idx] < ema_slow[idx]
        
        # 2. RSI بين 40-60
        rsi_ok = 40 <= rsi[idx] <= 60
        
        # 3. MACD معكوس
        macd_cross = macd[idx-1] >= macd_signal[idx-1] and macd[idx] < macd_signal[idx]
        
        # 4. Volume
        avg_vol = np.mean(volume[max(0, idx-20):idx])
        vol_ok = volume[idx] >= avg_vol * 1.2
        
        # 5. تحت المقاومة
        resistance_ok = close[idx] < resistance[idx]
        
        return ema_cross and rsi_ok and macd_cross and vol_ok and resistance_ok


# ==================== محرك الباكتستنق ====================
class BacktestEngine:
    """محرك الباكتستنق الرئيسي"""
    
    def __init__(
        self,
        initial_capital: float = 10000,
        risk_per_trade: float = 0.02,
        max_daily_loss: float = 0.05
    ):
        self.initial_capital = initial_capital
        self.risk_per_trade = risk_per_trade
        self.max_daily_loss = max_daily_loss
        
        self.trades = []
        self.current_capital = initial_capital
    
    def run_backtest(
        self,
        dates: np.ndarray,
        close: np.ndarray,
        high: np.ndarray,
        low: np.ndarray,
        volume: np.ndarray,
        ema_fast: np.ndarray,
        ema_slow: np.ndarray,
        rsi: np.ndarray,
        macd: np.ndarray,
        macd_signal: np.ndarray,
        atr: np.ndarray,
        support: np.ndarray,
        resistance: np.ndarray
    ) -> Dict:
        """تشغيل الباكتستنق"""
        
        open_position = None  # None أو {'entry_price', 'entry_idx', 'stop_loss', 'take_profit', 'direction'}
        
        for idx in range(len(close)):
            current_date = pd.to_datetime(dates[idx]).date()
            
            # ===== فحص الخروج من الصفقة المفتوحة =====
            if open_position:
                current_price = close[idx]
                exit_triggered = False
                exit_reason = None
                exit_price = current_price
                
                if open_position['direction'] == 'long':
                    # وصل الهدف
                    if current_price >= open_position['take_profit']:
                        exit_triggered = True
                        exit_reason = "Take Profit ✅"
                        exit_price = open_position['take_profit']
                    
                    # وصل الوقف
                    elif current_price <= open_position['stop_loss']:
                        exit_triggered = True
                        exit_reason = "Stop Loss ❌"
                        exit_price = open_position['stop_loss']
                
                else:  # short
                    # وصل الهدف
                    if current_price <= open_position['take_profit']:
                        exit_triggered = True
                        exit_reason = "Take Profit ✅"
                        exit_price = open_position['take_profit']
                    
                    # وصل الوقف
                    elif current_price >= open_position['stop_loss']:
                        exit_triggered = True
                        exit_reason = "Stop Loss ❌"
                        exit_price = open_position['stop_loss']
                
                if exit_triggered:
                    # احسب الربح/الخسارة
                    if open_position['direction'] == 'long':
                        pnl = exit_price - open_position['entry_price']
                    else:
                        pnl = open_position['entry_price'] - exit_price
                    
                    # حدّث رأس المال
                    self.current_capital += pnl
                    
                    # سجل الصفقة
                    self.trades.append({
                        'entry_idx': open_position['entry_idx'],
                        'exit_idx': idx,
                        'entry_date': pd.to_datetime(dates[open_position['entry_idx']]).strftime('%Y-%m-%d %H:%M'),
                        'exit_date': pd.to_datetime(dates[idx]).strftime('%Y-%m-%d %H:%M'),
                        'direction': open_position['direction'],
                        'entry_price': round(open_position['entry_price'], 2),
                        'exit_price': round(exit_price, 2),
                        'stop_loss': round(open_position['stop_loss'], 2),
                        'take_profit': round(open_position['take_profit'], 2),
                        'pnl': round(pnl, 2),
                        'exit_reason': exit_reason
                    })
                    
                    open_position = None
            
            # ===== فحص الدخول الجديد =====
            if not open_position:
                # فحص LONG
                if SignalEngine.check_long_entry(
                    idx, close, ema_fast, ema_slow, rsi, macd, macd_signal, volume, support
                ):
                    entry_price = close[idx]
                    stop_loss = entry_price - (1.5 * atr[idx])
                    take_profit = entry_price + (2.0 * 1.5 * atr[idx])
                    
                    open_position = {
                        'entry_idx': idx,
                        'entry_price': entry_price,
                        'stop_loss': stop_loss,
                        'take_profit': take_profit,
                        'direction': 'long'
                    }
                
                # فحص SHORT (إذا ما في LONG)
                elif SignalEngine.check_short_entry(
                    idx, close, ema_fast, ema_slow, rsi, macd, macd_signal, volume, resistance
                ):
                    entry_price = close[idx]
                    stop_loss = entry_price + (1.5 * atr[idx])
                    take_profit = entry_price - (2.0 * 1.5 * atr[idx])
                    
                    open_position = {
                        'entry_idx': idx,
                        'entry_price': entry_price,
                        'stop_loss': stop_loss,
                        'take_profit': take_profit,
                        'direction': 'short'
                    }
        
        # أغلق أي صفقة متبقية
        if open_position:
            exit_price = close[-1]
            if open_position['direction'] == 'long':
                pnl = exit_price - open_position['entry_price']
            else:
                pnl = open_position['entry_price'] - exit_price
            
            self.current_capital += pnl
            self.trades.append({
                'entry_idx': open_position['entry_idx'],
                'exit_idx': len(close) - 1,
                'entry_date': pd.to_datetime(dates[open_position['entry_idx']]).strftime('%Y-%m-%d %H:%M'),
                'exit_date': pd.to_datetime(dates[-1]).strftime('%Y-%m-%d %H:%M'),
                'direction': open_position['direction'],
                'entry_price': round(open_position['entry_price'], 2),
                'exit_price': round(exit_price, 2),
                'stop_loss': round(open_position['stop_loss'], 2),
                'take_profit': round(open_position['take_profit'], 2),
                'pnl': round(pnl, 2),
                'exit_reason': 'Backtest Ended'
            })
        
        return self._calculate_statistics()
    
    def _calculate_statistics(self) -> Dict:
        """احسب الإحصائيات"""
        trades = self.trades
        
        if not trades:
            return {
                'total_trades': 0,
                'winning_trades': 0,
                'losing_trades': 0,
                'win_rate': 0,
                'gross_profit': 0,
                'gross_loss': 0,
                'net_profit': 0,
                'net_profit_percent': 0,
                'avg_win': 0,
                'avg_loss': 0,
                'profit_factor': 0,
                'final_capital': self.current_capital
            }
        
        total = len(trades)
        winning = sum(1 for t in trades if t['pnl'] > 0)
        losing = sum(1 for t in trades if t['pnl'] < 0)
        
        gross_profit = sum(t['pnl'] for t in trades if t['pnl'] > 0)
        gross_loss = abs(sum(t['pnl'] for t in trades if t['pnl'] < 0))
        net_profit = gross_profit - gross_loss
        
        win_rate = winning / total if total > 0 else 0
        avg_win = gross_profit / winning if winning > 0 else 0
        avg_loss = gross_loss / losing if losing > 0 else 0
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
        
        return {
            'total_trades': total,
            'winning_trades': winning,
            'losing_trades': losing,
            'win_rate': round(win_rate * 100, 2),
            'gross_profit': round(gross_profit, 2),
            'gross_loss': round(gross_loss, 2),
            'net_profit': round(net_profit, 2),
            'net_profit_percent': round((net_profit / self.initial_capital) * 100, 2),
            'avg_win': round(avg_win, 2),
            'avg_loss': round(avg_loss, 2),
            'profit_factor': round(profit_factor, 2) if profit_factor != float('inf') else "∞",
            'final_capital': round(self.current_capital, 2)
        }


# ==================== إنشاء بيانات تجريبية واقعية ====================
def generate_realistic_price_data(days: int = 30, timeframe: int = 15) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    توليد بيانات أسعار واقعية (محاكاة حركة السوق الحقيقية)
    
    يحاكي:
    - Trend (اتجاه طويل الأجل)
    - Mean Reversion (العودة للمتوسط)
    - Volatility (التقلبات)
    """
    
    # عدد الشموع
    candles = (days * 24 * 60) // timeframe + 1
    
    # الأسعار (محاكاة حركة Brownian)
    np.random.seed(42)
    
    # ابدأ من 50000 (سعر BTC تقريبي)
    price = 50000
    
    close_prices = [price]
    high_prices = [price]
    low_prices = [price]
    volumes = []
    
    # معاملات المحاكاة
    drift = 0.0001  # الاتجاه الصاعد الخفيف
    volatility = 0.008  # 0.8% تقلب لكل شمعة
    mean_reversion = 0.95  # 95% من آخر سعر
    
    for i in range(1, candles + 1):
        # حركة عشوائية مع اتجاه
        random_change = np.random.normal(drift, volatility)
        
        # mean reversion (العودة للسعر السابق)
        price = price * mean_reversion + close_prices[-1] * (1 - mean_reversion)
        price = price * (1 + random_change)
        
        # تقلبات اليوم (high/low)
        daily_volatility = abs(np.random.normal(0, volatility * 2))
        high = price * (1 + daily_volatility)
        low = price * (1 - daily_volatility)
        
        close_prices.append(price)
        high_prices.append(high)
        low_prices.append(low)
        volumes.append(np.random.randint(1000, 10000))
    
    # تواريخ
    start_date = datetime.now() - timedelta(days=days)
    dates = [start_date + timedelta(minutes=timeframe*i) for i in range(len(close_prices))]
    
    # تأكد أن جميع المصفوفات بنفس الطول
    min_len = min(len(close_prices), len(high_prices), len(low_prices), len(volumes), len(dates))
    
    return (
        np.array(dates[:min_len]),
        np.array(close_prices[:min_len]),
        np.array(high_prices[:min_len]),
        np.array(low_prices[:min_len]),
        np.array(volumes[:min_len])
    )


# ==================== الرئيسي ====================
if __name__ == "__main__":
    print("\n" + "="*70)
    print("🚀 برنامج الباكتستنق المستقل")
    print("="*70 + "\n")
    
    # توليد البيانات
    print("📊 توليد بيانات تجريبية واقعية...")
    dates, close, high, low, volume = generate_realistic_price_data(days=30, timeframe=15)
    
    print(f"✅ تم توليد {len(close)} شمعة")
    print(f"   الفترة: من {pd.to_datetime(dates[0]).strftime('%Y-%m-%d')} إلى {pd.to_datetime(dates[-1]).strftime('%Y-%m-%d')}")
    print(f"   النطاق السعري: ${close.min():.2f} - ${close.max():.2f}\n")
    
    # حساب المؤشرات
    print("📈 حساب المؤشرات الفنية...")
    calc = IndicatorsCalculator()
    
    ema_fast = calc.calculate_ema(close, period=20)
    ema_slow = calc.calculate_ema(close, period=50)
    rsi = calc.calculate_rsi(close, period=14)
    macd, macd_signal, _ = calc.calculate_macd(close)
    atr = calc.calculate_atr(high, low, close, period=14)
    
    # Dعم والمقاومة (بسيط)
    support = np.array([np.min(low[max(0, i-20):i+1]) if i >= 1 else low[i] for i in range(len(low))])
    resistance = np.array([np.max(high[max(0, i-20):i+1]) if i >= 1 else high[i] for i in range(len(high))])
    
    print("✅ تم حساب المؤشرات\n")
    
    # تشغيل الباكتستنق
    print("▶️  تشغيل محرك الباكتستنق...")
    print("-" * 70)
    
    backtest = BacktestEngine(initial_capital=10000, risk_per_trade=0.02)
    
    results = backtest.run_backtest(
        dates, close, high, low, volume,
        ema_fast, ema_slow, rsi, macd, macd_signal, atr,
        support, resistance
    )
    
    # طبع التقرير
    print("\n" + "="*70)
    print("📊 تقرير النتائج النهائي")
    print("="*70 + "\n")
    
    print("💰 النتائج المالية:")
    print(f"   رأس المال الابتدائي: ${10000:,.2f}")
    print(f"   رأس المال النهائي: ${results['final_capital']:,.2f}")
    print(f"   الربح الصافي: ${results['net_profit']:,.2f}")
    print(f"   نسبة العائد: {results['net_profit_percent']:.2f}%")
    
    print("\n📈 إحصائيات الصفقات:")
    print(f"   إجمالي الصفقات: {results['total_trades']}")
    print(f"   صفقات رابحة: {results['winning_trades']} ({results['win_rate']}%)")
    print(f"   صفقات خاسرة: {results['losing_trades']}")
    
    print("\n💹 الأرباح والخسائر:")
    print(f"   إجمالي الأرباح: ${results['gross_profit']:,.2f}")
    print(f"   إجمالي الخسائر: ${results['gross_loss']:,.2f}")
    print(f"   متوسط الربح: ${results['avg_win']:,.2f}")
    print(f"   متوسط الخسارة: ${results['avg_loss']:,.2f}")
    print(f"   Profit Factor: {results['profit_factor']}")
    
    print("\n" + "="*70)
    
    if backtest.trades:
        print("\n📋 تفاصيل جميع الصفقات:")
        print("-" * 70)
        
        for i, trade in enumerate(backtest.trades, 1):
            direction_emoji = "🟢 LONG" if trade['direction'] == 'long' else "🔴 SHORT"
            pnl_sign = "+" if trade['pnl'] >= 0 else ""
            
            print(f"\n{i}. {direction_emoji}")
            print(f"   الدخول: {trade['entry_date']} @ ${trade['entry_price']:.2f}")
            print(f"   الخروج: {trade['exit_date']} @ ${trade['exit_price']:.2f} ({trade['exit_reason']})")
            print(f"   الربح/الخسارة: {pnl_sign}${trade['pnl']:.2f}")
    
    print("\n" + "="*70)
    print("✅ انتهى الباكتستنق!")
    print("="*70 + "\n")
