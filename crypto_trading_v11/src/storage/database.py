"""
التخزين — البندان 31 و32.
=========================
SQLite بدل bot_state.json. السبب:
  • JSON يفسد عند انقطاع الكهرباء أثناء الكتابة
  • لا يدعم استعلامات (ما دقّتي في السوق الهابط؟)
  • لا يدعم كتابة متزامنة

WAL مفعّل، والمعاملات ذرّية. حالة النظام قابلة للاستعادة كاملة.
"""
import sqlite3, json, os, time, threading
from typing import Optional, List, Dict, Any, Tuple
from contextlib import contextmanager

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL, symbol TEXT NOT NULL, interval TEXT NOT NULL,
  bar_time INTEGER NOT NULL, decision TEXT NOT NULL,
  strategy_version TEXT NOT NULL, config_fingerprint TEXT,
  score REAL, stars INTEGER, confidence REAL,
  raw_probability REAL, calibrated_probability REAL, probability_source TEXT,
  entry REAL, stop_loss REAL, take_profit REAL, risk_reward REAL, atr REAL,
  market_regime TEXT, data_quality REAL, btc_context TEXT,
  evidence TEXT, reasons TEXT,
  UNIQUE(symbol, interval, bar_time, strategy_version)
);
CREATE INDEX IF NOT EXISTS ix_sig_sym ON signals(symbol, ts);
CREATE INDEX IF NOT EXISTS ix_sig_dec ON signals(decision);

CREATE TABLE IF NOT EXISTS recommendations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  signal_id INTEGER REFERENCES signals(id),
  ts INTEGER NOT NULL, symbol TEXT NOT NULL, decision TEXT NOT NULL,
  acted INTEGER DEFAULT 0, reason TEXT,
  outcome TEXT, exit_price REAL, exit_reason TEXT,
  pnl REAL, pnl_pct REAL, mae_pct REAL, mfe_pct REAL,
  holding_bars INTEGER, closed_ts INTEGER
);
CREATE INDEX IF NOT EXISTS ix_rec_open ON recommendations(outcome);

CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  recommendation_id INTEGER REFERENCES recommendations(id),
  symbol TEXT NOT NULL, side TEXT NOT NULL DEFAULT 'LONG',
  qty REAL NOT NULL, entry_price REAL NOT NULL,
  stop_loss REAL, take_profit REAL,
  opened_ts INTEGER NOT NULL, closed_ts INTEGER,
  status TEXT NOT NULL DEFAULT 'OPEN',
  entry_order_id TEXT, stop_order_id TEXT, exit_order_id TEXT,
  realized_pnl REAL, fees REAL DEFAULT 0,
  sold_qty REAL DEFAULT 0, target_order_id TEXT,
  order_list_id TEXT, list_client_order_id TEXT,
  stop_client_order_id TEXT, target_client_order_id TEXT,
  UNIQUE(recommendation_id)
);
CREATE INDEX IF NOT EXISTS ix_pos_status ON positions(status);

CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id INTEGER REFERENCES positions(id),
  exchange_order_id TEXT, client_order_id TEXT,
  symbol TEXT NOT NULL, side TEXT NOT NULL, type TEXT NOT NULL,
  qty REAL, price REAL, stop_price REAL,
  status TEXT NOT NULL, ts INTEGER NOT NULL,
  raw_response TEXT,
  filled_qty REAL DEFAULT 0, remaining_qty REAL,
  cummulative_quote REAL DEFAULT 0, updated_ts INTEGER
);
CREATE INDEX IF NOT EXISTS ix_ord_ex ON orders(exchange_order_id);

CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER REFERENCES orders(id),
  exchange_trade_id TEXT, symbol TEXT NOT NULL,
  qty REAL NOT NULL, price REAL NOT NULL,
  commission REAL, commission_asset TEXT, ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL, kind TEXT NOT NULL, severity TEXT NOT NULL,
  symbol TEXT, detail TEXT, equity REAL
);
CREATE INDEX IF NOT EXISTS ix_risk_ts ON risk_events(ts);

CREATE TABLE IF NOT EXISTS system_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL, kind TEXT NOT NULL, detail TEXT
);

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
);
CREATE INDEX IF NOT EXISTS ix_oppscan_ts ON opportunity_scans(ts);
CREATE INDEX IF NOT EXISTS ix_oppscan_selected ON opportunity_scans(selected_symbol);

CREATE TABLE IF NOT EXISTS daily_equity (
  day TEXT PRIMARY KEY, starting_equity REAL NOT NULL,
  ending_equity REAL, realized_pnl REAL DEFAULT 0,
  trades INTEGER DEFAULT 0, halted INTEGER DEFAULT 0, halt_reason TEXT
);

CREATE TABLE IF NOT EXISTS data_quality (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL, symbol TEXT, interval TEXT,
  score REAL, completeness REAL, freshness REAL,
  integrity REAL, validity REAL, issues TEXT
);

CREATE TABLE IF NOT EXISTS backtests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL, symbol TEXT, interval TEXT,
  strategy_version TEXT, config_fingerprint TEXT,
  bars INTEGER, period_from INTEGER, period_to INTEGER,
  metrics TEXT, verdict TEXT
);

CREATE TABLE IF NOT EXISTS backtest_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  backtest_id INTEGER REFERENCES backtests(id),
  entry_time INTEGER, exit_time INTEGER,
  entry_price REAL, exit_price REAL, qty REAL,
  pnl REAL, exit_reason TEXT, stars INTEGER,
  raw_probability REAL, mae_pct REAL, mfe_pct REAL
);

CREATE TABLE IF NOT EXISTS order_intents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  client_order_id TEXT NOT NULL UNIQUE,
  order_type TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
  state TEXT NOT NULL, fingerprint TEXT NOT NULL,
  recommendation_id INTEGER, position_id INTEGER, version INTEGER DEFAULT 1,
  exchange_order_id TEXT, attempts INTEGER DEFAULT 0,
  created_ts INTEGER NOT NULL, updated_ts INTEGER NOT NULL, detail TEXT,
  payload_hash TEXT, full_identity TEXT,
  requested_qty REAL, filled_qty REAL DEFAULT 0, remaining_qty REAL,
  last_error TEXT, last_error_class TEXT
);
CREATE INDEX IF NOT EXISTS ix_intent_state ON order_intents(state);
CREATE INDEX IF NOT EXISTS ix_intent_pos ON order_intents(position_id);

CREATE TABLE IF NOT EXISTS process_lock (
  name TEXT PRIMARY KEY, pid INTEGER NOT NULL, host TEXT,
  acquired_ts INTEGER NOT NULL, heartbeat_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS state_transitions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  client_order_id TEXT NOT NULL,
  from_state TEXT, to_state TEXT NOT NULL,
  reason TEXT, actor TEXT, ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_trans_cid ON state_transitions(client_order_id, ts);

CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_client_order_id
  ON orders(client_order_id) WHERE client_order_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_fills_exchange_trade_id
  ON fills(exchange_trade_id) WHERE exchange_trade_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_entry_per_recommendation
  ON order_intents(recommendation_id)
  WHERE order_type='ENTRY' AND recommendation_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_stop_per_position_version
  ON order_intents(position_id, version)
  WHERE order_type='STOP' AND position_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_target_per_position_version
  ON order_intents(position_id, version)
  WHERE order_type='TARGET' AND position_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_intent_type_state ON order_intents(order_type, state);

CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT, ts INTEGER);

-- Setup state machine (Breakout Retest / Pullback) — البند 10
CREATE TABLE IF NOT EXISTS entry_setups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  setup_id TEXT NOT NULL UNIQUE,
  setup_type TEXT NOT NULL,           -- BREAKOUT | PULLBACK
  state TEXT NOT NULL,
  symbol TEXT NOT NULL, interval TEXT NOT NULL,
  strategy_version TEXT, config_fingerprint TEXT,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
  breakout_level REAL, support_level REAL, invalidation_level REAL,
  expiry_at INTEGER,
  signal_id INTEGER,
  entered INTEGER NOT NULL DEFAULT 0,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_setup_symbol_state
  ON entry_setups(symbol, interval, state);
CREATE INDEX IF NOT EXISTS ix_setup_expiry ON entry_setups(expiry_at);

CREATE TABLE IF NOT EXISTS entry_setup_transitions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  setup_id TEXT NOT NULL,
  from_state TEXT, to_state TEXT NOT NULL,
  reason TEXT, ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_setup_trans ON entry_setup_transitions(setup_id, ts);
"""


class Database:
    def __init__(self, path: str = 'trading.db'):
        self.path = path
        self._lock = threading.RLock()
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        # executescript ينهي المعاملة ضمنياً، فلا يُلف بـ BEGIN/COMMIT
        existed = os.path.exists(path) and os.path.getsize(path) > 0
        if existed:
            # ترحيل آمن لقاعدة قائمة قبل لمس المخطط
            from . import migrations
            self.migration = migrations.run(path)
        else:
            self.migration = {'applied': [], 'note': 'قاعدة جديدة'}

        conn = sqlite3.connect(self.path, timeout=30)
        try:
            conn.executescript(SCHEMA)
            from .migrations import SCHEMA_VERSION
            conn.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def _conn(self):
        with self._lock:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute('BEGIN IMMEDIATE')
                yield conn
                if conn.in_transaction:
                    conn.execute('COMMIT')
            except Exception:
                try:
                    if conn.in_transaction:
                        conn.execute('ROLLBACK')
                except Exception:
                    pass
                raise
            finally:
                conn.close()

    def _ins(self, table: str, row: Dict[str, Any]) -> int:
        cols = ','.join(row)
        marks = ','.join('?' * len(row))
        with self._conn() as c:
            cur = c.execute(f'INSERT OR IGNORE INTO {table}({cols}) VALUES({marks})',
                            list(row.values()))
            return int(cur.lastrowid or 0)

    def query(self, sql: str, params=()) -> List[Dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, params).fetchall()]

    def execute(self, sql: str, params=()):
        with self._conn() as c:
            return c.execute(sql, params).rowcount

    # ── إشارات ──
    def save_signal(self, sig: Dict, fingerprint: str = '') -> int:
        return self._ins('signals', {
            'ts': int(time.time() * 1000), 'symbol': sig['symbol'],
            'interval': sig['interval'], 'bar_time': sig['timestamp'],
            'decision': sig['decision'],
            'strategy_version': sig['strategy_version'],
            'config_fingerprint': fingerprint,
            'score': sig.get('score'), 'stars': sig.get('stars'),
            'confidence': sig.get('confidence'),
            'raw_probability': sig.get('raw_probability'),
            'calibrated_probability': sig.get('calibrated_probability'),
            'probability_source': sig.get('probability_source'),
            'entry': sig.get('entry'), 'stop_loss': sig.get('stop_loss'),
            'take_profit': sig.get('take_profit'),
            'risk_reward': sig.get('risk_reward'), 'atr': sig.get('atr'),
            'market_regime': sig.get('regime'),
            'data_quality': sig.get('data_quality'),
            'btc_context': sig.get('btc_context'),
            'evidence': json.dumps(sig.get('evidence', []), ensure_ascii=False),
            'reasons': json.dumps(sig.get('reasons', []), ensure_ascii=False)})

    # ── توصيات ──
    def save_recommendation(self, signal_id: int, sig: Dict,
                            acted: bool, reason: str = '') -> int:
        return self._ins('recommendations', {
            'signal_id': signal_id, 'ts': int(time.time() * 1000),
            'symbol': sig['symbol'], 'decision': sig['decision'],
            'acted': int(acted), 'reason': reason})

    def close_recommendation(self, rec_id: int, *, outcome: str, exit_price: float,
                             exit_reason: str, pnl: float, pnl_pct: float,
                             mae_pct: float = 0.0, mfe_pct: float = 0.0,
                             holding_bars: int = 0):
        self.execute("""UPDATE recommendations SET outcome=?, exit_price=?, exit_reason=?,
                        pnl=?, pnl_pct=?, mae_pct=?, mfe_pct=?, holding_bars=?, closed_ts=?
                        WHERE id=?""",
                     (outcome, exit_price, exit_reason, pnl, pnl_pct,
                      mae_pct, mfe_pct, holding_bars, int(time.time() * 1000), rec_id))

    # ── مراكز ──
    def open_position(self, **kw) -> int:
        kw.setdefault('opened_ts', int(time.time() * 1000))
        kw.setdefault('status', 'OPEN')
        return self._ins('positions', kw)

    def close_position(self, pos_id: int, realized_pnl: float, fees: float = 0.0,
                       exit_order_id: str = ''):
        self.execute("""UPDATE positions SET status='CLOSED', closed_ts=?,
                        realized_pnl=?, fees=?, exit_order_id=? WHERE id=?""",
                     (int(time.time() * 1000), realized_pnl, fees, exit_order_id, pos_id))

    def open_positions(self) -> List[Dict]:
        return self.query("SELECT * FROM positions WHERE status='OPEN'")

    def update_position(self, pos_id: int, **kw):
        if not kw: return
        sets = ','.join(f'{k}=?' for k in kw)
        self.execute(f'UPDATE positions SET {sets} WHERE id=?',
                     list(kw.values()) + [pos_id])

    # ── أوامر وتنفيذات ──
    def save_order(self, **kw) -> int:
        kw.setdefault('ts', int(time.time() * 1000))
        if isinstance(kw.get('raw_response'), (dict, list)):
            kw['raw_response'] = json.dumps(kw['raw_response'], ensure_ascii=False)
        return self._ins('orders', kw)

    def save_fill(self, **kw) -> int:
        kw.setdefault('ts', int(time.time() * 1000))
        return self._ins('fills', kw)

    # ── أحداث ──
    def risk_event(self, kind: str, severity: str, detail: str = '',
                   symbol: str = '', equity: Optional[float] = None):
        self._ins('risk_events', {'ts': int(time.time() * 1000), 'kind': kind,
                                  'severity': severity, 'symbol': symbol,
                                  'detail': detail, 'equity': equity})

    def system_event(self, kind: str, detail: str = ''):
        self._ins('system_events', {'ts': int(time.time() * 1000),
                                    'kind': kind, 'detail': detail})

    def save_opportunity_scan(self, result, btc_context: str = 'UNKNOWN') -> int:
        """
        سجل التدقيق الكامل لمسح أفضل فرصة — القسم 93 من متطلبات V11
        (78-98). يُخزِّن `RankingResult` كاملاً (كل رمز مُقيَّم بدرجته
        وأسباب رفضه، لا الفائز فقط) — يُمكِّن لاحقاً الإجابة على
        "لماذا اختار النظام SOL بدل ETH؟" من سجل حقيقي، لا استنتاجاً.
        """
        opportunities = [o.to_dict() for o in result.opportunities]
        symbols = [o.symbol for o in result.opportunities]
        row_id = self._ins('opportunity_scans', {
            'ts': int(time.time() * 1000),
            'scanned_symbols': json.dumps(symbols, ensure_ascii=False),
            'decision': result.decision,
            'selected_symbol': result.selected_symbol,
            'reason': result.reason,
            'tie_break_applied': int(bool(result.tie_break_applied)),
            'btc_context': btc_context,
            'opportunities_json': json.dumps(opportunities, ensure_ascii=False),
        })
        return row_id

    def recent_opportunity_scans(self, limit: int = 20) -> List[Dict]:
        rows = self.query(
            "SELECT * FROM opportunity_scans ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r['scanned_symbols'] = json.loads(r['scanned_symbols'] or '[]')
            r['opportunities'] = json.loads(r['opportunities_json'] or '[]')
            del r['opportunities_json']
        return rows

    def last_opportunity_scan(self) -> Optional[Dict]:
        rows = self.recent_opportunity_scans(limit=1)
        return rows[0] if rows else None

    def save_data_quality(self, symbol: str, interval: str, q: Dict):
        self._ins('data_quality', {
            'ts': int(time.time() * 1000), 'symbol': symbol, 'interval': interval,
            'score': q.get('score'), 'completeness': q.get('completeness'),
            'freshness': q.get('freshness'), 'integrity': q.get('integrity'),
            'validity': q.get('validity'),
            'issues': json.dumps(q.get('issues', []), ensure_ascii=False)})

    # ── حقوق يومية ──
    def start_day(self, day: str, equity: float):
        with self._conn() as c:
            c.execute("""INSERT INTO daily_equity(day, starting_equity)
                         VALUES(?,?) ON CONFLICT(day) DO NOTHING""", (day, equity))

    def get_day(self, day: str) -> Optional[Dict]:
        r = self.query('SELECT * FROM daily_equity WHERE day=?', (day,))
        return r[0] if r else None

    def update_day(self, day: str, **kw):
        if not kw: return
        sets = ','.join(f'{k}=?' for k in kw)
        self.execute(f'UPDATE daily_equity SET {sets} WHERE day=?',
                     list(kw.values()) + [day])

    def execute_script(self, script: str):
        """تشغيل DDL. executescript ينهي المعاملة ضمنياً فلا يُلف."""
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            conn.executescript(script)
            conn.commit()
        finally:
            conn.close()

    def insert_ignore(self, table: str, row: Dict) -> int:
        return self._ins(table, row)

    # ── نوايا الأوامر ──
    def reserve_intent(self, **kw) -> int:
        """
        حجز ذرّي. يُرجع 0 إذا كان المعرّف محجوزاً — وهذا هو ما يحسم
        السباق بين عمليتين متوازيتين: كلتاهما تحاول، واحدة تفوز.
        """
        now = int(time.time() * 1000)
        kw.setdefault('created_ts', now)
        kw.setdefault('updated_ts', now)
        kw.setdefault('attempts', 0)
        return self._ins('order_intents', kw)

    def intent_by_cid(self, cid: str) -> Optional[Dict]:
        r = self.query('SELECT * FROM order_intents WHERE client_order_id=?', (cid,))
        return r[0] if r else None

    def active_entry_intent(self, recommendation_id: int) -> Optional[Dict]:
        """أي نية دخول غير ملغاة لهذه التوصية = ممنوع دخول ثانٍ."""
        r = self.query("""SELECT * FROM order_intents
                          WHERE recommendation_id=? AND order_type='ENTRY'
                            AND state NOT IN ('ABORTED','REJECTED','FAILED')
                          LIMIT 1""", (recommendation_id,))
        return r[0] if r else None

    def get_intent_by_client_order_id(self, cid: str) -> Optional[Dict]:
        return self.intent_by_cid(cid)

    # ══ حالة Setup — Breakout Retest / Pullback (البند 10) ══
    def reserve_setup(self, **kw) -> int:
        """
        حجز ذرّي بنفس نمط reserve_intent — UNIQUE(setup_id) يحسم
        السباق بين تقييمين متزامنين لنفس شرط الاكتشاف.
        """
        now = int(time.time() * 1000)
        kw.setdefault('created_at', now)
        kw.setdefault('updated_at', now)
        kw.setdefault('entered', 0)
        rid = self._ins('entry_setups', kw)
        if rid:
            self.execute(
                "INSERT INTO entry_setup_transitions"
                "(setup_id, from_state, to_state, reason, ts) VALUES(?,?,?,?,?)",
                (kw['setup_id'], None, kw['state'], 'CREATED', now))
        return rid

    def get_setup(self, setup_id: str) -> Optional[Dict]:
        r = self.query('SELECT * FROM entry_setups WHERE setup_id=?', (setup_id,))
        return r[0] if r else None

    def active_setups(self, symbol: str, interval: str,
                      setup_type: Optional[str] = None,
                      terminal: Tuple[str, ...] = (
                          'ENTERED', 'FAILED', 'EXPIRED', 'INVALIDATED')
                      ) -> List[Dict]:
        """Setups لم تصل لحالة نهائية بعد — لهذا الرمز/الفريم."""
        q = ("SELECT * FROM entry_setups WHERE symbol=? AND interval=? "
            "AND state NOT IN (%s)" % ','.join('?' * len(terminal)))
        params = [symbol, interval] + list(terminal)
        if setup_type:
            q += " AND setup_type=?"; params.append(setup_type)
        return self.query(q, tuple(params))

    def transition_setup_state(self, setup_id: str, to_state: str, *,
                               reason: str = '', **fields) -> bool:
        """
        يُحدِّث الحالة ويسجّل الانتقال ذرّياً. يُرجع False إن كان
        setup_id غير موجود (لا يرمي — النداء الخاطئ يُكتشف بالنتيجة).
        """
        cur = self.get_setup(setup_id)
        if cur is None:
            return False
        now = int(time.time() * 1000)
        sets = ['state=?', 'updated_at=?']
        params: List[Any] = [to_state, now]
        for k, v in fields.items():
            sets.append(f'{k}=?'); params.append(v)
        params.append(setup_id)
        self.execute(f"UPDATE entry_setups SET {', '.join(sets)} "
                    f"WHERE setup_id=?", tuple(params))
        self.execute(
            "INSERT INTO entry_setup_transitions"
            "(setup_id, from_state, to_state, reason, ts) VALUES(?,?,?,?,?)",
            (setup_id, cur['state'], to_state, reason, now))
        return True

    def mark_setup_entered(self, setup_id: str) -> bool:
        """
        علامة ذرّية تمنع دخولاً مزدوجاً لنفس setup — UPDATE مشروط
        بـ entered=0 الحالي، لا قراءة-ثم-كتابة عرضة لسباق.
        """
        n = self.execute(
            "UPDATE entry_setups SET entered=1, updated_at=? "
            "WHERE setup_id=? AND entered=0",
            (int(time.time() * 1000), setup_id))
        return n > 0

    def setup_transitions(self, setup_id: str) -> List[Dict]:
        return self.query(
            "SELECT * FROM entry_setup_transitions WHERE setup_id=? ORDER BY ts",
            (setup_id,))

    def get_order_by_client_order_id(self, cid: str) -> Optional[Dict]:
        r = self.query('SELECT * FROM orders WHERE client_order_id=?', (cid,))
        return r[0] if r else None

    def get_order_by_recommendation_id(self, rec_id: int) -> Optional[Dict]:
        r = self.query("""SELECT o.* FROM orders o
                          JOIN positions p ON o.position_id = p.id
                          WHERE p.recommendation_id=? AND o.side='BUY' LIMIT 1""",
                       (rec_id,))
        return r[0] if r else None

    def insert_order_if_absent(self, **kw) -> Dict:
        """
        يُرجع {'created': bool, 'order': row}.
        السجل القائم يُرجَع كما هو بدل إنشاء نسخة ثانية.
        """
        cid = kw.get('client_order_id')
        if cid:
            ex = self.get_order_by_client_order_id(cid)
            if ex:
                return {'created': False, 'order': ex}
        oid = self.save_order(**kw)
        if oid == 0 and cid:
            return {'created': False, 'order': self.get_order_by_client_order_id(cid)}
        r = self.query('SELECT * FROM orders WHERE id=?', (oid,))
        return {'created': True, 'order': r[0] if r else None}

    def record_fill_if_absent(self, **kw) -> Dict:
        """التعبئة تُسجَّل مرة واحدة — يمنعها فهرس فريد على exchange_trade_id."""
        tid = kw.get('exchange_trade_id')
        if tid:
            ex = self.query('SELECT * FROM fills WHERE exchange_trade_id=?', (tid,))
            if ex:
                return {'created': False, 'fill': ex[0]}
        fid = self.save_fill(**kw)
        if fid == 0 and tid:
            ex = self.query('SELECT * FROM fills WHERE exchange_trade_id=?', (tid,))
            return {'created': False, 'fill': ex[0] if ex else None}
        r = self.query('SELECT * FROM fills WHERE id=?', (fid,))
        return {'created': True, 'fill': r[0] if r else None}

    def transition_order_state(self, cid: str, to_state: str, *,
                               reason: str = '', actor: str = '',
                               **fields) -> Dict:
        """
        انتقال ذرّي مع تحقق من الشرعية وتسجيل السجل.
        يُرجع {'ok', 'from', 'to', 'error'}.
        """
        from ..execution.order_state import can_transition, IllegalTransition
        now = int(time.time() * 1000)
        with self._conn() as c:
            row = c.execute(
                'SELECT state FROM order_intents WHERE client_order_id=?',
                (cid,)).fetchone()
            if row is None:
                return {'ok': False, 'from': None, 'to': to_state,
                        'error': 'نية غير موجودة'}
            frm = row['state']
            if frm == to_state and not fields:
                return {'ok': True, 'from': frm, 'to': to_state, 'error': None}
            if not can_transition(frm, to_state):
                c.execute("""INSERT INTO state_transitions
                             (client_order_id,from_state,to_state,reason,actor,ts)
                             VALUES(?,?,?,?,?,?)""",
                          (cid, frm, to_state, f'REJECTED:{reason}', actor, now))
                return {'ok': False, 'from': frm, 'to': to_state,
                        'error': f'انتقال غير مسموح {frm} → {to_state}'}
            upd = {'state': to_state, 'updated_ts': now, **fields}
            sets = ','.join(f'{k}=?' for k in upd)
            c.execute(f'UPDATE order_intents SET {sets} WHERE client_order_id=?',
                      list(upd.values()) + [cid])
            c.execute("""INSERT INTO state_transitions
                         (client_order_id,from_state,to_state,reason,actor,ts)
                         VALUES(?,?,?,?,?,?)""",
                      (cid, frm, to_state, reason, actor, now))
            return {'ok': True, 'from': frm, 'to': to_state, 'error': None}

    def transitions_for(self, cid: str) -> List[Dict]:
        return self.query(
            'SELECT * FROM state_transitions WHERE client_order_id=? ORDER BY id',
            (cid,))

    def unresolved_intents(self) -> List[Dict]:
        from ..execution.order_state import BLOCKING
        marks = ','.join('?' * len(BLOCKING))
        return self.query(
            f'SELECT * FROM order_intents WHERE state IN ({marks}) ORDER BY id',
            tuple(sorted(BLOCKING)))

    def active_intents(self, order_type: str, position_id: int) -> List[Dict]:
        from ..execution.order_state import TERMINAL
        marks = ','.join('?' * len(TERMINAL))
        return self.query(
            f"""SELECT * FROM order_intents WHERE order_type=? AND position_id=?
                AND state NOT IN ({marks}) ORDER BY version DESC""",
            (order_type, position_id) + tuple(sorted(TERMINAL)))

    # ── قفل العملية ──
    def acquire_lock(self, name: str, pid: int, host: str,
                     stale_after_ms: int = 180_000) -> bool:
        """
        قفل تعاوني في القاعدة. يمنع تشغيل عاملين على نفس الحساب.
        القفل الميت (نبض قديم) يُنتزع تلقائياً بعد stale_after_ms.
        """
        now = int(time.time() * 1000)
        with self._conn() as c:
            row = c.execute('SELECT * FROM process_lock WHERE name=?',
                            (name,)).fetchone()
            if row:
                if row['pid'] == pid and row['host'] == host:
                    c.execute('UPDATE process_lock SET heartbeat_ts=? WHERE name=?',
                              (now, name))
                    return True
                if now - row['heartbeat_ts'] < stale_after_ms:
                    return False
                c.execute("""UPDATE process_lock SET pid=?, host=?, acquired_ts=?,
                             heartbeat_ts=? WHERE name=?""", (pid, host, now, now, name))
                return True
            c.execute("""INSERT INTO process_lock(name,pid,host,acquired_ts,heartbeat_ts)
                         VALUES(?,?,?,?,?)""", (name, pid, host, now, now))
            return True

    def heartbeat(self, name: str, pid: int) -> bool:
        n = self.execute('UPDATE process_lock SET heartbeat_ts=? WHERE name=? AND pid=?',
                         (int(time.time() * 1000), name, pid))
        return n > 0

    def release_lock(self, name: str, pid: int):
        self.execute('DELETE FROM process_lock WHERE name=? AND pid=?', (name, pid))

    def lock_holder(self, name: str) -> Optional[Dict]:
        r = self.query('SELECT * FROM process_lock WHERE name=?', (name,))
        return r[0] if r else None

    # ── kv ──
    def set_kv(self, k: str, v: Any):
        with self._conn() as c:
            c.execute("""INSERT INTO kv(k,v,ts) VALUES(?,?,?)
                         ON CONFLICT(k) DO UPDATE SET v=excluded.v, ts=excluded.ts""",
                      (k, json.dumps(v, ensure_ascii=False), int(time.time() * 1000)))

    def get_kv(self, k: str, default=None):
        r = self.query('SELECT v FROM kv WHERE k=?', (k,))
        return json.loads(r[0]['v']) if r else default

    # ── باكتست ──
    def save_backtest(self, meta: Dict, metrics: Dict, trades: List[Dict],
                      verdict: str = '') -> int:
        bid = self._ins('backtests', {
            'ts': int(time.time() * 1000), 'symbol': meta.get('symbol'),
            'interval': meta.get('interval'),
            'strategy_version': meta.get('strategy_version'),
            'config_fingerprint': meta.get('config_fingerprint'),
            'bars': meta.get('bars'), 'period_from': meta.get('from_ms'),
            'period_to': meta.get('to_ms'),
            'metrics': json.dumps(metrics, ensure_ascii=False, default=str),
            'verdict': verdict})
        for t in trades:
            self._ins('backtest_trades', {
                'backtest_id': bid, 'entry_time': t.get('entry_time'),
                'exit_time': t.get('exit_time'), 'entry_price': t.get('entry_price'),
                'exit_price': t.get('exit_price'), 'qty': t.get('qty'),
                'pnl': t.get('pnl'), 'exit_reason': t.get('exit_reason'),
                'stars': t.get('stars'), 'raw_probability': t.get('raw_probability'),
                'mae_pct': t.get('mae_pct'), 'mfe_pct': t.get('mfe_pct')})
        return bid
