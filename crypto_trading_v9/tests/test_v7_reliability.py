"""
اختبارات الاعتمادية — المرحلة العاشرة (24 سيناريو).
تعمل مع unittest و pytest. لا مفاتيح حقيقية ولا اتصال بأي شبكة.
"""
import unittest, os, sys, time, tempfile, threading, warnings, gc
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.storage.database import Database
from src.storage import migrations
from src.core.config import Config
from src.execution import order_state as S
from src.execution.errors import classify, ErrorClass, redact
from src.execution.idempotency import (
    IdempotentOrderGate, DuplicateOrderError, build_client_order_id,
    intent_fingerprint, CID_PATTERN, CID_MAX)
from src.execution.order_manager import OrderManager
from src.execution.reconciliation import Reconciler, D_NO_STOP, D_UNKNOWN_EXCHANGE_ORDER
from src.execution.binance_client import BinanceClient, BinanceError, MainnetBlocked
from src.monitoring.health import HealthMonitor
from tests.fake_exchange import FakeExchange


def tmpdb():
    return Database(os.path.join(tempfile.mkdtemp(), 't.db'))


def mkgate(ex=None, health=None):
    db = tmpdb()
    ex = ex or FakeExchange()
    return db, ex, IdempotentOrderGate(db, ex, health, sleep_fn=lambda s: None)


def buy(gate, ex, rid=1, notional=500.0, symbol='BTCUSDT'):
    return gate.execute(order_type='ENTRY', symbol=symbol, side='BUY',
                        recommendation_id=rid, quote_amount=notional,
                        send=lambda cid: ex.market_buy_quote(symbol, notional, cid))


# ── 1-2: تكرار ENTRY والتوازي ──
class Test01_DuplicateEntry(unittest.TestCase):
    def test_same_entry_ten_times_one_order(self):
        db, ex, g = mkgate()
        ok = 0
        for _ in range(10):
            try:
                r = buy(g, ex)
                ok += 1 if r['ok'] and not r['recovered'] else 0
            except DuplicateOrderError:
                pass
        self.assertEqual(len(ex.orders), 1, 'أمر واحد فقط على المنصة')
        self.assertEqual(ok, 1)

    def test_six_parallel_workers_one_order(self):
        db, ex, _ = mkgate()
        path = db.path
        barrier = threading.Barrier(6)
        errs = []

        def worker():
            d = Database(path)
            gg = IdempotentOrderGate(d, ex, sleep_fn=lambda s: None)
            barrier.wait()
            try:
                buy(gg, ex, rid=99)
            except DuplicateOrderError:
                pass
            except Exception as e:
                errs.append(str(e))

        ts = [threading.Thread(target=worker, daemon=True) for _ in range(6)]
        for t in ts: t.start()
        for t in ts: t.join(timeout=25)
        self.assertFalse(any(t.is_alive() for t in ts), 'بقيت Threads')
        self.assertEqual(len(ex.orders), 1, f'أوامر={len(ex.orders)} أخطاء={errs}')


# ── 3-4: Timeout ──
class Test03_Timeout(unittest.TestCase):
    def test_timeout_after_accept_no_second_order(self):
        db, ex, g = mkgate()
        ex.fail('accept_then_timeout', 1)
        r = buy(g, ex)
        self.assertTrue(r['recovered'])
        self.assertEqual(r['state'], S.FILLED)
        self.assertEqual(len(ex.orders), 1)

    def test_network_disconnect_after_accept(self):
        db, ex, g = mkgate()
        ex.fail('network_disconnect_after_accept', 1)
        buy(g, ex)
        self.assertEqual(len(ex.orders), 1)
        with self.assertRaises(DuplicateOrderError):
            buy(g, ex)
        self.assertEqual(len(ex.orders), 1)

    def test_timeout_before_arrival_retries_same_id(self):
        db, ex, g = mkgate()
        ex.fail('not_found', 1)          # مهلة بلا وصول
        r = buy(g, ex)
        cid = build_client_order_id('ENTRY', 'BTCUSDT', recommendation_id=1)
        self.assertEqual(r['client_order_id'], cid)
        used = {o['clientOrderId'] for o in ex.orders.values()}
        self.assertTrue(used <= {cid}, f'معرّفات مختلفة: {used}')

    def test_all_retries_use_same_id(self):
        db, ex, g = mkgate()
        ex.fail('not_found', 2)
        buy(g, ex)
        self.assertLessEqual(len({o['clientOrderId'] for o in ex.orders.values()}), 1)

    def test_delayed_visibility_resolved(self):
        db, ex, g = mkgate()
        ex.fail('delayed_visibility', 1)
        r = buy(g, ex)
        self.assertEqual(len(ex.orders), 1, 'لم يُنشأ أمر ثانٍ رغم الاختفاء المؤقت')
        self.assertIn(r['state'], (S.FILLED, S.MANUAL, S.ABORTED))


# ── 5-6: إعادة التشغيل ──
class Test05_Restart(unittest.TestCase):
    def test_restart_during_in_flight_recovers(self):
        db, ex, g = mkgate()
        cid = build_client_order_id('ENTRY', 'BTCUSDT', recommendation_id=3)
        g.reserve(order_type='ENTRY', symbol='BTCUSDT', side='BUY',
                  recommendation_id=3, quote_amount=500.0)
        db.transition_order_state(cid, S.IN_FLIGHT, reason='sim')
        ex.market_buy_quote('BTCUSDT', 500.0, cid)   # نُفّذ فعلاً

        g2 = IdempotentOrderGate(Database(db.path), ex, sleep_fn=lambda s: None)
        out = g2.recover_in_flight()
        self.assertEqual(out[0]['state'], S.FILLED)
        self.assertEqual(g2.has_unresolved(), 0)

    def test_restart_during_unknown_blocks_new_trades(self):
        db, ex, g = mkgate()
        cid = build_client_order_id('ENTRY', 'BTCUSDT', recommendation_id=4)
        g.reserve(order_type='ENTRY', symbol='BTCUSDT', side='BUY',
                  recommendation_id=4, quote_amount=500.0)
        db.transition_order_state(cid, S.IN_FLIGHT, reason='sim')
        db.transition_order_state(cid, S.UNKNOWN, reason='sim')
        db2 = Database(db.path)
        self.assertGreater(len(db2.unresolved_intents()), 0,
                           'UNKNOWN يجب أن يمنع فتح صفقات')

    def test_reserved_never_sent_aborted(self):
        db, ex, g = mkgate()
        g.reserve(order_type='ENTRY', symbol='BTCUSDT', side='BUY',
                  recommendation_id=8, quote_amount=500.0)
        out = g.recover_in_flight()
        self.assertEqual(out[0]['state'], S.ABORTED)


# ── 7-9: قيود القاعدة ──
class Test07_DatabaseGuards(unittest.TestCase):
    def setUp(self): self.db = tmpdb()

    def test_duplicate_trade_id_not_recorded_twice(self):
        a = self.db.record_fill_if_absent(order_id=1, symbol='B', qty=1,
                                          price=100, exchange_trade_id='tX')
        b = self.db.record_fill_if_absent(order_id=1, symbol='B', qty=1,
                                          price=100, exchange_trade_id='tX')
        self.assertTrue(a['created']); self.assertFalse(b['created'])
        self.assertEqual(len(self.db.query('SELECT * FROM fills')), 1)

    def test_duplicate_client_order_id_fails_safely(self):
        a = self.db.insert_order_if_absent(symbol='B', side='BUY', type='MARKET',
                                           status='FILLED', client_order_id='cq')
        b = self.db.insert_order_if_absent(symbol='B', side='BUY', type='MARKET',
                                           status='FILLED', client_order_id='cq')
        self.assertTrue(a['created']); self.assertFalse(b['created'])
        self.assertIsNotNone(b['order'])

    def test_different_payload_same_identity_rejected(self):
        db, ex, g = mkgate()
        g.reserve(order_type='ENTRY', symbol='BTCUSDT', side='BUY',
                  recommendation_id=11, quote_amount=50.0)
        with self.assertRaises(DuplicateOrderError):
            g.reserve(order_type='ENTRY', symbol='BTCUSDT', side='BUY',
                      recommendation_id=11, quote_amount=99.0)

    def test_null_client_order_ids_allowed_multiple(self):
        """الفهرس الجزئي يسمح بـ NULL متعدد."""
        a = self.db.save_order(symbol='B', side='BUY', type='MARKET', status='NEW')
        b = self.db.save_order(symbol='B', side='BUY', type='MARKET', status='NEW')
        self.assertGreater(a, 0); self.assertGreater(b, 0)


# ── 10-12: الوقف والهدف والخروج ──
class Test10_StopTargetExit(unittest.TestCase):
    def _om(self, ex=None):
        db = tmpdb(); ex = ex or FakeExchange()
        h = HealthMonitor(db)
        om = OrderManager(db, ex, Config(), h)
        om.gate._sleep = lambda s: None
        return db, ex, om, h

    def test_duplicate_stop_not_placed(self):
        db, ex, om, _ = self._om()
        a = om._place_stop('BTCUSDT', 0.01, 49000, pos_id=1)
        b = om._place_stop('BTCUSDT', 0.01, 49000, pos_id=1)
        self.assertIsNotNone(a)
        self.assertIsNone(b, 'وقف ثانٍ لمركز محميّ')
        self.assertEqual(len(ex.orders), 1)

    def test_stop_replacement_only_when_intentional(self):
        db, ex, om, _ = self._om()
        om._place_stop('BTCUSDT', 0.01, 49000, pos_id=2)
        self.assertIsNone(om._place_stop('BTCUSDT', 0.01, 48000, pos_id=2,
                                         intentional=False))
        cid = build_client_order_id('STOP', 'BTCUSDT', position_id=2, version=1)
        db.transition_order_state(cid, S.CANCELED, reason='intentional')
        self.assertIsNotNone(om._place_stop('BTCUSDT', 0.01, 48000, pos_id=2,
                                            intentional=True))

    def test_unresolved_stop_blocks_replacement(self):
        db, ex, om, _ = self._om()
        om._place_stop('BTCUSDT', 0.01, 49000, pos_id=3)
        cid = build_client_order_id('STOP', 'BTCUSDT', position_id=3, version=1)
        db.transition_order_state(cid, S.UNKNOWN, reason='sim')
        self.assertIsNone(om._place_stop('BTCUSDT', 0.01, 48000, pos_id=3,
                                         intentional=True),
                          'وقف جديد رغم نية غير محسومة')

    def test_duplicate_exit_no_double_sell(self):
        ex = FakeExchange(base_free=0.02)
        db, ex, om, _ = self._om(ex)
        pos = {'id': 5, 'symbol': 'BTCUSDT', 'qty': 0.01, 'entry_price': 49000,
               'fees': 0.0, 'status': 'OPEN', 'stop_order_id': None,
               'recommendation_id': None}
        self.assertTrue(om._exit_via_gate(pos, 'BTCUSDT', 0.01, 'EXIT', 'SIGNAL_EXIT'))
        before = ex._base
        om._exit_via_gate(pos, 'BTCUSDT', 0.01, 'EXIT', 'SIGNAL_EXIT')
        self.assertAlmostEqual(ex._base, before, places=9, msg='بيع مضاعف')

    def test_exit_capped_by_actual_balance(self):
        ex = FakeExchange(base_free=0.004)
        db, ex, om, _ = self._om(ex)
        pos = {'id': 6, 'symbol': 'BTCUSDT', 'qty': 0.01, 'entry_price': 49000,
               'fees': 0.0, 'status': 'OPEN', 'stop_order_id': None,
               'recommendation_id': None}
        om._exit_via_gate(pos, 'BTCUSDT', 0.01, 'EXIT', 'SIGNAL_EXIT')
        sells = [o for o in ex.orders.values() if o['side'] == 'SELL']
        self.assertTrue(sells)
        self.assertLessEqual(float(sells[0]['origQty']), 0.004 + 1e-9,
                             'بيع أكثر من الرصيد الفعلي')

    def test_target_order_placed(self):
        db, ex, om, _ = self._om()
        tid = om.place_target('BTCUSDT', 0.01, 52000, pos_id=7)
        self.assertIsNotNone(tid)
        self.assertIsNone(om.place_target('BTCUSDT', 0.01, 52000, pos_id=7))
        self.assertEqual(len([o for o in ex.orders.values()
                              if o['type'] == 'LIMIT']), 1)

    def test_partial_fill_records_quantities(self):
        ex = FakeExchange(); ex.fail('partial_fill', 1)
        db, ex, om, _ = self._om(ex)
        r = om.gate.execute(order_type='ENTRY', symbol='BTCUSDT', side='BUY',
                            recommendation_id=20, quote_amount=500.0,
                            send=lambda cid: ex.market_buy_quote('BTCUSDT', 500.0, cid))
        row = db.get_intent_by_client_order_id(r['client_order_id'])
        self.assertGreater(float(row['filled_qty'] or 0), 0)

    def test_exit_blocked_when_stop_uncancellable(self):
        db, ex, om, h = self._om()
        om._place_stop('BTCUSDT', 0.01, 49000, pos_id=9)
        sid = [o['orderId'] for o in ex.orders.values()][0]
        ex._base = 0.01
        orig = ex.cancel
        ex.cancel = lambda s, o: (_ for _ in ()).throw(BinanceError('boom'))
        pos = {'id': 9, 'symbol': 'BTCUSDT', 'qty': 0.01, 'entry_price': 49000,
               'fees': 0.0, 'status': 'OPEN', 'stop_order_id': sid,
               'recommendation_id': None}
        ok = om._exit_via_gate(pos, 'BTCUSDT', 0.01, 'EXIT', 'SIGNAL_EXIT')
        ex.cancel = orig
        self.assertFalse(ok, 'باع رغم بقاء الوقف حياً')
        self.assertTrue(h.kill_switch_on())


# ── 13-16: المصالحة ──
class Test13_Reconciliation(unittest.TestCase):
    def test_unknown_exchange_order_blocks(self):
        db = tmpdb(); ex = FakeExchange(base_free=0.01)
        ex.stop_loss_limit('BTCUSDT', 0.01, 49000, 48900, 'foreign-cid')
        g = IdempotentOrderGate(db, ex, sleep_fn=lambda s: None)
        r = Reconciler(db, ex, gate=g).run(['BTCUSDT'])
        self.assertFalse(r.ok)
        self.assertTrue(any(d['kind'] == D_UNKNOWN_EXCHANGE_ORDER
                            for d in r.discrepancies))

    def test_position_without_stop_flagged(self):
        db = tmpdb(); ex = FakeExchange(base_free=0.01)
        sig = {'symbol': 'BTCUSDT', 'interval': '4h', 'timestamp': 1,
               'decision': 'BUY', 'strategy_version': 'v', 'evidence': [],
               'reasons': []}
        sid = db.save_signal(sig); rid = db.save_recommendation(sid, sig, True)
        db.open_position(recommendation_id=rid, symbol='BTCUSDT', qty=0.01,
                         entry_price=49000)
        r = Reconciler(db, ex, gate=IdempotentOrderGate(db, ex)).run(['BTCUSDT'])
        self.assertFalse(r.ok)
        self.assertTrue(any(d['kind'] == D_NO_STOP for d in r.discrepancies))

    def test_local_position_missing_on_exchange(self):
        db = tmpdb(); ex = FakeExchange(base_free=0.0)
        sig = {'symbol': 'BTCUSDT', 'interval': '4h', 'timestamp': 2,
               'decision': 'BUY', 'strategy_version': 'v', 'evidence': [],
               'reasons': []}
        sid = db.save_signal(sig); rid = db.save_recommendation(sid, sig, True)
        db.open_position(recommendation_id=rid, symbol='BTCUSDT', qty=0.01,
                         entry_price=49000, stop_order_id='x')
        r = Reconciler(db, ex, gate=IdempotentOrderGate(db, ex)).run(['BTCUSDT'])
        self.assertFalse(r.ok)
        self.assertTrue(r.blocks_trading)

    def test_unresolved_intent_blocks_reconciliation(self):
        db = tmpdb(); ex = FakeExchange()
        g = IdempotentOrderGate(db, ex, sleep_fn=lambda s: None)
        cid = build_client_order_id('ENTRY', 'BTCUSDT', recommendation_id=30)
        g.reserve(order_type='ENTRY', symbol='BTCUSDT', side='BUY',
                  recommendation_id=30, quote_amount=500.0)
        db.transition_order_state(cid, S.IN_FLIGHT, reason='s')
        db.transition_order_state(cid, S.MANUAL, reason='s')
        r = Reconciler(db, ex, gate=g).run(['BTCUSDT'])
        self.assertFalse(r.ok)
        self.assertTrue(r.needs_manual)


# ── 17-20: الأمان ──
class Test17_Safety(unittest.TestCase):
    def test_no_api_keys_in_logs(self):
        db = tmpdb()
        secret = 'MYSUPERSECRETKEY1234567890'
        try:
            raise BinanceError(f'failed url signature=deadbeefcafe apiKey={secret}')
        except BinanceError as e:
            msg = redact(str(e))
        db.risk_event('TEST', 'INFO', msg)
        rows = db.query('SELECT detail FROM risk_events')
        self.assertNotIn('deadbeefcafe', rows[0]['detail'])
        self.assertNotIn(secret, rows[0]['detail'])

    def test_mainnet_blocked_by_default(self):
        os.environ.pop('ALLOW_MAINNET', None)
        with self.assertRaises(MainnetBlocked):
            BinanceClient('k' * 20, 's' * 20, testnet=False)

    def test_no_mainnet_contact_in_tests(self):
        from src.execution.binance_client import MAINNET, mainnet_allowed
        self.assertFalse(mainnet_allowed())
        c = BinanceClient('k' * 20, 's' * 20, testnet=True)
        self.assertNotEqual(c.base, MAINNET)

    def test_random_client_id_forbidden(self):
        with self.assertRaises(RuntimeError):
            BinanceClient.new_client_id()

    def test_auth_error_halts_system(self):
        db = tmpdb(); ex = FakeExchange(); h = HealthMonitor(db)
        g = IdempotentOrderGate(db, ex, h, sleep_fn=lambda s: None)
        ex.fail('auth_error', 1)
        r = buy(g, ex)
        self.assertTrue(h.kill_switch_on())
        self.assertEqual(r['state'], S.MANUAL)

    def test_unknown_error_never_creates_new_id(self):
        db = tmpdb(); ex = FakeExchange()
        g = IdempotentOrderGate(db, ex, sleep_fn=lambda s: None)
        ex.fail('unknown_error', 1)
        r = buy(g, ex)
        cid = build_client_order_id('ENTRY', 'BTCUSDT', recommendation_id=1)
        self.assertEqual(r['client_order_id'], cid)


# ── 21-24: النظافة والقيم الشاذة ──
class Test21_Hygiene(unittest.TestCase):
    def test_no_threads_left_behind(self):
        before = threading.active_count()
        db, ex, _ = mkgate()
        ts = [threading.Thread(
            target=lambda: IdempotentOrderGate(
                Database(db.path), ex, sleep_fn=lambda s: None).has_unresolved(),
            daemon=True) for _ in range(4)]
        for t in ts: t.start()
        for t in ts: t.join(timeout=10)
        gc.collect()
        self.assertLessEqual(threading.active_count(), before + 1)

    def test_no_resource_warnings(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter('always')
            db, ex, g = mkgate()
            buy(g, ex)
            db.query('SELECT * FROM order_intents')
            gc.collect()
            rw = [x for x in w if issubclass(x.category, ResourceWarning)]
            self.assertEqual(rw, [], f'ResourceWarnings: {[str(x.message) for x in rw]}')

    def test_no_sqlite_lock_errors_under_contention(self):
        db = tmpdb(); errs = []

        def hammer(i):
            d = Database(db.path)
            for j in range(20):
                try:
                    d.reserve_intent(client_order_id=f'c{i}-{j}', order_type='ENTRY',
                                     symbol='B', side='BUY', state=S.RESERVED,
                                     fingerprint='f', payload_hash='f')
                except Exception as e:
                    errs.append(str(e))

        ts = [threading.Thread(target=hammer, args=(i,), daemon=True)
              for i in range(5)]
        for t in ts: t.start()
        for t in ts: t.join(timeout=30)
        locks = [e for e in errs if 'locked' in e.lower()]
        self.assertEqual(locks, [], f'أخطاء قفل SQLite: {locks[:3]}')

    def test_invalid_numeric_inputs_rejected(self):
        """NaN و inf والكميات غير الموجبة تُرفض قبل أي نداء شبكة."""
        for i, bad in enumerate((float('nan'), float('inf'), -1.0, 0.0)):
            with self.subTest(v=bad):
                db, ex, g = mkgate()
                with self.assertRaises(ValueError):
                    g.execute(order_type='EXIT', symbol='BTCUSDT', side='SELL',
                              position_id=100 + i, qty=bad,
                              send=lambda cid: ex.market_sell('BTCUSDT', bad, cid))
                self.assertEqual(ex.send_calls, 0, 'وصل نداء رغم قيمة غير صالحة')

    def test_fingerprint_handles_nan(self):
        a = intent_fingerprint('ENTRY', 'B', 'BUY', qty=float('nan'))
        b = intent_fingerprint('ENTRY', 'B', 'BUY', qty=float('nan'))
        self.assertEqual(a, b)

    def test_migration_preserves_data(self):
        d = tempfile.mkdtemp(); p = os.path.join(d, 'm.db')
        db = Database(p)
        db.save_order(symbol='B', side='BUY', type='MARKET', status='FILLED',
                      client_order_id='keep-me')
        n_before = len(db.query('SELECT * FROM orders'))
        r = migrations.run(p)
        db2 = Database(p)
        self.assertEqual(len(db2.query('SELECT * FROM orders')), n_before)


# ── آلة الحالات ──
class Test25_StateMachine(unittest.TestCase):
    def test_all_allowed_transitions(self):
        for frm, tos in S.ALLOWED.items():
            for to in tos:
                self.assertTrue(S.can_transition(frm, to), f'{frm}→{to}')

    def test_terminal_states_are_sinks(self):
        for t in S.TERMINAL:
            self.assertEqual(S.ALLOWED[t], set(), f'{t} ليست نهائية')

    def test_illegal_transitions_rejected(self):
        for frm, to in [(S.FILLED, S.RESERVED), (S.FILLED, S.IN_FLIGHT),
                        (S.CANCELED, S.OPEN), (S.REJECTED, S.IN_FLIGHT),
                        (S.ABORTED, S.FILLED), (S.EXPIRED, S.OPEN)]:
            self.assertFalse(S.can_transition(frm, to), f'{frm}→{to} مسموح!')
            with self.assertRaises(S.IllegalTransition):
                S.assert_transition(frm, to)

    def test_transitions_logged_with_metadata(self):
        db = tmpdb()
        db.reserve_intent(client_order_id='tx', order_type='ENTRY', symbol='B',
                          side='BUY', state=S.RESERVED, fingerprint='f',
                          payload_hash='f')
        db.transition_order_state('tx', S.IN_FLIGHT, reason='send', actor='gate')
        db.transition_order_state('tx', S.RESERVED, reason='bad', actor='gate')
        rows = db.transitions_for('tx')
        self.assertGreaterEqual(len(rows), 2)
        self.assertTrue(all(r['ts'] and r['to_state'] for r in rows))
        self.assertTrue(any('REJECTED' in (r['reason'] or '') for r in rows))

    def test_error_classification_never_assumes_failure(self):
        class E(Exception):
            def __init__(s, m, c=None): super().__init__(m); s.code = c
        for msg, code in [('timed out', None), ('weird', None),
                          ('HTTP 503', 503), ('[-1007] TIMEOUT', -1007)]:
            self.assertTrue(classify(E(msg, code)).must_query,
                            f'{msg} افتُرض فشله')


if __name__ == '__main__':
    unittest.main(verbosity=2)
