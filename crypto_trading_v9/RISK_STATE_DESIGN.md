# RISK_STATE_DESIGN

## القرار: `ACCOUNT_WIDE`

صريح، ليس افتراضياً ضمنياً. السبب: النظام يُشغِّل رمزاً واحداً لكل
عملية تشغيل (مؤكَّد في `ENTRY_LOGIC_BASELINE.md` — لا تعدد رموز في
أي نسخة سابقة من هذا المشروع). `PER_SYMBOL`/`PER_STRATEGY` غير
مُنفَّذين — استخدامهما يرفع `NotImplementedError` صريحاً (`src/risk/risk_guard.py`)
بدل التصرّف كـ `ACCOUNT_WIDE` بصمت، لتجنّب وهم عزل غير موجود فعلياً.

**الأثر الموثَّق لهذا القرار:** خسارة من صفقة Baseline تُحسَب ضد
الحد نفسه المطبَّق على صفقة Breakout التالية — هذا **مقصود**، لا
عرَضي. البند 8 من المهمة السابقة نصّ صراحة: "لا تسمح للنموذج الجديد
بتجاوز Baseline risk guard".

## البنية المكتشَفة قبل هذا الإصلاح

كان يوجد **تطبيقان متوازيان** لعدّاد الخسائر المتتالية:

| | `RiskGuard` (الباكتست) | `LiveTrader._consec_losses()` (المحذوفة الآن) |
|---|---|---|
| التخزين | في الذاكرة فقط | إعادة حساب من `recommendations` كل نداء |
| المعنى | عداد يومي يُصفَّر بـ `new_day()` | آخر 10 صفقات مغلقة، بلا حد يومي |
| النجاة من Restart | لا (غير مطلوبة — باكتست منتهٍ) | نعم ضمنياً (يُعاد حسابها من القاعدة) |

هذا التناقض هو ما طلبت المهمة توحيده. لم يكن خطأً حرِجاً بذاته (كل
تطبيق كان "يعمل" بمعناه الخاص)، لكنه يعني أن **Paper والباكتست كانا
يُطبِّقان سياسة خسائر متتالية مختلفة المعنى فعلياً** — الأول "متتالية
خلال اليوم"، الثاني "أحدث 10 بلا حدود زمنية".

## التصميم الجديد

`RiskGuard(cfg, db=None, scope=ACCOUNT_WIDE)` — كائن واحد، تُستخدَمه
كل البيئات:

- **الباكتست** (`src/backtest/engine.py`): `RiskGuard(cfg.risk)` — بلا
  `db`، سلوك في الذاكرة فقط **بلا أي تغيير عن السابق**. محاكاة
  تاريخية منتهية لا تحتاج نجاة من Restart.
- **Paper/Testnet/Live** (`live_trader.py`): `RiskGuard(cfg.risk, db=self.db)`
  — يُستعاد تلقائياً من `kv['risk_state']` عند الإنشاء، ويُحفَظ ذرّياً
  (UPSERT واحد) بعد كل تحديث.

`OrderManager` يقبل `risk_guard=None` اختيارياً (توافق خلفي كامل —
كل الاختبارات القديمة التي لا تمرّره تعمل بلا أي فرق). عند تمريره،
`record_open()`/`record_trade()` يُستدعيان تلقائياً في نقطتي الدخول
والخروج المركزيتين (`open_long`, `_record_exit`, `_settle_after_stop`)
— لا حاجة لتوزيع الاستدعاء في `live_trader.py` نفسه.

## الحقول المحفوظة (`kv['risk_state']`)

```json
{
  "scope": "ACCOUNT_WIDE",
  "consecutive_losses": 0,
  "halted": false, "halt_reason": "",
  "daily_trades": 0, "day_key": "2026-01-01",
  "daily_starting_equity": 1000.0,
  "last_trade_id": null, "last_trade_symbol": null, "last_trade_pnl": null,
  "recent_ids": [],
  "saved_at": 1234567890
}
```

## منع تكرار Fill

`record_trade(pnl, trade_id=...)`: إن كان `trade_id` مطابقاً لـ
`last_trade_id` أو موجوداً في نافذة `recent_ids` (آخر 50)، **لا
يُحدَّث شيء** — يُسجَّل `RISK_DUPLICATE_TRADE_IGNORED` ويُعاد `False`.
`trade_id` مُشتَق من `client_order_id` الحقيقي عند توفره، أو
`exit-{position_id}-{order_id}` كبديل حتمي.

## حالة غير منطقية

`halted=False` مع `consecutive_losses >= max_consecutive_losses`
محفوظة سابقاً: **لا تُصلَح بصمت عند الاستعادة** — تُسجَّل
`[INCONSISTENT_STATE_DETECTED]` في حدث `RISK_STATE_RESTORED` للتتبّع؛
`can_trade()` نفسها ستُعيد فرض `halted=True` في أول استدعاء تالٍ على
أي حال (الفحص يُعاد حسابه من `consecutive_losses`، لا من `halted`
المخزَّنة فقط).

## نطاق مختلف محفوظ سابقاً

إن كان `scope` في القاعدة مختلفاً عن `scope` المطلوب حالياً، **لا
تُستخدَم القيم القديمة إطلاقاً** — تُجوهَل، ويُسجَّل `RISK_STATE_SCOPE_MISMATCH`
حرِج. هذا يمنع خلط عداد بمعنى نطاق واحد كأنه بمعنى نطاق آخر لو تغيّر
القرار مستقبلاً.

## ملاحظة جانبية اكتُشفت أثناء الربط

`acc['daily_loss_hit']` في `live_trader.py` كان يُغذَّى من `not h.can_open_new`
(حالة صحة شاملة تضم kill_switch/reconciliation/API failures)، لا من
فحص خسارة يومية فعلي — تحميل زائد للمعنى غير مقصود ظاهراً. استُبدل
بـ `self.risk_guard.daily_loss_hit(equity)` الصحيح دلالياً. **الأمان
لم يتأثر**: `kill_switch`/`reconciliation` يُفحصان بشكل مستقل ومباشر
في `OrderManager.pre_trade()` (فحص `'11_health'`) قبل أي تنفيذ فعلي
— هذا الحاجز الحقيقي بقي دون تغيير.
