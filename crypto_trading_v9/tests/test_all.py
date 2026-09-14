"""
مجموعة اختبارات V4 — البند 33.
متوافقة مع unittest و pytest.
تشغيل:  python -m unittest tests.test_all -v   أو   pytest tests/
"""
import unittest, time, os, sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.config import Config, DEFAULT
from src.data.types import OHLCV, INTERVAL_MS
from src.data.validation import validate, drop_unclosed, dedupe_and_sort
from src.data.cache import Cache
from src.indicators.engine import IndicatorEngine, ema, rsi, atr, sma
from src.market.structure import StructureEngine, find_swings, causal_levels
from src.market.regime import detect_at, TRENDING_BULL
from src.market.mtf import MultiTimeframe, aggregate
from src.market import btc_context
from src.signals.scoring import score_to_stars, aggregate as agg_score, Evidence
from src.signals.engine import SignalEngine, BUY, WAIT, NO_TRADE
from src.signals.no_trade import NoTradeEngine, R
from src.risk.position_sizing import PositionSizer
from src.risk.risk_guard import RiskGuard
from src.backtest.costs import CostModel
from src.backtest.execution import Bar, resolve_long_exit, STOP_LOSS, TAKE_PROFIT, BACKTEST_END
from src.backtest.engine import BacktestEngine
from src.backtest import metrics as M
from src.validation.lookahead import check_arrays, check_decisions, mutate_future
from src.validation.calibration import ProbabilityCalibrator, brier
from tests.fixtures import make_fixture


# ═══════════════ 1. البيانات ═══════════════
class TestDataValidation(unittest.TestCase):
    def setUp(self):
        self.now = int(time.time() * 1000)
        self.d = make_fixture(400, '1h', seed=1)

    def test_clean_data_scores_high(self):
        q = validate(self.d, now_ms=self.d.open_time[-1] + INTERVAL_MS['1h'])
        self.assertGreater(q.score, 0.85)
        self.assertTrue(q.ok)

    def test_invalid_ohlc_rejected(self):
        h = self.d.high.copy(); h[10] = self.d.low[10] - 1
        bad = OHLCV(self.d.symbol, '1h', self.d.open_time, self.d.open, h,
                    self.d.low, self.d.close, self.d.volume)
        q = validate(bad, now_ms=self.d.open_time[-1] + INTERVAL_MS['1h'])
        self.assertGreater(q.invalid_ohlc, 0)
        self.assertLess(q.score, 0.80)

    def test_stale_data_rejected(self):
        old = OHLCV(self.d.symbol, '1h', self.d.open_time - INTERVAL_MS['1h'] * 100,
                    self.d.open, self.d.high, self.d.low, self.d.close, self.d.volume)
        self.assertLess(validate(old, now_ms=self.now).score, 0.80)

    def test_unclosed_candle_dropped(self):
        step = INTERVAL_MS['1h']
        ot = np.append(self.d.open_time, self.d.open_time[-1] + step)
        ext = OHLCV('T', '1h', ot,
                    np.append(self.d.open, 1.0), np.append(self.d.high, 2.0),
                    np.append(self.d.low, 0.5), np.append(self.d.close, 1.0),
                    np.append(self.d.volume, 1.0))
        now = int(self.d.open_time[-1]) + step + 10   # الشمعة الأخيرة لم تُغلق
        self.assertEqual(len(drop_unclosed(ext, now)), len(ext) - 1)

    def test_length_mismatch_raises(self):
        with self.assertRaises(ValueError):
            OHLCV('T', '1h', np.arange(5), np.arange(5), np.arange(5),
                  np.arange(5), np.arange(4), np.arange(5))

    def test_duplicates_removed(self):
        ot = self.d.open_time.copy(); ot[5] = ot[4]
        dup = OHLCV('T', '1h', ot, self.d.open, self.d.high, self.d.low,
                    self.d.close, self.d.volume)
        self.assertEqual(len(dedupe_and_sort(dup)), len(dup) - 1)


class TestCache(unittest.TestCase):
    def test_atomic_write_and_coverage(self):
        import shutil
        d = '/tmp/v4_cache_test'; shutil.rmtree(d, ignore_errors=True)
        ca = Cache(d)
        data = make_fixture(300, '4h', seed=2)
        ca.write(data)
        got = ca.read(data.symbol, '4h')
        self.assertIsNotNone(got)
        _, meta = got
        self.assertEqual(meta['n_bars'], 300)
        self.assertEqual([f for f in os.listdir(d) if f.endswith('.tmp')], [])
        now = int(data.open_time[-1])
        self.assertTrue(ca.covers(meta, now - 20 * 86400000, now, '4h'))
        self.assertFalse(ca.covers(meta, now - 3000 * 86400000, now, '4h'))


# ═══════════════ 2. Look-Ahead ═══════════════
class TestNoLookAhead(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = make_fixture(1200, '1h', seed=7)

    def test_indicators_causal(self):
        r = check_arrays(lambda x: IndicatorEngine().compute(x, key=None), self.d)
        self.assertTrue(r['passed'], f"تسريب: {r['failures']}")

    def test_support_resistance_causal(self):
        def fn(x):
            out = StructureEngine().compute(x.high, x.low, x.close, x.open_time)
            return {k: v for k, v in out.items() if isinstance(v, np.ndarray)}
        r = check_arrays(fn, self.d)
        self.assertTrue(r['passed'], f"تسريب: {r['failures']}")

    def test_full_decision_causal(self):
        def decide(data, i):
            s = SignalEngine().evaluate(data, i, data_quality=0.95)
            return {'decision': s.decision, 'score': float(s.score),
                    'stars': s.stars, 'regime': s.regime}
        for seed in (11, 22):
            r = check_decisions(decide, self.d, sample=15, seed=seed)
            self.assertTrue(r['passed'], f"تسريب seed={seed}: {r['failures']}")

    def test_mutation_preserves_past(self):
        cut = 500
        m = mutate_future(self.d, cut)
        np.testing.assert_array_equal(self.d.close[:cut+1], m.close[:cut+1])
        self.assertFalse(np.allclose(self.d.close[cut+1:], m.close[cut+1:]))

    def test_swing_confirmation_delay(self):
        sw = find_swings(self.d.high, self.d.low, DEFAULT.structure, self.d.open_time)
        self.assertGreater(len(sw), 0)
        for s in sw:
            self.assertGreater(s.confirmation_index, s.index,
                               "المحور مؤكَّد قبل مرور شموع التأكيد")


# ═══════════════ 3. النجوم والاحتمال ═══════════════
class TestScoring(unittest.TestCase):
    def test_stars_monotonic(self):
        xs = np.arange(-5, 15, 0.01)
        st = [score_to_stars(x) for x in xs]
        for i in range(len(st) - 1):
            self.assertLessEqual(st[i], st[i+1], f"انكسرت الرتابة عند {xs[i]}")

    def test_stars_range(self):
        self.assertEqual(score_to_stars(-100), 1)
        self.assertEqual(score_to_stars(1000), 5)
        self.assertEqual(score_to_stars(float('nan')), 1)

    def test_no_modulo_wrap(self):
        """الصيغة القديمة (score*2)%6 كانت تلتف — يجب ألا تتكرر."""
        self.assertGreaterEqual(score_to_stars(10.0), score_to_stars(3.0))
        self.assertGreaterEqual(score_to_stars(20.0), score_to_stars(6.0))

    def test_confidence_is_not_probability(self):
        ev = [Evidence('A', 2.0, True), Evidence('B', 1.0, True),
              Evidence('C', 1.0, False), Evidence('D', -2.0, True)]
        r = agg_score(ev, data_quality=1.0)
        self.assertIsNone(r.calibrated_probability)
        self.assertEqual(r.probability_source, 'UNCALIBRATED')
        self.assertLessEqual(r.confidence, 1.0)

    def test_confidence_drops_with_missing_evidence(self):
        ev = [Evidence(f'E{i}', 1.0, True) for i in range(8)]
        full = agg_score(ev, 1.0)
        partial = agg_score(ev, 1.0, evaluable=['E0', 'E1'])
        self.assertLess(partial.confidence, full.confidence)


class TestCalibration(unittest.TestCase):
    def test_refuses_without_fit(self):
        c = ProbabilityCalibrator()
        self.assertFalse(c.is_fitted)
        with self.assertRaises(RuntimeError):
            c.predict([0.5])

    def test_insufficient_sample_refused(self):
        c = ProbabilityCalibrator()
        rep = c.fit([0.5] * 10, [1, 0] * 5)
        self.assertFalse(c.is_fitted)
        self.assertEqual(rep.method, 'INSUFFICIENT_DATA')

    def test_improves_brier_out_of_sample(self):
        rng = np.random.default_rng(3); n = 1200
        raw = rng.uniform(0, 1, n)
        y = (rng.uniform(0, 1, n) < (0.15 + 0.45 * raw)).astype(float)
        c = ProbabilityCalibrator('isotonic')
        c.fit(raw[:800], y[:800])
        self.assertLess(brier(c.predict(raw[800:]), y[800:]),
                        brier(raw[800:], y[800:]))

    def test_never_outputs_certainty(self):
        rng = np.random.default_rng(4); n = 600
        raw = rng.uniform(0, 1, n); y = (raw > 0.5).astype(float)
        c = ProbabilityCalibrator('isotonic'); c.fit(raw, y)
        p = c.predict(raw)
        self.assertTrue((p > 0).all() and (p < 1).all(), "أنتج يقيناً مطلقاً")


# ═══════════════ 4. التنفيذ ═══════════════
class TestExecution(unittest.TestCase):
    def test_stop_only(self):
        r = resolve_long_exit(Bar(100, 101, 94, 96), 95, 110)
        self.assertEqual(r[0], STOP_LOSS)

    def test_target_only(self):
        r = resolve_long_exit(Bar(100, 112, 99, 111), 95, 110)
        self.assertEqual(r[0], TAKE_PROFIT)

    def test_same_candle_conservative_picks_stop(self):
        r = resolve_long_exit(Bar(100, 112, 94, 105), 95, 110, 'conservative')
        self.assertEqual(r[0], STOP_LOSS, "conservative يجب أن يختار الأسوأ")

    def test_same_candle_optimistic_picks_target(self):
        r = resolve_long_exit(Bar(100, 112, 94, 105), 95, 110, 'optimistic')
        self.assertEqual(r[0], TAKE_PROFIT)

    def test_gap_down_fills_at_open(self):
        r = resolve_long_exit(Bar(90, 92, 88, 89), 95, 110)
        self.assertEqual(r[0], STOP_LOSS)
        self.assertAlmostEqual(r[1], 90, places=6)

    def test_no_exit(self):
        self.assertIsNone(resolve_long_exit(Bar(100, 102, 98, 101), 95, 110))

    def test_uses_high_low_not_close(self):
        """الشمعة أغلقت فوق الوقف لكن قاعها اخترقه — يجب اكتشافه."""
        r = resolve_long_exit(Bar(100, 101, 90, 100), 95, 110)
        self.assertEqual(r[0], STOP_LOSS)


class TestCosts(unittest.TestCase):
    def test_buy_worse_than_ideal(self):
        f = CostModel().buy(100.0, 1.0)
        self.assertGreater(f.price, 100.0)
        self.assertGreater(f.fee, 0)

    def test_stop_slippage_worse_than_normal(self):
        cm = CostModel()
        self.assertLess(cm.sell(100, 1, is_stop=True).price,
                        cm.sell(100, 1, is_stop=False).price)

    def test_breakeven_positive(self):
        self.assertGreater(CostModel().breakeven_move_pct(), 0)


# ═══════════════ 5. المخاطر ═══════════════
class TestRisk(unittest.TestCase):
    def test_daily_limit_uses_fixed_baseline(self):
        g = RiskGuard(); g.new_day(10000)
        self.assertFalse(g.daily_loss_hit(9800))
        self.assertTrue(g.daily_loss_hit(9700))
        g.new_day(9700)
        self.assertFalse(g.daily_loss_hit(9600), "الأساس يجب أن يُعاد ضبطه يومياً")

    def test_consecutive_losses_halt(self):
        g = RiskGuard(); g.new_day(10000)
        for _ in range(4):
            g.record_trade(-10)
        self.assertFalse(g.can_trade(10000)['allowed'])

    def test_max_positions(self):
        g = RiskGuard(); g.new_day(10000); g.record_open()
        self.assertFalse(g.can_trade(10000)['allowed'])

    def test_effective_risk_exceeds_nominal(self):
        ps = PositionSizer(cost_model=CostModel())
        r = ps.calculate(equity=10000, entry=50000, stop=49000, stars=3)
        self.assertGreater(r['effective_risk_per_unit'], r['nominal_risk_per_unit'])

    def test_size_shrinks_after_losses(self):
        ps = PositionSizer(cost_model=CostModel())
        a = ps.calculate(equity=10000, entry=100, stop=98, stars=5, consecutive_losses=0)
        b = ps.calculate(equity=10000, entry=100, stop=98, stars=5, consecutive_losses=3)
        self.assertLess(b['risk_pct'], a['risk_pct'])

    def test_notional_cap(self):
        ps = PositionSizer(cost_model=CostModel())
        r = ps.calculate(equity=10000, entry=100, stop=99.9, stars=5)
        self.assertLessEqual(r['notional'], 10000 * DEFAULT.risk.max_position_notional_pct / 100 + 1)

    def test_invalid_stop_rejected(self):
        ps = PositionSizer(cost_model=CostModel())
        self.assertEqual(ps.calculate(equity=10000, entry=100, stop=101)['qty'], 0.0)


# ═══════════════ 6. محرك المنع ═══════════════
class TestNoTrade(unittest.TestCase):
    def setUp(self):
        self.e = NoTradeEngine()
        self.base = dict(data_quality=0.95, bars_available=500, warmup=200)

    def test_allows_clean(self):
        self.assertTrue(self.e.check(**self.base).allowed)

    def test_blocks_low_quality(self):
        v = self.e.check(**{**self.base, 'data_quality': 0.4})
        self.assertFalse(v.allowed)
        self.assertIn(R['DATA_QUALITY'], v.reasons)

    def test_blocks_daily_loss(self):
        v = self.e.check(**self.base, daily_loss_hit=True)
        self.assertIn(R['DAILY_LOSS'], v.reasons)

    def test_blocks_close_resistance(self):
        v = self.e.check(**self.base, resistance_distance_pct=0.2)
        self.assertIn(R['RESISTANCE'], v.reasons)

    def test_blocks_bad_rr(self):
        v = self.e.check(**self.base, risk_reward=0.8, min_rr=1.5)
        self.assertIn(R['RR'], v.reasons)

    def test_blocks_wide_spread(self):
        v = self.e.check(**self.base, spread_bps=50.0)
        self.assertIn(R['SPREAD'], v.reasons)

    def test_blocks_dead_and_wild_volatility(self):
        self.assertIn(R['VOL_LOW'], self.e.check(**self.base, atr_pct=0.05).reasons)
        self.assertIn(R['VOL_HIGH'], self.e.check(**self.base, atr_pct=12.0).reasons)

    def test_reasons_recorded(self):
        v = self.e.check(**{**self.base, 'data_quality': 0.3}, risk_reward=0.5)
        self.assertGreaterEqual(len(v.reasons), 2)
        self.assertIsNotNone(v.primary)


# ═══════════════ 7. الباكتست ═══════════════
class TestBacktest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = make_fixture(2000, '1h', seed=42)
        cls.r = BacktestEngine(Config(), initial_capital=10000).run(cls.d, data_quality=0.95)

    def test_runs_and_reports(self):
        self.assertIn('profit_factor', self.r.metrics)
        self.assertEqual(self.r.strategy_version, DEFAULT.version)
        self.assertTrue(self.r.config_fingerprint)

    def test_all_trades_have_exit_reason(self):
        valid = {STOP_LOSS, TAKE_PROFIT, 'TRAILING_STOP', 'BREAK_EVEN',
                 'SIGNAL_EXIT', BACKTEST_END}
        for t in self.r.trades:
            self.assertIn(t['exit_reason'], valid)

    def test_no_position_lost_at_end(self):
        """البند 5: أي مركز مفتوح يجب أن يظهر كصفقة مغلقة."""
        d = make_fixture(1000, '1h', seed=5, kind='up')
        r = BacktestEngine(Config(), initial_capital=10000).run(d, data_quality=0.95)
        if r.open_at_end_closed:
            self.assertEqual(r.trades[-1]['exit_reason'], BACKTEST_END)
        self.assertEqual(len(r.equity), len(d) + 1)

    def test_costs_reduce_return(self):
        m = self.r.metrics
        if m['total_trades'] > 0:
            self.assertLessEqual(m['net_return_pct'], m['gross_return_pct'] + 1e-6)
            self.assertGreaterEqual(m['total_fees'], 0)

    def test_equity_curve_length(self):
        self.assertEqual(len(self.r.equity), len(self.d) + 1)

    def test_trades_record_mae_mfe(self):
        for t in self.r.trades:
            self.assertLessEqual(t['mae_pct'], 0.0 + 1e-9)
            self.assertGreaterEqual(t['mfe_pct'], 0.0 - 1e-9)

    def test_determinism(self):
        r2 = BacktestEngine(Config(), initial_capital=10000).run(self.d, data_quality=0.95)
        self.assertEqual(self.r.metrics['total_trades'], r2.metrics['total_trades'])
        self.assertAlmostEqual(self.r.metrics['net_profit'], r2.metrics['net_profit'], places=6)

    def test_rejections_recorded(self):
        self.assertIsInstance(self.r.rejections, dict)


class TestMetrics(unittest.TestCase):
    def test_annualization_documented(self):
        m = M.compute([], np.array([1000.0, 1010.0]), 1000.0, '4h')
        a = m['annualization']
        self.assertEqual(a['interval'], '4h')
        self.assertAlmostEqual(a['bars_per_year'], 2190.0, places=0)
        self.assertIn('365', a['basis'])

    def test_interval_changes_factor(self):
        a = M.compute([], np.array([1000.0, 1010.0]), 1000.0, '1h')['annualization']
        b = M.compute([], np.array([1000.0, 1010.0]), 1000.0, '1d')['annualization']
        self.assertGreater(a['factor'], b['factor'])

    def test_drawdown_nonnegative(self):
        eq = np.array([1000, 1100, 900, 950, 1200], dtype=float)
        self.assertGreater(M.compute([], eq, 1000.0, '1h')['max_drawdown_pct'], 0)


# ═══════════════ 8. الأطر المتعددة ═══════════════
class TestMultiTimeframe(unittest.TestCase):
    def setUp(self):
        self.d = make_fixture(1000, '1h', seed=8)

    def test_aggregation_correct(self):
        htf, avail = aggregate(self.d, '4h')
        self.assertEqual(htf.interval, '4h')
        self.assertLessEqual(len(htf) * 4, len(self.d) + 4)

    def test_only_closed_htf_candles_used(self):
        mtf = MultiTimeframe(self.d, ['4h'])
        step = INTERVAL_MS['4h']
        for i in range(300, len(self.d), 37):
            j = mtf.index_at('4h', i)
            if j >= 0:
                close_t = int(mtf.frames['4h'].open_time[j]) + step - 1
                self.assertLessEqual(close_t, int(self.d.open_time[i]),
                                     "استُخدمت شمعة إطار أعلى لم تُغلق")

    def test_bias_unavailable_early(self):
        mtf = MultiTimeframe(self.d, ['4h'])
        self.assertFalse(mtf.bias_at('4h', 5)['available'])


# ═══════════════ 9. حالة السوق و BTC ═══════════════
class TestRegime(unittest.TestCase):
    def test_uptrend_detected(self):
        d = make_fixture(600, '1h', seed=3, kind='up')
        from src.indicators.engine import dmi_adx
        a, _, _ = dmi_adx(d.high, d.low, d.close)
        r = detect_at(d.close, len(d) - 1, DEFAULT.regime, a, '1h')
        self.assertEqual(r.regime, TRENDING_BULL)
        self.assertTrue(r.long_friendly)

    def test_downtrend_not_long_friendly(self):
        d = make_fixture(600, '1h', seed=3, kind='down')
        from src.indicators.engine import dmi_adx
        a, _, _ = dmi_adx(d.high, d.low, d.close)
        self.assertFalse(detect_at(d.close, len(d) - 1, DEFAULT.regime, a, '1h').long_friendly)

    def test_unknown_before_warmup(self):
        d = make_fixture(600, '1h', seed=3)
        self.assertEqual(detect_at(d.close, 5, DEFAULT.regime).regime, 'UNKNOWN')

    def test_thresholds_from_config(self):
        from src.core.config import RegimeConfig
        d = make_fixture(600, '1h', seed=3, kind='up')
        strict = RegimeConfig(adx_trend_min=99.0)
        self.assertNotEqual(detect_at(d.close, len(d) - 1, strict, None, '1h').regime,
                            TRENDING_BULL)


class TestBTCContext(unittest.TestCase):
    def test_unavailable_is_honest(self):
        c = btc_context.evaluate(None, int(time.time() * 1000))
        self.assertFalse(c.available)
        self.assertEqual(c.risk_level, 'UNKNOWN')

    def test_uses_only_closed_candles(self):
        d = make_fixture(400, '1h', seed=6)
        at = int(d.open_time[200])
        c = btc_context.evaluate(d, at)
        self.assertTrue(c.available or c.note)


# ═══════════════ 10. تكامل الإشارة ═══════════════
class TestSignalIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = make_fixture(1500, '1h', seed=13)
        cls.e = SignalEngine()
        cls.prep = cls.e.prepare(cls.d)

    def test_only_three_decisions(self):
        for i in range(0, len(self.d), 53):
            s = self.e.evaluate(self.d, i, data_quality=0.95, prep=self.prep)
            self.assertIn(s.decision, (BUY, WAIT, NO_TRADE))

    def test_never_short(self):
        for i in range(0, len(self.d), 53):
            s = self.e.evaluate(self.d, i, data_quality=0.95, prep=self.prep)
            self.assertNotIn(s.decision, ('SELL', 'SHORT'))

    def test_warmup_blocks_early(self):
        s = self.e.evaluate(self.d, 10, data_quality=0.95, prep=self.prep)
        self.assertEqual(s.decision, NO_TRADE)
        self.assertIn(R['WARMUP'], s.reasons)

    def test_buy_has_complete_levels(self):
        for i in range(0, len(self.d), 7):
            s = self.e.evaluate(self.d, i, data_quality=0.95, prep=self.prep)
            if s.decision == BUY:
                self.assertIsNotNone(s.entry)
                self.assertIsNotNone(s.stop_loss)
                self.assertIsNotNone(s.take_profit)
                self.assertLess(s.stop_loss, s.entry)
                self.assertGreater(s.take_profit, s.entry)
                self.assertGreaterEqual(s.risk_reward, DEFAULT.signal.min_rr - 1e-9)
                return

    def test_no_probability_without_calibration(self):
        for i in range(300, len(self.d), 101):
            s = self.e.evaluate(self.d, i, data_quality=0.95, prep=self.prep)
            self.assertIsNone(s.calibrated_probability)
            self.assertEqual(s.probability_source, 'UNCALIBRATED')

    def test_low_quality_blocks_everything(self):
        for i in range(400, len(self.d), 149):
            s = self.e.evaluate(self.d, i, data_quality=0.3, prep=self.prep)
            self.assertEqual(s.decision, NO_TRADE)

    def test_audit_fields_present(self):
        s = self.e.evaluate(self.d, 800, data_quality=0.95, prep=self.prep)
        d = s.to_dict()
        for k in ('timestamp', 'symbol', 'interval', 'strategy_version', 'decision',
                  'score', 'confidence', 'raw_probability', 'calibrated_probability',
                  'regime', 'data_quality', 'reasons'):
            self.assertIn(k, d)


# ═══════════════ 11. مصدر واحد للحقيقة ═══════════════
class TestSingleSourceOfTruth(unittest.TestCase):
    def test_same_data_same_decision(self):
        d = make_fixture(900, '1h', seed=17)
        a = SignalEngine().evaluate(d, 700, data_quality=0.95)
        b = SignalEngine().evaluate(d, 700, data_quality=0.95)
        self.assertEqual(a.decision, b.decision)
        self.assertAlmostEqual(a.score, b.score, places=9)

    def test_backtest_and_live_use_same_engine(self):
        """المحرك المستخدم في الباكتست هو SignalEngine نفسه."""
        bt = BacktestEngine(Config())
        self.assertIsInstance(bt.engine, SignalEngine)

    def test_config_fingerprint_changes(self):
        c1 = Config(); c2 = Config()
        c2.signal.min_score = 9.9
        self.assertNotEqual(c1.fingerprint(), c2.fingerprint())


if __name__ == '__main__':
    unittest.main(verbosity=2)
