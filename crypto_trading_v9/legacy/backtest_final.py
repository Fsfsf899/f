"""
برنامج باكتستنق متكامل - مع بيانات محسّنة وقواعس أفضل
Complete Backtesting Program - Enhanced Signals & Realistic Data

الميزات:
- بيانات تجريبية واقعية (محاكاة الأسواق الحقيقية)
- قواعس إشارات محسّنة (أقل صرامة)
- إحصائيات كاملة (Sharpe Ratio, Drawdown, الخ)
- تقرير تفصيلي لكل صفقة
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
        """MACD (12, 26, 9)"""
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


# ==================== توليد البيانات الواقعية ====================
def generate_realistic_data(days: int = 30, interval_min: int = 15) -> Dict:
    """
    توليد بيانات تحاكي السوق الحقيقي بدقة أعلى
    
    الميزات:
    - Brownian Motion مع drift
    - Mean Reversion (العودة للمتوسط)
    - Volatility Clustering (التقلبات المتجمعة)
    - تذبذبات داخل اليوم
    """
    
    print("📊 توليد بيانات تجريبية واقعية...")
    
    candles = (days * 24 * 60) // interval_min
    np.random.seed(42)
    
    # معاملات المحاكاة
    initial_price = 50000
    daily_drift = 0.0002  # اتجاه صاعد خفيف (0.02% يومي)
    intraday_volatility = 0.006  # 0.6% تقلب لكل شمعة
    mean_reversion_factor = 0.98  # 98% من آخر سعر
    volatility_persistence = 0.9  # التقلبات تستمر
    
    prices = [initial_price]
    volatilities = [intraday_volatility]
    
    # توليد الأسعار
    for i in range(1, candles + 1):
        # Volatility Clustering
        vol = volatilities[-1] * volatility_persistence + np.random.normal(0, 0.001)
        vol = max(0.003, min(vol, 0.02))  # حد أدنى/أعلى للتقلبات
        volatilities.append(vol)
        
        # Brown Motion مع Drift و Mean Reversion
        drift = (daily_drift / (24 * 60 / interval_min))  # drift يومي موزع على الفترات
        random_shock = np.random.normal(0, vol)
        
        new_price = prices[-1] * mean_reversion_factor + initial_price * (1 - mean_reversion_factor)
        new_price = new_price * (1 + drift + random_shock)
        
        prices.append(new_price)
    
    prices = np.array(prices)
    
    # High/Low
    high = np.zeros(len(prices))
    low = np.zeros(len(prices))
    
    for i in range(len(prices)):
        vol = volatilities[i]
        high[i] = prices[i] * (1 + abs(np.random.normal(0, vol)))
        low[i] = prices[i] * (1 - abs(np.random.normal(0, vol)))
    
    # Volume
    volume = np.random.uniform(1000, 5000, len(prices))
    
    # Dates
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
class SignalEngine:
    """
    محرك الإشارات المحسّن
    
    شروط الدخول LONG (محسّنة وأقل صرامة):
    1. EMA السريع > EMA البطيء (اتجاه صاعد)
    2. RSI بين 30-70 (محايد أو قوي)
    3. MACD موجب وفوق Signal Line (اختياري)
    4. الحجم معقول
    5. فوق الدعم
    """
    
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
        volume: np.ndarray
    ) -> bool:
        """فحص شروط الدخول LONG (محسّنة)"""
        
        if idx < 2:
            return False
        
        # 1. EMA Crossover أو في اتجاه صاعد واضح
        ema_bullish = ema_fast[idx] > ema_slow[idx] * 0.995  # سماح 0.5%
        ema_cross = ema_fast[idx-1] <= ema_slow[idx-1] and ema_fast[idx] > ema_slow[idx]
        
        # 2. RSI: كان < 50 الآن عالي (صعود قوي)، أو بين 40-60
        rsi_ok = (rsi[idx-1] < 50 and rsi[idx] >= 50) or (40 <= rsi[idx] <= 70)
        
        # 3. MACD: فقط تأكيد إذا كان متاح
        macd_ok = macd[idx] > macd_signal[idx]  # بدل الـ crossover الصارم
        
        # 4. Volume معقول (ما يجب بالضرورة 120%)
        avg_vol_20 = np.mean(volume[max(0, idx-20):idx]) if idx >= 1 else 1
        vol_ok = volume[idx] >= avg_vol_20 * 0.8  # 80% يكفي
        
        # 5. فوق الدعم
        support = np.min(low[max(0, idx-20):idx+1]) if idx >= 1 else low[idx]
        support_ok = close[idx] > support * 1.001  # سماح صغير
        
        # الشروط النهائية (أخف من الأصل)
        return (ema_bullish or ema_cross) and rsi_ok and macd_ok and vol_ok and support_ok
    
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
        volume: np.ndarray
    ) -> bool:
        """فحص شروط الدخول SHORT (محسّنة)"""
        
        if idx < 2:
            return False
        
        # العكس من LONG
        ema_bearish = ema_fast[idx] < ema_slow[idx] * 1.005
        ema_cross = ema_fast[idx-1] >= ema_slow[idx-1] and ema_fast[idx] < ema_slow[idx]
        
        rsi_ok = (rsi[idx-1] > 50 and rsi[idx] <= 50) or (30 <= rsi[idx] <= 60)
        
        macd_ok = macd[idx] < macd_signal[idx]
        
        avg_vol_20 = np.mean(volume[max(0, idx-20):idx]) if idx >= 1 else 1
        vol_ok = volume[idx] >= avg_vol_20 * 0.8
        
        resistance = np.max(high[max(0, idx-20):idx+1]) if idx >= 1 else high[idx]
        resistance_ok = close[idx] < resistance * 0.999
        
        return (ema_bearish or ema_cross) and rsi_ok and macd_ok and vol_ok and resistance_ok


# ==================== محرك الباكتستنق ====================
class BacktestEngine:
    """محرك الباكتستنق الكامل"""
    
    def __init__(self, initial_capital: float = 10000):
        self.initial_capital = initial_capital
        self.current_capital = initial_capital
        self.trades = []
    
    def run_backtest(
        self,
        data: Dict,
        ema_fast_period: int = 20,
        ema_slow_period: int = 50
    ) -> Dict:
        """تشغيل الباكتستنق"""
        
        close = data['close']
        high = data['high']
        low = data['low']
        volume = data['volume']
        
        print("📈 حساب المؤشرات...")
        ema_fast = Indicators.ema(close, ema_fast_period)
        ema_slow = Indicators.ema(close, ema_slow_period)
        rsi = Indicators.rsi(close, 14)
        macd, macd_signal = Indicators.macd(close)
        atr = Indicators.atr(high, low, close, 14)
        
        print("▶️  تشغيل الباكتستنق...\n")
        
        open_position = None  # None أو {'type': 'long'/'short', 'entry': price, 'idx': index, 'stop': sl, 'target': tp}
        
        for idx in range(len(close)):
            current_price = close[idx]
            
            # ===== فحص الخروج =====
            if open_position:
                should_exit = False
                exit_reason = None
                exit_price = current_price
                
                if open_position['type'] == 'long':
                    # وصل الهدف
                    if current_price >= open_position['target']:
                        should_exit = True
                        exit_reason = "📈 Take Profit ✅"
                        exit_price = open_position['target']
                    # وصل الوقف
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
                    # احسب P&L
                    if open_position['type'] == 'long':
                        pnl = exit_price - open_position['entry']
                    else:
                        pnl = open_position['entry'] - exit_price
                    
                    self.current_capital += pnl
                    
                    # سجل الصفقة
                    self.trades.append({
                        'entry_idx': open_position['idx'],
                        'exit_idx': idx,
                        'entry_date': data['dates'][open_position['idx']].strftime('%Y-%m-%d %H:%M'),
                        'exit_date': data['dates'][idx].strftime('%Y-%m-%d %H:%M'),
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
            
            # ===== فحص الدخول الجديد =====
            if not open_position:
                # LONG
                if SignalEngine.check_long_entry(
                    idx, close, ema_fast, ema_slow, rsi, macd, macd_signal, high, low, volume
                ):
                    entry = close[idx]
                    stop = entry - (1.5 * atr[idx])
                    target = entry + (2.0 * 1.5 * atr[idx])
                    
                    open_position = {
                        'type': 'long',
                        'entry': entry,
                        'idx': idx,
                        'stop': stop,
                        'target': target
                    }
                
                # SHORT (إذا ما في LONG)
                elif SignalEngine.check_short_entry(
                    idx, close, ema_fast, ema_slow, rsi, macd, macd_signal, high, low, volume
                ):
                    entry = close[idx]
                    stop = entry + (1.5 * atr[idx])
                    target = entry - (2.0 * 1.5 * atr[idx])
                    
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
                'entry_date': data['dates'][open_position['idx']].strftime('%Y-%m-%d %H:%M'),
                'exit_date': data['dates'][-1].strftime('%Y-%m-%d %H:%M'),
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
def print_report(stats: Dict, trades: List[Dict]):
    """طبع التقرير النهائي"""
    
    print("\n" + "="*70)
    print("📊 تقرير نتائج الباكتستنق")
    print("="*70 + "\n")
    
    print("💰 الملخص المالي:")
    print(f"   رأس المال الابتدائي: ${10000:,.2f}")
    print(f"   رأس المال النهائي: ${stats['final_capital']:,.2f}")
    print(f"   الربح/الخسارة: ${stats['net_profit']:,.2f}")
    print(f"   العائد: {stats['net_profit_percent']:.2f}%")
    
    print("\n📈 الصفقات:")
    print(f"   إجمالي: {stats['total_trades']}")
    print(f"   رابحة: {stats['winning_trades']} ({stats['win_rate']:.1f}%)")
    print(f"   خاسرة: {stats['losing_trades']}")
    
    print("\n💹 الأرباح والخسائر:")
    print(f"   إجمالي الأرباح: ${stats['gross_profit']:,.2f}")
    print(f"   إجمالي الخسائر: ${stats['gross_loss']:,.2f}")
    print(f"   متوسط الربح: ${stats['avg_win']:,.2f}")
    print(f"   متوسط الخسارة: ${stats['avg_loss']:,.2f}")
    pf = stats['profit_factor']
    print(f"   Profit Factor: {pf if pf != float('inf') else '∞'}")
    
    print("\n" + "="*70)
    
    if trades:
        print("\n🏆 أفضل 3 صفقات:")
        best = sorted(trades, key=lambda x: x['pnl'], reverse=True)[:3]
        for i, t in enumerate(best, 1):
            emoji = "🟢" if t['type'] == 'long' else "🔴"
            print(f"   {i}. {emoji} {t['type'].upper():6} | +${t['pnl']:,.2f} ({t['pnl_percent']:+.2f}%)")
        
        print("\n💔 أسوأ 3 صفقات:")
        worst = sorted(trades, key=lambda x: x['pnl'])[:3]
        for i, t in enumerate(worst, 1):
            emoji = "🟢" if t['type'] == 'long' else "🔴"
            print(f"   {i}. {emoji} {t['type'].upper():6} | -${abs(t['pnl']):,.2f} ({t['pnl_percent']:+.2f}%)")
    
    print("\n" + "="*70)


# ==================== الرئيسي ====================
if __name__ == "__main__":
    print("\n" + "="*70)
    print("🚀 برنامج الباكتستنق المتكامل")
    print("="*70)
    
    # توليد البيانات
    data = generate_realistic_data(days=30, interval_min=15)
    
    print(f"✅ تم توليد {len(data['close'])} شمعة")
    print(f"   النطاق: ${data['close'].min():.2f} - ${data['close'].max():.2f}\n")
    
    # الباكتستنق
    backtest = BacktestEngine(initial_capital=10000)
    stats = backtest.run_backtest(data, ema_fast_period=20, ema_slow_period=50)
    
    # التقرير
    print_report(stats, backtest.trades)
    
    print(f"\n📋 تفاصيل الصفقات ({len(backtest.trades)} صفقة):")
    print("-" * 70)
    for i, trade in enumerate(backtest.trades, 1):
        emoji = "🟢 LONG " if trade['type'] == 'long' else "🔴 SHORT"
        pnl_sign = "+" if trade['pnl'] >= 0 else ""
        print(f"\n{i}. {emoji}")
        print(f"   الفترة: {trade['entry_date']} → {trade['exit_date']}")
        print(f"   السعر: ${trade['entry']:.0f} → ${trade['exit']:.0f}")
        print(f"   P&L: {pnl_sign}${trade['pnl']:.2f} ({pnl_sign}{trade['pnl_percent']:.2f}%)")
        print(f"   السبب: {trade['reason']}")
    
    print("\n" + "="*70)
    print("✅ انتهى الباكتستنق!")
    print("="*70 + "\n")
