"""
سلامة بوابات الترقية — آخر ما يقف قبل المال الحقيقي.

مشكلتان وجدتهما المراجعة النهائية، وكلتاهما تجعل الترقية **أسهل**
مما ينبغي:

1. `_runtime_days` كانت تقيس زمن الحائط منذ أول إقلاع لا التشغيل
   الفعلي — فنظام عمل عشر دقائق ثم تُرك يُبلِّغ عن 14 يوم تشغيل بعد
   أسبوعين خمول. شرط «شغّله N يوماً» صار «اترك القاعدة موجودة N يوماً».

2. الانخفاض الأقصى كان يُحسَب على أساس اصطناعي (`2×مجموع|الأرباح|+1`)
   بدل حقوق الحساب. مع ربح كبير سابق يتضخّم الأساس فيصغر الانخفاض:
   قياس فعلي أظهر انخفاضاً حقيقياً 45.00% يُبلَّغ عنه 18.75%.
"""
import os
import sys
import time
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.storage.database import Database
from src.monitoring import gates as G


def _db():
    return Database(os.path.join(tempfile.mkdtemp(), 'g.db'))


class Test01_RuntimeIsActivityNotWallClock(unittest.TestCase):

    def test_idle_database_reports_no_runtime(self):
        db = _db()
        db.set_kv('run_started_ts', int((time.time() - 30 * 86400) * 1000))
        self.assertEqual(G._runtime_days(db), 0.0,
                         'قاعدة قديمة بلا نشاط تُبلِّغ عن أيام تشغيل')

    def test_counts_only_active_days(self):
        db = _db()
        db.set_kv('run_started_ts', int((time.time() - 30 * 86400) * 1000))
        for d in ('2026-09-01', '2026-09-02', '2026-09-03'):
            db.start_day(d, 10_000.0)
        self.assertEqual(G._runtime_days(db), 3.0)

    def test_same_day_twice_counts_once(self):
        """يومان من الدورات في يوم واحد = يوم واحد."""
        db = _db()
        db.start_day('2026-09-01', 10_000.0)
        db.start_day('2026-09-01', 10_000.0)
        self.assertEqual(G._runtime_days(db), 1.0)

    def test_gate_rejects_idle_database(self):
        db = _db()
        db.set_kv('run_started_ts', int((time.time() - 30 * 86400) * 1000))
        g = G.evaluate(db, 'paper', tests_passed=True)
        rt = next(c for c in g.to_dict()['checks'] if c['name'] == 'runtime_days')
        self.assertFalse(rt['passed'], 'البوابة قبلت قاعدة خاملة')


class Test02_DrawdownUsesRealEquity(unittest.TestCase):

    def _with_trades(self, cap, pnls):
        db = _db()
        db.start_day('2026-09-01', cap)
        for i, p in enumerate(pnls, 1):
            sig = {'symbol': 'BTCUSDT', 'interval': '4h',
                   'timestamp': 1_700_000_000_000 + i * 1000,
                   'decision': 'BUY', 'strategy_version': 'v11.0.0'}
            sid = db.save_signal(sig)
            rid = db.save_recommendation(sid, sig, acted=True, reason='t')
            db.close_recommendation(rid, outcome='WIN' if p > 0 else 'LOSS',
                                    exit_price=1.0, exit_reason='TAKE_PROFIT',
                                    pnl=p, pnl_pct=0.0)
        return db

    def _dd(self, db):
        g = G.evaluate(db, 'paper', tests_passed=True)
        c = next(x for x in g.to_dict()['checks'] if x['name'] == 'max_drawdown')
        return c

    def test_big_win_then_loss_is_not_understated(self):
        """
        الحالة التي كانت تمرّ خطأً: ربح كبير يتضخّم به الأساس الاصطناعي
        فيصغر الانخفاض المُبلَّغ.
        """
        db = self._with_trades(1000.0, [500.0, 500.0, -900.0])
        c = self._dd(db)
        # الحقيقي: 2000 → 1100 ⇒ 45%
        self.assertIn('45.0', c['detail'].replace('45.00', '45.0'),
                      f"انخفاض غير حقيقي: {c['detail']}")
        self.assertFalse(c['passed'], 'انخفاض 45% مرّ من البوابة')

    def test_small_loss_is_not_overstated(self):
        """والاتجاه الآخر: خسارة 1% لا تُبلَّغ كـ33%."""
        db = self._with_trades(10_000.0, [-50.0, -50.0])
        c = self._dd(db)
        self.assertTrue(c['passed'], f'خسارة 1% رُفضت: {c["detail"]}')
        self.assertTrue(c['detail'].startswith('1.0'),
                        f'انخفاض مُبالَغ: {c["detail"]}')

    def test_missing_starting_equity_fails_closed(self):
        """
        بلا حقوق بداية مسجَّلة لا يمكن حساب انخفاض حقيقي — يفشل الفحص
        بدل اختراع أساس مُطمئِن.
        """
        db = _db()
        sig = {'symbol': 'BTCUSDT', 'interval': '4h',
               'timestamp': 1_700_000_000_000, 'decision': 'BUY',
               'strategy_version': 'v'}
        sid = db.save_signal(sig)
        rid = db.save_recommendation(sid, sig, acted=True, reason='t')
        db.close_recommendation(rid, outcome='LOSS', exit_price=1.0,
                                exit_reason='STOP_LOSS', pnl=-100.0, pnl_pct=0.0)
        c = self._dd(db)
        self.assertFalse(c['passed'])
        self.assertIn('غير مسجَّلة', c['detail'])

    def test_no_trades_means_no_drawdown(self):
        db = _db()
        db.start_day('2026-09-01', 10_000.0)
        self.assertTrue(self._dd(db)['passed'])


class Test03_GateStillBlocksByDefault(unittest.TestCase):
    """حارس: البوابة تبقى مانعة، ولم يُضعفها الإصلاح."""

    def test_empty_database_never_passes(self):
        self.assertFalse(G.evaluate(_db(), 'paper', tests_passed=True).passed)

    def test_untested_code_never_passes(self):
        db = _db()
        for i in range(40):
            db.start_day(f'2026-08-{i%28+1:02d}', 10_000.0)
        self.assertFalse(G.evaluate(db, 'paper', tests_passed=None).passed,
                         'اختبارات لم تُشغَّل واعتُبرت نجاحاً')

    def test_readiness_needs_both_environments(self):
        r = G.readiness(paper_db=_db(), testnet_db=None, tests_passed=True)
        self.assertNotEqual(r['classification'], G.PRODUCTION_READY)


if __name__ == '__main__':
    unittest.main(verbosity=2)
