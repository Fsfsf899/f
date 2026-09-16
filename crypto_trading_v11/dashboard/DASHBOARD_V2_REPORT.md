# DASHBOARD V2 — تقرير الأمان

## 1. ملخص التغييرات

مصادقة بجلسات · تحديد معدّل · CSP بـ nonce بلا `unsafe-inline` ·
إزالة ناقل XSS · فحص نسخة المخطط · ظرف رد موحّد · وسوم مصدر البيانات ·
عزل قاعدة أعمق · سجل وصول · CORS صارم · رفض إقلاع غير آمن.

**لم تُضَف أي صلاحية تداول.** لا Buy ولا Sell ولا Cancel ولا Execute.

## 2. المشاكل المكتشفة

### Critical
| # | المشكلة | الإصلاح |
|---|---|---|
| C1 | لا مصادقة إطلاقاً | جلسات PBKDF2 + رفض إقلاع عند ربط عام بلا مصادقة |
| C2 | `CORS_ORIGIN` يقبل `*` | مرفوض عند الإقلاع بـ `RuntimeError` |
| C3 | `innerHTML` في `ui.js:8` — ناقل XSS | حُذف الخيار؛ كل نص عبر `createTextNode` |

### High
| # | المشكلة | الإصلاح |
|---|---|---|
| H4 | لا Rate Limiting | نافذة منزلقة 240/دقيقة + `Retry-After` |
| H5 | `unsafe-inline` في CSP | الأنماط السطرية → أصناف؛ nonce للسكربت |
| H6 | لا فحص نسخة مخطط | `SCHEMA_MISMATCH` / `DEGRADED` في الرد |
| H7 | ظرف رد ناقص | `{ok,data,meta}` مع `data_as_of`/`stale`/`sample_size` |
| H8 | لا وسم مصدر | `ENGINE_REPORTED` / `DASHBOARD_DERIVED` / … |

### Medium
لا timeout للاستعلام → progress handler 4 ثوانٍ ·
لا سقف صفوف → 5000 · `offset` سالب → يُصحَّح ·
لا `Permissions-Policy`/`Pragma` → أُضيفا · لا سجل وصول → سجل دائري

### اكتُشف أثناء العمل
**path traversal** في خدمة الملفات الساكنة — `realpath` + فحص البادئة.
**ResourceWarning** من `send_from_directory` يترك مقبضاً مفتوحاً — قراءة صريحة.

## 3. الملفات المعدَّلة

**جديدة:** `backend/security.py` · `backend/envelope.py` ·
`tests/test_dashboard_security.py` · `DEPLOYMENT.md`

**معدَّلة:** `backend/app.py` · `backend/readonly_db.py` ·
`frontend/assets/{ui,api,app,styles.css}` · `frontend/index.html` ·
`tests/test_dashboard_api.py` · `tests/test_frontend.mjs` ·
`.env.example` · `DASHBOARD_README.md`

## 4. القراءة فقط

كل طريقة غير `GET`/`HEAD`/`OPTIONS` تُرفض بـ 405 **قبل التوجيه**.
الاستثناء الوحيد `POST /api/auth/{login,logout}` — جلسات عرض لا تمسّ
قاعدة المحرك، ومُختبَر أن عدد الإشارات لا يتغير بعدها.

## 5. عزل القاعدة

`mode=ro` + `PRAGMA query_only` + فحص نصي + لا استيراد لكلاس الكتابة.
تجاوز الفحص النصي والوصول لـ sqlite مباشرة **ما زال يفشل**.
قاعدة مفقودة ⇒ `OFFLINE` **ولا تُنشأ بديلة**. جداول ناقصة ⇒ `DEGRADED`.

## 6. منع تسريب الأسرار

اللوحة لا تقرأ `TESTNET_API_*` ولا `MAINNET_API_*` — مُختبَر بفحص المصدر.
المفتاح يظهر كبصمة SHA-256 (12 محرفاً) فقط. الأخطاء بلا stack trace
ولا مسار ملف. السجل بلا كوكيز ولا رؤوس مصادقة، وIP مُخفى جزئياً.

## 7. المصادقة

PBKDF2-HMAC-SHA256 · 200,000 جولة · ملح فريد · مقارنة ثابتة الزمن.
كوكي `HttpOnly` + `SameSite=Strict` + `Secure` (بالضبط) + انتهاء ساعة.
قفل بعد 5 محاولات لمدة 15 دقيقة. **لا يكشف وجود المستخدم** — نفس الرد
والرمز لكل فشل، والتحقق يجري حتى مع مستخدم غير موجود.

## 8. CORS

معطّل افتراضياً. `*` مرفوض عند الإقلاع. Origins محددة تُقابَل بالضبط
مع `Vary: Origin`. Origin غير معروف لا يُعاد.

## 9. رؤوس الحماية

```
Content-Security-Policy: default-src 'none'; script-src 'self' 'nonce-…';
  style-src 'self'; img-src 'self' data:; connect-src 'self';
  base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'
X-Frame-Options: DENY · X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer · Permissions-Policy: geolocation=(), …
Cache-Control: no-store, no-cache, must-revalidate · Pragma: no-cache
```

**بلا `unsafe-inline` وبلا `unsafe-eval`.** nonce جديد لكل طلب.

## 10. تحديد المعدّل

240 طلب/دقيقة لكل IP على `/api/*`. الملفات الساكنة غير محدودة.
`X-RateLimit-Remaining` في كل رد. 429 + `Retry-After` عند التجاوز.

## 11. Pagination

`limit` مسقوف بـ 200 · `offset` سالب يُصحَّح · سقف 5000 صف للاستعلام ·
timeout 4 ثوانٍ · معاملات مربوطة حصراً.

## 12. Cache-Control

`no-store, no-cache, must-revalidate` + `Pragma` + `Expires: 0` على كل رد.

## 13. Backend Tests — 91

| المجموعة | العدد |
|---|---|
| `test_dashboard_api` | 45 |
| `test_dashboard_security` | 46 |

## 14. Frontend Tests — 42

تنسيق · مكوّنات · مخططات · XSS · لا `eval` · لا أسرار · nonce ·
لوحة المفاتيح · الجوال · عدم الاعتماد على اللون.

## 15. compileall

```
python3 -m compileall -q dashboard    PASS
```

## 16. SQL Injection

`?symbol=' OR '1'='1` → **200 · صفر نتائج**. كل الاستعلامات معاملات مربوطة.
معاملات فاسدة (`limit=abc`, `date_from=NaN`) → 200 بلا انهيار.

## 17. الفصل عن محرك التنفيذ

لا استيراد لـ `order_manager` · `BinanceClient` · `IdempotentOrderGate` ·
`PaperBroker` · `market_buy` · `market_sell` — مُختبَر بفحص المصدر.

15 قراءة متتالية: الإشارات 46 → 46. والمحرك كتب أثناءها بنجاح.
اختبارات المحرك الـ263 ما زالت تنجح — لم يُمَس.

## 18. المخاطر المتبقية

الجلسات في الذاكرة — تُبطَل بإعادة التشغيل (مقصود، لكن لا توزيع) ·
تحديد المعدّل بالذاكرة — لا يعمل عبر عدة عمّال ·
`Werkzeug` خادم تطوير — استخدم gunicorn ·
لا TLS داخل التطبيق — يتولاه الوكيل ·
لا 2FA ·
السعر الحالي من آخر شمعة إشارة لا لحظي ·
**لا بيانات فعلية** حتى تشغّل `live_trader.py paper`.

## 19. التشغيل الآمن

```bash
cd dashboard && pip install -r requirements.txt
cp .env.example .env
DASHBOARD_API_HOST=127.0.0.1 ./run.sh
```

خارج localhost: انظر `DEPLOYMENT.md`. الربط العام بلا مصادقة **يرفض الإقلاع**.

## 20. الجاهزية

| البيئة | التصنيف |
|---|---|
| Localhost | ✅ **LOCAL READY** |
| شبكة خاصة | ✅ **PRIVATE NETWORK READY** — بمصادقة + TLS + CORS مقيد |
| الإنترنت العام | ⛔ **NOT READY** — ينقص SSO وWAF ومراقبة وتدقيق |

---

## THE DASHBOARD CANNOT TRADE

`can_trade: false` و`can_modify_engine: false` في كل رد.
لا Kill Switch ولا Release ولا تعديل إعدادات — لو لزمت مستقبلاً،
فخدمة منفصلة بمصادقة أقوى وتأكيد يدوي وتدقيق مستقل.
