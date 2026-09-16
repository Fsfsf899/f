"""
اختبارات خطة التحجيم ونقاط حالة الحساب —
الأقسام 13 و14 و25 و34 من مواصفة V11 FINAL.
"""
import os
import sys
import json
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.account.state import AccountSnapshot, UNAVAILABLE, STALE
from src.account.providers import ExchangeAccountProvider
from src.account.sizing_plan import build_sizing_plan, SizingPlan
from src.risk.position_sizing import PositionSizer
from src.backtest.costs import CostModel
from src.storage.database import Database
from src.core.config import Config

ENTRY, STOP, TARGET = 50000.0, 49000.0, 53000.0

# الحقول التي تفرضها المواصفة صراحةً (القسمان 13 و25)
REQUIRED_FIELDS = [
    'symbol', 'decision', 'reason', 'account',
    'base_risk_pct', 'effective_risk_pct', 'max_risk_amount',
    'entry', 'stop', 'target', 'stop_distance_pct', 'risk_per_unit',
    'risk_reward', 'raw_quantity', 'final_quantity', 'position_value',
    'estimated_entry_fee', 'estimated_exit_fee', 'slippage_bps',
    'spread_bps', 'estimated_max_loss', 'max_position_value',
    'capped_by_balance', 'remaining_available', 'actual_risk_pct',
    'explanation',
]


class _Client:
    def __init__(self, free=10000.0, locked=0.0):
        self._free, self._locked = free, locked

    def account(self):
        return {'canTrade': True}

    def balances(self):
        return {'USDT': {'free': self._free, 'locked': self._locked}}

    def rules(self, symbol):
        return {'base': symbol.replace('USDT', ''), 'quote': 'USDT',
                'step_size': 0.00001, 'min_qty': 0.00001, 'min_notional': 10.0}

    def price(self, symbol):
        return ENTRY


class _Sig:
    def __init__(self, entry=ENTRY, stop=STOP, target=TARGET, stars=3):
        self.entry, self.stop_loss, self.take_profit = entry, stop, target
        self.stars = stars
        self.net_risk_reward = 2.5
        self.risk_reward = 2.5


def _cfg(reserve_pct=10.0):
    c = Config()
    c.risk.min_cash_reserve_pct = reserve_pct
    return c


def _plan(free=10000.0, signal=None, cfg=None, max_notional=0.0):
    c = cfg or _cfg()
    snap = ExchangeAccountProvider(c.risk, _Client(free=free)).snapshot()
    sizer = PositionSizer(c.risk, CostModel(c.costs))
    return build_sizing_plan(
        symbol='BTCUSDT', sizer=sizer, account=snap, cfg=c,
        signal=signal, rules=_Client().rules('BTCUSDT'),
        max_notional=max_notional), snap


class Test01_PlanShape(unittest.TestCase):
    """القسم 13: كل حقل تفرضه المواصفة موجود فعلاً."""

    def test_all_required_fields_present(self):
        p, _ = _plan(signal=_Sig())
        d = p.to_dict()
        missing = [f for f in REQUIRED_FIELDS if f not in d]
        self.assertEqual(missing, [], f'حقول مفقودة: {missing}')

    def test_enter_decision_has_real_numbers(self):
        p, _ = _plan(signal=_Sig())
        self.assertEqual(p.decision, 'ENTER')
        self.assertGreater(p.final_quantity, 0)
        self.assertGreater(p.position_value, 0)
        self.assertGreater(p.estimated_max_loss, 0)

    def test_explanation_is_never_empty(self):
        for sig in (_Sig(), None):
            p, _ = _plan(signal=sig)
            self.assertTrue(p.explanation, 'لا تفسير — القسم 25 يوجبه')


class Test02_NoSignalIsAnswerable(unittest.TestCase):
    """«كم أستطيع أن أدخل؟» له جواب حتى بلا إشارة قائمة."""

    def test_no_signal_still_reports_balance_and_risk(self):
        p, snap = _plan(free=1000.0, signal=None)
        self.assertEqual(p.decision, 'NO_TRADE')
        self.assertEqual(p.reason, 'NO_SIGNAL')
        self.assertAlmostEqual(p.max_risk_amount, 900.0 * 0.01, places=6)
        self.assertGreater(len(p.explanation), 1)

    def test_reserve_is_explained_in_plain_language(self):
        p, _ = _plan(free=1000.0, signal=None)
        joined = ' '.join(p.explanation)
        self.assertIn('احتياطي', joined)


class Test03_BlockedPlansExplainWhy(unittest.TestCase):
    """القسم 25: عند المنع يُشرح المانع، لا تُعرَض أصفار بلا سبب."""

    def _blocked(self, snap):
        c = _cfg()
        return build_sizing_plan(
            symbol='BTCUSDT', sizer=PositionSizer(c.risk, CostModel(c.costs)),
            account=snap, cfg=c, signal=_Sig(),
            rules=_Client().rules('BTCUSDT'))

    def test_unavailable_balance(self):
        p = self._blocked(AccountSnapshot(status=UNAVAILABLE))
        self.assertEqual(p.decision, 'NO_TRADE')
        self.assertEqual(p.reason, 'ACCOUNT_BALANCE_UNAVAILABLE')
        self.assertTrue(any('تعذّر' in x for x in p.explanation))

    def test_stale_balance(self):
        p = self._blocked(AccountSnapshot(status=STALE, available_balance=100.0))
        self.assertEqual(p.reason, 'ACCOUNT_BALANCE_STALE')
        self.assertTrue(p.explanation)

    def test_exchange_minimum_is_explained(self):
        c = _cfg()
        snap = ExchangeAccountProvider(c.risk, _Client(free=20.0)).snapshot()
        p = build_sizing_plan(
            symbol='BTCUSDT', sizer=PositionSizer(c.risk, CostModel(c.costs)),
            account=snap, cfg=c, signal=_Sig(),
            rules={'step_size': 0.00001, 'min_qty': 0.0, 'min_notional': 5000.0})
        self.assertEqual(p.reason, 'EXCHANGE_MINIMUM_EXCEEDS_RISK_LIMIT')
        self.assertTrue(any('لا يُقرَّب' in x or 'الحد الأدنى' in x
                            for x in p.explanation))


class Test04_NumbersComeFromTheSizer(unittest.TestCase):
    """
    القسم 25: الأرقام من PositionSizer الكنسي لا من حساب موازٍ.
    نُثبتها بمقارنة الخطة بنتيجة المحجِّم مباشرةً.
    """

    def test_plan_matches_sizer_output_exactly(self):
        c = _cfg()
        snap = ExchangeAccountProvider(c.risk, _Client(free=10000.0)).snapshot()
        sizer = PositionSizer(c.risk, CostModel(c.costs))
        rules = _Client().rules('BTCUSDT')
        sz = sizer.calculate(
            equity=snap.usable_equity, entry=ENTRY, stop=STOP, stars=3,
            consecutive_losses=0, account=snap,
            step_size=rules['step_size'], min_qty=rules['min_qty'],
            min_notional=rules['min_notional'], fee_rate=c.costs.taker_fee)
        p = build_sizing_plan(symbol='BTCUSDT', sizer=sizer, account=snap,
                              cfg=c, signal=_Sig(), rules=rules)
        self.assertAlmostEqual(p.final_quantity, sz['qty'], places=8)
        self.assertAlmostEqual(p.position_value, sz['notional'], places=6)
        self.assertAlmostEqual(p.actual_risk_pct, sz['actual_risk_pct'],
                               places=6)

    def test_max_loss_never_exceeds_allowed_risk_materially(self):
        c = _cfg()
        p, snap = _plan(free=10000.0, signal=_Sig(), cfg=c)
        # الخسارة القصوى = مسافة الوقف + رسوم الجهتين؛ يجب أن تبقى
        # قريبة من المخاطرة المسموحة لا أضعافها
        self.assertLess(p.estimated_max_loss, p.max_risk_amount * 1.5)


class Test05_Persistence(unittest.TestCase):
    """اللوحة تقرأ من القاعدة — فالخطة يجب أن تُخزَّن وتُقرأ سليمة."""

    def setUp(self):
        self.db = Database(os.path.join(tempfile.mkdtemp(), 's.db'))

    def test_plan_round_trips(self):
        p, _ = _plan(signal=_Sig())
        rid = self.db.save_sizing_plan(p)
        self.assertGreater(rid, 0)
        back = self.db.latest_sizing_plan()
        self.assertIsNotNone(back)
        self.assertEqual(back['symbol'], 'BTCUSDT')
        self.assertEqual(back['decision'], 'ENTER')
        self.assertAlmostEqual(back['position_value'], p.position_value, places=6)

    def test_explanation_survives_storage(self):
        p, _ = _plan(signal=_Sig())
        self.db.save_sizing_plan(p)
        back = self.db.latest_sizing_plan()
        self.assertEqual(back['plan']['explanation'], p.explanation)

    def test_blocked_plan_is_also_stored(self):
        """أهم ما يريد المستخدم معرفته: **لماذا لم يدخل النظام**."""
        c = _cfg()
        p = build_sizing_plan(
            symbol='ETHUSDT', sizer=PositionSizer(c.risk, CostModel(c.costs)),
            account=AccountSnapshot(status=UNAVAILABLE), cfg=c, signal=None)
        self.db.save_sizing_plan(p)
        back = self.db.latest_sizing_plan()
        self.assertEqual(back['decision'], 'NO_TRADE')
        self.assertEqual(back['reason'], 'ACCOUNT_BALANCE_UNAVAILABLE')

    def test_stored_plan_has_no_secrets(self):
        p, _ = _plan(signal=_Sig())
        self.db.save_sizing_plan(p)
        blob = json.dumps(self.db.latest_sizing_plan(), default=str).lower()
        for bad in ('api_key', 'secret', 'signature', 'apikey', 'fingerprint'):
            self.assertNotIn(bad, blob)


class Test06_FrontendDoesNotCompute(unittest.TestCase):
    """
    القسم 25 صريح: الأرقام من الخلفية لا من حساب في الواجهة.
    حارس نصّي على صفحة التحجيم وحدها — أي ضرب/قسمة فيها يجعل اللوحة
    مصدر حقيقة ثانياً يخالف الخلفية بصمت.
    """

    def test_sizing_page_has_no_arithmetic(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), 'dashboard', 'frontend', 'assets',
            'app.js')
        src = open(path, encoding='utf-8').read()
        start = src.index('PAGES.sizing')
        end = src.index('PAGES.opportunity')
        body = src[start:end]
        import re
        # تُزال النصوص الحرفية أولاً: `paper/testnet` داخل رسالة ليست
        # قسمة، وحارس يسقط عليها حارس هشّ لا حارس حقيقي.
        no_str = re.sub(r"'[^'\n]*'|\"[^\"\n]*\"|`(?:[^`\\\\]|\\\\.)*`",
                        "''", body, flags=re.S)
        # `* 1000` تحويل ثوانٍ إلى ميلي ثانية لدالة عرض — لا حساب مالي
        no_str = no_str.replace('* 1000', '').replace('*1000', '')
        bad = re.findall(r'[a-zA-Z_\]\)]\s*[*/]\s*[a-zA-Z0-9_(]', no_str)
        self.assertEqual(bad, [],
                         f'حساب في الواجهة يخالف القسم 25: {bad}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
