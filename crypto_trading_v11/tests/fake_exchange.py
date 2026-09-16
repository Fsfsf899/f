"""
منصة وهمية قابلة للتحكم — TEST DOUBLE.
======================================
ليست بيانات سوق ولا تُستورد من src/. غرضها الوحيد محاكاة أنماط
فشل بينانس لاختبار منطق البوابة والمصالحة.

الأنماط المدعومة:
  accept_then_timeout          قُبل الأمر ثم انقطع الرد   ← الأخطر
  network_disconnect_after_accept  مرادف
  reject_definitively          رفض قاطع (رصيد/مخاطر)
  not_found                    الاستعلام لا يجد الأمر
  partial_fill                 تنفيذ جزئي
  rate_limit                   429
  duplicate_response           المنصة ترد بـ Duplicate
  delayed_visibility           الأمر لا يظهر في الاستعلام إلا بعد N محاولات
  auth_error                   خطأ مصادقة
  unknown_error                خطأ غير مصنَّف
"""
import threading
from typing import Dict, List, Optional
from src.execution.binance_client import BinanceError

RULES = {'symbol': 'BTCUSDT', 'status': 'TRADING', 'spot': True,
         'base': 'BTC', 'quote': 'USDT', 'step_size': 0.00001,
         'min_qty': 0.00001, 'tick_size': 0.01, 'min_notional': 10.0}


class FakeExchange:
    def __init__(self, price: float = 50000.0, base_free: float = 0.0,
                 quote_free: float = 10000.0):
        self.orders: Dict[str, Dict] = {}
        self.calls = 0
        self.send_calls = 0
        self.query_calls = 0
        self.mode: Optional[str] = None
        self.times = 0
        self.hidden: Dict[str, int] = {}
        self._price = price
        self.testnet = True
        self._base = base_free
        self._quote = quote_free
        self._id = 1
        self._lock = threading.Lock()
        self.mainnet_contacted = False

    # ── التحكم ──
    def fail(self, mode: str, times: int = 1):
        self.mode, self.times = mode, times
        return self

    def _next_id(self):
        self._id += 1
        return 7000 + self._id

    def _trigger(self, cid: str, symbol: str, side: str, qty: float,
                 px: float, otype: str):
        if self.times <= 0 or self.mode is None:
            return
        self.times -= 1
        m = self.mode

        if m in ('accept_then_timeout', 'network_disconnect_after_accept'):
            self._store(cid, symbol, side, qty, px, otype, 'FILLED', qty)
            raise BinanceError('Read timed out')
        if m == 'partial_fill':
            self._store(cid, symbol, side, qty, px, otype, 'PARTIALLY_FILLED',
                        qty * 0.4)
            return
        if m == 'delayed_visibility':
            self._store(cid, symbol, side, qty, px, otype, 'FILLED', qty)
            self.hidden[cid] = 2
            raise BinanceError('Connection reset')
        if m == 'reject_definitively':
            raise BinanceError('[-2010] Account has insufficient balance.', -2010)
        if m == 'risk_reject':
            raise BinanceError('[-2010] Order would trigger immediately.', -2010)
        if m == 'not_found':
            raise BinanceError('Read timed out')
        if m == 'rate_limit':
            raise BinanceError('[-1003] TOO_MANY_REQUESTS', -1003)
        if m == 'duplicate_response':
            raise BinanceError('[-2010] Duplicate order sent.', -2010)
        if m == 'auth_error':
            raise BinanceError('[-2015] Invalid API-key.', -2015)
        if m == 'unknown_error':
            raise BinanceError('something entirely unexpected')
        if m == 'invalid_params':
            raise BinanceError('[-1013] Filter failure: LOT_SIZE', -1013)

    def _store(self, cid, symbol, side, qty, px, otype, status, executed):
        self.orders[cid] = {
            'orderId': self._next_id(), 'clientOrderId': cid, 'symbol': symbol,
            'side': side, 'type': otype, 'status': status,
            'origQty': f'{qty:.8f}', 'executedQty': f'{executed:.8f}',
            'cummulativeQuoteQty': f'{executed * px:.8f}',
            'price': f'{px:.2f}',
            'fills': ([{'qty': f'{executed:.8f}', 'price': f'{px:.2f}',
                        'commission': '0.0', 'commissionAsset': 'USDT',
                        'tradeId': f'tr-{cid}'}] if executed > 0 and
                      otype == 'MARKET' else [])}

    # ── واجهة العميل ──
    def sync_time(self): return 0
    def maybe_resync(self): return False
    def now_ms(self):
        import time; return int(time.time() * 1000)
    def ping(self): return True
    def can_trade(self): return True
    def price(self, symbol): return self._price
    def set_price(self, p): self._price = p
    def rules(self, symbol): return dict(RULES)
    def round_qty(self, symbol, q): return round(q, 5)
    def round_price(self, symbol, p): return round(p, 2)
    def base_free(self, symbol): return self._base
    def account(self): return {'canTrade': True, 'balances': [
        {'asset': 'BTC', 'free': str(self._base), 'locked': '0'},
        {'asset': 'USDT', 'free': str(self._quote), 'locked': '0'}]}
    def balances(self):
        return {'BTC': {'free': self._base, 'locked': 0.0},
                'USDT': {'free': self._quote, 'locked': 0.0}}

    def market_buy_quote(self, symbol, quote, cid):
        with self._lock:
            self.calls += 1; self.send_calls += 1
            if cid in self.orders:
                raise BinanceError('[-2010] Duplicate order sent.', -2010)
            qty = quote / self._price
            self._trigger(cid, symbol, 'BUY', qty, self._price, 'MARKET')
            self._store(cid, symbol, 'BUY', qty, self._price, 'MARKET', 'FILLED', qty)
            self._base += qty; self._quote -= quote
            return self.orders[cid]

    def market_sell(self, symbol, qty, cid):
        with self._lock:
            self.calls += 1; self.send_calls += 1
            if cid in self.orders:
                raise BinanceError('[-2010] Duplicate order sent.', -2010)
            if qty > self._base + 1e-9:
                raise BinanceError('[-2010] Account has insufficient balance.', -2010)
            self._trigger(cid, symbol, 'SELL', qty, self._price, 'MARKET')
            self._store(cid, symbol, 'SELL', qty, self._price, 'MARKET', 'FILLED', qty)
            self._base -= qty; self._quote += qty * self._price
            return self.orders[cid]

    def limit_sell(self, symbol, qty, price, cid):
        with self._lock:
            self.calls += 1; self.send_calls += 1
            if cid in self.orders:
                raise BinanceError('[-2010] Duplicate order sent.', -2010)
            self._trigger(cid, symbol, 'SELL', qty, price, 'LIMIT')
            self._store(cid, symbol, 'SELL', qty, price, 'LIMIT', 'NEW', 0)
            return self.orders[cid]

    def stop_loss_limit(self, symbol, qty, stop, limit, cid):
        with self._lock:
            self.calls += 1; self.send_calls += 1
            if cid in self.orders:
                raise BinanceError('[-2010] Duplicate order sent.', -2010)
            self._trigger(cid, symbol, 'SELL', qty, limit, 'STOP_LOSS_LIMIT')
            self._store(cid, symbol, 'SELL', qty, limit, 'STOP_LOSS_LIMIT', 'NEW', 0)
            self.orders[cid]['stopPrice'] = f'{stop:.2f}'
            return self.orders[cid]

    def oco_sell(self, symbol, qty, target_price, stop_price, stop_limit_price,
                cid, *, above_client_id=None, below_client_id=None):
        """
        استجابة OCO واقعية عبر عقد `/api/v3/orderList/oco` الحالي —
        أب (orderListId) وابنان بمعرّفات مستقلة، مطابقة تماماً لبنية
        استجابة بينانس الحقيقية (orderReports).
        """
        with self._lock:
            self.calls += 1; self.send_calls += 1
            stop_cid = below_client_id or f'{cid}-STOP'
            target_cid = above_client_id or f'{cid}-TARGET'
            if stop_cid in self.orders or target_cid in self.orders:
                raise BinanceError('[-2010] Duplicate order sent.', -2010)
            self._trigger(cid, symbol, 'SELL', qty, target_price, 'OCO')
            list_id = self._next_id()
            self._store(stop_cid, symbol, 'SELL', qty, stop_limit_price,
                       'STOP_LOSS_LIMIT', 'NEW', 0)
            self.orders[stop_cid]['stopPrice'] = f'{stop_price:.2f}'
            self.orders[stop_cid]['orderListId'] = list_id
            self.orders[stop_cid]['listClientOrderId'] = cid
            self._store(target_cid, symbol, 'SELL', qty, target_price,
                       'LIMIT_MAKER', 'NEW', 0)
            self.orders[target_cid]['orderListId'] = list_id
            self.orders[target_cid]['listClientOrderId'] = cid
            return {
                'orderListId': list_id, 'listClientOrderId': cid,
                'listStatusType': 'EXEC_STARTED', 'listOrderStatus': 'EXECUTING',
                'symbol': symbol,
                'orders': [
                    {'symbol': symbol, 'orderId': self.orders[stop_cid]['orderId'],
                     'clientOrderId': stop_cid},
                    {'symbol': symbol, 'orderId': self.orders[target_cid]['orderId'],
                     'clientOrderId': target_cid}],
                'orderReports': [dict(self.orders[stop_cid]),
                                 dict(self.orders[target_cid])],
            }

    def order_by_client_id(self, symbol, cid):
        self.calls += 1; self.query_calls += 1
        if cid in self.hidden and self.hidden[cid] > 0:
            self.hidden[cid] -= 1
            raise BinanceError('[-2013] Order does not exist.', -2013)
        o = self.orders.get(cid)
        if o is None:
            raise BinanceError('[-2013] Order does not exist.', -2013)
        return o

    def order_status(self, symbol, order_id):
        self.calls += 1
        for o in self.orders.values():
            if str(o['orderId']) == str(order_id):
                return o
        raise BinanceError('[-2013] Order does not exist.', -2013)

    def open_orders(self, symbol=None):
        return [o for o in self.orders.values()
                if o['status'] in ('NEW', 'PARTIALLY_FILLED')
                and (symbol is None or o['symbol'] == symbol)]

    def cancel(self, symbol, order_id):
        for o in self.orders.values():
            if str(o['orderId']) == str(order_id):
                if o['status'] not in ('NEW', 'PARTIALLY_FILLED'):
                    raise BinanceError('[-2011] CANCEL_REJECTED', -2011)
                o['status'] = 'CANCELED'
                return o
        raise BinanceError('[-2013] Order does not exist.', -2013)

    def my_trades(self, symbol, limit=100):
        out = []
        for o in self.orders.values():
            for f in o.get('fills', []):
                out.append({'id': f['tradeId'], 'qty': f['qty'],
                            'price': f['price'], 'time': 0})
        return out[-limit:]
