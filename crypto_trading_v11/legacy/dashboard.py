"""
لوحة التحكم — موقع محلي
========================
يشتغل بـ Python وحده. لا Flask ولا أي مكتبة خارجية.

التشغيل:  python3 dashboard.py
ثم افتح:  http://localhost:8000

كل رقم في الموقع محسوب من بيانات بينانس الحقيقية.
إذا فشل جلب البيانات، الموقع يقول لك بصراحة ولا يعرض أرقاماً مخترعة.
"""

import json, os, threading, webbrowser, traceback
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone

import numpy as np
import real_data
from final_engine import (FinalBacktest, FinalSignalEngine, WalkForward,
                          ema, rsi, atr, adx)
from execution_engine import CostModel
from market_structure import MarketRegime, SupportResistance

STATE = {'status': 'idle', 'error': None, 'data': None}
HERE = os.path.dirname(os.path.abspath(__file__))


# ═══════════════════════════════════════════════
def analyze(symbol='BTCUSDT', interval='4h', days=730,
            min_score=4, capital=10000, force=False):
    """التحليل الكامل على بيانات حقيقية"""
    raw = real_data.get_data(symbol, interval, days, force=force)

    o = np.array(raw['open']); h = np.array(raw['high'])
    l = np.array(raw['low']);  c = np.array(raw['close'])
    v = np.array(raw['volume']); t = raw['time']

    if len(c) < 300:
        raise RuntimeError(f'بيانات قليلة ({len(c)} شمعة). زد days.')

    eng = FinalSignalEngine(min_score=min_score)
    bt = FinalBacktest(capital, cost_model=CostModel(), engine=eng)
    stats = bt.run(o, h, l, c, v)
    wf = WalkForward.run(o, h, l, c, v, n_windows=4, capital=capital)

    # الحالة الآن
    ind = {'ema_f': ema(c,20), 'ema_s': ema(c,50), 'rsi': rsi(c,14),
           'atr': atr(h,l,c,14), 'adx': adx(h,l,c,14)}
    i = len(c) - 1
    live = eng.evaluate(i, o, h, l, c, v, ind)
    reg = MarketRegime.detect(c, h, l, i)
    lv = SupportResistance.get_levels(h[-200:], l[-200:])

    # منحنى رأس المال (مبسّط للعرض)
    step = max(1, len(c)//200)
    price_series = [{'t': t[k], 'p': round(float(c[k]),2)}
                    for k in range(0, len(c), step)]

    d0 = datetime.fromtimestamp(t[0]/1000, timezone.utc).strftime('%Y-%m-%d')
    d1 = datetime.fromtimestamp(t[-1]/1000, timezone.utc).strftime('%Y-%m-%d')

    # الحكم النهائي
    pf = stats.get('profit_factor')
    n = stats.get('total_trades', 0)
    if n < 30:
        verdict = 'insufficient'
        vtext = f'عينة صغيرة ({n} صفقة). تحتاج 100+ صفقة لأي استنتاج.'
    elif pf and pf >= 1.3 and wf.get('consistency', 0) >= 0.75:
        verdict = 'good'
        vtext = 'نتائج واعدة. جرّب Testnet قبل أي مال حقيقي.'
    elif pf and pf >= 1.0:
        verdict = 'marginal'
        vtext = 'على الحافة. الرسوم ستأكل الربح. لا تستخدمه بأموال حقيقية.'
    else:
        verdict = 'bad'
        vtext = 'خاسرة. لا تربطها بحساب حقيقي.'

    return {
        'meta': {'symbol': symbol, 'interval': interval,
                 'candles': len(c), 'from': d0, 'to': d1,
                 'price': round(float(c[-1]),2),
                 'updated': datetime.now().strftime('%Y-%m-%d %H:%M'),
                 'source': 'Binance API — بيانات حقيقية'},
        'stats': stats,
        'walkforward': wf,
        'live': live,
        'regime': reg,
        'levels': {
            'res': sorted([r for r in lv['resistances']
                           if r['level'] > c[-1]], key=lambda x: x['level'])[:3],
            'sup': sorted([s for s in lv['supports']
                           if s['level'] < c[-1]], key=lambda x: -x['level'])[:3],
        },
        'series': price_series,
        'verdict': verdict, 'verdict_text': vtext,
        'cost_note': round(CostModel().breakeven_move_pct(), 3),
    }


def run_analysis(**kw):
    STATE['status'] = 'running'; STATE['error'] = None
    try:
        STATE['data'] = analyze(**kw)
        STATE['status'] = 'done'
    except Exception as e:
        STATE['error'] = str(e)
        STATE['status'] = 'error'
        traceback.print_exc()


# ═══════════════════════════════════════════════
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header('Content-Type', ctype + '; charset=utf-8')
        self.end_headers()
        self.wfile.write(body.encode('utf-8'))

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)

        if u.path == '/':
            with open(os.path.join(HERE, 'dashboard.html'), encoding='utf-8') as f:
                self._send(200, 'text/html', f.read())

        elif u.path == '/api/state':
            self._send(200, 'application/json',
                       json.dumps({'status': STATE['status'],
                                   'error': STATE['error'],
                                   'data': STATE['data']}, ensure_ascii=False))

        elif u.path == '/api/run':
            q = parse_qs(u.query)
            kw = dict(
                symbol=q.get('symbol', ['BTCUSDT'])[0],
                interval=q.get('interval', ['4h'])[0],
                days=int(q.get('days', ['730'])[0]),
                min_score=float(q.get('min_score', ['4'])[0]),
                capital=float(q.get('capital', ['10000'])[0]),
                force=q.get('force', ['0'])[0] == '1',
            )
            threading.Thread(target=run_analysis, kwargs=kw, daemon=True).start()
            self._send(200, 'application/json', json.dumps({'ok': True}))
        else:
            self._send(404, 'text/plain', 'not found')


def main(port=8000):
    print(f"""
╔════════════════════════════════════════════════╗
║   لوحة تحكم التداول — بيانات حقيقية           ║
╠════════════════════════════════════════════════╣
║   افتح المتصفح على:                            ║
║   http://localhost:{port}                        ║
║                                                ║
║   Ctrl+C للإيقاف                               ║
╚════════════════════════════════════════════════╝
""")
    try:
        webbrowser.open(f'http://localhost:{port}')
    except Exception:
        pass
    HTTPServer(('127.0.0.1', port), Handler).serve_forever()


if __name__ == '__main__':
    import sys
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 8000)
