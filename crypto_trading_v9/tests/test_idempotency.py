"""
اختبارات منع التكرار.
=====================
FakeExchange هنا **مُثبِّت اختبار** (test double) يحاكي سلوك بينانس
لفحص منطق البوابة. ليس بيانات سوق ولا يُستورد من src/.
"""
import unittest, os, sys, time, tempfile, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.storage.database import Database
from src.execution.idempotency import (
    build_client_order_id, intent_fingerprint, IdempotentOrderGate,
    DuplicateOrderError, CID_PATTERN, CID_MAX)
from src.execution.order_state import (
    RESERVED, IN_FLIGHT, CONFIRMED, OPEN, PARTIALLY_FILLED, FILLED,
    REJECTED, UNKNOWN, ABORTED, MANUAL)
FAILED = ABORTED
from src.execution.binance_client import BinanceError


def tmpdb():
    return Database(os.path.join(tempfile.mkdtemp(), 't.db'))


class FakeExchange:
    """TEST DOUBLE — يحاكي بينانس. يرفض المعرّف المكرر كما تفعل المنصة."""

    def __init__(self):
        self.orders = {}          # cid -> order
        self.calls = 0
        self.fail_mode = None     # 'timeout' | 'reject' | 'timeout_after_exec'
        self.fail_times = 0

    def _maybe_fail(self, cid, executed_anyway=False):
        if self.fail_times > 0:
            self.fail_times -= 1
            if self.fail_mode == 'reject':
                raise BinanceError('[-2010] رصيد غير كافٍ', -2010)
            if self.fail_mode == 'timeout_after_exec':
                # الحالة الأخطر: نُفّذ فعلاً ثم انقطع الرد
                self.orders[cid] = {'orderId': 9000 + self.calls,
                                    'clientOrderId': cid, 'status': 'FILLED',
                                    'executedQty': '0.01',
                                    'cummulativeQuoteQty': '500.0', 'fills': []}
                raise BinanceError('Read timed out', None)
            raise BinanceError('Connection reset', None)

    def market_buy_quote(self, symbol, quote, cid):
        self.calls += 1
        if cid in self.orders:
            raise BinanceError('[-2010] Duplicate order sent.', -2010)
        self._maybe_fail(cid)
        o = {'orderId': 1000 + self.calls, 'clientOrderId': cid, 'status': 'FILLED',
             'executedQty': '0.01', 'cummulativeQuoteQty': str(quote),
             'fills': [{'qty': '0.01', 'price': str(quote / 0.01),
                        'commission': '0.001', 'commissionAsset': 'BNB',
                        'tradeId': self.calls}]}
        self.orders[cid] = o
        return o

    def market_sell(self, symbol, qty, cid):
        self.calls += 1
        if cid in self.orders:
            raise BinanceError('[-2010] Duplicate order sent.', -2010)
        self._maybe_fail(cid)
        o = {'orderId': 2000 + self.calls, 'clientOrderId': cid, 'status': 'FILLED',
             'executedQty': str(qty), 'cummulativeQuoteQty': str(qty * 100),
             'fills': [{'qty': str(qty), 'price': '100', 'commission': '0'}]}
        self.orders[cid] = o
        return o

    def stop_loss_limit(self, symbol, qty, stop, limit, cid):
        self.calls += 1
        if cid in self.orders:
            raise BinanceError('[-2010] Duplicate order sent.', -2010)
        self._maybe_fail(cid)
        o = {'orderId': 3000 + self.calls, 'clientOrderId': cid, 'status': 'NEW'}
        self.orders[cid] = o
        return o

    def order_by_client_id(self, symbol, cid):
        if cid not in self.orders:
            raise BinanceError('[-2013] Order does not exist.', -2013)
        return self.orders[cid]


class TestDeterministicId(unittest.TestCase):
    def test_same_intent_same_id(self):
        for _ in range(50):
            self.assertEqual(
                build_client_order_id('ENTRY', 'BTCUSDT', recommendation_id=42),
                build_client_order_id('ENTRY', 'BTCUSDT', recommendation_id=42))

    def test_different_inputs_differ(self):
        ids = {
            build_client_order_id('ENTRY', 'BTCUSDT', recommendation_id=1),
            build_client_order_id('ENTRY', 'BTCUSDT', recommendation_id=2),
            build_client_order_id('EXIT', 'BTCUSDT', position_id=1),
            build_client_order_id('STOP', 'BTCUSDT', position_id=1),
            build_client_order_id('STOP', 'BTCUSDT', position_id=1, version=2),
            build_client_order_id('ENTRY', 'ETHUSDT', recommendation_id=1),
        }
        self.assertEqual(len(ids), 6)

    def test_no_collisions_at_scale(self):
        ids = [build_client_order_id(t, s, recommendation_id=r, version=v)
               for t in ('ENTRY', 'EXIT', 'STOP', 'EMERGENCY')
               for s in ('BTCUSDT', 'ETHUSDT', 'SOLUSDT')
               for r in range(1, 400) for v in (1, 2)]
        self.assertEqual(len(set(ids)), len(ids))

    def test_binance_constraints(self):
        for sym in ('BTCUSDT', '1000SATSUSDT', 'ETHBTC'):
            for rid in (1, 999999, 12345678901234):
                cid = build_client_order_id('EMERGENCY', sym, position_id=rid)
                self.assertLessEqual(len(cid), CID_MAX)
                self.assertTrue(CID_PATTERN.match(cid))

    def test_requires_a_reference(self):
        with self.assertRaises(ValueError):
            build_client_order_id('ENTRY', 'BTCUSDT')

    def test_rejects_unknown_type(self):
        with self.assertRaises(ValueError):
            build_client_order_id('WITHDRAW', 'BTCUSDT', recommendation_id=1)

    def test_fingerprint_detects_param_change(self):
        a = intent_fingerprint('ENTRY', 'BTCUSDT', 'BUY', quote_amount=50.0)
        b = intent_fingerprint('ENTRY', 'BTCUSDT', 'BUY', quote_amount=50.0)
        c = intent_fingerprint('ENTRY', 'BTCUSDT', 'BUY', quote_amount=51.0)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)


class TestGate(unittest.TestCase):
    def setUp(self):
        self.db = tmpdb()
        self.ex = FakeExchange()
        self.gate = IdempotentOrderGate(self.db, self.ex)

    def _buy(self, rid=1, notional=500.0):
        return self.gate.execute(
            order_type='ENTRY', symbol='BTCUSDT', side='BUY',
            recommendation_id=rid, quote_amount=notional,
            send=lambda cid: self.ex.market_buy_quote('BTCUSDT', notional, cid))

    def test_happy_path(self):
        """
        تغيير دلالي مقصود في v7: الحالة تُشتق من حالة المنصة الفعلية
        (FILLED/OPEN/PARTIALLY_FILLED) بدل CONFIRMED العامة في v6.
        هذا أدق ويسمح بتتبّع التنفيذ الجزئي.
        """
        r = self._buy()
        self.assertTrue(r['ok'])
        self.assertEqual(r['state'], FILLED)
        self.assertEqual(self.ex.calls, 1)

    def test_second_attempt_blocked_no_network_call(self):
        self._buy()
        calls = self.ex.calls
        with self.assertRaises(DuplicateOrderError):
            self._buy()
        self.assertEqual(self.ex.calls, calls, 'يجب ألا يصل أي نداء للمنصة')

    def test_timeout_after_execution_recovers_not_duplicates(self):
        """السيناريو الأخطر: نُفّذ الأمر ثم انقطع الرد."""
        self.ex.fail_mode = 'timeout_after_exec'
        self.ex.fail_times = 1
        r = self._buy()
        self.assertTrue(r['ok'], 'كان يجب استرجاع الأمر المنفَّذ')
        self.assertTrue(r['recovered'])
        self.assertEqual(len(self.ex.orders), 1, 'أمر واحد فقط على المنصة')

    def test_retry_after_timeout_still_one_order(self):
        self.ex.fail_mode = 'timeout_after_exec'; self.ex.fail_times = 1
        self._buy()
        with self.assertRaises(DuplicateOrderError):
            self._buy()
        self.assertEqual(len(self.ex.orders), 1)

    def test_pure_timeout_no_execution_marked_failed(self):
        self.ex.fail_mode = 'timeout'; self.ex.fail_times = 1
        r = self._buy()
        self.assertFalse(r['ok'])
        self.assertEqual(r['state'], FAILED)
        self.assertEqual(len(self.ex.orders), 0)

    def test_definitive_rejection_is_terminal(self):
        self.ex.fail_mode = 'reject'; self.ex.fail_times = 1
        r = self._buy()
        self.assertEqual(r['state'], REJECTED)
        with self.assertRaises(DuplicateOrderError):
            self._buy()

    def test_changed_params_rejected(self):
        self.gate.reserve(order_type='ENTRY', symbol='BTCUSDT', side='BUY',
                          recommendation_id=7, quote_amount=50.0)
        with self.assertRaises(DuplicateOrderError):
            self.gate.reserve(order_type='ENTRY', symbol='BTCUSDT', side='BUY',
                              recommendation_id=7, quote_amount=99.0)

    def test_intent_persisted_before_send(self):
        seen = {}
        def send(cid):
            seen['state'] = self.gate.get(cid).state
            return self.ex.market_buy_quote('BTCUSDT', 500.0, cid)
        self.gate.execute(order_type='ENTRY', symbol='BTCUSDT', side='BUY',
                          recommendation_id=99, quote_amount=500.0, send=send)
        self.assertEqual(seen['state'], IN_FLIGHT,
                         'يجب أن تُحفظ النية قبل نداء الشبكة')

    def test_restart_recovers_in_flight(self):
        self.ex.fail_mode = 'timeout_after_exec'; self.ex.fail_times = 1
        try:
            self.gate.execute(order_type='ENTRY', symbol='BTCUSDT', side='BUY',
                              recommendation_id=5, quote_amount=500.0,
                              send=lambda cid: (_ for _ in ()).throw(
                                  BinanceError('crash', None)))
        except Exception:
            pass
        cid = build_client_order_id('ENTRY', 'BTCUSDT', recommendation_id=5)
        self.db.execute("UPDATE order_intents SET state=? WHERE client_order_id=?",
                        (IN_FLIGHT, cid))
        gate2 = IdempotentOrderGate(Database(self.db.path), self.ex,
                                    sleep_fn=lambda s: None)
        out = gate2.recover_in_flight()
        self.assertTrue(len(out) >= 1)
        # v7: ما يتعذّر حسمه ينتقل إلى ERROR_REQUIRES_MANUAL_REVIEW
        # ويبقى مانعاً للتداول عمداً — لا يُمسح صامتاً.
        for o in out:
            self.assertIn(o['state'], (FILLED, OPEN, ABORTED, REJECTED, MANUAL))

    def test_reserved_never_sent_is_aborted(self):
        self.gate.reserve(order_type='ENTRY', symbol='BTCUSDT', side='BUY',
                          recommendation_id=77, quote_amount=50.0)
        out = self.gate.recover_in_flight()
        self.assertEqual(out[0]['state'], ABORTED)

    def test_parallel_workers_only_one_order(self):
        """
        ستة عمّال متزامنين على نفس التوصية ⇒ أمر واحد ومالك واحد.

        النسخة السابقة كانت تعلّق العملية: Barrier(6) مع سبعة منتظرين
        (الرئيسي طرف سابع) وخيوط غير daemon. انظر tests/concurrency.py.
        """
        import threading as _th
        from tests.concurrency import ConcurrentRunner, assert_no_leaked_threads
        before = _th.active_count()
        path = self.db.path
        ex = self.ex

        def setup(i):
            return Database(path)

        def work(i, db):
            g = IdempotentOrderGate(db, ex, sleep_fn=lambda s: None)
            try:
                return g.execute(
                    order_type='ENTRY', symbol='BTCUSDT', side='BUY',
                    recommendation_id=123, quote_amount=500.0,
                    send=lambda cid: ex.market_buy_quote('BTCUSDT', 500.0, cid))
            except DuplicateOrderError:
                return {'duplicate': True}

        runner = ConcurrentRunner(6, timeout=20.0)
        outcomes = runner.run(work, setup=setup)

        self.assertEqual(runner.stuck_threads, [], 'خيوط عالقة')
        self.assertEqual(len(outcomes), 6, 'لم يكمل كل العمّال')
        unexpected = [o for o in outcomes
                      if o.error is not None
                      and not isinstance(o.error, DuplicateOrderError)]
        self.assertEqual(unexpected, [],
                         f'أخطاء غير متوقعة: {[o.traceback_text for o in unexpected]}')

        self.assertEqual(len(ex.orders), 1,
                         f'أمر واحد فقط، وُجد {len(ex.orders)}')
        # الضمان: عامل واحد فقط "مالك" (أرسل الأمر بنفسه). البقية إما
        # DuplicateOrderError أو نتيجة مسترجَعة موسومة not_owner — وهي
        # صحيحة دلالياً (الأمر موجود) لكنها لا تخوّل تسجيل مركز.
        results = [o.result for o in outcomes if isinstance(o.result, dict)]
        owners = [r for r in results
                  if r.get('ok') and not r.get('recovered')
                  and not r.get('not_owner')]
        self.assertEqual(len(owners), 1, f'أكثر من مالك: {owners}')
        for r in results:
            if r.get('ok') and r not in owners:
                self.assertTrue(r.get('not_owner'),
                                f'نتيجة ناجحة بلا وسم not_owner: {r}')
        for o in outcomes:
            r = o.result
            if isinstance(r, dict) and not r.get('ok') and not r.get('duplicate'):
                self.assertTrue(r.get('not_owner') or r.get('error'))
        assert_no_leaked_threads(self, before)

    def test_sell_also_protected(self):
        r1 = self.gate.execute(order_type='EXIT', symbol='BTCUSDT', side='SELL',
                               position_id=3, qty=0.01,
                               send=lambda cid: self.ex.market_sell('BTCUSDT', 0.01, cid))
        self.assertTrue(r1['ok'])
        with self.assertRaises(DuplicateOrderError):
            self.gate.execute(order_type='EXIT', symbol='BTCUSDT', side='SELL',
                              position_id=3, qty=0.01,
                              send=lambda cid: self.ex.market_sell('BTCUSDT', 0.01, cid))

    def test_stop_version_allows_intentional_replacement(self):
        a = self.gate.execute(order_type='STOP', symbol='BTCUSDT', side='SELL',
                              position_id=4, version=1, qty=0.01, stop_price=49000,
                              send=lambda cid: self.ex.stop_loss_limit(
                                  'BTCUSDT', 0.01, 49000, 48900, cid))
        b = self.gate.execute(order_type='STOP', symbol='BTCUSDT', side='SELL',
                              position_id=4, version=2, qty=0.01, stop_price=49000,
                              send=lambda cid: self.ex.stop_loss_limit(
                                  'BTCUSDT', 0.01, 49000, 48900, cid))
        self.assertTrue(a['ok'] and b['ok'])
        self.assertNotEqual(a['client_order_id'], b['client_order_id'])


class TestDatabaseGuards(unittest.TestCase):
    def setUp(self): self.db = tmpdb()

    def test_intent_unique(self):
        kw = dict(client_order_id='x1', order_type='ENTRY', symbol='B',
                  side='BUY', state=RESERVED, fingerprint='f')
        self.assertGreater(self.db.reserve_intent(**kw), 0)
        self.assertEqual(self.db.reserve_intent(**kw), 0)

    def test_order_client_id_unique(self):
        a = self.db.save_order(symbol='B', side='BUY', type='MARKET',
                               status='FILLED', client_order_id='c1')
        b = self.db.save_order(symbol='B', side='BUY', type='MARKET',
                               status='FILLED', client_order_id='c1')
        self.assertGreater(a, 0); self.assertEqual(b, 0)

    def test_one_position_per_recommendation(self):
        sig = {'symbol': 'B', 'interval': '4h', 'timestamp': 1, 'decision': 'BUY',
               'strategy_version': 'v', 'evidence': [], 'reasons': []}
        sid = self.db.save_signal(sig)
        rid = self.db.save_recommendation(sid, sig, True)
        self.assertGreater(self.db.open_position(recommendation_id=rid, symbol='B',
                                                 qty=1, entry_price=100), 0)
        self.assertEqual(self.db.open_position(recommendation_id=rid, symbol='B',
                                               qty=1, entry_price=100), 0)

    def test_process_lock_excludes(self):
        self.assertTrue(self.db.acquire_lock('t', 1, 'h'))
        self.assertFalse(self.db.acquire_lock('t', 2, 'h'))
        self.db.release_lock('t', 1)
        self.assertTrue(self.db.acquire_lock('t', 2, 'h'))

    def test_stale_lock_reclaimed(self):
        self.db.acquire_lock('t', 1, 'h')
        self.db.execute('UPDATE process_lock SET heartbeat_ts=? WHERE name=?',
                        (int(time.time() * 1000) - 10 ** 6, 't'))
        self.assertTrue(self.db.acquire_lock('t', 2, 'h'))

    def test_random_client_id_forbidden(self):
        from src.execution.binance_client import BinanceClient
        with self.assertRaises(RuntimeError):
            BinanceClient.new_client_id()


if __name__ == '__main__':
    unittest.main(verbosity=2)
