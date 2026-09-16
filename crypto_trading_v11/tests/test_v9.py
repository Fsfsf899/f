"""اختبارات v9 — التزامن، المحفظة، الإجهاد، قفل Mainnet، الحتمية."""
import unittest, os, sys, threading, tempfile, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np

from tests.concurrency import ConcurrentRunner, assert_no_leaked_threads
from tests.fixtures import make_fixture
from src.storage.database import Database
from src.core.config import Config
from src.risk.portfolio import (PortfolioRisk, PortfolioConfig,
                                correlation_matrix, cluster_by_correlation)
from src.validation import stress
from src.validation.execution_gap import compare, MATCH, MISMATCH, INSUFFICIENT
from src.execution.binance_client import (BinanceClient, MainnetBlocked,
                                          mainnet_allowed, mainnet_block_reason,
                                          MAINNET_ENABLED_IN_SOURCE)
from src.backtest import OFFICIAL_ENGINE
from src.backtest.engine import BacktestEngine
from src.signals.engine import SignalEngine


# ── 1. التزامن ودورة حياة العمّال ──
class Test01_Concurrency(unittest.TestCase):
    def test_runner_completes_all_workers(self):
        r = ConcurrentRunner(6, timeout=10)
        out = r.run(lambda i: i * 2)
        self.assertEqual(len(out), 6)
        self.assertEqual(sorted(o.result for o in out), [0, 2, 4, 6, 8, 10])
        self.assertEqual(r.stuck_threads, [])

    def test_all_threads_are_daemon(self):
        r = ConcurrentRunner(4, timeout=10)
        r.run(lambda i: i)
        for t in r._threads:
            self.assertTrue(t.daemon, 'خيط غير daemon يمنع خروج المفسّر')

    def test_no_leaked_threads(self):
        before = threading.active_count()
        ConcurrentRunner(5, timeout=10).run(lambda i: i)
        assert_no_leaked_threads(self, before)

    def test_worker_error_captured_not_hanging(self):
        def boom(i):
            if i == 2:
                raise ValueError('متعمَّد')
            return i
        r = ConcurrentRunner(4, timeout=10)
        out = r.run(boom)
        self.assertEqual(len(out), 4)
        self.assertEqual(len([o for o in out if o.error]), 1)
        self.assertEqual(r.stuck_threads, [])

    def test_cleanup_runs_on_error(self):
        cleaned = []
        def boom(i, ctx): raise RuntimeError('x')
        r = ConcurrentRunner(3, timeout=10)
        r.run(boom, setup=lambda i: i,
              cleanup=lambda i, ctx: cleaned.append(i))
        self.assertEqual(sorted(cleaned), [0, 1, 2])

    def test_barrier_parties_match_workers(self):
        """السبب الجذري للتعليق في v8: أطراف الحاجز ≠ عدد المنتظرين."""
        r = ConcurrentRunner(5, timeout=10)
        self.assertEqual(r._barrier.parties, 5)
        r.run(lambda i: i)
        self.assertEqual(r.stuck_threads, [])

    def test_cancellation_stops_workers(self):
        r = ConcurrentRunner(3, timeout=5)
        r.cancel()
        out = r.run(lambda i: i)
        self.assertEqual(len(out), 3)
        self.assertTrue(all(not o.completed for o in out))

    def test_finishes_within_timeout(self):
        import time
        t0 = time.monotonic()
        ConcurrentRunner(8, timeout=10).run(lambda i: sum(range(1000)))
        self.assertLess(time.monotonic() - t0, 10)


# ── 2. مخاطر المحفظة ──
class Test02_Portfolio(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(7); n = 300
        btc = 100 * np.cumprod(1 + rng.normal(0, 0.02, n))
        drv = np.diff(np.log(np.concatenate([[100], btc])))
        self.series = {
            'BTCUSDT': btc,
            'ETHUSDT': 100 * np.cumprod(1 + drv * 0.9 + rng.normal(0, 0.006, n)),
            'SOLUSDT': 100 * np.cumprod(1 + drv * 0.85 + rng.normal(0, 0.008, n)),
            'INDEP': 100 * np.cumprod(1 + rng.normal(0, 0.02, n)),
        }

    def test_correlated_assets_clustered(self):
        corr, syms, _ = correlation_matrix(self.series)
        self.assertIsNotNone(corr)
        cl = cluster_by_correlation(corr, syms, 0.70)
        big = max(cl, key=len)
        self.assertIn('BTCUSDT', big)
        self.assertIn('ETHUSDT', big)
        self.assertNotIn('INDEP', big)

    def test_cluster_exposure_blocks(self):
        r = PortfolioRisk().evaluate(
            equity=10000, open_notional={'BTCUSDT': 2000, 'ETHUSDT': 2000},
            new_symbol='SOLUSDT', new_notional=1500, price_series=self.series)
        self.assertFalse(r.allowed)
        self.assertTrue(any('CLUSTER_EXPOSURE' in x for x in r.reasons))

    def test_uncorrelated_allowed(self):
        r = PortfolioRisk().evaluate(
            equity=10000, open_notional={'BTCUSDT': 2000},
            new_symbol='INDEP', new_notional=1500, price_series=self.series)
        self.assertTrue(r.allowed, r.reasons)

    def test_total_exposure_cap(self):
        r = PortfolioRisk(PortfolioConfig(max_total_exposure_pct=30)).evaluate(
            equity=10000, open_notional={'BTCUSDT': 2000},
            new_symbol='INDEP', new_notional=2000, price_series=self.series)
        self.assertFalse(r.allowed)

    def test_unknown_correlation_reported_not_assumed(self):
        r = PortfolioRisk().evaluate(
            equity=10000, open_notional={'BTCUSDT': 1000},
            new_symbol='ETHUSDT', new_notional=1000)
        self.assertFalse(r.correlation_available)
        self.assertTrue(any('CORRELATION_UNKNOWN' in x for x in r.reasons))

    def test_insufficient_returns_no_matrix(self):
        short = {'A': np.array([1.0, 2, 3]), 'B': np.array([1.0, 2, 3])}
        corr, _, note = correlation_matrix(short)
        self.assertIsNone(corr)
        self.assertIn('غير كافية', note)


# ── 3. الإجهاد ──
class Test03_Stress(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = make_fixture(2000, '1h', seed=42)
        cls.res = stress.run(cls.data, Config(), 10000)

    def test_all_scenarios_run(self):
        self.assertEqual(len(self.res.scenarios), len(stress.DEFAULT_SCENARIOS))

    def test_costs_monotonically_reduce_return(self):
        by = {s['name']: s for s in self.res.scenarios}
        if by['base']['trades'] > 0:
            self.assertLessEqual(by['costs_+50%']['net_return_pct'],
                                 by['base']['net_return_pct'] + 1e-9)

    def test_refuses_verdict_on_small_sample(self):
        by = {s['name']: s for s in self.res.scenarios}
        if by['base']['trades'] < 30:
            self.assertEqual(self.res.verdict, stress.NO_EDGE)
            self.assertIn('غير كافية', self.res.note)

    def test_no_edge_at_base_short_circuits(self):
        class C(Config): pass
        cfg = Config(); cfg.costs.taker_fee = 0.05     # تكلفة قاتلة
        r = stress.run(self.data, cfg, 10000)
        self.assertIn(r.verdict, (stress.NO_EDGE, stress.FRAGILE))


# ── 4. قفل Mainnet ──
class Test04_MainnetLock(unittest.TestCase):
    def test_disabled_in_source(self):
        self.assertFalse(MAINNET_ENABLED_IN_SOURCE,
                         'التداول الحقيقي يجب أن يبقى معطَّلاً في المصدر')

    def test_env_var_alone_insufficient(self):
        os.environ['ALLOW_MAINNET'] = '1'
        try:
            self.assertFalse(mainnet_allowed(),
                             'متغير البيئة وحده فتح Mainnet')
            with self.assertRaises(MainnetBlocked):
                BinanceClient('k' * 20, 's' * 20, testnet=False)
        finally:
            os.environ.pop('ALLOW_MAINNET', None)

    def test_block_reason_names_source(self):
        self.assertIn('MAINNET_ENABLED_IN_SOURCE', mainnet_block_reason())

    def test_live_environment_blocked(self):
        from src.environment.env import build, preflight, EnvironmentError_
        os.environ['ALLOW_MAINNET'] = '1'
        os.environ['MAINNET_API_KEY'] = 'k'
        os.environ['MAINNET_API_SECRET'] = 's'
        try:
            with self.assertRaises(EnvironmentError_):
                preflight(build('live', base_dir=tempfile.mkdtemp()))
        finally:
            for k in ('ALLOW_MAINNET', 'MAINNET_API_KEY', 'MAINNET_API_SECRET'):
                os.environ.pop(k, None)

    def test_testnet_unaffected(self):
        c = BinanceClient('k' * 20, 's' * 20, testnet=True)
        self.assertIn('testnet', c.base)


# ── 5. محرك واحد وحتمية ──
class Test05_SingleEngineDeterminism(unittest.TestCase):
    def test_official_engine_exported(self):
        self.assertIs(OFFICIAL_ENGINE, BacktestEngine)

    def test_same_inputs_same_results(self):
        d = make_fixture(1200, '1h', seed=11)
        a = BacktestEngine(Config(), SignalEngine(Config()), 10000).run(
            d, data_quality=0.95).metrics
        b = BacktestEngine(Config(), SignalEngine(Config()), 10000).run(
            d, data_quality=0.95).metrics
        self.assertEqual(a['total_trades'], b['total_trades'])
        self.assertAlmostEqual(a['net_profit'], b['net_profit'], places=9)
        self.assertEqual(a['profit_factor'], b['profit_factor'])

    def test_fixture_seed_deterministic(self):
        np.testing.assert_array_equal(
            make_fixture(500, '1h', seed=5).close,
            make_fixture(500, '1h', seed=5).close)

    def test_different_config_different_fingerprint(self):
        c1, c2 = Config(), Config()
        c2.signal.min_score = 9.9
        self.assertNotEqual(c1.fingerprint(), c2.fingerprint())


# ── 6. فجوة التنفيذ ──
class Test06_ExecutionGap(unittest.TestCase):
    def test_insufficient_sample_flagged(self):
        r = compare({'total_trades': 5, 'win_rate': 50, 'profit_factor': 1.2,
                     'expectancy': 1.0, 'max_drawdown_pct': 5,
                     'total_fees': 5, 'total_slippage': 2},
                    {'overall': {'n': 4, 'win_rate': 50, 'profit_factor': 1.2,
                                 'expectancy': 1.0, 'max_drawdown_pct': 5},
                     'costs': {'fees_from_fills': 4}})
        self.assertEqual(r.status, INSUFFICIENT)

    def test_mismatch_detected(self):
        bt = {'total_trades': 50, 'win_rate': 60, 'profit_factor': 2.0,
              'expectancy': 10.0, 'max_drawdown_pct': 5,
              'total_fees': 50, 'total_slippage': 20}
        pp = {'overall': {'n': 50, 'win_rate': 30, 'profit_factor': 0.7,
                          'expectancy': -5.0, 'max_drawdown_pct': 20},
              'costs': {'fees_from_fills': 200}}
        r = compare(bt, pp)
        self.assertEqual(r.status, MISMATCH)
        self.assertIn('win_rate', r.note + str(r.comparisons))

    def test_match_within_tolerance(self):
        bt = {'total_trades': 50, 'win_rate': 55, 'profit_factor': 1.5,
              'expectancy': 10.0, 'max_drawdown_pct': 10,
              'total_fees': 50, 'total_slippage': 20}
        pp = {'overall': {'n': 50, 'win_rate': 52, 'profit_factor': 1.4,
                          'expectancy': 9.0, 'max_drawdown_pct': 12},
              'costs': {'fees_from_fills': 55}}
        self.assertEqual(compare(bt, pp).status, MATCH)


# ── 7. سياسة البيانات الاصطناعية ──
class Test07_SyntheticPolicy(unittest.TestCase):
    def test_src_never_imports_fixtures(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        offenders = []
        for dp, _, fns in os.walk(os.path.join(root, 'src')):
            for fn in fns:
                if not fn.endswith('.py'):
                    continue
                with open(os.path.join(dp, fn), encoding='utf-8') as f:
                    txt = f.read()
                if 'fixtures' in txt or 'fake_exchange' in txt:
                    offenders.append(os.path.join(dp, fn))
        self.assertEqual(offenders, [], f'كود إنتاجي يستورد بيانات اختبار: {offenders}')

    def test_no_random_market_data_in_src(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        allowed = {'lookahead.py', 'paper_broker.py'}   # موثَّقة الاستثناء
        offenders = []
        for dp, _, fns in os.walk(os.path.join(root, 'src')):
            for fn in fns:
                if not fn.endswith('.py') or fn in allowed:
                    continue
                with open(os.path.join(dp, fn), encoding='utf-8') as f:
                    txt = f.read()
                if 'np.random' in txt:
                    offenders.append(fn)
        self.assertEqual(offenders, [], f'مولّد عشوائي في الإنتاج: {offenders}')

    def test_real_validation_has_no_fallback(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, 'validate_real.py'), encoding='utf-8') as f:
            src = f.read()
        self.assertIn('REAL MARKET DATA UNAVAILABLE', src)
        self.assertNotIn('make_fixture', src)
        self.assertNotIn('synthetic', src.lower().replace('اصطناعي', ''))


if __name__ == '__main__':
    unittest.main(verbosity=2)
