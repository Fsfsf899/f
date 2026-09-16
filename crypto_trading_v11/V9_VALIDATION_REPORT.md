# V9 — تقرير التحقق

**النسخة:** v9.0.0 · **التاريخ:** 2026-09-06
**MAINNET TRADING = DISABLED** (مقفول في المصدر، لا في متغير بيئة فقط)

---

## A. ما أُصلح

### P0 — تعليق مجموعة الاختبارات

`test_parallel_workers_only_one_order` كان يعلّق العملية. الاختبارات
تُبلّغ "Ran 26 tests in 21s" بينما **زمن الجدار 200 ثانية = ضرب المهلة**.

**السبب الجذري:**

```python
barrier = threading.Barrier(6)     # ستة أطراف
... ستة عمّال ينادون wait()
barrier.wait()                      # الخيط الرئيسي = طرف سابع
```

أول ستة يعبرون فيُعاد ضبط الحاجز، والسابع ينتظر جيلاً لا يأتي.
الخيوط كانت **غير daemon**، فتنتظرها Python عند الخروج.

**الإصلاح:** `tests/concurrency.py` — `ConcurrentRunner` بدورة حياة
كاملة: أطراف الحاجز = عدد العمّال بالضبط · الرئيسي يُطلق عبر `Event`
منفصل · كل الخيوط daemon · مهلة على الحاجز والانضمام · إلغاء عبر
`Event` لا قتل · `cleanup` يعمل حتى عند الاستثناء.

**القياس: 200,018ms → 1,484ms.**

### P0 — Mainnet كان يُفتح بمتغير بيئة وحده

خطأ في `.env` أو سكربت تشغيل كان يكفي. الآن بوابتان مستقلتان:
`MAINNET_ENABLED_IN_SOURCE = False` في الكود **و** `ALLOW_MAINNET=1`.
مُختبَر: مع `ALLOW_MAINNET=1` يبقى `mainnet_allowed() == False`.

### P1 — البنود الناقصة

| البند | المضاف |
|---|---|
| 19 مخاطر المحفظة | `src/risk/portfolio.py` — مصفوفة ارتباط سببية، تجميع بالوصل، حدود عنقودية |
| 35 اختبار الإجهاد | `src/validation/stress.py` — 6 سيناريوهات تكلفة |
| 26 مقارنة Paper/Backtest | `src/validation/execution_gap.py` — يرفع `EXECUTION_MODEL_MISMATCH` |
| 14/36 بيانات حقيقية | `validate_real.py` — بلا fallback |
| 5 محرك واحد | `src/backtest/__init__.py` يصدّر `OFFICIAL_ENGINE` + `legacy/README.md` |
| 32 بوابة التحقق | `VALIDATION_STATUS` منفصل عن `STRATEGY_EDGE_STATUS` |

---

## B. المشاكل الباقية

**pytest غير مثبَّت** ولا يمكن تثبيته (بيئة البناء بلا شبكة). استُخدم
`unittest` — الاختبارات متوافقة مع pytest حين يتوفر.

**لا بيانات سوق حقيقية.** بينانس محجوب (403). كل الاختبارات ضد
fixtures ومنصات وهمية.

**لم يُرسل أمر OCO حقيقي واحد.** محاكاتي قد تخالف سلوك المنصة.

**Order Book** — الدوال موجودة، لكن بلا شبكة الحالة `UNKNOWN`.
**News Risk** — `UNKNOWN` دائماً، لا مصدر حقيقي.

---

## C. نتائج الاختبارات

```
compileall (src, tests, roots)          PASS
unittest discover                       263 اختباراً — OK
زمن الجدار                              25,431ms
Timeouts                                0
Hangs                                   0
ResourceWarnings                        0 (شُغّل بـ -W error)
Failures                                0
```

| المجموعة | العدد |
|---|---|
| `test_all` | 76 |
| `test_v8_acceptance` | 59 |
| `test_v7_reliability` | 43 |
| `test_v9` | 33 |
| `test_production` | 26 |
| `test_idempotency` | 26 |

---

## D. توفّر البيانات الحقيقية

```
python3 validate_real.py --symbols BTCUSDT,ETHUSDT,SOLUSDT

⛔ VALIDATION BLOCKED: REAL MARKET DATA UNAVAILABLE
   HTTP 403 @ data-api.binance.vision — Host not in allowlist
   لا يوجد بديل اصطناعي. النتيجة: لا نتائج.
```

`reports/real_validation.json` يسجّل `BLOCKED` بلا أي رقم أداء.

---

## E. منهجية الباكتست

Event-driven. الإشارة تُقرأ عند الشمعة i والتنفيذ عند فتح i+1.
الخروج من High/Low لا Close. سياسة الشمعة المزدوجة **conservative**
(تختار الوقف). الفجوات تُنفَّذ عند الفتح. المركز المفتوح يُغلق صراحةً
بـ `BACKTEST_END`. التكاليف: رسوم + انزلاق + سبريد، وانزلاق الوقف ×2.5.

**Gross و Net معروضان دائماً. الحكم على Net.**

---

## F. منهجية Walk-Forward

```
TRAIN → OPTIMIZE → FREEZE → OOS → نافذة تالية
```

تداخل TRAIN/OOS مفحوص برمجياً ويُرفض بـ `assert`. المعاملات تُجمَّد
قبل OOS. المعايرة على TRAIN فقط. لكل نافذة: الفترتان، الصفقات،
نسبة الفوز، PF، التوقع، العائد، التراجع، Sharpe، Sortino، Calmar.

---

## G. نتائج OOS

**لا توجد.** البيانات الحقيقية محجوبة.

من fixtures سابقة (**لا دلالة على السوق**): TRAIN أعطى PF بين 3.6 و6.3
ثم انهار OOS إلى 1.65 مع نافذتين خاسرتين — بصمة Overfitting.

---

## H. المعايرة

Isotonic و Platt، مقاسان بـ Brier وخطأ المعايرة وجدول الموثوقية.
تُدرَّب على TRAIN حصراً. قبل المعايرة `predict()` **يرمي استثناءً** —
لا يُعرض أي احتمال. مُختبَر أن Brier يتحسّن خارج العينة.

**لم تُعاير على بيانات حقيقية.**

---

## I. المخاطر

الحد اليومي من `daily_starting_equity` الثابت. المخاطرة الفعلية تشمل
الرسوم والانزلاق (**أعلى بـ 18.27%** من الاسمية). الارتباط يجمّع
BTC/ETH/SOL في عنقود واحد ويمنع 55% انكشاف عند حد 35%.
عند نقص البيانات: `CORRELATION_UNKNOWN` — لا يُفترض استقلال.

---

## J. Paper Trading

| | |
|---|---|
| تشغيل فعلي | **0 يوم** |
| صفقات | **0** |
| بوابة paper | **FAIL** — `runtime_days 0/14` |

الوسيط الورقي مختبَر آلياً: فجوة عند 49,000 نُفّذت عند **45,714**
(انزلاق 60bps) · تنفيذ جزئي · أرصدة locked · OCO · رفض ومهلة و
rate limit وانقطاع بعد القبول · استعادة بعد إعادة التشغيل.

---

## K. حالة الحافة

# `STRATEGY_EDGE_STATUS: UNKNOWN`

لا بيانات حقيقية ⇒ لا حكم. **لم تُثبَت حافة، ولم تُنفَ.**

`VALIDATION_STATUS: BLOCKED`

---

## L. القيود المعروفة

Testnet ≠ Mainnet في السيولة والانزلاق والرفض · الوسيط الورقي يقيّم
عند النبضة لا لحظياً فنتائجه **حد أعلى** · `STOP_LOSS_LIMIT` ليس
ضماناً أثناء الانهيارات · نية عالقة أياماً قد تبقى `MANUAL` بانتظار
تدخل يدوي · منطق الإشارة ما زال EMA/RSI/ADX/MACD وأي حافة فيها
استُهلكت منذ عقود.

---

## M. الجاهزية

| | |
|---|---|
| Testnet | ❌ — بوابة paper لم تُجتَز |
| Mainnet | ❌ **مقفول في المصدر** |
| التصنيف | `PAPER_ONLY` |

**MAINNET يبقى DISABLED مهما كانت نتائج الاختبارات.**

---

## الخطوة التالية

```bash
python3 -m unittest discover -s tests
python3 live_trader.py check
python3 live_trader.py paper            # ≥14 يوماً
python3 live_trader.py gate --env paper
python3 validate_real.py                # يتطلب وصولاً لبينانس
```

نجاح 263 اختباراً يعني أن الكود يتصرف صحيحاً تحت الفشل.
**لا يعني أن الاستراتيجية تربح.**
