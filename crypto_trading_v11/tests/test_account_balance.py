"""
اختبارات حالة الحساب والتحجيم المرتبط بالرصيد —
الأقسام 3-11 و15-17 و28 من مواصفة V11 FINAL.
"""
import os
import sys
import time
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.account.state import (AccountSnapshot, compute_reserve,
                               OK, STALE, UNAVAILABLE, RESTRICTED,
                               R_UNAVAILABLE, R_STALE, R_INSUFFICIENT)
from src.account.providers import PaperAccountProvider, ExchangeAccountProvider
from src.risk.position_sizing import PositionSizer
from src.backtest.costs import CostModel
from src.core.config import Config

ENTRY, STOP = 50000.0, 49000.0


class _Client:
    """منصة مُصادَقة وهمية — تحاكي أشكال ردود بينانس وأعطالها."""

    def __init__(self, free=10000.0, locked=0.0, can_trade=True,
                 fail=None, balances=None, quote='USDT'):
        self._free, self._locked = free, locked
        self._can_trade, self._fail = can_trade, fail
        self._balances = balances
        self._quote = quote

    def account(self):
        if self._fail == 'account':
            raise RuntimeError('endpoint down')
        if self._fail == 'malformed_account':
            return 'not-a-dict'
        return {'canTrade': self._can_trade}

    def balances(self):
        if self._fail == 'balances':
            raise RuntimeError('balances down')
        if self._balances is not None:
            return self._balances
        return {self._quote: {'free': self._free, 'locked': self._locked}}

    def rules(self, symbol):
        return {'base': symbol.replace('USDT', ''), 'quote': 'USDT',
                'step_size': 0.00001, 'min_qty': 0.00001, 'min_notional': 10.0}

    def price(self, symbol):
        return ENTRY


class _Broker(_Client):
    pass


def _cfg(reserve_pct=0.0, reserve_flat=0.0, max_age=60.0):
    c = Config()
    c.risk.min_cash_reserve_pct = reserve_pct
    c.risk.min_cash_reserve_quote = reserve_flat
    c.risk.max_balance_age_s = max_age
    return c


class Test01_BalanceParsing(unittest.TestCase):
    """القسم 3: تمييز free/locked/total والأصول المنفردة."""

    def test_free_and_locked_are_distinguished(self):
        c = _cfg()
        p = ExchangeAccountProvider(c.risk, _Client(free=800.0, locked=200.0))
        s = p.snapshot()
        self.assertEqual(s.status, OK)
        self.assertAlmostEqual(s.available_balance, 800.0)
        self.assertAlmostEqual(s.locked_balance, 200.0)
        self.assertAlmostEqual(s.total_balance, 1000.0)

    def test_locked_is_never_usable(self):
        """المحظور الصريح في القسم 32: المحجوز ليس متاحاً."""
        c = _cfg()
        p = ExchangeAccountProvider(c.risk, _Client(free=100.0, locked=900.0))
        s = p.snapshot()
        self.assertAlmostEqual(s.usable_equity, 100.0,
                               msg='المحجوز دخل الحقوق القابلة للاستخدام')

    def test_per_asset_balances_are_kept(self):
        c = _cfg()
        p = ExchangeAccountProvider(c.risk, _Client(balances={
            'USDT': {'free': 500.0, 'locked': 0.0},
            'BTC': {'free': 0.01, 'locked': 0.0}}))
        s = p.snapshot()
        self.assertIn('BTC', s.assets)
        self.assertAlmostEqual(s.assets['BTC']['free'], 0.01)

    def test_position_value_uses_real_prices_no_double_count(self):
        """القسم 5: لا ازدواج حساب بين النقد وقيمة المراكز."""
        c = _cfg()
        p = ExchangeAccountProvider(c.risk, _Client(balances={
            'USDT': {'free': 500.0, 'locked': 0.0},
            'BTC': {'free': 0.01, 'locked': 0.0}}))
        s = p.snapshot(['BTCUSDT'])
        self.assertAlmostEqual(s.total_balance, 500.0)
        self.assertAlmostEqual(s.position_value, 0.01 * ENTRY)
        self.assertAlmostEqual(s.total_equity, 500.0 + 0.01 * ENTRY)


class Test02_FailClosed(unittest.TestCase):
    """القسم 17: أي تعذّر ⇒ لا تداول. لا تخمين ولا رصيد أخير."""

    def _blocked(self, client, cfg=None):
        p = ExchangeAccountProvider((cfg or _cfg()).risk, client)
        return p.snapshot()

    def test_endpoint_failure(self):
        s = self._blocked(_Client(fail='account'))
        self.assertEqual(s.status, UNAVAILABLE)
        self.assertEqual(s.blocking_reason(), R_UNAVAILABLE)
        self.assertEqual(s.usable_equity, 0.0)

    def test_balances_failure(self):
        s = self._blocked(_Client(fail='balances'))
        self.assertEqual(s.status, UNAVAILABLE)

    def test_malformed_response(self):
        s = self._blocked(_Client(fail='malformed_account'))
        self.assertEqual(s.status, UNAVAILABLE)

    def test_missing_quote_balance_is_not_assumed_zero(self):
        s = self._blocked(_Client(balances={'BTC': {'free': 1.0, 'locked': 0.0}}))
        self.assertEqual(s.status, UNAVAILABLE)
        self.assertIn('QUOTE_BALANCE_MISSING', s.reasons)

    def test_key_cannot_trade_is_restricted(self):
        s = self._blocked(_Client(can_trade=False))
        self.assertEqual(s.status, RESTRICTED)

    def test_negative_balance_rejected(self):
        s = self._blocked(_Client(free=-5.0))
        self.assertEqual(s.status, UNAVAILABLE)
        self.assertIn('NEGATIVE_BALANCE', s.reasons)

    def test_unavailable_snapshot_blocks_sizer(self):
        sizer = PositionSizer(Config().risk, CostModel(Config().costs))
        bad = AccountSnapshot(status=UNAVAILABLE)
        r = sizer.calculate(equity=10000.0, entry=ENTRY, stop=STOP, account=bad)
        self.assertEqual(r['qty'], 0.0)
        self.assertEqual(r['reason'], R_UNAVAILABLE)


class Test03_Freshness(unittest.TestCase):
    """القسم 6: كل قرار تحجيم يستند إلى لقطة ذات عمر معلوم."""

    def test_fresh_snapshot_is_ok(self):
        p = ExchangeAccountProvider(_cfg(max_age=60).risk, _Client())
        self.assertEqual(p.snapshot().status, OK)

    def test_stale_snapshot_is_rejected(self):
        p = ExchangeAccountProvider(_cfg(max_age=0.0).risk, _Client())
        s = p.snapshot()
        time.sleep(0.01)
        s2 = p.snapshot()
        self.assertEqual(s2.status, STALE)
        self.assertEqual(s2.blocking_reason(), R_STALE)

    def test_stale_snapshot_blocks_sizer(self):
        sizer = PositionSizer(Config().risk, CostModel(Config().costs))
        stale = AccountSnapshot(status=STALE, available_balance=10000.0)
        r = sizer.calculate(equity=10000.0, entry=ENTRY, stop=STOP, account=stale)
        self.assertEqual(r['qty'], 0.0)
        self.assertEqual(r['reason'], R_STALE)

    def test_snapshot_without_timestamp_is_infinitely_old(self):
        self.assertEqual(AccountSnapshot().age_seconds, float('inf'))


class Test04_Reserve(unittest.TestCase):
    """القسم 8: الاحتياطي يُقتطَع قبل التحجيم، لا بعده."""

    def test_percentage_reserve(self):
        p = ExchangeAccountProvider(_cfg(reserve_pct=10.0).risk,
                                    _Client(free=1000.0))
        s = p.snapshot()
        self.assertAlmostEqual(s.reserve_amount, 100.0)
        self.assertAlmostEqual(s.usable_equity, 900.0)

    def test_flat_reserve(self):
        p = ExchangeAccountProvider(_cfg(reserve_flat=250.0).risk,
                                    _Client(free=1000.0))
        self.assertAlmostEqual(p.snapshot().usable_equity, 750.0)

    def test_larger_of_the_two_wins(self):
        self.assertAlmostEqual(
            compute_reserve(1000.0, _cfg(reserve_pct=10.0, reserve_flat=250.0).risk),
            250.0)

    def test_reserve_consuming_everything_blocks_trade(self):
        p = ExchangeAccountProvider(_cfg(reserve_flat=1000.0).risk,
                                    _Client(free=1000.0))
        s = p.snapshot()
        self.assertEqual(s.usable_equity, 0.0)
        self.assertEqual(s.blocking_reason(), R_INSUFFICIENT)

    def test_reserve_actually_remains_after_sizing(self):
        """الاختبار الحقيقي: الاحتياطي باقٍ فعلاً بعد الأمر المقترح."""
        cfg = _cfg(reserve_pct=10.0)
        p = ExchangeAccountProvider(cfg.risk, _Client(free=1000.0))
        snap = p.snapshot()
        sizer = PositionSizer(cfg.risk, CostModel(cfg.costs))
        r = sizer.calculate(equity=0, entry=ENTRY, stop=STOP, account=snap,
                            fee_rate=cfg.costs.taker_fee, min_notional=10.0)
        spent = r['notional'] + r.get('estimated_entry_fee', 0.0)
        self.assertLessEqual(spent, 900.0 + 1e-6,
                             'أُنفق من الاحتياطي')


class Test05_BalanceAwareSizing(unittest.TestCase):
    """الأقسام 7 و10: الرصيد يحدّ الأمر، والمخاطرة تُعاد حسابها."""

    def _sizer(self, cfg=None):
        c = cfg or _cfg()
        return PositionSizer(c.risk, CostModel(c.costs)), c

    def test_position_never_exceeds_usable_balance(self):
        sizer, c = self._sizer()
        snap = ExchangeAccountProvider(c.risk, _Client(free=100.0)).snapshot()
        r = sizer.calculate(equity=0, entry=ENTRY, stop=STOP, account=snap,
                            fee_rate=c.costs.taker_fee)
        self.assertLessEqual(r['notional'] + r.get('estimated_entry_fee', 0),
                             100.0 + 1e-6)

    def test_balance_cap_binds_when_notional_cap_allows_more(self):
        """
        سقف الرصيد شبكة أمان أخيرة. بالإعداد الافتراضي
        (`max_position_notional_pct=25`) لا يُفعَّل أبداً لأن سقف
        القيمة الاسمية أضيق منه — وهذا مقصود. نرفع سقف القيمة الاسمية
        فوق 100% لنُثبت أن الشبكة تعمل فعلاً عند الحاجة إليها، بدل
        افتراض أنها تعمل لأنها مكتوبة.
        """
        c = _cfg()
        c.risk.max_position_notional_pct = 500.0
        sizer = PositionSizer(c.risk, CostModel(c.costs))
        snap = ExchangeAccountProvider(c.risk, _Client(free=1000.0)).snapshot()
        # وقف ضيّق جداً ⇒ كمية ضخمة ⇒ قيمة تتجاوز المتاح بلا هذا السقف
        r = sizer.calculate(equity=0, entry=ENTRY, stop=ENTRY * 0.999,
                            account=snap, fee_rate=c.costs.taker_fee)
        self.assertTrue(r.get('capped_by_balance'),
                        'سقف الرصيد لم يُفعَّل رغم تجاوز القيمة للمتاح')
        self.assertLessEqual(r['notional'] + r.get('estimated_entry_fee', 0),
                             1000.0 + 1e-6)

    def test_actual_risk_is_recalculated_after_rounding(self):
        sizer, c = self._sizer()
        snap = ExchangeAccountProvider(c.risk, _Client(free=10000.0)).snapshot()
        r = sizer.calculate(equity=0, entry=ENTRY, stop=STOP, account=snap,
                            step_size=0.00001, fee_rate=c.costs.taker_fee)
        self.assertIn('actual_risk_pct', r)
        self.assertLessEqual(r['actual_risk_pct'],
                             c.risk.max_risk_per_trade_pct + 1e-9)

    def test_exchange_minimum_exceeding_risk_blocks(self):
        """القسم 10: لا تقريب لأعلى لإرضاء الحد الأدنى."""
        sizer, c = self._sizer()
        snap = ExchangeAccountProvider(c.risk, _Client(free=20.0)).snapshot()
        r = sizer.calculate(equity=0, entry=ENTRY, stop=STOP, account=snap,
                            min_notional=5000.0, fee_rate=c.costs.taker_fee)
        self.assertEqual(r['qty'], 0.0)
        self.assertEqual(r['reason'], 'EXCHANGE_MINIMUM_EXCEEDS_RISK_LIMIT')

    def test_never_rounds_up(self):
        sizer, c = self._sizer()
        snap = ExchangeAccountProvider(c.risk, _Client(free=10000.0)).snapshot()
        r = sizer.calculate(equity=0, entry=ENTRY, stop=STOP, account=snap,
                            step_size=0.01, fee_rate=c.costs.taker_fee)
        if r['qty'] > 0:
            self.assertAlmostEqual(r['qty'] % 0.01, 0.0, places=9)

    def test_no_account_keeps_legacy_behavior(self):
        """توافق خلفي: بلا لقطة، السلوك القديم حرفياً."""
        sizer, c = self._sizer()
        a = sizer.calculate(equity=10000.0, entry=ENTRY, stop=STOP)
        self.assertGreater(a['notional'], 0)
        self.assertIsNone(a.get('blocked'))


class Test06_PaperIsolation(unittest.TestCase):
    """القسم 15: Paper بلا شبكة، وبنفس العقد."""

    def test_paper_provider_uses_broker_only(self):
        p = PaperAccountProvider(_cfg().risk, _Broker(free=5000.0, locked=100.0))
        s = p.snapshot()
        self.assertEqual(s.status, OK)
        self.assertEqual(s.source, 'paper')
        self.assertAlmostEqual(s.available_balance, 5000.0)
        self.assertAlmostEqual(s.locked_balance, 100.0)

    def test_paper_and_exchange_share_the_contract(self):
        """نفس المفاتيح تماماً — فيستهلكهما PositionSizer بلا فرع."""
        a = PaperAccountProvider(_cfg().risk, _Broker()).snapshot().to_dict()
        b = ExchangeAccountProvider(_cfg().risk, _Client()).snapshot().to_dict()
        self.assertEqual(sorted(a), sorted(b))

    def test_paper_provider_never_touches_network(self):
        class Exploding(_Broker):
            def account(self):
                raise AssertionError('Paper لمس الشبكة عبر account()')
        p = PaperAccountProvider(_cfg().risk, Exploding(free=1000.0))
        self.assertEqual(p.snapshot().status, OK)

    def test_snapshot_dict_contains_no_secrets(self):
        d = ExchangeAccountProvider(_cfg().risk, _Client()).snapshot().to_dict()
        blob = repr(d).lower()
        for bad in ('api_key', 'secret', 'signature', 'apikey'):
            self.assertNotIn(bad, blob)


if __name__ == '__main__':
    unittest.main(verbosity=2)
