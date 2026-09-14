"""اختبارات V3 — الإعدادات، المفتاح السري، الجلسات، التخمين."""
import json, os, sys, threading, time, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'dashboard', 'backend'))

import security as SEC
from config import (DashboardConfig, ConfigError, secret_problems,
                    generate_secret, LOCAL, PRIVATE, PRODUCTION, MIN_SECRET_LEN)
from app import create_app, SESSION_COOKIE
from dashboard.tests.test_dashboard_api import seed

PW = 'A-Very-Str0ng-Passphrase!'
STRONG = generate_secret()
PREFIX = 'DASHBOARD_'


def clean():
    for k in [k for k in os.environ if k.startswith(PREFIX)]:
        os.environ.pop(k, None)
    os.environ['DASHBOARD_API_HOST'] = '127.0.0.1'


def cfg(**env) -> DashboardConfig:
    clean()
    os.environ.update({k: str(v) for k, v in env.items()})
    return DashboardConfig()


# ══ 1. المفتاح السري ══
class Test01_SecretKey(unittest.TestCase):
    def tearDown(self): clean()

    def test_missing_rejected_outside_local(self):
        for tier in (PRIVATE, PRODUCTION):
            c = cfg(DASHBOARD_ENVIRONMENT_TIER=tier,
                    DASHBOARD_TRUST_PROXY_AUTH='1',
                    DASHBOARD_HTTPS_ENABLED='1')
            errs, _ = c.validate(users_count=1)
            self.assertTrue(any('SECRET_KEY' in e for e in errs), tier)

    def test_known_weak_values_rejected(self):
        for weak in ('changeme', 'secret', 'password', 'dev', 'flask'):
            self.assertTrue(secret_problems(weak), weak)

    def test_short_rejected(self):
        self.assertTrue(secret_problems('x' * (MIN_SECRET_LEN - 1)))

    def test_repeated_char_rejected(self):
        probs = secret_problems('a' * 64)
        self.assertTrue(any('مكرر' in p or 'تنوّع' in p for p in probs))

    def test_low_entropy_rejected(self):
        self.assertTrue(secret_problems('abab' * 16))

    def test_generated_accepted(self):
        for _ in range(5):
            self.assertEqual(secret_problems(generate_secret()), [])

    def test_local_only_warns(self):
        c = cfg(DASHBOARD_SECRET_KEY='changeme')
        errs, warns = c.validate(users_count=0)
        self.assertEqual(errs, [])
        self.assertTrue(warns)

    def test_secret_not_in_safe_dict(self):
        c = cfg(DASHBOARD_SECRET_KEY=STRONG)
        self.assertNotIn(STRONG, json.dumps(c.safe_dict()))
        self.assertTrue(c.safe_dict()['secret_key_set'])


# ══ 2. مستويات البيئة ══
class Test02_Tiers(unittest.TestCase):
    def tearDown(self): clean()

    def test_unknown_tier_rejected(self):
        errs, _ = cfg(DASHBOARD_ENVIRONMENT_TIER='staging').validate(1)
        self.assertTrue(any('غير معروف' in e for e in errs))

    def test_debug_rejected_outside_local(self):
        for tier in (PRIVATE, PRODUCTION):
            c = cfg(DASHBOARD_ENVIRONMENT_TIER=tier,
                    DASHBOARD_SECRET_KEY=STRONG, DASHBOARD_DEBUG='1',
                    DASHBOARD_HTTPS_ENABLED='1',
                    DASHBOARD_TRUST_PROXY_AUTH='1')
            errs, _ = c.validate(1)
            self.assertTrue(any('DEBUG' in e for e in errs), tier)

    def test_debug_allowed_in_local(self):
        errs, _ = cfg(DASHBOARD_DEBUG='1').validate(0)
        self.assertEqual(errs, [])

    def test_production_requires_https(self):
        c = cfg(DASHBOARD_ENVIRONMENT_TIER=PRODUCTION,
                DASHBOARD_SECRET_KEY=STRONG, DASHBOARD_TRUST_PROXY_AUTH='1')
        errs, _ = c.validate(1)
        self.assertTrue(any('HTTPS' in e for e in errs))

    def test_production_rejects_http_origin(self):
        c = cfg(DASHBOARD_ENVIRONMENT_TIER=PRODUCTION,
                DASHBOARD_SECRET_KEY=STRONG, DASHBOARD_HTTPS_ENABLED='1',
                DASHBOARD_TRUST_PROXY_AUTH='1',
                DASHBOARD_CORS_ORIGIN='http://plain.internal')
        errs, _ = c.validate(1)
        self.assertTrue(any('غير مشفَّر' in e for e in errs))

    def test_auth_default_by_tier(self):
        self.assertFalse(cfg().effective_auth_required)
        self.assertTrue(cfg(DASHBOARD_ENVIRONMENT_TIER=PRIVATE)
                        .effective_auth_required)

    def test_secure_cookies_default_by_tier(self):
        self.assertFalse(cfg().effective_secure_cookies)
        self.assertTrue(cfg(DASHBOARD_ENVIRONMENT_TIER=PRODUCTION,
                            DASHBOARD_HTTPS_ENABLED='1')
                        .effective_secure_cookies)


# ══ 3. الربط ══
class Test03_Binding(unittest.TestCase):
    def tearDown(self): clean()

    def test_public_bind_local_tier_rejected(self):
        errs, _ = cfg(DASHBOARD_API_HOST='0.0.0.0',
                      DASHBOARD_TRUST_PROXY_AUTH='1').validate(1)
        self.assertTrue(any('local' in e for e in errs))

    def test_public_bind_without_auth_rejected(self):
        errs, _ = cfg(DASHBOARD_API_HOST='0.0.0.0',
                      DASHBOARD_AUTH_REQUIRED='false').validate(0)
        self.assertTrue(any('بلا مصادقة' in e for e in errs))

    def test_auth_required_without_users_rejected(self):
        errs, _ = cfg(DASHBOARD_AUTH_REQUIRED='true').validate(0)
        self.assertTrue(any('لا مستخدمين' in e for e in errs))

    def test_loopback_local_ok(self):
        errs, _ = cfg().validate(0)
        self.assertEqual(errs, [])

    def test_app_refuses_unsafe_startup(self):
        clean()
        os.environ['DASHBOARD_API_HOST'] = '0.0.0.0'
        with self.assertRaises(RuntimeError):
            create_app(seed(5), 'paper')


# ══ 4. الجلسات ══
class Test04_Sessions(unittest.TestCase):
    def test_idle_timeout(self):
        st = SEC.SessionStore(ttl=1000, idle_s=1)
        t = st.create('u')
        self.assertIsNotNone(st.get(t))
        time.sleep(1.05)
        self.assertIsNone(st.get(t))
        self.assertEqual(st.last_expiry_reason, 'IDLE_TIMEOUT')

    def test_max_age_independent_of_activity(self):
        st = SEC.SessionStore(ttl=1, idle_s=1000)
        t = st.create('u')
        for _ in range(5):
            time.sleep(0.25); st.get(t)      # نشاط مستمر
        self.assertIsNone(st.get(t))
        self.assertEqual(st.last_expiry_reason, 'MAX_AGE')

    def test_activity_extends_idle(self):
        st = SEC.SessionStore(ttl=1000, idle_s=1)
        t = st.create('u')
        for _ in range(4):
            time.sleep(0.4)
            self.assertIsNotNone(st.get(t), 'انتهت رغم النشاط')

    def test_rotation_invalidates_old(self):
        st = SEC.SessionStore(ttl=1000)
        old = st.create('u')
        new = st.rotate(old, 'u')
        self.assertNotEqual(old, new)
        self.assertIsNone(st.get(old))
        self.assertIsNotNone(st.get(new))

    def test_destroy_user_kills_all(self):
        st = SEC.SessionStore(ttl=1000)
        a, b = st.create('u'), st.create('u')
        c = st.create('other')
        self.assertEqual(st.destroy_user('u'), 2)
        self.assertIsNone(st.get(a)); self.assertIsNone(st.get(b))
        self.assertIsNotNone(st.get(c))

    def test_purge_removes_dead(self):
        st = SEC.SessionStore(ttl=1, idle_s=1)
        st.create('u'); time.sleep(1.05)
        self.assertEqual(st.purge(), 1)
        self.assertEqual(st.count, 0)

    def test_token_is_high_entropy(self):
        st = SEC.SessionStore()
        toks = {st.create('u') for _ in range(50)}
        self.assertEqual(len(toks), 50)
        self.assertTrue(all(len(t) >= 32 for t in toks))


# ══ 5. حماية التخمين ══
class Test05_BruteForce(unittest.TestCase):
    def test_per_ip_lockout(self):
        g = SEC.LoginGuard(max_attempts=3, user_max_attempts=99, lockout_s=60)
        for _ in range(3):
            g.record_attempt_failure('1.1.1.1', 'ops')
        self.assertFalse(g.check('1.1.1.1', 'ops')[0])
        self.assertTrue(g.check('2.2.2.2', 'other')[0])

    def test_per_user_lockout_across_ips(self):
        """التوزيع على عناوين متعددة لا يتجاوز حد المستخدم."""
        g = SEC.LoginGuard(max_attempts=99, user_max_attempts=3, lockout_s=60)
        for i in range(3):
            g.record_attempt_failure(f'{i}.{i}.{i}.{i}', 'ops')
        allowed, _, why = g.check('9.9.9.9', 'ops')
        self.assertFalse(allowed)
        self.assertEqual(why, 'USER')

    def test_progressive_delay(self):
        g = SEC.LoginGuard(max_attempts=10)
        delays = []
        for _ in range(4):
            delays.append(g.check('1.1.1.1', 'ops')[1])
            g.record_attempt_failure('1.1.1.1', 'ops')
        self.assertEqual(delays[0], 0.0)
        for a, b in zip(delays, delays[1:]):
            self.assertGreaterEqual(b, a)
        self.assertGreater(delays[-1], 0)

    def test_delay_is_capped(self):
        g = SEC.LoginGuard(max_attempts=100, max_delay_s=2.0)
        for _ in range(20):
            g.record_attempt_failure('1.1.1.1', 'ops')
        self.assertLessEqual(g.check('1.1.1.1', 'ops')[1], 2.0)

    def test_success_clears_counters(self):
        g = SEC.LoginGuard(max_attempts=3)
        g.record_attempt_failure('1.1.1.1', 'ops')
        g.record_attempt_failure('1.1.1.1', 'ops')
        g.record_attempt_success('1.1.1.1', 'ops')
        self.assertEqual(g.check('1.1.1.1', 'ops')[1], 0.0)

    def test_no_password_in_guard_state(self):
        g = SEC.LoginGuard()
        g.record_attempt_failure('1.1.1.1', 'ops')
        self.assertNotIn(PW, json.dumps(
            {k: list(v) for k, v in g._fails.items()}, default=str))


# ══ 6. تكامل حيّ ══
class Test06_Integration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        clean()
        os.environ['DASHBOARD_USERS'] = f'ops:{SEC.hash_password(PW)}'
        os.environ['DASHBOARD_AUTH_REQUIRED'] = 'true'
        os.environ['DASHBOARD_SESSION_IDLE_SECONDS'] = '1'
        os.environ['DASHBOARD_SESSION_TTL_SECONDS'] = '1000'
        cls.path = seed(35)
        cls.app = create_app(cls.path, 'paper')
        cls.app.testing = True

    @classmethod
    def tearDownClass(cls): clean()

    def setUp(self):
        self.c = self.app.test_client()
        self.app.config['LOGIN_GUARD'].reset()
        self.app.config['LIMITER'].reset()

    def login(self):
        return self.c.post('/api/auth/login',
                           json={'username': 'ops', 'password': PW})

    def test_login_and_access(self):
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(self.c.get('/api/dashboard').status_code, 200)

    def test_session_id_rotates(self):
        self.c.set_cookie(SESSION_COOKIE, 'attacker-planted-token')
        r = self.login()
        sc = r.headers.get('Set-Cookie', '')
        self.assertNotIn('attacker-planted-token', sc)
        self.assertEqual(self.c.get('/api/dashboard').status_code, 200)

    def test_idle_expiry_blocks(self):
        self.login()
        self.assertEqual(self.c.get('/api/dashboard').status_code, 200)
        time.sleep(1.1)
        r = self.c.get('/api/dashboard')
        self.assertEqual(r.status_code, 401)
        self.assertEqual(json.loads(r.data)['error'].get('session_expiry'),
                         'IDLE_TIMEOUT')

    def test_logout_invalidates(self):
        self.login()
        self.c.post('/api/auth/logout')
        self.assertEqual(self.c.get('/api/dashboard').status_code, 401)

    def test_no_token_in_json(self):
        body = self.login().data.decode()
        for bad in ('token', 'session_id', 'pbkdf2'):
            self.assertNotIn(bad, body.lower())

    def test_session_endpoint_is_public(self):
        r = self.c.get('/api/auth/session')
        self.assertEqual(r.status_code, 200)
        d = json.loads(r.data)['data']
        self.assertFalse(d['authenticated'])
        self.assertTrue(d['auth_required'])

    def test_meta_exposes_no_secret(self):
        body = self.c.get('/api/meta').data.decode()
        self.assertNotIn(PW, body)
        self.assertNotIn('pbkdf2', body)
        d = json.loads(body)['data']
        self.assertIn('tier', d)
        self.assertIn('rate_limits', d)

    def test_rate_limit_groups_in_headers(self):
        self.login()
        r = self.c.get('/api/performance')
        self.assertEqual(r.headers.get('X-RateLimit-Group'), 'heavy')
        r2 = self.c.get('/api/meta')
        self.assertEqual(r2.headers.get('X-RateLimit-Group'), 'light')

    def test_no_threads_left(self):
        before = threading.active_count()
        for _ in range(10):
            self.c.get('/api/meta')
        time.sleep(0.2)
        self.assertLessEqual(threading.active_count(), before + 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
