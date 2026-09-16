"""
ترحيل قاعدة البيانات — المرحلة الثالثة.
=======================================
ترحيل تراكمي آمن: يضيف أعمدة وفهارس دون حذف بيانات.

ضمانات:
  • كل ترحيل idempotent — تشغيله مرتين لا يضر
  • نسخة احتياطية تلقائية قبل أول ترحيل
  • رقم المخطط محفوظ في PRAGMA user_version
  • فشل الترحيل يستعيد النسخة الاحتياطية
"""
import os
import shutil
import sqlite3
import time
from typing import List, Tuple, Callable

SCHEMA_VERSION = 6


def _cols(conn, table: str) -> List[str]:
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    except sqlite3.Error:
        return []


def _table_exists(conn, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone() is not None


def _add_col(conn, table: str, col: str, decl: str):
    if _table_exists(conn, table) and col not in _cols(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def migrate_v1_to_v2(conn):
    """
    v2: تتبّع الكميات، بصمة الحمولة، سجل الانتقالات،
    وفهارس فريدة جزئية تسمح بـ NULL.
    """
    # ── order_intents: حقول التتبّع
    for col, decl in [
        ('payload_hash', 'TEXT'),
        ('filled_qty', 'REAL DEFAULT 0'),
        ('remaining_qty', 'REAL'),
        ('requested_qty', 'REAL'),
        ('last_error', 'TEXT'),
        ('last_error_class', 'TEXT'),
        ('full_identity', 'TEXT'),
    ]:
        _add_col(conn, 'order_intents', col, decl)

    # fingerprint القديم يصير payload_hash
    if _table_exists(conn, 'order_intents'):
        cols = _cols(conn, 'order_intents')
        if 'fingerprint' in cols and 'payload_hash' in cols:
            conn.execute("""UPDATE order_intents SET payload_hash=fingerprint
                            WHERE payload_hash IS NULL""")

    # ── orders: كميات فعلية
    for col, decl in [('filled_qty', 'REAL DEFAULT 0'),
                      ('remaining_qty', 'REAL'),
                      ('cummulative_quote', 'REAL DEFAULT 0'),
                      ('updated_ts', 'INTEGER')]:
        _add_col(conn, 'orders', col, decl)

    # ── positions: كميات مباعة
    for col, decl in [('sold_qty', 'REAL DEFAULT 0'),
                      ('target_order_id', 'TEXT')]:
        _add_col(conn, 'positions', col, decl)

    # ── سجل انتقالات الحالات
    conn.execute("""
        CREATE TABLE IF NOT EXISTS state_transitions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          client_order_id TEXT NOT NULL,
          from_state TEXT, to_state TEXT NOT NULL,
          reason TEXT, actor TEXT, ts INTEGER NOT NULL
        )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS ix_trans_cid
                    ON state_transitions(client_order_id, ts)""")

    # ── فهارس فريدة جزئية (تسمح بـ NULL متعدد)
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_client_order_id
                    ON orders(client_order_id) WHERE client_order_id IS NOT NULL""")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ux_fills_exchange_trade_id
                    ON fills(exchange_trade_id) WHERE exchange_trade_id IS NOT NULL""")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ux_intent_cid
                    ON order_intents(client_order_id)""")
    # أمر دخول واحد لكل توصية
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ux_entry_per_recommendation
                    ON order_intents(recommendation_id)
                    WHERE order_type='ENTRY' AND recommendation_id IS NOT NULL""")
    # وقف واحد فعّال لكل مركز/إصدار
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ux_stop_per_position_version
                    ON order_intents(position_id, version)
                    WHERE order_type='STOP' AND position_id IS NOT NULL""")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ux_target_per_position_version
                    ON order_intents(position_id, version)
                    WHERE order_type='TARGET' AND position_id IS NOT NULL""")
    conn.execute("""CREATE INDEX IF NOT EXISTS ix_intent_type_state
                    ON order_intents(order_type, state)""")


def migrate_v2_to_v3(conn):
    """v3: جداول حالة Setup (Breakout Retest / Pullback)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entry_setups (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          setup_id TEXT NOT NULL UNIQUE,
          setup_type TEXT NOT NULL,
          state TEXT NOT NULL,
          symbol TEXT NOT NULL, interval TEXT NOT NULL,
          strategy_version TEXT, config_fingerprint TEXT,
          created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
          breakout_level REAL, support_level REAL, invalidation_level REAL,
          expiry_at INTEGER,
          signal_id INTEGER,
          entered INTEGER NOT NULL DEFAULT 0,
          detail TEXT
        )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS ix_setup_symbol_state
                    ON entry_setups(symbol, interval, state)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS ix_setup_expiry
                    ON entry_setups(expiry_at)""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entry_setup_transitions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          setup_id TEXT NOT NULL,
          from_state TEXT, to_state TEXT NOT NULL,
          reason TEXT, ts INTEGER NOT NULL
        )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS ix_setup_trans
                    ON entry_setup_transitions(setup_id, ts)""")


def migrate_v3_to_v4(conn):
    """
    v4: إصلاح خلط OCO الحرج — orderListId كان يُخزَّن في كلا
    stop_order_id وtarget_order_id، فلا وسيلة للتمييز بين الأمر
    الفرعي الحقيقي للوقف والأمر الفرعي الحقيقي للهدف. يضيف أعمدة
    منفصلة بلا حذف أو تعديل البيانات الموجودة (Alter فقط).
    """
    cur = conn.execute("PRAGMA table_info(positions)")
    existing = {row[1] for row in cur.fetchall()}
    for col in ('order_list_id', 'list_client_order_id',
               'stop_client_order_id', 'target_client_order_id'):
        if col not in existing:
            conn.execute(f"ALTER TABLE positions ADD COLUMN {col} TEXT")


def migrate_v4_to_v5(conn):
    """
    v5: سجل تدقيق محرك اختيار أفضل فرصة (القسم 93 من متطلبات V11،
    الأقسام 78-98) — جدول جديد بالكامل، لا تعديل على جداول موجودة.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS opportunity_scans (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts INTEGER NOT NULL,
          scanned_symbols TEXT NOT NULL,
          decision TEXT NOT NULL,
          selected_symbol TEXT,
          reason TEXT,
          tie_break_applied INTEGER DEFAULT 0,
          btc_context TEXT,
          opportunities_json TEXT NOT NULL
        )""")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_oppscan_ts ON opportunity_scans(ts)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_oppscan_selected "
        "ON opportunity_scans(selected_symbol)")


def migrate_v5_to_v6(conn):
    """
    v6: خطط التحجيم (الأقسام 13 و25 من مواصفة V11 FINAL) — جدول جديد
    بالكامل، لا تعديل على جداول موجودة.

    تُخزَّن هنا لأن لوحة المتابعة تتصل بقاعدة **للقراءة فقط وبلا مفاتيح
    API**، فلا تستطيع سؤال بينانس بنفسها — ولا يجوز أن تستطيع. المتداول
    يحسب الخطة بمحجِّمه الكنسي، واللوحة تعرض ما حُسب.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sizing_plans (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts INTEGER NOT NULL,
          symbol TEXT NOT NULL,
          decision TEXT NOT NULL,
          reason TEXT,
          account_status TEXT,
          balance_age_s REAL,
          available_balance REAL,
          locked_balance REAL,
          usable_equity REAL,
          reserve_amount REAL,
          effective_risk_pct REAL,
          max_risk_amount REAL,
          entry REAL, stop REAL, target REAL,
          final_quantity REAL,
          position_value REAL,
          estimated_max_loss REAL,
          remaining_available REAL,
          plan_json TEXT NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_sizing_ts ON sizing_plans(ts)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_sizing_symbol ON sizing_plans(symbol)")


MIGRATIONS: List[Tuple[int, Callable]] = [
    (2, migrate_v1_to_v2), (3, migrate_v2_to_v3), (4, migrate_v3_to_v4),
    (5, migrate_v4_to_v5), (6, migrate_v5_to_v6)]


def current_version(path: str) -> int:
    if not os.path.exists(path):
        return 0
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def backup(path: str) -> str:
    """نسخة احتياطية قابلة للاسترجاع قبل أي تعديل بنيوي."""
    stamp = time.strftime('%Y%m%d-%H%M%S')
    dst = f"{path}.bak-{stamp}"
    shutil.copy2(path, dst)
    return dst


def run(path: str, verbose: bool = False) -> dict:
    """
    يطبّق الترحيلات الناقصة. يُرجع تقريراً.
    عند الفشل: يستعيد النسخة الاحتياطية ويرمي الاستثناء.
    """
    if not os.path.exists(path):
        return {'applied': [], 'from': 0, 'to': SCHEMA_VERSION,
                'note': 'قاعدة جديدة — المخطط يُنشأ كاملاً'}

    frm = current_version(path)
    pending = [(v, fn) for v, fn in MIGRATIONS if v > frm]
    if not pending:
        return {'applied': [], 'from': frm, 'to': frm, 'note': 'محدّثة'}

    bak = backup(path)
    conn = sqlite3.connect(path, timeout=30)
    applied = []
    try:
        conn.execute('PRAGMA foreign_keys=OFF')
        for v, fn in pending:
            if verbose:
                print(f'  ⇪ ترحيل إلى v{v}')
            fn(conn)
            conn.execute(f'PRAGMA user_version={v}')
            applied.append(v)
        conn.commit()
    except Exception:
        conn.close()
        shutil.copy2(bak, path)     # استرجاع
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return {'applied': applied, 'from': frm, 'to': SCHEMA_VERSION, 'backup': bak}
