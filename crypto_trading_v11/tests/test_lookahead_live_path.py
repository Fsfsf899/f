"""
سدّ فجوة تغطية — انحياز النظر للأمام على **المسار الحيّ الحقيقي**.

فحوص السببية القائمة (`tests/test_all.py` و `run_validation.py`) تستدعي
`SignalEngine` المباشرة. لكن `live_trader.py` لا يستخدمها: يستخدم
`RouterAsSignalEngine` (غلاف حول `EntryRouter`)، وعند تفعيل
`BREAKOUT_ENABLED` أو `PULLBACK_ENABLED` يتخذ القرار فعلياً
`BreakoutEntryModel`/`PullbackEntryModel` — ولم تُفحَص أيٌّ منها للسببية
قط.

هذا نفس نمط الخطأ الموثَّق في `CRITICAL_BTC_WIRING_REGRESSION_REPORT.md`:
فحص ممتاز مُوجَّه إلى مكوّن ليس هو العامل في الإنتاج.

النتيجة عند الكتابة: **النماذج سببية فعلاً وتمرّ كلها** — أي أن هذا سدّ
فجوة تغطية لا إصلاح بق. القيمة في أن الخاصية صارت محروسة: أي تسريب
مستقبلي يُدخَل على نماذج الدخول سيسقط هنا بدل أن يمرّ صامتاً وينتج
باكتست مزيَّفاً لا يتكرّر في السوق.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tests.fixtures import make_fixture
from src.validation.lookahead import check_decisions
from src.research.router_adapter import RouterAsSignalEngine
from src.core.config import Config

KEYS = ('d', 'sc', 'st', 'entry', 'stop')


def _cfg(breakout=False, pullback=False, retest=False, mtf=False):
    c = Config()
    c.breakout.enabled = breakout
    c.pullback.enabled = pullback
    c.breakout.retest_enabled = retest
    c.mtf_entry.confirmation_enabled = mtf
    return c


class LookaheadOnLivePath(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = make_fixture(1200, '1h', seed=7)

    def _assert_causal(self, cfg, label):
        eng = RouterAsSignalEngine(cfg)

        def dec(dd, i):
            s = eng.evaluate(dd, i, data_quality=0.95)
            return {'d': s.decision, 'sc': float(s.score), 'st': s.stars,
                    'entry': float(s.entry or 0), 'stop': float(s.stop_loss or 0)}

        for seed in (11, 22):
            r = check_decisions(dec, self.d, sample=15, seed=seed,
                                keys=list(KEYS))
            self.assertTrue(
                r['passed'],
                f'تسريب مستقبلي في {label} (seed={seed}): {r["failures"][:3]}')
            self.assertGreater(r['checked'], 0, 'لم يُفحص شيء فعلياً')

    def test_router_default_is_causal(self):
        """الغلاف الذي يعمل فعلاً في live_trader.py، بالإعداد الافتراضي."""
        self._assert_causal(_cfg(), 'RouterAsSignalEngine الافتراضي')

    def test_breakout_model_is_causal(self):
        self._assert_causal(_cfg(breakout=True), 'BreakoutEntryModel')

    def test_pullback_model_is_causal(self):
        self._assert_causal(_cfg(pullback=True), 'PullbackEntryModel')

    def test_all_entry_flags_together_are_causal(self):
        """أشمل تركيبة: كل النماذج + إعادة الاختبار + تأكيد الإطار الأعلى."""
        self._assert_causal(
            _cfg(breakout=True, pullback=True, retest=True, mtf=True),
            'كل أعلام الدخول مجتمعة')


class BreakoutRetestReachableFromLivePath(unittest.TestCase):
    """
    بق اكتشفه اختبار السببية أعلاه أثناء كتابته: `process_breakout_retest()`
    تقرأ `context['cfg']`/`['symbol']`/`['interval']`، لكن
    `RouterAsSignalEngine` — الغلاف الذي يستخدمه `live_trader.py` حصراً —
    لم يكن يضع أياً منها. فتفعيل `BREAKOUT_RETEST_ENABLED=1` يرفع
    `KeyError: 'cfg'` في كل دورة، وحلقة `run()` تبتلعه وتسجّله TICK_ERROR
    وتتابع — النظام يبدو حيّاً وهو لا يتخذ أي قرار إطلاقاً.

    `IsolatedModelAdapter` (مسار البحث) كان يبني السياق كاملاً: الميزة
    تعمل حيث تُختبَر وتتعطّل حيث تعمل.
    """

    @classmethod
    def setUpClass(cls):
        cls.d = make_fixture(600, '1h', seed=3)

    def test_retest_does_not_raise_on_live_path(self):
        eng = RouterAsSignalEngine(_cfg(breakout=True, retest=True))
        try:
            sig = eng.evaluate(self.d, len(self.d) - 1, data_quality=0.95)
        except KeyError as e:
            self.fail(f'مسار retest الحيّ يرفع KeyError: {e}')
        self.assertIsNotNone(sig.decision)

    def test_retest_without_db_degrades_explicitly(self):
        """بلا قاعدة: رفض موسوم NO_DB_CONTEXT، لا انهيار ولا صمت."""
        eng = RouterAsSignalEngine(_cfg(breakout=True, retest=True))
        sig = eng.evaluate(self.d, len(self.d) - 1, data_quality=0.95)
        self.assertNotEqual(sig.decision, 'BUY')

    def test_db_is_passed_through_to_retest(self):
        """مع قاعدة: تصل فعلاً إلى السياق فتعمل الميزة لا تُرفَض بصمت."""
        import tempfile, os as _os
        from src.storage.database import Database
        db = Database(_os.path.join(tempfile.mkdtemp(), 'rt.db'))
        eng = RouterAsSignalEngine(_cfg(breakout=True, retest=True), db=db)
        seen = {}
        real = eng.router._retest_context

        def spy(data, context):
            ctx = real(data, context)
            seen.update({k: ctx.get(k) for k in
                         ('cfg', 'symbol', 'interval', 'interval_ms', 'db')})
            return ctx

        eng.router._retest_context = spy
        eng.evaluate(self.d, len(self.d) - 1, data_quality=0.95)
        self.assertIsNotNone(seen.get('cfg'), 'cfg لم يصل إلى سياق retest')
        self.assertEqual(seen.get('symbol'), self.d.symbol)
        self.assertEqual(seen.get('interval'), self.d.interval)
        self.assertGreater(seen.get('interval_ms') or 0, 0)
        self.assertIs(seen.get('db'), db, 'القاعدة لم تصل — الميزة معطَّلة بصمت')


if __name__ == '__main__':
    unittest.main(verbosity=2)
