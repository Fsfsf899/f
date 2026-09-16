"""
بيانات اصطناعية — **للاختبارات الوحدوية فقط**.
==============================================
TEST FIXTURE — ليست بيانات سوق ولا تُستخدم في أي مسار إنتاجي.
الغرض: التحقق من صحة الحسابات البرمجية بمدخلات معلومة.

كود الإنتاج (src/) لا يستورد هذا الملف إطلاقاً.
"""
import numpy as np
import time
from src.data.types import OHLCV, INTERVAL_MS


def make_fixture(n: int = 1500, interval: str = '1h', seed: int = 42,
                 symbol: str = 'TESTUSDT', kind: str = 'mixed') -> OHLCV:
    """FIXTURE: سلسلة أسعار محددة البذرة لاختبار الآليات فقط."""
    rng = np.random.default_rng(seed)
    step = INTERVAL_MS[interval]
    now = int(time.time() * 1000)
    now = now - (now % step)
    ot = np.array([now - (n - i) * step for i in range(n)], dtype=np.int64)

    if kind == 'up':
        c = 100 * np.cumprod(1 + rng.normal(0.0012, 0.006, n))
    elif kind == 'down':
        c = 100 * np.cumprod(1 + rng.normal(-0.0010, 0.006, n))
    elif kind == 'range':
        c = 100 + np.sin(np.arange(n) / 20) * 4 + rng.normal(0, 0.4, n)
    else:
        segs, p = [], 100.0
        while sum(len(s) for s in segs) < n:
            L = int(rng.integers(80, 220))
            drift = float(rng.choice([0.0015, -0.0012, 0.0, 0.0006]))
            vol = float(rng.choice([0.004, 0.008, 0.015]))
            s = p * np.cumprod(1 + rng.normal(drift, vol, L))
            segs.append(s); p = float(s[-1])
        c = np.concatenate(segs)[:n]

    c = np.maximum(c, 1e-6)
    o = np.concatenate([[c[0]], c[:-1]])
    wick = np.abs(rng.normal(0, 0.004, n)) * c
    h = np.maximum(o, c) + wick
    l = np.minimum(o, c) - wick
    v = rng.lognormal(7, 0.6, n)
    return OHLCV(symbol, interval, ot, o, h, l, c, v, source='TEST_FIXTURE')
