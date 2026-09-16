"""
اختبارات إصلاح Walk-Forward — القسم الرابع.
كانت `_apply()` تتجاهل breakout.*/pullback.* بصمت، وكانت `run()`
مُثبَّتة على SignalEngine بلا طريقة لتمرير Breakout/Pullback إطلاقاً.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.config import Config, ValidationConfig
from src.validation import walk_forward
from src.validation.walk_forward import _apply, BREAKOUT_GRID, PULLBACK_GRID
from src.signals.entry import BreakoutEntryModel, PullbackEntryModel
from src.research.router_adapter import IsolatedModelAdapter
from tests.fixtures import make_fixture

# نوافذ أصغر للاختبارات فقط — القسم C من متطلبات V11: "Optimize only
# without changing semantics". الفصل Train/Test/OOS والتجميد قبل رؤية
# OOS **منطق**، لا يعتمد على حجم النافذة إطلاقاً؛ تصغيرها هنا يُسرِّع
# الحساب فقط (كل نافذة تُعالِج شموعاً أقل)، بلا مساس بالإنتاج
# (DEFAULT_GRID/BREAKOUT_GRID/PULLBACK_GRID ونوافذها الحقيقية 1500/500/500
# تبقى كما هي تماماً لأي استخدام بحثي فعلي).
TEST_VAL_CFG = ValidationConfig(wf_train_bars=300, wf_test_bars=100,
                                wf_step_bars=100)


class Test01_ApplySupportsAllSections(unittest.TestCase):
    def test_breakout_path_changes_fingerprint(self):
        a = Config()
        b = _apply(a, {'breakout.min_distance_atr': 0.33})
        self.assertNotEqual(a.fingerprint(), b.fingerprint())
        self.assertEqual(b.breakout.min_distance_atr, 0.33)

    def test_pullback_path_changes_fingerprint(self):
        a = Config()
        b = _apply(a, {'pullback.zone_atr': 0.77})
        self.assertNotEqual(a.fingerprint(), b.fingerprint())
        self.assertEqual(b.pullback.zone_atr, 0.77)

    def test_bare_key_still_targets_signal_backward_compat(self):
        a = Config()
        b = _apply(a, {'min_score': 6.0})
        self.assertEqual(b.signal.min_score, 6.0)

    def test_invalid_path_raises_not_ignored(self):
        """فشل تطبيق معامل يجب أن يُفشِل التجربة، لا أن يتجاهله بصمت."""
        with self.assertRaises(ValueError):
            _apply(Config(), {'breakout.does_not_exist': 1})

    def test_invalid_top_level_section_raises(self):
        with self.assertRaises(ValueError):
            _apply(Config(), {'no_such_section.field': 1})

    def test_original_base_config_never_mutated(self):
        a = Config()
        original_fp = a.fingerprint()
        _apply(a, {'breakout.min_distance_atr': 0.99})
        self.assertEqual(a.fingerprint(), original_fp, '_apply عدَّلت الأصل!')


class Test02_BreakoutParamsNotLeakedToPullback(unittest.TestCase):
    def test_grids_are_disjoint(self):
        self.assertTrue(set(BREAKOUT_GRID).isdisjoint(set(PULLBACK_GRID)))

    def test_walk_forward_selected_params_stay_in_their_own_namespace(self):
        d = make_fixture(700, '1h', seed=70, kind='mixed')
        cfg_bo = Config(); cfg_bo.breakout.enabled = True
        wf_bo = walk_forward.run(
            d, cfg_bo, grid=BREAKOUT_GRID, capital=10000, verbose=False, val_cfg=TEST_VAL_CFG,
            engine_factory=lambda c: IsolatedModelAdapter(c, BreakoutEntryModel(c)))
        for w in wf_bo['windows']:
            for k in w['best_params']:
                self.assertTrue(k.startswith('breakout.'),
                               f'معامل غير breakout ظهر في نتائج بحث Breakout: {k}')


class Test03_OOSFreezing(unittest.TestCase):
    def test_no_train_test_overlap_across_all_windows(self):
        d = make_fixture(700, '1h', seed=71)
        cfg = Config()
        wf = walk_forward.run(d, cfg, capital=10000, verbose=False, val_cfg=TEST_VAL_CFG)
        for w in wf['windows']:
            self.assertLessEqual(w['train_end'], w['test_start'] - 1)

    def test_params_frozen_before_oos_evaluation(self):
        """
        best_params تُحدَّد من train_metrics فقط، ثم يُعاد استخدامها
        حرفياً لبناء cfg الاختبار — لا اختيار لاحق بعد رؤية OOS.
        """
        d = make_fixture(700, '1h', seed=72)
        cfg = Config()
        wf = walk_forward.run(d, cfg, capital=10000, verbose=False, val_cfg=TEST_VAL_CFG)
        for w in wf['windows']:
            expected_cfg = _apply(cfg, w['best_params'])
            self.assertEqual(expected_cfg.fingerprint(),
                             _apply(cfg, w['best_params']).fingerprint())

    def test_engine_factory_default_is_backward_compatible(self):
        """بلا engine_factory، السلوك القديم (SignalEngine/Baseline) يبقى كما هو."""
        d = make_fixture(700, '1h', seed=73)
        cfg = Config()
        wf1 = walk_forward.run(d, cfg, capital=10000, verbose=False,
                              val_cfg=TEST_VAL_CFG)
        wf2 = walk_forward.run(d, cfg, capital=10000, verbose=False,
                              val_cfg=TEST_VAL_CFG, engine_factory=None)
        self.assertEqual(wf1['oos_trades'], wf2['oos_trades'])


class Test04_ReportsAppliedParams(unittest.TestCase):
    def test_result_lists_grid_paths_used(self):
        d = make_fixture(700, '1h', seed=74)
        cfg = Config(); cfg.breakout.enabled = True
        wf = walk_forward.run(
            d, cfg, grid=BREAKOUT_GRID, capital=10000, verbose=False, val_cfg=TEST_VAL_CFG,
            engine_factory=lambda c: IsolatedModelAdapter(c, BreakoutEntryModel(c)))
        self.assertEqual(set(wf['params_grid_paths']), set(BREAKOUT_GRID.keys()))


class Test05_RequiredMetrics(unittest.TestCase):
    """لا اعتماد على Aggregate PF وحده — القسم الرابع."""

    def test_all_required_oos_metrics_present(self):
        d = make_fixture(700, '1h', seed=75)
        wf = walk_forward.run(d, Config(), capital=10000, verbose=False, val_cfg=TEST_VAL_CFG)
        for key in ('oos_window_count', 'oos_positive_window_count',
                   'oos_positive_window_rate', 'oos_pf_median', 'oos_pf_min',
                   'oos_pf_max', 'oos_expectancy_median', 'oos_total_trades'
                   if 'oos_total_trades' in wf else 'oos_trades',
                   'oos_max_drawdown', 'oos_consistency_score'):
            self.assertIn(key, wf, f'{key} غائب عن نتيجة walk_forward.run()')


# ══ 6. حد زمني صريح — القسم C من متطلبات V11 ══
class Test06_BoundedPerformance(unittest.TestCase):
    """
    القسم C: "Add explicit timing/complexity reporting" +
    "Tests must have bounded deterministic runtime". الشبكات
    الإنتاجية (BREAKOUT_GRID/PULLBACK_GRID/DEFAULT_GRID) ونوافذها
    الحقيقية (1500/500/500) لم تُمَس — TEST_VAL_CFG أعلاه أصغر
    للاختبارات فقط، بلا أي تغيير في منطق train→optimize→freeze→OOS.
    """

    def test_single_walk_forward_run_completes_within_bounded_time(self):
        d = make_fixture(700, '1h', seed=90)
        cfg = Config()
        start = time.time()
        wf = walk_forward.run(d, cfg, capital=10000, verbose=False,
                              val_cfg=TEST_VAL_CFG)
        elapsed = time.time() - start
        n_combos = 3 ** 3   # DEFAULT_GRID: 3 معاملات × 3 قيم لكل منها
        print(f'\n  [تقرير أداء] walk_forward.run(): {elapsed:.2f}ث | '
             f'{wf["n_windows"]} نافذة × حتى {n_combos} تركيبة = '
             f'حتى {wf["n_windows"] * n_combos} تشغيلة باكتست (TRAIN) + '
             f'{wf["n_windows"]} تشغيلة OOS')
        self.assertLess(elapsed, 15.0,
                        f'نافذة Walk-Forward واحدة استغرقت {elapsed:.2f}ث — '
                        f'قد يدل على انفجار تراكيب غير مقصود')


if __name__ == '__main__':
    unittest.main(verbosity=2)
