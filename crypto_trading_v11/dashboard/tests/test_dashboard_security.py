"""اختبارات أمان V2 — مصادقة، معدّل، CORS، حداثة، مصدر البيانات."""
import json, os, sys, tempfile, time, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'dashboard', 'backend'))

from src.storage.database import Database
import security as SEC
from envelope import (metric, engine, derived, unavailable, Envelope,
                      ENGINE_REPORTED, DASHBOARD_DERIVED, INSUFFICIENT_SAMPLE,
                      UNAVAILABLE, MIN_SAMPLE)
from readonly_db import ReadOnlyDB, ReadOnlyViolation
from app import create_app, SESSION_COOKIE
from dashboard.tests.test_dashboard_api import seed

PW = 'S0me-Str0ng-Passphrase!'
ENVVARS = ('DASHBOARD_USERS', 'DASHBOARD_CORS_ORIGIN', 'DASHBOARD_API_HOST',
           'DASHBOARD_TRUST_PROXY_AUTH', 'DASHBOARD_SECURE_COOKIES',
           'DASHBOARD_ENVIRONMENT_TIER', 'DASHBOARD_SECRET_KEY',
           'DASHBOARD_DEBUG', 'DASHBOARD_HTTPS_ENABLED',
           'DASHBOARD_AUTH_REQUIRED', 'DASHBOARD_RATE_LIMIT',
           'DASHBOARD_SESSION_TTL_SECONDS', 'DASHBOARD_SESSION_IDLE_SECONDS')

STRONG_SECRET = 'k' + 'Xq7Zt2Pw9Lm4Nb6Vc8Hs3Jd5Rf1Gy0Ae' * 2


def clean():
    for k in ENVVARS:
        os.environ.pop(k, None)
    os.environ['DASHBOARD_API_HOST'] = '127.0.0.1'


# ══ 1. كلمات المرور ══
class Test01_Passwords(unittest.TestCase):
    def test_never_plaintext(self):
        h = SEC.hash_password(PW)
        self.assertNotIn(PW, h)
        self.assertTrue(h.startswith('pbkdf2_sha256$'))

    def test_verify(self):
        h = SEC.hash_password(PW)
        self.assertTrue(SEC.verify_password(PW, h))
        self.assertFalse(SEC.verify_password(PW + 'x', h))

    def test_unique_salt(self):
        self.assertNotEqual(SEC.hash_password(PW), SEC.hash_password(PW))

    def test_malformed_hash_rejected(self):
        for bad in ('', 'plain', 'md5$1$a$b', 'pbkdf2_sha256$bad'):
            self.assertFalse(SEC.verify_password(PW, bad))


# ══ 2. المصادقة ══
class Test02_Auth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        clean()
        cls.path = seed(35)
        os.environ['DASHBOARD_USERS'] = f'viewer:{SEC.hash_password(PW)}'
        cls.app = create_app(cls.path, 'paper')
        cls.app.testing = True

    @classmethod
    def tearDownClass(cls):
        clean()

    def setUp(self):
        self.c = self.app.test_client()
        self.app.config['LOGIN_GUARD'].reset()
        self.app.config['LIMITER'].reset()

    def test_unauthenticated_blocked(self):
        r = self.c.get('/api/dashboard')
        self.assertEqual(r.status_code, 401)
        self.assertEqual(json.loads(r.data)['error']['code'], 'AUTH_REQUIRED')

    def test_meta_is_public(self):
        self.assertEqual(self.c.get('/api/meta').status_code, 200)

    def test_login_then_access(self):
        r = self.c.post('/api/auth/login',
                        json={'username': 'viewer', 'password': PW})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.c.get('/api/dashboard').status_code, 200)

    def test_cookie_is_hardened(self):
        r = self.c.post('/api/auth/login',
                        json={'username': 'viewer', 'password': PW})
        sc = r.headers.get('Set-Cookie', '')
        self.assertIn('HttpOnly', sc)
        self.assertIn('SameSite=Strict', sc)
        self.assertIn('Max-Age=', sc)

    def test_secure_flag_when_enabled(self):
        os.environ['DASHBOARD_SECURE_COOKIES'] = '1'
        try:
            app = create_app(self.path, 'paper'); app.testing = True
            r = app.test_client().post(
                '/api/auth/login', json={'username': 'viewer', 'password': PW})
            self.assertIn('Secure', r.headers.get('Set-Cookie', ''))
        finally:
            os.environ.pop('DASHBOARD_SECURE_COOKIES', None)

    def test_wrong_password_rejected(self):
        r = self.c.post('/api/auth/login',
                        json={'username': 'viewer', 'password': 'wrong'})
        self.assertEqual(r.status_code, 401)

    def test_does_not_reveal_user_existence(self):
        a = self.c.post('/api/auth/login',
                        json={'username': 'viewer', 'password': 'wrong'})
        self.app.config['LOGIN_GUARD'].reset()
        b = self.c.post('/api/auth/login',
                        json={'username': 'ghost', 'password': 'wrong'})
        self.assertEqual(a.status_code, b.status_code)
        self.assertEqual(json.loads(a.data)['error']['code'],
                         json.loads(b.data)['error']['code'])

    def test_lockout_after_failures(self):
        for _ in range(SEC.LOGIN_MAX_ATTEMPTS):
            self.c.post('/api/auth/login',
                        json={'username': 'viewer', 'password': 'x'})
        r = self.c.post('/api/auth/login',
                        json={'username': 'viewer', 'password': PW})
        self.assertEqual(r.status_code, 429)
        self.assertEqual(json.loads(r.data)['error']['code'], 'LOGIN_LOCKED')

    def test_logout_invalidates(self):
        self.c.post('/api/auth/login',
                    json={'username': 'viewer', 'password': PW})
        self.c.post('/api/auth/logout')
        self.assertEqual(self.c.get('/api/dashboard').status_code, 401)

    def test_forged_cookie_rejected(self):
        self.c.set_cookie(SESSION_COOKIE, 'not-a-real-token')
        self.assertEqual(self.c.get('/api/dashboard').status_code, 401)

    def test_expired_session_rejected(self):
        store = SEC.SessionStore(ttl=0)
        tok = store.create('viewer')
        time.sleep(0.01)
        self.assertIsNone(store.get(tok))

    def test_no_secrets_in_login_response(self):
        r = self.c.post('/api/auth/login',
                        json={'username': 'viewer', 'password': PW})
        body = r.data.decode()
        self.assertNotIn(PW, body)
        self.assertNotIn('pbkdf2', body)


# ══ 3. سياسة الربط ══
class Test03_BindPolicy(unittest.TestCase):
    def setUp(self): clean()
    def tearDown(self): clean()

    def test_public_bind_without_auth_refuses_startup(self):
        os.environ['DASHBOARD_API_HOST'] = '0.0.0.0'
        with self.assertRaises(RuntimeError):
            create_app(seed(5), 'paper')

    def test_public_bind_with_users_allowed(self):
        # V3: الربط العام يتطلب tier غير local + مفتاح سري قوي
        os.environ['DASHBOARD_API_HOST'] = '0.0.0.0'
        os.environ['DASHBOARD_ENVIRONMENT_TIER'] = 'private'
        os.environ['DASHBOARD_SECRET_KEY'] = STRONG_SECRET
        os.environ['DASHBOARD_USERS'] = f'v:{SEC.hash_password(PW)}'
        self.assertIsNotNone(create_app(seed(5), 'paper'))

    def test_public_bind_behind_proxy_allowed(self):
        os.environ['DASHBOARD_API_HOST'] = '0.0.0.0'
        os.environ['DASHBOARD_ENVIRONMENT_TIER'] = 'private'
        os.environ['DASHBOARD_SECRET_KEY'] = STRONG_SECRET
        os.environ['DASHBOARD_TRUST_PROXY_AUTH'] = '1'
        self.assertIsNotNone(create_app(seed(5), 'paper'))

    def test_public_bind_local_tier_refused(self):
        os.environ['DASHBOARD_API_HOST'] = '0.0.0.0'
        os.environ['DASHBOARD_TRUST_PROXY_AUTH'] = '1'
        with self.assertRaises(RuntimeError):
            create_app(seed(5), 'paper')

    def test_loopback_anonymous_ok(self):
        app = create_app(seed(5), 'paper'); app.testing = True
        self.assertEqual(app.test_client().get('/api/dashboard').status_code, 200)


# ══ 4. CORS ══
class Test04_CORS(unittest.TestCase):
    def setUp(self): clean()
    def tearDown(self): clean()

    def test_wildcard_refused_outside_local(self):
        os.environ['DASHBOARD_ENVIRONMENT_TIER'] = 'private'
        os.environ['DASHBOARD_SECRET_KEY'] = STRONG_SECRET
        os.environ['DASHBOARD_TRUST_PROXY_AUTH'] = '1'
        os.environ['DASHBOARD_CORS_ORIGIN'] = '*'
        with self.assertRaises(RuntimeError):
            create_app(seed(5), 'paper')

    def test_wildcard_never_echoed_even_in_local(self):
        """في local يُقبل الإقلاع بتحذير، لكن `*` لا يُعاد في أي رد."""
        os.environ['DASHBOARD_CORS_ORIGIN'] = '*'
        app = create_app(seed(5), 'paper'); app.testing = True
        self.assertEqual(app.config['CORS_ORIGINS'], [])
        r = app.test_client().get('/api/meta',
                                  headers={'Origin': 'https://any.example'})
        self.assertIsNone(r.headers.get('Access-Control-Allow-Origin'))

    def test_invalid_origin_refused(self):
        os.environ['DASHBOARD_CORS_ORIGIN'] = 'evil.com'
        with self.assertRaises(RuntimeError):
            create_app(seed(5), 'paper')

    def test_allowed_origin_echoed(self):
        os.environ['DASHBOARD_CORS_ORIGIN'] = 'https://ops.internal'
        app = create_app(seed(5), 'paper'); app.testing = True
        r = app.test_client().get('/api/meta',
                                  headers={'Origin': 'https://ops.internal'})
        self.assertEqual(r.headers.get('Access-Control-Allow-Origin'),
                         'https://ops.internal')

    def test_unknown_origin_not_echoed(self):
        os.environ['DASHBOARD_CORS_ORIGIN'] = 'https://ops.internal'
        app = create_app(seed(5), 'paper'); app.testing = True
        r = app.test_client().get('/api/meta',
                                  headers={'Origin': 'https://evil.example'})
        self.assertIsNone(r.headers.get('Access-Control-Allow-Origin'))

    def test_disabled_by_default(self):
        app = create_app(seed(5), 'paper'); app.testing = True
        r = app.test_client().get('/api/meta',
                                  headers={'Origin': 'https://x.example'})
        self.assertIsNone(r.headers.get('Access-Control-Allow-Origin'))


# ══ 5. تحديد المعدّل ══
class Test05_RateLimit(unittest.TestCase):
    def setUp(self):
        clean()
        self.app = create_app(seed(5), 'paper')
        self.app.testing = True
        # V3: الحدود بمجموعات — نخفضها كلها للاختبار
        self._orig = dict(SEC.ENDPOINT_LIMITS)
        for k in SEC.ENDPOINT_LIMITS:
            SEC.ENDPOINT_LIMITS[k] = 5
        self.app.config['LIMITER'] = SEC.RateLimiter(5, 60)
        self.c = self.app.test_client()

    def tearDown(self):
        SEC.ENDPOINT_LIMITS.clear()
        SEC.ENDPOINT_LIMITS.update(self._orig)
        clean()

    def test_limit_enforced(self):
        codes = [self.c.get('/api/meta').status_code for _ in range(8)]
        self.assertIn(429, codes)
        self.assertEqual(codes[:5], [200] * 5)

    def test_429_has_retry_after_and_reset(self):
        for _ in range(6):
            r = self.c.get('/api/meta')
        self.assertEqual(r.status_code, 429)
        self.assertIn('Retry-After', r.headers)
        self.assertIn('X-RateLimit-Reset', r.headers)
        self.assertEqual(r.headers.get('X-RateLimit-Remaining'), '0')
        self.assertEqual(json.loads(r.data)['error']['code'], 'RATE_LIMITED')

    def test_groups_are_independent(self):
        """استنفاد مجموعة لا يُعطّل أخرى."""
        for _ in range(6):
            self.c.get('/api/meta')                 # light
        self.assertEqual(self.c.get('/api/meta').status_code, 429)
        self.assertEqual(self.c.get('/api/trades').status_code, 200)  # list

    def test_reset_header_present_on_success(self):
        r = self.c.get('/api/meta')
        for h in ('X-RateLimit-Limit', 'X-RateLimit-Remaining',
                  'X-RateLimit-Reset', 'X-RateLimit-Group'):
            self.assertIn(h, r.headers)



    def test_static_not_limited(self):
        for _ in range(20):
            self.c.get('/api/meta')
        self.assertEqual(self.c.get('/assets/styles.css').status_code, 200)


# ══ 6. وسوم مصدر البيانات ══
class Test06_Provenance(unittest.TestCase):
    def test_engine_reported(self):
        m = engine(1.42)
        self.assertEqual(m['source'], ENGINE_REPORTED)
        self.assertEqual(m['status'], ENGINE_REPORTED)

    def test_derived_flagged(self):
        self.assertEqual(derived(61.4, sample_size=120)['source'],
                         DASHBOARD_DERIVED)

    def test_small_sample_flagged(self):
        self.assertEqual(derived(66.7, sample_size=3)['status'],
                         INSUFFICIENT_SAMPLE)

    def test_missing_is_none_not_zero(self):
        u = unavailable()
        self.assertIsNone(u['value'])
        self.assertNotEqual(u['value'], 0)
        self.assertEqual(u['status'], UNAVAILABLE)

    def test_envelope_declares_no_trade(self):
        m = Envelope(data={}, environment='paper', api_version='v1',
                     dashboard_version='2.0.0').to_dict()['meta']
        self.assertTrue(m['read_only'])
        self.assertFalse(m['can_trade'])
        self.assertFalse(m['can_modify_engine'])


# ══ 7. الحداثة والمخطط ══
class Test07_Freshness(unittest.TestCase):
    def setUp(self): clean()
    def tearDown(self): clean()

    def test_stale_flag_on_old_data(self):
        app = create_app(seed(35), 'paper'); app.testing = True
        b = json.loads(app.test_client().get('/api/dashboard').data)
        # بيانات seed قديمة (2023) ⇒ stale
        self.assertTrue(b['meta']['stale'])
        self.assertIn('STALE_DATA', b['meta']['warnings'])

    def test_schema_mismatch_warned(self):
        import sqlite3
        p = seed(5)
        c = sqlite3.connect(p); c.execute('PRAGMA user_version=1')
        c.commit(); c.close()
        app = create_app(p, 'paper'); app.testing = True
        b = json.loads(app.test_client().get('/api/meta').data)
        self.assertIn('SCHEMA_MISMATCH', b['meta']['warnings'])
        self.assertEqual(b['data']['schema_status'], 'SCHEMA_MISMATCH')

    def test_missing_tables_degraded(self):
        import sqlite3
        d = tempfile.mkdtemp(); p = os.path.join(d, 'partial.db')
        c = sqlite3.connect(p)
        c.execute('CREATE TABLE signals(id INTEGER)')
        c.execute('PRAGMA user_version=2'); c.commit(); c.close()
        self.assertEqual(ReadOnlyDB(p).health()['status'], 'DEGRADED')

    def test_missing_db_creates_nothing(self):
        p = os.path.join(tempfile.mkdtemp(), 'absent.db')
        ReadOnlyDB(p).health()
        self.assertFalse(os.path.exists(p), 'أُنشئت قاعدة بديلة')

    def test_row_cap(self):
        ro = ReadOnlyDB(seed(5), max_rows=2)
        from readonly_db import DatabaseUnavailable
        with self.assertRaises(DatabaseUnavailable):
            ro.query('SELECT * FROM signals')


# ══ 8. سجل الوصول ══
class Test08_AccessLog(unittest.TestCase):
    def setUp(self):
        clean()
        self.app = create_app(seed(5), 'paper'); self.app.testing = True
        self.c = self.app.test_client()

    def tearDown(self): clean()

    def test_records_requests(self):
        self.c.get('/api/meta')
        entries = self.app.config['ACCESS_LOG'].recent(10)
        self.assertTrue(any(e['path'] == '/api/meta' for e in entries))

    def test_ip_anonymized(self):
        self.assertEqual(SEC.anonymize_ip('192.168.1.77'), '192.168.1.0')
        self.assertNotIn('77', SEC.anonymize_ip('192.168.1.77').split('.')[-1])

    def test_no_secrets_logged(self):
        self.c.get('/api/meta', headers={'Authorization': 'Bearer SECRET123',
                                         'Cookie': 'x=SECRET456'})
        dump = json.dumps(self.app.config['ACCESS_LOG'].recent(10))
        self.assertNotIn('SECRET123', dump)
        self.assertNotIn('SECRET456', dump)

    def test_endpoint_available(self):
        self.assertEqual(self.c.get('/api/access-log').status_code, 200)


# ══ 9. لا تسريب ══
class Test09_NoLeak(unittest.TestCase):
    def setUp(self):
        clean()
        self.app = create_app(seed(5), 'paper'); self.app.testing = True
        self.c = self.app.test_client()

    def tearDown(self): clean()

    def test_errors_have_no_paths_or_traces(self):
        for url in ('/api/recommendations/999999', '/api/nope',
                    '/api/positions/999999'):
            body = self.c.get(url).data.decode()
            self.assertNotIn('Traceback', body)
            self.assertNotIn('/home/', body)
            self.assertNotIn('.py', body)

    def test_write_rejection_has_no_internals(self):
        body = self.c.post('/api/orders').data.decode()
        self.assertNotIn('Traceback', body)
        self.assertNotIn('/home/', body)

    def test_no_engine_env_read(self):
        import app as A
        with open(A.__file__, encoding='utf-8') as f:
            src = f.read()
        for bad in ('TESTNET_API_SECRET', 'MAINNET_API_SECRET',
                    'BINANCE_API_KEY', 'BINANCE_API_SECRET'):
            self.assertNotIn(bad, src)


if __name__ == '__main__':
    unittest.main(verbosity=2)
