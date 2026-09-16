# OCO_LIFECYCLE_FIX_REPORT

## نطاق هذا التقرير — صريح

المستند المرفوع (1475 سطراً، 59 قسماً) يعيد وصف معظم ما بُني فعلاً
عبر أدوار سابقة كثيرة من هذا المشروع (محرك الإشارة الموحَّد، No-Trade،
BTC Context، المخاطر مع استمرارية، Testnet/Mainnet lock، اللوحة
بأكثر من 40 صفحة/endpoint). **لم أُعِد تدقيق كل قسم من الـ59** — ذلك
يتطلب عدة أدوار كاملة أخرى. ركّزت هذا الدور على **الادعاء الوحيد
الجديد والمحدَّد بدقة كافية للتحقق الفوري**: القسم 13/56.A —
خلط `orderListId` مع معرّفات الوقف والهدف. هذا التقرير يوثِّق ذلك
الإصلاح فقط بصدق كامل، لا التدقيق الشامل الكامل الذي يطلبه المستند.

## 1. الملفات المُعدَّلة

- `src/storage/database.py` — أعمدة OCO جديدة في `positions`
- `src/storage/migrations.py` — هجرة v3→v4
- `src/execution/paper_broker.py` — `oco_sell()` بنموذج أب/ابنين واقعي
- `src/execution/order_manager.py` — `_place_oco()` يُحلِّل `orderReports`
- `src/execution/reconciliation.py` — تمييز أطراف OCO عن أوامر مجهولة
- `dashboard/backend/queries.py` — حقلا `protected`/`oco_status`
- `dashboard/backend/readonly_db.py` — `EXPECTED_SCHEMA_VERSION=4`
- `tests/fake_exchange.py` — `oco_sell()` جديد كلياً (لم يكن موجوداً)
- `tests/test_v8_acceptance.py` — تصحيح اختبارين كانا يؤكِّدان البق نفسه

## 2. الملفات المُضافة

- `tests/test_oco_lifecycle.py` — 12 اختباراً جديداً

## 3. هجرة قاعدة البيانات

v3→v4: أعمدة `order_list_id`, `list_client_order_id`,
`stop_client_order_id`, `target_client_order_id` في `positions` —
`ALTER TABLE` فقط، **بلا حذف أو إعادة إنشاء بيانات موجودة**.

## 4. المشكلة المؤكَّدة فعلياً — لا افتراضاً

```python
# قبل — src/execution/order_manager.py::_place_oco()
oid = str(r.get('orderListId') or r.get('orderId'))
self.db.update_position(pos_id, stop_order_id=oid, target_order_id=oid)
```

نفس القيمة الواحدة تُخزَّن في **كلا** الحقلين — لا وسيلة للتمييز بين
الأمر الفرعي الحقيقي للوقف والأمر الفرعي الحقيقي للهدف. `PaperBroker`
نفسه كان يُحاكي هذا الخلط (`o['orderListId'] = o['orderId']`)، فكل
اختبار سابق نجح لأنه يختبر مقابل نفس التبسيط الخاطئ.

**الأثر الحقيقي المُكتشَف أثناء الإصلاح (لا في المستند الأصلي):**
`reconciliation.py` كانت تطابق `clientOrderId` من المنصة مقابل
`order_intents` المحلية — لكن بعد الإصلاح، أطراف OCO الفرعية تحمل
`{cid}-STOP`/`{cid}-TARGET`، لا `cid` الأصلي المسجَّل. بلا إصلاح إضافي،
**كل أمر OCO حقيقي كان سيُصنَّف "غير معروف" خطأً** بعد تصحيح المعرّفات
— اكتشفته وأصلحته في نفس الجولة.

## 5. الإصلاح

`_place_oco()` يُحلِّل الآن `orderReports`/`orders` من الاستجابة،
يستخرج معرّف الوقف الفرعي (`STOP_LOSS_LIMIT`) ومعرّف الهدف الفرعي
(`LIMIT_MAKER`) **منفصلين حقاً**، ويُخزِّنهما في الأعمدة الجديدة.

**فشل صريح، لا نجاح مصطنع:** استجابة مشوَّهة (بلا `orderReports`، أو
`orderId` مفقود لأحد الطرفين) ⇒ `OCO_RESPONSE_MALFORMED` حرِج + تفعيل
مفتاح الإيقاف + `return None, None` — لا تخمين، لا `"None"` كنص
مُخزَّن.

## 6. PaperBroker — نموذج واقعي

`oco_sell()` ينشئ الآن أمرين منفصلين فعلياً (`STOP_LOSS_LIMIT` +
`LIMIT_MAKER`) بمعرّفات Order/Client مستقلة، مرتبطين بحقل
`sibling_cid`. عند تنفيذ أحدهما، `_cancel_sibling()` يُلغي الآخر
ويُحرِّر رصيده — تحقَّقت أن هذا **لا يُسبِّب تحريراً مزدوجاً** للرصيد
المحجوز (مُختبَر صراحة).

## 7. FakeExchange — فجوة اختبار لم تكن مكتشَفة

لم يكن يملك `oco_sell()` إطلاقاً — أي اختبار يستخدم `FakeExchange`
(محاكي عميل بينانس الحقيقي، بخلاف `PaperBroker`) لم يختبر مسار OCO
مطلقاً من قبل. أُضيف بنفس البنية الواقعية.

## 8. الاختبارات — 12 اختباراً جديداً (`tests/test_oco_lifecycle.py`)

fixture مطابق حرفياً للمثال المطلوب في المستند:
`orderListId=9001`, `target.orderId=50001/CID-T`, `stop.orderId=50002/CID-S`.

| المجموعة | يثبت |
|---|---|
| معرّفات منفصلة | PaperBroker وFakeExchange كلاهما، تخزين القاعدة |
| الاستمرارية | المعرّفات تبقى بعد Restart حقيقي |
| إلغاء الأخ | كلا الاتجاهين، بلا تحرير مزدوج للرصيد |
| استجابة مشوَّهة | فشل صريح + مفتاح إيقاف، لا `"None"` مُخزَّنة |
| Reconciliation | أطراف OCO شرعية لا تُصنَّف مجهولة |
| اللوحة | `protected`/`oco_status` يُعرَضان بشكل صحيح |

## 9. نتيجة الاختبارات

```
compileall: PASS
tests/ (الكل):        496 اختباراً — OK (تخطٍّ واحد موثَّق)
dashboard/tests (Py): 136 اختباراً — OK
test_frontend.mjs:    42 اختباراً — OK
المجموع: 674 اختباراً — صفر فشل
```

مُختبَر مرتين بعد الإصلاحات الأخيرة، نظيف في كل مرة.

## 10. Mainnet وMainnet lock

```bash
$ grep "MAINNET_ENABLED_IN_SOURCE" src/execution/binance_client.py
MAINNET_ENABLED_IN_SOURCE = False      # ⛔ لا تغيّره إلا بقرار واعٍ
```

بلا مساس. لم تُستخدَم أو تُطلَب أي مفاتيح Mainnet طوال هذه الجولة.

## 11. Paper

```bash
$ python3 live_trader.py paper --once
exit=0
```

يعمل بعد كل التعديلات — نسخة المخطط `4` مؤكَّدة في القاعدة الناتجة.

## 12. Testnet

لم يُشغَّل — لا مفاتيح Testnet متاحة في هذه البيئة.

## 13. المشاكل المتبقية (صريحة)

- **58 من 59 قسماً في المستند لم تُعَد مراجعتها هذا الدور** — كثير
  منها مبني فعلاً من أدوار سابقة (يحتاج التحقق لا البناء)، لكن لم
  يحدث ذلك التحقق هنا.
- OCO الحي (BinanceClient الحقيقي ضد Testnet فعلي) لم يُختبَر — لا
  مفاتيح Testnet متاحة؛ الإصلاح مُختبَر فقط ضد `PaperBroker`/`FakeExchange`.
- الأمر الفردي المنفصل (`place_target()`/`stop_loss_limit()` بلا OCO)
  لم يُلمَس — يبقى كما كان، خارج نطاق هذا الإصلاح.
- لا اختبار "unknown-order detection" الحقيقي (أمر عشوائي غير مرتبط
  بأي نية محلية يجب أن يبقى يُصنَّف مجهولاً بعد الإصلاح — لم يُضَف
  اختبار سلبي مقابل الاختبار الإيجابي).
- لم أُدقِّق حالات `ORDER/POSITION STATES` (القسم 16) المُفصَّلة
  الجديدة (`PROTECTION_PENDING`, `RECOVERY_REQUIRED`, إلخ) — الحالات
  الحالية (`order_state.py`) أبسط مما يطلبه المستند.

## 14. الجاهزية

# `NOT READY FOR TESTNET`

السبب: هذا الدور أصلح ثغرة حرجة واحدة مؤكَّدة (خلط معرّفات OCO)
واختبرها بعمق ضد المحاكاة — لكن **لم يُشغَّل أي شيء ضد Testnet حقيقي
بعد هذا الإصلاح بالذات**، ولم تُراجَع بقية الأقسام الـ58. المستند
نفسه يمنع صراحة `READY FOR TESTNET` قبل التحقق الكامل من دورة حياة
OCO — تحقَّق جزء منها هنا (المحاكاة)، لا الجزء الحي (اتصال Testnet
فعلي بعد هذا التغيير تحديداً).

**الخطوة التالية المنطقية**: تشغيل `testnet --once` بمفاتيح Testnet
حقيقية بعد هذا الإصلاح، لإثبات أن `_place_oco()` يُحلِّل استجابة
Binance **الحقيقية** بشكل صحيح، لا استجابة محاكاة فقط.
