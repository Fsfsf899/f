# v6 → v7

## الحالة: `PAPER ONLY`

171 اختباراً تنجح. **لم يجرِ تشغيل فعلي على Paper أو Testnet** — الجاهزية
تُثبَت بسجلات تشغيل لا باختبارات وحدة.

---

## أخطاء اكتشفتها الاختبارات أثناء البناء

لم تكن في التشخيص الأولي — ظهرت عند التشغيل الفعلي:

### خاسر السباق يدّعي النجاح — **حرج**
عامل يفشل انتقاله إلى `IN_FLIGHT` كان يقرأ حالة عامل آخر من القاعدة
ويُرجع `ok=True, state=FILLED`. المنصة محمية (أمر واحد)، لكن `open_long`
كان سيسجّل **مركزاً لأمر لم يرسله قط**.
الإصلاح: `not_owner=True` مع `ok=False`.

### نسخة الوقف بعد الإلغاء
`active_intents` يستبعد الحالات النهائية، فبعد إلغاء وقف تعود النسخة
إلى 1 — ومعرّف `v1` مستهلَك ⇒ رفض دائم. أُضيف `_max_intent_version`.

### نية `RESERVED` لا تُنظَّف
ليست ضمن الحالات المانعة فلا يمسحها التعافي. المعرّف الحتمي يبقى
محجوزاً للأبد. `recover_in_flight` يشملها الآن.

### قبول NaN / inf / صفر
البوابة كانت تمرر هذه القيم للمنصة. `validate_numeric` يرفضها قبل الحجز.

---

## إصلاحات التشخيص

| # | المشكلة في v6 | الإصلاح |
|---|---|---|
| C1 | `take_profit` يُخزَّن ولا يُرسل أمر إطلاقاً — المركز يخرج بالوقف فقط بينما الباكتست يفترض خروجاً عند الهدف | نوع `TARGET` + `place_target()` + `limit_sell()` |
| C2 | `_stop_version` يرفع النسخة تلقائياً، فنية `UNKNOWN` تدفع لوضع وقف ثانٍ — التفاف كامل على البوابة | النسخة تُرفع فقط عند استبدال مقصود ومُثبَت |
| C3 | فشل إلغاء الوقف يُبتلَع بـ `except: pass` ثم يُرسل بيع ⇒ بيعان | `_ensure_stop_gone()` — فشل الإلغاء يمنع الخروج ويفعّل مفتاح الإيقاف |
| H4 | `_record_exit` يقرأ `avg=0.0` من الردود المسترجَعة (لا `fills`) ⇒ خسارة كاملة وهمية في التقارير | `normalize_response()` تشتق المتوسط من `cummulativeQuoteQty ÷ executedQty` |
| H5 | المصالحة لا تذكر `order_intents` إطلاقاً | 11 نوع اختلاف: النوايا، التعبئات، انحراف الكميات، أوامر مجهولة |
| H6 | `has_unresolved` عند الإقلاع فقط | فحص في مقدمة كل `tick` |
| H7 | لا Paper Trading — `report` يبقى فارغاً | `PaperBroker` يطابق واجهة العميل بالكامل |
| H8 | لا حاجز Mainnet | `MainnetBlocked` — يتطلب `ALLOW_MAINNET=1` |
| M9 | القفل لـ testnet/live فقط | يشمل كل الأوضاع |
| M10 | `sync_time` مرة واحدة ⇒ انحراف ⇒ `-1021` | `maybe_resync()` كل 30 دقيقة |
| M11 | `pos['qty']` قد يتجاوز الرصيد الفعلي | `min(qty, base_free)` + تقريب الخطوة |

---

## المضاف

**`src/execution/errors.py`** — 10 فئات خطأ. القاعدة: أي خطأ غير مصنَّف
يُعامَل كملتبس (`must_query=True`)، لا كفشل. `redact()` يحجب التواقيع
والمفاتيح قبل أي تسجيل.

**`src/execution/order_state.py`** — 13 حالة، انتقالات مسموحة صراحةً،
`IllegalTransition` عند الخرق. الحالات النهائية مصارف لا تُغادَر.

**`src/storage/migrations.py`** — ترحيل تراكمي، نسخة احتياطية تلقائية،
استرجاع عند الفشل، `PRAGMA user_version`.

**`src/execution/paper_broker.py`** — منصة محاكاة بأسعار حقيقية. تطابق
واجهة `BinanceClient` كاملة، فنفس `OrderManager` و`Gate` و`Reconciler`
تعمل فوقها بلا تعديل.

**`tests/fake_exchange.py`** — 10 أنماط فشل قابلة للتحكم.

---

## قاعدة البيانات

فهارس فريدة **جزئية** (تسمح بـ NULL متعدد):

```sql
ux_orders_client_order_id        ON orders(client_order_id) WHERE NOT NULL
ux_fills_exchange_trade_id       ON fills(exchange_trade_id) WHERE NOT NULL
ux_entry_per_recommendation      WHERE order_type='ENTRY'
ux_stop_per_position_version     WHERE order_type='STOP'
ux_target_per_position_version   WHERE order_type='TARGET'
```

جدول `state_transitions` يسجّل **حتى المرفوضة** — محاولة انتقال غير
شرعي دليل على خلل يستحق التوثيق.

دوال ذرّية: `reserve_intent` · `insert_order_if_absent` ·
`record_fill_if_absent` · `transition_order_state` · `unresolved_intents`.

---

## تغييرات دلالية مقصودة

`test_happy_path` في مجموعة v6 كان يتوقع `CONFIRMED`. v7 يشتق الحالة من
حالة المنصة الفعلية (`FILLED`/`OPEN`/`PARTIALLY_FILLED`) — أدق ويسمح
بتتبّع التنفيذ الجزئي. حُدّث التوقع مع توثيق السبب في الاختبار نفسه.

**لم يُحذف أي اختبار فاشل ولم تُخفَّض توقعاته لإخفاء مشكلة.**

---

## منطق الاستراتيجية

**لم يُمَس.** الإشارات، المؤشرات، المخاطر، الباكتست، المقاييس —
نجاح 76 اختباراً في `test_all` بلا تعديل واحد هو الدليل.

---

## الاختبارات

```
tests.test_all              76   الاستراتيجية والبيانات
tests.test_v7_reliability   43   الاعتمادية (24 سيناريو مطلوب)
tests.test_production       26   التخزين والصحة والدقة
tests.test_idempotency      26   منع التكرار
                           ───
                           171   OK
```

شُغّلت بـ `-W error::ResourceWarning`.

---

## غير منفَّذ

- Outbox كامل — البديل المكافئ مطبَّق (النية قبل الشبكة + تعافٍ عند الإقلاع)
- تشغيل فعلي على Paper أو Testnet
- البنود 19، 21، 27، 34–42 من مواصفات v4
