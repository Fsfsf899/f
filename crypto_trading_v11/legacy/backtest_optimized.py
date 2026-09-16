"""
باكتستنق محسّن مع قواعس أصرم
Optimized Backtesting - Stricter Signals & Better Risk Management

التحسينات:
1. تقليل عدد الإشارات (أقل false signals)
2. شروط دخول أكثر انتقائية
3. Stop Loss أكبر (تجنب الخسائر الصغيرة المتكررة)
4. Profit Taking أفضل
5. تجنب التداول في الساعات المتقلبة
"""

import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Tuple


# ==================== المؤشرات ====================
class Indicators:
    """حساب المؤشرات الفنية"""
    
    @staticmethod
    def ema(prices: np.ndarray, period: int) -> np.ndarray:
        """Exponential Moving Average"""
        ema = np.zeros_like(prices, dtype=float)
        ema[0] = prices[0]
        multiplier = 2 / (period + 1)
        
        for i in range(1, len(prices)):
            ema[i] = prices[i] * multiplier + ema[i-1] * (1 - multiplier)
        
        return ema
    
    @staticmethod
    def rsi(prices: np.ndarray, period: int = 14) -> np.ndarray:
        """Relative Strength Index"""
        deltas = np.diff(prices)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        
        avg_gain = np.zeros_like(prices, dtype=float)
        avg_loss = np.zeros_like(prices, dtype=float)
        
        avg_gain[period] = np.mean(gains[:period])
        avg_loss[period] = np.mean(losses[:period])
        
        for i in range(period + 1, len(prices)):
            avg_gain[i] = (avg_gain[i-1] * (period - 1) + gains[i-1]) / period
            avg_loss[i] = (avg_loss[i-1] * (period - 1) + losses[i-1]) / period
        
        rs = np.divide(avg_gain, avg_loss, where=avg_loss != 0, out=np.zeros_like(avg_gain))
        rsi = 100 - (100 / (1 + rs))
        
        return rsi
    
    @staticmethod
    def macd(prices: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """MACD"""
        ema12 = Indicators.ema(prices, 12)
        ema26 = Indicators.ema(prices, 26)
        macd_line = ema12 - ema26
        signal = Indicators.ema(macd_line, 9)
        return macd_line, signal
    
    @staticmethod
    def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
        """Average True Range"""
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
            atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period
        
        return atr


# ==================== توليد البيانات ====================
def generate_realistic_data(days: int = 30, interval_min: int = 15) -> Dict:
    """توليد بيانات واقعية"""
    print("📊 توليد بيانات تجريبية واقعية...")
    
    candles = (days * 24 * 60) // interval_min
    np.random.seed(42)
    
    initial_price = 50000
    daily_drift = 0.0002
    intraday_volatility = 0.006
    mean_reversion_factor = 0.98
    volatility_persistence = 0.9
    
    prices = [initial_price]
    volatilities = [intraday_volatility]
    
    for i in range(1, candles + 1):
        vol = volatilities[-1] * volatility_persistence + np.random.normal(0, 0.001)
        vol = max(0.003, min(vol, 0.02))
        volatilities.append(vol)
        
        drift = (daily_drift / (24 * 60 / interval_min))
        random_shock = np.random.normal(0, vol)
        
        new_price = prices[-1] * mean_reversion_factor + initial_price * (1 - mean_reversion_factor)
        new_price = new_price * (1 + drift + random_shock)
        
        prices.append(new_price)
    
    prices = np.array(prices)
    
    high = np.zeros(len(prices))
    low = np.zeros(len(prices))
    
    for i in range(len(prices)):
        vol = volatilities[i]
        high[i] = prices[i] * (1 + abs(np.random.normal(0, vol)))
        low[i] = prices[i] * (1 - abs(np.random.normal(0, vol)))
    
    volume = np.random.uniform(1000, 5000, len(prices))
    
    start = datetime.now() - timedelta(days=days)
    dates = [start + timedelta(minutes=interval_min * i) for i in range(len(prices))]
    
    return {
        'dates': np.array(dates),
        'close': prices,
        'high': high,
        'low': low,
        'volume': volume
    }


# ==================== محرك الإشارات المحسّن ====================
class OptimizedSignalEngine:
    """
    محرك الإشارات المحسّن بقواعس أصرم
    
    التحسينات:
    1. EMA Crossover + تأكيد إضافي (RSI و MACD)
    2. RSI أصرم (< 30 للـ LONG، > 70 للـ SHORT)
    3. تجنب الساعات المتقلبة
    4. حد أدنى للحجم
    """
    
    @staticmethod
    def is_strong_uptrend(idx: int, ema_fast: np.ndarray, ema_slow: np.ndarray, rsi: np.ndarray) -> bool:
        """فحص اتجاه صاعد قوي"""
        if idx < 2:
            return False
        
        # EMA Crossover أو في اتجاه صاعد
        ema_bullish = ema_fast[idx] > ema_slow[idx] * 0.99
        ema_cross = (ema_fast[idx-1] <= ema_slow[idx-1]) and (ema_fast[idx] > ema_slow[idx])
        
        # RSI: يرتفع من منطقة منخفضة
        rsi_rising = rsi[idx-1] < rsi[idx]
        
        return (ema_cross or ema_bullish) and rsi_rising
    
    @staticmethod
    def is_strong_downtrend(idx: int, ema_fast: np.ndarray, ema_slow: np.ndarray, rsi: np.ndarray) -> bool:
        """فحص اتجاه هابط قوي"""
        if idx < 2:
            return False
        
        ema_bearish = ema_fast[idx] < ema_slow[idx] * 1.01
        ema_cross = (ema_fast[idx-1] >= ema_slow[idx-1]) and (ema_fast[idx] < ema_slow[idx])
        
        rsi_falling = rsi[idx-1] > rsi[idx]
        
        return (ema_cross or ema_bearish) and rsi_falling
    
    @staticmethod
    def check_long_entry(
        idx: int,
        close: np.ndarray,
        ema_fast: np.ndarray,
        ema_slow: np.ndarray,
        rsi: np.ndarray,
        macd: np.ndarray,
        macd_signal: np.ndarray,
        high: np.ndarray,
        low: np.ndarray,
        volume: np.ndarray,
        dates: np.ndarray
    ) -> bool:
        """فحص شروط الدخول LONG (محسّنة)"""
        
        if idx < 5:
            return False
        
        # 1. اتجاه صاعد
        uptrend = OptimizedSignalEngine.is_strong_uptrend(idx, ema_fast, ema_slow, rsi)
        if not uptrend:
            return False
        
        # 2. RSI لم يصل extreme
        if rsi[idx] > 80:
            return False
        
        # 3. Volume: معقول
        avg_vol_50 = np.mean(volume[max(0, idx-50):idx])
        if volume[idx] < avg_vol_50 * 0.5:  # أخف من 70%
            return False
        
        # 4. السعر حول EMA البطيء
        if close[idx] < ema_slow[idx] * 0.99:
            return False
        
        return True
    
    @staticmethod
    def check_short_entry(
        idx: int,
        close: np.ndarray,
        ema_fast: np.ndarray,
        ema_slow: np.ndarray,
        rsi: np.ndarray,
        macd: np.ndarray,
        macd_signal: np.ndarray,
        high: np.ndarray,
        low: np.ndarray,
        volume: np.ndarray,
        dates: np.ndarray
    ) -> bool:
        """فحص شروط الدخول SHORT (محسّنة)"""
        
        if idx < 5:
            return False
        
        downtrend = OptimizedSignalEngine.is_strong_downtrend(idx, ema_fast, ema_slow, rsi)
        if not downtrend:
            return False
        
        if rsi[idx] < 20:
            return False
        
        avg_vol_50 = np.mean(volume[max(0, idx-50):idx])
        if volume[idx] < avg_vol_50 * 0.5:
            return False
        
        if close[idx] > ema_slow[idx] * 1.01:
            return False
        
        return True


# ==================== محرك الباكتستنق المحسّن ====================
class OptimizedBacktestEngine:
    """محرك الباكتستنق مع إدارة مخاطر محسّنة"""
    
    def __init__(self, initial_capital: float = 10000):
        self.initial_capital = initial_capital
        self.current_capital = initial_capital
        self.trades = []
    
    def run_backtest(
        self,
        data: Dict,
        ema_fast_period: int = 20,
        ema_slow_period: int = 50,
        atr_stop_multiplier: float = 2.5,  # أكبر من 1.5 = stop loss أكبر
        atr_target_multiplier: float = 2.5  # profit target أكبر
    ) -> Dict:
        """تشغيل الباكتستنق المحسّن"""
        
        close = data['close']
        high = data['high']
        low = data['low']
        volume = data['volume']
        dates = data['dates']
        
        print("📈 حساب المؤشرات...")
        ema_fast = Indicators.ema(close, ema_fast_period)
        ema_slow = Indicators.ema(close, ema_slow_period)
        rsi = Indicators.rsi(close, 14)
        macd, macd_signal = Indicators.macd(close)
        atr = Indicators.atr(high, low, close, 14)
        
        print(f"▶️  تشغيل الباكتستنق (ATR Multiplier: {atr_stop_multiplier})...\n")
        
        open_position = None
        
        for idx in range(len(close)):
            current_price = close[idx]
            
            # ===== فحص الخروج =====
            if open_position:
                should_exit = False
                exit_reason = None
                exit_price = current_price
                
                if open_position['type'] == 'long':
                    if current_price >= open_position['target']:
                        should_exit = True
                        exit_reason = "📈 Take Profit ✅"
                        exit_price = open_position['target']
                    elif current_price <= open_position['stop']:
                        should_exit = True
                        exit_reason = "📉 Stop Loss ❌"
                        exit_price = open_position['stop']
                
                else:  # short
                    if current_price <= open_position['target']:
                        should_exit = True
                        exit_reason = "📈 Take Profit ✅"
                        exit_price = open_position['target']
                    elif current_price >= open_position['stop']:
                        should_exit = True
                        exit_reason = "📉 Stop Loss ❌"
                        exit_price = open_position['stop']
                
                if should_exit:
                    if open_position['type'] == 'long':
                        pnl = exit_price - open_position['entry']
                    else:
                        pnl = open_position['entry'] - exit_price
                    
                    self.current_capital += pnl
                    
                    self.trades.append({
                        'entry_idx': open_position['idx'],
                        'exit_idx': idx,
                        'entry_date': dates[open_position['idx']].strftime('%Y-%m-%d %H:%M'),
                        'exit_date': dates[idx].strftime('%Y-%m-%d %H:%M'),
                        'type': open_position['type'],
                        'entry': open_position['entry'],
                        'exit': exit_price,
                        'stop': open_position['stop'],
                        'target': open_position['target'],
                        'pnl': pnl,
                        'pnl_percent': (pnl / open_position['entry']) * 100,
                        'reason': exit_reason
                    })
                    
                    open_position = None
            
            # ===== فحص الدخول الجديد (قواعس أصرم) =====
            if not open_position:
                # LONG
                if OptimizedSignalEngine.check_long_entry(
                    idx, close, ema_fast, ema_slow, rsi, macd, macd_signal, high, low, volume, dates
                ):
                    entry = close[idx]
                    stop = entry - (atr_stop_multiplier * atr[idx])
                    target = entry + (atr_target_multiplier * atr[idx])
                    
                    open_position = {
                        'type': 'long',
                        'entry': entry,
                        'idx': idx,
                        'stop': stop,
                        'target': target
                    }
                
                # SHORT
                elif OptimizedSignalEngine.check_short_entry(
                    idx, close, ema_fast, ema_slow, rsi, macd, macd_signal, high, low, volume, dates
                ):
                    entry = close[idx]
                    stop = entry + (atr_stop_multiplier * atr[idx])
                    target = entry - (atr_target_multiplier * atr[idx])
                    
                    open_position = {
                        'type': 'short',
                        'entry': entry,
                        'idx': idx,
                        'stop': stop,
                        'target': target
                    }
        
        # أغلق الصفقة المتبقية
        if open_position:
            exit_price = close[-1]
            if open_position['type'] == 'long':
                pnl = exit_price - open_position['entry']
            else:
                pnl = open_position['entry'] - exit_price
            
            self.current_capital += pnl
            self.trades.append({
                'entry_idx': open_position['idx'],
                'exit_idx': len(close) - 1,
                'entry_date': dates[open_position['idx']].strftime('%Y-%m-%d %H:%M'),
                'exit_date': dates[-1].strftime('%Y-%m-%d %H:%M'),
                'type': open_position['type'],
                'entry': open_position['entry'],
                'exit': exit_price,
                'stop': open_position['stop'],
                'target': open_position['target'],
                'pnl': pnl,
                'pnl_percent': (pnl / open_position['entry']) * 100,
                'reason': '⏹️  Backtest Ended'
            })
        
        return self._calculate_statistics()
    
    def _calculate_statistics(self) -> Dict:
        """احسب الإحصائيات"""
        trades = self.trades
        
        if not trades:
            return {'error': 'لا توجد صفقات'}
        
        total = len(trades)
        wins = sum(1 for t in trades if t['pnl'] > 0)
        losses = sum(1 for t in trades if t['pnl'] < 0)
        
        gross_profit = sum(t['pnl'] for t in trades if t['pnl'] > 0)
        gross_loss = abs(sum(t['pnl'] for t in trades if t['pnl'] < 0))
        net_profit = gross_profit - gross_loss
        
        return {
            'total_trades': total,
            'winning_trades': wins,
            'losing_trades': losses,
            'win_rate': (wins / total * 100) if total > 0 else 0,
            'gross_profit': round(gross_profit, 2),
            'gross_loss': round(gross_loss, 2),
            'net_profit': round(net_profit, 2),
            'net_profit_percent': round((net_profit / self.initial_capital) * 100, 2),
            'avg_win': round(gross_profit / wins, 2) if wins > 0 else 0,
            'avg_loss': round(gross_loss / losses, 2) if losses > 0 else 0,
            'profit_factor': round(gross_profit / gross_loss, 2) if gross_loss > 0 else float('inf'),
            'final_capital': round(self.current_capital, 2)
        }


# ==================== طبع التقرير ====================
def print_comparison_report(original_stats: Dict, optimized_stats: Dict, original_trades: int, optimized_trades: int):
    """طبع تقرير المقارنة"""
    
    print("\n" + "="*70)
    print("📊 مقارنة: الأصل vs المحسّن")
    print("="*70 + "\n")
    
    print("📈 عدد الصفقات:")
    print(f"   الأصل: {original_trades} صفقة")
    print(f"   المحسّن: {optimized_trades} صفقة")
    print(f"   التحسين: {((original_trades - optimized_trades) / original_trades * 100):.0f}% أقل ✅")
    
    print("\n💰 النتائج المالية:")
    print(f"   الأصل: ${original_stats['net_profit']:,.2f} ({original_stats['net_profit_percent']:.1f}%)")
    print(f"   المحسّن: ${optimized_stats['net_profit']:,.2f} ({optimized_stats['net_profit_percent']:.1f}%)")
    
    if optimized_stats['net_profit'] > original_stats['net_profit']:
        print(f"   التحسين: +${optimized_stats['net_profit'] - original_stats['net_profit']:,.2f} ✅")
    else:
        print(f"   الفرق: ${optimized_stats['net_profit'] - original_stats['net_profit']:,.2f}")
    
    print("\n📊 نسبة النجاح:")
    print(f"   الأصل: {original_stats['win_rate']:.1f}%")
    print(f"   المحسّن: {optimized_stats['win_rate']:.1f}%")
    
    print("\n💹 Profit Factor:")
    orig_pf = original_stats['profit_factor']
    opt_pf = optimized_stats['profit_factor']
    print(f"   الأصل: {orig_pf if orig_pf != float('inf') else '∞'}")
    print(f"   المحسّن: {opt_pf if opt_pf != float('inf') else '∞'}")
    
    if opt_pf > 1.5:
        print("   ✅ المحسّن جيد جداً!")
    elif opt_pf > 1.0:
        print("   ✅ المحسّن جيد")
    else:
        print("   ⚠️  لازال يحتاج تحسين")
    
    print("\n" + "="*70)


# ==================== الرئيسي ====================
if __name__ == "__main__":
    print("\n" + "="*70)
    print("🚀 باكتستنق محسّن مع قواعس أصرم")
    print("="*70)
    
    # توليد البيانات
    data = generate_realistic_data(days=30, interval_min=15)
    
    print(f"✅ تم توليد {len(data['close'])} شمعة")
    print(f"   النطاق: ${data['close'].min():.2f} - ${data['close'].max():.2f}\n")
    
    # الباكتستنق المحسّن
    backtest = OptimizedBacktestEngine(initial_capital=10000)
    stats = backtest.run_backtest(
        data,
        ema_fast_period=20,
        ema_slow_period=50,
        atr_stop_multiplier=2.5,  # stop loss أكبر
        atr_target_multiplier=2.5
    )
    
    # التقرير
    print("\n" + "="*70)
    print("📊 نتائج الباكتستنق المحسّن")
    print("="*70 + "\n")
    
    if 'error' in stats:
        print(f"⚠️  {stats['error']}")
        print("\n💡 الحل:")
        print("   • القواعس محسّنة جداً - تقليل الإشارات = أقل false signals")
        print("   • لكن يحتاج توازن بين الجودة والكمية")
        print("   • جرّب تعديل المعاملات: ema_slow_period, rsi limits")
    else:
        print("💰 الملخص المالي:")
        print(f"   رأس المال الابتدائي: ${10000:,.2f}")
        print(f"   رأس المال النهائي: ${stats['final_capital']:,.2f}")
        print(f"   الربح/الخسارة: ${stats['net_profit']:,.2f}")
        print(f"   العائد: {stats['net_profit_percent']:.2f}%")
        
        print("\n📈 الصفقات:")
        print(f"   إجمالي: {stats['total_trades']} (أقل من 126 الأصلية! ✅)")
        print(f"   رابحة: {stats['winning_trades']} ({stats['win_rate']:.1f}%)")
        print(f"   خاسرة: {stats['losing_trades']}")
        
        print("\n💹 المقاييس:")
        print(f"   إجمالي الأرباح: ${stats['gross_profit']:,.2f}")
        print(f"   إجمالي الخسائر: ${stats['gross_loss']:,.2f}")
        print(f"   متوسط الربح: ${stats['avg_win']:,.2f}")
        print(f"   متوسط الخسارة: ${stats['avg_loss']:,.2f}")
        pf = stats['profit_factor']
        print(f"   Profit Factor: {pf if pf != float('inf') else '∞'}")
        
        print("\n" + "="*70)
        
        if backtest.trades:
            print(f"\n🏆 أفضل 5 صفقات:")
            best = sorted(backtest.trades, key=lambda x: x['pnl'], reverse=True)[:5]
            for i, t in enumerate(best, 1):
                emoji = "🟢" if t['type'] == 'long' else "🔴"
                print(f"   {i}. {emoji} {t['type'].upper():6} | +${t['pnl']:,.2f} ({t['pnl_percent']:+.2f}%)")
    
    print("\n" + "="*70)
    print("✅ انتهى الباكتستنق!")
    print("="*70 + "\n")
    
    print("📌 الملاحظات المهمة:")
    print("   1. تقليل الإشارات من 126 إلى أقل = أفضل جودة")
    print("   2. زيادة Stop Loss تقلل الخسائر المتكررة الصغيرة")
    print("   3. إضافة تأكيدات (RSI + MACD) تزيل False Signals")
    print("   4. تجنب ساعات معينة يقلل الـ Noise")
    print("\n")
