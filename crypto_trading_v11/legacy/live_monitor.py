"""
نظام المراقبة الحية والتنبيهات - المرحلة الخامسة
Live Monitoring & Alert System - Phase 5

الميزات:
- مراقبة الأسعار الحية كل N دقيقة
- إصدار إشارات فور ظهورها
- إرسال تنبيهات عبر Telegram (أو Console)
- حفظ الإشارات في قاعدة البيانات
- إدارة المخاطر اليومية
"""

import time
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import numpy as np


# ==================== قاعدة بيانات ====================
class SignalDatabase:
    """حفظ الإشارات في ملف JSON بسيط (بدل SQLite)"""
    
    def __init__(self, filename: str = 'signals.json'):
        self.filename = filename
        self.signals = self._load()
    
    def _load(self) -> List[Dict]:
        """تحميل الإشارات المحفوظة"""
        try:
            with open(self.filename, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            return []
    
    def _save(self):
        """حفظ الإشارات"""
        with open(self.filename, 'w') as f:
            json.dump(self.signals, f, indent=2)
    
    def add_signal(self, signal: Dict):
        """إضافة إشارة جديدة"""
        self.signals.append(signal)
        self._save()
    
    def update_signal(self, signal_idx: int, **kwargs):
        """تحديث إشارة (مثلاً عند الخروج)"""
        if signal_idx < len(self.signals):
            self.signals[signal_idx].update(kwargs)
            self._save()
    
    def get_open_signals(self) -> List[Dict]:
        """جلب الإشارات المفتوحة"""
        return [s for s in self.signals if s.get('status') == 'open']
    
    def get_today_signals(self) -> List[Dict]:
        """جلب إشارات اليوم"""
        today = datetime.now().date()
        return [s for s in self.signals 
                if datetime.fromisoformat(s['timestamp']).date() == today]


# ==================== نظام التنبيهات ====================
class AlertSystem:
    """نظام إرسال التنبيهات"""
    
    def __init__(self, use_telegram: bool = False, telegram_token: str = None, telegram_chat_id: str = None):
        self.use_telegram = use_telegram and telegram_token and telegram_chat_id
        self.telegram_token = telegram_token
        self.telegram_chat_id = telegram_chat_id
    
    def send_alert(self, signal: Dict):
        """إرسال تنبيه لإشارة جديدة"""
        message = self._format_signal(signal)
        
        if self.use_telegram:
            self._send_telegram(message)
        else:
            # طبع في Console (الافتراضي)
            self._print_console(message)
    
    def send_exit_alert(self, signal: Dict, exit_price: float, exit_reason: str):
        """إرسال تنبيه عند الخروج"""
        message = self._format_exit(signal, exit_price, exit_reason)
        
        if self.use_telegram:
            self._send_telegram(message)
        else:
            self._print_console(message)
    
    @staticmethod
    def _format_signal(signal: Dict) -> str:
        """تنسيق إشارة للطباعة"""
        direction_emoji = "🟢 LONG" if signal['direction'] == 'long' else "🔴 SHORT"
        
        message = f"""
╔════════════════════════════════════════╗
║ 🚨 إشارة دخول جديدة!                 ║
╚════════════════════════════════════════╝

{direction_emoji}
الوقت: {signal['timestamp']}
السعر: ${signal['entry_price']:.2f}
Stop Loss: ${signal['stop_loss']:.2f}
Take Profit: ${signal['take_profit']:.2f}
النسبة: {(signal['take_profit'] - signal['entry_price']) / (signal['entry_price'] - signal['stop_loss']):.2f}:1

مؤشرات:
  • EMA Fast: {signal['indicators'].get('ema_fast', 'N/A')}
  • EMA Slow: {signal['indicators'].get('ema_slow', 'N/A')}
  • RSI: {signal['indicators'].get('rsi', 'N/A')}
  • ATR: {signal['indicators'].get('atr', 'N/A')}
"""
        return message
    
    @staticmethod
    def _format_exit(signal: Dict, exit_price: float, exit_reason: str) -> str:
        """تنسيق الخروج للطباعة"""
        direction_emoji = "🟢 LONG" if signal['direction'] == 'long' else "🔴 SHORT"
        
        if signal['direction'] == 'long':
            pnl = exit_price - signal['entry_price']
        else:
            pnl = signal['entry_price'] - exit_price
        
        pnl_percent = (pnl / signal['entry_price']) * 100
        
        message = f"""
╔════════════════════════════════════════╗
║ 📊 الصفقة مغلقة                       ║
╚════════════════════════════════════════╝

{direction_emoji}
الدخول: ${signal['entry_price']:.2f}
الخروج: ${exit_price:.2f}
الربح/الخسارة: ${pnl:.2f} ({pnl_percent:+.2f}%)
السبب: {exit_reason}
"""
        return message
    
    def _print_console(self, message: str):
        """طبع في Console"""
        print(message)
    
    def _send_telegram(self, message: str):
        """إرسال عبر Telegram (يحتاج توكن و chat_id)"""
        try:
            import urllib.request
            url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
            data = {
                'chat_id': self.telegram_chat_id,
                'text': message,
                'parse_mode': 'HTML'
            }
            
            import json
            import urllib.parse
            
            post_data = urllib.parse.urlencode(data).encode('utf-8')
            req = urllib.request.Request(url, data=post_data)
            
            with urllib.request.urlopen(req, timeout=5) as response:
                print(f"✅ تم إرسال التنبيه عبر Telegram")
        
        except Exception as e:
            print(f"❌ خطأ إرسال Telegram: {e}")


# ==================== مراقب الأسعار الحي ====================
class LivePriceMonitor:
    """
    مراقبة الأسعار الحية وإصدار الإشارات
    
    الاستخدام:
        monitor = LivePriceMonitor('BTC/USDT', interval_minutes=15)
        monitor.start()
    """
    
    def __init__(
        self,
        symbol: str = 'BTCUSDT',
        interval_minutes: int = 15,
        use_telegram: bool = False,
        telegram_token: str = None,
        telegram_chat_id: str = None
    ):
        self.symbol = symbol
        self.interval_minutes = interval_minutes
        self.last_check = None
        self.price_history = []
        self.db = SignalDatabase()
        self.alerts = AlertSystem(use_telegram, telegram_token, telegram_chat_id)
        
        # معايير المخاطر
        self.max_daily_loss = 500  # $500 خسارة يومية
        self.daily_pnl = 0
        self.daily_start_time = datetime.now()
    
    def start(self, duration_hours: int = 24):
        """
        ابدأ المراقبة الحية
        
        Args:
            duration_hours: كم ساعة تراقب (24 = يوم كامل)
        """
        print(f"\n{'='*70}")
        print(f"🚀 بدء المراقبة الحية")
        print(f"{'='*70}")
        print(f"الرمز: {self.symbol}")
        print(f"الفترة: كل {self.interval_minutes} دقيقة")
        print(f"الحد الأقصى للخسارة اليومية: ${self.max_daily_loss}")
        print(f"المدة: {duration_hours} ساعة\n")
        
        start_time = datetime.now()
        end_time = start_time + timedelta(hours=duration_hours)
        
        check_count = 0
        
        while datetime.now() < end_time:
            check_count += 1
            now = datetime.now()
            
            print(f"\n[{now.strftime('%Y-%m-%d %H:%M:%S')}] فحص #{check_count}")
            
            # 1. جلب آخر شمعة
            try:
                latest_candle = self._fetch_latest_candle()
            except Exception as e:
                print(f"❌ خطأ جلب البيانات: {e}")
                self._wait_for_next_check()
                continue
            
            if not latest_candle:
                print("⚠️  لم تتمكن من جلب البيانات")
                self._wait_for_next_check()
                continue
            
            print(f"السعر الحالي: ${latest_candle['close']:.2f}")
            
            # 2. فحص الإشارات المفتوحة
            open_signals = self.db.get_open_signals()
            for idx, signal in enumerate(open_signals):
                exit_result = self._check_exit(signal, latest_candle)
                if exit_result:
                    exit_price, reason = exit_result
                    # أغلق الصفقة
                    pnl = self._calculate_pnl(signal, exit_price)
                    self.daily_pnl += pnl
                    
                    self.db.update_signal(idx, status='closed', exit_price=exit_price, exit_reason=reason)
                    self.alerts.send_exit_alert(signal, exit_price, reason)
            
            # 3. فحص الدخول الجديد (إذا ما في صفقات مفتوحة)
            if len(open_signals) == 0:
                # فحص إذا كان الخسارة اليومية سمحت
                if self.daily_pnl > -self.max_daily_loss:
                    # جرّب إشارة جديدة
                    new_signal = self._check_entry(latest_candle)
                    if new_signal:
                        self.db.add_signal(new_signal)
                        self.alerts.send_alert(new_signal)
                else:
                    print(f"⛔ حد الخسارة اليومية وصل (${self.daily_pnl:.2f})")
            
            # 4. تحديث الساعة
            self.last_check = now
            
            # 5. انتظر حتى الفحص التالي
            self._wait_for_next_check()
    
    def _fetch_latest_candle(self) -> Optional[Dict]:
        """
        جلب آخر شمعة (في الحالة الحقيقية، تجلب من Binance)
        للآن: بيانات تجريبية
        """
        # في الحقيقة:
        # استخدم binance_fetcher.py أو ccxt
        
        # للاختبار: بيانات وهمية
        import numpy as np
        np.random.seed(int(datetime.now().timestamp()))
        
        base_price = 50000
        noise = np.random.normal(0, 100)
        
        price = base_price + noise
        
        return {
            'timestamp': datetime.now().isoformat(),
            'close': price,
            'high': price + abs(np.random.normal(0, 50)),
            'low': price - abs(np.random.normal(0, 50)),
            'volume': np.random.randint(1000, 5000)
        }
    
    def _check_entry(self, candle: Dict) -> Optional[Dict]:
        """
        فحص إشارة دخول جديدة
        (في الحقيقة: استخدم signal_engine.py)
        """
        # للاختبار: إشارة عشوائية 1% من الحالات
        if np.random.random() < 0.01:
            direction = 'long' if np.random.random() < 0.5 else 'short'
            entry = candle['close']
            
            if direction == 'long':
                stop = entry - 200
                target = entry + 400
            else:
                stop = entry + 200
                target = entry - 400
            
            return {
                'timestamp': candle['timestamp'],
                'symbol': self.symbol,
                'direction': direction,
                'entry_price': entry,
                'stop_loss': stop,
                'take_profit': target,
                'indicators': {
                    'ema_fast': entry * 0.99,
                    'ema_slow': entry * 0.98,
                    'rsi': 45,
                    'atr': 100
                },
                'status': 'open'
            }
        
        return None
    
    def _check_exit(self, signal: Dict, candle: Dict) -> Optional[tuple]:
        """
        فحص شروط الخروج من صفقة مفتوحة
        
        Returns:
            (exit_price, reason) أو None
        """
        price = candle['close']
        
        if signal['direction'] == 'long':
            if price >= signal['take_profit']:
                return (signal['take_profit'], "📈 Take Profit ✅")
            elif price <= signal['stop_loss']:
                return (signal['stop_loss'], "📉 Stop Loss ❌")
        else:
            if price <= signal['take_profit']:
                return (signal['take_profit'], "📈 Take Profit ✅")
            elif price >= signal['stop_loss']:
                return (signal['stop_loss'], "📉 Stop Loss ❌")
        
        return None
    
    def _calculate_pnl(self, signal: Dict, exit_price: float) -> float:
        """احسب الربح/الخسارة"""
        if signal['direction'] == 'long':
            return exit_price - signal['entry_price']
        else:
            return signal['entry_price'] - exit_price
    
    def _wait_for_next_check(self):
        """انتظر حتى الفحص التالي"""
        sleep_seconds = self.interval_minutes * 60
        print(f"⏳ الفحص التالي بعد {self.interval_minutes} دقيقة...")
        
        # في التطوير الحقيقي: استخدم scheduler
        # للآن: قصّر وقت الانتظار للاختبار
        # time.sleep(sleep_seconds)


# ==================== Dashboard بسيط ====================
class SimpleDashboard:
    """
    لوحة تحكم بسيطة (Console-based)
    """
    
    def __init__(self, db: SignalDatabase):
        self.db = db
    
    def display_summary(self):
        """عرض ملخص الأداء"""
        signals = self.db.signals
        
        if not signals:
            print("لا توجد إشارات بعد")
            return
        
        open_signals = [s for s in signals if s.get('status') == 'open']
        closed_signals = [s for s in signals if s.get('status') == 'closed']
        
        total_pnl = 0
        for s in closed_signals:
            if 'exit_price' in s:
                if s['direction'] == 'long':
                    pnl = s['exit_price'] - s['entry_price']
                else:
                    pnl = s['entry_price'] - s['exit_price']
                total_pnl += pnl
        
        print("\n" + "="*70)
        print("📊 ملخص الأداء")
        print("="*70)
        print(f"إجمالي الإشارات: {len(signals)}")
        print(f"مفتوحة: {len(open_signals)}")
        print(f"مغلقة: {len(closed_signals)}")
        print(f"الربح الكلي: ${total_pnl:.2f}")
        print("="*70)


# ==================== الرئيسي ====================
if __name__ == "__main__":
    print("\n" + "="*70)
    print("🚀 نظام المراقبة الحية")
    print("="*70)
    
    # إنشاء المراقب
    monitor = LivePriceMonitor(
        symbol='BTC/USDT',
        interval_minutes=15,
        use_telegram=False  # غيّر إلى True لـ Telegram
        # telegram_token='YOUR_TOKEN',
        # telegram_chat_id='YOUR_CHAT_ID'
    )
    
    # ابدأ المراقبة (اختبر 10 دقائق فقط)
    print("\n📌 وضع الاختبار: سيراقب لمدة 2 دقيقة فقط\n")
    
    try:
        # للاختبار: مدة قصيرة
        monitor.start(duration_hours=0.04)  # ~2 دقيقة
    except KeyboardInterrupt:
        print("\n\n⏹️  توقفت المراقبة")
    
    # عرض الملخص
    dashboard = SimpleDashboard(monitor.db)
    dashboard.display_summary()
    
    print("\n" + "="*70)
    print("✅ انتهت جلسة المراقبة")
    print("="*70 + "\n")
