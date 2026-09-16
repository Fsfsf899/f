"""
اختبارات القسم 6 من متطلبات V11 Final: "لا إعدادات ميتة" —
`min_confidence`/`min_probability` كانا معرَّفين في Config بلا أي
استخدام فعلي. الفحص الحقيقي كان يقارن `confidence` بقيمة مُشتقة
عشوائياً (`min_data_quality * 0.6`)، و`probability` برقم مكتوب حرفياً
(`0.40`) يطابق القيمة الافتراضية لـ `min_probability` بالصدفة فقط —
أي تغيير مستقبلي في الإعداد كان سيُهمَل تماماً.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.signals.no_trade import NoTradeEngine, R
from src.core.config import NoTradeConfig, RiskConfig, Config
from src.signals.engine import SignalEngine, BUY, WAIT, NO_TRADE
from tests.fixtures import make_fixture


def base_kwargs():
    return dict(data_quality=0.95, bars_available=300, warmup=200,
               atr_pct=1.0, regime_long_friendly=True, resistance_distance_pct=2.0,
               stop_distance_pct=1.5, risk_reward=2.0, min_rr=1.5,
               score=6.0, min_score=4.0,
               # القسم 12 من متطلبات V11 Final Patch: BTC غائب يعني
               # NO TRADE الآن — سياق "نظيف" حقيقي يتطلب توفّره صراحة.
               btc_available=True, btc_ok=True)


class Test01_MinConfidenceNoLongerDead(unittest.TestCase):
    def test_high_threshold_blocks_signal_that_would_otherwise_pass(self):
        eng = NoTradeEngine(NoTradeConfig(), RiskConfig())
        v_low = eng.check(**base_kwargs(), confidence=0.5, min_confidence=0.3)
        v_high = eng.check(**base_kwargs(), confidence=0.5, min_confidence=0.9)
        self.assertTrue(v_low.allowed)
        self.assertFalse(v_high.allowed)
        self.assertIn(R['CONFIDENCE'], v_high.reasons)

    def test_default_none_preserves_backward_compatible_behavior(self):
        """توافق خلفي: بلا تمرير min_confidence صراحة، السلوك القديم يبقى كما هو."""
        eng = NoTradeEngine(NoTradeConfig(), RiskConfig())
        v = eng.check(**base_kwargs(), confidence=0.5)
        self.assertTrue(v.allowed)   # 0.5 > 0.80*0.6=0.48 (السلوك القديم المُشتق)

    def test_config_value_actually_flows_to_check(self):
        """الاختبار الحاسم: تغيير Config.signal.min_confidence يُغيِّر القرار فعلياً."""
        cfg_lenient = Config()
        cfg_lenient.signal.min_confidence = 0.01
        # نعزل متغيّر الثقة عن بوابة BTC (القسم 12) — هذا الاختبار
        # يفحص min_confidence تحديداً، لا سلوك BTC؛ اختباره منفصل في
        # test_btc_fail_closed.py
        cfg_lenient.no_trade.require_btc_ok = False
        cfg_strict = Config()
        cfg_strict.signal.min_confidence = 0.99
        cfg_strict.no_trade.require_btc_ok = False

        d = make_fixture(1000, '1h', seed=200)
        eng_lenient = SignalEngine(cfg_lenient)
        eng_strict = SignalEngine(cfg_strict)
        prep_l = eng_lenient.prepare(d)
        prep_s = eng_strict.prepare(d)

        decisions_lenient, decisions_strict = [], []
        for i in range(300, 900):
            sl = eng_lenient.evaluate(d, i, data_quality=0.95, prep=prep_l)
            ss = eng_strict.evaluate(d, i, data_quality=0.95, prep=prep_s)
            decisions_lenient.append(sl.decision)
            decisions_strict.append(ss.decision)

        n_buy_lenient = decisions_lenient.count(BUY)
        n_buy_strict = decisions_strict.count(BUY)
        self.assertGreaterEqual(
            n_buy_lenient, n_buy_strict,
            f'رفع min_confidence إلى 0.99 يجب ألا يزيد صفقات BUY '
            f'(lenient={n_buy_lenient}, strict={n_buy_strict}) — '
            f'إن تساويا فالإعداد على الأرجح لا يزال ميتاً')
        self.assertGreater(n_buy_lenient, n_buy_strict,
                           'لا فرق إطلاقاً بين عتبة متساهلة وعتبة شبه مستحيلة — '
                           'الإعداد لا يزال بلا أثر فعلي')


class Test02_MinProbabilityNoLongerDead(unittest.TestCase):
    def test_high_threshold_blocks_when_probability_present(self):
        eng = NoTradeEngine(NoTradeConfig(), RiskConfig())
        v_low = eng.check(**base_kwargs(), probability=0.5, min_probability=0.3)
        v_high = eng.check(**base_kwargs(), probability=0.5, min_probability=0.9)
        self.assertTrue(v_low.allowed)
        self.assertFalse(v_high.allowed)
        self.assertIn(R['PROBABILITY'], v_high.reasons)

    def test_none_probability_skips_check_entirely(self):
        """لا احتمال معاير حقيقي (uncalibrated) — الفحص يُتجاوَز بأمان، لا يُفشِل زوراً."""
        eng = NoTradeEngine(NoTradeConfig(), RiskConfig())
        v = eng.check(**base_kwargs(), probability=None, min_probability=0.99)
        self.assertTrue(v.allowed)

    def test_default_none_preserves_backward_compatible_behavior(self):
        eng = NoTradeEngine(NoTradeConfig(), RiskConfig())
        v = eng.check(**base_kwargs(), probability=0.5)
        self.assertTrue(v.allowed)   # 0.5 > 0.40 (السلوك القديم المكتوب حرفياً)


if __name__ == '__main__':
    unittest.main(verbosity=2)
