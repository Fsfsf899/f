"""
كاش يعرف حدوده — البند 10.
يخزّن الميتاداتا (النطاق، وقت الجلب، المصدر) ويرفض إرجاع
بيانات لا تغطي المطلوب. يجلب الجزء الناقص فقط.
"""
import json, os, time
from typing import Optional, Tuple
import numpy as np
from .types import OHLCV, INTERVAL_MS
from .binance import BinancePublic, DataUnavailable
from .validation import dedupe_and_sort, drop_unclosed
from ..core.config import DataConfig

DEFAULT_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), 'cache')


class Cache:
    def __init__(self, directory: str = DEFAULT_DIR, cfg: Optional[DataConfig] = None):
        self.dir = directory
        self.cfg = cfg or DataConfig()
        os.makedirs(self.dir, exist_ok=True)

    def _path(self, symbol: str, interval: str) -> str:
        return os.path.join(self.dir, f"{symbol}_{interval}.json")

    # ── قراءة ──
    def read(self, symbol: str, interval: str) -> Optional[Tuple[OHLCV, dict]]:
        p = self._path(symbol, interval)
        if not os.path.exists(p):
            return None
        try:
            with open(p) as f:
                blob = json.load(f)
            return OHLCV.from_dict(blob['data']), blob['meta']
        except Exception:
            return None

    # ── كتابة ذرّية (البند 32) ──
    def write(self, data: OHLCV):
        p = self._path(data.symbol, data.interval)
        meta = {
            'symbol': data.symbol, 'interval': data.interval,
            'first_timestamp': int(data.open_time[0]),
            'last_timestamp': int(data.open_time[-1]),
            'n_bars': len(data),
            'fetched_at': data.fetched_at or int(time.time() * 1000),
            'source': data.source,
        }
        tmp = p + '.tmp'
        with open(tmp, 'w') as f:
            json.dump({'meta': meta, 'data': data.to_dict()}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)     # ذرّي

    def covers(self, meta: dict, start_ms: int, end_ms: int, interval: str) -> bool:
        """هل الكاش يغطي النطاق المطلوب فعلاً؟"""
        step = INTERVAL_MS[interval]
        return (meta['first_timestamp'] <= start_ms + step and
                meta['last_timestamp'] >= end_ms - step * 2)

    # ── الواجهة الرئيسية ──
    def get(self, client: BinancePublic, symbol: str, interval: str,
            days: int = 365, force: bool = False, verbose: bool = True) -> OHLCV:
        """
        يُرجع بيانات تغطي آخر `days` يوماً.
        - يستخدم الكاش فقط إذا غطّى النطاق وكان طازجاً
        - يجلب الجزء الناقص فقط (لا يعيد تحميل كل شيء)
        - عند تعذّر الشبكة والكاش غير كافٍ: يرمي DataUnavailable
        """
        step = INTERVAL_MS[interval]
        try:
            now = client.now_ms()
        except Exception:
            now = int(time.time() * 1000)
        want_start = now - days * 86_400_000

        cached = None if force else self.read(symbol, interval)

        if cached:
            data, meta = cached
            age = now - meta.get('fetched_at', 0)
            covers = self.covers(meta, want_start, now, interval)
            fresh = age <= self.cfg.cache_ttl_seconds * 1000

            if covers and fresh:
                if verbose:
                    print(f"💾 كاش: {symbol} {interval} — {len(data)} شمعة "
                          f"(عمر {age/1000/60:.0f}د)")
                return data.slice(int(np.searchsorted(data.open_time, want_start)))

            # ── تحديث تزايدي: نجلب الناقص فقط
            if covers and not fresh:
                gap_start = int(meta['last_timestamp']) + step
                if verbose:
                    print(f"🔄 تحديث تزايدي من {gap_start}")
                try:
                    new = client.klines(symbol, interval, start_ms=gap_start, end_ms=now)
                    merged = self._merge(data, new)
                    self.write(merged)
                    return merged.slice(int(np.searchsorted(merged.open_time, want_start)))
                except DataUnavailable as e:
                    if verbose:
                        print(f"⚠️ تعذّر التحديث ({e}) — استخدام الكاش كما هو")
                    return data.slice(int(np.searchsorted(data.open_time, want_start)))

            if verbose:
                have_days = (meta['last_timestamp'] - meta['first_timestamp']) / 86_400_000
                print(f"⚠️ الكاش يغطي {have_days:.0f} يوم فقط والمطلوب {days} — إعادة جلب")

        # ── جلب كامل
        if verbose:
            print(f"📡 جلب {symbol} {interval} — {days} يوم من بينانس")
        data = client.klines(symbol, interval, start_ms=want_start, end_ms=now,
                             verbose=verbose)
        self.write(data)
        return data

    @staticmethod
    def _merge(old: OHLCV, new: OHLCV) -> OHLCV:
        merged = OHLCV(old.symbol, old.interval,
                       np.concatenate([old.open_time, new.open_time]),
                       np.concatenate([old.open, new.open]),
                       np.concatenate([old.high, new.high]),
                       np.concatenate([old.low, new.low]),
                       np.concatenate([old.close, new.close]),
                       np.concatenate([old.volume, new.volume]),
                       source=new.source, fetched_at=new.fetched_at)
        return dedupe_and_sort(merged)
