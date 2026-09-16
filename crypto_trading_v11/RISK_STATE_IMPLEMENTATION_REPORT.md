# RISK_STATE_IMPLEMENTATION_REPORT

## المشكلة المؤكَّدة (لا افتراضاً)

فحصت `live_trader.py` فعلياً ووجدت **تطبيقين متوازيين** لعدّاد الخسائر
المتتالية — لا تطبيقاً واحداً به بق كما ظُنَّ أول الأمر:

1. `RiskGuard` (الباكتست فقط) — في الذاكرة، يُصفَّر بـ `new_day()`
2. `LiveTrader._consec_losses()` — إعادة حساب من `recommendations`
   (آخر 10 صفقات مغلقة) في كل نداء، بلا حد يومي إطلاقاً

هذا يعني أن **Paper والباكتست كانا يُطبِّقان سياسة مختلفة المعنى
فعلياً**، رغم أن كلاهما "يعمل" بمنطقه الخاص. مُوثَّق بالتفصيل في
`RISK_STATE_DESIGN.md`.

## الإصلاح

`RiskGuard(cfg, db=None, scope=ACCOUNT_WIDE)` — كائن واحد للجميع:

- **الباكتست**: `db=None` — بلا تغيير سلوكي عن السابق تماماً (مؤكَّد:
  445 اختباراً كانت تعمل قبل هذا التعديل، بقيت 468 تعمل بعده)
- **Paper/Testnet/Live**: `db=self.db` — استعادة تلقائية، حفظ ذرّي
  بعد كل تحديث، حماية تكرار Fill

`OrderManager` يقبل `risk_guard=None` اختيارياً — `record_open()`/`record_trade()`
يُستدعيان تلقائياً من نقطتين مركزيتين فقط (`open_long`, `_record_exit`,
`_settle_after_stop`)، لا من متفرقات في `live_trader.py`.

## 1. القراءة عند الإنشاء

```python
def _restore(self):
    raw = self.db.get_kv('risk_state')
    if not raw:
        self.db.system_event('RISK_STATE_RESTORED', 'لا حالة سابقة...')
        return
    # تحقق صحة: نطاق مختلف؟ لا يُخلَط — يُجوهَل ويُسجَّل حرِجاً
    # استعادة كل الحقول، تسجيل RISK_STATE_RESTORED دائماً
```

## 2. الحفظ الذرّي

`_save()`: نداء `db.set_kv()` واحد (UPSERT SQLite ذرّي أصلاً) بعد كل
تحديث — `new_day()`, `can_trade()` (عند التحوّل لـ halted),
`record_open()`, `record_trade()`.

## 3. منع تكرار Fill

```python
def record_trade(self, pnl, *, trade_id=None, symbol=None) -> bool:
    if trade_id is not None:
        if trade_id == self.last_trade_id or trade_id in self._recent_ids:
            return False   # لا تحديث — يُسجَّل RISK_DUPLICATE_TRADE_IGNORED
```

مُختبَر: `test_same_trade_id_ignored` — العداد يتحرّك مرة واحدة فقط
لنفس `trade_id` مهما تكرَّر النداء.

## 4. الاختبارات — 17 اختباراً (`tests/test_risk_state.py`)

| المجموعة | العدد | تثبت |
|---|---|---|
| السلوك الأساسي | 3 | خسارة تزيد، ربح يصفِّر، يوم جديد يصفِّر |
| الاستمرارية | 5 | حفظ، استعادة عبر Restart حقيقي، `can_trade()` يعكس المُستعاد |
| حماية التكرار | 4 | نفس trade_id يُتجاهَل، معرّفات مختلفة تُحسَب |
| النطاق | 3 | `ACCOUNT_WIDE` افتراضي، `PER_SYMBOL` يرفض، تناقض نطاق لا يُخلَط |
| الاتساق | 2 | `OrderManager` يقبل/يتجاهل `risk_guard` بتوافق خلفي كامل |

## 5. إثبات فعلي عبر Paper حقيقي (لا اختبار معزول فقط)

```bash
$ python3 live_trader.py paper --once   # تشغيل أول
$ python3 live_trader.py paper --once   # تشغيل ثانٍ — نفس القاعدة
```

**النتيجة الفعلية**: التشغيل الثاني سجَّل `RISK_STATE_RESTORED` بنفس
`day_key`/`consecutive_losses` من التشغيل الأول — الاستمرارية تعمل
عبر عملية Python منفصلة تماماً، لا فقط داخل نفس الاختبار.

## 6. أثر جانبي مكتشَف أثناء الربط

`acc['daily_loss_hit']` في `live_trader.py` كان مُغذَّى من
`not h.can_open_new` (حالة صحة شاملة تضم kill_switch/reconciliation)
بدل فحص خسارة يومية فعلي. استُبدل بـ `risk_guard.daily_loss_hit(equity)`
الصحيح دلالياً. **الأمان لم يتأثر** — `pre_trade()` يفحص الصحة الشاملة
بشكل مستقل ومباشر (`'11_health'`) قبل أي تنفيذ فعلي، بلا مساس.

## النتيجة

468 اختباراً — OK. `MAINNET_ENABLED_IN_SOURCE=False` بلا مساس.
`paper --once` يعمل قبل وبعد الإصلاح بلا فرق ظاهر للمستخدم، مع فرق
جوهري في الصحة الداخلية: عداد واحد موثوق بدل اثنين متناقضين.
