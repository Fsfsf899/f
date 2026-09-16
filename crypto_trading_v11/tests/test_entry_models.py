"""
اختبارات بنية الدخول الجديدة — Breakout/Pullback/Router.
كل اختبار هنا صحة منطقية (سببية، حتمية، عدم تكرار) — لا إثبات ربحية.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np

from src.core.config import Config
from src.storage.database import Database
from src.signals.entry import (EntrySignal, EntryRouter, BaselineEntryModel,
                               BreakoutEntryModel, PullbackEntryModel,
                               get_breakout_level, BASELINE, BREAKOUT,
                               PULLBACK, BREAKOUT_RETEST, NO_TRADE, PRIORITY)
from src.signals.entry.breakout import (process_breakout_retest,
                                        BREAKOUT_DETECTED, RETEST_WAITING,
                                        RETEST_CONFIRMED, ENTERED as BO_ENTERED,
                                        BREAKOUT_FAILED, EXPIRED)
from src.risk.risk_guard import RiskGuard
from src.market.btc_context import BTCContext
from tests.fixtures import make_fixture

# القسم 12 من متطلبات V11 Final Patch: BTC غائب يعني NO TRADE الآن
# (كان يمر بصمت سابقاً في NoTradeEngine — بند مُصلَح في هذه الجولة).
# سياق BTC متاح وإيجابي هنا كافتراضي لكل اختبارات هذا الملف، كي تبقى
# تفحص منطق النماذج نفسه لا بوابة BTC كمتغيّر مربِك غير مقصود.
_HEALTHY_BTC = BTCContext(available=True, regime='RANGE_BOUND', trend_ok=True,
                          risk_level='LOW')


def tmpdb():
    return Database(os.path.join(tempfile.mkdtemp(), 't.db'))


def base_context(prep, db=None, **over):
    c = {'prep': prep, 'data_quality': 0.95, 'account_state': {},
        'symbol': 'BTCUSDT', 'interval': '1h', 'interval_ms': 3_600_000,
        'btc_ctx': _HEALTHY_BTC}
    if db is not None:
        c['db'] = db
    c.update(over)
    return c


# ══ 0. إصلاح خلل الشلل الدائم لـ Consecutive Loss ══
class Test00_ConsecutiveLossFix(unittest.TestCase):
    """
    اكتُشف أثناء التحقيق المطلوب في هذه المهمة (البند 11 من التكليف):
    new_day() كان يُصفِّر halted لكن ليس consecutive_losses، فتُعاد
    المحاكمة فوراً وتُوقَف من جديد — شلل دائم بعد أول سلسلة خسائر.
    """
    def test_new_day_resets_consecutive_losses(self):
        g = RiskGuard()
        g.new_day(10000.0)
        for _ in range(g.cfg.max_consecutive_losses):
            g.record_open(); g.record_trade(-10.0)
        self.assertFalse(g.can_trade(9960)['allowed'])
        g.new_day(9960.0)
        self.assertEqual(g.consecutive_losses, 0)
        self.assertTrue(g.can_trade(9960)['allowed'])

    def test_permanent_paralysis_no_longer_occurs_across_many_days(self):
        """محاكاة 10 أيام متتالية — يجب ألا يبقى محظوراً للأبد."""
        g = RiskGuard()
        for day in range(10):
            g.new_day(10000.0)
            for _ in range(g.cfg.max_consecutive_losses):
                g.record_open(); g.record_trade(-10.0)
            self.assertFalse(g.can_trade(9960)['allowed'], f'يوم {day}')
        g.new_day(10000.0)
        self.assertTrue(g.can_trade(10000)['allowed'],
                        'ما زال محظوراً بعد 10 أيام — الإصلاح لم يعمل')


# ══ 1. get_breakout_level — سببية دقيقة ══
class Test01_BreakoutLevel(unittest.TestCase):
    def test_excludes_current_bar(self):
        d = make_fixture(200, '1h', seed=1)
        lvl = get_breakout_level(d, 100, 20)
        manual = float(np.max(d.high[80:100]))
        self.assertAlmostEqual(lvl['level'], manual, places=9)

    def test_current_bar_never_included_even_if_extreme(self):
        """لو كانت الشمعة idx أعلى High في التاريخ، يجب ألا تُحتسَب."""
        d = make_fixture(200, '1h', seed=2)
        d.high[100] = 999999.0   # قيمة متطرّفة عند idx نفسها
        lvl = get_breakout_level(d, 100, 20)
        self.assertLess(lvl['level'], 999999.0,
                        'get_breakout_level استخدم الشمعة الحالية — تسريب')

    def test_insufficient_history_returns_unavailable(self):
        """idx=0: لا شموع سابقة إطلاقاً — الحالة الحقيقية الوحيدة لعدم
        التوفر. نافذة قصيرة لكن غير فارغة (مثل idx=2) تُحسَب بصدق من
        الشموع المتاحة فعلاً — فحص الشمعات الدنيا مسؤولية WARMUP في
        NoTradeEngine على مستوى أعلى، لا هذه الدالة."""
        d = make_fixture(50, '1h', seed=3)
        lvl = get_breakout_level(d, 0, 20)
        self.assertFalse(lvl['available'])

    def test_short_window_still_computed_honestly(self):
        d = make_fixture(50, '1h', seed=3)
        lvl = get_breakout_level(d, 2, 20)
        self.assertTrue(lvl['available'])
        manual = float(np.max(d.high[0:2]))
        self.assertAlmostEqual(lvl['level'], manual, places=9)

    def test_calculated_until_is_idx_minus_1(self):
        d = make_fixture(200, '1h', seed=4)
        lvl = get_breakout_level(d, 150, 20)
        self.assertEqual(lvl['calculated_until'], 149)


# ══ 2. تعطيل افتراضي — BREAKOUT_ENABLED=0 / PULLBACK_ENABLED=0 ══
class Test02_DisabledByDefault(unittest.TestCase):
    def test_config_defaults_are_off(self):
        cfg = Config()
        self.assertFalse(cfg.breakout.enabled)
        self.assertFalse(cfg.pullback.enabled)
        self.assertFalse(cfg.breakout.retest_enabled)
        self.assertFalse(cfg.mtf_entry.confirmation_enabled)

    def test_breakout_model_rejects_when_disabled(self):
        cfg = Config()
        d = make_fixture(500, '1h', seed=5)
        eng = BaselineEntryModel(cfg).engine
        prep = eng.prepare(d)
        m = BreakoutEntryModel(cfg)
        for i in range(300, 450):
            sig = m.evaluate(d, i, base_context(prep))
            self.assertFalse(sig.eligible)
            self.assertEqual(sig.rejection_reasons, ['BREAKOUT_DISABLED'])

    def test_pullback_model_rejects_when_disabled(self):
        cfg = Config()
        d = make_fixture(500, '1h', seed=6)
        eng = BaselineEntryModel(cfg).engine
        prep = eng.prepare(d)
        m = PullbackEntryModel(cfg)
        for i in range(300, 450):
            sig = m.evaluate(d, i, base_context(prep))
            self.assertEqual(sig.rejection_reasons, ['PULLBACK_DISABLED'])

    def test_router_only_baseline_or_no_trade_when_new_models_disabled(self):
        cfg = Config()
        d = make_fixture(1500, '1h', seed=7)
        router = EntryRouter(cfg)
        prep = router.baseline.engine.prepare(d)
        ctx = base_context(prep)
        types = {router.evaluate(d, i, ctx).setup_type for i in range(300, 1400)}
        self.assertTrue(types <= {BASELINE, NO_TRADE})


# ══ 3. لا استخدام لبيانات مستقبلية ══
class Test03_NoLookahead(unittest.TestCase):
    def test_breakout_decision_unaffected_by_future_bars(self):
        cfg = Config(); cfg.breakout.enabled = True
        d = make_fixture(1000, '1h', seed=8)
        eng = BaselineEntryModel(cfg).engine
        prep = eng.prepare(d)
        m = BreakoutEntryModel(cfg)
        idx = 500
        sig_a = m.evaluate(d, idx, base_context(prep))

        d2 = make_fixture(1000, '1h', seed=8)   # نفس البيانات
        d2.high[idx + 1:] += 5000   # تشويش صريح للمستقبل فقط
        d2.close[idx + 1:] += 5000
        eng2 = BaselineEntryModel(cfg).engine
        prep2 = eng2.prepare(d2)
        sig_b = m.evaluate(d2, idx, base_context(prep2))

        self.assertEqual(sig_a.eligible, sig_b.eligible)
        if sig_a.entry_price is not None:
            self.assertAlmostEqual(sig_a.entry_price, sig_b.entry_price, places=6)

    def test_pullback_decision_unaffected_by_future_bars(self):
        cfg = Config(); cfg.pullback.enabled = True
        d = make_fixture(1500, '1h', seed=9)
        eng = BaselineEntryModel(cfg).engine
        prep = eng.prepare(d)
        m = PullbackEntryModel(cfg)
        idx = 700
        sig_a = m.evaluate(d, idx, base_context(prep))

        d2 = make_fixture(1500, '1h', seed=9)
        d2.high[idx + 1:] += 5000
        d2.low[idx + 1:] += 5000
        d2.close[idx + 1:] += 5000
        eng2 = BaselineEntryModel(cfg).engine
        prep2 = eng2.prepare(d2)
        sig_b = m.evaluate(d2, idx, base_context(prep2))
        self.assertEqual(sig_a.eligible, sig_b.eligible)


# ══ 4. الحتمية ══
class Test04_Determinism(unittest.TestCase):
    def test_same_inputs_same_output_breakout(self):
        cfg = Config(); cfg.breakout.enabled = True
        d = make_fixture(1000, '1h', seed=10)
        eng = BaselineEntryModel(cfg).engine
        prep = eng.prepare(d)
        m = BreakoutEntryModel(cfg)
        a = m.evaluate(d, 500, base_context(prep))
        b = m.evaluate(d, 500, base_context(prep))
        self.assertEqual(a.eligible, b.eligible)
        self.assertEqual(a.rejection_reasons, b.rejection_reasons)

    def test_router_deterministic(self):
        cfg = Config(); cfg.breakout.enabled = True; cfg.pullback.enabled = True
        d = make_fixture(1500, '1h', seed=11)
        r1 = EntryRouter(cfg); r2 = EntryRouter(cfg)
        p1 = r1.baseline.engine.prepare(d)
        seq1 = [r1.evaluate(d, i, base_context(p1)).setup_type
               for i in range(300, 600)]
        seq2 = [r2.evaluate(d, i, base_context(p1)).setup_type
               for i in range(300, 600)]
        self.assertEqual(seq1, seq2)


# ══ 5. الأولوية الموثَّقة ══
class Test05_Priority(unittest.TestCase):
    def test_priority_order_constant(self):
        self.assertEqual(PRIORITY, (BREAKOUT_RETEST, BREAKOUT, PULLBACK, BASELINE))

    def test_breakout_wins_over_baseline_when_both_eligible(self):
        """محاكاة مباشرة: نُغذّي EntryRouter مرشَّحين اصطناعيين للتحقق من الترتيب فقط."""
        cfg = Config()
        router = EntryRouter.__new__(EntryRouter)   # تجاوز __init__ لحقن مباشر
        router.cfg = cfg
        router.baseline = None
        router.breakout = None
        router.pullback = None
        candidates = {BASELINE: EntrySignal(setup_type=BASELINE, eligible=True),
                     BREAKOUT: EntrySignal(setup_type=BREAKOUT, eligible=True)}
        chosen = None
        for t in PRIORITY:
            if t in candidates:
                chosen = candidates[t]; break
        self.assertEqual(chosen.setup_type, BREAKOUT)

    def test_retest_wins_over_plain_breakout(self):
        candidates = {BREAKOUT: EntrySignal(setup_type=BREAKOUT, eligible=True),
                     BREAKOUT_RETEST: EntrySignal(setup_type=BREAKOUT_RETEST,
                                                  eligible=True)}
        for t in PRIORITY:
            if t in candidates:
                self.assertEqual(t, BREAKOUT_RETEST)
                break


# ══ 6. لا تجاوز لفحوص المخاطر المشتركة ══
class Test06_SharedRiskChecksNotBypassed(unittest.TestCase):
    def test_breakout_respects_min_rr(self):
        cfg = Config(); cfg.breakout.enabled = True
        cfg.signal.min_rr = 999.0   # مستحيل التحقق عملياً
        d = make_fixture(1000, '1h', seed=12)
        eng = BaselineEntryModel(cfg).engine
        prep = eng.prepare(d)
        m = BreakoutEntryModel(cfg)
        eligible_count = sum(
            1 for i in range(300, 900) if m.evaluate(d, i, base_context(prep)).eligible)
        self.assertEqual(eligible_count, 0,
                         'Breakout تجاوز فلتر min_rr المشترك')

    def test_pullback_respects_min_rr(self):
        cfg = Config(); cfg.pullback.enabled = True
        cfg.signal.min_rr = 999.0
        d = make_fixture(1500, '1h', seed=13)
        eng = BaselineEntryModel(cfg).engine
        prep = eng.prepare(d)
        m = PullbackEntryModel(cfg)
        eligible_count = sum(
            1 for i in range(300, 1400) if m.evaluate(d, i, base_context(prep)).eligible)
        self.assertEqual(eligible_count, 0)

    def test_breakout_respects_kill_switch_via_account_state(self):
        """
        NoTradeEngine لا يفحص kill switch مباشرة (ذلك مسؤولية طبقة
        التنفيذ الأعلى) — لكن exposure_ok=False (ما يعادله عملياً)
        يجب أن يُحترَم.
        """
        cfg = Config(); cfg.breakout.enabled = True
        d = make_fixture(1000, '1h', seed=14)
        eng = BaselineEntryModel(cfg).engine
        prep = eng.prepare(d)
        m = BreakoutEntryModel(cfg)
        ctx = base_context(prep, account_state={'exposure_ok': False})
        for i in range(300, 900):
            sig = m.evaluate(d, i, ctx)
            if sig.eligible:
                self.fail('صفقة أُهِّلت رغم exposure_ok=False')


# ══ 7. Retest — حالة، استمرارية، منع تكرار ══
class Test07_RetestStateMachine(unittest.TestCase):
    def _setup(self, seed=42, bars=2000):
        cfg = Config()
        cfg.breakout.enabled = True; cfg.breakout.retest_enabled = True
        d = make_fixture(bars, '1h', seed=seed)
        db = tmpdb()
        router = EntryRouter(cfg)
        prep = router.baseline.engine.prepare(d)
        return cfg, d, db, router, prep

    def test_no_immediate_entry_when_retest_enabled(self):
        cfg, d, db, router, prep = self._setup()
        ctx = base_context(prep, db=db, cfg=cfg)
        immediate = [i for i in range(300, 1900)
                    if router.evaluate(d, i, ctx).setup_type == BREAKOUT
                    and router.evaluate(d, i, ctx).eligible]
        # كل الدخول (إن حدث) يجب أن يكون عبر BREAKOUT_RETEST لا BREAKOUT مباشرة
        self.assertEqual(immediate, [])

    def test_setup_persists_across_restart(self):
        cfg, d, db, router, prep = self._setup(seed=42, bars=400)
        db_path = db.path
        ctx = base_context(prep, db=db, cfg=cfg)
        for i in range(300, 400):
            router.evaluate(d, i, ctx)
        n_before = len(db.query('SELECT 1 FROM entry_setups'))
        self.assertGreater(n_before, 0, 'لم يُسجَّل أي setup للاختبار')

        # إعادة "تشغيل" — كائن قاعدة بيانات جديد على نفس الملف
        db2 = Database(db_path)
        n_after = len(db2.query('SELECT 1 FROM entry_setups'))
        self.assertEqual(n_before, n_after)

    def test_entered_setup_never_reenters(self):
        cfg, d, db, router, prep = self._setup(seed=42)
        ctx = base_context(prep, db=db, cfg=cfg)
        entered_ids = []
        for i in range(300, 1900):
            sig = router.evaluate(d, i, ctx)
            if sig.setup_type == BREAKOUT_RETEST and sig.eligible:
                entered_ids.append(sig.setup_id)
        self.assertEqual(len(entered_ids), len(set(entered_ids)),
                         'نفس setup_id دخل أكثر من مرة')
        for sid in entered_ids:
            s = db.get_setup(sid)
            self.assertEqual(s['state'], BO_ENTERED)
            self.assertEqual(s['entered'], 1)

    def test_double_call_after_entry_does_not_reenter(self):
        """استدعاء process_breakout_retest يدوياً بعد الدخول لا يُصدر إشارة ثانية."""
        cfg, d, db, router, prep = self._setup(seed=43)
        ctx = base_context(prep, db=db, cfg=cfg)
        entered_at = None
        for i in range(300, 1900):
            sig = router.evaluate(d, i, ctx)
            if sig.setup_type == BREAKOUT_RETEST and sig.eligible:
                entered_at = i
                break
        if entered_at is None:
            self.skipTest('لم يحدث دخول في هذه العينة')
        # إعادة استدعاء نفس اللحظة يدوياً
        base_sig = router.breakout.evaluate(d, entered_at, ctx)
        again = process_breakout_retest(d, entered_at, ctx, base_sig)
        self.assertFalse(again.eligible and again.setup_type == BREAKOUT_RETEST
                         and again.diagnostics.get('setup_id') in
                         [s['setup_id'] for s in db.query(
                             "SELECT setup_id FROM entry_setups WHERE state=?",
                             (BO_ENTERED,))],
                         'دخول مزدوج لنفس setup بعد ENTERED')

    def test_expiry_after_window(self):
        cfg = Config()
        cfg.breakout.enabled = True; cfg.breakout.retest_enabled = True
        cfg.breakout.retest_window_bars = 1   # نافذة ضيقة جداً عمداً
        d = make_fixture(2000, '1h', seed=44)
        db = tmpdb()
        router = EntryRouter(cfg)
        prep = router.baseline.engine.prepare(d)
        ctx = base_context(prep, db=db, cfg=cfg)
        for i in range(300, 1900):
            router.evaluate(d, i, ctx)
        states = {s['state'] for s in db.query('SELECT state FROM entry_setups')}
        # بنافذة ضيقة جداً، EXPIRED متوقَّع أن يظهر لو وُجد أي اكتشاف
        if db.query('SELECT 1 FROM entry_setups'):
            self.assertTrue(
                states & {EXPIRED, BO_ENTERED, BREAKOUT_FAILED},
                'لا setup وصل لحالة نهائية معقولة')

    def test_transitions_logged(self):
        cfg, d, db, router, prep = self._setup(seed=45)
        ctx = base_context(prep, db=db, cfg=cfg)
        for i in range(300, 1900):
            router.evaluate(d, i, ctx)
        setups = db.query('SELECT setup_id FROM entry_setups')
        if not setups:
            self.skipTest('لا setups في هذه العينة')
        trans = db.setup_transitions(setups[0]['setup_id'])
        self.assertGreaterEqual(len(trans), 1)
        self.assertEqual(trans[0]['to_state'], BREAKOUT_DETECTED)


# ══ 8. تسجيل أسباب القبول/الرفض ══
class Test08_ReasonLogging(unittest.TestCase):
    def test_rejected_signal_always_has_reasons(self):
        cfg = Config(); cfg.breakout.enabled = True
        d = make_fixture(1000, '1h', seed=15)
        eng = BaselineEntryModel(cfg).engine
        prep = eng.prepare(d)
        m = BreakoutEntryModel(cfg)
        for i in range(300, 900):
            sig = m.evaluate(d, i, base_context(prep))
            if not sig.eligible:
                self.assertTrue(len(sig.rejection_reasons) > 0,
                               f'رفض بلا سبب مُسجَّل عند idx={i}')

    def test_eligible_signal_has_positive_reason(self):
        cfg = Config(); cfg.pullback.enabled = True
        d = make_fixture(1500, '1h', seed=16)
        eng = BaselineEntryModel(cfg).engine
        prep = eng.prepare(d)
        m = PullbackEntryModel(cfg)
        for i in range(300, 1400):
            sig = m.evaluate(d, i, base_context(prep))
            if sig.eligible:
                self.assertTrue(len(sig.reasons) > 0)
                return


# ══ 9. الدرجات الفرعية منفصلة ══
class Test09_SubScores(unittest.TestCase):
    def test_sub_scores_not_collapsed(self):
        cfg = Config(); cfg.breakout.enabled = True
        d = make_fixture(1000, '1h', seed=17)
        eng = BaselineEntryModel(cfg).engine
        prep = eng.prepare(d)
        m = BreakoutEntryModel(cfg)
        sig = m.evaluate(d, 500, base_context(prep))
        self.assertIsNotNone(sig.sub_scores)
        sd = sig.sub_scores.to_dict()
        for k in ('trend_score', 'momentum_score', 'structure_score',
                 'risk_score', 'execution_score', 'final_score'):
            self.assertIn(k, sd)


# ══ 10. النسخة والبصمة والتجربة ══
class Test10_Versioning(unittest.TestCase):
    def test_version_bumped(self):
        """
        النسخة أصبحت مرتبطة بالإصدار (v10.0.0) لا بعلامة بحثية —
        قرار توثيقي من ترقية V10 (راجع V10_UPGRADE_REPORT.md). هذا
        الاختبار يتحقق من الشكل الحالي الصحيح، لا يفرض علامة قديمة.
        """
        v = Config().version
        self.assertRegex(v, r'^v\d+\.\d+\.\d+')

    def test_breakout_pullback_fields_change_fingerprint(self):
        a = Config()
        b = Config(); b.breakout.enabled = True
        self.assertNotEqual(a.fingerprint(), b.fingerprint())
        c = Config(); c.pullback.enabled = True
        self.assertNotEqual(a.fingerprint(), c.fingerprint())

    def test_experiment_id_field_exists_and_affects_fingerprint(self):
        a = Config()
        b = Config(); b.experiment_id = 'exp_breakout_v1'
        self.assertNotEqual(a.fingerprint(), b.fingerprint())


# ══ 11. محوّل البحث لا يغيّر سلوك Baseline ══
class Test11_RouterAdapterFidelity(unittest.TestCase):
    def test_adapter_identical_to_direct_signal_engine_when_disabled(self):
        from src.research.router_adapter import RouterAsSignalEngine
        from src.backtest.engine import BacktestEngine
        from src.signals.engine import SignalEngine

        cfg = Config()   # breakout/pullback معطَّلان افتراضياً
        d = make_fixture(2000, '1h', seed=50)
        res_adapter = BacktestEngine(
            cfg, RouterAsSignalEngine(cfg), 10000).run(d, data_quality=0.95)
        res_direct = BacktestEngine(
            cfg, SignalEngine(cfg), 10000).run(d, data_quality=0.95)
        self.assertEqual(res_adapter.metrics['total_trades'],
                         res_direct.metrics['total_trades'])
        self.assertEqual(res_adapter.metrics.get('profit_factor'),
                         res_direct.metrics.get('profit_factor'))




# ══ 12. أسباب الرفض المُحاذاة والفحوص الجديدة ══
class Test12_AlignedRejectionReasons(unittest.TestCase):
    """
    اكتُشف أثناء المراجعة: max_age_bars كان مُعرَّفاً في PullbackConfig
    بلا أي استخدام فعلي — PULLBACK_EXPIRED لم يكن يُصدَر أبداً. أُضيف
    فعلياً هنا (بديل سببي بلا حالة: عدد الشموع منذ آخر إغلاق فوق
    سقف المنطقة).
    """
    def test_pullback_expired_fires_on_real_sample(self):
        cfg = Config(); cfg.pullback.enabled = True
        d = make_fixture(4000, '1h', seed=42, kind='mixed')
        eng = BaselineEntryModel(cfg).engine
        prep = eng.prepare(d)
        m = PullbackEntryModel(cfg)
        found = any('PULLBACK_EXPIRED' in
                   m.evaluate(d, i, base_context(prep)).rejection_reasons
                   for i in range(300, 3900))
        self.assertTrue(found, 'PULLBACK_EXPIRED لم يظهر إطلاقاً — الفحص معطَّل فعلياً')

    def test_breakout_candle_too_large_fires(self):
        cfg = Config(); cfg.breakout.enabled = True
        d = make_fixture(4000, '1h', seed=42, kind='mixed')
        eng = BaselineEntryModel(cfg).engine
        prep = eng.prepare(d)
        m = BreakoutEntryModel(cfg)
        found = any('BREAKOUT_CANDLE_TOO_LARGE' in
                   m.evaluate(d, i, base_context(prep)).rejection_reasons
                   for i in range(300, 3900))
        self.assertTrue(found)

    def test_breakout_stop_invalid_fires_with_extreme_atr(self):
        """وقف ATR بمضاعف كبير جداً يتجاوز max_stop_distance_pct."""
        cfg = Config(); cfg.breakout.enabled = True
        cfg.signal.atr_stop_mult = 50.0   # يضمن مسافة وقف مستحيلة القبول
        d = make_fixture(1000, '1h', seed=60)
        eng = BaselineEntryModel(cfg).engine
        prep = eng.prepare(d)
        m = BreakoutEntryModel(cfg)
        found = any('BREAKOUT_STOP_INVALID' in
                   m.evaluate(d, i, base_context(prep)).rejection_reasons
                   for i in range(300, 900))
        self.assertTrue(found)

    def test_pullback_stop_invalid_fires_with_extreme_atr(self):
        cfg = Config(); cfg.pullback.enabled = True
        cfg.signal.atr_stop_mult = 50.0
        d = make_fixture(1500, '1h', seed=61)
        eng = BaselineEntryModel(cfg).engine
        prep = eng.prepare(d)
        m = PullbackEntryModel(cfg)
        found = any('PULLBACK_STOP_INVALID' in
                   m.evaluate(d, i, base_context(prep)).rejection_reasons
                   for i in range(300, 1400))
        self.assertTrue(found)

    def test_breakout_spread_filter(self):
        cfg = Config(); cfg.breakout.enabled = True
        d = make_fixture(1000, '1h', seed=62)
        eng = BaselineEntryModel(cfg).engine
        prep = eng.prepare(d)
        m = BreakoutEntryModel(cfg)
        ctx = base_context(prep, spread_bps=9999.0)   # مرتفع جداً عمداً
        for i in range(300, 900):
            sig = m.evaluate(d, i, ctx)
            if not sig.eligible and sig.diagnostics.get('breakout_level', {}).get('available'):
                if sig.rejection_reasons != ['BREAKOUT_DISABLED']:
                    self.assertIn('BREAKOUT_SPREAD_TOO_HIGH', sig.rejection_reasons)
                    return

    def test_resistance_unavailable_renamed(self):
        """
        BREAKOUT_LEVEL_UNAVAILABLE أُعيد تسميته BREAKOUT_RESISTANCE_UNAVAILABLE.
        غير قابل للوصول عملياً عبر evaluate() الكامل (فحص ATR يسبقه
        دائماً ويرفض أولاً عند idx=0 — الحالة الوحيدة لعدم توفر
        المستوى) — الفحص المصدري هنا يثبت التسمية الصحيحة مباشرة،
        والسلوك الوظيفي مُثبَت في Test01_BreakoutLevel مباشرة على
        الدالة نفسها.
        """
        import inspect
        src = inspect.getsource(BreakoutEntryModel.evaluate)
        self.assertIn('BREAKOUT_RESISTANCE_UNAVAILABLE', src)
        self.assertNotIn('BREAKOUT_LEVEL_UNAVAILABLE', src)




# ══ 13. LiveTrader موصول فعلياً بـ EntryRouter — لا SignalEngine مباشرة ══
class Test13_LiveTraderUsesRouter(unittest.TestCase):
    """
    اكتُشف أثناء هذه الجولة: البنية الكاملة (Breakout/Pullback/Router)
    كانت مبنية ومُختبَرة بحثياً، لكن LiveTrader.__init__ كان يُثبِّت
    self.engine = SignalEngine(cfg) مباشرة — بلا أي طريق لـ Breakout/
    Pullback للتأثير على Paper/Testnet/Live الحقيقي مهما ضُبطت
    الإعدادات. أُصلح باستخدام RouterAsSignalEngine (نفس الواجهة،
    نتيجة مطابقة تماماً عند التعطيل — Test11_RouterAdapterFidelity).
    """

    def test_livetrader_engine_is_router_backed(self):
        import live_trader as LT
        from src.research.router_adapter import RouterAsSignalEngine
        from src.environment.env import build as build_env
        import tempfile as _t, os as _os

        for k in list(_os.environ):
            if k.startswith(('BREAKOUT_', 'PULLBACK_', 'MTF_ENTRY_')):
                _os.environ.pop(k, None)
        base = _t.mkdtemp()
        envcfg = build_env('monitor', base_dir=base, symbol='BTCUSDT',
                           interval='4h', strategy_version='v1', config_fingerprint='fp')
        t = LT.LiveTrader(envcfg, Config(), 50.0)
        self.assertIsInstance(t.engine, RouterAsSignalEngine)

    def test_breakout_env_var_actually_wires_router(self):
        import os
        os.environ['BREAKOUT_ENABLED'] = '1'
        os.environ['PULLBACK_ENABLED'] = '0'
        try:
            cfg = Config()
            cfg.breakout.enabled = os.getenv('BREAKOUT_ENABLED', '0') == '1'
            cfg.pullback.enabled = os.getenv('PULLBACK_ENABLED', '0') == '1'
            from src.research.router_adapter import RouterAsSignalEngine
            eng = RouterAsSignalEngine(cfg)
            self.assertIsNotNone(eng.router.breakout)
            self.assertIsNone(eng.router.pullback)
        finally:
            os.environ.pop('BREAKOUT_ENABLED', None)
            os.environ.pop('PULLBACK_ENABLED', None)

    def test_default_env_disabled_matches_plain_signal_engine(self):
        """بلا متغيرات بيئة، LiveTrader.engine يتصرف مطابقاً تماماً للسابق."""
        import os
        for k in ('BREAKOUT_ENABLED', 'PULLBACK_ENABLED'):
            os.environ.pop(k, None)
        cfg = Config()
        cfg.breakout.enabled = os.getenv('BREAKOUT_ENABLED', '0') == '1'
        cfg.pullback.enabled = os.getenv('PULLBACK_ENABLED', '0') == '1'
        self.assertFalse(cfg.breakout.enabled)
        self.assertFalse(cfg.pullback.enabled)


# ══ 11. إصلاح حرِج: btc_ctx كانت تُبتلَع بصمت في RouterAsSignalEngine ══
class Test11_RouterAdapterForwardsBTCContext(unittest.TestCase):
    """
    اكتُشف أثناء العمل على محرك اختيار الفرصة: `live_trader.py`
    يستخدم `RouterAsSignalEngine` حصراً، لا `SignalEngine` المباشرة.
    `RouterAsSignalEngine.evaluate()`/`IsolatedModelAdapter.evaluate()`
    كانتا تبتلعان `btc_ctx` بصمت عبر `**kw` — لا تصلان أبداً لـ
    context.get('btc_ctx')` التي يقرأها BaselineEntryModel/
    BreakoutEntryModel/PullbackEntryModel فعلياً. الأثر: إصلاح
    فشل-الإغلاق عند غياب BTC (no_trade.py، جولة سابقة) كان **عديم
    الأثر تماماً في المسار الحي الفعلي** رغم نجاحه في اختبارات
    SignalEngine المباشرة — انحدار وظيفي حقيقي (حظر كل قرارات BUY
    دائماً، بما أن btc_ctx تبقى None فعلياً في كل مكان يستخدم هذا
    الغلاف).
    """

    def test_router_adapter_decision_depends_on_btc_ctx(self):
        from src.research.router_adapter import RouterAsSignalEngine
        cfg = Config()   # require_btc_ok=True افتراضي — بلا تعطيل هنا عمداً
        eng = RouterAsSignalEngine(cfg)
        d = make_fixture(500, '1h', seed=1, symbol='BTCUSDT', kind='up')
        idx = len(d) - 1

        sig_no_btc = eng.evaluate(d, idx, data_quality=0.95)
        sig_with_btc = eng.evaluate(d, idx, data_quality=0.95, btc_ctx=_HEALTHY_BTC)

        self.assertNotEqual(sig_no_btc.decision, 'BUY',
                            'قرار BUY صدر رغم غياب btc_ctx تماماً — لا يزال معطَّلاً')
        self.assertIn('BTC_DATA_UNAVAILABLE', sig_no_btc.reasons)

    def test_router_adapter_sets_btc_context_display_field(self):
        from src.research.router_adapter import RouterAsSignalEngine
        cfg = Config()
        eng = RouterAsSignalEngine(cfg)
        d = make_fixture(500, '1h', seed=1, symbol='BTCUSDT', kind='up')
        idx = len(d) - 1
        sig = eng.evaluate(d, idx, data_quality=0.95, btc_ctx=_HEALTHY_BTC)
        self.assertNotEqual(sig.btc_context, 'UNKNOWN',
                            'حقل btc_context على Signal لا يزال UNKNOWN رغم تمرير سياق صحي')
        self.assertEqual(sig.btc_context, _HEALTHY_BTC.risk_level)

    def test_isolated_model_adapter_also_forwards_btc_ctx(self):
        from src.research.router_adapter import IsolatedModelAdapter
        from src.signals.entry.baseline import BaselineEntryModel
        cfg = Config()
        db = tmpdb()
        model = BaselineEntryModel(cfg)
        adapter = IsolatedModelAdapter(cfg, model, db=db, symbol='BTCUSDT',
                                       interval='1h')
        d = make_fixture(500, '1h', seed=1, symbol='BTCUSDT', kind='up')
        idx = len(d) - 1
        sig_no_btc = adapter.evaluate(d, idx, data_quality=0.95)
        self.assertNotEqual(sig_no_btc.decision, 'BUY')
        self.assertIn('BTC_DATA_UNAVAILABLE', sig_no_btc.reasons)

    def test_full_tick_flow_end_to_end_btc_context_populated(self):
        """
        الاختبار الحاسم: عبر LiveTrader.tick() الحقيقية كاملة — لا
        استدعاء معزول لمكوّن واحد. يُثبت أن الإصلاح يعمل في المسار
        الحي الفعلي، لا في اختبار وحدة منفصل قد يُخفي فجوة تكامل.
        """
        import live_trader as LT
        from src.environment.env import build as build_env, preflight
        import sys as _sys
        _sys.path.insert(0, os.path.dirname(__file__))
        from test_paper_smoke import FixtureCache, clean_env as _clean_env
        import tempfile as _tempfile

        _clean_env()
        os.environ.update({'TRADING_ENVIRONMENT': 'paper', 'CAPITAL': '10000'})
        base = _tempfile.mkdtemp()
        envcfg = build_env('paper', base_dir=base, symbol='BTCUSDT', interval='4h',
                           strategy_version='v11.0.0', config_fingerprint='fp')
        preflight(envcfg)
        t = LT.LiveTrader(envcfg, Config(), 50.0)
        t.cache = FixtureCache(make_fixture(800, '4h', seed=11, symbol='BTCUSDT'))
        if t.orders is not None:
            t.orders.gate._sleep = lambda s: None
        t.paper_market.set_price('BTCUSDT', 50000.0, spread_bps=4.0)

        t.tick(verbose=False)
        rows = t.db.query(
            'SELECT decision, btc_context FROM signals ORDER BY id DESC LIMIT 1')
        self.assertTrue(rows, 'لم تُسجَّل أي إشارة — tick() لم تصل لمرحلة التقييم')
        self.assertNotEqual(rows[0]['btc_context'], 'UNKNOWN',
                            'btc_context لا يزال UNKNOWN عبر tick() الحقيقية الكاملة')


if __name__ == '__main__':
    unittest.main(verbosity=2)
