"""
عميل بينانس الحقيقي — REST موقّع
==================================
HMAC-SHA256 صحيح، بدون مكتبات خارجية.

الفرق عن binance_live_trader.py القديم:
ذاك كان يرجّع بيانات وهمية ولا يوقّع الطلبات إطلاقاً — أي أنه ما كان
يتصل ببينانس أصلاً. هذا الملف يتصل فعلاً.
"""

import hmac, hashlib, json, time
import urllib.request, urllib.parse, urllib.error

TESTNET = "https://testnet.binance.vision"
MAINNET = "https://api.binance.com"


class BinanceError(Exception):
    pass


class BinanceClient:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = True,
                 recv_window: int = 5000):
        if not api_key or not api_secret:
            raise BinanceError("مفاتيح API مفقودة")
        self.key = api_key.strip()
        self.secret = api_secret.strip().encode()
        self.base = TESTNET if testnet else MAINNET
        self.testnet = testnet
        self.recv_window = recv_window
        self._offset = 0
        self._filters = {}

    # ── الطبقة الدنيا ──
    def _sign(self, params: dict) -> str:
        qs = urllib.parse.urlencode(params)
        sig = hmac.new(self.secret, qs.encode(), hashlib.sha256).hexdigest()
        return f"{qs}&signature={sig}"

    def _request(self, method, path, params=None, signed=False):
        params = dict(params or {})
        if signed:
            params['timestamp'] = int(time.time() * 1000) + self._offset
            params['recvWindow'] = self.recv_window
            body = self._sign(params)
        else:
            body = urllib.parse.urlencode(params)

        url = f"{self.base}{path}"
        headers = {'X-MBX-APIKEY': self.key, 'User-Agent': 'Mozilla/5.0'}

        if method == 'GET':
            url = f"{url}?{body}" if body else url
            req = urllib.request.Request(url, headers=headers, method='GET')
        else:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
            req = urllib.request.Request(url, data=body.encode(),
                                         headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                j = json.loads(raw)
                raise BinanceError(f"[{j.get('code')}] {j.get('msg')}") from None
            except json.JSONDecodeError:
                raise BinanceError(f"HTTP {e.code}: {raw[:200]}") from None
        except Exception as e:
            raise BinanceError(f"{type(e).__name__}: {e}") from None

    # ── عام ──
    def ping(self):
        self._request('GET', '/api/v3/ping')
        return True

    def sync_time(self):
        """مزامنة الساعة — أهم خطوة، أغلب أخطاء -1021 سببها فرق التوقيت"""
        srv = self._request('GET', '/api/v3/time')['serverTime']
        self._offset = srv - int(time.time() * 1000)
        return self._offset

    def price(self, symbol):
        r = self._request('GET', '/api/v3/ticker/price', {'symbol': symbol})
        return float(r['price'])

    def klines(self, symbol, interval, limit=500):
        return self._request('GET', '/api/v3/klines',
                             {'symbol': symbol, 'interval': interval, 'limit': limit})

    def symbol_filters(self, symbol):
        """
        قيود الرمز: أصغر كمية، خطوة الكمية، أصغر قيمة أمر.
        تجاهلها = رفض الأوامر برسالة LOT_SIZE أو NOTIONAL.
        """
        if symbol in self._filters:
            return self._filters[symbol]
        info = self._request('GET', '/api/v3/exchangeInfo', {'symbol': symbol})
        s = info['symbols'][0]
        out = {'base_precision': s['baseAssetPrecision'],
               'base': s['baseAsset'], 'quote': s['quoteAsset']}
        for f in s['filters']:
            if f['filterType'] == 'LOT_SIZE':
                out['step'] = float(f['stepSize'])
                out['min_qty'] = float(f['minQty'])
            elif f['filterType'] == 'PRICE_FILTER':
                out['tick'] = float(f['tickSize'])
            elif f['filterType'] in ('MIN_NOTIONAL', 'NOTIONAL'):
                out['min_notional'] = float(f.get('minNotional', 10))
        self._filters[symbol] = out
        return out

    def round_qty(self, symbol, qty):
        """تقريب الكمية لخطوة الرمز — إلزامي"""
        f = self.symbol_filters(symbol)
        step = f.get('step', 0.00001)
        rounded = int(qty / step) * step
        decimals = max(0, len(f"{step:.10f}".rstrip('0').split('.')[-1]))
        return float(f"{rounded:.{decimals}f}")

    def validate_order(self, symbol, qty, price):
        """فحص الأمر قبل الإرسال"""
        f = self.symbol_filters(symbol)
        q = self.round_qty(symbol, qty)
        if q < f.get('min_qty', 0):
            return False, f"الكمية {q} أقل من الحد {f.get('min_qty')}", q
        notional = q * price
        if notional < f.get('min_notional', 10):
            return False, f"قيمة الأمر ${notional:.2f} أقل من الحد ${f.get('min_notional')}", q
        return True, 'ok', q

    # ── موقّع ──
    def account(self):
        return self._request('GET', '/api/v3/account', signed=True)

    def balances(self, nonzero=True):
        acc = self.account()
        out = {}
        for b in acc['balances']:
            free, locked = float(b['free']), float(b['locked'])
            if not nonzero or free + locked > 0:
                out[b['asset']] = {'free': free, 'locked': locked}
        return out

    def can_trade(self):
        return self.account().get('canTrade', False)

    def market_buy(self, symbol, quote_amount):
        """شراء بمبلغ محدد من العملة المقابلة (مثلاً 50 USDT)"""
        return self._request('POST', '/api/v3/order', {
            'symbol': symbol, 'side': 'BUY', 'type': 'MARKET',
            'quoteOrderQty': round(quote_amount, 2)}, signed=True)

    def market_sell(self, symbol, qty):
        ok, msg, q = self.validate_order(symbol, qty, self.price(symbol))
        if not ok:
            raise BinanceError(msg)
        return self._request('POST', '/api/v3/order', {
            'symbol': symbol, 'side': 'SELL', 'type': 'MARKET',
            'quantity': q}, signed=True)

    def limit_order(self, symbol, side, qty, price):
        f = self.symbol_filters(symbol)
        tick = f.get('tick', 0.01)
        px = round(int(price / tick) * tick, 8)
        ok, msg, q = self.validate_order(symbol, qty, px)
        if not ok:
            raise BinanceError(msg)
        return self._request('POST', '/api/v3/order', {
            'symbol': symbol, 'side': side, 'type': 'LIMIT',
            'timeInForce': 'GTC', 'quantity': q, 'price': f"{px}"}, signed=True)

    def stop_loss_limit(self, symbol, qty, stop_price, limit_price):
        """وقف خسارة فعلي على المنصة — يعمل حتى لو انطفأ جهازك"""
        f = self.symbol_filters(symbol)
        tick = f.get('tick', 0.01)
        sp = round(int(stop_price / tick) * tick, 8)
        lp = round(int(limit_price / tick) * tick, 8)
        ok, msg, q = self.validate_order(symbol, qty, lp)
        if not ok:
            raise BinanceError(msg)
        return self._request('POST', '/api/v3/order', {
            'symbol': symbol, 'side': 'SELL', 'type': 'STOP_LOSS_LIMIT',
            'timeInForce': 'GTC', 'quantity': q,
            'stopPrice': f"{sp}", 'price': f"{lp}"}, signed=True)

    def open_orders(self, symbol=None):
        return self._request('GET', '/api/v3/openOrders',
                             {'symbol': symbol} if symbol else {}, signed=True)

    def cancel(self, symbol, order_id):
        return self._request('DELETE', '/api/v3/order',
                             {'symbol': symbol, 'orderId': order_id}, signed=True)

    def cancel_all(self, symbol):
        out = []
        for o in self.open_orders(symbol):
            try:
                out.append(self.cancel(symbol, o['orderId']))
            except BinanceError:
                pass
        return out

    def my_trades(self, symbol, limit=50):
        return self._request('GET', '/api/v3/myTrades',
                             {'symbol': symbol, 'limit': limit}, signed=True)


def diagnose(api_key, api_secret, testnet=True):
    """فحص شامل قبل التشغيل"""
    print(f"\n{'='*54}")
    print(f"  فحص الاتصال — {'TESTNET' if testnet else '🔴 MAINNET حقيقي'}")
    print('='*54)
    try:
        c = BinanceClient(api_key, api_secret, testnet)
        c.ping();                     print("✅ الوصول للخادم")
        off = c.sync_time();          print(f"✅ مزامنة الوقت (فرق {off}ms)")
        print(f"✅ صلاحية التداول: {'نعم' if c.can_trade() else '❌ لا'}")
        bals = c.balances()
        print("✅ الأرصدة:")
        for a, b in list(bals.items())[:6]:
            print(f"     {a}: {b['free']:.8f}")
        if not bals:
            print("     (فارغ)")
        return c
    except BinanceError as e:
        print(f"❌ {e}\n")
        m = str(e)
        if '-2015' in m:
            print("   السبب: مفتاح خاطئ، أو IP غير مسموح، أو صلاحيات ناقصة")
        elif '-1021' in m:
            print("   السبب: ساعة جهازك غير متزامنة — صحّح وقت النظام")
        elif '-2014' in m:
            print("   السبب: صيغة المفتاح خاطئة")
        elif '403' in m:
            print("   السبب: حجب جغرافي — تحتاج VPN")
        return None


if __name__ == '__main__':
    import os, sys
    k = os.getenv('BINANCE_API_KEY', '')
    s = os.getenv('BINANCE_API_SECRET', '')
    t = os.getenv('USE_TESTNET', 'True').lower() != 'false'
    if not k:
        print("ضع المفاتيح في .env أو متغيرات البيئة أولاً")
        sys.exit(1)
    diagnose(k, s, t)
