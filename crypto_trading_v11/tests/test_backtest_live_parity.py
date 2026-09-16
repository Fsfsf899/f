"""
تكافؤ الباكتست والتنفيذ الحيّ في التحجيم — القسمان 9 و23.

بق حقيقي وجدته المراجعة النهائية: بعد ربط المسار الحيّ بلقطة الحساب،
بقي الباكتست ينادي `PositionSizer` بلا لقطة — فيسلك المسار القديم بلا
احتياطي نقدي ولا قيود منصة ولا إعادة حساب مخاطرة. القياس وقتها:
**الباكتست يفتح مراكز أكبر بـ 11.1%** من التنفيذ الحيّ لنفس الإشارة.

نفس صنف الانحراف الذي وُجد سابقاً في معادلة الرسوم: مساران يحسبان
الشيء نفسه بطريقتين، فيُبالغ الباكتست في العوائد بلا سبب استراتيجي.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.account.providers import SimulatedAccountProvider
from src.account.state import OK
from src.risk.position_sizing import PositionSizer
from src.backtest.costs import CostModel
from src.backtest.engine import BacktestEngine
from src.core.config import Config

ENTRY, STOP, CASH = 50000.0, 49000.0, 10000.0


class Test01_SizingParity(unittest.TestCase):

    def setUp(self):
        self.cfg = Config()
        self.sizer = PositionSizer(self.cfg.risk, CostModel(self.cfg.costs))
        self.prov = SimulatedAccountProvider(self.cfg.risk)

    def test_simulated_snapshot_applies_the_same_reserve(self):
        snap = self.prov.snapshot(available=CASH)
        self.assertEqual(snap.status, OK)
        self.assertAlmostEqual(
            snap.reserve_amount, CASH * self.cfg.risk.min_cash_reserve_pct / 100,
            places=6)
        self.assertAlmostEqual(snap.usable_equity,
                               CASH - snap.reserve_amount, places=6)

    def test_backtest_and_live_size_identically(self):
        """نفس الإشارة ونفس النقد ⇒ نفس الكمية بالضبط."""
        snap = self.prov.snapshot(available=CASH)
        common = dict(entry=ENTRY, stop=STOP, stars=3, consecutive_losses=0,
                      fee_rate=self.cfg.costs.taker_fee)
        bt = self.sizer.calculate(equity=CASH, account=snap, **common)
        lv = self.sizer.calculate(equity=snap.usable_equity, account=snap,
                                  **common)
        self.assertAlmostEqual(bt['qty'], lv['qty'], places=10)
        self.assertAlmostEqual(bt['notional'], lv['notional'], places=8)

    def test_legacy_path_would_be_materially_larger(self):
        """
        توثيق حجم البق: المسار القديم (بلا لقطة) يُنتج مركزاً أكبر
        بوضوح. لو تساوى المساران فهذا يعني أن الاحتياطي توقّف عن العمل.
        """
        snap = self.prov.snapshot(available=CASH)
        legacy = self.sizer.calculate(equity=CASH, entry=ENTRY, stop=STOP)
        aware = self.sizer.calculate(
            equity=CASH, entry=ENTRY, stop=STOP, account=snap,
            fee_rate=self.cfg.costs.taker_fee)
        self.assertGreater(legacy['notional'], aware['notional'] * 1.05,
                           'الاحتياطي لم يعد يُقلِّص الحجم — تحقّق منه')


class Test02_EngineUsesTheAccountPath(unittest.TestCase):
    """الإثبات على المحرك نفسه لا على المحجِّم بمعزل."""

    def test_engine_owns_a_simulated_provider(self):
        eng = BacktestEngine(Config())
        self.assertIsInstance(eng.account, SimulatedAccountProvider)

    @staticmethod
    def _trading_cfg():
        """
        عتبات مُخفَّضة عمداً: الإعداد الافتراضي لا يُنتج صفقة واحدة على
        بيانات اصطناعية (١٨ تشغيلاً بلا صفقة)، واختبار يتخطّى نفسه لا
        يُثبت شيئاً. هذه العتبات لتشغيل المسار لا لتقييم استراتيجية.
        """
        c = Config()
        c.signal.min_score = 1.0
        c.no_trade.min_data_quality = 0.5
        c.no_trade.require_btc_ok = False
        return c

    def test_engine_passes_account_to_sizer(self):
        from tests.fixtures import make_fixture
        eng = BacktestEngine(self._trading_cfg(), initial_capital=10000)
        seen = {}
        real = eng.sizer.calculate

        def spy(**kw):
            seen.setdefault('account', kw.get('account'))
            seen.setdefault('fee_rate', kw.get('fee_rate'))
            return real(**kw)

        eng.sizer.calculate = spy
        r = eng.run(make_fixture(1200, '1h', seed=5, kind='up'),
                    data_quality=0.95)
        self.assertGreater(len(r.trades), 0, 'لم تُفتَح صفقة — الاختبار أجوف')
        self.assertIsNotNone(seen.get('account'),
                             'الباكتست يحجّم بلا لقطة حساب — انحراف عن الحيّ')
        self.assertGreater(seen.get('fee_rate') or 0, 0,
                           'الباكتست يحجّم بلا رسوم — انحراف عن الحيّ')

    def test_reserve_is_respected_across_a_whole_backtest(self):
        """كل مركز مفتوح في الباكتست يحترم الاحتياطي، لا أوّله فقط."""
        from tests.fixtures import make_fixture
        cfg = self._trading_cfg()
        eng = BacktestEngine(cfg, initial_capital=10000)
        r = eng.run(make_fixture(1200, '1h', seed=5, kind='up'),
                    data_quality=0.95)
        self.assertGreater(len(r.trades), 0)
        # لا صفقة تتجاوز قيمتها المتاح بعد الاحتياطي عند لحظة فتحها
        for t in r.trades:
            entry_equity = t.get('equity_before') or 10000.0
            cap = entry_equity * (1 - cfg.risk.min_cash_reserve_pct / 100)
            value = (t.get('qty') or 0) * (t.get('entry_price') or 0)
            self.assertLessEqual(value, cap + 1e-6,
                                 f'صفقة تجاوزت المتاح بعد الاحتياطي: {value}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
