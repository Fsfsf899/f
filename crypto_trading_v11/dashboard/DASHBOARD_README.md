# CRYPTO TRADING INTELLIGENCE DASHBOARD — V3

## THE DASHBOARD CANNOT TRADE

طبقة مراقبة وتحليل فقط. لا تشتري، لا تبيع، لا تلغي، لا تعدّل، لا تُعِدّ.

- لا يوجد endpoint واحد لإنشاء أو تعديل أمر
- كل طريقة غير `GET`/`HEAD`/`OPTIONS` تُرفض بـ **405 قبل التوجيه**
- القاعدة تُفتح `mode=ro` + `PRAGMA query_only` + فحص نصي — ثلاث طبقات
- اللوحة **لا تقرأ مفاتيح التداول** ولا تمرّرها للمتصفح
- إيقافها أو حذفها أو اختراقها لا يؤثر على محرك التداول

---

## الجديد في V2

مصادقة بجلسات (PBKDF2 · 200k جولة) · قفل محاولات الدخول ·
تحديد معدّل · CSP بـ nonce **بلا `unsafe-inline`** · إزالة `innerHTML` ·
فحص نسخة المخطط ⇒ `SCHEMA_MISMATCH` · ظرف رد `{ok,data,meta}` ·
وسوم مصدر البيانات · timeout للاستعلام وسقف للصفوف · سجل وصول بـ IP مُخفى ·
CORS يرفض `*` · منع path traversal · رفض الإقلاع عند ربط عام بلا مصادقة.

## التثبيت والتشغيل

```bash
cd dashboard
pip install -r requirements.txt        # flask فقط
cp .env.example .env
./run.sh                               # أو: python3 backend/app.py
```

افتح `http://127.0.0.1:8080`

### مصادقة (إلزامية خارج localhost)

```bash
python3 -c "import sys;sys.path.insert(0,'backend');
import security as s;print(s.hash_password('كلمتك'))"

# ضع الناتج في .env
DASHBOARD_USERS=ops:pbkdf2_sha256$200000$...
```

انظر `DEPLOYMENT.md` للنشر على شبكة خاصة.

لا خطوة بناء. لا `npm install`. الواجهة ES Modules أصلية.

---

## ملاحظة على المكدّس

المواصفات طلبت **React + TypeScript + Vite + Tailwind + Recharts**.
بيئة البناء لدينا **تحجب سجلّ npm (403)**، فتعذّر تثبيت أو بناء أو
اختبار أيٍّ منها.

اخترت ما يمكن **تشغيله واختباره فعلاً**:

| المطلوب | المسلَّم | السبب |
|---|---|---|
| FastAPI | **Flask 3.1** | FastAPI/pydantic غير مثبَّتين ولا يمكن تثبيتهما |
| React + Vite | **ES Modules أصلية** | npm محجوب — لا build toolchain |
| Tailwind | **CSS مخصص** | يحتاج بناء |
| Recharts | **SVG مكتوب يدوياً** | يحتاج npm |
| TanStack Query | **كاش مركزي في `api.js`** | يحتاج npm |

البنية مفصولة (`api.js` / `ui.js` / `charts.js` / `app.js`) فالنقل إلى
React مباشر: `api.js` يصبح طبقة query، و`ui.js` مكوّنات.

---

## البنية

```
dashboard/
  backend/
    readonly_db.py    ثلاث طبقات منع كتابة
    queries.py        استعلامات العرض — لا إعادة حساب
    app.py            Flask read-only API
  frontend/
    index.html
    assets/
      styles.css      dark trading UI
      api.js          عميل + كاش (قراءة فقط)
      ui.js           مكوّنات قابلة لإعادة الاستخدام
      charts.js       SVG بلا مكتبات
      app.js          توجيه + 9 صفحات
  tests/
    dom_stub.mjs      بيئة DOM مصغّرة
    test_frontend.mjs 27 اختباراً
    test_dashboard_api.py  41 اختباراً
```

---

## المعمارية

```
Trading Engine ──كتابة──> SQLite ──قراءة فقط──> Dashboard API ──> Frontend
     (مستقل)                                    (Flask)          (متصفح)
```

اللوحة **لا تستورد** `order_manager` ولا `BinanceClient` ولا
`IdempotentOrderGate` ولا `PaperBroker` — مثبَت باختبار
`test_dashboard_does_not_import_trading_engine_execution`.

---

## Endpoints

جميعها `GET` فقط:

```
/api/meta            /api/dashboard      /api/market
/api/recommendations /api/recommendations/{id}
/api/positions       /api/positions/{id}
/api/trades          /api/trades/{id}
/api/performance     /api/accuracy       /api/health
/api/audit           /api/equity         /api/data-quality
/api/settings
```

الاستجابة: `{ data, meta: { api_version, environment, read_only, server_time_ms } }`

---

## ضمانات القراءة فقط

| الطبقة | الآلية | مُختبَرة |
|---|---|---|
| البروتوكول | `before_request` يرفض غير GET بـ 405 | ✅ |
| التوجيه | كل قاعدة مسجَّلة `GET` فقط | ✅ |
| فحص SQL | `SELECT`/`WITH` فقط، كلمات الكتابة مرفوضة | ✅ |
| جلسة SQLite | `PRAGMA query_only=ON` | ✅ |
| اتصال SQLite | `file:...?mode=ro` — المحرك يرفض | ✅ |
| الاعتماديات | لا استيراد لأي وحدة تنفيذ | ✅ |
| الأسرار | لا مفاتيح؛ بصمة SHA-256 فقط | ✅ |

---

## الصفحات

Dashboard · Recommendations (+تفاصيل) · Open Positions (+تفاصيل وخط زمني) ·
Trade History · Performance · Accuracy (+منحنى معايرة) · System Health ·
Audit Log · Settings (عرض فقط)

كل صفحة تدعم: Loading · Empty · Error · Stale · Normal

---

## السياسة تجاه البيانات

- **لا Mock Data إطلاقاً.** الاصطناعي في `tests/` حصراً.
- غياب البيانات ⇒ `N/A` وليس `0`.
- عينة أقل من 30 ⇒ `INSUFFICIENT SAMPLE` بارز.
- معايرة غير كافية ⇒ `CALIBRATION INSUFFICIENT` بلا رسم.
- المحرك `ONLINE` فقط بنبض حديث — لا لمجرد وجود عملية.
- الأرقام تُقرأ كما أنتجها المحرك؛ التجميعات فقط تُحسب وتُوسم `derived`.

---

## التحديث التلقائي

| البيانات | الفاصل |
|---|---|
| Dashboard / Positions | 7 ثوانٍ |
| Audit | 10 ثوانٍ |
| Health | 12 ثانية |
| التاريخية | عند تغيير الفلتر فقط |

كاش مركزي يمنع تكرار الطلب لنفس البيانات. التحديث يتوقف عند إخفاء التبويب.

---

## الاختبارات

```bash
python3 -m unittest dashboard.tests.test_dashboard_api        # 45
python3 -m unittest dashboard.tests.test_dashboard_security   # 46
node dashboard/tests/test_frontend.mjs                        # 42
```

---

## الأمان

CSP · `X-Frame-Options: DENY` · `nosniff` · `no-store` · CORS معطّل افتراضياً ·
لا stack traces للعميل · معاملات SQL مربوطة (لا حقن) · `limit` مسقوف بـ 200.

---

## استكشاف الأخطاء

| العرض | السبب | الحل |
|---|---|---|
| `ENGINE CONNECTION LOST` | الخادم متوقف | شغّل `run.sh` |
| `DATABASE UNAVAILABLE` | القاعدة غير موجودة | تحقق من `DASHBOARD_ENVIRONMENT` |
| `NO DATA` / `N/A` | المحرك لم يعمل بعد | `python3 live_trader.py paper` |
| `UNKNOWN MODE` | القاعدة بلا مفتاح `environment` | طبيعي قبل أول تشغيل |
| `STALE` | نبض المحرك قديم | تحقق من عمل المحرك |
