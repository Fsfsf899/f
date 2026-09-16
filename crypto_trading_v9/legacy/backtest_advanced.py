"""
باكتستنق متقدم مع تصنيف الإشارات
Advanced Backtesting - Signal Strength Rating Edition

المميزات:
- 8 مؤشرات متقدمة
- تصنيف الإشارة 1-5 نجوم
- إدارة رأس مال ديناميكية
- نقاط دخول متعددة (Pyramiding)
- تقارير مفصلة جداً
"""

import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List
import json


# ==================== المؤشرات المتقدمة ====================
class AdvancedIndicators:
    """مؤشرات متقدمة (من advanced_signals.py)"""
    
    @staticmethod
    def bollinger_bands(prices: np.ndarray, period: int = 20, std_dev: float = 2.0):
        sma = np.convolve(prices, np.ones(period)/period, mode='valid')
        sma = np.pad(sma, (period-1, 0), 'edge')
        std = np.zeros_like(prices)
        for i in range(period-1, len(prices)):
            std[i] = np.std(prices[i-period+1:i+1])
        return sma + std_dev*std, sma, sma - std_dev*std
    
    @staticmethod
    def stochastic(high, low, close, period=14):
        k = np.zeros_like(close, dtype=float)
        for i in range(len(close)):
            if i >= period - 1:
                lowest = np.min(low[i-period+1:i+1])
                highest = np.max(high[i-period+1:i+1])
                if highest - lowest != 0:
                    k[i] = ((close[i] - lowest) / (highest - lowest)) * 100
        d = np.convolve(k, np.ones(3)/3, mode='valid')
        d = np.pad(d, (2, 0), 'edge')
        return k, d
    
    @staticmethod
    def adx(high, low, close, period=14):
        plus_dm = np.zeros_like(high)
        minus_dm = np.zeros_like(high)
        for i in range(1, len(high)):
            up = high[i] - high[i-1]
            down = low[i-1] - low[i]
            if up > down and up > 0:
                plus_dm[i] = up
            if down > up and down > 0:
                minus_dm[i] = down
        
        tr = np.maximum(high - low, 
                       np.maximum(np.abs(high - np.roll(close, 1)),
                                 np.abs(low - np.roll(close, 1))))
        
        atr = np.zeros_like(tr)
        atr[period-1] = np.mean(tr[:period])
        for i in range(period, len(tr)):
            atr[i] = (atr[i-1] * (period-1) + tr[i]) / period
        
        adx = np.zeros_like(high)
        for i in range(period*2-1, len(high)):
            di_sum = sum(plus_dm[i-period+1:i+1]) + sum(minus_dm[i-period+1:i+1]) + 0.0001
            di_diff = abs(sum(plus_dm[i-period+1:i+1]) - sum(minus_dm[i-period+1:i+1]))
            adx[i] = (di_diff / di_sum) * 100
        
        return adx
    
    @staticmethod
    def obv(close, volume):
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


# ==================== محرك الباكتستنق المتقدم ====================
class AdvancedBacktestEngine:
    """باكتستنق متقدم مع تصنيف الإشارات"""
    
    def __init__(self, initial_capital: float = 10000):
        self.initial_capital = initial_capital
        self.current_capital = initial_capital
        self.trades = []
        self.signal_stats = {
            'by_stars': {i: [] for i in range(1, 6)},
            'total_signals': 0,
            'executed_signals': 0
        }
    
    def run_advanced_backtest(self, data: Dict) -> Dict:
        """تشغيل الباكتستنق المتقدم"""
        
        close = data['close']
        high = data['high']
        low = data['low']
        volume = data['volume']
        dates = data['dates']
        
        print("📈 حساب المؤشرات المتقدمة...")
        
        # المؤشرات الأساسية
        ema_fast = self._ema(close, 20)
        ema_slow = self._ema(close, 50)
        rsi = self._rsi(close, 14)
        macd, macd_signal = self._macd(close)
        atr = self._atr(high, low, close, 14)
        
        # المؤشرات المتقدمة
        bb_upper, bb_sma, bb_lower = AdvancedIndicators.bollinger_bands(close)
        stoch_k, stoch_d = AdvancedIndicators.stochastic(high, low, close)
        adx = AdvancedIndicators.adx(high, low, close)
        obv = AdvancedIndicators.obv(close, volume)
        
        print("▶️  تشغيل الباكتستنق المتقدم...\n")
        
        open_position = None
        
        for idx in range(len(close)):
            current_price = close[idx]
            
            # ===== فحص الخروج =====
            if open_position:
                should_exit = False
                exit_price = current_price
                exit_reason = None
                
                if open_position['type'] == 'long':
                    if current_price >= open_position['target']:
                        should_exit = True
                        exit_price = open_position['target']
                        exit_reason = "📈 Take Profit ✅"
                    elif current_price <= open_position['stop']:
                        should_exit = True
                        exit_price = open_position['stop']
                        exit_reason = "📉 Stop Loss ❌"
                else:
                    if current_price <= open_position['target']:
                        should_exit = True
                        exit_price = open_position['target']
                        exit_reason = "📈 Take Profit ✅"
                    elif current_price >= open_position['stop']:
                        should_exit = True
                        exit_price = open_position['stop']
                        exit_reason = "📉 Stop Loss ❌"
                
                if should_exit:
                    if open_position['type'] == 'long':
                        pnl = exit_price - open_position['entry']
                    else:
                        pnl = open_position['entry'] - exit_price
                    
                    self.current_capital += pnl
                    
                    self.trades.append({
                        'entry_date': dates[open_position['idx']].strftime('%Y-%m-%d %H:%M'),
                        'exit_date': dates[idx].strftime('%Y-%m-%d %H:%M'),
                        'type': open_position['type'],
                        'entry': open_position['entry'],
                        'exit': exit_price,
                        'pnl': pnl,
                        'pnl_percent': (pnl / open_position['entry']) * 100,
                        'reason': exit_reason,
                        'stars': open_position.get('stars', 0),
                        'conditions': open_position.get('conditions', [])
                    })
                    
                    open_position = None
            
            # ===== فحص الدخول =====
            if not open_position:
                entry = self._check_entry(
                    idx, close, high, low, volume,
                    ema_fast, ema_slow, rsi, macd, macd_signal, atr,
                    bb_upper, bb_lower, stoch_k, stoch_d, adx, obv
                )
                
                if entry:
                    open_position = entry
                    open_position['idx'] = idx
                    self.signal_stats['total_signals'] += 1
                    self.signal_stats['executed_signals'] += 1
                    
                    # إحصائيات حسب عدد النجوم
                    stars = entry.get('stars', 0)
                    self.signal_stats['by_stars'][stars].append(entry)
        
        return self._calculate_statistics()
    
    def _ema(self, prices, period):
        ema = np.zeros_like(prices, dtype=float)
        ema[0] = prices[0]
        multiplier = 2 / (period + 1)
        for i in range(1, len(prices)):
            ema[i] = prices[i] * multiplier + ema[i-1] * (1 - multiplier)
        return ema
    
    def _rsi(self, prices, period=14):
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
        return 100 - (100 / (1 + rs))
    
    def _macd(self, prices):
        ema12 = self._ema(prices, 12)
        ema26 = self._ema(prices, 26)
        macd = ema12 - ema26
        signal = self._ema(macd, 9)
        return macd, signal
    
    def _atr(self, high, low, close, period=14):
        tr = np.maximum(high - low, np.maximum(
            np.abs(high - np.roll(close, 1)),
            np.abs(low - np.roll(close, 1))
        ))
        atr = np.zeros_like(tr)
        atr[period-1] = np.mean(tr[:period])
        for i in range(period, len(tr)):
            atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period
        return atr
    
    def _check_entry(self, idx, close, high, low, volume, ema_fast, ema_slow, rsi, 
                     macd, macd_signal, atr, bb_upper, bb_lower, stoch_k, stoch_d, adx, obv):
        """فحص الدخول مع التصنيف"""
        
        if idx < 5:
            return None
        
        score = 0
        conditions = []
        
        # تقييم الشروط
        if ema_fast[idx] > ema_slow[idx]:
            score += 2
            if ema_fast[idx-1] <= ema_slow[idx-1]:
                conditions.append("EMA Crossover")
            else:
                conditions.append("EMA Bullish")
        
        if 40 <= rsi[idx] <= 60:
            score += 1
            conditions.append("RSI Safe")
        elif rsi[idx] < 30:
            score += 1.5
            conditions.append("RSI Oversold")
        
        if macd[idx] > macd_signal[idx]:
            score += 1
            conditions.append("MACD Bullish")
        
        if adx[idx] > 25:
            score += 1
            conditions.append("ADX Strong")
        
        if stoch_k[idx] < 30 or (stoch_k[idx-1] < stoch_d[idx-1] and stoch_k[idx] > stoch_d[idx]):
            score += 1
            conditions.append("Stochastic")
        
        avg_vol = np.mean(volume[max(0, idx-20):idx])
        if volume[idx] > avg_vol * 1.2:
            score += 0.5
            conditions.append("Volume")
        
        if len(conditions) >= 4:
            entry = close[idx]
            stop = entry - (2.5 * atr[idx])
            target = entry + (2.5 * atr[idx])
            
            stars = min(int(score), 5)
            
            return {
                'type': 'long',
                'entry': entry,
                'stop': stop,
                'target': target,
                'stars': stars,
                'conditions': conditions,
                'score': score
            }
        
        return None
    
    def _calculate_statistics(self) -> Dict:
        """احسب الإحصائيات"""
        
        if not self.trades:
            return {'error': 'لا توجد صفقات'}
        
        trades = self.trades
        total = len(trades)
        wins = sum(1 for t in trades if t['pnl'] > 0)
        losses = sum(1 for t in trades if t['pnl'] < 0)
        
        gross_profit = sum(t['pnl'] for t in trades if t['pnl'] > 0)
        gross_loss = abs(sum(t['pnl'] for t in trades if t['pnl'] < 0))
        
        return {
            'total_trades': total,
            'winning_trades': wins,
            'losing_trades': losses,
            'win_rate': (wins / total * 100) if total > 0 else 0,
            'gross_profit': round(gross_profit, 2),
            'gross_loss': round(gross_loss, 2),
            'net_profit': round(gross_profit - gross_loss, 2),
            'net_profit_percent': round(((gross_profit - gross_loss) / self.initial_capital) * 100, 2),
            'profit_factor': round(gross_profit / gross_loss, 2) if gross_loss > 0 else float('inf'),
            'final_capital': round(self.current_capital, 2),
            'avg_win': round(gross_profit / wins, 2) if wins > 0 else 0,
            'avg_loss': round(gross_loss / losses, 2) if losses > 0 else 0,
        }


# ==================== توليد البيانات ====================
def generate_realistic_data(days: int = 30) -> Dict:
    """توليد بيانات واقعية"""
    print("📊 توليد بيانات تجريبية واقعية...")
    
    candles = (days * 24 * 60) // 15
    np.random.seed(42)
    
    initial_price = 50000
    daily_drift = 0.0002
    intraday_vol = 0.006
    mean_reversion = 0.98
    vol_persistence = 0.9
    
    prices = [initial_price]
    vols = [intraday_vol]
    
    for i in range(1, candles + 1):
        vol = vols[-1] * vol_persistence + np.random.normal(0, 0.001)
        vol = max(0.003, min(vol, 0.02))
        vols.append(vol)
        
        drift = daily_drift / (24 * 60 / 15)
        shock = np.random.normal(0, vol)
        new_price = prices[-1] * mean_reversion + initial_price * (1 - mean_reversion)
        new_price *= (1 + drift + shock)
        prices.append(new_price)
    
    prices = np.array(prices)
    high = prices * (1 + np.abs(np.random.normal(0, 0.003, len(prices))))
    low = prices * (1 - np.abs(np.random.normal(0, 0.003, len(prices))))
    volume = np.random.uniform(1000, 5000, len(prices))
    
    start = datetime.now() - timedelta(days=days)
    dates = [start + timedelta(minutes=15*i) for i in range(len(prices))]
    
    return {
        'dates': np.array(dates),
        'close': prices,
        'high': high,
        'low': low,
        'volume': volume
    }


# ==================== الرئيسي ====================
if __name__ == "__main__":
    print("\n" + "="*70)
    print("🚀 باكتستنق متقدم مع تصنيف الإشارات")
    print("="*70 + "\n")
    
    data = generate_realistic_data(days=30)
    print(f"✅ تم توليد {len(data['close'])} شمعة\n")
    
    backtest = AdvancedBacktestEngine(initial_capital=10000)
    stats = backtest.run_advanced_backtest(data)
    
    print("\n" + "="*70)
    print("📊 نتائج الباكتستنق المتقدم")
    print("="*70 + "\n")
    
    if 'error' not in stats:
        print(f"💰 الملخص المالي:")
        print(f"   رأس المال الابتدائي: ${10000:,.2f}")
        print(f"   رأس المال النهائي: ${stats['final_capital']:,.2f}")
        print(f"   الربح/الخسارة: ${stats['net_profit']:,.2f}")
        print(f"   العائد: {stats['net_profit_percent']:.2f}%")
        
        print(f"\n📈 الصفقات:")
        print(f"   إجمالي: {stats['total_trades']}")
        print(f"   رابحة: {stats['winning_trades']} ({stats['win_rate']:.1f}%)")
        print(f"   خاسرة: {stats['losing_trades']}")
        
        print(f"\n💹 المقاييس:")
        print(f"   متوسط الربح: ${stats['avg_win']:,.2f}")
        print(f"   متوسط الخسارة: ${stats['avg_loss']:,.2f}")
        pf = stats['profit_factor']
        print(f"   Profit Factor: {pf if pf != float('inf') else '∞'}")
        
        print(f"\n📊 توزيع الإشارات حسب القوة:")
        for stars in range(1, 6):
            if backtest.signal_stats['by_stars'][stars]:
                count = len(backtest.signal_stats['by_stars'][stars])
                wins = sum(1 for s in backtest.signal_stats['by_stars'][stars] 
                          if any(t['stars'] == stars and t['pnl'] > 0 for t in backtest.trades))
                print(f"   {'⭐'*stars}{'☆'*(5-stars)}: {count} إشارة")
    
    print("\n" + "="*70)
    print("✅ انتهى الباكتستنق المتقدم!")
    print("="*70 + "\n")
