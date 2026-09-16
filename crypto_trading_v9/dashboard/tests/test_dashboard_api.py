"""
اختبارات Dashboard — القراءة فقط، الأمان، العزل.
THE DASHBOARD CANNOT TRADE — مثبَت آلياً هنا.
"""
import json, os, sys, tempfile, time, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'dashboard', 'backend'))

from src.storage.database import Database
from readonly_db import ReadOnlyDB, ReadOnlyViolation, DatabaseUnavailable
from queries import DashboardQueries
from app import create_app

SIG = {'symbol': 'BTCUSDT', 'interval': '4h', 'decision': 'BUY',
       'strategy_version': 'v9.0.0', 'score': 6.0, 'stars': 4,
       'confidence': 0.9, 'raw_probability': 0.62,
       'calibrated_probability': 0.58, 'probability_source': 'isotonic',
       'entry': 50000.0, 'stop_loss': 49000.0, 'take_profit': 52000.0,
       'risk_reward': 2.0, 'atr': 500.0, 'market_regime': 'TRENDING_BULLISH',
       'data_quality': 0.96, 'btc_context': 'LOW',
       'evidence': ['EMA_TREND', 'ADX_STRONG'], 'reasons': []}


def seed(n_closed=40):
    """FIXTURE — بيانات اختبار فقط، لا تُستخدم في الإنتاج."""
    d = tempfile.mkdtemp()
    path = os.path.join(d, 'paper.db')
    db = Database(path)
    db.set_kv('environment', 'paper')
    db.set_kv('run_started_ts', int(time.time() * 1000) - 20 * 86400000)
    db.set_kv('last_successful_cycle_ts', int(time.time() * 1000))
    db.set_kv('kill_switch', False)
    db.acquire_lock('trader:paper', 4242, 'testhost')

    base = 1700000000000
    for i in range(n_closed):
        s = dict(SIG, timestamp=base + i * 14400000,
                 calibrated_probability=0.50 + (i % 7) * 0.05,
                 market_regime='TRENDING_BULLISH' if i % 2 else 'RANGING')
        sid = db.save_signal(s)
        rid = db.save_recommendation(sid, s, True)
        pid = db.open_position(recommendation_id=rid, symbol='BTCUSDT',
                               qty=0.01, entry_price=50000,
                               stop_loss=49000, take_profit=52000,
                               stop_order_id=f'so{i}')
        win = i % 3 != 0
        pnl = 18.0 if win else -11.0
        db.close_position(pid, realized_pnl=pnl, fees=0.5)
        db.close_recommendation(rid, outcome='WIN' if win else 'LOSS',
                                exit_price=52000 if win else 49000,
                                exit_reason='TAKE_PROFIT' if win else 'STOP_LOSS',
                                pnl=pnl, pnl_pct=pnl / 500,
                                mae_pct=-1.2, mfe_pct=3.4, holding_bars=12)
    # مركز مفتوح
    s = dict(SIG, timestamp=base + 999 * 14400000, symbol='ETHUSDT')
    sid = db.save_signal(s)
    rid = db.save_recommendation(sid, s, True)
    db.open_position(recommendation_id=rid, symbol='ETHUSDT', qty=0.5,
                     entry_price=3000, stop_loss=2900, take_profit=3200,
                     stop_order_id='so-open')
    db.risk_event('KILL_SWITCH', 'CRITICAL', 'اختبار')
    db.system_event('STARTUP', 'paper')
    db.save_data_quality('BTCUSDT', '4h', {'score': 0.96, 'completeness': 1.0,
                                           'freshness': 0.98, 'integrity': 1.0,
                                           'validity': 1.0, 'issues': []})
    db.start_day(time.strftime('%Y-%m-%d', time.gmtime()), 10000)
    db.update_day(time.strftime('%Y-%m-%d', time.gmtime()), realized_pnl=42.15)
    return path


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = seed()
        os.environ.pop('DASHBOARD_USERS', None)
        os.environ.pop('DASHBOARD_CORS_ORIGIN', None)
        os.environ['DASHBOARD_API_HOST'] = '127.0.0.1'
        cls.app = create_app(cls.path, 'paper')
        cls.app.testing = True
        cls.app.config['LIMITER'].limit = 100000   # لا يعيق بقية الاختبارات
        cls.c = cls.app.test_client()

    def get(self, url):
        r = self.c.get(url)
        return r, (json.loads(r.data) if r.data else None)

    def data(self, url):
        _, b = self.get(url)
        return b['data']


# ══ 1. فرض القراءة فقط ══
class Test01_ReadOnlyEnforcement(Base):
    WRITE_TARGETS = [
        '/api/orders', '/api/trades', '/api/signals', '/api/positions',
        '/api/config', '/api/settings', '/api/recommendations',
        '/api/dashboard', '/api/health', '/', '/api/anything',
    ]
    # /api/auth/* مستثناة عمداً: جلسات عرض فقط، لا تمسّ بيانات المحرك

    def test_all_write_methods_rejected(self):
        for path in self.WRITE_TARGETS:
            for m in ('post', 'put', 'patch', 'delete'):
                with self.subTest(path=path, method=m):
                    r = getattr(self.c, m)(path, json={'x': 1})
                    self.assertIn(r.status_code, (403, 405),
                                  f'{m.upper()} {path} → {r.status_code}')

    def test_rejection_before_routing(self):
        """الرفض يقع قبل التوجيه — حتى المسارات غير الموجودة."""
        r = self.c.post('/api/does-not-exist')
        self.assertEqual(r.status_code, 405)
        self.assertEqual(json.loads(r.data)['error']['code'],
                         'READ_ONLY_DASHBOARD')

    def test_no_trading_endpoints_exist(self):
        rules = {str(r.rule) for r in self.app.url_map.iter_rules()}
        banned = ('order', 'buy', 'sell', 'cancel', 'close', 'execute', 'trade/new')
        for rule in rules:
            for b in banned:
                if b in rule.lower() and rule != '/api/trades':
                    self.fail(f'مسار تداول محتمل: {rule}')

    def test_only_get_except_auth(self):
        AUTH = {'/api/auth/login', '/api/auth/logout'}
        for rule in self.app.url_map.iter_rules():
            methods = rule.methods - {'HEAD', 'OPTIONS'}
            if str(rule) in AUTH:
                self.assertTrue(methods <= {'POST'})
                continue
            self.assertTrue(methods <= {'GET'}, f'{rule} يسمح بـ {methods}')

    def test_auth_routes_touch_no_engine_data(self):
        """مسارات الجلسة لا تلمس قاعدة المحرك."""
        w = Database(self.path)
        before = w.query('SELECT COUNT(*) c FROM signals')[0]['c']
        self.c.post('/api/auth/login', json={'username': 'x', 'password': 'y'})
        self.c.post('/api/auth/logout')
        self.assertEqual(w.query('SELECT COUNT(*) c FROM signals')[0]['c'], before)

    def test_readonly_header_present(self):
        r, _ = self.get('/api/meta')
        self.assertEqual(r.headers.get('X-Dashboard-Mode'), 'READ-ONLY')

    def test_meta_declares_cannot_trade(self):
        _, b = self.get('/api/meta')
        d = b['data']
        self.assertFalse(d['can_trade'])
        self.assertFalse(d['can_modify_engine'])
        self.assertTrue(d['read_only'])
        self.assertTrue(b['ok'])
        for f in ('environment', 'engine_version', 'schema_version'):
            self.assertIn(f, d)

    def test_envelope_shape(self):
        for url in ('/api/dashboard', '/api/trades', '/api/health'):
            _, b = self.get(url)
            self.assertIn('ok', b); self.assertIn('data', b); self.assertIn('meta', b)
            for f in ('environment', 'generated_at', 'data_as_of', 'stale',
                      'read_only', 'can_trade', 'can_modify_engine',
                      'schema_version', 'warnings'):
                self.assertIn(f, b['meta'], f'{f} مفقود في {url}')


# ══ 2. أمان قاعدة البيانات ══
class Test02_DatabaseSafety(Base):
    def test_ro_layer_rejects_writes(self):
        ro = ReadOnlyDB(self.path)
        for sql in ('INSERT INTO signals(ts) VALUES(1)',
                    'UPDATE signals SET score=9',
                    'DELETE FROM signals',
                    'DROP TABLE signals',
                    'CREATE TABLE x(a)',
                    'PRAGMA journal_mode=DELETE'):
            with self.subTest(sql=sql[:24]):
                with self.assertRaises(ReadOnlyViolation):
                    ro.query(sql)

    def test_sqlite_engine_blocks_even_if_guard_bypassed(self):
        import sqlite3
        conn = sqlite3.connect(f'file:{self.path}?mode=ro', uri=True)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO system_events(ts,kind) VALUES(1,'x')")
        finally:
            conn.close()

    def test_engine_db_unchanged_after_dashboard_reads(self):
        w = Database(self.path)
        before = w.query('SELECT COUNT(*) c FROM signals')[0]['c']
        for url in ('/api/dashboard', '/api/recommendations', '/api/trades',
                    '/api/performance', '/api/accuracy', '/api/audit',
                    '/api/health', '/api/positions', '/api/settings'):
            self.c.get(url)
        after = w.query('SELECT COUNT(*) c FROM signals')[0]['c']
        self.assertEqual(before, after, 'طبقة العرض غيّرت القاعدة')

    def test_missing_database_reported_not_hidden(self):
        app = create_app('/tmp/definitely-missing.db', 'paper')
        app.testing = True
        r = app.test_client().get('/api/dashboard')
        body = json.loads(r.data)
        self.assertTrue(
            r.status_code == 503
            or body['data']['kpis']['system']['system_status'] == 'OFFLINE')


# ══ 3. لا أسرار ══
class Test03_NoSecrets(Base):
    PATTERNS = ('BINANCE_API_KEY', 'BINANCE_SECRET', 'API_SECRET',
                'PRIVATE_KEY', 'PASSWORD', 'MAINNET_API_SECRET')

    def test_no_secrets_in_api_responses(self):
        for url in ('/api/settings', '/api/meta', '/api/health',
                    '/api/dashboard'):
            body = self.c.get(url).data.decode()
            for p in self.PATTERNS:
                self.assertNotIn(p, body, f'{p} في {url}')

    def test_no_secrets_in_frontend_source(self):
        fe = os.path.join(ROOT, 'dashboard', 'frontend')
        for dp, _, fns in os.walk(fe):
            for fn in fns:
                with open(os.path.join(dp, fn), encoding='utf-8') as f:
                    src = f.read()
                for p in self.PATTERNS:
                    self.assertNotIn(p, src, f'{p} في {fn}')

    def test_key_exposed_only_as_fingerprint(self):
        _, b = self.get('/api/settings')
        fp = b['data']['api_key_fingerprint']
        self.assertTrue(fp in (None, 'none') or len(str(fp)) <= 16)

    def test_no_stack_trace_leaked(self):
        r = self.c.get('/api/recommendations/999999')
        self.assertEqual(r.status_code, 404)
        self.assertNotIn('Traceback', r.data.decode())

    def test_security_headers(self):
        r = self.c.get('/api/meta')
        self.assertEqual(r.headers.get('X-Content-Type-Options'), 'nosniff')
        self.assertEqual(r.headers.get('X-Frame-Options'), 'DENY')
        self.assertEqual(r.headers.get('Referrer-Policy'), 'no-referrer')
        self.assertIn('geolocation=()', r.headers.get('Permissions-Policy', ''))
        self.assertEqual(r.headers.get('Pragma'), 'no-cache')
        self.assertIn('no-store', r.headers.get('Cache-Control', ''))

    def test_csp_has_no_unsafe_directives(self):
        csp = self.c.get('/api/meta').headers.get('Content-Security-Policy', '')
        self.assertNotIn('unsafe-inline', csp)
        self.assertNotIn('unsafe-eval', csp)
        for d in ("default-src 'none'", "frame-ancestors 'none'",
                  "object-src 'none'", "base-uri 'none'"):
            self.assertIn(d, csp)

    def test_csp_nonce_is_per_request(self):
        import re
        g = lambda: re.search(r"nonce-([\w-]+)",
                              self.c.get('/api/meta').headers
                              .get('Content-Security-Policy', ''))
        a, b = g(), g()
        self.assertIsNotNone(a)
        self.assertNotEqual(a.group(1), b.group(1), 'nonce ثابت')


# ══ 4. الـ endpoints ══
class Test04_Endpoints(Base):
    def test_dashboard(self):
        r, b = self.get('/api/dashboard')
        self.assertEqual(r.status_code, 200)
        k = b['data']['kpis']
        self.assertTrue(k['available'])
        for f in ('open_positions', 'total_pnl', 'win_rate', 'profit_factor',
                  'max_drawdown_pct', 'data_quality', 'today_pnl'):
            self.assertIn(f, k)

    def test_market(self):
        _, b = self.get('/api/market')
        self.assertTrue(len(b['data']) >= 1)
        for m in b['data']:
            for f in ('symbol', 'market_regime', 'trend', 'data_quality',
                      'last_bar_ts', 'stale'):
                self.assertIn(f, m)

    def test_recommendations_pagination(self):
        _, b = self.get('/api/recommendations?limit=5&offset=0')
        self.assertEqual(len(b['data']['items']), 5)
        self.assertEqual(b['data']['limit'], 5)
        _, b2 = self.get('/api/recommendations?limit=5&offset=5')
        self.assertNotEqual(b['data']['items'][0]['signal_id'],
                            b2['data']['items'][0]['signal_id'])

    def test_limit_is_capped(self):
        _, b = self.get('/api/recommendations?limit=999999')
        self.assertLessEqual(b['data']['limit'], 200)

    def test_recommendations_filters(self):
        _, b = self.get('/api/recommendations?symbol=BTCUSDT')
        self.assertTrue(all(i['symbol'] == 'BTCUSDT' for i in b['data']['items']))
        _, b = self.get('/api/recommendations?regime=RANGING')
        self.assertTrue(all(i['market_regime'] == 'RANGING'
                            for i in b['data']['items']))

    def test_recommendation_detail(self):
        _, lst = self.get('/api/recommendations?limit=1')
        sid = lst['data']['items'][0]['signal_id']
        r, b = self.get(f'/api/recommendations/{sid}')
        self.assertEqual(r.status_code, 200)
        for f in ('entry', 'stop_loss', 'take_profit', 'risk_reward',
                  'calibrated_probability', 'engine_version', 'reasons',
                  'no_trade_reasons', 'market_regime', 'data_quality'):
            self.assertIn(f, b['data'])

    def test_positions(self):
        _, b = self.get('/api/positions')
        self.assertTrue(len(b['data']) >= 1)
        p = b['data'][0]
        for f in ('symbol', 'entry_price', 'current_price', 'unrealized_pnl',
                  'r_multiple', 'duration_ms', 'stop_loss', 'take_profit'):
            self.assertIn(f, p)

    def test_position_detail_has_timeline(self):
        _, lst = self.get('/api/positions')
        pid = lst['data'][0]['id']
        _, b = self.get(f'/api/positions/{pid}')
        for f in ('timeline', 'orders', 'fills', 'intents'):
            self.assertIn(f, b['data'])

    def test_trades(self):
        _, b = self.get('/api/trades?limit=10')
        self.assertGreater(b['data']['total'], 0)
        t = b['data']['items'][0]
        for f in ('trade_id', 'entry', 'exit', 'net_pnl', 'fees',
                  'exit_reason', 'mae_pct', 'mfe_pct', 'probability'):
            self.assertIn(f, t)

    def test_trades_result_filter(self):
        _, b = self.get('/api/trades?result=WIN&limit=100')
        self.assertTrue(all(i['net_pnl'] > 0 for i in b['data']['items']))

    def test_performance(self):
        _, b = self.get('/api/performance')
        d = b['data']
        self.assertTrue(d['available'])
        for f in ('win_rate', 'profit_factor', 'expectancy', 'net_pnl',
                  'max_drawdown_pct', 'equity_curve', 'drawdown_curve',
                  'daily', 'monthly', 'distribution', 'payoff_ratio'):
            self.assertIn(f, d)

    def test_accuracy_with_calibration(self):
        _, b = self.get('/api/accuracy')
        d = b['data']
        for f in ('total_recommendations', 'resolved', 'correct', 'accuracy',
                  'by_symbol', 'by_timeframe', 'by_regime', 'calibration',
                  'no_trade_count'):
            self.assertIn(f, d)
        self.assertIn(d['calibration']['status'],
                      ('OK', 'CALIBRATION_INSUFFICIENT'))

    def test_health_components(self):
        _, b = self.get('/api/health')
        names = {c['component'] for c in b['data']['components']}
        for n in ('Trading Engine', 'Database', 'Signal Engine',
                  'Reconciliation', 'Risk Engine', 'Dashboard API'):
            self.assertIn(n, names)
        for c in b['data']['components']:
            self.assertIn(c['status'], ('ONLINE', 'OFFLINE', 'DEGRADED', 'UNKNOWN'))

    def test_audit_union_and_filters(self):
        _, b = self.get('/api/audit?limit=20')
        self.assertGreater(b['data']['total'], 0)
        _, b2 = self.get('/api/audit?severity=CRITICAL')
        self.assertTrue(all(i['severity'] == 'CRITICAL'
                            for i in b2['data']['items']))

    def test_settings_read_only_flag(self):
        _, b = self.get('/api/settings')
        self.assertTrue(b['data']['read_only'])
        self.assertFalse(b['data']['editable'])
        self.assertFalse(b['data']['mainnet_enabled'])

    def test_invalid_params_do_not_crash(self):
        for url in ('/api/recommendations?limit=abc',
                    '/api/recommendations?min_score=xyz',
                    '/api/trades?date_from=notanumber',
                    "/api/recommendations?symbol=' OR 1=1--"):
            with self.subTest(url=url):
                self.assertEqual(self.c.get(url).status_code, 200)

    def test_sql_injection_returns_nothing(self):
        _, b = self.get("/api/recommendations?symbol=' OR '1'='1")
        self.assertEqual(len(b['data']['items']), 0)


# ══ 5. سلامة البيانات ══
class Test05_DataIntegrity(Base):
    def test_values_match_engine_exactly(self):
        w = Database(self.path)
        src = w.query('SELECT * FROM signals ORDER BY id LIMIT 1')[0]
        _, b = self.get(f"/api/recommendations/{src['id']}")
        d = b['data']
        for api_f, db_f in (('entry', 'entry'), ('stop_loss', 'stop_loss'),
                            ('take_profit', 'take_profit'),
                            ('score', 'score'), ('risk_reward', 'risk_reward'),
                            ('calibrated_probability', 'calibrated_probability')):
            self.assertEqual(d[api_f], src[db_f], f'{api_f} تغيّر')

    def test_trade_pnl_not_recalculated(self):
        w = Database(self.path)
        src = w.query('SELECT * FROM recommendations WHERE outcome IS NOT NULL '
                      'ORDER BY closed_ts DESC LIMIT 1')[0]
        _, b = self.get('/api/trades?limit=1')
        self.assertEqual(b['data']['items'][0]['net_pnl'], src['pnl'])

    def test_derived_aggregates_flagged(self):
        _, b = self.get('/api/performance')
        self.assertTrue(b['data'].get('derived'))

    def test_no_data_returns_none_not_zero(self):
        empty_path = os.path.join(tempfile.mkdtemp(), 'empty.db')
        Database(empty_path)
        app = create_app(empty_path, 'paper'); app.testing = True
        b = json.loads(app.test_client().get('/api/dashboard').data)
        k = b['data']['kpis']
        self.assertIsNone(k['total_pnl'], 'عرض 0 بدل غياب البيانات')
        self.assertIsNone(k['win_rate'])
        self.assertIsNone(k['profit_factor'])

    def test_small_sample_flagged(self):
        p = os.path.join(tempfile.mkdtemp(), 'small.db')
        db = Database(p)
        s = dict(SIG, timestamp=1700000000000)
        sid = db.save_signal(s); rid = db.save_recommendation(sid, s, True)
        db.close_recommendation(rid, outcome='WIN', exit_price=1,
                                exit_reason='TP', pnl=1.0, pnl_pct=1.0)
        app = create_app(p, 'paper'); app.testing = True
        b = json.loads(app.test_client().get('/api/performance').data)
        self.assertFalse(b['data']['sample_sufficient'])
        self.assertEqual(b['data']['note'], 'INSUFFICIENT_SAMPLE')


# ══ 6. عزل الأعطال ══
class Test06_FailureIsolation(Base):
    def test_dashboard_crash_does_not_touch_engine_db(self):
        w = Database(self.path)
        before = w.query('SELECT COUNT(*) c FROM signals')[0]['c']
        self.c.get('/api/recommendations/abc')     # 404
        self.c.post('/api/orders')                 # 405
        self.c.get('/api/nonexistent')             # 404
        after = w.query('SELECT COUNT(*) c FROM signals')[0]['c']
        self.assertEqual(before, after)

    def test_engine_can_still_write_while_dashboard_reads(self):
        """لا يمسك Dashboard قفلاً يمنع المحرك من الكتابة."""
        self.c.get('/api/dashboard')
        w = Database(self.path)
        w.system_event('ENGINE_STILL_ALIVE', 'أثناء قراءة اللوحة')
        rows = w.query("SELECT 1 FROM system_events WHERE kind='ENGINE_STILL_ALIVE'")
        self.assertEqual(len(rows), 1)

    def test_dashboard_does_not_import_trading_engine_execution(self):
        import queries as Q
        import readonly_db as R
        import app as A
        for mod in (Q, R, A):
            with open(mod.__file__, encoding='utf-8') as f:
                src = f.read()
            for banned in ('order_manager', 'BinanceClient', 'IdempotentOrderGate',
                           'market_buy', 'market_sell', 'PaperBroker'):
                self.assertNotIn(banned, src,
                                 f'{banned} في {os.path.basename(mod.__file__)}')

    def test_no_write_capable_db_class_imported(self):
        import queries as Q
        with open(Q.__file__, encoding='utf-8') as f:
            src = f.read()
        self.assertNotIn('from src.storage.database', src)


if __name__ == '__main__':
    unittest.main(verbosity=2)
