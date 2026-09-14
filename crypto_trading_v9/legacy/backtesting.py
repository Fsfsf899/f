"""
محرك الباكتستنق والأداء - المرحلة الرابعة
Backtesting Engine & Performance Report - Phase 4

يشغل الإشارات على بيانات تاريخية ويحسب الإحصائيات:
- عدد الصفقات
- نسبة النجاح
- أقصى خسارة متتالية
- منحنى رأس المال
- إلخ
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import json
from dataclasses import dataclass, asdict

from indicators import IndicatorsCalculator
from signal_engine import EntryExitEngine, Signal, Direction, SignalStatus


@dataclass
class BacktestConfig:
    """إعدادات الباكتستنق"""
    initial_capital: float = 10000  # رأس المال الابتدائي (بالدولار)
    risk_per_trade: float = 0.02  # 2% من رأس المال لكل صفقة
    slippage: float = 0.001  # الانزلاق (0.1%)
    trading_fee: float = 0.0005  # رسم التداول (0.05%)
    max_daily_loss_percent: float = 0.05  # 5% خسارة يومية تيقف التداول
    max_open_trades: int = 3  # أقصى عدد صفقات مفتوحة
    
    # معاملات المؤشرات (لكل وضع)
    modes: Dict = None
    
    def __post_init__(self):
        if self.modes is None:
            self.modes = {
                'scalping': {
                    'timeframe': '5m',
                    'ema_fast': 9,
                    'ema_slow': 21,
                    'rsi_period': 7,
                    'atr_period': 7,
                    'risk_reward': 1.5
                },
                'swing': {
                    'timeframe': '15m',
                    'ema_fast': 20,
                    'ema_slow': 50,
                    'rsi_period': 14,
                    'atr_period': 14,
                    'risk_reward': 2.0
                },
                'longterm': {
                    'timeframe': '1h',
                    'ema_fast': 50,
                    'ema_slow': 200,
                    'rsi_period': 14,
                    'atr_period': 14,
                    'risk_reward': 3.0
                }
            }


@dataclass
class DailyStats:
    """إحصائيات يومية"""
    date: str
    starting_balance: float
    ending_balance: float
    pnl: float  # الربح/الخسارة اليومية
    pnl_percent: float
    trades_count: int
    winning_trades: int
    losing_trades: int
    trading_halted: bool = False


class BacktestEngine:
    """
    محرك الباكتستنق
    
    يشغل الإشارات على بيانات تاريخية بشكل كامل:
    1. يحمل البيانات التاريخية
    2. يحسب المؤشرات
    3. يفحص الإشارات شمعة بشمعة (لا lookahead bias)
    4. يحاسب الخروج (Stop/Target/Early Exit)
    5. يحسب الإحصائيات
    """
    
    def __init__(self, config: BacktestConfig):
        self.config = config
        self.engine = EntryExitEngine()
        
        # السجلات
        self.signals: List[Signal] = []
        self.closed_trades: List[Dict] = []
        self.daily_stats: List[DailyStats] = []
        
        self.current_capital = config.initial_capital
        self.daily_capital = config.initial_capital
    
    def run_backtest(
        self,
        df: pd.DataFrame,
        symbol: str = 'BTC/USDT',
        modes_to_test: List[str] = None
    ) -> Dict:
        """
        شغل الباكتستنق على البيانات التاريخية
        
        Args:
            df: DataFrame بيانات OHLCV
            symbol: الرمز (BTC/USDT)
            modes_to_test: قائمة الأوضاع (scalping, swing, longterm)
        
        Returns:
            Dictionary بنتائج الباكتستنق والإحصائيات
        """
        if modes_to_test is None:
            modes_to_test = ['swing']  # الوضع الافتراضي للاختبار
        
        print(f"\n{'='*70}")
        print(f"🚀 بدء الباكتستنق")
        print(f"{'='*70}")
        print(f"الرمز: {symbol}")
        print(f"البيانات: من {df.index[0]} إلى {df.index[-1]}")
        print(f"عدد الشموع: {len(df)}")
        print(f"رأس المال الابتدائي: ${self.config.initial_capital:,.2f}")
        print(f"المخاطرة لكل صفقة: {self.config.risk_per_trade*100}%")
        print(f"الأوضاع: {', '.join(modes_to_test)}")
        print()
        
        # حساب المؤشرات مرة واحدة للبيانات الكاملة
        print("📊 حساب المؤشرات...")
        df = self._calculate_all_indicators(df, modes_to_test)
        
        # متابعة الصفقات المفتوحة
        open_trades: Dict[str, Signal] = {}  # key = mode
        
        # حلقة على كل شمعة
        print("🔄 تشغيل محرك الإشارات...")
        for idx in range(len(df)):
            current_date = df.index[idx].date()
            
            # بيانات حتى الشمعة الحالية (بدون lookahead)
            df_until_now = df.iloc[:idx+1]
            
            # ===== فحص الخروج من الصفقات المفتوحة =====
            for mode in list(open_trades.keys()):
                signal = open_trades[mode]
                current_row = df.iloc[idx]
                
                # فحص شروط الخروج
                exit_result = self.engine.check_exit_signal(
                    signal,
                    current_row['close'],
                    current_row['ema_fast'] if f'ema_fast_{mode}' in current_row else current_row.get('ema_fast'),
                    current_row['ema_slow'] if f'ema_slow_{mode}' in current_row else current_row.get('ema_slow'),
                    current_row['rsi'] if f'rsi_{mode}' in current_row else current_row.get('rsi')
                )
                
                if exit_result:
                    # أغلق الصفقة
                    exit_price, exit_reason = exit_result
                    signal.exit_price = exit_price
                    signal.exit_reason = exit_reason
                    signal.status = SignalStatus.CLOSED
                    
                    # احسب النتائج
                    pnl = self.engine.calculate_result(signal)
                    r_multiple = self.engine.calculate_risk_multiple(signal)
                    
                    # حدّث رأس المال
                    self.current_capital += pnl
                    
                    # سجل الصفقة المغلقة
                    self.closed_trades.append({
                        'timestamp': str(signal.timestamp),
                        'exit_time': str(df.index[idx]),
                        'symbol': signal.symbol,
                        'mode': signal.mode,
                        'direction': signal.direction.value,
                        'entry_price': signal.entry_price,
                        'exit_price': signal.exit_price,
                        'stop_loss': signal.stop_loss,
                        'take_profit': signal.take_profit,
                        'pnl': pnl,
                        'r_multiple': r_multiple,
                        'exit_reason': exit_reason
                    })
                    
                    del open_trades[mode]
            
            # ===== فحص الدخول الجديد =====
            # تحقق من حد الخسارة اليومية
            daily_loss = self.daily_capital - self.current_capital
            daily_loss_percent = daily_loss / self.daily_capital if self.daily_capital > 0 else 0
            
            trading_halted = daily_loss_percent >= self.config.max_daily_loss_percent
            
            # إذا لم نصل حد الخسارة والصفقات المفتوحة < الحد الأقصى
            if not trading_halted and len(open_trades) < self.config.max_open_trades:
                for mode in modes_to_test:
                    if mode not in open_trades:
                        # فحص إشارة LONG و SHORT
                        signal_long = self.engine.check_entry_signal_long(
                            df_until_now,
                            mode=mode,
                            symbol=symbol,
                            risk_reward_ratio=self.config.modes[mode]['risk_reward']
                        )
                        
                        if signal_long:
                            open_trades[mode] = signal_long
                            self.signals.append(signal_long)
                        
                        # إذا ما في LONG، فحص SHORT
                        if not signal_long:
                            signal_short = self.engine.check_entry_signal_short(
                                df_until_now,
                                mode=mode,
                                symbol=symbol,
                                risk_reward_ratio=self.config.modes[mode]['risk_reward']
                            )
                            
                            if signal_short:
                                open_trades[mode] = signal_short
                                self.signals.append(signal_short)
        
        # أغلق أي صفقات متبقية بآخر شمعة
        last_row = df.iloc[-1]
        for mode in list(open_trades.keys()):
            signal = open_trades[mode]
            signal.exit_price = last_row['close']
            signal.exit_reason = "Backtest Ended"
            signal.status = SignalStatus.CLOSED
            
            pnl = self.engine.calculate_result(signal)
            r_multiple = self.engine.calculate_risk_multiple(signal)
            
            self.closed_trades.append({
                'timestamp': str(signal.timestamp),
                'exit_time': str(df.index[-1]),
                'symbol': signal.symbol,
                'mode': signal.mode,
                'direction': signal.direction.value,
                'entry_price': signal.entry_price,
                'exit_price': signal.exit_price,
                'stop_loss': signal.stop_loss,
                'take_profit': signal.take_profit,
                'pnl': pnl,
                'r_multiple': r_multiple,
                'exit_reason': "Backtest Ended"
            })
        
        # احسب الإحصائيات
        results = self._calculate_statistics()
        
        return results
    
    def _calculate_all_indicators(
        self,
        df: pd.DataFrame,
        modes: List[str]
    ) -> pd.DataFrame:
        """حساب المؤشرات لكل وضع"""
        df_copy = df.copy()
        
        for mode in modes:
            config = self.config.modes[mode]
            
            # حساب المؤشرات
            df_indicators = IndicatorsCalculator.get_all_indicators(
                df_copy,
                ema_fast=config['ema_fast'],
                ema_slow=config['ema_slow'],
                rsi_period=config['rsi_period'],
                atr_period=config['atr_period']
            )
            
            # نسخ الأعمدة (لو كان الوضع الأول، ضع مباشرة)
            if mode == modes[0]:
                df_copy = df_indicators
            # إلا، لا تعديل (الأوضاع الأخرى ستستخدم نفس المؤشرات)
        
        return df_copy
    
    def _calculate_statistics(self) -> Dict:
        """احسب جميع الإحصائيات"""
        trades = self.closed_trades
        
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
                'max_consecutive_losses': 0,
                'max_drawdown': 0,
                'max_drawdown_percent': 0,
                'sharpe_ratio': 0,
                'final_capital': self.current_capital
            }
        
        # أساسي
        total = len(trades)
        winning = sum(1 for t in trades if t['pnl'] > 0)
        losing = sum(1 for t in trades if t['pnl'] < 0)
        
        # الأرباح والخسائر
        gross_profit = sum(t['pnl'] for t in trades if t['pnl'] > 0)
        gross_loss = sum(abs(t['pnl']) for t in trades if t['pnl'] < 0)
        net_profit = gross_profit - gross_loss
        
        # نسبة النجاح
        win_rate = winning / total if total > 0 else 0
        
        # المتوسطات
        avg_win = gross_profit / winning if winning > 0 else 0
        avg_loss = gross_loss / losing if losing > 0 else 0
        
        # Profit Factor
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
        
        # أقصى خسائر متتالية
        max_consecutive = 0
        current_consecutive = 0
        for trade in trades:
            if trade['pnl'] < 0:
                current_consecutive += 1
                max_consecutive = max(max_consecutive, current_consecutive)
            else:
                current_consecutive = 0
        
        # Max Drawdown
        capital_curve = [self.config.initial_capital]
        for trade in trades:
            capital_curve.append(capital_curve[-1] + trade['pnl'])
        
        max_dd = 0
        peak = capital_curve[0]
        for val in capital_curve:
            if val > peak:
                peak = val
            dd = peak - val
            max_dd = max(max_dd, dd)
        
        max_dd_percent = (max_dd / self.config.initial_capital) * 100 if self.config.initial_capital > 0 else 0
        
        # Sharpe Ratio (تقريبي)
        pnl_list = np.array([t['pnl'] for t in trades])
        if len(pnl_list) > 0 and np.std(pnl_list) > 0:
            sharpe = (np.mean(pnl_list) / np.std(pnl_list)) * np.sqrt(252)  # 252 أيام تداول في السنة
        else:
            sharpe = 0
        
        return {
            'total_trades': total,
            'winning_trades': winning,
            'losing_trades': losing,
            'win_rate': win_rate,
            'gross_profit': round(gross_profit, 2),
            'gross_loss': round(gross_loss, 2),
            'net_profit': round(net_profit, 2),
            'net_profit_percent': round((net_profit / self.config.initial_capital) * 100, 2),
            'avg_win': round(avg_win, 2),
            'avg_loss': round(avg_loss, 2),
            'profit_factor': round(profit_factor, 2) if profit_factor != float('inf') else "∞",
            'max_consecutive_losses': max_consecutive,
            'max_drawdown': round(max_dd, 2),
            'max_drawdown_percent': round(max_dd_percent, 2),
            'sharpe_ratio': round(sharpe, 2),
            'final_capital': round(self.current_capital, 2)
        }


def print_backtest_report(stats: Dict, trades: List[Dict]):
    """طبع تقرير الباكتستنق بشكل جميل"""
    
    print("\n" + "="*70)
    print("📊 تقرير الباكتستنق النهائي")
    print("="*70)
    
    print("\n💰 النتائج المالية:")
    print(f"   رأس المال الابتدائي: ${10000:,.2f}")
    print(f"   رأس المال النهائي: ${stats['final_capital']:,.2f}")
    print(f"   الربح الصافي: ${stats['net_profit']:,.2f}")
    print(f"   نسبة العائد: {stats['net_profit_percent']:.2f}%")
    
    print("\n📈 إحصائيات الصفقات:")
    print(f"   إجمالي الصفقات: {stats['total_trades']}")
    print(f"   صفقات رابحة: {stats['winning_trades']} ({stats['win_rate']*100:.1f}%)")
    print(f"   صفقات خاسرة: {stats['losing_trades']} ({(1-stats['win_rate'])*100:.1f}%)")
    
    print("\n💹 الأرباح والخسائر:")
    print(f"   إجمالي الأرباح: ${stats['gross_profit']:,.2f}")
    print(f"   إجمالي الخسائر: ${stats['gross_loss']:,.2f}")
    print(f"   متوسط الربح: ${stats['avg_win']:,.2f}")
    print(f"   متوسط الخسارة: ${stats['avg_loss']:,.2f}")
    print(f"   Profit Factor: {stats['profit_factor']}")
    
    print("\n⚠️ إدارة المخاطر:")
    print(f"   أقصى خسائر متتالية: {stats['max_consecutive_losses']}")
    print(f"   أقصى تراجع (Drawdown): ${stats['max_drawdown']:,.2f} ({stats['max_drawdown_percent']:.2f}%)")
    print(f"   Sharpe Ratio: {stats['sharpe_ratio']}")
    
    print("\n" + "="*70)
    
    # أفضل وأسوأ صفقات
    if trades:
        trades_sorted = sorted(trades, key=lambda x: x['pnl'], reverse=True)
        
        print("\n🏆 أفضل 3 صفقات:")
        for i, trade in enumerate(trades_sorted[:3], 1):
            direction = "🟢 LONG" if trade['direction'] == 'long' else "🔴 SHORT"
            print(f"   {i}. {direction} | أرباح: ${trade['pnl']:,.2f} | R: {trade['r_multiple']:.2f}x")
        
        print("\n💔 أسوأ 3 صفقات:")
        for i, trade in enumerate(trades_sorted[-3:], 1):
            direction = "🟢 LONG" if trade['direction'] == 'long' else "🔴 SHORT"
            print(f"   {i}. {direction} | خسارة: ${trade['pnl']:,.2f} | R: {trade['r_multiple']:.2f}x")


# ==================== اختبار ====================
if __name__ == "__main__":
    print("🚀 اختبار محرك الباكتستنق\n")
    
    from data_fetcher import CryptoDataFetcher
    
    # إنشاء fetcher
    fetcher = CryptoDataFetcher('binance', 'BTC/USDT', '15m')
    
    # جلب بيانات تاريخية (30 يوم)
    print("📥 جلب البيانات التاريخية...")
    df = fetcher.fetch_historical_data(days=30)
    
    if df is None or len(df) < 100:
        print("❌ لم تتمكن من جلب البيانات. تأكد من اتصالك بالإنترنت وتثبيت ccxt")
        print("\nبدلاً من ذلك، سأنشئ بيانات تجريبية:")
        
        # بيانات تجريبية
        import numpy as np
        dates = pd.date_range('2024-01-01', periods=720, freq='15min')  # 5 أيام بـ 15 دقيقة
        close = 50000 + np.cumsum(np.random.randn(720) * 50)
        high = close + np.abs(np.random.randn(720) * 30)
        low = close - np.abs(np.random.randn(720) * 30)
        volume = np.random.randint(1000, 5000, 720)
        
        df = pd.DataFrame({
            'open': close * 0.99,
            'high': high,
            'low': low,
            'close': close,
            'volume': volume
        }, index=dates)
    
    # إعدادات الباكتستنق
    config = BacktestConfig(
        initial_capital=10000,
        risk_per_trade=0.02,  # 2% لكل صفقة
        max_daily_loss_percent=0.05  # 5% يومي
    )
    
    # شغل الباكتستنق
    backtest = BacktestEngine(config)
    results = backtest.run_backtest(df, symbol='BTC/USDT', modes_to_test=['swing'])
    
    # طبع التقرير
    print_backtest_report(results, backtest.closed_trades)
    
    # حفظ النتائج
    print("\n💾 حفظ النتائج...")
    with open('backtest_results.json', 'w') as f:
        json.dump({
            'config': {
                'initial_capital': config.initial_capital,
                'risk_per_trade': config.risk_per_trade,
                'max_daily_loss': config.max_daily_loss_percent
            },
            'statistics': results,
            'trades': backtest.closed_trades
        }, f, indent=2)
    
    print("✅ تم الحفظ في: backtest_results.json")
