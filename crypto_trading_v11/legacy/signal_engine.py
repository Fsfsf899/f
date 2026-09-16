"""
محرك إشارات الدخول والخروج - المرحلة الثالثة
Entry/Exit Signal Engine - Phase 3

طبق القواعس من المواصفات تماماً:
- إشارة LONG (شراء): جميع الشروط يجب تنطبق معاً
- إشارة SHORT (بيع): نفس الشروط لكن معكوسة
- خروج: عند الهدف أو عند كسر EMA السريع
"""

import pandas as pd
import json
from typing import Dict, Optional, List, Tuple
from dataclasses import dataclass
from enum import Enum


class Direction(Enum):
    """اتجاه الصفقة"""
    LONG = "long"  # شراء
    SHORT = "short"  # بيع


class SignalStatus(Enum):
    """حالة الإشارة"""
    OPEN = "open"  # صفقة مفتوحة
    CLOSED = "closed"  # صفقة مغلقة


@dataclass
class Signal:
    """
    تمثيل إشارة دخول/خروج
    
    Attributes:
        timestamp: وقت الإشارة
        symbol: الرمز (BTC/USDT)
        mode: وضع التداول (scalping/swing/longterm)
        direction: اتجاه (long/short)
        entry_price: سعر الدخول
        stop_loss: وقف الخسارة
        take_profit: الهدف المستهدف
        indicators: قيم المؤشرات وقت الإشارة
        status: حالة الصفقة
        exit_price: سعر الخروج (إذا أغلقت)
        exit_reason: سبب الخروج
    """
    timestamp: pd.Timestamp
    symbol: str
    mode: str
    direction: Direction
    entry_price: float
    stop_loss: float
    take_profit: float
    indicators: Dict  # قيم المؤشرات وقت الإشارة
    status: SignalStatus = SignalStatus.OPEN
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    
    def to_dict(self) -> Dict:
        """تحويل إلى dictionary للحفظ في قاعدة البيانات"""
        return {
            'timestamp': str(self.timestamp),
            'symbol': self.symbol,
            'mode': self.mode,
            'direction': self.direction.value,
            'entry_price': self.entry_price,
            'stop_loss': self.stop_loss,
            'take_profit': self.take_profit,
            'indicators_json': json.dumps(self.indicators),
            'status': self.status.value,
            'exit_price': self.exit_price,
            'exit_reason': self.exit_reason
        }


class EntryExitEngine:
    """
    محرك قواعد الدخول والخروج
    
    القواعس مأخوذة من المواصفات تماماً:
    
    LONG (شراء) — جميع الشروط يجب تنطبق معاً:
    1. EMA السريع يقطع فوق EMA البطيء (crossover)
    2. RSI بين 40-60 (ما في extreme)
    3. MACD يقطع فوق Signal Line
    4. حجم الشمعة الحالية ≥ 120% من متوسط آخر 20 شمعة
    5. السعر فوق أقرب مستوى دعم
    
    SHORT (بيع) — نفس الشروط معكوسة:
    1. EMA السريع يقطع تحت EMA البطيء
    2. RSI بين 40-60
    3. MACD يقطع تحت Signal Line
    4. حجم الشمعة ≥ 120% من المتوسط
    5. السعر تحت أقرب مستوى مقاومة
    """
    
    def __init__(self):
        self.open_signals: List[Signal] = []
        self.closed_signals: List[Signal] = []
    
    def check_entry_signal_long(
        self,
        df: pd.DataFrame,
        mode: str,
        symbol: str,
        risk_reward_ratio: float = 2.0
    ) -> Optional[Signal]:
        """
        فحص شروط الدخول للشراء (LONG)
        
        يرجع Signal إذا انطبقت جميع الشروط، وإلا None
        
        Args:
            df: DataFrame مع المؤشرات (يجب يحتوي على close, ema_fast, ema_slow, rsi, macd, etc)
            mode: وضع التداول
            symbol: الرمز (BTC/USDT)
            risk_reward_ratio: نسبة المخاطرة/العائد (1.5، 2.0، 3.0 حسب الوضع)
        
        Returns:
            Signal إذا كل الشروط صحيحة، وإلا None
        """
        # احصل على آخر شمعتين
        if len(df) < 2:
            return None
        
        current = df.iloc[-1]
        previous = df.iloc[-2]
        
        # ===== الشرط 1: EMA Crossover =====
        # EMA السريع تقاطع فوق البطيء (من تحت لفوق)
        ema_fast_crossover = (
            previous['ema_fast'] <= previous['ema_slow'] and
            current['ema_fast'] > current['ema_slow']
        )
        
        if not ema_fast_crossover:
            return None  # الشرط الأول ما انطبق
        
        # ===== الشرط 2: RSI =====
        # RSI بين 40-60 (محايد، ما في extreme)
        rsi_ok = 40 <= current['rsi'] <= 60
        
        if not rsi_ok:
            return None
        
        # ===== الشرط 3: MACD =====
        # MACD يقطع فوق Signal Line
        macd_crossover = (
            previous['macd'] <= previous['macd_signal'] and
            current['macd'] > current['macd_signal']
        )
        
        if not macd_crossover:
            return None
        
        # ===== الشرط 4: Volume =====
        # حجم الشمعة الحالية ≥ 120% من متوسط آخر 20 شمعة
        avg_volume_20 = df['volume'].tail(20).mean()
        volume_ok = current['volume'] >= (avg_volume_20 * 1.20)
        
        if not volume_ok:
            return None
        
        # ===== الشرط 5: Support =====
        # السعر فوق الدعم (ولا يكسره)
        support_ok = current['close'] > current['support']
        
        if not support_ok:
            return None
        
        # ✅ جميع الشروط انطبقت! أصدر إشارة LONG
        entry_price = current['close']
        stop_loss = entry_price - (1.5 * current['atr'])
        take_profit = entry_price + (risk_reward_ratio * 1.5 * current['atr'])
        
        signal = Signal(
            timestamp=df.index[-1],
            symbol=symbol,
            mode=mode,
            direction=Direction.LONG,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            indicators={
                'ema_fast': float(current['ema_fast']),
                'ema_slow': float(current['ema_slow']),
                'rsi': float(current['rsi']),
                'macd': float(current['macd']),
                'macd_signal': float(current['macd_signal']),
                'atr': float(current['atr']),
                'volume': float(current['volume']),
                'support': float(current['support']),
                'resistance': float(current['resistance'])
            }
        )
        
        return signal
    
    def check_entry_signal_short(
        self,
        df: pd.DataFrame,
        mode: str,
        symbol: str,
        risk_reward_ratio: float = 2.0
    ) -> Optional[Signal]:
        """
        فحص شروط الدخول للبيع (SHORT)
        
        عكس الشروط:
        1. EMA السريع يقطع تحت EMA البطيء
        2. RSI بين 40-60
        3. MACD يقطع تحت Signal Line
        4. حجم ≥ 120%
        5. السعر تحت المقاومة
        
        Args:
            df: DataFrame مع المؤشرات
            mode: وضع التداول
            symbol: الرمز
            risk_reward_ratio: نسبة المخاطرة/العائد
        
        Returns:
            Signal إذا كل الشروط صحيحة، وإلا None
        """
        if len(df) < 2:
            return None
        
        current = df.iloc[-1]
        previous = df.iloc[-2]
        
        # ===== الشرط 1: EMA Crossover (معكوس) =====
        ema_fast_crossover = (
            previous['ema_fast'] >= previous['ema_slow'] and
            current['ema_fast'] < current['ema_slow']
        )
        
        if not ema_fast_crossover:
            return None
        
        # ===== الشرط 2: RSI =====
        rsi_ok = 40 <= current['rsi'] <= 60
        
        if not rsi_ok:
            return None
        
        # ===== الشرط 3: MACD (معكوس) =====
        macd_crossover = (
            previous['macd'] >= previous['macd_signal'] and
            current['macd'] < current['macd_signal']
        )
        
        if not macd_crossover:
            return None
        
        # ===== الشرط 4: Volume =====
        avg_volume_20 = df['volume'].tail(20).mean()
        volume_ok = current['volume'] >= (avg_volume_20 * 1.20)
        
        if not volume_ok:
            return None
        
        # ===== الشرط 5: Resistance (معكوس) =====
        resistance_ok = current['close'] < current['resistance']
        
        if not resistance_ok:
            return None
        
        # ✅ جميع الشروط انطبقت! أصدر إشارة SHORT
        entry_price = current['close']
        stop_loss = entry_price + (1.5 * current['atr'])  # معكوس (فوق للبيع)
        take_profit = entry_price - (risk_reward_ratio * 1.5 * current['atr'])  # معكوس (تحت)
        
        signal = Signal(
            timestamp=df.index[-1],
            symbol=symbol,
            mode=mode,
            direction=Direction.SHORT,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            indicators={
                'ema_fast': float(current['ema_fast']),
                'ema_slow': float(current['ema_slow']),
                'rsi': float(current['rsi']),
                'macd': float(current['macd']),
                'macd_signal': float(current['macd_signal']),
                'atr': float(current['atr']),
                'volume': float(current['volume']),
                'support': float(current['support']),
                'resistance': float(current['resistance'])
            }
        )
        
        return signal
    
    def check_exit_signal(
        self,
        signal: Signal,
        current_price: float,
        current_ema_fast: float,
        current_ema_slow: float,
        current_rsi: float
    ) -> Optional[Tuple[float, str]]:
        """
        فحص شروط الخروج من صفقة مفتوحة
        
        الخروج يحدث عند أحد الحالات:
        1. وصل السعر للهدف المستهدف (ربح)
        2. وصل السعر لوقف الخسارة (خسارة)
        3. كسر EMA السريع قبل الهدف (خروج مبكر)
           - للـ LONG: RSI > 75 و EMA السريع تحت البطيء
           - للـ SHORT: RSI < 25 و EMA السريع فوق البطيء
        
        Args:
            signal: الصفقة المفتوحة
            current_price: السعر الحالي
            current_ema_fast: EMA السريع الحالي
            current_ema_slow: EMA البطيء الحالي
            current_rsi: RSI الحالي
        
        Returns:
            Tuple من (exit_price, reason) إذا يجب الخروج، وإلا None
        """
        if signal.direction == Direction.LONG:
            # ===== شراء =====
            
            # 1. وصل الهدف
            if current_price >= signal.take_profit:
                return (signal.take_profit, "Take Profit Hit ✅")
            
            # 2. وصل الوقف
            if current_price <= signal.stop_loss:
                return (signal.stop_loss, "Stop Loss Hit ❌")
            
            # 3. خروج مبكر (كسر EMA)
            if (current_rsi > 75 and current_ema_fast < current_ema_slow):
                return (current_price, "Early Exit - Strong Momentum Break 📉")
        
        elif signal.direction == Direction.SHORT:
            # ===== بيع =====
            
            # 1. وصل الهدف
            if current_price <= signal.take_profit:
                return (signal.take_profit, "Take Profit Hit ✅")
            
            # 2. وصل الوقف
            if current_price >= signal.stop_loss:
                return (signal.stop_loss, "Stop Loss Hit ❌")
            
            # 3. خروج مبكر (كسر EMA)
            if (current_rsi < 25 and current_ema_fast > current_ema_slow):
                return (current_price, "Early Exit - Strong Momentum Break 📈")
        
        return None  # لا توجد شروط خروج
    
    def calculate_result(self, signal: Signal) -> float:
        """
        احسب الربح/الخسارة من صفقة مغلقة
        
        للـ LONG: exit_price - entry_price
        للـ SHORT: entry_price - exit_price
        
        Args:
            signal: صفقة مغلقة (يجب يكون exit_price موجود)
        
        Returns:
            الربح أو الخسارة (بالدولار)
        """
        if signal.exit_price is None:
            return 0
        
        if signal.direction == Direction.LONG:
            return signal.exit_price - signal.entry_price
        else:  # SHORT
            return signal.entry_price - signal.exit_price
    
    def calculate_risk_multiple(self, signal: Signal) -> float:
        """
        احسب كم Risk/Reward تحقق
        
        إذا كان المخطط:
        - Stop Loss مسافة = 100
        - Take Profit مسافة = 200
        - نسبة = 200/100 = 2.0x
        
        وإذا خرجنا عند 150:
        - العائد الفعلي = 50
        - R multiple = 50/100 = 0.5x
        
        Args:
            signal: صفقة مغلقة
        
        Returns:
            كم risk unit حققنا (قد تكون موجبة أو سالبة)
        """
        if signal.exit_price is None or signal.direction is None:
            return 0
        
        if signal.direction == Direction.LONG:
            risk_amount = signal.entry_price - signal.stop_loss
            actual_pnl = signal.exit_price - signal.entry_price
        else:  # SHORT
            risk_amount = signal.stop_loss - signal.entry_price
            actual_pnl = signal.entry_price - signal.exit_price
        
        if risk_amount == 0:
            return 0
        
        return actual_pnl / risk_amount


# ==================== اختبار ====================
if __name__ == "__main__":
    print("🚀 اختبار محرك الإشارات\n")
    
    # من المكتبات السابقة
    from indicators import IndicatorsCalculator
    import numpy as np
    
    # إنشاء بيانات تجريبية
    print("📌 إنشاء بيانات تجريبية...")
    dates = pd.date_range('2024-01-01', periods=100, freq='1H')
    
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
    
    # حساب المؤشرات
    df = IndicatorsCalculator.get_all_indicators(df, ema_fast=9, ema_slow=21)
    
    print(f"✅ تم إنشاء {len(df)} شمعة مع المؤشرات\n")
    
    # اختبار محرك الإشارات
    print("="*60)
    print("اختبار محرك الإشارات")
    print("="*60)
    
    engine = EntryExitEngine()
    
    # فحص إشارة LONG
    print("\n1️⃣ فحص إشارة LONG (شراء)...")
    signal_long = engine.check_entry_signal_long(
        df,
        mode='swing',
        symbol='BTC/USDT',
        risk_reward_ratio=2.0
    )
    
    if signal_long:
        print(f"✅ تم العثور على إشارة LONG!")
        print(f"   السعر: {signal_long.entry_price:.2f}")
        print(f"   Stop Loss: {signal_long.stop_loss:.2f}")
        print(f"   Take Profit: {signal_long.take_profit:.2f}")
        print(f"   Risk/Reward: {(signal_long.take_profit - signal_long.entry_price) / (signal_long.entry_price - signal_long.stop_loss):.2f}x")
    else:
        print("❌ لم توجد إشارة LONG في البيانات الحالية")
    
    # فحص إشارة SHORT
    print("\n2️⃣ فحص إشارة SHORT (بيع)...")
    signal_short = engine.check_entry_signal_short(
        df,
        mode='swing',
        symbol='BTC/USDT',
        risk_reward_ratio=2.0
    )
    
    if signal_short:
        print(f"✅ تم العثور على إشارة SHORT!")
        print(f"   السعر: {signal_short.entry_price:.2f}")
        print(f"   Stop Loss: {signal_short.stop_loss:.2f}")
        print(f"   Take Profit: {signal_short.take_profit:.2f}")
    else:
        print("❌ لم توجد إشارة SHORT في البيانات الحالية")
    
    # محاكاة الخروج
    if signal_long:
        print("\n3️⃣ محاكاة الخروج من صفقة LONG...")
        current = df.iloc[-1]
        
        # حالة 1: وصل الهدف
        exit_result = engine.check_exit_signal(
            signal_long,
            signal_long.take_profit + 10,  # وصل الهدف
            current['ema_fast'],
            current['ema_slow'],
            current['rsi']
        )
        
        if exit_result:
            print(f"✅ شروط الخروج متحققة: {exit_result[1]}")
            signal_long.exit_price = exit_result[0]
            signal_long.exit_reason = exit_result[1]
            signal_long.status = SignalStatus.CLOSED
            
            pnl = engine.calculate_result(signal_long)
            r_multiple = engine.calculate_risk_multiple(signal_long)
            
            print(f"   الربح/الخسارة: ${pnl:.2f}")
            print(f"   Risk Multiple: {r_multiple:.2f}x")
    
    print("\n✅ انتهى الاختبار!")
