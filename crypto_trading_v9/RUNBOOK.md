# RUNBOOK

دليل تشغيل خطوة بخطوة. `OPERATIONS_RUNBOOK.md` يغطي التشغيل المستمر
بلا إشراف عبر `watchdog.py`؛ هذا الملف يغطي كل شيء آخر من الصفر.

---

## 1. المتطلبات

- Python 3.10+ (اختُبر على 3.12)
- لا حاجة لـ Node.js لتشغيل `live_trader.py` (مطلوب فقط لاختبارات
  واجهة اللوحة: `dashboard/tests/test_frontend.mjs`)
- اتصال إنترنت للبيانات العامة (`data-api.binance.vision`) عند
  التشغيل الفعلي — **غير متاح في بيئة البناء الحالية**؛ هذا موثَّق
  ومتوقَّع، لا خطأ في الكود

---

## 2. إنشاء البيئة الافتراضية

```bash
python3 -m venv .venv
source .venv/bin/activate       # أو .venv\Scripts\activate على ويندوز
```

---

## 3. تثبيت الاعتماديات

```bash
pip install -r requirements.txt
```

`numpy` و`scikit-learn` إلزاميان (يُستوردان فعلياً في `src/`).
`scipy`/`pandas` غير مطلوبين إلا لملفات `legacy/` التاريخية — معلَّقان
في `requirements.txt` بتعليق يوضّح السبب. `pytest` اختياري بالكامل؛
لا اختبار واحد يستورده.

---

## 4. تشغيل الاختبارات

```bash
python3 -m compileall -q src tests dashboard/backend dashboard/tests \
    live_trader.py watchdog.py

python3 -m unittest discover -s tests -v
python3 -m unittest discover -s dashboard/tests -v
node dashboard/tests/test_frontend.mjs
```

**567 اختباراً** يجب أن تنجح (389 محرك+Live، 136 لوحة، 42 واجهة).

---

## 5. تشغيل check

```bash
python3 live_trader.py check
```

يفحص كل البيئات الخمس (`shadow`/`monitor`/`paper`/`testnet`/`live`)
ويحاول اتصال Testnet فعلياً. **رمز الخروج مرتبط بنجاح اتصال Testnet
تحديداً** — `live` يظهر `⛔` دائماً بالتصميم ولا يُعد ذلك فشلاً لـ
`check` نفسه، بينما فشل الاتصال بـ Testnet (مفاتيح مفقودة أو انقطاع
شبكة) يُرجع رمز خروج 1 بصدق بدل ادّعاء نجاح لا معنى له.

---

## 6. تشغيل Paper مرة واحدة

```bash
python3 live_trader.py paper --once
```

دورة واحدة كاملة: جلب بيانات → فحص جودة → إشارة → PaperBroker → تسجيل
→ توقف. لا حلقة لا نهائية. رمز الخروج 0 يعني اكتمال الدورة، لا أنها
أنتجت صفقة بالضرورة — `DATA_UNAVAILABLE` عند فشل الشبكة هو نجاح
تشغيلي بمعنى "لم ينهر البرنامج"، مع صدق كامل في عدم وجود بيانات.

للتحكم بعدد الدورات:

```bash
python3 live_trader.py paper --iterations 5 --poll 60
```

---

## 7. تشغيل Paper المستمر

```bash
python3 live_trader.py paper
```

بلا `--once`/`--iterations`: حلقة مستمرة بفاصل `--poll` ثانية
(افتراضي 300). للتشغيل بلا إشراف لأيام، استخدم `watchdog.py` بدلاً
من هذا مباشرة — انظر `OPERATIONS_RUNBOOK.md`.

---

## 8. إيقاف Paper

`Ctrl+C` — يُسجَّل `SHUTDOWN` في القاعدة، يُحرَّر القفل، وتُعرَض أي
مراكز مفتوحة (الأوقاف على المنصة/المحاكاة تبقى فعّالة رغم الإيقاف).

---

## 9. قراءة report

```bash
python3 live_trader.py report --env paper
python3 live_trader.py report --env paper --json   # لتكامل برمجي
```

Win Rate لا يُعرض وحده أبداً — دائماً مع عدد الصفقات وتحذير
`INSUFFICIENT SAMPLE` تحت 30 صفقة.

---

## 10. قراءة health

```bash
python3 live_trader.py health --env paper
```

رمز الخروج 0 فقط إذا كانت الحالة `healthy` فعلاً — لا ادّعاء.

---

## 11. استخدام Kill Switch

```bash
python3 live_trader.py kill --env paper
python3 live_trader.py release --env paper           # يُرفَض إن بقي السبب
python3 live_trader.py release --env paper --force   # تجاوز واعٍ، يُسجَّل حرِجاً
```

---

## 12. إعداد Testnet

في `.env`:

```bash
TESTNET_API_KEY=...
TESTNET_API_SECRET=...
```

مفاتيح **Testnet فقط** — أُنشئت من [testnet.binance.vision](https://testnet.binance.vision)،
صلاحية تداول بلا سحب. لا تضع مفاتيح Mainnet هنا تحت أي ظرف — النظام
يرفض المفتاح المشترك بين البيئتين تلقائياً (بالبصمة، لا بالقيمة).

---

## 13. تشغيل Testnet مرة واحدة

```bash
python3 live_trader.py testnet --once
```

بلا مفاتيح، يطبع `TESTNET_KEYS_MISSING` صراحةً ويخرج برمز 1 — **لا
سقوط صامت إلى Mainnet ولا استخدام Paper كبديل**. بمفاتيح صحيحة،
يتحقق بالترتيب: الاتصال، مزامنة الوقت، الحساب، `canTrade`،
`canWithdraw` (يُرفض إن `True`)، القواعد، ثم الدورة.

---

## 14. تشغيل Testnet المستمر بعد القبول

فقط بعد اجتياز smoke test أعلاه يدوياً ومراجعة السجلات:

```bash
python3 watchdog.py testnet
```

---

## 15. مسارات قواعد البيانات

```
data/shadow/shadow.db
data/monitor/monitor.db
data/paper/paper.db
data/testnet/testnet.db
data/live/live.db          (لن تُنشأ — Mainnet مقفول في المصدر)
```

معزولة تماماً — لا مشاركة بين البيئات، مُختبَر آلياً.

---

## 16. مسارات السجلات

```
logs/paper.log
logs/testnet.log
logs/live_<mode>.log
watchdog.log                (إن استخدمت nohup مع watchdog.py)
```

---

## 17. استعادة الحالة بعد Restart

قاعدة SQLite تُفتح بنفس المسار فتستعيد كل الإشارات والمراكز والنوايا
المسجَّلة تلقائياً — لا حاجة لأي إجراء يدوي. `intents` يعرض أي نية
عالقة تحتاج تدخلاً:

```bash
python3 live_trader.py intents --env paper
```

---

## 18. معنى NO_VALID_SIGNAL

القرار `NONE`/`WAIT` من محرك الإشارة — لا خطأ اتصال، البيانات وصلت
والدورة اكتملت بنجاح، لكن لا توجد فرصة دخول حسب معايير الاستراتيجية.
لا يُعامَل كفشل.

---

## 19. معنى DATA_UNAVAILABLE

تعذّر جلب بيانات السوق (شبكة، حجب، صيانة المنصة). **لا بيانات
اصطناعية أبداً بديلاً.** الدورة تنتهي بأمان وتُسجَّل، والدورة التالية
تحاول من جديد.

---

## 20. ⚠️ Paper وTestnet لا يثبتان الربحية

نجاح كل ما سبق يعني أن **التنفيذ سليم هندسياً** — لا تكرار أوامر، لا
تسريب أسرار، مصالحة صحيحة، استعادة موثوقة. **لا يعني أن الاستراتيجية
تربح.** حالة الحافة (`Strategy Edge`) تبقى `UNKNOWN` حتى تتوفر بيانات
تشغيل حقيقية كافية (14 يوماً و100 صفقة على الأقل) على بيانات سوق
فعلية، لا على fixtures اختبارية.

---

## 21. ⚠️ Mainnet ممنوع

```python
# src/execution/binance_client.py
MAINNET_ENABLED_IN_SOURCE = False
```

مقفول في المصدر، لا بمتغير بيئة. لا يُفعَّل بـ `LIVE_TRADING_ENABLED=1`
ولا `ALLOW_MAINNET=1` ولا أي تركيبة متغيرات — يتطلب تعديل هذا السطر
يدوياً في الكود، وحتى بعده تبقى بوابة `live-status` بـ 13+ شرطاً
إضافياً يجب اجتيازها كلها. **مُختبَر أن هذا يصمد حتى مع متغيرات وهمية
مضبوطة عمداً.**

---

## 22. البحث الاستراتيجي (v9.1.0-research)

```bash
python3 generate_research_reports.py
```

يُنتج `reports/{strategy_baseline,strategy_candidates,walk_forward_results,
stress_results,entry_exit_breakdown}.json` — **على fixture اصطناعي
حصراً**، موسوم صراحة في كل ملف. انظر `STRATEGY_EXPERIMENTS.md` قبل
قراءة أي رقم فيها.

الإعدادات الجديدة (كلها في `.env` أو مباشرة في `Config`، معطَّلة
افتراضياً عدا الأولى):

```python
cfg.signal.use_net_risk_reward   # True افتراضياً — R:R بعد التكاليف
cfg.signal.stop_method            # 'atr' افتراضياً | 'structure' | 'hybrid'
cfg.signal.max_holding_bars       # None افتراضياً (معطَّل) | عدد شموع
cfg.signal.trailing_stop_enabled  # False افتراضياً
```

**لا قيمة افتراضية جديدة هنا مُثبَتة بربح حقيقي — كلها بانتظار
معايرة على بيانات سوق حقيقية.**
