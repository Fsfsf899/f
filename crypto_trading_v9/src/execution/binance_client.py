"""
عميل بينانس الموقّع — البند 28.
================================
HMAC-SHA256 مُتحقَّق ضد المثال الرسمي في وثائق بينانس.

TEST MODE = testnet.binance.vision (أوامر حقيقية، أموال وهمية)
LIVE MODE = api.binance.com          (أموال حقيقية)

لا يوجد وضع "محاكاة محلية" هنا. أي محاكاة تعيش في الباكتست فقط،
حتى لا يُخلط أمر وهمي بأمر منفَّذ.
"""
import hmac, hashlib, json, os, time, random
import urllib.request, urllib.parse, urllib.error
from typing import Optional, Dict, List

TESTNET = "https://testnet.binance.vision"
MAINNET = "https://api.binance.com"

# ── حاجز Mainnet (المرحلة 12) ──
# بناء عميل mainnet يتطلب متغير بيئة صريحاً. الافتراضي: ممنوع.
# هذا يمنع أي اتصال عرضي بحساب حقيقي أثناء التطوير أو الاختبارات.
MAINNET_ENV = 'ALLOW_MAINNET'

# ══════════════════════════════════════════════════════════════
#  قفل مستوى الكود — v9
#
#  التداول الحقيقي معطَّل في المصدر نفسه، لا في متغير بيئة فقط.
#  متغير البيئة وحده لا يكفي: خطأ في ملف .env أو في سكربت تشغيل
#  يكفي لفتح حساب حقيقي بلا قصد.
#
#  لتمكينه يلزم **تعديل هذا السطر يدوياً** ثم ضبط ALLOW_MAINNET=1.
#  بوابتان مستقلتان، إحداهما لا تُضبط بالخطأ.
# ══════════════════════════════════════════════════════════════
MAINNET_ENABLED_IN_SOURCE = False      # ⛔ لا تغيّره إلا بقرار واعٍ


class MainnetBlocked(Exception):
    """محاولة بناء عميل mainnet بلا إذن صريح."""


def mainnet_allowed() -> bool:
    """يتطلب البوابتين معاً: قفل المصدر + متغير البيئة."""
    if not MAINNET_ENABLED_IN_SOURCE:
        return False
    return os.getenv(MAINNET_ENV, '').strip().lower() in ('1', 'true', 'yes')


def mainnet_block_reason() -> str:
    if not MAINNET_ENABLED_IN_SOURCE:
        return ('MAINNET_ENABLED_IN_SOURCE=False في '
                'src/execution/binance_client.py — التداول الحقيقي معطَّل '
                'في المصدر')
    if os.getenv(MAINNET_ENV, '').strip().lower() not in ('1', 'true', 'yes'):
        return f'{MAINNET_ENV} غير مضبوط'
    return 


class BinanceError(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg); self.code = code


class BinanceClient:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = True,
                 recv_window: int = 5000):
        if not api_key or not api_secret:
            raise BinanceError("مفاتيح API مفقودة")
        self.key = api_key.strip()
        self._secret = api_secret.strip().encode()
        if not testnet and not mainnet_allowed():
            raise MainnetBlocked(
                f"⛔ الاتصال بـ Mainnet ممنوع. السبب: {mainnet_block_reason()}")
        self.base = TESTNET if testnet else MAINNET
        self.testnet = testnet
        self._last_sync = 0
        self.resync_interval_ms = 30 * 60 * 1000
        self.recv_window = recv_window
        self._offset = 0
        self._rules: Dict[str, Dict] = {}

    def _sign(self, params: Dict) -> str:
        qs = urllib.parse.urlencode(params)
        sig = hmac.new(self._secret, qs.encode(), hashlib.sha256).hexdigest()
        return f"{qs}&signature={sig}"

    def _req(self, method: str, path: str, params: Optional[Dict] = None,
             signed: bool = False):
        params = dict(params or {})
        if signed:
            params['timestamp'] = int(time.time() * 1000) + self._offset
            params['recvWindow'] = self.recv_window
            body = self._sign(params)
        else:
            body = urllib.parse.urlencode(params)
        headers = {'X-MBX-APIKEY': self.key, 'User-Agent': 'Mozilla/5.0'}
        url = self.base + path
        if method == 'GET':
            req = urllib.request.Request(f"{url}?{body}" if body else url,
                                         headers=headers, method='GET')
        else:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
            req = urllib.request.Request(url, data=body.encode(),
                                         headers=headers, method=method)
        from .errors import redact
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                j = json.loads(raw)
                raise BinanceError(
                    f"[{j.get('code')}] {redact(str(j.get('msg')))}", j.get('code'))
            except json.JSONDecodeError:
                raise BinanceError(f"HTTP {e.code}: {redact(raw[:200])}", e.code)
        except urllib.error.URLError as e:
            raise BinanceError(f"URLError: {redact(str(e.reason))}")
        except Exception as e:
            # الرسالة قد تحوي الـ URL بتوقيعه — تُحجب قبل أي تسجيل
            raise BinanceError(f"{type(e).__name__}: {redact(str(e))}")

    # ── عام ──
    def ping(self): self._req('GET', '/api/v3/ping'); return True

    def sync_time(self) -> int:
        srv = self._req('GET', '/api/v3/time')['serverTime']
        self._offset = srv - int(time.time() * 1000)
        self._last_sync = int(time.time() * 1000)
        return self._offset

    def maybe_resync(self) -> bool:
        """
        إعادة مزامنة دورية. انحراف ساعة الجهاز خلال أيام يُنتج -1021،
        وهو مصنَّف كخطأ مصادقة يوقف النظام — فمنعه أولى من علاجه.
        """
        now = int(time.time() * 1000)
        if now - self._last_sync < self.resync_interval_ms:
            return False
        try:
            self.sync_time()
            return True
        except BinanceError:
            return False

    def now_ms(self) -> int:
        return int(time.time() * 1000) + self._offset

    def price(self, symbol: str) -> float:
        return float(self._req('GET', '/api/v3/ticker/price',
                               {'symbol': symbol})['price'])

    def book_ticker(self, symbol: str) -> Dict:
        b = self._req('GET', '/api/v3/ticker/bookTicker', {'symbol': symbol})
        bid, ask = float(b['bidPrice']), float(b['askPrice'])
        mid = (bid + ask) / 2
        return {'bid': bid, 'ask': ask, 'mid': mid,
                'bid_qty': float(b['bidQty']), 'ask_qty': float(b['askQty']),
                'spread_bps': (ask - bid) / mid * 10000 if mid > 0 else float('inf')}

    def order_book(self, symbol: str, limit: int = 100) -> Dict:
        d = self._req('GET', '/api/v3/depth', {'symbol': symbol, 'limit': limit})
        return {'bids': [[float(p), float(q)] for p, q in d['bids']],
                'asks': [[float(p), float(q)] for p, q in d['asks']]}

    def rules(self, symbol: str) -> Dict:
        if symbol in self._rules:
            return self._rules[symbol]
        s = self._req('GET', '/api/v3/exchangeInfo', {'symbol': symbol})['symbols'][0]
        out = {'symbol': s['symbol'], 'status': s['status'],
               'base': s['baseAsset'], 'quote': s['quoteAsset'],
               'spot': s.get('isSpotTradingAllowed', False),
               'step_size': 1e-8, 'min_qty': 0.0, 'tick_size': 0.01,
               'min_notional': 10.0}
        for f in s['filters']:
            t = f['filterType']
            if t == 'LOT_SIZE':
                out['step_size'] = float(f['stepSize']); out['min_qty'] = float(f['minQty'])
            elif t == 'PRICE_FILTER':
                out['tick_size'] = float(f['tickSize'])
            elif t in ('MIN_NOTIONAL', 'NOTIONAL'):
                out['min_notional'] = float(f.get('minNotional', 10))
        self._rules[symbol] = out
        return out

    @staticmethod
    def _round_step(v: float, step: float) -> float:
        """
        تقريب لأسفل إلى مضاعف الخطوة.

        الفاصلة العائمة تجعل 0.01/1e-5 = 999.9999999999999 فيصبح الناتج
        0.00999 — وحدة كاملة أقل. تصحيح بهامش صغير قبل floor.
        """
        import math
        if step <= 0:
            return v
        n = v / step
        if abs(n - round(n)) < 1e-9:
            n = round(n)
        r = math.floor(n) * step
        dec = max(0, len(f"{step:.10f}".rstrip('0').split('.')[-1]))
        return float(f"{r:.{dec}f}")

    def round_qty(self, symbol: str, qty: float) -> float:
        return self._round_step(qty, self.rules(symbol)['step_size'])

    def round_price(self, symbol: str, px: float) -> float:
        return self._round_step(px, self.rules(symbol)['tick_size'])

    # ── موقّع ──
    def account(self) -> Dict:
        return self._req('GET', '/api/v3/account', signed=True)

    def balances(self) -> Dict[str, Dict]:
        return {b['asset']: {'free': float(b['free']), 'locked': float(b['locked'])}
                for b in self.account()['balances']
                if float(b['free']) + float(b['locked']) > 0}

    def can_trade(self) -> bool:
        return bool(self.account().get('canTrade', False))

    @staticmethod
    def new_client_id(prefix: str = 'v6') -> str:
        """
        ⛔ مهجورة. المعرّف العشوائي يجعل التعافي مستحيلاً: إعادة المحاولة
        بعد انقطاع تحمل معرّفاً جديداً، فتراها بينانس أمراً مستقلاً وتنفّذه.
        استخدم build_client_order_id عبر IdempotentOrderGate.
        """
        raise RuntimeError(
            "معرّف عشوائي ممنوع — استخدم "
            "src.execution.idempotency.build_client_order_id")

    def market_buy_quote(self, symbol: str, quote_amount: float,
                         client_id: Optional[str] = None) -> Dict:
        return self._req('POST', '/api/v3/order', {
            'symbol': symbol, 'side': 'BUY', 'type': 'MARKET',
            'quoteOrderQty': round(quote_amount, 2),
            'newClientOrderId': client_id or self.new_client_id()}, signed=True)

    def market_sell(self, symbol: str, qty: float,
                    client_id: Optional[str] = None) -> Dict:
        return self._req('POST', '/api/v3/order', {
            'symbol': symbol, 'side': 'SELL', 'type': 'MARKET',
            'quantity': self.round_qty(symbol, qty),
            'newClientOrderId': client_id or self.new_client_id()}, signed=True)

    def limit_sell(self, symbol: str, qty: float, price: float,
                   client_id: str) -> Dict:
        """أمر بيع محدَّد — يُستخدم لأمر الهدف."""
        return self._req('POST', '/api/v3/order', {
            'symbol': symbol, 'side': 'SELL', 'type': 'LIMIT',
            'timeInForce': 'GTC', 'quantity': self.round_qty(symbol, qty),
            'price': f"{self.round_price(symbol, price)}",
            'newClientOrderId': client_id}, signed=True)

    def oco_sell(self, symbol: str, qty: float, target_price: float,
                 stop_price: float, stop_limit_price: float,
                 client_id: str) -> Dict:
        """
        أمر OCO: هدف + وقف في قائمة واحدة، ينفَّذ أحدهما ويُلغى الآخر.

        على Spot لا يمكن حجز نفس الكمية لأمرين منفصلين — الوقف يحجز
        الرصيد فيتعذّر وضع الهدف. OCO هو الحل الوحيد لحماية مزدوجة.
        """
        return self._req('POST', '/api/v3/order/oco', {
            'symbol': symbol, 'side': 'SELL',
            'quantity': self.round_qty(symbol, qty),
            'price': f"{self.round_price(symbol, target_price)}",
            'stopPrice': f"{self.round_price(symbol, stop_price)}",
            'stopLimitPrice': f"{self.round_price(symbol, stop_limit_price)}",
            'stopLimitTimeInForce': 'GTC',
            'listClientOrderId': client_id}, signed=True)

    def stop_loss_limit(self, symbol: str, qty: float, stop_price: float,
                        limit_price: float, client_id: Optional[str] = None) -> Dict:
        return self._req('POST', '/api/v3/order', {
            'symbol': symbol, 'side': 'SELL', 'type': 'STOP_LOSS_LIMIT',
            'timeInForce': 'GTC', 'quantity': self.round_qty(symbol, qty),
            'stopPrice': f"{self.round_price(symbol, stop_price)}",
            'price': f"{self.round_price(symbol, limit_price)}",
            'newClientOrderId': client_id or self.new_client_id()}, signed=True)

    def order_by_client_id(self, symbol: str, client_order_id: str) -> Dict:
        """
        الاستعلام بالمعرّف الذي نولّده نحن.
        هذا ما يجعل التعافي بعد الانقطاع ممكناً: نسأل بينانس عن نيّتنا
        بالاسم، بدل التخمين. يرمي BinanceError برمز -2013 إن لم يوجد.
        """
        return self._req('GET', '/api/v3/order',
                         {'symbol': symbol, 'origClientOrderId': client_order_id},
                         signed=True)

    def cancel_by_client_id(self, symbol: str, client_order_id: str) -> Dict:
        return self._req('DELETE', '/api/v3/order',
                         {'symbol': symbol, 'origClientOrderId': client_order_id},
                         signed=True)

    def base_free(self, symbol: str) -> float:
        """رصيد الأصل الأساس الفعلي — لا يُباع أكثر منه."""
        base = self.rules(symbol)['base']
        b = self.balances().get(base, {})
        return float(b.get('free', 0.0))

    def order_status(self, symbol: str, order_id) -> Dict:
        return self._req('GET', '/api/v3/order',
                         {'symbol': symbol, 'orderId': order_id}, signed=True)

    def open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        return self._req('GET', '/api/v3/openOrders',
                         {'symbol': symbol} if symbol else {}, signed=True)

    def cancel(self, symbol: str, order_id) -> Dict:
        return self._req('DELETE', '/api/v3/order',
                         {'symbol': symbol, 'orderId': order_id}, signed=True)

    def my_trades(self, symbol: str, limit: int = 100) -> List[Dict]:
        return self._req('GET', '/api/v3/myTrades',
                         {'symbol': symbol, 'limit': limit}, signed=True)
