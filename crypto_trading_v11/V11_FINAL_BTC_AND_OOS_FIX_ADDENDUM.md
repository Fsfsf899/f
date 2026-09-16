# V11_FINAL_BTC_AND_OOS_FIX_ADDENDUM

⚠️ يُكمِّل `V11_FINAL_POSITION_SIZING_AUDIT.md` — يوثِّق إصلاحين
إضافيين من نفس الجولة، من قسمين مختلفين تماماً في مستند "V11 Final
Patch".

## ⚠️ ملاحظة بيئة صادقة

منتصف هذه الجولة، أُعيدت تهيئة بيئة التشغيل بالكامل (فُقدت كل الملفات
المؤقتة). استعدت من آخر حزمة سلَّمتها بنجاح (`CRYPTO_TRADING_V11_FINAL_PARTIAL.zip`)
وأعدت تطبيق العمل الذي كان في سياقي فقط (لم يُحفَظ على القرص بعد)
يدوياً. تحقَّقت أن النتيجة النهائية مطابقة لما كانت عليه قبل الانقطاع
(559 اختباراً بفشل واحد معروف قبله، ثم إصلاحه).

## الإصلاح 1 — القسم 12: فشل-الإغلاق عند غياب BTC

**المشكلة المؤكَّدة**: `NoTradeEngine.check()` كان يمنع التداول فقط
عندما تكون بيانات BTC **متوفرة وسيئة**
(`c.require_btc_ok and btc_available and btc_ok is False`) — غيابها
الكامل (`btc_available=False`) كان يمر بصمت تام، حتى مع
`require_btc_ok=True` صراحةً. تفاقم المشكلة: `BTCContext.evaluate()`
نفسها تُعيد `trend_ok=True` كقيمة افتراضية للحقل عند غياب البيانات —
إيجابي صامت بمعنى حرفي لعبارة المستند: "Do not invent BTC values".

**التحقق قبل الإصلاح**: `eng.check(..., btc_ok=None, btc_available=False)`
مع `require_btc_ok=True` → `allowed=True, reasons=[]`. مؤكَّد بالتشغيل
الفعلي، لا افتراضاً.

**الإصلاح**: فصل الحالتين صراحة — `BTC_UNAVAILABLE` (سبب جديد) عند
الغياب، `BTC_RISK` (كما كان) عند التوفر مع سوء. `require_btc_ok=True`
يمنع كلتيهما الآن.

**الأثر الجانبي — ريبل واسع اكتُشف بالتشغيل الفعلي لا بالتخمين**:
هذا تغيير سلوك نشط افتراضياً (`require_btc_ok=True` هو الافتراضي)،
فكسر 9 اختبارات كانت تفترض ضمنياً "BTC غير متوفر = يمر". صحَّحت كل
واحد على حدة حسب طبيعته الحقيقية:
- `test_all.py`/`test_no_trade_thresholds.py`: أُضيف `btc_available=True, btc_ok=True`
  لسياق "نظيف" الحقيقي (BTC متوفر وسليم، لا غيابه)
- `test_entry_models.py`: `base_context()` تحمل الآن `BTCContext` صحياً
  افتراضياً — 43+ اختباراً في هذا الملف تفحص منطق النماذج نفسه، لا
  بوابة BTC كمتغيّر مربِك غير مقصود
- اختبار واحد (`test_config_value_actually_flows_to_check`) عُزل صراحة
  عبر `require_btc_ok=False` — يفحص متغيّراً مختلفاً تماماً (عتبة الثقة)

## الإصلاح 2 — اكتُشف كأثر جانبي: مفاتيح OOS ناقصة عند صفر صفقات

الريبل من الإصلاح 1 كشف مساراً لم يكن يُختبَر مسبقاً بما يكفي:
`walk_forward.run()`'s "لا نوافذ OOS إطلاقاً" (`NO_OOS_TRADES`) كان
يُعيد مجموعة جزئية فقط من المفاتيح — أي كود يقرأ
`wf['oos_window_count']` مباشرة (كما تفترض متطلبات القسم 16 صراحة:
"Report: ... consistency ...") كان يفشل بـ `KeyError` في هذا المسار
تحديداً، رغم نجاحه في المسار العادي. أُصلح بإضافة كل المفاتيح
(`oos_window_count`, `oos_pf_median`, إلخ) بقيم `0`/`None` مناسبة —
لا حذف، لا استثناء خاص، القارئ لا يحتاج فحص "أي مسار وصلني".

## الملفات المُعدَّلة

- `src/signals/no_trade.py` — فصل `BTC_UNAVAILABLE`/`BTC_RISK`
- `src/validation/walk_forward.py` — مفاتيح OOS كاملة في مسار الصفر صفقات
- `tests/test_all.py`, `tests/test_no_trade_thresholds.py`, `tests/test_entry_models.py` — إصلاح سياقات BTC الناقصة

## الملفات المُضافة

- `tests/test_btc_fail_closed.py` — 6 اختبارات مخصَّصة (القسم 24، البند 20)

## النتائج

```bash
$ python3 -m unittest discover -s tests -p 'test_*.py'
Ran 565 tests in 96.127s
OK (skipped=2)

$ python3 -m unittest dashboard.tests...
Ran 136 tests — OK

$ node dashboard/tests/test_frontend.mjs
42/42 ناجح

$ python3 live_trader.py paper --once
exit=0
```

**المجموع: 743 اختباراً — صفر فشل.**

## غير مُنجَز

كل الأقسام الأخرى من مستند "V11 Final Patch" (8-11، 13-23) — OCO
الموسَّع، آلة حالة المركز الصريحة، Restart/Reconciliation الكامل،
جودة البيانات، الباكتست الواقعي، Look-ahead الرسمي — لم تُدقَّق هذا
الدور.

```
READINESS: NOT READY
```
