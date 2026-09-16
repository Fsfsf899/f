# ENTRY_LOGIC_BASELINE

توثيق دقيق بقراءة الكود فعلياً، شامل التحقيق المطلوب في العداد.

---

## 1-9. أين تُنشَأ الإشارة، Score، Stars، Confidence، BUY، NO_TRADE

مطابق لِما وُثِّق في `STRATEGY_BASELINE.md` من الدور السابق — لم يتغيّر
شيء في هذه النقاط. **الإضافة الوحيدة الجديدة هذا الدور**: `BaselineEntryModel`
في `src/signals/entry/baseline.py` غلاف رقيق حول `SignalEngine` نفسه
— **لا نسخة موازية، لا إعادة كتابة**. `sig.decision == BUY` تحدَّد
كما كانت دائماً.

`confidence`: **ليس احتمالاً معايراً** — نسبة الأدلة القابلة للتقييم
المتوفرة فقط. `raw_probability`/`calibrated_probability` منفصلان
تماماً (الأخير `None` إلا إن وُجد معايِر مُدرَّب فعلياً).

## 10-16. Regime / Support-Resistance / ATR / Trend / Volume / BTC Context / MTF

كلها موجودة ولم تُمَس:
- Regime: `src/market/regime.py::detect_at()`
- Support/Resistance: `src/market/structure.py` — سببي، تأخير تأكيد
- ATR: `src/indicators/engine.py`
- MTF: `src/market/mtf.py` — موجود، اختياري، الآن أيضاً `MTFEntryConfig.confirmation_enabled` (معطَّل افتراضياً) كطبقة تأكيد إضافية للدخول الجديد فقط، لا تغيير على Baseline

## 17-19. شمعة مغلقة فقط / لا بيانات مستقبلية / نفس المحرك للباكتست والـ Paper

كلها مؤكَّدة كما في الدور السابق. **الجديد**: `BreakoutEntryModel`
و`PullbackEntryModel` اختُبرا صراحةً لعدم النظر للمستقبل (تشويش
الشموع اللاحقة لا يغيّر القرار عند idx — `Test03_NoLookahead`).

## 20. التنفيذ عند فتح الشمعة التالية؟

نعم — لم يتغيّر. نماذج الدخول الجديدة تُنتج `entry_price` كسعر مرجع
فقط (سعر إغلاق الشمعة الحالية)؛ التنفيذ الفعلي في الباكتست/التنفيذ
الحي يبقى عند فتح الشمعة التالية كما كان دائماً.

## 21-23. Stop / Target / Net R:R

Baseline: كما في `STRATEGY_DESIGN.md` (v9.1.0). Breakout/Pullback:
كل منهما يحسب Stop حسب `stop_mode` الخاص به (`atr`/`level`/`hybrid`
لـ Breakout، `atr`/`structure`/`hybrid` لـ Pullback)، ويستخدم **نفس**
`net_risk_reward()` النقية من `src/risk/position_sizing.py` — لا
منطق موازٍ.

---

## 24-27. تحقيق Consecutive Loss Guard — البند المطلوب صراحةً

### أين يُحدَّث العداد؟

`RiskGuard.record_trade(pnl)`: `consecutive_losses = consecutive_losses + 1 if pnl < 0 else 0`
— يُصفَّر عند أي صفقة رابحة (`pnl >= 0`)، يزيد عند خسارة.

### متى يُعاد ضبطه؟

**⚠️ هنا الخلل الحرج المكتشَف هذا الدور.** `new_day()` كان يُصفِّر
`halted` و`daily_trades` لكن **ليس** `consecutive_losses`. والنتيجة:

```
دورة 1-4: 4 خسائر متتالية → consecutive_losses=4 → halted=True
new_day(): halted=False لكن consecutive_losses يبقى 4
can_trade(): يفحص consecutive_losses أولاً → 4>=4 → halted=True مجدداً فوراً
```

**لا صفقة يمكن أن تُفتَح لتُغلَق وتُصفِّر العداد — شلل دائم.** مؤكَّد
بمحاكاة 10 أيام متتالية في `tests/test_entry_models.py::Test00_ConsecutiveLossFix`.

**الأثر على نتائج الدور السابق:** `STRATEGY_EXPERIMENTS.md` (v9.1.0)
سجَّل `CONSECUTIVE_LOSS_LIMIT` كسبب 90.8% من الرفض على fixture 4000
شمعة — **هذا البق هو التفسير**، لا سلوك سوق. الأرقام في ذلك التقرير
كانت مبنية فوق محرك مشلول جزئياً منذ وقت مبكر من الاختبار. لم يكن
ذلك خطأً في استنتاج الدور السابق (كان صادقاً بعرض الرقم كسؤال مفتوح
بدل تجاهله) — لكنه يستحق التصحيح الآن.

### هل يُعاد ضبطه بعد صفقة رابحة؟

نعم، هذا الجزء كان يعمل بشكل صحيح دائماً (`record_trade`).

### هل يُحسَب من قاعدة البيانات أم في الذاكرة؟

**في الذاكرة فقط** (`self.consecutive_losses` على كائن `RiskGuard`
حيّ). هذا يعني: إعادة تشغيل عملية التداول الحي (`live_trader.py`)
تُصفِّر العداد ضمنياً لأن كائن `RiskGuard` جديد يُبنى من الصفر — سلوك
مختلف عن الباكتست (حيث `RiskGuard` واحد يعيش طوال التشغيل). **هذا
تناقض حقيقي بين بيئتي التنفيذ لم أُصلحه في نطاق هذه المهمة** — يستحق
معالجة منفصلة (تخزين `consecutive_losses` في `kv` واستعادته عند
البناء، بنفس نمط `paper_orders`/`paper_balances` الموجود أصلاً
لـ PaperBroker).

### هل ينتقل بين الرموز بالخطأ؟

**نعم — بالتصميم الحالي.** `RiskGuard` كائن واحد لكل تشغيل، لا
تمييز بين رموز. صفقة خاسرة على ETHUSDT تُحسَب ضد الحد نفسه المطبَّق
لاحقاً على BTCUSDT. بما أن هذا المشروع (حسب كل الأدوار السابقة)
يتداول رمزاً واحداً لكل عملية تشغيل عملياً، الأثر محدود حالياً — لكنه
قيد معماري حقيقي لو تعدّدت الرموز مستقبلاً.

### هل ينتقل بين Baseline وBreakout وPullback؟

**نعم — وهذا صحيح ومقصود.** `RiskGuard` مشترك عبر `account_state`
المُمرَّر لكل النماذج الثلاثة عبر `EntryRouter` — خسارة متتالية من
صفقة Baseline تُحسَب ضد صفقة Breakout التالية بنفس الحد. هذا مطابق
تماماً للبند 8 من التكليف: "لا تسمح للنموذج الجديد بتجاوز Baseline
risk guard" — الحدود مشتركة عمداً، لا معزولة لكل نموذج.

### هل يتأثر بالـ Restart؟

نعم (انظر أعلاه: في الذاكرة، بلا استعادة من القاعدة).

### هل يُطبَّق قبل الصفقة أم بعدها؟

**قبلها** — `can_trade()` يُفحَص كجزء من `NoTradeEngine`/بوابة
المخاطر قبل أي محاولة دخول، عبر `account_state['consecutive_losses']`
المُمرَّر للنماذج الجديدة أيضاً.

---

## الفرق بين إشارة Baseline وBreakout وPullback

| | Baseline | Breakout | Pullback |
|---|---|---|---|
| المصدر | 14 دليلاً مُجمَّعة في `score` واحد | مستوى مقاومة سببي + شروط فردية | تأكيد اتجاه + منطقة تراجع + شمعة تأكيد |
| الوقف | ATR فقط (أو `stop_method` إن ضُبط) | `atr`/`level`/`hybrid` مستقل | `atr`/`structure`/`hybrid` مستقل |
| الحالة | بلا حالة بين الشموع | اختياري: آلة حالة Retest (DB) | بلا حالة (تقييم لحظي) |
| الافتراضي | **مفعَّل دائماً** | **معطَّل** (`BREAKOUT_ENABLED=0`) | **معطَّل** (`PULLBACK_ENABLED=0`) |

---

## عدد الإشارات / صفقات Baseline / أعلى أسباب الرفض

على fixture اصطناعي بعد إصلاح خلل `consecutive_losses` — انظر
`reports/entry_exit_breakdown.json` المُحدَّث و`STRATEGY_EXPERIMENTS.md`
لأرقام ما بعد الإصلاح مقابل ما قبله. **لا رقم هنا من بيانات حقيقية.**
