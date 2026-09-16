"""
وسيط ورقي محافظ — المرحلة الثالثة.
==================================
ليس Fake Exchange بسيطاً. محاكاة **متحيّزة ضد المتداول** عمداً:

    شراء سوق  = ask + انزلاق
    بيع سوق   = bid − انزلاق
    وقف خسارة = أسوأ سعر عند وجود فجوة
    الرسوم    = تُخصم من كل تنفيذ

لا يُنفَّذ أي أمر بسعر الإغلاق المثالي. السبب: محاكاة متفائلة تُنتج
نتائج ورقية جميلة تنهار على المنصة، وهذا أسوأ من عدم الاختبار أصلاً.

يطابق واجهة BinanceClient بالكامل، فنفس OrderManager و Gate و
Reconciler تعمل فوقه بلا تعديل — أي مسار خاص بالورقي يعني اختبار
شيء آخر غير ما سيُشغَّل.

كل معاملات التكلفة تُحفظ **مع كل تنفيذ** في قاعدة البيانات لتمكين
إعادة التحليل لاحقاً.
"""
import json
import random
import time
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional
from .binance_client import BinanceError

PAPER_QUOTE = 'USDT'


@dataclass
class PaperCosts:
    """نموذج التكلفة. يُحفظ مع كل صفقة."""
    maker_fee: float = 0.0010
    taker_fee: float = 0.0010
    spread_bps: float = 4.0
    normal_slippage_bps: float = 4.0
    stop_slippage_bps: float = 15.0      # الوقف أسوأ: السوق يتحرك ضدك
    gap_slippage_bps: float = 60.0       # الفجوة أسوأ بكثير
    latency_ms: int = 250
    partial_fill_ratio: float = 0.0      # 0 = بلا تنفيذ جزئي
    reject_probability: float = 0.0
    timeout_probability: float = 0.0
    rate_limit_probability: float = 0.0
    disconnect_after_accept_probability: float = 0.0

    def to_dict(self) -> Dict: return asdict(self)


class PaperMarketProvider:
    """
    مزوّد سوق محلي حتمي بالكامل لوضع Paper — القسم A من متطلبات V11.

    **بلا أي اتصال شبكة إطلاقاً.** يُستبدَل به `BinancePublic` الحقيقي
    كعميل بيانات عامة لـ `PaperBroker` في وضع Paper. يُغذَّى صراحة من
    نفس بيانات الشمعة المستخدَمة لتقييم الإشارة في الدورة نفسها
    (`LiveTrader.tick()`) — لا استعلام سعر منفصل قد يفشل أو يتأخر أو
    يتباين عن السعر الذي اتُّخذ القرار على أساسه.

    اكتُشف أثناء تدقيق V11: `LiveTrader` كان يُمرِّر `BinancePublic()`
    الحقيقية مباشرة لـ `PaperBroker` — أي دورة Paper كانت تستطيع فعلياً
    لمس الشبكة عبر `_book()`/`rules()`. في بيئة تفشل فيها الشبكة فوراً
    (كهذه) يظهر ذلك كرسالة تحذير فقط؛ في بيئة بشروط شبكة مختلفة
    (Timeout بطيء بدل رفض فوري) قد يُسبِّب تعليقاً فعلياً — وهذا بالضبط
    الشرح الجذري لتقرير تعليق `test_paper_does_not_duplicate_order`.
    """

    def __init__(self):
        self._prices: Dict[str, Dict[str, float]] = {}
        self._rules_cache: Dict[str, Dict] = {}

    def set_price(self, symbol: str, mid: float, spread_bps: float = 4.0):
        if mid is None or mid <= 0:
            raise ValueError(f'سعر محلي غير صالح لـ {symbol}: {mid}')
        h = spread_bps / 20000
        self._prices[symbol] = {'bid': mid * (1 - h), 'ask': mid * (1 + h),
                                'mid': float(mid)}

    def set_rules(self, symbol: str, rules: Dict):
        self._rules_cache[symbol] = dict(rules)

    def ticker(self, symbol: str) -> Dict:
        if symbol not in self._prices:
            # فشل فوري محلي — لا انتظار شبكة، لا Timeout بطيء يُشبه
            # التعليق. PaperBroker.price()/_book() تتوقع استثناء عند
            # غياب السعر وتتعامل معه بأمان (لا محاولة شبكة ثانية).
            raise BinanceError(
                f'لا سعر محلي مُحدَّث لـ {symbol} في هذه الدورة — '
                f'PaperMarketProvider بلا اتصال شبكة بالتصميم')
        return dict(self._prices[symbol])

    def exchange_rules(self, symbol: str) -> Dict:
        if symbol not in self._rules_cache:
            raise BinanceError(
                f'لا قواعد محلية مُحدَّثة لـ {symbol} — '
                f'PaperMarketProvider بلا اتصال شبكة بالتصميم')
        return dict(self._rules_cache[symbol])


class PaperBroker:
    def __init__(self, db, public_client, costs: Optional[PaperCosts] = None,
                 starting_quote: float = 1000.0, seed: Optional[int] = None,
                 env_tag: str = '[PAPER]'):
        self.db = db
        self.public = public_client
        self.costs = costs or PaperCosts()
        self.testnet = True                 # لا mainnet أبداً
        self.env_tag = env_tag
        self._rng = random.Random(seed if seed is not None else 0xC0FFEE)
        self._rules: Dict[str, Dict] = {}
        self._orders: Dict[str, Dict] = {}
        self._next_id = 1
        self._last_price: Dict[str, float] = {}
        self.api_failures = 0
        self._load()
        if self.db.get_kv('paper_balances') is None:
            self.db.set_kv('paper_balances', {PAPER_QUOTE: starting_quote})
            self.db.set_kv('paper_costs', self.costs.to_dict())
            self.db.set_kv('paper_started_ts', int(time.time() * 1000))

    # ── الاستمرارية عبر إعادة التشغيل ──
    def _load(self):
        self._orders = self.db.get_kv('paper_orders', {}) or {}
        self._next_id = int(self.db.get_kv('paper_next_id', 1) or 1)
        self._last_price = self.db.get_kv('paper_last_price', {}) or {}

    def _save(self):
        self.db.set_kv('paper_orders', self._orders)
        self.db.set_kv('paper_next_id', self._next_id)
        self.db.set_kv('paper_last_price', self._last_price)

    def _bal(self) -> Dict[str, Dict[str, float]]:
        raw = self.db.get_kv('paper_balances', {PAPER_QUOTE: 0.0}) or {}
        out = {}
        for a, v in raw.items():
            out[a] = v if isinstance(v, dict) else {'free': float(v), 'locked': 0.0}
        return out

    def _set_bal(self, b: Dict[str, Dict[str, float]]):
        self.db.set_kv('paper_balances',
                       {a: {'free': round(x['free'], 12),
                            'locked': round(x['locked'], 12)}
                        for a, x in b.items()})

    @staticmethod
    def _ensure(b: Dict, asset: str) -> Dict:
        b.setdefault(asset, {'free': 0.0, 'locked': 0.0})
        return b[asset]

    # ── محاكاة الأعطال ──
    def _simulate_faults(self, stage: str):
        c = self.costs
        if c.latency_ms:
            time.sleep(min(c.latency_ms, 50) / 1000.0)
        r = self._rng.random
        if stage == 'pre':
            if r() < c.rate_limit_probability:
                raise BinanceError('[-1003] TOO_MANY_REQUESTS', -1003)
            if r() < c.reject_probability:
                raise BinanceError('[-2010] Order would trigger immediately.', -2010)
            if r() < c.timeout_probability:
                raise BinanceError('Read timed out')

    # ── أسعار bid/ask واقعية ──
    def _book(self, symbol: str) -> Dict[str, float]:
        try:
            t = self.public.ticker(symbol)
            bid, ask = float(t['bid']), float(t['ask'])
            if bid <= 0 or ask <= 0 or ask < bid:
                raise ValueError
        except Exception:
            try:
                mid = float(self.public.ticker(symbol)['mid'])
            except Exception as e:
                self.api_failures += 1
                raise BinanceError(f"سعر غير متاح: {e}")
            half = self.costs.spread_bps / 20000
            bid, ask = mid * (1 - half), mid * (1 + half)
        mid = (bid + ask) / 2
        prev = self._last_price.get(symbol)
        self._last_price[symbol] = mid
        gap = abs(mid - prev) / prev if prev else 0.0
        return {'bid': bid, 'ask': ask, 'mid': mid, 'gap': gap}

    # ── واجهة العميل ──
    def sync_time(self): return 0
    def maybe_resync(self): return False
    def now_ms(self): return int(time.time() * 1000)
    def ping(self): return True
    def can_trade(self): return True

    def account(self) -> Dict:
        return {'canTrade': True, 'canWithdraw': False, 'balances': [
            {'asset': a, 'free': str(v['free']), 'locked': str(v['locked'])}
            for a, v in self._bal().items()]}

    def balances(self) -> Dict[str, Dict]:
        return {a: dict(v) for a, v in self._bal().items()
                if v['free'] + v['locked'] > 0}

    def price(self, symbol: str) -> float:
        return self._book(symbol)['mid']

    def base_free(self, symbol: str) -> float:
        return self._bal().get(self.rules(symbol)['base'], {}).get('free', 0.0)

    def rules(self, symbol: str) -> Dict:
        if symbol in self._rules:
            return self._rules[symbol]
        try:
            r = dict(self.public.exchange_rules(symbol))
        except Exception:
            r = {'symbol': symbol, 'status': 'TRADING',
                 'base': symbol.replace(PAPER_QUOTE, '') or 'BASE',
                 'quote': PAPER_QUOTE, 'step_size': 1e-6, 'min_qty': 1e-6,
                 'tick_size': 0.01, 'min_notional': 10.0}
        r.setdefault('spot', True); r.setdefault('step_size', 1e-6)
        r.setdefault('min_qty', 1e-6); r.setdefault('tick_size', 0.01)
        r.setdefault('min_notional', 10.0)
        self._rules[symbol] = r
        return r

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

    def round_qty(self, symbol, qty): return self._round_step(qty, self.rules(symbol)['step_size'])
    def round_price(self, symbol, px): return self._round_step(px, self.rules(symbol)['tick_size'])

    # ── إنشاء الأوامر ──
    def _new(self, cid, symbol, side, otype, qty, price, status, **extra) -> Dict:
        oid = self._next_id; self._next_id += 1
        o = {'orderId': oid, 'clientOrderId': cid, 'symbol': symbol, 'side': side,
             'type': otype, 'status': status, 'origQty': f'{qty:.10f}',
             'executedQty': '0', 'cummulativeQuoteQty': '0',
             'price': f'{price:.10f}', 'transactTime': self.now_ms(),
             'environment': self.env_tag, 'costs': self.costs.to_dict()}
        o.update(extra)
        self._orders[cid] = o
        return o

    def _execute(self, o: Dict, price: float, qty: float,
                 reason: str = 'normal') -> Dict:
        """ينفّذ كمية عند سعر، مع الرسوم. يدعم التنفيذ الجزئي."""
        symbol = o['symbol']; r = self.rules(symbol)
        b = self._bal()
        fee_rate = self.costs.taker_fee if o['type'] == 'MARKET' else self.costs.maker_fee

        if o['side'] == 'BUY':
            cost = qty * price; fee = cost * fee_rate
            q = self._ensure(b, r['quote'])
            if q['free'] + 1e-9 < cost + fee:
                o['status'] = 'REJECTED'; self._save()
                raise BinanceError('[-2010] Account has insufficient balance.', -2010)
            q['free'] -= cost + fee
            self._ensure(b, r['base'])['free'] += qty
        else:
            base = self._ensure(b, r['base'])
            avail = base['free'] + base['locked']
            if avail + 1e-9 < qty:
                o['status'] = 'REJECTED'; self._save()
                raise BinanceError('[-2010] Account has insufficient balance.', -2010)
            take_locked = min(base['locked'], qty)
            base['locked'] -= take_locked
            base['free'] -= (qty - take_locked)
            proceeds = qty * price; fee = proceeds * fee_rate
            self._ensure(b, r['quote'])['free'] += proceeds - fee
        self._set_bal(b)

        prev_q = float(o['executedQty']); prev_quote = float(o['cummulativeQuoteQty'])
        o['executedQty'] = f'{prev_q + qty:.10f}'
        o['cummulativeQuoteQty'] = f'{prev_quote + qty * price:.10f}'
        o['status'] = ('FILLED' if abs(float(o['executedQty']) - float(o['origQty'])) < 1e-9
                       else 'PARTIALLY_FILLED')
        o.setdefault('fills', []).append({
            'qty': f'{qty:.10f}', 'price': f'{price:.10f}',
            'commission': f'{qty * price * fee_rate:.10f}',
            'commissionAsset': r['quote'],
            'tradeId': f"paper-{o['orderId']}-{len(o.get('fills', []))}",
            'fill_reason': reason})
        self._save()
        return o

    def _slip(self, bps: float) -> float:
        return bps / 10000.0

    def market_buy_quote(self, symbol: str, quote_amount: float, cid: str) -> Dict:
        if cid in self._orders:
            raise BinanceError('[-2010] Duplicate order sent.', -2010)
        self._simulate_faults('pre')
        bk = self._book(symbol)
        px = bk['ask'] * (1 + self._slip(self.costs.normal_slippage_bps))
        qty = self.round_qty(symbol, quote_amount / px)
        r = self.rules(symbol)
        if qty < r['min_qty'] or qty * px < r['min_notional']:
            raise BinanceError('[-1013] Filter failure: LOT_SIZE/NOTIONAL', -1013)
        o = self._new(cid, symbol, 'BUY', 'MARKET', qty, px, 'NEW',
                      ref_ask=bk['ask'], ref_bid=bk['bid'])
        fill_qty = qty
        if self.costs.partial_fill_ratio > 0:
            fill_qty = self.round_qty(symbol, qty * self.costs.partial_fill_ratio)
            if fill_qty <= 0:
                fill_qty = qty
        self._execute(o, px, fill_qty)
        if self._rng.random() < self.costs.disconnect_after_accept_probability:
            raise BinanceError('Connection reset after accept')
        return o

    def market_sell(self, symbol: str, qty: float, cid: str) -> Dict:
        if cid in self._orders:
            raise BinanceError('[-2010] Duplicate order sent.', -2010)
        self._simulate_faults('pre')
        bk = self._book(symbol)
        px = bk['bid'] * (1 - self._slip(self.costs.normal_slippage_bps))
        q = self.round_qty(symbol, qty)
        o = self._new(cid, symbol, 'SELL', 'MARKET', q, px, 'NEW',
                      ref_ask=bk['ask'], ref_bid=bk['bid'])
        self._execute(o, px, q)
        if self._rng.random() < self.costs.disconnect_after_accept_probability:
            raise BinanceError('Connection reset after accept')
        return o

    def limit_sell(self, symbol: str, qty: float, price: float, cid: str) -> Dict:
        if cid in self._orders:
            raise BinanceError('[-2010] Duplicate order sent.', -2010)
        self._simulate_faults('pre')
        q = self.round_qty(symbol, qty)
        b = self._bal(); base = self._ensure(b, self.rules(symbol)['base'])
        if base['free'] + 1e-9 < q:
            raise BinanceError('[-2010] Account has insufficient balance.', -2010)
        base['free'] -= q; base['locked'] += q      # الحجز كما في المنصة
        self._set_bal(b)
        o = self._new(cid, symbol, 'SELL', 'LIMIT', q,
                      self.round_price(symbol, price), 'NEW')
        self._save()
        return o

    def stop_loss_limit(self, symbol: str, qty: float, stop_price: float,
                        limit_price: float, cid: str) -> Dict:
        if cid in self._orders:
            raise BinanceError('[-2010] Duplicate order sent.', -2010)
        self._simulate_faults('pre')
        q = self.round_qty(symbol, qty)
        b = self._bal(); base = self._ensure(b, self.rules(symbol)['base'])
        if base['free'] + 1e-9 < q:
            raise BinanceError('[-2010] Account has insufficient balance.', -2010)
        base['free'] -= q; base['locked'] += q
        self._set_bal(b)
        o = self._new(cid, symbol, 'SELL', 'STOP_LOSS_LIMIT', q,
                      self.round_price(symbol, limit_price), 'NEW',
                      stopPrice=f'{self.round_price(symbol, stop_price):.10f}')
        self._save()
        return o

    def oco_sell(self, symbol: str, qty: float, target_price: float,
                 stop_price: float, stop_limit_price: float,
                 client_id: str, *, above_client_id: Optional[str] = None,
                 below_client_id: Optional[str] = None) -> Dict:
        """
        OCO ورقي واقعي — أب (orderListId) وابنان مستقلان بمعرّفات
        Order/Client خاصة بكل منهما، مطابق لشكل استجابة بينانس
        الحقيقية عبر `/api/v3/orderList/oco` (orderReports). كانت
        النسخة السابقة تُنشئ أمراً واحداً بمعرّف واحد يخدم الوقف
        والهدف معاً — يمنع التمييز بين الأمرين الفرعيين تماماً كما في
        العميل الحقيقي.

        `above_client_id`/`below_client_id`: إن مُرِّرا (كما تفعل
        `OrderManager._place_oco()` الآن)، يُستخدَمان حرفياً — لا
        تخمين لاحقة داخلي. "above"=الهدف (فوق السعر)، "below"=الوقف
        (تحته)، مطابقاً لتسمية بينانس الحالية.
        """
        stop_cid = below_client_id or f'{client_id}-STOP'
        target_cid = above_client_id or f'{client_id}-TARGET'
        if stop_cid in self._orders or target_cid in self._orders:
            # الفحص القديم كان يقارن client_id (معرّف القائمة) بمفاتيح
            # self._orders — لكن المفاتيح دائماً {cid}-STOP/{cid}-TARGET
            # (أو المعرّفين الصريحين)، لا client_id نفسه أبداً. الفحص
            # كان كوداً ميتاً تماماً؛ التكرار كان يُرفَض صدفة أحياناً
            # فقط عبر خطأ رصيد غير مرتبط لا حارساً موثوقاً صريحاً.
            raise BinanceError('[-2010] Duplicate order sent.', -2010)
        self._simulate_faults('pre')
        q = self.round_qty(symbol, qty)
        b = self._bal(); base = self._ensure(b, self.rules(symbol)['base'])
        if base['free'] + 1e-9 < q:
            raise BinanceError('[-2010] Account has insufficient balance.', -2010)
        base['free'] -= q; base['locked'] += q      # حجز واحد يخدم الابنين معاً
        self._set_bal(b)

        list_id = self._next_id; self._next_id += 1
        sp = self.round_price(symbol, stop_price)
        slp = self.round_price(symbol, stop_limit_price)
        tp = self.round_price(symbol, target_price)

        stop_o = self._new(stop_cid, symbol, 'SELL', 'STOP_LOSS_LIMIT', q, slp, 'NEW',
                           stopPrice=f'{sp:.10f}', orderListId=list_id,
                           listClientOrderId=client_id, sibling_cid=target_cid)
        target_o = self._new(target_cid, symbol, 'SELL', 'LIMIT_MAKER', q, tp, 'NEW',
                             orderListId=list_id, listClientOrderId=client_id,
                             sibling_cid=stop_cid)
        self._save()
        return {
            'orderListId': list_id, 'listClientOrderId': client_id,
            'listStatusType': 'EXEC_STARTED', 'listOrderStatus': 'EXECUTING',
            'symbol': symbol,
            'orders': [
                {'symbol': symbol, 'orderId': stop_o['orderId'],
                 'clientOrderId': stop_cid},
                {'symbol': symbol, 'orderId': target_o['orderId'],
                 'clientOrderId': target_cid}],
            'orderReports': [dict(stop_o), dict(target_o)],
        }

    # ── الاستعلام ──
    def order_by_client_id(self, symbol: str, cid: str) -> Dict:
        o = self._orders.get(cid)
        if o is None:
            raise BinanceError('[-2013] Order does not exist.', -2013)
        return o

    def query_oco(self, *, order_list_id: Optional[str] = None,
                  list_client_order_id: Optional[str] = None) -> Dict:
        """
        يُحاكي `GET /api/v3/orderList` — يبحث بين الأوامر المخزَّنة عن
        طرفَي OCO المرتبطين بنفس القائمة، ويُعيد نفس شكل استجابة
        بينانس الحقيقية. يُستخدَم في اختبار مسار التعافي
        (`IdempotentOrderGate._try_recover_oco`) دون شبكة حقيقية.
        """
        if not order_list_id and not list_client_order_id:
            raise ValueError('يلزم order_list_id أو list_client_order_id')
        matches = [o for o in self._orders.values()
                  if (order_list_id and str(o.get('orderListId', '')) == str(order_list_id))
                  or (list_client_order_id
                      and o.get('listClientOrderId') == list_client_order_id)]
        if not matches:
            raise BinanceError('[-2013] Order does not exist.', -2013)
        list_id = matches[0].get('orderListId')
        list_cid = matches[0].get('listClientOrderId')
        all_done = all(o['status'] in ('FILLED', 'CANCELED') for o in matches)
        return {
            'orderListId': list_id, 'listClientOrderId': list_cid,
            'listStatusType': 'ALL_DONE' if all_done else 'EXEC_STARTED',
            'listOrderStatus': 'ALL_DONE' if all_done else 'EXECUTING',
            'symbol': matches[0]['symbol'],
            'orders': [{'symbol': o['symbol'], 'orderId': o['orderId'],
                       'clientOrderId': cid} for cid, o in self._orders.items()
                      if o in matches],
            'orderReports': [dict(o) for o in matches],
        }

    def order_status(self, symbol: str, order_id) -> Dict:
        for o in self._orders.values():
            if str(o['orderId']) == str(order_id):
                return o
        raise BinanceError('[-2013] Order does not exist.', -2013)

    def open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        return [o for o in self._orders.values()
                if o['status'] in ('NEW', 'PARTIALLY_FILLED')
                and (symbol is None or o['symbol'] == symbol)]

    def cancel(self, symbol: str, order_id) -> Dict:
        for o in self._orders.values():
            if str(o['orderId']) == str(order_id):
                if o['status'] not in ('NEW', 'PARTIALLY_FILLED'):
                    raise BinanceError('[-2011] CANCEL_REJECTED', -2011)
                self._unlock(o)
                o['status'] = 'CANCELED'
                if o.get('sibling_cid'):
                    self._cancel_sibling(o)
                self._save(); return o
        raise BinanceError('[-2013] Order does not exist.', -2013)

    def _unlock(self, o: Dict):
        remaining = float(o['origQty']) - float(o['executedQty'])
        if remaining <= 0 or o['side'] != 'SELL':
            return
        b = self._bal(); base = self._ensure(b, self.rules(o['symbol'])['base'])
        move = min(base['locked'], remaining)
        base['locked'] -= move; base['free'] += move
        self._set_bal(b)

    def _cancel_sibling(self, executed: Dict):
        """
        عند تنفيذ أحد طرفي OCO، يُلغى الطرف الآخر تلقائياً — نفس سلوك
        بينانس الحقيقي. `_unlock` على الأخ آمنة رغم أن الرصيد المحجوز
        فُرِّغ بالفعل بتنفيذ الأول: `min(locked, remaining)` يُعيد صفراً
        عندها، لا تحريراً مزدوجاً.
        """
        sib_cid = executed.get('sibling_cid')
        if not sib_cid:
            return
        sib = self._orders.get(sib_cid)
        if sib is None or sib['status'] not in ('NEW', 'PARTIALLY_FILLED'):
            return
        self._unlock(sib)
        sib['status'] = 'CANCELED'
        self._save()

    def my_trades(self, symbol: str, limit: int = 100) -> List[Dict]:
        out = []
        for o in self._orders.values():
            if o['symbol'] != symbol:
                continue
            for f in o.get('fills', []):
                out.append({'id': f['tradeId'], 'qty': f['qty'], 'price': f['price'],
                            'time': o['transactTime'], 'commission': f['commission'],
                            'isBuyer': o['side'] == 'BUY'})
        return out[-limit:]

    # ── تقييم الأوامر المعلَّقة ──
    def evaluate_pending(self, symbol: str) -> List[Dict]:
        """
        تُستدعى كل نبضة. الوقف يُنفَّذ بسعر **أسوأ** عند الفجوة.

        قيد معلن: التقييم عند النبضة لا لحظياً — فرصة الوقف أفضل هنا
        مما هي على المنصة أثناء الحركات السريعة.
        """
        try:
            bk = self._book(symbol)
        except BinanceError:
            return []
        px, gap = bk['bid'], bk['gap']
        out = []
        for cid, o in list(self._orders.items()):
            if o['symbol'] != symbol or o['status'] not in ('NEW', 'PARTIALLY_FILLED'):
                continue
            remaining = float(o['origQty']) - float(o['executedQty'])
            if remaining <= 0:
                continue

            if o['type'] == 'STOP_LOSS_LIMIT' and px <= float(o['stopPrice']):
                stop = float(o['stopPrice'])
                # الفجوة تُنفَّذ عند السعر الحالي لا عند الوقف
                bps = (self.costs.gap_slippage_bps if gap > 0.005
                       else self.costs.stop_slippage_bps)
                fill_px = min(stop, px) * (1 - self._slip(bps))
                try:
                    self._execute(o, fill_px, remaining,
                                  reason='gap_stop' if gap > 0.005 else 'stop')
                    self._cancel_sibling(o)
                    out.append({'cid': cid, 'kind': 'STOP', 'price': fill_px,
                                'stop_price': stop, 'gap': round(gap, 5),
                                'slippage_bps': bps, 'oco': bool(o.get('sibling_cid'))})
                except BinanceError:
                    pass

            elif o['type'] == 'LIMIT_MAKER' and px >= float(o['price']):
                try:
                    self._execute(o, float(o['price']), remaining, reason='target')
                    self._cancel_sibling(o)
                    out.append({'cid': cid, 'kind': 'TARGET',
                                'price': float(o['price']), 'oco': True})
                except BinanceError:
                    pass

            elif o['type'] == 'LIMIT' and o['side'] == 'SELL' and px >= float(o['price']):
                try:
                    self._execute(o, float(o['price']), remaining, reason='target')
                    out.append({'cid': cid, 'kind': 'TARGET',
                                'price': float(o['price'])})
                except BinanceError:
                    pass
        return out

    def equity(self, symbols: List[str]) -> float:
        b = self._bal()
        eq = b.get(PAPER_QUOTE, {}).get('free', 0.0) + b.get(PAPER_QUOTE, {}).get('locked', 0.0)
        for s in symbols:
            base = self.rules(s)['base']
            held = b.get(base, {}).get('free', 0.0) + b.get(base, {}).get('locked', 0.0)
            if held > 0:
                try:
                    eq += held * self.price(s)
                except BinanceError:
                    pass
        return eq
