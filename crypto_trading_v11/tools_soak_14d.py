"""
اختبار تحمّل: ١٤ يوماً مُسرَّعة على بيانات اصطناعية.
=====================================================
⚠️ **ما يُثبته وما لا يُثبته** — اقرأ هذا قبل أي رقم أدناه:

يُثبت: أن الآلة تصمد. دورات متتالية بعدد أسبوعين، مع إعادة تشغيل
وسط الطريق، بلا انهيار ولا تسريب حالة ولا فساد قاعدة ولا نية عالقة
ولا مركز بلا حماية.

**لا يُثبت شيئاً عن الربح.** البيانات مولَّدة بـ `make_fixture()`
— ضوضاء عشوائية محدَّدة البذرة، لا سوق. أي PnL هنا خاصية للبذرة لا
للاستراتيجية. القراءة الوحيدة المشروعة: «النظام لم ينكسر».

## حدّ جوهري: `ALREADY_PROCESSED`

جزء كبير من الدورات يُرجع `ALREADY_PROCESSED`، وهذا **سلوك صحيح لا
خلل**: النظام يتّخذ قراراً واحداً لكل شمعة مُغلَقة، ويرفض البيانات
البائتة. لضغط أسبوعين في ثوانٍ يجب إزاحة المحور الزمني حتى تبدو آخر
شمعة طازجة — فتتكرّر بصمة الشمعة عبر دورات متتالية ويُلغيها حارس
التكرار.

النتيجة: عدد نقاط القرار المتميّزة أقل من عدد الدورات. لا يمكن إصلاح
ذلك بحيلة أفضل — **التوتر أصيل**: نظام يرفض البيانات القديمة لا
يُشغَّل أربعة عشر يوماً في نصف دقيقة. تشغيل ورقي حقيقي لأسبوعين يستغرق
أسبوعين، على جهاز يعمل، بشبكة مفتوحة. انظر `RUNBOOK_14D_PAPER.md`.
"""
import os
import sys
import shutil
import tempfile
import time
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, 'tests'))

import live_trader as LT
from src.environment.env import build as build_env, preflight
from src.core.config import Config
from tests.fixtures import make_fixture

INTERVAL = '4h'
BARS_PER_DAY = 6
DAYS = 14
TICKS = DAYS * BARS_PER_DAY          # 84 دورة = أسبوعان بفريم 4 ساعات
WARMUP = 500
SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT']
RESTART_AT = TICKS // 2              # إعادة تشغيل في منتصف المدة


class WindowCache:
    """
    يكشف للنظام نافذة تنتهي عند الشمعة الحالية فقط — كما في الزمن
    الحقيقي. كشف السلسلة كاملة كان سيمنح النظام رؤية للمستقبل ويُبطل
    الاختبار من أصله.
    """

    def __init__(self, series):
        self._series = series
        self.cursor = WARMUP

    def get(self, client, symbol, interval, days=365, force=False, verbose=True):
        return self._series[symbol].slice_to(self.cursor)


def _slice_to(self, end):
    """
    نافذة تنتهي عند الشمعة `end`، **مع إزاحة المحور الزمني** حتى تكون
    آخر شمعة فيها طازجة بالنسبة للزمن الحقيقي.

    ⚠️ بلا هذه الإزاحة كان النظام يرفض كل مرشَّح بـ
    `INSUFFICIENT_DATA_QUALITY` — وهو **محقّ تماماً**: نافذة تنتهي قبل
    ٨٣ يوماً من الآن بيانات بائتة، ورفضها هو السلوك الصحيح. الخلل كان
    في أداة الاختبار لا في النظام، والتسجيل هنا حتى لا يُقرأ ذلك لاحقاً
    كعيب في المحرك.
    """
    import numpy as np
    from src.data.types import OHLCV, INTERVAL_MS
    e = min(end + 1, len(self.close))
    step = INTERVAL_MS[self.interval]
    now = int(time.time() * 1000)
    now -= now % step
    # `(e - i)` لا `(e - 1 - i)`: آخر شمعة تُفتح عند `now - step` فتكون
    # **مُغلَقة** عند `now`. النسخة الأولى جعلتها تُفتح عند `now` — أي
    # شمعة جارية لم تُغلق بعد — فأسقط المُحقِّق الجودة إلى 0.55 ورفض كل
    # مرشَّح. وهو **محقّ**: بناء قرار على شمعة مفتوحة تسريب مستقبلي.
    ot = np.array([now - (e - i) * step for i in range(e)], dtype=np.int64)
    return OHLCV(self.symbol, self.interval, ot,
                 self.open[:e], self.high[:e], self.low[:e],
                 self.close[:e], self.volume[:e],
                 source='soak', fetched_at=now)


def build_trader(base_dir, cache, scan=True):
    os.environ.update({'TRADING_ENVIRONMENT': 'paper', 'CAPITAL': '10000',
                       'PAPER_LATENCY_MS': '0'})
    envcfg = build_env('paper', base_dir=base_dir, symbol='BTCUSDT',
                       interval=INTERVAL, strategy_version='v11.0.0',
                       config_fingerprint='soak')
    preflight(envcfg)
    t = LT.LiveTrader(envcfg, Config(), 500.0,
                      scan_symbols=SYMBOLS if scan else None)
    t.cache = cache
    if t.orders is not None:
        t.orders.gate._sleep = lambda s: None
    return t


def main():
    from src.data.types import OHLCV
    OHLCV.slice_to = _slice_to

    series = {s: make_fixture(WARMUP + TICKS + 5, INTERVAL, seed=100 + i,
                              symbol=s, kind='mixed')
              for i, s in enumerate(SYMBOLS)}
    cache = WindowCache(series)
    base = tempfile.mkdtemp(prefix='soak14d_')

    print(f"\n{'='*64}")
    print(f"  اختبار تحمّل — {DAYS} يوماً مُسرَّعة ({TICKS} دورة بفريم {INTERVAL})")
    print(f"  ⚠️ بيانات اصطناعية — لا دلالة ربحية إطلاقاً")
    print(f"{'='*64}\n")

    t = build_trader(base, cache)
    stats = {'ticks': 0, 'errors': 0, 'executed': 0, 'no_data': 0,
             'no_trade': 0, 'blocked': 0, 'restarts': 0, 'other': {}}
    t0 = time.time()
    start_day = datetime.utcnow() - timedelta(days=DAYS)

    for i in range(TICKS):
        cache.cursor = WARMUP + i
        for s in SYMBOLS:
            px = float(series[s].close[cache.cursor])
            if t.paper_market is not None:
                t.paper_market.set_price(s, px, spread_bps=4.0)

        if i == RESTART_AT:
            # إعادة تشغيل حقيقية: كائن جديد على نفس القاعدة
            del t
            t = build_trader(base, cache)
            stats['restarts'] += 1
            print(f"  [يوم {i//BARS_PER_DAY+1:>2}] 🔄 إعادة تشغيل — "
                  f"استعادة الحالة من القاعدة")

        try:
            r = t.tick(verbose=False) or {}
            st = r.get('status', '?')
            stats['ticks'] += 1
            if st == 'EXECUTED':
                stats['executed'] += 1
            elif st == 'NO_DATA':
                stats['no_data'] += 1
            elif st in ('NO_TRADE', 'WAIT', 'NO_ELIGIBLE_OPPORTUNITY'):
                stats['no_trade'] += 1
            elif st in ('PRE_TRADE_BLOCKED', 'RECON_FAILED',
                        'UNRESOLVED_INTENTS', 'MULTI_SYMBOL_POSITIONS'):
                stats['blocked'] += 1
            else:
                stats['other'][st] = stats['other'].get(st, 0) + 1
        except Exception as e:
            stats['errors'] += 1
            print(f"  ⛔ دورة {i}: {type(e).__name__}: {str(e)[:160]}")

        if (i + 1) % BARS_PER_DAY == 0:
            day = (i + 1) // BARS_PER_DAY
            eq = t.equity()
            pos = len(t.db.open_positions())
            print(f"  [يوم {day:>2}] {(start_day+timedelta(days=day)).strftime('%m-%d')} "
                  f"| حقوق {eq:>9,.2f} | مراكز {pos} | "
                  f"صفقات {stats['executed']}")

    dur = time.time() - t0
    print(f"\n{'─'*64}")
    print(f"  انتهى في {dur:.1f} ثانية (زمن حقيقي)\n")

    # ── فحوص السلامة بعد الأسبوعين ──
    db = t.db
    checks = []
    unresolved = db.unresolved_intents()
    checks.append(('لا نوايا عالقة', len(unresolved) == 0,
                   f'{len(unresolved)} نية'))
    open_pos = db.open_positions()
    unprot = [p for p in open_pos if not p.get('stop_order_id')]
    checks.append(('كل مركز مفتوح محميّ', not unprot,
                   f'{len(unprot)} بلا وقف'))
    syms = {p['symbol'] for p in open_pos}
    checks.append(('سياسة المركز الواحد', len(open_pos) <= 1,
                   f'{len(open_pos)} مفتوح على {sorted(syms) or "—"}'))
    checks.append(('بلا أعطال دورة', stats['errors'] == 0,
                   f"{stats['errors']} عطل"))
    crit = db.query("SELECT COUNT(*) c FROM risk_events WHERE severity='CRITICAL'")
    ncrit = crit[0]['c'] if crit else 0
    checks.append(('بلا أحداث حرجة', ncrit == 0, f'{ncrit} حدث'))
    ks = db.get_kv('kill_switch')
    checks.append(('مفتاح الإيقاف مُطفأ', not ks, str(ks)))
    integ = db.query('PRAGMA integrity_check')
    ok_int = bool(integ) and list(integ[0].values())[0] == 'ok'
    checks.append(('سلامة قاعدة البيانات', ok_int, str(integ[:1])))
    plans = db.query('SELECT COUNT(*) c FROM sizing_plans')
    checks.append(('خطط تحجيم مُسجَّلة', True,
                   f"{plans[0]['c'] if plans else 0} خطة"))
    scans = db.query('SELECT COUNT(*) c FROM opportunity_scans')
    checks.append(('عمليات مسح مُسجَّلة', True,
                   f"{scans[0]['c'] if scans else 0} مسح"))

    print('  نتيجة الدورات:')
    for k, v in (('دورات', stats['ticks']), ('صفقات مُنفَّذة', stats['executed']),
                 ('لا تداول', stats['no_trade']), ('محظورة', stats['blocked']),
                 ('بلا بيانات', stats['no_data']),
                 ('إعادات تشغيل', stats['restarts']),
                 ('أعطال', stats['errors'])):
        print(f'    {k:<18} {v}')
    if stats['other']:
        print(f"    حالات أخرى        {stats['other']}")

    print('\n  فحوص السلامة:')
    failed = 0
    for name, ok, detail in checks:
        mark = '✅' if ok else '⛔'
        if not ok:
            failed += 1
        print(f'    {mark} {name:<26} {detail}')

    eq = t.equity()
    print(f'\n  الحقوق النهائية: {eq:,.2f} USDT (بدأت 10,000)')
    print('  ⚠️ هذا الرقم خاصية لبذرة عشوائية، لا نتيجة استراتيجية.')
    print(f"\n{'='*64}")
    print(f"  النتيجة: {'PASS — الآلة صمدت' if failed == 0 else f'FAIL — {failed} فحص'}")
    print(f"  الحافة: غير مُثبَتة (بيانات اصطناعية)")
    print(f"{'='*64}\n")

    shutil.rmtree(base, ignore_errors=True)
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
