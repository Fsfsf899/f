# DASHBOARD V1 — تقرير التسليم

## 1. المعمارية

```
Trading Engine ──كتابة──> SQLite ──قراءة فقط──> Dashboard API ──> Frontend
   (عملية مستقلة)                                 (Flask)         (متصفح)
```

اللوحة عملية منفصلة تماماً. لا تشارك المحرك عملية ولا قفلاً ولا اتصال
كتابة. إيقافها أو حذفها لا يؤثر عليه.

## 2. الصفحات (9)

Dashboard · Recommendations (+تفاصيل) · Open Positions (+تفاصيل وخط زمني) ·
Trade History · Performance · Accuracy (+منحنى معايرة) · System Health ·
Audit Log · Settings (عرض فقط)

كل صفحة: Loading · Empty · Error · Stale · Normal

## 3. Endpoints — GET فقط

```
/api/meta  /api/dashboard  /api/market  /api/recommendations[/{id}]
/api/positions[/{id}]  /api/trades[/{id}]  /api/performance
/api/accuracy  /api/health  /api/audit  /api/equity
/api/data-quality  /api/settings
```

## 4. مصادر البيانات

`signals` · `recommendations` · `positions` · `orders` · `fills` ·
`order_intents` · `state_transitions` · `risk_events` · `system_events` ·
`daily_equity` · `data_quality` · `process_lock` · `kv`

**لا Mock Data.** الاصطناعي في `tests/` حصراً.

## 5. ضمانات القراءة فقط — كلها مُختبَرة

| الطبقة | الآلية |
|---|---|
| البروتوكول | `before_request` يرفض غير GET بـ 405 **قبل التوجيه** |
| التوجيه | كل قاعدة مسجَّلة `GET` فقط |
| فحص SQL | `SELECT`/`WITH` فقط |
| جلسة SQLite | `PRAGMA query_only=ON` |
| اتصال SQLite | `file:...?mode=ro` — المحرك نفسه يرفض |
| الاعتماديات | لا استيراد لأي وحدة تنفيذ |
| الأسرار | لا مفاتيح؛ بصمة SHA-256 فقط |

مثبَت: تجاوز الفحص النصي والوصول لـ sqlite مباشرة **ما زال يفشل**
بـ `attempt to write a readonly database`.

## 6. الاختبارات — 331

| المجموعة | العدد | النتيجة |
|---|---|---|
| محرك التداول | 263 | OK |
| Dashboard backend | 41 | OK |
| Dashboard frontend | 27 | OK |

شُغّلت بـ `-W error::ResourceWarning`. ستة تشغيلات متتالية بلا تذبذب.

## 7. نتيجة البناء

لا خطوة بناء — لا `npm install` ولا bundler.

```
compileall (src, tests, dashboard, roots)   PASS
تشغيل حيّ على 127.0.0.1:8099                PASS
  GET /                    200 · HTML
  GET /assets/*.{css,js}   200 · 5 ملفات
  13 endpoint              200 · بيانات حقيقية
  POST/PUT/PATCH/DELETE    405 على كل المسارات
  20 طلب قراءة             القاعدة دون تغيير
  المحرك يكتب أثناء العرض  نجح
```

## 8. الفحوص الأمنية

بحث عن `BINANCE_API_KEY` · `BINANCE_SECRET` · `API_SECRET` ·
`PRIVATE_KEY` · `PASSWORD` · `MAINNET_API_SECRET` في backend وfrontend:
**لا نتائج.**

CSP · `X-Frame-Options: DENY` · `nosniff` · `no-store` · CORS معطّل ·
لا stack traces · معاملات SQL مربوطة · `limit` مسقوف بـ 200 ·
حقن SQL مُختبَر ويعيد صفر نتائج.

## 9. القيود المعروفة

**المكدّس ليس المطلوب.** طُلب React/TypeScript/Vite/Tailwind/Recharts،
لكن **npm محجوب (403)** في بيئة البناء فتعذّر تثبيت أو بناء أو اختبار
أيٍّ منها. سُلّم بديل يعمل فعلاً: Flask + ES Modules + SVG يدوي +
كاش مركزي. البنية مفصولة فالنقل مباشر.

**Flask بدل FastAPI** — FastAPI/pydantic غير مثبَّتين ولا يمكن تثبيتهما.
لا يوجد OpenAPI؛ العقد موثَّق في `/api/meta` و`DASHBOARD_README.md`.

**السعر الحالي** يُقرأ من آخر شمعة إشارة مسجَّلة — اللوحة لا تتصل
بالمنصة ولا تجلب أسعاراً لحظية. مُعلَن في واجهة المراكز.

**لا WebSocket** — Flask التطويري لا يدعمه جيداً. Polling ذكي بدلاً منه
(7–12 ثانية حسب النوع، يتوقف عند إخفاء التبويب).

**لا مصادقة** — مصمَّم لـ `127.0.0.1`. النشر خارجياً يتطلب إضافتها.

**خادم تطوير** — لإنتاج فعلي استخدم WSGI حقيقي.

**بيانات فعلية = صفر.** المحرك لم يُشغَّل بعد، فاللوحة ستعرض `N/A` و
`NO DATA` حتى تشغّل `live_trader.py paper`.

## 10. التشغيل

```bash
cd dashboard
pip install -r requirements.txt
cp .env.example .env
./run.sh
```

`http://127.0.0.1:8080`

---

## THE DASHBOARD CANNOT TRADE

لا Buy · لا Sell · لا Cancel · لا Modify · لا Configure · لا Execute.

إن توقفت اللوحة أو تعطّلت أو حُذفت أو اختُرقت — المحرك يستمر، ولا
توجد فيها بيانات اعتماد تداول ولا صلاحية تنفيذ.
