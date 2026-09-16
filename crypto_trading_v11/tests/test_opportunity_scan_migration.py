"""
اختبار ترحيل قاعدة بيانات من v4 (قبل محرك اختيار أفضل فرصة) إلى v5 —
القسم 93 من متطلبات V11 (78-98): يتحقق أن قاعدة V10/V11 قائمة فعلياً
(لا قاعدة جديدة) تترقّى بأمان وتكتسب جدول `opportunity_scans` بلا
فقدان أي بيانات موجودة.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.storage import migrations
from src.storage.database import Database


def make_v4_database(path: str):
    """
    يُحاكي قاعدة v4 حقيقية بأقل مخطط كافٍ (positions بأعمدة v4 الكاملة
    + بيانات فعلية) — لا قاعدة فارغة، كي يُثبت الترحيل عدم فقدان بيانات.
    """
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE positions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          symbol TEXT NOT NULL, qty REAL NOT NULL, entry_price REAL NOT NULL,
          opened_ts INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'OPEN',
          stop_loss REAL, sold_qty REAL DEFAULT 0,
          order_list_id TEXT, list_client_order_id TEXT,
          stop_order_id TEXT, stop_client_order_id TEXT,
          target_order_id TEXT, target_client_order_id TEXT
        );
        CREATE TABLE system_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts INTEGER NOT NULL, kind TEXT NOT NULL, detail TEXT
        );
    """)
    conn.execute(
        "INSERT INTO positions (symbol, qty, entry_price, opened_ts, status) "
        "VALUES ('BTCUSDT', 0.01, 50000.0, 1, 'OPEN')")
    conn.execute('PRAGMA user_version=4')
    conn.commit()
    conn.close()


class Test01_V4ToV5Migration(unittest.TestCase):
    def test_migration_adds_opportunity_scans_table_without_data_loss(self):
        path = os.path.join(tempfile.mkdtemp(), 'v4.db')
        make_v4_database(path)

        self.assertEqual(migrations.current_version(path), 4)
        report = migrations.run(path, verbose=False)
        # يتتبّع SCHEMA_VERSION لا رقماً حرفياً — الترقيم الثابت كان
        # يكسر هذا الاختبار عند كل ترحيل جديد بلا سبب حقيقي.
        self.assertEqual(report['to'], migrations.SCHEMA_VERSION)
        self.assertIn(5, report['applied'])
        self.assertEqual(migrations.current_version(path),
                         migrations.SCHEMA_VERSION)

        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn('opportunity_scans', tables)

        # البيانات القديمة (positions) نجت بلا فقدان
        pos = conn.execute("SELECT * FROM positions").fetchall()
        self.assertEqual(len(pos), 1)
        self.assertEqual(pos[0]['symbol'], 'BTCUSDT')
        conn.close()

    def test_migrated_database_accepts_a_real_opportunity_scan_save(self):
        """الترحيل لا يكفي وحده — الجدول الناتج يجب أن يعمل فعلياً مع Database."""
        path = os.path.join(tempfile.mkdtemp(), 'v4.db')
        make_v4_database(path)
        migrations.run(path, verbose=False)

        db = Database(path)   # يفتح القاعدة المُرحَّلة، لا ينشئ جديدة
        from src.selection.opportunity import RankingResult, OpportunityScore
        result = RankingResult(
            decision='BUY', selected_symbol='ETHUSDT', reason='BEST_ELIGIBLE_OPPORTUNITY',
            opportunities=[OpportunityScore(symbol='ETHUSDT', eligible=True,
                                            decision='BUY', score=75.0)])
        row_id = db.save_opportunity_scan(result, btc_context='LOW')
        self.assertGreater(row_id, 0)

        last = db.last_opportunity_scan()
        self.assertEqual(last['selected_symbol'], 'ETHUSDT')
        self.assertEqual(last['opportunities'][0]['score'], 75.0)


class Test02_FreshDatabaseHasTableFromStart(unittest.TestCase):
    def test_fresh_database_already_has_opportunity_scans(self):
        path = os.path.join(tempfile.mkdtemp(), 'fresh.db')
        db = Database(path)
        conn = sqlite3.connect(path)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn('opportunity_scans', tables)
        conn.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
