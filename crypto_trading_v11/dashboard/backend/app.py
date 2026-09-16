"""
CRYPTO TRADING INTELLIGENCE DASHBOARD — READ-ONLY API
=====================================================
THE DASHBOARD CANNOT TRADE.

  • لا endpoint لإنشاء أو تعديل أو إلغاء أمر
  • كل طريقة غير GET/HEAD/OPTIONS تُرفض بـ 405 قبل أي توجيه
  • القاعدة تُفتح بوضع mode=ro + query_only + فحص نصي
  • لا يقرأ مفاتيح تداول ولا يمرّرها للمتصفح

توقف هذا الخادم لا يؤثر على محرك التداول إطلاقاً — لا يشاركه عملية
ولا قفلاً ولا اتصال كتابة.
"""
import os
import secrets
import sys
import time
from typing import Any, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import (Flask, jsonify, request, send_from_directory, g,
                   make_response, Response)

from readonly_db import (ReadOnlyDB, ReadOnlyViolation, DatabaseUnavailable,
                         SchemaMismatch)
from queries import DashboardQueries
from envelope import Envelope, error_envelope, MIN_SAMPLE
from config import DashboardConfig, ConfigError

import security as SEC

API_VERSION = 'v1'
DASHBOARD_VERSION = "3.0.0"
SESSION_COOKIE = 'dash_session'
SAFE_METHODS = {'GET', 'HEAD', 'OPTIONS'}
MAX_LIMIT = 200

HERE = os.path.dirname(os.path.abspath(__file__))
FRONTEND = os.path.abspath(os.path.join(HERE, '..', 'frontend'))


def resolve_db_path() -> str:
    explicit = os.getenv('DATABASE_READ_ONLY_URL', '').strip()
    if explicit:
        return explicit.replace('sqlite:///', '')
    env = os.getenv('DASHBOARD_ENVIRONMENT', 'paper').strip()
    root = os.path.abspath(os.path.join(HERE, '..', '..'))
    return os.path.join(root, 'data', env, f'{env}.db')


def create_app(db_path: Optional[str] = None,
               environment: Optional[str] = None) -> Flask:
    app = Flask(__name__, static_folder=None)
    path = db_path or resolve_db_path()
    env = environment or os.getenv('DASHBOARD_ENVIRONMENT', 'paper')
    app.config['DB_PATH'] = path
    app.config['ENVIRONMENT'] = env
    ro = ReadOnlyDB(path, cache_ttl=float(os.getenv('CACHE_TTL', '3')))
    q = DashboardQueries(ro, env)
    app.config['RO'] = ro
    app.config['Q'] = q

    # ══ فحص الإقلاع — الإعداد غير الآمن يمنع التشغيل ══
    dcfg = DashboardConfig()
    if environment:
        dcfg.data_env = environment
    users = SEC.load_users()
    warns = dcfg.enforce(users_count=len(users))     # يرمي ConfigError

    policy = SEC.AccessPolicy(users=users,
                              trust_proxy_auth=dcfg.trust_proxy_auth,
                              host=dcfg.host)

    app.config['SECRET_KEY'] = dcfg.secret_key or SEC.secrets.token_urlsafe(48)
    app.config.update(
        DCFG=dcfg, POLICY=policy, CONFIG_WARNINGS=warns,
        POLICY_MSG=(f'tier={dcfg.tier} · auth='
                    f'{"required" if dcfg.effective_auth_required else "optional"}'),
        CORS_ORIGINS=[o for o in dcfg.cors_origins if o != '*'],
        AUTH_REQUIRED=dcfg.effective_auth_required,
        SESSIONS=SEC.SessionStore(ttl=dcfg.session_ttl_s,
                                  idle_s=dcfg.session_idle_s),
        LOGIN_GUARD=SEC.LoginGuard(),
        LIMITER=SEC.RateLimiter(dcfg.rate_limit_rpm),
        ACCESS_LOG=SEC.AccessLog(),
        SECURE_COOKIES=dcfg.effective_secure_cookies)
    for w in warns:
        app.logger.warning('config: %s', w)

    # ══ فرض القراءة على مستوى البروتوكول ══
    def _client_key() -> str:
        return request.remote_addr or 'unknown'

    def _current_user():
        s = app.config['SESSIONS'].get(request.cookies.get(SESSION_COOKIE))
        return s.user if s else None

    @app.before_request
    def _gate():
        g._t0 = time.monotonic()
        g._nonce = secrets.token_urlsafe(16)
        g._user = None

        # (1) القراءة فقط — قبل أي شيء آخر، حتى قبل المصادقة
        # الاستثناء الوحيد: POST /api/auth/login و /api/auth/logout، وهما
        # لا يمسّان بيانات المحرك إطلاقاً — جلسات عرض فقط.
        if request.method not in SAFE_METHODS:
            if request.path not in ('/api/auth/login', '/api/auth/logout'):
                return jsonify(error_envelope(
                    'READ_ONLY_DASHBOARD',
                    'لوحة العرض للقراءة فقط. لا تنفّذ أوامر تداول.',
                    env, API_VERSION, DASHBOARD_VERSION,
                    method=request.method)), 405

        # (2) تحديد المعدّل — حد لكل مجموعة مسارات
        if request.path.startswith('/api/'):
            group = SEC.limit_group(request.path)
            lim = SEC.ENDPOINT_LIMITS.get(group)
            ok_rate, remaining, reset_in, applied = app.config['LIMITER'].check(
                f'{group}:{_client_key()}', lim)
            g._rate = (applied, remaining, reset_in, group)
            if not ok_rate:
                r = jsonify(error_envelope(
                    'RATE_LIMITED', 'تجاوزت حد الطلبات',
                    env, API_VERSION, DASHBOARD_VERSION,
                    retry_after_s=reset_in, limit_group=group))
                r.status_code = 429
                r.headers['Retry-After'] = str(int(reset_in) + 1)
                r.headers['X-RateLimit-Limit'] = str(applied)
                r.headers['X-RateLimit-Remaining'] = '0'
                r.headers['X-RateLimit-Reset'] = str(int(reset_in) + 1)
                return r

        # (3) المصادقة
        policy = app.config['POLICY']
        g._user = _current_user()
        public = request.path in ('/api/auth/login', '/api/auth/session',
                                  '/api/meta') \
            or not request.path.startswith('/api/')
        if not public and g._user is None:
            anon_ok = (not app.config['AUTH_REQUIRED']
                       and policy.allows_anonymous(request.remote_addr))
            if not anon_ok:
                reason = app.config['SESSIONS'].last_expiry_reason
                return jsonify(error_envelope(
                    'AUTH_REQUIRED', 'يلزم تسجيل الدخول',
                    env, API_VERSION, DASHBOARD_VERSION,
                    session_expiry=reason)), 401

    @app.after_request
    def _headers(resp):
        nonce = getattr(g, '_nonce', '')
        resp.headers['X-Dashboard-Mode'] = 'READ-ONLY'
        resp.headers['X-Content-Type-Options'] = 'nosniff'
        resp.headers['X-Frame-Options'] = 'DENY'
        resp.headers['Referrer-Policy'] = 'no-referrer'
        resp.headers['Permissions-Policy'] = (
            'geolocation=(), microphone=(), camera=(), payment=(), usb=()')
        # لا unsafe-inline ولا unsafe-eval — الأنماط في ملف والسكربتات وحدات
        resp.headers['Content-Security-Policy'] = (
            "default-src 'none'; "
            f"script-src 'self' 'nonce-{nonce}'; "
            "style-src 'self'; img-src 'self' data:; font-src 'self'; "
            "connect-src 'self'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'; object-src 'none'")
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
        resp.headers['Pragma'] = 'no-cache'
        resp.headers['Expires'] = '0'

        # CORS: قائمة محددة فقط. `*` مرفوض عند الإقلاع.
        origins = app.config['CORS_ORIGINS']
        req_origin = request.headers.get('Origin')
        if origins and req_origin in origins:
            resp.headers['Access-Control-Allow-Origin'] = req_origin
            resp.headers['Access-Control-Allow-Methods'] = 'GET, HEAD, OPTIONS'
            resp.headers['Access-Control-Allow-Credentials'] = 'true'
            resp.headers['Vary'] = 'Origin'

        rate = getattr(g, '_rate', None)
        if rate:
            applied, remaining, reset_in, group = rate
            resp.headers['X-RateLimit-Limit'] = str(applied)
            resp.headers['X-RateLimit-Remaining'] = str(remaining)
            resp.headers['X-RateLimit-Reset'] = str(int(reset_in) + 1)
            resp.headers['X-RateLimit-Group'] = group

        t0 = getattr(g, '_t0', None)
        dt = (time.monotonic() - t0) * 1000 if t0 else 0.0
        if t0:
            resp.headers['X-Response-Time-Ms'] = f'{dt:.1f}'

        if request.path.startswith('/api/'):
            app.config['ACCESS_LOG'].record(
                ts=time.time(), user=getattr(g, '_user', None) or 'anonymous',
                method=request.method, path=request.path,
                status=resp.status_code, duration_ms=dt,
                ip_anon=SEC.anonymize_ip(request.remote_addr),
                environment=env)
        return resp

    # ══ معالجة الأخطاء — بلا تسريب داخلي ══
    def err(code, msg, status, **extra):
        return jsonify(error_envelope(code, msg, env, API_VERSION,
                                      DASHBOARD_VERSION, **extra)), status

    @app.errorhandler(ReadOnlyViolation)
    def _ro(e):
        return err('READ_ONLY_VIOLATION', 'محاولة كتابة مرفوضة', 403)

    @app.errorhandler(DatabaseUnavailable)
    def _dbu(e):
        return err('DATABASE_UNAVAILABLE', 'قاعدة بيانات المحرك غير متاحة', 503)

    @app.errorhandler(SchemaMismatch)
    def _sm(e):
        return err('SCHEMA_MISMATCH', 'نسخة مخطط غير متوافقة', 503)

    @app.errorhandler(404)
    def _404(e):
        return err('NOT_FOUND', 'المورد غير موجود', 404)

    @app.errorhandler(405)
    def _405(e):
        return err('METHOD_NOT_ALLOWED', 'READ-ONLY: GET فقط', 405)

    @app.errorhandler(429)
    def _429(e):
        return err('RATE_LIMITED', 'تجاوزت حد الطلبات', 429)

    @app.errorhandler(Exception)
    def _500(e):
        # لا stack trace ولا مسار ملف ولا رسالة الاستثناء للعميل
        app.logger.exception('dashboard error')
        return err('INTERNAL_ERROR', 'خطأ داخلي في طبقة العرض', 500)

    # ══ أدوات ══
    def _int(name, default=None):
        v = request.args.get(name)
        if v is None or v == '':
            return default
        try:
            return int(v)
        except ValueError:
            return default

    def _float(name, default=None):
        v = request.args.get(name)
        if v is None or v == '':
            return default
        try:
            return float(v)
        except ValueError:
            return default

    def _page():
        limit = min(max(_int('limit', 50) or 50, 1), MAX_LIMIT)
        offset = max(_int('offset', 0) or 0, 0)     # سالب يُصحَّح إلى 0
        return limit, offset

    def _s(name):
        v = (request.args.get(name) or '').strip()
        return v[:64] if v else None

    def ok(payload: Any, *, as_of=None, stale=None, sample=None,
           warnings=None) -> Any:
        sch = ro.schema()
        warn = list(warnings or [])
        if sch['status'] == 'SCHEMA_MISMATCH':
            warn.append('SCHEMA_MISMATCH')
        elif sch['status'] == 'DEGRADED':
            warn.append('SCHEMA_DEGRADED')

        if as_of is None:
            as_of = _latest_data_ts()
        if stale is None:
            stale = _is_stale(as_of)
        if stale:
            warn.append('STALE_DATA')
        if sample is not None and sample < MIN_SAMPLE:
            warn.append('INSUFFICIENT_SAMPLE')

        return jsonify(Envelope(
            data=payload, environment=env, api_version=API_VERSION,
            dashboard_version=DASHBOARD_VERSION,
            engine_version=_engine_version(), schema_version=sch.get('version'),
            data_as_of_ms=as_of, stale=bool(stale), sample_size=sample,
            warnings=sorted(set(warn))).to_dict())

    def _engine_version():
        try:
            r = ro.one('SELECT strategy_version v FROM signals '
                       'ORDER BY id DESC LIMIT 1')
            return r['v'] if r else None
        except Exception:
            return None

    def _latest_data_ts():
        try:
            r = ro.one('SELECT MAX(bar_time) v FROM signals')
            return r['v'] if r and r.get('v') else None
        except Exception:
            return None

    def _is_stale(as_of):
        if not as_of:
            return True
        return (time.time() * 1000 - as_of) > 3 * 3600_000

    # ══ المصادقة ══
    @app.post('/api/auth/login')
    def api_login():
        policy = app.config['POLICY']
        guard = app.config['LOGIN_GUARD']
        key = _client_key()

        body = request.get_json(silent=True) or {}
        user = str(body.get('username', ''))[:64]
        pw = str(body.get('password', ''))[:256]

        allowed, wait, why = guard.check(key, user)
        if not allowed:
            app.config['ACCESS_LOG'].record(
                ts=time.time(), user='(locked)', method='POST',
                path='/api/auth/login', status=429, duration_ms=0.0,
                ip_anon=SEC.anonymize_ip(request.remote_addr), environment=env)
            return err('LOGIN_LOCKED', 'محاولات كثيرة — حاول لاحقاً', 429,
                       retry_after_s=wait)
        if wait:
            time.sleep(min(wait, 4.0))      # تأخير تدريجي
        if not policy.users:
            return err('AUTH_DISABLED', 'المصادقة غير مفعّلة', 400)

        stored = policy.users.get(user)
        # التحقق يجري حتى مع مستخدم غير موجود — لا فرق في الزمن ولا في الرد
        valid = SEC.verify_password(pw, stored) if stored else \
            SEC.verify_password(pw, SEC.hash_password('dummy'))
        if not stored or not valid:
            guard.record_attempt_failure(key, user)
            return err('INVALID_CREDENTIALS', 'بيانات دخول غير صحيحة', 401)

        guard.record_attempt_success(key, user)
        # دوران المعرّف: يبطل أي رمز زُرع قبل الدخول (session fixation)
        token = app.config['SESSIONS'].rotate(
            request.cookies.get(SESSION_COOKIE), user)
        resp = make_response(jsonify(Envelope(
            data={'user': user, 'expires_in_s': SEC.SESSION_TTL_S},
            environment=env, api_version=API_VERSION,
            dashboard_version=DASHBOARD_VERSION).to_dict()))
        resp.set_cookie(SESSION_COOKIE, token, httponly=True,
                        secure=app.config['SECURE_COOKIES'],
                        samesite='Strict', max_age=SEC.SESSION_TTL_S,
                        path='/')
        return resp

    @app.post('/api/auth/logout')
    def api_logout():
        app.config['SESSIONS'].destroy(request.cookies.get(SESSION_COOKIE))
        resp = make_response(jsonify(Envelope(
            data={'logged_out': True}, environment=env,
            api_version=API_VERSION,
            dashboard_version=DASHBOARD_VERSION).to_dict()))
        resp.delete_cookie(SESSION_COOKIE, path='/')
        return resp

    @app.get('/api/auth/session')
    def api_session():
        policy = app.config['POLICY']
        d = app.config['DCFG']
        return ok({'authenticated': g._user is not None,
                   'user': g._user,
                   'auth_required': app.config['AUTH_REQUIRED'],
                   'anonymous_allowed': (not app.config['AUTH_REQUIRED']
                                         and policy.allows_anonymous(
                                             request.remote_addr)),
                   'session_ttl_s': d.session_ttl_s,
                   'session_idle_s': d.session_idle_s,
                   'tier': d.tier})

    @app.get('/api/access-log')
    def api_access_log():
        if app.config['POLICY'].auth_required and g._user is None:
            return err('AUTH_REQUIRED', 'يلزم تسجيل الدخول', 401)
        limit = min(max(_int('limit', 100) or 100, 1), MAX_LIMIT)
        return ok(app.config['ACCESS_LOG'].recent(limit))

    # ══ Endpoints ══
    @app.get('/api/health')
    def api_health():
        return ok(q.health())

    @app.get('/api/dashboard')
    def api_dashboard():
        return ok({'kpis': q.kpis(), 'market': q.market(),
                   'recommendations': q.recommendations(limit=8)['items'],
                   'positions': q.positions('OPEN')})

    @app.get('/api/market')
    def api_market():
        return ok(q.market())

    @app.get('/api/recommendations')
    def api_recs():
        limit, offset = _page()
        return ok(q.recommendations(
            limit=limit, offset=offset, symbol=_s('symbol'),
            interval=_s('interval'), decision=_s('signal'),
            status=_s('status'), regime=_s('regime'),
            min_score=_float('min_score'),
            min_probability=_float('min_probability'),
            date_from=_int('date_from'), date_to=_int('date_to')))

    @app.get('/api/recommendations/<int:sid>')
    def api_rec(sid):
        r = q.recommendation(sid)
        if r is None:
            return jsonify({'error': 'NOT_FOUND'}), 404
        return ok(r)

    @app.get('/api/positions')
    def api_positions():
        return ok(q.positions(_s('status') or 'OPEN'))

    @app.get('/api/positions/<int:pid>')
    def api_position(pid):
        p = q.position(pid)
        if p is None:
            return jsonify({'error': 'NOT_FOUND'}), 404
        return ok(p)

    @app.get('/api/trades')
    def api_trades():
        limit, offset = _page()
        return ok(q.trades(
            limit=limit, offset=offset, symbol=_s('symbol'),
            result=_s('result'), exit_reason=_s('exit_reason'),
            regime=_s('regime'), date_from=_int('date_from'),
            date_to=_int('date_to')))

    @app.get('/api/trades/<int:tid>')
    def api_trade(tid):
        res = q.trades(limit=1, offset=0)
        for t in q.trades(limit=MAX_LIMIT)['items']:
            if t['trade_id'] == tid:
                return ok(t)
        return jsonify({'error': 'NOT_FOUND'}), 404

    @app.get('/api/performance')
    def api_perf():
        return ok(q.performance(
            symbol=_s('symbol'), interval=_s('interval'),
            regime=_s('regime'), date_from=_int('date_from'),
            date_to=_int('date_to')))

    @app.get('/api/accuracy')
    def api_accuracy():
        return ok(q.accuracy())

    @app.get('/api/audit')
    def api_audit():
        limit, offset = _page()
        return ok(q.audit(
            limit=limit, offset=offset, severity=_s('severity'),
            component=_s('component'), event_type=_s('event_type'),
            symbol=_s('symbol'), search=_s('search'),
            date_from=_int('date_from'), date_to=_int('date_to')))

    @app.get('/api/equity')
    def api_equity():
        return ok(q.equity())

    @app.get('/api/data-quality')
    def api_dq():
        return ok(q.data_quality(min(_int('limit', 100) or 100, MAX_LIMIT)))

    @app.get('/api/opportunity-scans')
    def api_opportunity_scans():
        """القسمان 86/93: سجل مسح أفضل فرصة — جدول المقارنة + سجل التدقيق."""
        return ok(q.opportunity_scans(min(_int('limit', 20) or 20, MAX_LIMIT)))

    @app.get('/api/opportunity-scans/last')
    def api_last_opportunity_scan():
        """القسمان 92/96: مخطَّط قريب من API القسم 92 — آخر قرار فقط."""
        last = q.last_opportunity_scan()
        if last is None:
            return ok({'selected_symbol': None, 'decision': 'NO_TRADE',
                      'reason': 'NO_SCAN_YET', 'opportunities': []})
        return ok(last)

    @app.get('/api/account')
    def api_account():
        """
        القسم 14: حالة الحساب. **لا أسرار إطلاقاً** — لا مفتاح ولا توقيع
        ولا بصمة؛ الحقول محدَّدة صراحةً في `queries.account_status()`
        ولا يُمرَّر قاموس خام من أي مصدر.
        """
        return ok(q.account_status())

    @app.get('/api/sizing-plan')
    def api_sizing_plan():
        """القسمان 13/25: «كم أدخل؟ ولماذا هذا المبلغ؟»."""
        return ok(q.sizing_plan(request.args.get('symbol') or None))

    @app.get('/api/sizing-plans')
    def api_sizing_plans():
        return ok(q.sizing_plans(min(_int('limit', 20) or 20, MAX_LIMIT)))

    @app.get('/api/settings')
    def api_settings():
        return ok(q.settings())

    @app.get('/api/meta')
    def api_meta():
        sch = ro.schema()
        policy = app.config['POLICY']
        return ok({'api_version': API_VERSION,
                   'dashboard_version': DASHBOARD_VERSION,
                   'read_only': True, 'can_trade': False,
                   'can_modify_engine': False,
                   'environment': env,
                   'engine_version': _engine_version(),
                   'schema_version': sch.get('version'),
                   'schema_status': sch.get('status'),
                   'auth_required': app.config['AUTH_REQUIRED'],
                   'access_policy': app.config['POLICY_MSG'],
                   'tier': app.config['DCFG'].tier,
                   'cors_origins': app.config['CORS_ORIGINS'],
                   'rate_limits': SEC.ENDPOINT_LIMITS,
                   'config_warnings': app.config['CONFIG_WARNINGS'],
                   'database': os.path.basename(path),
                   'endpoints': sorted(
                       str(r) for r in app.url_map.iter_rules()
                       if str(r).startswith('/api'))})

    # ══ الواجهة الساكنة ══
    @app.get('/')
    def index():
        with open(os.path.join(FRONTEND, 'index.html'), encoding='utf-8') as f:
            html = f.read()
        html = html.replace('__NONCE__', getattr(g, '_nonce', ''))
        r = make_response(html)
        r.headers['Content-Type'] = 'text/html; charset=utf-8'
        return r

    MIME = {'.css': 'text/css; charset=utf-8',
            '.js': 'text/javascript; charset=utf-8',
            '.mjs': 'text/javascript; charset=utf-8',
            '.html': 'text/html; charset=utf-8',
            '.svg': 'image/svg+xml', '.json': 'application/json'}

    @app.get('/<path:filename>')
    def static_files(filename):
        if filename.startswith('api/'):
            return err('NOT_FOUND', 'المورد غير موجود', 404)
        # منع الخروج من مجلد الواجهة
        base = os.path.realpath(FRONTEND)
        full = os.path.realpath(os.path.join(base, filename))
        if not full.startswith(base + os.sep):
            return err('NOT_FOUND', 'المورد غير موجود', 404)
        if not os.path.isfile(full):
            return index()
        # قراءة صريحة تغلق المقبض — send_from_directory يتركه مفتوحاً
        with open(full, 'rb') as f:
            body = f.read()
        r = make_response(body)
        r.headers['Content-Type'] = MIME.get(
            os.path.splitext(full)[1], 'application/octet-stream')
        return r

    return app


def main():
    host = os.getenv('DASHBOARD_API_HOST', '127.0.0.1')
    port = int(os.getenv('DASHBOARD_API_PORT', '8080'))
    app = create_app()
    print(f"""
╔══════════════════════════════════════════════════════════╗
║  CRYPTO TRADING INTELLIGENCE DASHBOARD  v{DASHBOARD_VERSION}          ║
║  READ-ONLY — لا ينفّذ أوامر تداول إطلاقاً                ║
╠══════════════════════════════════════════════════════════╣
║  http://{host}:{port}
║  البيئة: {app.config['ENVIRONMENT']}
║  القاعدة: {app.config['DB_PATH']}
╚══════════════════════════════════════════════════════════╝
""")
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == '__main__':
    main()
