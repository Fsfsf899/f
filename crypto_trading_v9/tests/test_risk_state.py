"""
اختبارات استمرارية RiskGuard — RISK_STATE_DESIGN.md.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.storage.database import Database
from src.risk.risk_guard import RiskGuard, ACCOUNT_WIDE, PER_SYMBOL
from src.core.config import RiskConfig
from src.execution.order_manager import OrderManager
from src.execution.paper_broker import PaperBroker
from src.data.binance import BinancePublic
from src.core.config import Config


def tmpdb():
    return Database(os.path.join(tempfile.mkdtemp(), 't.db'))


class Test01_BasicBehavior(unittest.TestCase):
    def test_loss_increments_counter(self):
        g = RiskGuard()
        g.record_trade(-10.0)
        self.assertEqual(g.consecutive_losses, 1)
        g.record_trade(-5.0)
        self.assertEqual(g.consecutive_losses, 2)

    def test_win_resets_counter(self):
        g = RiskGuard()
        g.record_trade(-10.0); g.record_trade(-5.0)
        g.record_trade(20.0)
        self.assertEqual(g.consecutive_losses, 0)

    def test_new_day_resets_per_documented_policy(self):
        """السياسة الموثَّقة: consecutive_losses يُصفَّر كل يوم — لا شلل دائم."""
        g = RiskGuard(RiskConfig(max_consecutive_losses=3))
        g.new_day(10000.0)
        for _ in range(3):
            g.record_trade(-1.0)
        self.assertFalse(g.can_trade(9997)['allowed'])
        g.new_day(9997.0)
        self.assertEqual(g.consecutive_losses, 0)
        self.assertTrue(g.can_trade(9997)['allowed'])


class Test02_Persistence(unittest.TestCase):
    def test_state_saved_after_mutation(self):
        db = tmpdb()
        g = RiskGuard(db=db)
        g.new_day(10000.0, day_key='2026-01-01')
        g.record_trade(-10.0, trade_id='t1')
        raw = db.get_kv('risk_state')
        self.assertEqual(raw['consecutive_losses'], 1)
        self.assertEqual(raw['day_key'], '2026-01-01')

    def test_restart_restores_state(self):
        db = tmpdb()
        g1 = RiskGuard(db=db)
        g1.new_day(10000.0, day_key='2026-01-01')
        g1.record_trade(-10.0, trade_id='t1')
        g1.record_trade(-5.0, trade_id='t2')

        g2 = RiskGuard(db=Database(db.path))   # كائن جديد، نفس الملف
        self.assertEqual(g2.consecutive_losses, 2)
        self.assertEqual(g2.day_key, '2026-01-01')
        self.assertEqual(g2.last_trade_id, 't2')

    def test_restart_logs_risk_state_restored(self):
        db = tmpdb()
        RiskGuard(db=db).new_day(10000.0)
        RiskGuard(db=Database(db.path))
        rows = db.query("SELECT 1 FROM system_events WHERE kind='RISK_STATE_RESTORED'")
        self.assertGreaterEqual(len(rows), 2)   # مرة لكل بناء

    def test_can_trade_reflects_restored_state(self):
        db = tmpdb()
        g1 = RiskGuard(RiskConfig(max_consecutive_losses=2), db=db)
        g1.new_day(10000.0)
        g1.record_trade(-1.0, trade_id='a'); g1.record_trade(-1.0, trade_id='b')

        g2 = RiskGuard(RiskConfig(max_consecutive_losses=2), db=Database(db.path))
        self.assertFalse(g2.can_trade(9998)['allowed'])
        self.assertEqual(g2.can_trade(9998)['reason'], 'CONSECUTIVE_LOSS_LIMIT')

    def test_no_prior_state_starts_clean(self):
        db = tmpdb()
        g = RiskGuard(db=db)
        self.assertEqual(g.consecutive_losses, 0)
        self.assertFalse(g.halted)


class Test03_DuplicateFillProtection(unittest.TestCase):
    def test_same_trade_id_ignored(self):
        g = RiskGuard()
        ok1 = g.record_trade(-10.0, trade_id='fill_x')
        ok2 = g.record_trade(-10.0, trade_id='fill_x')
        self.assertTrue(ok1)
        self.assertFalse(ok2)
        self.assertEqual(g.consecutive_losses, 1, 'العداد تحرَّك مرتين لنفس fill')

    def test_duplicate_logged(self):
        db = tmpdb()
        g = RiskGuard(db=db)
        g.record_trade(-10.0, trade_id='fill_y')
        g.record_trade(-10.0, trade_id='fill_y')
        rows = db.query(
            "SELECT 1 FROM system_events WHERE kind='RISK_DUPLICATE_TRADE_IGNORED'")
        self.assertEqual(len(rows), 1)

    def test_different_trade_ids_both_count(self):
        g = RiskGuard()
        g.record_trade(-10.0, trade_id='a')
        g.record_trade(-10.0, trade_id='b')
        self.assertEqual(g.consecutive_losses, 2)

    def test_no_trade_id_always_processes(self):
        """استدعاء بلا trade_id (مثل الباكتست) — لا حماية تكرار، كالسابق."""
        g = RiskGuard()
        g.record_trade(-10.0)
        g.record_trade(-10.0)
        self.assertEqual(g.consecutive_losses, 2)


class Test04_Scope(unittest.TestCase):
    def test_default_scope_is_account_wide(self):
        g = RiskGuard()
        self.assertEqual(g.scope, ACCOUNT_WIDE)

    def test_per_symbol_raises_explicitly(self):
        with self.assertRaises(NotImplementedError):
            RiskGuard(scope=PER_SYMBOL)

    def test_scope_mismatch_on_restore_does_not_mix_state(self):
        """
        نطاق مختلف محفوظ سابقاً لا يُخلَط بصمت — الحالة القديمة تُجوهَل
        وتُسجَّل كتناقض، لا استخدامها كأنها بنطاق آخر.
        """
        db = tmpdb()
        db.set_kv('risk_state', {'scope': 'SOME_OTHER_SCOPE',
                                 'consecutive_losses': 99})
        g = RiskGuard(db=db)   # ACCOUNT_WIDE افتراضياً
        self.assertEqual(g.consecutive_losses, 0, 'اختلط بحالة نطاق آخر')
        rows = db.query(
            "SELECT 1 FROM risk_events WHERE kind='RISK_STATE_SCOPE_MISMATCH'")
        self.assertEqual(len(rows), 1)


class Test05_ConsistencyAcrossPaperAndBacktest(unittest.TestCase):
    """Paper وBacktest يستخدمان نفس الكائن (RiskGuard) — لا تطبيقين متوازيين."""

    def test_order_manager_accepts_risk_guard(self):
        db = tmpdb()
        guard = RiskGuard(db=db)
        pub = BinancePublic()
        pb = PaperBroker(db, pub)
        om = OrderManager(db, pb, Config(), risk_guard=guard)
        self.assertIs(om.risk_guard, guard)

    def test_order_manager_without_risk_guard_is_noop_backward_compatible(self):
        """الاستدعاء القديم بلا risk_guard يبقى يعمل بلا أي فرق."""
        db = tmpdb()
        pub = BinancePublic()
        pb = PaperBroker(db, pub)
        om = OrderManager(db, pb, Config())
        self.assertIsNone(om.risk_guard)


if __name__ == '__main__':
    unittest.main(verbosity=2)
