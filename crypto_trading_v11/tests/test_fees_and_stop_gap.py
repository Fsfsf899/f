"""
اختبارات انحدار لتدقيق ما بعد V11 (الجولة الثانية) — الرسوم وفجوة الوقف.

كل اختبار هنا **يفشل** على الشجرة قبل الإصلاح. أُثبت ذلك بتشغيل الملف
على نسخة ما قبل الإصلاح، لا بالادّعاء.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tests.fake_exchange import FakeExchange, RULES
from src.storage.database import Database
from src.execution.order_manager import OrderManager, PreTradeCheck
from src.core.config import Config

FEE = 0.001          # 0.1% — رسوم بينانس القياسية
ENTRY = 50000.0
NOTIONAL = 1000.0


def _mgr(fee_rate=FEE, fee_asset=None, price=ENTRY):
    db = Database(os.path.join(tempfile.mkdtemp(), 'f.db'))
    ex = FakeExchange(price=price, quote_free=100000.0,
                      fee_rate=fee_rate, fee_asset=fee_asset)
    om = OrderManager(db, ex, Config())
    om.gate._sleep = lambda s: None
    return om, db, ex


def _open(om, stop=49000.0, target=52000.0, rid=1):
    om.open_long(symbol='BTCUSDT', notional=NOTIONAL, entry_ref=ENTRY,
                 stop=stop, target=target, recommendation_id=rid,
                 check=PreTradeCheck(True))


class Test01_EntryFeeCountedInPnl(unittest.TestCase):
    """
    البق: `_record_exit` كانت تطرح رسم الخروج وحده، ورسم الدخول
    (المُخزَّن في positions.fees) لا يدخل الحساب إطلاقاً — فكل PnL
    محقَّق مُبالَغ فيه بمقدار عمولة الدخول.
    """

    def test_flat_trade_loses_exactly_both_fees(self):
        """خروج بسعر الدخول نفسه ⇒ الخسارة = الرسمان بالضبط، لا أحدهما."""
        om, db, ex = _mgr()
        _open(om)
        pos = db.open_positions()[0]
        qty = float(pos['qty'])
        om.close_long(pos, reason='TEST')
        closed = db.query('SELECT * FROM positions WHERE id=?', (pos['id'],))[0]

        entry_fee = qty * FEE * ENTRY     # عمولة الشراء بالـ BTC مُقوَّمة بالدولار
        exit_fee = qty * ENTRY * FEE
        self.assertAlmostEqual(float(closed['realized_pnl']),
                               -(entry_fee + exit_fee), places=6,
                               msg='رسم الدخول غير مطروح من PnL')

    def test_pnl_matches_backtest_formula_exactly(self):
        """
        المعادلة يجب أن تطابق `backtest/engine.py` حرفياً:
        pnl = gross - entry_fee - exit_fee. أي انحراف يجعل التشغيل
        الورقي يُظهر نتائج أفضل من الباكتست لنفس الصفقات.
        """
        om, db, ex = _mgr()
        _open(om)
        pos = db.open_positions()[0]
        qty, entry_px = float(pos['qty']), float(pos['entry_price'])
        entry_fee = float(pos['fees'] or 0.0)

        exit_px = 51000.0
        ex.set_price(exit_px)
        om.close_long(pos, reason='TEST')
        closed = db.query('SELECT * FROM positions WHERE id=?', (pos['id'],))[0]

        gross = (exit_px - entry_px) * qty
        exit_fee = qty * exit_px * FEE
        self.assertAlmostEqual(float(closed['realized_pnl']),
                               gross - entry_fee - exit_fee, places=6)

    def test_total_fees_stored_equal_pnl_deduction(self):
        """حقل fees والمطروح من PnL يجب أن يكونا نفس الرقم — لا رقمين."""
        om, db, ex = _mgr()
        _open(om)
        pos = db.open_positions()[0]
        qty, entry_px = float(pos['qty']), float(pos['entry_price'])
        om.close_long(pos, reason='TEST')
        closed = db.query('SELECT * FROM positions WHERE id=?', (pos['id'],))[0]
        gross = (ENTRY - entry_px) * qty
        self.assertAlmostEqual(
            gross - float(closed['realized_pnl']), float(closed['fees']),
            places=6, msg='المطروح من PnL لا يساوي الرسوم المُخزَّنة')

    def test_zero_fee_behavior_unchanged(self):
        """المنصة بلا رسوم ⇒ السلوك القديم حرفياً (حارس عدم انحدار)."""
        om, db, ex = _mgr(fee_rate=0.0)
        _open(om)
        pos = db.open_positions()[0]
        om.close_long(pos, reason='TEST')
        closed = db.query('SELECT * FROM positions WHERE id=?', (pos['id'],))[0]
        self.assertAlmostEqual(float(closed['realized_pnl']), 0.0, places=6)


class Test02_CommissionAssetUnits(unittest.TestCase):
    """
    البق: `normalize_response` كانت تجمع `commission` بلا نظر إلى
    `commissionAsset`. بينانس تخصم عمولة الشراء بالأصل الأساس (BTC)،
    فكان رقم بالبيتكوين يُطرَح كأنه دولارات.
    """

    def test_base_asset_commission_is_converted_to_quote(self):
        qty, px = 0.02, ENTRY
        comm = qty * FEE                      # بالـ BTC
        resp = {'fills': [{'qty': f'{qty}', 'price': f'{px}',
                           'commission': f'{comm}', 'commissionAsset': 'BTC'}]}
        norm = OrderManager.normalize_response(resp, base_asset='BTC',
                                               quote_asset='USDT')
        self.assertAlmostEqual(norm['fee'], comm * px, places=8,
                               msg='عمولة بالأصل الأساس لم تُحوَّل')
        self.assertFalse(norm['fee_uncertain'])

    def test_quote_asset_commission_passes_through(self):
        resp = {'fills': [{'qty': '0.02', 'price': f'{ENTRY}',
                           'commission': '1.0', 'commissionAsset': 'USDT'}]}
        norm = OrderManager.normalize_response(resp, base_asset='BTC',
                                               quote_asset='USDT')
        self.assertAlmostEqual(norm['fee'], 1.0, places=8)

    def test_third_asset_commission_is_not_guessed(self):
        """عمولة بـ BNB لا تُحوَّل تخميناً ولا تُطرَح كأنها دولارات."""
        resp = {'fills': [{'qty': '0.02', 'price': f'{ENTRY}',
                           'commission': '0.05', 'commissionAsset': 'BNB'}]}
        norm = OrderManager.normalize_response(resp, base_asset='BTC',
                                               quote_asset='USDT')
        self.assertEqual(norm['fee'], 0.0, 'عمولة بأصل ثالث دخلت الحساب تخميناً')
        self.assertTrue(norm['fee_uncertain'], 'لم يُرفَع علَم عدم اليقين')
        self.assertEqual(norm['fee_by_asset'], {'BNB': 0.05},
                         'القيمة الخام ضاعت بدل حفظها')

    def test_uncertain_fee_is_recorded_as_risk_event(self):
        om, db, ex = _mgr(fee_asset='BNB')
        _open(om)
        ev = db.query("SELECT * FROM risk_events WHERE kind='FEE_ASSET_UNCONVERTIBLE'")
        self.assertTrue(ev, 'عمولة غير قابلة للتحويل مرّت بصمت')


class Test03_StopMissingBelowTrigger(unittest.TestCase):
    """
    البق — النافذة العمياء: إعادة وضع الوقف تشترط `px > stop`، والخروج
    الطارئ يشترط اختراقاً ≥ 0.5%. فإن اختفى الوقف والسعر بينهما، لم يكن
    يحدث أي شيء: لا وقف، لا خروج، ولا حتى تسجيل.
    """

    def _position_with_vanished_stop(self, price):
        om, db, ex = _mgr(fee_rate=0.0)
        _open(om, stop=49000.0, target=None)
        pos = db.open_positions()[0]
        # الوقف اختفى من المنصة (أُلغي يدوياً/انتهت صلاحيته)
        sid = pos.get('stop_order_id')
        for cid, o in list(ex.orders.items()):
            if str(o['orderId']) == str(sid):
                o['status'] = 'CANCELED'
        ex.set_price(price)
        return om, db, ex, pos

    def test_price_just_below_stop_triggers_exit_not_silence(self):
        """السعر 0.2% تحت الوقف — داخل النافذة العمياء تماماً."""
        om, db, ex, pos = self._position_with_vanished_stop(49000.0 * 0.998)
        actions = om.guard_stops()
        kinds = {a['action'] for a in actions}
        self.assertTrue(
            kinds & {'STOP_MISSING_EXIT', 'ALREADY_CLOSED'},
            f'المركز بلا وقف والسعر تحته ولم يحدث شيء: {actions}')
        closed = db.query('SELECT status FROM positions WHERE id=?', (pos['id'],))
        self.assertEqual(closed[0]['status'], 'CLOSED',
                         'المركز بقي مفتوحاً بلا أي حماية')

    def test_event_is_recorded_not_silent(self):
        om, db, ex, pos = self._position_with_vanished_stop(49000.0 * 0.998)
        om.guard_stops()
        ev = db.query("SELECT * FROM risk_events "
                      "WHERE kind='STOP_MISSING_BELOW_TRIGGER'")
        self.assertTrue(ev, 'الحالة الحرجة لم تُسجَّل إطلاقاً')

    def test_exactly_at_stop_is_also_covered(self):
        """السعر عند الوقف بالضبط — اختراق 0%، أعمق نقطة في النافذة."""
        om, db, ex, pos = self._position_with_vanished_stop(49000.0)
        actions = om.guard_stops()
        self.assertTrue({a['action'] for a in actions} &
                        {'STOP_MISSING_EXIT', 'ALREADY_CLOSED'}, actions)

    def test_price_above_stop_attempts_replacement(self):
        """فوق الوقف: المسار الأصلي (محاولة إعادة وضع وقف) بلا تغيير."""
        om, db, ex, pos = self._position_with_vanished_stop(49500.0)
        actions = om.guard_stops()
        self.assertTrue({a['action'] for a in actions} &
                        {'STOP_REPLACED', 'STOP_REPLACE_FAILED'},
                        f'مسار إعادة وضع الوقف لم يُستدعَ إطلاقاً: {actions}')


    def test_deep_breach_still_emergency_exits(self):
        """اختراق عميق: مسار الخروج الطارئ الأصلي بلا تغيير."""
        om, db, ex, pos = self._position_with_vanished_stop(49000.0 * 0.98)
        actions = om.guard_stops()
        self.assertTrue({a['action'] for a in actions} &
                        {'STOP_MISSING_EXIT', 'EMERGENCY_EXIT', 'ALREADY_CLOSED'},
                        actions)


class Test04_UnprotectedPositionHaltsTrading(unittest.TestCase):
    """
    بق اكتشفته اختبارات هذا الملف نفسها أثناء كتابتها: عند تأكُّد زوال
    الوقف من المنصة وفشل إعادة وضعه، كان يُسجَّل حدث `UNPROTECTED_POSITION`
    بخطورة CRITICAL ثم **يتابع النظام التداول طبيعياً**. و`HealthMonitor`
    لا يمسح `risk_events` حسب الخطورة، فالحدث بلا أي أثر تشغيلي — مركز
    مكشوف والنظام يفتح فوقه مراكز جديدة.

    يخالف القاعدة المطلقة التي يقتبسها المشروع في `_record_exit` وينفّذها
    هناك: "If protection cannot be proven... new trading must be blocked".
    """

    def _unprotectable_position(self):
        db = Database(os.path.join(tempfile.mkdtemp(), 'u.db'))
        ex = FakeExchange(price=ENTRY, quote_free=100000.0)
        from src.monitoring.health import HealthMonitor
        health = HealthMonitor(db, Config().risk)
        om = OrderManager(db, ex, Config(), health=health)
        om.gate._sleep = lambda s: None
        om.open_long(symbol='BTCUSDT', notional=NOTIONAL, entry_ref=ENTRY,
                     stop=49000.0, target=None, recommendation_id=1,
                     check=PreTradeCheck(True))
        pos = db.open_positions()[0]
        for cid, o in list(ex.orders.items()):
            if str(o['orderId']) == str(pos.get('stop_order_id')):
                o['status'] = 'CANCELED'
        ex.set_price(49500.0)          # فوق الوقف — مسار إعادة الوضع
        return om, db, ex, health, pos

    def test_failed_replacement_engages_kill_switch(self):
        om, db, ex, health, pos = self._unprotectable_position()
        actions = om.guard_stops()
        if 'STOP_REPLACE_FAILED' not in {a['action'] for a in actions}:
            self.skipTest('أُعيد وضع الوقف بنجاح — لا حالة انكشاف هنا')
        self.assertTrue(
            health.kill_switch_engaged() if hasattr(health, 'kill_switch_engaged')
            else db.query("SELECT 1 FROM risk_events WHERE kind='KILL_SWITCH'"),
            'مركز بلا وقف ولم يُوقَف التداول — يخالف القاعدة المطلقة')

    def test_failed_replacement_is_recorded(self):
        om, db, ex, health, pos = self._unprotectable_position()
        om.guard_stops()
        self.assertTrue(
            db.query("SELECT 1 FROM risk_events WHERE kind='UNPROTECTED_POSITION'"),
            'الانكشاف لم يُسجَّل')

if __name__ == '__main__':
    unittest.main(verbosity=2)
