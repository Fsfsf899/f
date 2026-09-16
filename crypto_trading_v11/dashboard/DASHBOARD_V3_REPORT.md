# DASHBOARD V3 — تقرير التشديد

## 1. ملخص التغييرات

إعدادات مركزية بفحص إقلاع صارم · ثلاثة مستويات بيئة · فحص المفتاح السري ·
انتهاء الجلسة بالخمول · دوران معرّف الجلسة · حد تخمين لكل مستخدم ·
تأخير تدريجي · حدود معدّل بمجموعات مسارات · `X-RateLimit-Reset`.

**لم تُضَف أي صلاحية تداول.**

## 2. V2 ← V3

| | V2 | V3 |
|---|---|---|
| المفتاح السري | لا يوجد | إلزامي خارج local + 5 فحوص قوة |
| مستويات البيئة | لا يوجد | `local`/`private`/`production` |
| DEBUG | بلا فحص | ممنوع خارج local |
| HTTPS | بلا فحص | إلزامي في production |
| انتهاء الجلسة | عمر أقصى فقط | + خمول مستقل |
| دوران المعرّف | لا | نعم — يمنع session fixation |
| حد التخمين | IP فقط | IP **و** اسم مستخدم |
| تأخير الفشل | لا | أسّي بسقف 4 ثوانٍ |
| حدود المعدّل | واحد لكل المسارات | 4 مجموعات |
| رؤوس المعدّل | Limit/Remaining | + Reset/Group |
| الاختبارات | 133 | **178** |

## 3. الملفات المعدَّلة

**جديدة:** `backend/config.py` · `tests/test_dashboard_config.py`
**معدَّلة:** `backend/app.py` · `backend/security.py` ·
`tests/test_dashboard_security.py` · `.env.example` · `DEPLOYMENT.md`

## 4. Read-Only

بلا تغيير: كل طريقة غير `GET` مرفوضة بـ 405 قبل التوجيه، عدا
`POST /api/auth/{login,logout}` — مُختبَر أنها لا تمسّ قاعدة المحرك.
`can_trade:false` و`can_modify_engine:false` في كل رد.

## 5. المصادقة

PBKDF2-HMAC-SHA256 · 200,000 جولة · ملح فريد · `compare_digest`.

**لماذا ليس Argon2 أو bcrypt؟** غير مثبَّتين، وسجلّا pip و npm **محجوبان**
في بيئة البناء. PBKDF2 من المكتبة القياسية بـ 200k جولة مقبول لهذا
الاستخدام. الترقية سطر واحد في `security.py` عند توفر الشبكة.

الفشل لا يكشف وجود المستخدم: نفس الرد والرمز، والتحقق يجري بهاش وهمي
حتى مع مستخدم غير موجود.

## 6. المفتاح السري

مرفوض إن كان: مفقوداً · أقصر من 32 · قيمة من قائمة 17 قيمة معروفة ·
حرفاً مكرراً · تنوّع محارف أقل من 8. في `local` تحذير، خارجها **رفض إقلاع**.
لا يظهر في `safe_dict()` ولا في أي رد.

## 7. الجلسات

شرطان مستقلان: عمر أقصى (`ttl`) وخمول (`idle_s`). الثاني يحمي شاشة
تُركت مفتوحة. الرد يحمل `session_expiry: IDLE_TIMEOUT | MAX_AGE`.

دوران المعرّف عند الدخول يُبطل أي رمز زرعه مهاجم قبله.
`destroy_user()` يُبطل كل جلسات مستخدم.

## 8. CORS

معطّل افتراضياً. `*` يُرفض عند الإقلاع خارج local، وفي local يُقبل بتحذير
لكن **لا يُعاد في أي رد**. Origin غير مشفَّر مرفوض في production.

## 9. Rate Limiting

| المجموعة | الحد/دقيقة | المسارات |
|---|---|---|
| `login` | 10 | `/api/auth/*` |
| `heavy` | 30 | performance · accuracy · equity |
| `list` | 120 | recommendations · trades · audit · positions |
| `light` | 300 | meta · health · dashboard |

المجموعات مستقلة — استنفاد إحداها لا يُعطّل غيرها.
429 + `Retry-After` + `X-RateLimit-{Limit,Remaining,Reset,Group}`.

⚠️ في الذاكرة — لا يعمل عبر عدة عمّال. مع gunicorn استخدم الوكيل أو Redis.

## 10. CSP والرؤوس

```
default-src 'none'; script-src 'self' 'nonce-<فريد لكل طلب>';
style-src 'self'; img-src 'self' data:; connect-src 'self';
base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'
```

بلا `unsafe-inline` وبلا `unsafe-eval`. مع `X-Frame-Options: DENY` ·
`nosniff` · `no-referrer` · `Permissions-Policy` · `no-store` · `Pragma`.

## 11. SQLite

`mode=ro` + `PRAGMA query_only` + فحص نصي + لا استيراد لكلاس الكتابة.
تجاوز الفحص والوصول لـ sqlite مباشرة **ما زال يفشل**.
قاعدة مفقودة ⇒ `OFFLINE` ولا تُنشأ بديلة · جداول ناقصة ⇒ `DEGRADED` ·
نسخة مختلفة ⇒ `SCHEMA_MISMATCH` · سقف 5000 صف · timeout 4 ثوانٍ.

## 12. Pagination

`limit` مسقوف بـ 200 · `offset` سالب يُصحَّح · معاملات مربوطة حصراً.

## 13. سجل الوصول

المستخدم · المسار · الوقت · الحالة · المدة · البيئة · IP مُخفى جزئياً.
لا كلمات مرور ولا كوكيز ولا رؤوس مصادقة — مُختبَر بالبحث عنها.

## 14. Backend Tests — 136

| المجموعة | العدد |
|---|---|
| `test_dashboard_api` | 48 |
| `test_dashboard_security` | 46 |
| `test_dashboard_config` | 42 |

## 15. Frontend Tests — 42

## 16. compileall — PASS

## 17. XSS و SQL Injection

`?symbol=' OR '1'='1` → **200 · صفر نتائج**.
لا `innerHTML` ولا `eval` ولا `Function` — مُختبَر بفحص المصدر.
نص خبيث يُدرَج كـ TextNode ولا يُنفَّذ.

## 18. المخاطر المتبقية

PBKDF2 بدل Argon2 (الشبكة محجوبة) · الجلسات والمعدّل في الذاكرة —
لا توزيع عبر عمّال · Werkzeug خادم تطوير · لا 2FA · لا تدوير للمفتاح ·
المستخدمون في متغير بيئة (مخزن منفصل أفضل) · **لا بيانات فعلية** حتى
تشغّل `live_trader.py paper`.

## 19. التشغيل المحلي

```bash
cd dashboard && pip install -r requirements.txt
cp .env.example .env
./run.sh                     # 127.0.0.1:8080
```

## 20. شبكة خاصة

```bash
DASHBOARD_ENVIRONMENT_TIER=private
DASHBOARD_SECRET_KEY=$(python3 -c "import sys;sys.path.insert(0,'backend');import config;print(config.generate_secret())")
DASHBOARD_USERS=ops:pbkdf2_sha256$200000$...
DASHBOARD_HTTPS_ENABLED=1
DASHBOARD_CORS_ORIGIN=https://ops.internal
```

خلف وكيل عكسي بـ TLS. انظر `DEPLOYMENT.md`.

## 21. الجاهزية

| البيئة | التصنيف |
|---|---|
| Localhost | ✅ **LOCAL READY** |
| شبكة خاصة | ✅ **PRIVATE NETWORK READY** |
| الإنترنت العام | ⛔ **NOT READY** |

`PUBLIC INTERNET READY` يتطلب: SSO/OIDC · WAF · تحديد معدّل موزّع ·
مراقبة وتنبيه · خطة استجابة · تدقيق خارجي. لا شيء منها موجود.

---

## THE DASHBOARD CANNOT TRADE

لا Kill Switch ولا Release ولا تعديل إعدادات. أي إجراء حساس مستقبلاً
يكون في خدمة منفصلة بمصادقة أقوى وتأكيد يدوي وتدقيق مستقل.
