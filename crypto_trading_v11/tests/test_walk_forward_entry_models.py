"""
بحث Walk-Forward في معاملات Breakout/Pullback —
الفجوة التي وثّقها `ENTRY_MODEL_VALIDATION_REPORT.md` صراحةً:

    «Walk-Forward الحالي لا يبحث فعلياً في معاملات Breakout/Pullback
     الخاصة — فجوة معمارية موثَّقة، لا مخفية.»

كانت `BREAKOUT_GRID` و`PULLBACK_GRID` **مُعرَّفتين بلا أي استخدام**
(`grid = grid or DEFAULT_GRID`)، والمحرك الافتراضي `SignalEngine` لا
يقرأ إعداداتهما أصلاً — فحتى تمرير الشبكة يدوياً كان يقيس Baseline.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.validation import walk_forward as wf
from src.core.config import Config, ValidationConfig
from src.signals.engine import SignalEngine
from src.research.router_adapter import RouterAsSignalEngine
from tests.fixtures import make_fixture


def _cfg(bo=False, pb=False):
    c = Config()
    c.signal.min_score = 1.0
    c.no_trade.min_data_quality = 0.5
    c.no_trade.require_btc_ok = False
    c.breakout.enabled = bo
    c.pullback.enabled = pb
    return c


def _vc():
    v = ValidationConfig()
    # عيّنة صغيرة عمداً: الغرض إثبات أن البحث يجري ويُثبَّت ويُسجَّل،
    # لا تقييم استراتيجية. تكبيرها يُبطئ المجموعة بلا زيادة في الدلالة.
    v.wf_train_bars, v.wf_test_bars, v.wf_step_bars = 450, 200, 350
    return v


class Test01_GridExtension(unittest.TestCase):
    """الشبكة تُوسَّع بحسب ما هو مُفعَّل — لا يدوياً."""

    def _paths(self, **f):
        stages, _ = wf.resolve_search(_cfg(**f))
        return {k for st in stages for k in st}

    def test_baseline_grid_unchanged(self):
        self.assertEqual(self._paths(), set(wf.DEFAULT_GRID),
                         'الافتراضي تغيَّر — انحدار في التوافق الخلفي')

    def test_breakout_paths_added(self):
        p = self._paths(bo=True)
        self.assertTrue(set(wf.BREAKOUT_GRID) <= p)
        self.assertTrue(set(wf.DEFAULT_GRID) <= p)

    def test_pullback_paths_added(self):
        self.assertTrue(set(wf.PULLBACK_GRID) <= self._paths(pb=True))

    def test_both_added_together(self):
        p = self._paths(bo=True, pb=True)
        self.assertTrue(set(wf.BREAKOUT_GRID) <= p)
        self.assertTrue(set(wf.PULLBACK_GRID) <= p)

    def test_explicit_grid_is_respected(self):
        """شبكة مُمرَّرة صراحةً لا تُوسَّع — المستدعي يعرف ما يريد."""
        g = {'min_score': [3.0]}
        stages, _ = wf.resolve_search(_cfg(bo=True, pb=True), grid=g)
        self.assertEqual({k for st in stages for k in st}, {'min_score'})


class Test02_EngineSelection(unittest.TestCase):
    """
    الشقّ الثاني من الإصلاح: المحرك. `SignalEngine` لا يقرأ إعدادات
    breakout/pullback إطلاقاً، فالشبكة وحدها لا تكفي.
    """

    def test_baseline_uses_signal_engine(self):
        _, f = wf.resolve_search(_cfg())
        self.assertIsInstance(f(_cfg()), SignalEngine)

    def test_entry_models_use_the_live_wrapper(self):
        """
        `RouterAsSignalEngine` هو نفس الغلاف الذي يستخدمه
        `live_trader.py` حصراً — فيقيس Walk-Forward ما يعمل في الإنتاج.
        """
        for f_kw in ({'bo': True}, {'pb': True}, {'bo': True, 'pb': True}):
            _, f = wf.resolve_search(_cfg(**f_kw))
            self.assertIsInstance(f(_cfg(**f_kw)), RouterAsSignalEngine,
                                  f'محرك خاطئ لـ {f_kw}')

    def test_explicit_factory_wins(self):
        sentinel = object()
        _, f = wf.resolve_search(_cfg(bo=True), engine_factory=lambda c: sentinel)
        self.assertIs(f(_cfg()), sentinel)


class Test03_ParamsActuallySearchedAndFrozen(unittest.TestCase):
    """الإثبات على التشغيل الحقيقي لا على بناء الشبكة."""

    @classmethod
    def setUpClass(cls):
        cls.d = make_fixture(1200, '1h', seed=42, kind='mixed')

    def _run(self, **f):
        return wf.run(self.d, base_cfg=_cfg(**f), val_cfg=_vc(), verbose=False)

    def test_entry_params_appear_in_best_params(self):
        """
        الحارس الجوهري: كانت `best_params` تخلو من أي مفتاح نموذج حين
        لا تُحسِّن المعاملات — فيستحيل التمييز بين «بُحثت ولم تُفِد»
        و«لم تُبحَث إطلاقاً». وهذا الالتباس هو ما أبقى الفجوة مخفيّة.
        """
        r = self._run(bo=True)
        chosen = set()
        for w in r['windows']:
            chosen |= {k for k in w['best_params'] if k.startswith('breakout.')}
        self.assertTrue(chosen, 'لا معامل breakout في best_params — لم يُبحَث')

    def test_stage_log_distinguishes_searched_from_useful(self):
        r = self._run(bo=True, pb=True)
        stages = r['windows'][0]['search_stages']
        model_stages = [s for s in stages
                        if any('.' in p for p in s['paths'])]
        self.assertEqual(len(model_stages), 2, 'مرحلتا النماذج غير مُسجَّلتين')
        for s in model_stages:
            self.assertGreater(s['candidates'], 1, 'مرحلة بلا مرشَّحين فعليين')
            self.assertIn('improved', s)

    def test_baseline_run_has_no_model_stages(self):
        r = self._run()
        for w in r['windows']:
            extra = [s for s in w['search_stages']
                     if any('.' in p for p in s['paths'])]
            self.assertEqual(extra, [], 'مرحلة نماذج في تشغيل baseline')

    def test_no_train_test_overlap_still_holds(self):
        """الضمان الأهم: البحث كلّه داخل TRAIN، ولا تسريب لـ OOS."""
        r = self._run(bo=True, pb=True)
        for w in r['windows']:
            self.assertGreater(w['test_start'], w['train_end'],
                               'تداخل TRAIN/TEST — تسريب')

    def test_staged_search_is_bounded(self):
        """
        الضرب الديكارتي = 27×9×9 = 2187 لكل نافذة. البحث المرحلي
        يجب أن يبقى قريباً من 45.
        """
        r = self._run(bo=True, pb=True)
        per_window = r['windows'][0]['n_candidates']
        self.assertLessEqual(per_window, 60,
                             f'انفجار حسابي: {per_window} مرشَّح لكل نافذة')
        self.assertGreaterEqual(per_window, 27 + 9 + 9 - 5)


if __name__ == '__main__':
    unittest.main(verbosity=2)
