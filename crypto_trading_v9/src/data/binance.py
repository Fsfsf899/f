"""
عميل بينانس العام — بيانات سوق حقيقية فقط.
لا يولّد أي بيانات. عند الفشل يرمي استثناءً ولا يعيد شيئاً مصطنعاً.
"""
import json, time
import urllib.request, urllib.parse, urllib.error
import numpy as np
from typing import Optional, List, Dict
from .types import OHLCV, INTERVAL_MS

HOSTS = ["https://api.binance.com", "https://api1.binance.com",
         "https://api2.binance.com", "https://data-api.binance.vision"]
MAX_LIMIT = 1000


class DataUnavailable(Exception):
    """لا يمكن الحصول على بيانات حقيقية. لا بديل مصطنع."""


class BinancePublic:
    def __init__(self, hosts: Optional[List[str]] = None, timeout: int = 20,
                 sleep_between: float = 0.12):
        self.hosts = list(hosts or HOSTS)
        self.timeout = timeout
        self.sleep_between = sleep_between
        self._host_i = 0
        self._server_offset_ms = 0
        self._exchange_info: Dict[str, Dict] = {}

    def _get(self, path: str, params: Optional[Dict] = None):
        qs = ('?' + urllib.parse.urlencode(params)) if params else ''
        last = None
        for attempt in range(len(self.hosts) * 2):
            host = self.hosts[self._host_i % len(self.hosts)]
            req = urllib.request.Request(host + path + qs,
                                         headers={'User-Agent': 'Mozilla/5.0'})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                body = e.read().decode()[:200]
                last = f"HTTP {e.code} @ {host}: {body}"
                if e.code == 429:            # rate limit
                    time.sleep(2.0)
                self._host_i += 1
            except Exception as e:
                last = f"{type(e).__name__} @ {host}: {e}"
                self._host_i += 1
        raise DataUnavailable(last or "كل الخوادم فشلت")

    # ── وقت الخادم ──
    def sync_time(self) -> int:
        srv = self._get('/api/v3/time')['serverTime']
        self._server_offset_ms = srv - int(time.time() * 1000)
        return self._server_offset_ms

    def now_ms(self) -> int:
        return int(time.time() * 1000) + self._server_offset_ms

    # ── قواعد التداول ──
    def exchange_rules(self, symbol: str) -> Dict:
        if symbol in self._exchange_info:
            return self._exchange_info[symbol]
        info = self._get('/api/v3/exchangeInfo', {'symbol': symbol})
        s = info['symbols'][0]
        out = {'symbol': s['symbol'], 'status': s['status'],
               'base': s['baseAsset'], 'quote': s['quoteAsset'],
               'spot_allowed': s.get('isSpotTradingAllowed', False)}
        for f in s['filters']:
            t = f['filterType']
            if t == 'LOT_SIZE':
                out['step_size'] = float(f['stepSize'])
                out['min_qty'] = float(f['minQty'])
                out['max_qty'] = float(f['maxQty'])
            elif t == 'PRICE_FILTER':
                out['tick_size'] = float(f['tickSize'])
            elif t in ('MIN_NOTIONAL', 'NOTIONAL'):
                out['min_notional'] = float(f.get('minNotional', 10))
        self._exchange_info[symbol] = out
        return out

    # ── تيكر ──
    def ticker(self, symbol: str) -> Dict:
        b = self._get('/api/v3/ticker/bookTicker', {'symbol': symbol})
        bid, ask = float(b['bidPrice']), float(b['askPrice'])
        mid = (bid + ask) / 2
        return {'symbol': symbol, 'bid': bid, 'ask': ask, 'mid': mid,
                'bid_qty': float(b['bidQty']), 'ask_qty': float(b['askQty']),
                'spread_bps': (ask - bid) / mid * 10000 if mid > 0 else float('inf'),
                'ts': self.now_ms()}

    def order_book(self, symbol: str, limit: int = 100) -> Dict:
        """دفتر أوامر حقيقي. البند 20."""
        d = self._get('/api/v3/depth', {'symbol': symbol, 'limit': limit})
        bids = np.array([[float(p), float(q)] for p, q in d['bids']])
        asks = np.array([[float(p), float(q)] for p, q in d['asks']])
        return {'symbol': symbol, 'bids': bids, 'asks': asks, 'ts': self.now_ms()}

    # ── الشموع ──
    def klines(self, symbol: str, interval: str,
               start_ms: Optional[int] = None, end_ms: Optional[int] = None,
               limit: Optional[int] = None, verbose: bool = False) -> OHLCV:
        """
        يجلب الشموع في نطاق زمني. يُرجع الشموع المغلقة فقط.
        """
        if interval not in INTERVAL_MS:
            raise ValueError(f"إطار غير مدعوم: {interval}")
        step = INTERVAL_MS[interval]
        now = self.now_ms()
        end_ms = min(end_ms or now, now)
        if start_ms is None:
            start_ms = end_ms - (limit or 500) * step

        rows: List[list] = []
        cursor = start_ms
        while cursor < end_ms:
            batch = self._get('/api/v3/klines', {
                'symbol': symbol, 'interval': interval,
                'startTime': int(cursor), 'endTime': int(end_ms),
                'limit': MAX_LIMIT})
            if not batch:
                break
            rows.extend(batch)
            nxt = int(batch[-1][0]) + step
            if nxt <= cursor:
                break
            cursor = nxt
            if verbose:
                print(f"   … {len(rows)} شمعة")
            if len(batch) < MAX_LIMIT:
                break
            time.sleep(self.sleep_between)

        if not rows:
            raise DataUnavailable(f"لا توجد بيانات لـ {symbol} {interval}")

        arr = np.array(rows, dtype=object)
        ot = arr[:, 0].astype(np.int64)
        data = OHLCV(symbol, interval, ot,
                     arr[:, 1].astype(float), arr[:, 2].astype(float),
                     arr[:, 3].astype(float), arr[:, 4].astype(float),
                     arr[:, 5].astype(float), source="binance",
                     fetched_at=now)

        from .validation import drop_unclosed, dedupe_and_sort
        return drop_unclosed(dedupe_and_sort(data), now_ms=now)

    def ping(self) -> bool:
        self._get('/api/v3/ping')
        return True
