# دليل التشغيل — v8

## ⚠️ قبل كل شيء

هذا النظام **لا يضمن الربحية ولا يمنع خسارة رأس المال**. نجاح Paper
أو Testnet ليس دليلاً على أن الاستراتيجية مربحة أو أن التداول الحقيقي
آمن. Testnet ليس مماثلاً لـ Mainnet: السيولة والانزلاق والرفض تختلف.

---

## الأوامر

```bash
python3 live_trader.py check       # فحص كل البيئات (testnet فقط للاتصال)
python3 live_trader.py shadow      # يسجّل ما كان سيحدث، بلا تنفيذ
python3 live_trader.py monitor     # الافتراضي — توصيات بلا أوامر
python3 live_trader.py paper       # تنفيذ ورقي محافظ
python3 live_trader.py testnet     # أوامر حقيقية، أموال وهمية
python3 live_trader.py live        # 🔴 خلف بوابة + إقرار يدوي

python3 live_trader.py report      --env paper
python3 live_trader.py health      --env paper
python3 live_trader.py gate        --env paper
python3 live_trader.py readiness
python3 live_trader.py recon       --env testnet
python3 live_trader.py intents     --env paper
python3 live_trader.py kill        --env paper
python3 live_trader.py release     --env paper [--force]
python3 live_trader.py migrate     --env paper
```

---

## البيئات

كل بيئة معزولة: endpoint، مفاتيح، قاعدة، سجل، قفل.

| البيئة | endpoint | مفاتيح | قاعدة |
|---|---|---|---|
| shadow / monitor / paper | — | — | `data/<env>/<env>.db` |
| testnet | `testnet.binance.vision` | `TESTNET_API_*` | `data/testnet/testnet.db` |
| live | `api.binance.com` | `MAINNET_API_*` | `data/live/live.db` |

**يوقف التشغيل فوراً عند:** endpoint لا يطابق البيئة · مفاتيح مفقودة ·
نفس السر في بيئتين · قاعدة بيئة تُستخدم لأخرى · `live` بلا `ALLOW_MAINNET=1`.

**لا يوجد fallback صامت** من testnet إلى mainnet ولا من paper إلى testnet.

---

## Paper Broker

محاكاة **محافظة عمداً** — لا تنفيذ بسعر الإغلاق المثالي:

```
شراء سوق  = ask + normal_slippage
بيع سوق   = bid − normal_slippage
وقف عادي  = stop − stop_slippage
وقف بفجوة = السعر الحالي − gap_slippage   ← أسوأ بكثير
رسوم      = تُخصم من كل تنفيذ
```

معاملات التكلفة في `.env` وتُحفظ **مع كل أمر** لإعادة التحليل:
`maker_fee` · `taker_fee` · `spread_bps` · `normal_slippage_bps` ·
`stop_slippage_bps` · `gap_slippage_bps` · `latency_ms` ·
`partial_fill_ratio`.

### قيود معلنة

التقييم عند النبضة لا لحظياً — فرصة الوقف أفضل مما هي على المنصة
أثناء الحركات السريعة. لا عمق دفتر أوامر ولا رفض بسبب السيولة.
**النتائج الورقية حد أعلى، لا توقع.**

---

## OCO — لماذا؟

على Binance Spot **لا يمكن حجز نفس الكمية لأمرين منفصلين**. الوقف
وحده يحجز كامل الرصيد فيفشل الهدف بـ `-2010`.

الحل الوحيد للحماية المزدوجة هو OCO: حجز واحد لأمرين مرتبطين، وتنفيذ
أحدهما يُلغي الآخر.

النظام يحاول OCO أولاً. إن تعذّر، يضع الوقف وحده ويسجّل
`TARGET_SKIPPED_NO_OCO` — الحماية أولوية على الهدف.

---

## Kill Switch

```bash
python3 live_trader.py kill --env paper
python3 live_trader.py release --env paper
```

يستمر بعد إعادة التشغيل (مخزَّن في القاعدة). **لا يُلغي أوامر الوقف
تلقائياً** — إلغاؤها يترك المركز مكشوفاً.

`release` **لا يعمل ما دام السبب قائماً**. يعرض الأسباب المتبقية.
`--force` يتجاوز بقرار واعٍ ويُسجَّل `KILL_SWITCH_FORCE_RELEASED` كحدث حرج.

يُفعَّل تلقائياً عند: اختلاف مصالحة · نية غير محسومة · خطأ مصادقة ·
تجاوز الخسارة اليومية · فشل وضع وقف · بيانات خروج غير موثوقة ·
5 أعطال API · 3 أعطال أوامر · فقدان قفل العملية · اختلاف بيئة.

---

## ما يمنع التداول

نية `IN_FLIGHT` / `UNKNOWN` / `MANUAL` · أي اختلاف مصالحة · مفتاح
الإيقاف · تجاوز الحد اليومي · جودة بيانات دون العتبة · مركز بلا وقف ·
بيانات بايتة · قفل مملوك لعملية أخرى · فشل النبض.

---

## المسار الإلزامي

```
1. shadow    → قياس جودة الإشارة بلا تأثير تنفيذ
2. monitor   → تسجيل التوصيات على سوق حقيقي
3. paper     → تنفيذ ورقي محافظ ≥14 يوماً
   ↓ python3 live_trader.py gate --env paper
4. testnet   → أوامر حقيقية بأموال وهمية ≥14 يوماً
   ↓ python3 live_trader.py gate --env testnet
5. مراجعة يدوية مستقلة
6. live canary — يدوي فقط
```

**لا انتقال تلقائي بين المراحل.**

---

## قبل أي مفتاح Mainnet

- ✅ Spot Trading فقط
- ❌ **Withdrawals مقفول** — النظام يفحصه ويحذّر
- ❌ Futures / Margin
- ✅ تقييد IP
- ✅ مفتاح جديد مستقل تماماً عن testnet
- ✅ حدود صغيرة قابلة للضبط
- ✅ مراقبة بشرية مستمرة وخطة إيقاف طارئ
- ❌ لا رفع تلقائي لرأس المال

**لا يقترح النظام أي رقم مالي.** الحدود قرارك بعد مراجعة مستقلة.

---

## ⚠️ `STOP_LOSS_LIMIT` ليس ضماناً

أثناء الانهيارات قد يقفز السعر تحت حد الـ limit فلا يُنفَّذ ويبقى
مركزك مكشوفاً. طبقات الحماية الثلاث **تقلّل** الخطر ولا تلغيه.

---

## سلامة التنفيذ ≠ ربحية

كل ما في هذا المستند يخص عدم تكرار الأوامر واستعادة الحالة وفصل
البيئات. لا شيء منه يجعل الاستراتيجية رابحة.
