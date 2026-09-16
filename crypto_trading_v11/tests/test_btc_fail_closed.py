"""
اختبارات القسم 12 من متطلبات V11 Final Patch: "missing BTC data ->
NO TRADE... Do not invent BTC values."

كان `NoTradeEngine.check()` يمنع التداول فقط عندما تكون بيانات BTC
**متوفرة وسيئة** (`btc_available and btc_ok is False`) — غيابها
الكامل كان يمر بصمت تام، رغم أن `BTCContext.evaluate()` نفسها تُعيد
`trend_ok=True` كقيمة افتراضية للحقل عند غياب البيانات (إيجابي صامت).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.signals.no_trade import NoTradeEngine, R
from src.core.config import NoTradeConfig, RiskConfig
from src.market.btc_context import evaluate as btc_evaluate, BTCContext


def base_kwargs():
    return dict(data_quality=0.95, bars_available=300, warmup=200,
               atr_pct=1.0, regime_long_friendly=True, resistance_distance_pct=2.0,
               stop_distance_pct=1.5, risk_reward=2.0, min_rr=1.5,
               score=6.0, min_score=4.0)


class Test01_BTCUnavailableFailsClosed(unittest.TestCase):
    def test_missing_btc_blocks_when_required(self):
        eng = NoTradeEngine(NoTradeConfig(require_btc_ok=True), RiskConfig())
        v = eng.check(**base_kwargs(), btc_ok=None, btc_available=False)
        self.assertFalse(v.allowed, 'BTC غائب تماماً لكن التداول لم يُمنَع')
        self.assertIn(R['BTC_UNAVAILABLE'], v.reasons)

    def test_missing_btc_does_not_block_when_not_required(self):
        """require_btc_ok=False صراحة يجب أن يُعطِّل الفحص كاملاً، لا يُفرَض دائماً."""
        eng = NoTradeEngine(NoTradeConfig(require_btc_ok=False), RiskConfig())
        v = eng.check(**base_kwargs(), btc_ok=None, btc_available=False)
        self.assertTrue(v.allowed)
        self.assertNotIn(R['BTC_UNAVAILABLE'], v.reasons)

    def test_available_and_good_btc_allows(self):
        eng = NoTradeEngine(NoTradeConfig(require_btc_ok=True), RiskConfig())
        v = eng.check(**base_kwargs(), btc_ok=True, btc_available=True)
        self.assertTrue(v.allowed)

    def test_available_and_bad_btc_still_blocks_with_distinct_reason(self):
        """التمييز محفوظ: 'غائب' و'متاح لكن سيء' سببان مختلفان، لا يُدمَجان."""
        eng = NoTradeEngine(NoTradeConfig(require_btc_ok=True), RiskConfig())
        v = eng.check(**base_kwargs(), btc_ok=False, btc_available=True)
        self.assertFalse(v.allowed)
        self.assertIn(R['BTC_RISK'], v.reasons)
        self.assertNotIn(R['BTC_UNAVAILABLE'], v.reasons)


class Test02_BTCContextDefaultDoesNotInventFavorableValue(unittest.TestCase):
    """
    BTCContext.evaluate() نفسها تُعيد trend_ok=True (القيمة الافتراضية
    للحقل) عند غياب البيانات — هذا الاختبار يوثِّق هذا السلوك صراحة
    (لا يُصلحه، فالمصدر الصحيح للإصلاح هو بوابة القرار لا الحقل
    الافتراضي نفسه) كي لا يُفترَض خطأً أن BTCContext وحدها كافية.
    """

    def test_none_btc_data_returns_unavailable_with_default_trend_ok_true(self):
        ctx = btc_evaluate(None, 0)
        self.assertFalse(ctx.available)
        # القيمة الافتراضية للحقل — موثَّقة هنا صراحة، لا مفاجئة لاحقاً
        self.assertTrue(ctx.trend_ok)

    def test_gate_correctly_ignores_available_false_context_trend_ok(self):
        """
        التحقق من التكامل الكامل: تمرير BTCContext(available=False) —
        مطابقاً لما تُنتجه evaluate() فعلياً عند غياب البيانات — يجب أن
        يُمنَع رغم trend_ok=True الافتراضي داخل الكائن نفسه.
        """
        ctx = BTCContext(available=False)   # trend_ok=True افتراضياً
        eng = NoTradeEngine(NoTradeConfig(require_btc_ok=True), RiskConfig())
        v = eng.check(**base_kwargs(), btc_ok=ctx.trend_ok if ctx.available else None,
                      btc_available=ctx.available)
        self.assertFalse(v.allowed,
                         'trend_ok الافتراضي True تسرَّب عبر البوابة رغم availability=False')


if __name__ == '__main__':
    unittest.main(verbosity=2)
