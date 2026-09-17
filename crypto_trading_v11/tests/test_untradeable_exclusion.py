"""
البند 11: «An asset that cannot actually be traded with the current
account must not be selected as the best opportunity.»

البق: `est <= 0` تعني أن `PositionSizer` رفض تحجيم الأصل (رصيد غير
كافٍ، أو حد أدنى للمنصة يتجاوز المخاطرة، أو حساب غير متاح). وكان
`break` يُبقيه **فائزاً بالمسح** رغم استحالة تداوله — فتفشل الصفقة
لاحقاً في خطوة التحجيم ويُرجع النظام NO_TRADE، بينما مرشَّح قابل
للتداول فعلاً كان متاحاً في القائمة.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.selection.opportunity import (scan_and_rank, R_NOT_SIZEABLE,
                                       SUPPORTED_ASSETS)
from src.risk.portfolio import PortfolioRisk


class _Sig:
    def __init__(self, entry, stop, score=8.0):
        self.entry, self.stop_loss = entry, stop
        self.take_profit = entry * 1.05
        self.decision = 'BUY'
        self.score = score
        self.confidence = 0.8
        self.calibrated_probability = None
        self.net_risk_reward = 2.5
        self.risk_reward = 2.5
        self.data_quality = 1.0
        self.btc_context = 'LOW'
        self.stars = 4
        self.reasons = []

    def to_dict(self):
        return {}


class _Engine:
    def __init__(self, entry, stop, score=8.0):
        self.sig = _Sig(entry, stop, score)

    def evaluate(self, data, idx, **kw):
        return self.sig


def _scan(prices, notional_fn, symbols=None):
    symbols = symbols or list(prices)
    engines = {s: _Engine(p, p * 0.98, score=sc)
               for s, (p, sc) in prices.items()}
    return scan_and_rank(
        symbols, engines=engines,
        data_by_symbol={s: object() for s in symbols},
        idx_by_symbol={s: 0 for s in symbols},
        data_quality_by_symbol={s: 1.0 for s in symbols},
        portfolio=PortfolioRisk(), equity=10_000.0, open_notional={},
        notional_fn=notional_fn)


class Test01_UnsizeableIsSkipped(unittest.TestCase):

    def test_next_candidate_wins_instead(self):
        """الأعلى درجةً غير قابل للتحجيم ⇒ يفوز التالي، لا NO_TRADE."""
        r = _scan({'BTCUSDT': (50000.0, 9.0), 'ETHUSDT': (3000.0, 7.0)},
                  lambda s, sig: 0.0 if s == 'BTCUSDT' else 200.0)
        self.assertEqual(r.decision, 'BUY')
        self.assertEqual(r.selected_symbol, 'ETHUSDT',
                         'اختار أصلاً لا يمكن تداوله — يخالف البند 11')

    def test_rejected_symbol_is_marked_with_reason(self):
        r = _scan({'BTCUSDT': (50000.0, 9.0), 'ETHUSDT': (3000.0, 7.0)},
                  lambda s, sig: 0.0 if s == 'BTCUSDT' else 200.0)
        btc = next(o for o in r.opportunities if o.symbol == 'BTCUSDT')
        self.assertFalse(btc.eligible)
        self.assertIn(R_NOT_SIZEABLE, btc.rejection_reasons)

    def test_all_unsizeable_gives_explicit_no_trade(self):
        r = _scan({'BTCUSDT': (50000.0, 9.0), 'ETHUSDT': (3000.0, 7.0)},
                  lambda s, sig: 0.0)
        self.assertEqual(r.decision, 'NO_TRADE')
        self.assertEqual(r.reason, R_NOT_SIZEABLE,
                         'سبب غامض بدل السبب الحقيقي')
        self.assertIsNone(r.selected_symbol)

    def test_skips_more_than_one(self):
        """يتخطّى سلسلة غير قابلة للتحجيم حتى يجد قابلاً."""
        r = _scan({'BTCUSDT': (50000.0, 9.0), 'ETHUSDT': (3000.0, 8.0),
                   'BNBUSDT': (600.0, 7.0), 'SOLUSDT': (150.0, 6.0)},
                  lambda s, sig: 200.0 if s == 'SOLUSDT' else 0.0)
        self.assertEqual(r.selected_symbol, 'SOLUSDT')

    def test_negative_estimate_treated_as_unsizeable(self):
        r = _scan({'BTCUSDT': (50000.0, 9.0), 'ETHUSDT': (3000.0, 7.0)},
                  lambda s, sig: -5.0 if s == 'BTCUSDT' else 200.0)
        self.assertEqual(r.selected_symbol, 'ETHUSDT')


class Test02_NoRegression(unittest.TestCase):
    """السلوك القائم لا يتغيَّر حين تكون كل الأصول قابلة للتداول."""

    def test_highest_score_still_wins(self):
        r = _scan({'BTCUSDT': (50000.0, 9.0), 'ETHUSDT': (3000.0, 7.0)},
                  lambda s, sig: 200.0)
        self.assertEqual(r.selected_symbol, 'BTCUSDT')

    def test_missing_estimate_still_skips_the_check(self):
        """
        `None` ≠ `0`: غياب التقدير يعني «لا فحص متاح» ويُتخطّى صراحةً
        كما هو موثَّق — لا يُعامَل كرفض. التمييز مقصود.
        """
        r = _scan({'BTCUSDT': (50000.0, 9.0), 'ETHUSDT': (3000.0, 7.0)},
                  lambda s, sig: None)
        self.assertEqual(r.decision, 'BUY')
        self.assertEqual(r.selected_symbol, 'BTCUSDT')

    def test_no_notional_fn_at_all_is_unchanged(self):
        engines = {s: _Engine(100.0, 98.0) for s in SUPPORTED_ASSETS[:2]}
        r = scan_and_rank(
            SUPPORTED_ASSETS[:2], engines=engines,
            data_by_symbol={s: object() for s in SUPPORTED_ASSETS[:2]},
            idx_by_symbol={s: 0 for s in SUPPORTED_ASSETS[:2]},
            data_quality_by_symbol={s: 1.0 for s in SUPPORTED_ASSETS[:2]})
        self.assertEqual(r.decision, 'BUY')


if __name__ == '__main__':
    unittest.main(verbosity=2)
