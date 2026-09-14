# مسار التنفيذ الحي

## ⛔ الحالة الحالية: ممنوع

```
python3 live_trader.py live-status
```

يُظهر 13 من 15 شرطاً فاشلاً الآن. هذا **متوقَّع ومقصود** — لا خطأ.

---

## طبقتا الحماية

### 1. قفل المصدر (الأقوى)

```python
# src/execution/binance_client.py
MAINNET_ENABLED_IN_SOURCE = False
```

لا يُفعَّل بأي متغير بيئة. يتطلب **تعديل هذا السطر يدوياً** في الكود،
مراجعة، ونشر. لا مسار تشغيلي يتجاوزه.

### 2. بوابة التفعيل (15 شرطاً)

```
python3 live_trader.py live-status
```

| الفئة | الشروط |
|---|---|
| المصدر والإذن | `MAINNET_ENABLED_IN_SOURCE`، `LIVE_TRADING_ENABLED`، `RISK_ACCEPTED` |
| المفاتيح | `API_KEY_PRESENT`، `KEY_SEPARATION`، `ENDPOINT_MATCHES_STAGE` |
| الحساب | `NO_WITHDRAWAL_PERMISSION`، `CAN_TRADE`، `IP_RESTRICTED` (تحذير) |
| الترقية | `PAPER_GATE_PASSED`، `TESTNET_GATE_PASSED` |
| التشغيل | `NO_UNRESOLVED_UNKNOWN`، `RECONCILIATION_OK`، `KILL_SWITCH_OFF`، `DATABASE_HEALTHY`، `CLOCK_SYNCHRONIZED` |

**غياب الدليل = فشل**، لا نجاح ضمني. `account_info=None` يعني
`CAN_TRADE: FAIL`، لا تخطياً.

---

## عقد الإشارة

```python
from src.live.trade_signal import TradeSignal, from_engine_signal
```

`TradeSignal` **مجمَّد** (`@dataclass(frozen=True)`). محاولة تعديله
بعد الإنشاء ترفع `FrozenInstanceError`.

- `signal_id` حتمي: نفس الشمعة + النسخة + الإعدادات ⇒ نفس المعرّف
- `payload_hash` يُحسب من المحتوى؛ أي تلاعب بعد البناء يكسر التطابق
- المسار الوحيد للبناء: `from_engine_signal()` من
  `src.signals.engine.Signal` — **لا** `from_dict()` ولا `from_json()`

15 شرط قبول في `validate_signal()`: هوية، بصمة، بيئة، شمعة مغلقة،
حداثة، لا NaN/inf، لا أسباب منع، رمز مسموح، جانب Long فقط، دخول
`ENTER`، وقف موجود ومنطقي (بين 0.10% و10%)، R/R سليم، جودة بيانات ≥80%.

---

## الحدود (`.env`)

```bash
LIVE_MAX_RISK_PER_TRADE_PCT=0.25      # % من الحقوق لكل صفقة
LIVE_MAX_DAILY_LOSS_PCT=1.0
LIVE_MAX_POSITION_NOTIONAL=25.0       # دولار
LIVE_MAX_TOTAL_EXPOSURE_PCT=10.0
LIVE_MAX_CLUSTER_EXPOSURE_PCT=10.0    # BTC/ETH/SOL معاً
LIVE_MAX_OPEN_POSITIONS=1
LIVE_MAX_CONSECUTIVE_LOSSES=3
LIVE_MAX_DAILY_TRADES=3
LIVE_ALLOWED_SYMBOLS=BTCUSDT
```

**هذه أرقام تحتاج مراجعتك، لا توصية.** الافتراضات محافظة عمداً.

---

## عزل المفاتيح

```bash
TESTNET_API_KEY=...    TESTNET_API_SECRET=...
MAINNET_API_KEY=...    MAINNET_API_SECRET=...
```

متغيرات منفصلة تماماً. `key_separation_problems()` يرفض:
مفتاحاً مشتركاً بين البيئتين (بالبصمة لا القيمة) · مفاتيح Mainnet
مضبوطة في بيئة `paper`/`shadow`/`monitor` · تسرّب مفتاح Testnet إلى Live.

---

## المسار

```
shadow → monitor → paper (≥14 يوم) → gate paper
       → testnet (≥14 يوم) → gate testnet
       → مراجعة يدوية مستقلة
       → تعديل MAINNET_ENABLED_IN_SOURCE=True يدوياً
       → live-status حتى يمر كل شرط
       → live_canary (حد $25، مركز واحد)
```

لا انتقال تلقائي بين أي مرحلتين.

---

## `LiveOrderManager`

يُستدعى فقط عند `stage.real_money`. كل `submit()`:

1. `preflight()` — يعيد تقييم البوابة كاملة (لا cache)
2. التحقق من عقد الإشارة (15 شرطاً)
3. الحجم بحدود Live (لا حدود الاستراتيجية العامة)
4. فحص انكشاف المحفظة والعنقود (`PortfolioRisk`)
5. **إعادة تقييم البوابة** — أقرب زمنياً للتنفيذ
6. التنفيذ عبر `OrderManager.open_long` الموجود (نفس `IdempotentOrderGate`)

كل قرار يُسجَّل في `risk_events` بسببه الكامل — ليس فقط EXECUTED/REJECTED.

**البوابة المغلقة تمنع أي نداء شبكة قبل الوصول للمنصة** — مُختبَر
بعدّاد `send_calls == 0`.

---

## الاختبارات — 39

`tests/test_live_execution.py`. `FakeExchange` فيها **MOCK EXCHANGE**
موسوم صراحة — لا اتصال بأي شبكة ولا Binance حقيقي.

أهمها: القفل المصدري يمنع وحده حتى مع كل شرط آخر أخضر · مفتاح مشترك
بين Testnet وMainnet يُكتشف بالبصمة · التلاعب بعد البناء يكسر البصمة ·
غياب الدليل يُعامَل كفشل.

---

## ⚠️ ما هذا **ليس**

هذا لا يجعل الاستراتيجية رابحة. لا يضمن عدم خسارة رأس المال. لا يعني
أن اجتياز `live-status` قرار كافٍ للبدء — هو حد أدنى تقني، لا توصية
مالية.

**الرقم الوحيد المهم يبقى Profit Factor خارج العينة على 100+ صفقة
حقيقية.** لم يُقَس بعد.
