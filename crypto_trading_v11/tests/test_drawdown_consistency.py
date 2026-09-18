"""
اتّساق الانخفاض الأقصى عبر المشروع.

كان يُحسَب في **أربعة** مواضع بأربع طرق تُعطي إجابات متباينة لنفس
الصفقات. قياس فعلي برأس مال 1000:

    الصفقات            الحقيقي   paper_report      accuracy
    أرباح ثم انهيار     45.00%       18.75%        9.0e+01%
    خسارة أولاً         10.00%        0.00%        1.0e+13%
    خسائر صغيرة          2.00%       32.26%        2.0e+12%

فمستخدم يقرأ `report` و`gate` والباكتست يحصل على ثلاثة أرقام مختلفة
لنفس التشغيل — وهذا ما سيقرأه بعد تشغيله الورقي الحقيقي.
"""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.backtest.metrics import max_drawdown_pct, drawdown_from_pnl
from src.monitoring.paper_report import _agg
from src.monitoring.accuracy import _stats

CAP = 1000.0
CASES = [
    ('أرباح ثم انهيار', [500.0, 500.0, -900.0], 45.0),
    ('خسارة أولاً', [-100.0, 50.0], 10.0),
    ('خسائر صغيرة', [-10.0, -10.0], 2.0),
    ('ربح خالص', [100.0, 100.0], 0.0),
]


class Test01_CanonicalIsCorrect(unittest.TestCase):

    def test_matches_hand_computed_equity_curve(self):
        for label, pnl, expected in CASES:
            eq = np.concatenate([[CAP], CAP + np.cumsum(pnl)])
            self.assertAlmostEqual(max_drawdown_pct(eq), expected, places=6,
                                   msg=label)

    def test_from_pnl_matches_from_curve(self):
        for label, pnl, expected in CASES:
            self.assertAlmostEqual(drawdown_from_pnl(pnl, CAP), expected,
                                   places=6, msg=label)

    def test_empty_is_zero_not_error(self):
        self.assertEqual(max_drawdown_pct([]), 0.0)
        self.assertEqual(drawdown_from_pnl([], CAP), 0.0)


class Test02_NoFabricatedBasis(unittest.TestCase):
    """رقم مُطمئِن بلا سند أسوأ من الاعتراف بالجهل."""

    def test_missing_capital_returns_none(self):
        self.assertIsNone(drawdown_from_pnl([-100.0], None))

    def test_zero_or_negative_capital_returns_none(self):
        for bad in (0.0, -5.0):
            self.assertIsNone(drawdown_from_pnl([-100.0], bad))

    def test_non_numeric_capital_returns_none(self):
        self.assertIsNone(drawdown_from_pnl([-100.0], 'abc'))

    def test_reporters_propagate_none(self):
        rows = [{'pnl': -100.0}]
        self.assertIsNone(_agg(rows)['max_drawdown_pct'])
        self.assertIsNone(_stats(rows)['max_drawdown_pct'])


class Test03_AllReportersAgree(unittest.TestCase):
    """الحارس الجوهري: أربعة مواضع، رقم واحد."""

    def test_same_trades_same_number_everywhere(self):
        for label, pnl, expected in CASES:
            rows = [{'pnl': p} for p in pnl]
            got = {
                'canonical': drawdown_from_pnl(pnl, CAP),
                'paper_report': _agg(rows, CAP)['max_drawdown_pct'],
                'accuracy': _stats(rows, CAP)['max_drawdown_pct'],
            }
            for name, v in got.items():
                self.assertAlmostEqual(
                    v, expected, places=3,
                    msg=f'{name} يخالف الحقيقي في «{label}»: {got}')

    def test_no_astronomical_values(self):
        """
        حارس على البق الأصلي في `accuracy`: القسمة على قمة قرب الصفر
        كانت تُنتج نسباً بالتريليونات.
        """
        for label, pnl, _ in CASES:
            v = _stats([{'pnl': p} for p in pnl], CAP)['max_drawdown_pct']
            self.assertLessEqual(v, 100.0,
                                 f'انخفاض مستحيل في «{label}»: {v}')

    def test_drawdown_never_exceeds_one_hundred(self):
        v = drawdown_from_pnl([-999.0], CAP)
        self.assertLessEqual(v, 100.0)
        self.assertGreater(v, 99.0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
