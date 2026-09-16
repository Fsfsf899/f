# نظام تحليل وتوقيت التداول — v9

Spot / Long فقط على بينانس. استخدام شخصي.
**لا Futures، لا Leverage، لا Short، لا Margin.**

---

## الحالة: `PAPER_ONLY`

| الشرط | الحالة |
|---|---|
| الاختبارات | ✅ 646 تنجح (محرك+دخول+مخاطر 468، لوحة 136، واجهة 42) |
| MAINNET | ⛔ مقفول في المصدر |
| بيانات حقيقية | ❌ BLOCKED — لا fallback |
| حالة الحافة | UNKNOWN — لم تُثبَت |
| فصل البيئات | ✅ قاعدة/مفاتيح/قفل مستقل |
| Paper Broker محافظ | ✅ فجوات وتنفيذ جزئي وأعطال |
| بوابات الترقية | ✅ paper → testnet → readiness |
| منع تكرار الأوامر | ✅ مثبَت باختبارات تزامن ومهلة |
| استعادة الحالة | ✅ مثبَتة |
| المصالحة | ✅ 11 نوع اختلاف |
| Mainnet محجوب | ✅ افتراضياً |
| تشغيل Paper فعلي | ❌ لم يجرِ |
| تشغيل Testnet فعلي | ❌ لم يجرِ |
| ربحية الاستراتيجية | ❌ **غير مثبتة** |

**سلامة التنفيذ لا تعني ربحية الاستراتيجية.** كل العمل الهندسي هنا يضمن
أن الأمر لا يتكرر وأن الحالة تُستعاد — ولا يقول شيئاً عن الربح.

---

## التشغيل

```bash
pip install -r requirements.txt
python3 -m unittest discover -s tests

python3 live_trader.py check        # فحص البيئات
python3 live_trader.py monitor      # الافتراضي — بلا أوامر
python3 live_trader.py live-status  # تشخيص بوابة Live (بلا اتصال)
python3 live_trader.py paper        # تنفيذ ورقي محافظ
python3 watchdog.py paper           # نفسه، لكن يُعيد التشغيل تلقائياً عند الانهيار
python3 live_trader.py report --env paper
python3 live_trader.py gate   --env paper
python3 live_trader.py health --env paper
```

انظر `PRODUCTION.md` لدورة حياة الأمر والبيئات وOCO ومفتاح الإيقاف.

---

## البنية

```
src/
  core/config.py              كل العتبات — مصدر واحد
  data/                       OHLCV (شموع مغلقة)، بينانس، كاش، جودة
  indicators/engine.py        محرك واحد، سببي بالكامل
  market/                     هيكل، حالة سوق، أطر متعددة، سياق BTC
  signals/                    Score / Confidence / Probability منفصلة
  risk/                       مخاطرة فعلية، حد يومي بأساس ثابت
  backtest/                   Event-driven، تكاليف واقعية
  validation/                 look-ahead، walk-forward، معايرة
  storage/
    database.py               SQLite، 14 جدولاً، WAL
    migrations.py             ترحيل تراكمي بنسخة احتياطية
  environment/env.py          فصل البيئات وفحص البدء
  execution/
    errors.py                 10 فئات خطأ بسياسة تعامل
    order_state.py            13 حالة، انتقالات صريحة
    idempotency.py            المعرّف الحتمي والبوابة
    binance_client.py         REST موقّع + حاجز Mainnet
    order_manager.py          ENTRY/STOP/TARGET/EXIT/EMERGENCY
    reconciliation.py         11 نوع اختلاف
    paper_broker.py           محاكاة محافظة + OCO + أعطال
  monitoring/
    health.py                 صحة + مفتاح إيقاف مشروط
    accuracy.py               تتبع الدقة
    shadow.py                 تسجيل ما كان سيحدث
    paper_report.py           تقارير يومية وإجمالية
    gates.py                  بوابات الترقية والجاهزية
tests/                        468 اختباراً (محرك + Live + دخول + مخاطر)
```

---

## قواعد مفروضة في الكود

| القاعدة | التطبيق |
|---|---|
| Timeout ليس فشلاً | `errors.py` — غير المصنَّف يستوجب الاستعلام |
| معرّف حتمي | `build_client_order_id` — لا UUID |
| النية قبل الشبكة | `RESERVED` يُكتب قبل أي نداء |
| لا انتقال غير شرعي | `IllegalTransition` + سجل يشمل المرفوضة |
| `UNKNOWN` يمنع التداول | فحص في مقدمة كل دورة |
| لا نظر للأمام | اختبار تشويش المستقبل |
| شموع مغلقة فقط | `drop_unclosed` + خريطة MTF |
| لا احتمال بلا معايرة | `predict()` يرمي إن لم يُدرَّب |
| النجوم رتيبة | خريطة عتبات، لا `%` |
| Mainnet محجوب | `MainnetBlocked` + `preflight` |
| بيئات معزولة | قاعدة/مفاتيح/قفل مستقل، لا fallback |
| لا تحرير إيقاف بسبب قائم | `blocking_conditions()` |
| لا أسرار في السجلات | `redact()` على كل خطأ |
| لا بيانات مخترعة | `DataUnavailable` بلا بديل |

---

## قراءة النتائج

| المقياس | المعنى |
|---|---|
| **Profit Factor خارج العينة** | الرقم الوحيد المهم. تحت 1.0 خاسرة |
| **عدد الصفقات** | تحت 100 = لا استنتاج |
| **ثبات Walk-Forward** | تحت 0.7 = ضجيج |
| نسبة الفوز | مضلِّلة وحدها |

**علامة خطر:** PF داخل TRAIN أعلى بكثير من OOS = Overfitting.

---

## المسار

```
shadow → monitor → paper (≥14 يوماً) → gate → testnet (≥14 يوماً) → gate
       → مراجعة يدوية مستقلة → live canary
```

لا انتقال تلقائي بين المراحل. `live` يتطلب اجتياز البوابتين **و**
إقراراً صريحاً في `.env`.


## التنفيذ الحي

انظر `LIVE_TRADING.md`. القفل المصدري معطَّل افتراضياً؛ 15 شرطاً إضافياً تُفحص قبل أي أمر حقيقي.


## نماذج دخول بحثية — Breakout / Pullback

معطَّلة افتراضياً (`cfg.breakout.enabled` / `cfg.pullback.enabled` = `False`).
غير معتمدة ربحياً — انظر `ENTRY_MODEL_VALIDATION_REPORT.md` و
`ENTRY_LOGIC_BASELINE.md`. تفعيلها للبحث فقط:

```python
cfg.breakout.enabled = True     # أو
cfg.pullback.enabled = True
```

## التشغيل المستمر بلا إشراف

انظر `OPERATIONS_RUNBOOK.md`. `watchdog.py` يعيد تشغيل المحرك عند الانهيار، مع تنبيهات Telegram/Webhook اختيارية عند الأحداث الحرجة. لا يغيّر حالة بوابة Live بأي شكل.
