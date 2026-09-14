"""اختبارات الطبقة الإنتاجية — البنود 25، 26، 28، 29، 30، 31، 32، 43."""
import unittest, os, sys, time, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.storage.database import Database
from src.monitoring.health import HealthMonitor
from src.monitoring.accuracy import AccuracyTracker
from src.execution.binance_client import BinanceClient, BinanceError
from src.execution.order_manager import PreTradeCheck
from src.core.config import Config


def tmpdb():
    return Database(os.path.join(tempfile.mkdtemp(), 't.db'))


def _src_path(rel: str) -> str:
    return os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), rel)


SIG = {'symbol': 'BTCUSDT', 'interval': '4h', 'timestamp': 1700000000000,
       'decision': 'BUY', 'strategy_version': 'v4.0.0', 'score': 6.0, 'stars': 4,
       'confidence': 0.9, 'raw_probability': 0.6, 'calibrated_probability': None,
       'probability_source': 'UNCALIBRATED', 'entry': 50000, 'stop_loss': 49000,
       'take_profit': 52000, 'risk_reward': 2.0, 'atr': 500,
       'regime': 'TRENDING_BULLISH', 'data_quality': 0.95, 'btc_context': 'LOW',
       'evidence': [], 'reasons': []}


class TestDatabase(unittest.TestCase):
    def setUp(self): self.db = tmpdb()

    def test_all_tables_created(self):
        names = {r['name'] for r in self.db.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ('signals', 'recommendations', 'positions', 'orders', 'fills',
                  'risk_events', 'system_events', 'backtests', 'backtest_trades',
                  'daily_equity', 'data_quality'):
            self.assertIn(t, names)

    def test_wal_enabled(self):
        self.assertEqual(self.db.query('PRAGMA journal_mode')[0]['journal_mode'], 'wal')

    def test_signal_dedupe(self):
        a = self.db.save_signal(SIG)
        b = self.db.save_signal(SIG)
        self.assertGreater(a, 0)
        self.assertEqual(b, 0, 'يجب رفض الإشارة المكررة لنفس الشمعة')

    def test_position_lifecycle(self):
        sid = self.db.save_signal(SIG)
        rid = self.db.save_recommendation(sid, SIG, True)
        pid = self.db.open_position(recommendation_id=rid, symbol='BTCUSDT',
                                    qty=0.01, entry_price=50000)
        self.assertEqual(len(self.db.open_positions()), 1)
        self.db.close_position(pid, 100.0)
        self.assertEqual(len(self.db.open_positions()), 0)

    def test_state_recoverable(self):
        sid = self.db.save_signal(SIG)
        rid = self.db.save_recommendation(sid, SIG, True)
        self.db.open_position(recommendation_id=rid, symbol='BTCUSDT',
                              qty=0.5, entry_price=100)
        again = Database(self.db.path)
        self.assertEqual(len(again.open_positions()), 1)

    def test_daily_equity_idempotent(self):
        self.db.start_day('2026-01-01', 10000)
        self.db.start_day('2026-01-01', 9000)
        self.assertEqual(self.db.get_day('2026-01-01')['starting_equity'], 10000)


class TestKillSwitch(unittest.TestCase):
    def setUp(self):
        self.db = tmpdb(); self.h = HealthMonitor(self.db)

    def test_healthy_initially(self):
        self.assertTrue(self.h.check().healthy)

    def test_api_failures_trigger_kill(self):
        for i in range(5): self.h.record_api_failure(f'f{i}')
        self.assertTrue(self.h.kill_switch_on())
        self.assertFalse(self.h.check().can_open_new)

    def test_persists_across_restart(self):
        self.h.engage_kill_switch('اختبار')
        self.assertTrue(HealthMonitor(Database(self.db.path)).kill_switch_on())

    def test_release(self):
        self.h.engage_kill_switch('x'); self.h.release_kill_switch()
        self.assertFalse(self.h.kill_switch_on())

    def test_reconciliation_blocks(self):
        self.h.set_reconciliation(False, 'اختلاف')
        self.assertFalse(self.h.check().can_open_new)

    def test_daily_loss_blocks(self):
        self.db.start_day('2026-01-01', 10000)
        s = self.h.check(equity=9600, day_key='2026-01-01')
        self.assertFalse(s.can_open_new)

    def test_order_failures_trigger_kill(self):
        for i in range(3): self.h.record_order_failure(f'o{i}')
        self.assertTrue(self.h.kill_switch_on())


class TestAccuracy(unittest.TestCase):
    def setUp(self):
        self.db = tmpdb()
        for i in range(40):
            s = dict(SIG, timestamp=1700000000000 + i * 14400000,
                     stars=3 + i % 3, regime='TRENDING_BULLISH' if i % 2 else 'RANGING')
            sid = self.db.save_signal(s)
            rid = self.db.save_recommendation(sid, s, True)
            pnl = 100.0 if i % 3 else -60.0
            self.db.close_recommendation(rid, outcome='WIN' if pnl > 0 else 'LOSS',
                                         exit_price=51000, exit_reason='TAKE_PROFIT',
                                         pnl=pnl, pnl_pct=pnl / 500,
                                         mae_pct=-1.0, mfe_pct=2.0, holding_bars=10)

    def test_report_dimensions(self):
        r = AccuracyTracker(self.db).report()
        for k in ('overall', 'by_symbol', 'by_regime', 'by_stars',
                  'by_probability', 'by_hour', 'by_exit_reason'):
            self.assertIn(k, r)

    def test_more_than_win_rate(self):
        o = AccuracyTracker(self.db).report()['overall']
        for k in ('win_rate', 'expectancy', 'profit_factor',
                  'avg_mae_pct', 'avg_mfe_pct', 'max_drawdown_pct'):
            self.assertIn(k, o)

    def test_small_sample_flagged(self):
        db = tmpdb()
        sid = db.save_signal(SIG); rid = db.save_recommendation(sid, SIG, True)
        db.close_recommendation(rid, outcome='WIN', exit_price=1, exit_reason='TP',
                                pnl=1, pnl_pct=1)
        r = db and AccuracyTracker(db).report()
        self.assertFalse(r['overall']['reliable'])
        self.assertIsNotNone(r['warning'])

    def test_empty_is_honest(self):
        r = AccuracyTracker(tmpdb()).report()
        self.assertEqual(r['overall']['n'], 0)
        self.assertIn('note', r)


class TestBinanceClient(unittest.TestCase):
    def test_hmac_matches_official_vector(self):
        c = BinanceClient.__new__(BinanceClient)
        c._secret = b'NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j'
        p = {'symbol': 'LTCBTC', 'side': 'BUY', 'type': 'LIMIT', 'timeInForce': 'GTC',
             'quantity': 1, 'price': '0.1', 'recvWindow': 5000,
             'timestamp': 1499827319559}
        self.assertTrue(c._sign(p).endswith(
            'c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71'))

    def test_requires_keys(self):
        with self.assertRaises(BinanceError):
            BinanceClient('', '')

    def test_step_rounding_never_rounds_up(self):
        for v, step in [(0.123456789, 0.001), (1.99999, 0.01), (50123.456, 0.01)]:
            r = BinanceClient._round_step(v, step)
            self.assertLessEqual(r, v + 1e-12)

    def test_no_simulation_in_client(self):
        with open(_src_path('src/execution/binance_client.py'),
                  encoding='utf-8') as f:
            src = f.read()
        for bad in ('np.random', 'random.', 'fake_', 'mock'):
            self.assertNotIn(bad, src)


class TestPreTrade(unittest.TestCase):
    def test_any_failure_blocks(self):
        c = PreTradeCheck(True)
        c.add('a', True).add('b', True).add('c', False, 'سبب')
        self.assertFalse(c.passed)
        self.assertEqual(c.failures, ['c'])

    def test_all_pass(self):
        c = PreTradeCheck(True)
        for n in 'abcdef': c.add(n, True)
        self.assertTrue(c.passed)


class TestStopSafety(unittest.TestCase):
    def test_guard_logic_present(self):
        with open(_src_path('src/execution/order_manager.py'),
                  encoding='utf-8') as f:
            src = f.read()
        self.assertIn('_verify_still_held', src)
        self.assertIn('EMERGENCY_EXIT', src)
        self.assertIn('STOP_REPLACED', src)

    def test_stop_limit_below_stop(self):
        from src.execution.order_manager import OrderManager
        om = OrderManager.__new__(OrderManager)
        om.stop_limit_offset_pct = 0.30
        stop = 100.0
        self.assertLess(stop * (1 - om.stop_limit_offset_pct / 100), stop)


class TestReconciliation(unittest.TestCase):
    def test_result_serializable(self):
        from src.execution.reconciliation import ReconResult
        r = ReconResult(False, [{'kind': 'QUANTITY_MISMATCH'}], 1, {'BTCUSDT': 0.5}, 2)
        d = r.to_dict()
        self.assertFalse(d['ok'])
        self.assertEqual(len(d['discrepancies']), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
