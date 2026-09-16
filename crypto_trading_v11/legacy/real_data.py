"""
جالب البيانات الحقيقية من بينانس
=================================
لا يحتاج API key — بيانات الشموع العامة مفتوحة.
لا يحتاج مكتبات خارجية — urllib فقط.

يحفظ في cache/ عشان ما تعيد التحميل كل مرة.
"""

import json, os, time, csv
import urllib.request
import urllib.error
from datetime import datetime, timezone

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cache')
os.makedirs(CACHE_DIR, exist_ok=True)

BASE_HOSTS = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api-gcp.binance.com",
    "https://data-api.binance.vision",   # مرآة عامة، تعمل غالباً بلا قيود جغرافية
]

INTERVAL_MS = {
    '1m': 60_000, '5m': 300_000, '15m': 900_000, '30m': 1_800_000,
    '1h': 3_600_000, '4h': 14_400_000, '1d': 86_400_000,
}


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fetch_klines(symbol='BTCUSDT', interval='4h', days=365, verbose=True):
    """
    يجلب الشموع الحقيقية من بينانس.
    يقسّم الطلب على دفعات 1000 شمعة (حد بينانس).
    """
    if interval not in INTERVAL_MS:
        raise ValueError(f"إطار غير مدعوم: {interval}")

    ms = INTERVAL_MS[interval]
    end = int(time.time() * 1000)
    start = end - days * 86_400_000
    needed = (end - start) // ms

    if verbose:
        print(f"📡 جلب {symbol} | {interval} | {days} يوم (~{needed} شمعة)")

    rows = []
    cursor = start
    host_i = 0

    while cursor < end:
        url = (f"{BASE_HOSTS[host_i]}/api/v3/klines?symbol={symbol}"
               f"&interval={interval}&startTime={cursor}&limit=1000")
        try:
            batch = _get(url)
        except Exception as e:
            host_i += 1
            if host_i >= len(BASE_HOSTS):
                if rows:
                    print(f"⚠️  توقف الجلب بعد {len(rows)} شمعة: {e}")
                    break
                raise RuntimeError(
                    f"فشل الاتصال بكل خوادم بينانس.\n"
                    f"السبب: {e}\n"
                    f"جرّب VPN، أو استخدم load_csv() ببيانات نزّلتها يدوياً."
                )
            if verbose:
                print(f"   ↻ تبديل الخادم إلى {BASE_HOSTS[host_i]}")
            continue

        if not batch:
            break
        rows.extend(batch)
        cursor = batch[-1][0] + ms
        if verbose and len(rows) % 5000 < 1000:
            print(f"   … {len(rows)} شمعة")
        time.sleep(0.12)   # احترام حدود المعدل

    if not rows:
        raise RuntimeError("لم تصل أي بيانات")

    data = {
        'symbol': symbol, 'interval': interval,
        'time': [int(r[0]) for r in rows],
        'open': [float(r[1]) for r in rows],
        'high': [float(r[2]) for r in rows],
        'low':  [float(r[3]) for r in rows],
        'close':[float(r[4]) for r in rows],
        'volume':[float(r[5]) for r in rows],
    }

    path = os.path.join(CACHE_DIR, f"{symbol}_{interval}.json")
    with open(path, 'w') as f:
        json.dump(data, f)

    if verbose:
        d0 = datetime.fromtimestamp(data['time'][0]/1000, timezone.utc)
        d1 = datetime.fromtimestamp(data['time'][-1]/1000, timezone.utc)
        print(f"✅ {len(rows)} شمعة حقيقية | {d0:%Y-%m-%d} ← {d1:%Y-%m-%d}")
        print(f"   محفوظة: {path}")

    return data


def load_cached(symbol='BTCUSDT', interval='4h'):
    path = os.path.join(CACHE_DIR, f"{symbol}_{interval}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def get_data(symbol='BTCUSDT', interval='4h', days=365, force=False):
    """يستخدم الكاش إن وُجد، وإلا يجلب من بينانس"""
    if not force:
        c = load_cached(symbol, interval)
        if c and len(c['close']) > 200:
            print(f"💾 من الكاش: {symbol} {interval} ({len(c['close'])} شمعة)")
            return c
    return fetch_klines(symbol, interval, days)


def load_csv(path, has_header=True):
    """
    بديل لو الاتصال محجوب: نزّل CSV من أي مصدر.
    الأعمدة المطلوبة: time, open, high, low, close, volume
    """
    data = {'time': [], 'open': [], 'high': [], 'low': [],
            'close': [], 'volume': [], 'symbol': 'CSV', 'interval': '?'}
    with open(path) as f:
        rd = csv.reader(f)
        if has_header:
            next(rd)
        for row in rd:
            if len(row) < 6:
                continue
            try:
                data['time'].append(int(float(row[0])))
                data['open'].append(float(row[1]))
                data['high'].append(float(row[2]))
                data['low'].append(float(row[3]))
                data['close'].append(float(row[4]))
                data['volume'].append(float(row[5]))
            except ValueError:
                continue
    print(f"📄 CSV: {len(data['close'])} شمعة")
    return data


def check_connection():
    """اختبار سريع للاتصال"""
    for host in BASE_HOSTS:
        try:
            _get(f"{host}/api/v3/ping", timeout=8)
            print(f"✅ الاتصال يعمل عبر: {host}")
            return host
        except Exception as e:
            print(f"❌ {host} — {type(e).__name__}")
    print("\n⚠️  كل الخوادم محجوبة. الخيارات:")
    print("   1. استخدم VPN")
    print("   2. نزّل CSV واستخدم load_csv()")
    return None


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == 'check':
        check_connection()
    else:
        d = get_data('BTCUSDT', '4h', 730)
        print(f"\nآخر سعر: ${d['close'][-1]:,.2f}")
