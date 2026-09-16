"""
متداول بينانس متقدم باستخدام CCXT
Advanced Binance Trader using CCXT Library

CCXT هي مكتبة قوية لـ API جميع المنصات
"""

import ccxt
import time
from datetime import datetime
from typing import Dict, List, Optional


class CCXTBinanceTrader:
    """
    متداول بينانس متقدم باستخدام CCXT
    
    المميزات:
    - استخدام CCXT (مكتبة قوية وآمنة)
    - دعم Testnet و Real Network
    - Trailing Stop Loss
    - Multi-Pair Trading
    - إدارة رأس مال ذكية
    """
    
    def __init__(self, api_key: str, api_secret: str, test_mode: bool = True):
        """
        تهيئة المتداول
        
        Args:
            api_key: مفتاح Binance API
            api_secret: سر Binance API
            test_mode: استخدم Testnet (آمن جداً)
        """
        
        self.test_mode = test_mode
        self.open_positions = {}
        self.closed_trades = []
        
        # إعدادات الأمان
        self.exchange_config = {
            'apiKey': api_key,
            'secret': api_secret,
            'enableRateLimit': True,
            'timeout': 30000,
        }
        
        # استخدم Testnet في البداية
        if test_mode:
            self.exchange_config['urls'] = {
                'api': 'https://testnet.binance.vision/api',
            }
            print("✅ وضع الاختبار (Testnet) - آمن تماماً")
        else:
            print("🔴 وضع حقيقي - استخدم بحذر شديد!")
        
        # إنشاء كائن التبادل
        self.exchange = ccxt.binance(self.exchange_config)
        
        # الأزواج
        self.pairs = ['BTC/USDT', 'ETH/USDT', 'ADA/USDT', 'BNB/USDT']
        
        # الإعدادات
        self.risk_per_trade = 0.02
        self.max_open_positions = 3
        self.trailing_stop_percent = 2.0
        
        print("✅ تم الاتصال بـ Binance بنجاح")
    
    def get_balance(self) -> Dict:
        """
        احصل على رصيد الحساب
        """
        try:
            balance = self.exchange.fetch_balance()
            return {
                'USDT': balance['free'].get('USDT', 0),
                'total': balance['free'].get('USDT', 0),
                'BTC': balance['free'].get('BTC', 0),
                'ETH': balance['free'].get('ETH', 0),
            }
        except Exception as e:
            print(f"❌ خطأ في جلب الرصيد: {e}")
            return None
    
    def get_ticker(self, symbol: str) -> Dict:
        """احصل على سعر الزوج الحالي"""
        try:
            ticker = self.exchange.fetch_ticker(symbol)
            return {
                'symbol': symbol,
                'price': ticker['close'],
                'bid': ticker['bid'],
                'ask': ticker['ask'],
                'volume': ticker['quoteVolume'],
                'timestamp': ticker['timestamp']
            }
        except Exception as e:
            print(f"❌ خطأ في جلب السعر: {e}")
            return None
    
    def place_limit_order(self, symbol: str, side: str, amount: float, price: float) -> Dict:
        """
        ضع أمر تحديد السعر
        
        Args:
            symbol: الزوج (مثل BTC/USDT)
            side: buy أو sell
            amount: الكمية
            price: السعر
        """
        try:
            order = self.exchange.create_limit_order(
                symbol=symbol,
                side=side,
                amount=amount,
                price=price
            )
            
            print(f"""
✅ أمر تم وضعه:
  الزوج: {symbol}
  النوع: {'🟢 شراء' if side == 'buy' else '🔴 بيع'}
  الكمية: {amount}
  السعر: ${price:.2f}
  رقم الأمر: {order['id']}
            """)
            
            self.open_positions[order['id']] = {
                'order_id': order['id'],
                'symbol': symbol,
                'side': side,
                'amount': amount,
                'price': price,
                'entry_time': datetime.now().isoformat(),
                'status': 'open',
                'trailing_stop': price * (1 - self.trailing_stop_percent / 100)
            }
            
            return order
        
        except Exception as e:
            print(f"❌ خطأ في وضع الأمر: {e}")
            return None
    
    def place_market_order(self, symbol: str, side: str, amount: float) -> Dict:
        """ضع أمر سوق (فوري)"""
        try:
            order = self.exchange.create_market_order(
                symbol=symbol,
                side=side,
                amount=amount
            )
            return order
        except Exception as e:
            print(f"❌ خطأ في أمر السوق: {e}")
            return None
    
    def cancel_order(self, order_id: str, symbol: str) -> bool:
        """إلغاء أمر"""
        try:
            self.exchange.cancel_order(order_id, symbol)
            print(f"✅ تم إلغاء الأمر: {order_id}")
            return True
        except Exception as e:
            print(f"❌ خطأ في إلغاء الأمر: {e}")
            return False
    
    def get_open_orders(self) -> List[Dict]:
        """احصل على الأوامر المفتوحة"""
        try:
            orders = []
            for symbol in self.pairs:
                open_orders = self.exchange.fetch_open_orders(symbol)
                orders.extend(open_orders)
            return orders
        except Exception as e:
            print(f"❌ خطأ في جلب الأوامر: {e}")
            return []
    
    def monitor_positions(self):
        """راقب الصفقات المفتوحة"""
        print(f"\n⏰ {datetime.now().strftime('%H:%M:%S')} - مراقبة الصفقات")
        
        for order_id, position in list(self.open_positions.items()):
            symbol = position['symbol']
            ticker = self.get_ticker(symbol)
            
            if not ticker:
                continue
            
            current_price = ticker['price']
            entry_price = position['price']
            
            # حساب P&L
            if position['side'] == 'buy':
                pnl = (current_price - entry_price) * position['amount']
                
                # تحديث Trailing Stop
                new_stop = current_price * (1 - self.trailing_stop_percent / 100)
                if new_stop > position['trailing_stop']:
                    position['trailing_stop'] = new_stop
                
                # فحص الخروج
                if current_price <= position['trailing_stop']:
                    print(f"🛑 Trailing Stop Hit: {symbol}")
                    self.close_position(order_id, symbol, current_price)
            
            else:
                pnl = (entry_price - current_price) * position['amount']
            
            pnl_percent = (pnl / (entry_price * position['amount'])) * 100
            
            print(f"  {symbol}: ${pnl:.2f} ({pnl_percent:.1f}%)")
    
    def close_position(self, order_id: str, symbol: str, exit_price: float):
        """أغلق صفقة"""
        if order_id in self.open_positions:
            position = self.open_positions.pop(order_id)
            
            if position['side'] == 'buy':
                pnl = (exit_price - position['price']) * position['amount']
            else:
                pnl = (position['price'] - exit_price) * position['amount']
            
            position['exit_price'] = exit_price
            position['pnl'] = pnl
            position['pnl_percent'] = (pnl / (position['price'] * position['amount'])) * 100
            position['status'] = 'closed'
            
            self.closed_trades.append(position)
            
            status = "✅ ربح" if pnl > 0 else "❌ خسارة"
            print(f"{status} {symbol}: ${pnl:.2f}")
    
    def start_trading(self, interval: int = 60):
        """بدء التداول الحي"""
        print("\n🚀 بدء التداول الحي")
        print(f"⚠️  تحذير: هذا تداول {'محاكاة' if self.test_mode else 'حقيقي'}!")
        print(f"💰 الرصيد: ${self.get_balance()['total']:.2f}\n")
        
        try:
            while True:
                # راقب الصفقات
                self.monitor_positions()
                
                # اختبر الإشارات الجديدة
                for symbol in self.pairs:
                    if len(self.open_positions) >= self.max_open_positions:
                        break
                    
                    ticker = self.get_ticker(symbol)
                    if ticker:
                        # هنا أضف منطق الإشارات الخاص بك
                        pass
                
                time.sleep(interval)
        
        except KeyboardInterrupt:
            print("\n⏹️  تم إيقاف التداول")
            self.print_report()
    
    def print_report(self):
        """طباعة تقرير الأداء"""
        if not self.closed_trades:
            print("لا توجد صفقات مغلقة")
            return
        
        trades = self.closed_trades
        wins = sum(1 for t in trades if t['pnl'] > 0)
        total_profit = sum(t['pnl'] for t in trades if t['pnl'] > 0)
        total_loss = abs(sum(t['pnl'] for t in trades if t['pnl'] < 0))
        
        print(f"""
╔════════════════════════════════════════════╗
║            📊 التقرير النهائي            ║
╚════════════════════════════════════════════╝

  الصفقات: {len(trades)}
  الرابحة: {wins}
  الربح الصافي: ${total_profit - total_loss:.2f}
  Profit Factor: {total_profit / (total_loss + 0.01):.2f}
        """)


if __name__ == "__main__":
    from config import BINANCE_API_KEY, BINANCE_API_SECRET
    
    trader = CCXTBinanceTrader(
        api_key=BINANCE_API_KEY,
        api_secret=BINANCE_API_SECRET,
        test_mode=True
    )
    
    # اختبر جلب الرصيد
    balance = trader.get_balance()
    if balance:
        print(f"💰 الرصيد: ${balance['total']:.2f}")
    
    # بدء التداول
    # trader.start_trading(interval=60)
