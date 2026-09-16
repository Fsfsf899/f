"""
اختبارات انحدار لتدقيق ما بعد V11 (الجولة الثالثة) — التعافي والمصالحة.

كل اختبار هنا **يفشل** على الشجرة قبل الإصلاح — أُثبت بتشغيل الملف على
نسخة ما قبل الإصلاح.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tests.fake_exchange import FakeExchange
from src.storage.database import Database
from src.execution.order_manager import OrderManager, PreTradeCheck
from src.execution.reconciliation import Reconciler
from src.execution.idempotency import IdempotentOrderGate
from src.execution.binance_client import BinanceError
from src.execution.order_state import FILLED, CANCELED, OPEN
from src.risk.risk_guard import RiskGuard
from src.core.config import Config

ENTRY, STOP, NOTIONAL = 50000.0, 49000.0, 1000.0


class _OCOClient:
    """منصة وهمية: الأمر المفرد غير موجود، لكن قائمة OCO موجودة."""

    def __init__(self, leg_status):
        self.leg_status = leg_status

    def order_by_client_id(self, symbol, cid):
        raise BinanceError('[-2013] Order does not exist.', -2013)

    def query_oco(self, order_list_id=None, list_client_order_id=None):
        legs = [{'orderId': 1, 'status': self.leg_status[0],
                 'type': 'STOP_LOSS_LIMIT'},
                {'orderId': 2, 'status': self.leg_status[1],
                 'type': 'LIMIT_MAKER'}]
        done = all(l['status'] in ('FILLED', 'CANCELED') for l in legs)
        return {'orderListId': 7, 'listClientOrderId': list_client_order_id,
                'listStatusType': 'ALL_DONE' if done else 'EXEC_STARTED',
                'listOrderStatus': 'ALL_DONE' if done else 'EXECUTING',
                'symbol': 'BTCUSDT', 'orderReports': legs}


class Test01_OcoRecoveryReadsLegsNotListStatus(unittest.TestCase):
    """
    البق: `_try_recover_oco` كانت تترجم `listOrderStatus == 'ALL_DONE'`
    إلى FILLED مباشرة. لكن ALL_DONE في بينانس تعني فقط أن القائمة لم تعد
    نشطة — وتشمل **إلغاء الطرفين معاً** والمركز مفتوح تماماً.

    الأثر: تُسجَّل النية FILLED — "تنفيذ وقع" ولم يقع. و`_send_loop`
    يعامل FILLED كنجاح، فتُخزَّن معرّفات وقف وهدف مُلغيين على المركز
    فيبدو محمياً وهو مكشوف، وسجل التدقيق يقول إنه خرج وهو مفتوح.

    ملاحظة دقّة: هذا لا يمنع إعادة الحماية للأبد — `active_intents()`
    تستبعد الحالات النهائية فتُصدَر نسخة وقف جديدة. الضرر في زيف السجل
    وفي نافذة الانكشاف لا في استحالة التعافي؛ الاختبار الأخير أدناه
    يوثّق ذلك صراحةً (يمرّ قبل الإصلاح وبعده).
    """

    def _recover(self, legs):
        db = Database(os.path.join(tempfile.mkdtemp(), 'o.db'))
        g = IdempotentOrderGate(db, _OCOClient(legs))
        it = g.reserve(order_type='STOP', symbol='BTCUSDT', side='SELL',
                       position_id=1, version=1, qty=0.02, stop_price=STOP)
        g._transition(it.client_order_id, 'IN_FLIGHT', reason='t')
        g._transition(it.client_order_id, 'UNKNOWN', reason='t')
        return g.recover_one(g.get(it.client_order_id)), db

    def test_both_legs_canceled_is_not_filled(self):
        res, db = self._recover(['CANCELED', 'CANCELED'])
        self.assertEqual(res['state'], CANCELED,
                         'OCO مُلغاة بالكامل صُنِّفت كأن المركز خرج')

    def test_both_legs_canceled_is_recorded_as_critical(self):
        res, db = self._recover(['CANCELED', 'CANCELED'])
        self.assertTrue(
            db.query("SELECT 1 FROM risk_events WHERE kind='OCO_CANCELED_NOT_FILLED'"),
            'زوال الحماية بلا تنفيذ مرّ بصمت')

    def test_reprotection_possible_either_way(self):
        """
        حارس توثيقي: إعادة الحماية ممكنة قبل الإصلاح وبعده — يمرّ في
        الحالتين عمداً، ويوثّق حدود أثر البق بدل المبالغة فيه.
        """
        res, db = self._recover(['CANCELED', 'CANCELED'])
        om = OrderManager(db, _OCOClient(['CANCELED', 'CANCELED']), Config())
        self.assertIsNotNone(om._next_stop_version(1, intentional=False),
                             'تعذّرت إعادة حماية المركز إلى الأبد')

    def test_stop_leg_filled_is_filled(self):
        res, _ = self._recover(['FILLED', 'CANCELED'])
        self.assertEqual(res['state'], FILLED)

    def test_target_leg_filled_is_filled(self):
        res, _ = self._recover(['CANCELED', 'FILLED'])
        self.assertEqual(res['state'], FILLED)

    def test_still_executing_is_open(self):
        res, _ = self._recover(['NEW', 'NEW'])
        self.assertEqual(res['state'], OPEN)


class Test02_ReconRecordsRealExitNotZero(unittest.TestCase):
    """
    البق: `auto_resolve` كانت تُغلق المركز المفقود بـ `realized_pnl=0.0`
    دائماً. والحالة الشائعة أن الوقف نُفِّذ — أي خسارة حقيقية تُسجَّل
    صفراً، و`close_position()` لا تُحدِّث RiskGuard، فالحد اليومي وسلسلة
    الخسائر يُتجاوَزان. والمصالحة تسبق `guard_stops()` في `tick()` فتمنع
    المسار الصحيح من تسجيل الخروج.
    """

    def _stopped_out(self, with_manager=True):
        db = Database(os.path.join(tempfile.mkdtemp(), 'r.db'))
        ex = FakeExchange(price=ENTRY, quote_free=100000.0)
        rg = RiskGuard(Config().risk, db=db)
        om = OrderManager(db, ex, Config(), risk_guard=rg)
        om.gate._sleep = lambda s: None
        om.open_long(symbol='BTCUSDT', notional=NOTIONAL, entry_ref=ENTRY,
                     stop=STOP, target=None, recommendation_id=1,
                     check=PreTradeCheck(True))
        pos = db.open_positions()[0]
        qty = float(pos['qty'])
        for cid, o in list(ex.orders.items()):
            if str(o['orderId']) == str(pos.get('stop_order_id')):
                o['status'] = 'FILLED'
                o['executedQty'] = f'{qty:.8f}'
                o['cummulativeQuoteQty'] = f'{qty * STOP:.8f}'
        ex._base = 0.0
        rec = Reconciler(db, ex, gate=om.gate,
                         order_manager=om if with_manager else None)
        return rec.run(['BTCUSDT']), db, rg, pos, qty

    def test_real_loss_is_recorded_not_zero(self):
        _, db, rg, pos, qty = self._stopped_out()
        row = db.query('SELECT * FROM positions WHERE id=?', (pos['id'],))[0]
        self.assertEqual(row['status'], 'CLOSED')
        self.assertAlmostEqual(float(row['realized_pnl']), (STOP - ENTRY) * qty,
                               places=6, msg='الخسارة الحقيقية سُجِّلت صفراً')

    def test_risk_guard_sees_the_loss(self):
        """آخر خط دفاع: الخسارة يجب أن تدخل سلسلة الخسائر والحد اليومي."""
        _, db, rg, pos, qty = self._stopped_out()
        self.assertEqual(rg.consecutive_losses, 1,
                         'RiskGuard لم يرَ الخسارة — الحدود قابلة للتجاوز')

    def test_unprovable_exit_is_not_closed_at_zero(self):
        """بلا دليل خروج: لا إغلاق بصفر مُختلَق — يبقى مانعاً للتداول."""
        _, db, rg, pos, qty = self._stopped_out(with_manager=False)
        row = db.query('SELECT * FROM positions WHERE id=?', (pos['id'],))[0]
        self.assertEqual(row['status'], 'OPEN',
                         'أُغلق المركز بربح صفر مُختلَق رغم غياب الدليل')
        self.assertTrue(
            db.query("SELECT 1 FROM risk_events WHERE kind='RECON_EXIT_UNPROVEN'"),
            'الخروج غير المُثبَت لم يُسجَّل')

    def test_recon_still_blocks_trading(self):
        """الاختلاف يبقى مانعاً للتداول بعد التسجيل — حارس عدم انحدار."""
        res, db, rg, pos, qty = self._stopped_out()
        self.assertFalse(res.ok)
        self.assertTrue(res.blocks_trading)


if __name__ == '__main__':
    unittest.main(verbosity=2)
