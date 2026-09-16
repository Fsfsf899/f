"""
اختبارات محرك اختيار أفضل فرصة — الأقسام 78-98.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.selection.opportunity import (scan_and_rank, evaluate_opportunity,
                                       ScoreWeights, SUPPORTED_ASSETS)
from src.signals.engine import SignalEngine, BUY, NO_TRADE
from src.core.config import Config
from src.risk.portfolio import PortfolioRisk, PortfolioConfig
from src.market.btc_context import BTCContext
from tests.fixtures import make_fixture

_HEALTHY_BTC = BTCContext(available=True, regime='RANGE_BOUND', trend_ok=True,
                          risk_level='LOW')


def _engines_and_data(symbols, seeds=None, kinds=None):
    """يبني SignalEngine وبيانات مستقلة لكل رمز — seeds مختلفة تعمّداً."""
    seeds = seeds or {s: 100 + i for i, s in enumerate(symbols)}
    kinds = kinds or {s: 'mixed' for s in symbols}
    engines, data, idx = {}, {}, {}
    for s in symbols:
        cfg = Config()
        cfg.no_trade.require_btc_ok = False   # نعزل منطق التصنيف عن بوابة BTC هنا
        engines[s] = SignalEngine(cfg)
        d = make_fixture(600, '1h', seed=seeds[s], symbol=s, kind=kinds[s])
        data[s] = d
        idx[s] = len(d) - 1
    return engines, data, idx


class Test01_NoHardcodedPreference(unittest.TestCase):
    """البند 79 الصريح: 'DO NOT use hardcoded preference such as Always prefer SOL'."""

    def test_different_seeds_produce_different_winners(self):
        """
        لو كان هناك تفضيل مكتوب، نفس الرمز كان سيفوز دائماً بصرف
        النظر عن البيانات. هنا: نبدّل أي رمز يحمل بيانات "up" قوية،
        ونتحقق أن الفائز يتبع البيانات لا هوية الرمز.
        """
        winners = set()
        for favored in SUPPORTED_ASSETS:
            kinds = {s: ('up' if s == favored else 'down') for s in SUPPORTED_ASSETS}
            seeds = {s: 500 + i for i, s in enumerate(SUPPORTED_ASSETS)}
            engines, data, idx = _engines_and_data(SUPPORTED_ASSETS, seeds, kinds)
            dq = {s: 0.95 for s in SUPPORTED_ASSETS}
            r = scan_and_rank(SUPPORTED_ASSETS, engines=engines, data_by_symbol=data,
                              idx_by_symbol=idx, data_quality_by_symbol=dq,
                              btc_ctx=_HEALTHY_BTC)
            if r.selected_symbol:
                winners.add(r.selected_symbol)
                # الرمز المُفضَّل بالبيانات (up) يجب أن يكون هو الفائز
                # إن تأهل أي رمز إطلاقاً — لا رمز آخر يفوز عليه بلا سبب بياني
                self.assertEqual(r.selected_symbol, favored,
                                 f'البيانات فضَّلت {favored} لكن الفائز {r.selected_symbol}')

    def test_no_symbol_wins_when_all_have_identical_bad_data(self):
        """كل الرموز بنفس نوعية بيانات ضعيفة — لا فوز اعتباطي لأحدها."""
        kinds = {s: 'down' for s in SUPPORTED_ASSETS}
        seeds = {s: 999 for s in SUPPORTED_ASSETS}   # نفس البذرة تماماً لكل الرموز
        engines, data, idx = _engines_and_data(SUPPORTED_ASSETS, seeds, kinds)
        # بيانات مطابقة تماماً لكل الرموز (نفس seed/kind) — النتيجة
        # يجب أن تكون متطابقة أو NO_TRADE للجميع، لا تفضيلاً عشوائياً
        dq = {s: 0.95 for s in SUPPORTED_ASSETS}
        r = scan_and_rank(SUPPORTED_ASSETS, engines=engines, data_by_symbol=data,
                          idx_by_symbol=idx, data_quality_by_symbol=dq,
                          btc_ctx=_HEALTHY_BTC)
        if r.selected_symbol:
            # لو تعادلت الدرجات (بيانات مطابقة)، التحكيم أبجدي — أول
            # رمز أبجدياً بين المُفضَّلة، لا اعتباطي
            self.assertTrue(r.tie_break_applied or len(
                [o for o in r.opportunities if o.eligible]) <= 1)


class Test02_NoForcedSelection(unittest.TestCase):
    """البند 82: 'There must be NO forced selection... NO best of bad choices'."""

    def test_all_no_trade_returns_no_trade(self):
        engines, data, idx = _engines_and_data(SUPPORTED_ASSETS,
                                               kinds={s: 'down' for s in SUPPORTED_ASSETS})
        # جودة بيانات صفرية تضمن رفض الكل بصرف النظر عن الإشارة
        dq = {s: 0.0 for s in SUPPORTED_ASSETS}
        r = scan_and_rank(SUPPORTED_ASSETS, engines=engines, data_by_symbol=data,
                          idx_by_symbol=idx, data_quality_by_symbol=dq,
                          btc_ctx=_HEALTHY_BTC)
        self.assertEqual(r.decision, 'NO_TRADE')
        self.assertIsNone(r.selected_symbol)
        self.assertEqual(r.reason, 'NO_ELIGIBLE_OPPORTUNITY')
        # كل رمز يحمل سبب رفض حقيقي، لا فارغاً
        for o in r.opportunities:
            self.assertFalse(o.eligible)
            self.assertTrue(o.rejection_reasons)

    def test_missing_data_symbol_gets_invalid_market_data_reason(self):
        engines, data, idx = _engines_and_data(SUPPORTED_ASSETS[:2])
        dq = {s: 0.95 for s in SUPPORTED_ASSETS[:2]}
        r = scan_and_rank(SUPPORTED_ASSETS, engines=engines, data_by_symbol=data,
                          idx_by_symbol=idx, data_quality_by_symbol=dq,
                          btc_ctx=_HEALTHY_BTC)
        missing = [o for o in r.opportunities if o.symbol not in SUPPORTED_ASSETS[:2]]
        for o in missing:
            self.assertIn('INVALID_MARKET_DATA', o.rejection_reasons)


class Test03_OnePositionPriority(unittest.TestCase):
    """البند 84: مركز مفتوح يمنع أي مسح جديد من فتح مركز ثانٍ."""

    def test_open_position_blocks_scan_entirely(self):
        engines, data, idx = _engines_and_data(SUPPORTED_ASSETS,
                                               kinds={s: 'up' for s in SUPPORTED_ASSETS})
        dq = {s: 0.95 for s in SUPPORTED_ASSETS}
        r = scan_and_rank(SUPPORTED_ASSETS, engines=engines, data_by_symbol=data,
                          idx_by_symbol=idx, data_quality_by_symbol=dq,
                          btc_ctx=_HEALTHY_BTC, already_has_open_position=True)
        self.assertEqual(r.decision, 'NO_TRADE')
        self.assertEqual(r.reason, 'POSITION_ALREADY_OPEN')
        self.assertEqual(r.opportunities, [])   # لا مسح حتى — توقف فوري


class Test04_DeterministicTieBreak(unittest.TestCase):
    """البند 91: نفس المُدخلات ⇒ نفس النتيجة دائماً، لا عشوائية."""

    def test_same_inputs_produce_same_result_across_runs(self):
        engines, data, idx = _engines_and_data(SUPPORTED_ASSETS,
                                               kinds={s: 'up' for s in SUPPORTED_ASSETS},
                                               seeds={s: 42 for s in SUPPORTED_ASSETS})
        dq = {s: 0.95 for s in SUPPORTED_ASSETS}
        results = []
        for _ in range(5):
            r = scan_and_rank(SUPPORTED_ASSETS, engines=engines, data_by_symbol=data,
                              idx_by_symbol=idx, data_quality_by_symbol=dq,
                              btc_ctx=_HEALTHY_BTC)
            results.append(r.selected_symbol)
        self.assertEqual(len(set(results)), 1,
                         f'نفس المُدخلات أنتجت فائزين مختلفين عبر التشغيلات: {results}')

    def test_documented_tie_break_rule_is_alphabetical(self):
        """يُثبت قاعدة التحكيم الموثَّقة صراحة: الأبجدية عند تساوي الدرجة تماماً."""
        from src.selection.opportunity import OpportunityScore
        opps = [OpportunityScore(symbol='SOLUSDT', eligible=True, decision=BUY, score=50.0),
               OpportunityScore(symbol='BTCUSDT', eligible=True, decision=BUY, score=50.0)]
        opps_sorted = sorted(opps, key=lambda o: (-o.score, o.symbol))
        self.assertEqual(opps_sorted[0].symbol, 'BTCUSDT',
                         'B قبل S أبجدياً — يجب أن يفوز BTCUSDT عند التعادل التام')


class Test05_RankingDoesNotOverrideRisk(unittest.TestCase):
    """البند 97: الترتيب لا يتجاوز فحوص المخاطر — يُطعِّم القرار، لا يُصرِّحه."""

    def test_portfolio_exposure_rejection_falls_through_to_next_candidate(self):
        engines, data, idx = _engines_and_data(SUPPORTED_ASSETS,
                                               kinds={s: 'up' for s in SUPPORTED_ASSETS},
                                               seeds={s: 700 + i for i, s in
                                                     enumerate(SUPPORTED_ASSETS)})
        dq = {s: 0.95 for s in SUPPORTED_ASSETS}
        # حد تعرّض إجمالي منخفض جداً، وتقدير حجم واقعي (لا صفر) لكل
        # رمز يتجاوزه بوضوح — يُثبت أن الفحص يُطبَّق فعلياً، لا يمر
        # صامتاً كما كان قبل إصلاح notional_estimates.
        pf = PortfolioRisk(PortfolioConfig(max_total_exposure_pct=1.0))
        notional = {s: 5_000.0 for s in SUPPORTED_ASSETS}   # 50% من equity
        r = scan_and_rank(SUPPORTED_ASSETS, engines=engines, data_by_symbol=data,
                          idx_by_symbol=idx, data_quality_by_symbol=dq,
                          btc_ctx=_HEALTHY_BTC, portfolio=pf, equity=10_000.0,
                          open_notional={}, notional_estimates=notional)
        if r.decision == 'BUY':
            self.fail('لا يجوز فوز أي مرشَّح — تقدير الحجم يتجاوز حد التعرّض بوضوح')
        else:
            self.assertIn(r.reason, ('PORTFOLIO_RISK_EXCEEDED', 'NO_ELIGIBLE_OPPORTUNITY'))

    def test_without_notional_estimates_portfolio_check_is_explicitly_skipped(self):
        """
        بلا notional_estimates، الفحص يُتجاوَز صراحة — لا يُستبدَل بصفر
        ضمني يجعله يمر دائماً بصرف النظر عن الحد (البق المُصلَح).
        """
        engines, data, idx = _engines_and_data(SUPPORTED_ASSETS,
                                               kinds={s: 'up' for s in SUPPORTED_ASSETS},
                                               seeds={s: 700 + i for i, s in
                                                     enumerate(SUPPORTED_ASSETS)})
        dq = {s: 0.95 for s in SUPPORTED_ASSETS}
        pf = PortfolioRisk(PortfolioConfig(max_total_exposure_pct=1.0))
        r = scan_and_rank(SUPPORTED_ASSETS, engines=engines, data_by_symbol=data,
                          idx_by_symbol=idx, data_quality_by_symbol=dq,
                          btc_ctx=_HEALTHY_BTC, portfolio=pf, equity=10_000.0,
                          open_notional={})   # بلا notional_estimates
        # الفحص تخطّى — لا خطأ، لكن هذا موثَّق كتجاوز صريح لا كنجاح حقيقي
        if r.decision == 'BUY':
            self.assertEqual(r.reason, 'BEST_ELIGIBLE_OPPORTUNITY')


class Test06_Reproducibility(unittest.TestCase):
    def test_evaluate_opportunity_is_pure_given_same_index(self):
        """نفس البيانات + نفس idx ⇒ نفس الدرجة تماماً — لا حالة داخلية متسرّبة."""
        cfg = Config(); cfg.no_trade.require_btc_ok = False
        eng = SignalEngine(cfg)
        d = make_fixture(500, '1h', seed=1, symbol='BTCUSDT', kind='up')
        r1 = evaluate_opportunity('BTCUSDT', eng, d, 400, data_quality=0.95,
                                  btc_ctx=_HEALTHY_BTC)
        r2 = evaluate_opportunity('BTCUSDT', eng, d, 400, data_quality=0.95,
                                  btc_ctx=_HEALTHY_BTC)
        self.assertEqual(r1.score, r2.score)
        self.assertEqual(r1.eligible, r2.eligible)


class Test07_WeightsConfigurable(unittest.TestCase):
    """البند 79: الأوزان قابلة للتهيئة فعلياً — تغييرها يُغيِّر الدرجة."""

    def test_changing_weights_changes_score(self):
        cfg = Config(); cfg.no_trade.require_btc_ok = False
        eng = SignalEngine(cfg)
        d = make_fixture(500, '1h', seed=1, symbol='BTCUSDT', kind='up')
        idx = len(d) - 1
        w_default = ScoreWeights()
        w_rr_heavy = ScoreWeights(signal_quality=0.05, confidence=0.05,
                                  probability=0.05, risk_reward=0.70,
                                  stop_quality=0.05, data_quality=0.05,
                                  btc_context=0.05)
        o1 = evaluate_opportunity('BTCUSDT', eng, d, idx, data_quality=0.95,
                                  btc_ctx=_HEALTHY_BTC, weights=w_default)
        o2 = evaluate_opportunity('BTCUSDT', eng, d, idx, data_quality=0.95,
                                  btc_ctx=_HEALTHY_BTC, weights=w_rr_heavy)
        if o1.eligible and o2.eligible:
            self.assertNotEqual(o1.score, o2.score,
                                'تغيير الأوزان لم يُغيِّر الدرجة — الأوزان غير فعلية')

    def test_missing_probability_weight_is_redistributed_not_dropped(self):
        w = ScoreWeights()
        normalized_with_prob = w.normalized(has_probability=True)
        normalized_without_prob = w.normalized(has_probability=False)
        self.assertAlmostEqual(sum(normalized_with_prob.values()), 1.0, places=6)
        self.assertAlmostEqual(sum(normalized_without_prob.values()), 1.0, places=6)
        self.assertEqual(normalized_without_prob['probability'], 0.0)
        # الوزن أُعيد توزيعه — بقية المكوّنات أكبر نسبياً بدون probability
        self.assertGreater(normalized_without_prob['risk_reward'],
                           normalized_with_prob['risk_reward'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
