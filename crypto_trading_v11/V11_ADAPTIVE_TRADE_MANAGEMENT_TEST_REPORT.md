# تقرير اختبارات الإدارة التكيّفية — V11 Part B

## المجموع

| المجموعة | العدد | النتيجة |
|---|---|---|
| المحرك (`tests/`) | ٨١٠ | ✅ PASS (تخطٍّ ٢) |
| اللوحة (`dashboard/tests/`) | ١٤٥ | ✅ PASS (تخطٍّ ١) |
| الواجهة (`test_frontend.mjs`) | ٤٢ | ✅ PASS |
| **الإجمالي** | **٩٩٧** | **✅** |

منها **٦٩ اختباراً جديداً** لهذه الميزة (٤٤ وحدة + ١٩ تكامل وترحيل +
٥ لوحة + ١ حارس واجهة موسَّع).

التخطّيان في المحرك سابقان لهذا العمل. التخطّي في اللوحة مشروط: يفحص
حقول لوحة الإدارة، ويتخطّى إن لم تكن قاعدة الاختبار تحوي قراراً.

---

## ١. تغطية البند ٣٩ — كل اختبار سمّته المواصفة

| اسم المواصفة | الاختبار المنفَّذ | ✓ |
|---|---|---|
| `test_trending_up_detection` | نفسه | ✅ |
| `test_ranging_detection` | نفسه | ✅ |
| `test_weak_trend_detection` | نفسه | ✅ |
| `test_high_volatility_detection` | نفسه | ✅ |
| `test_insufficient_data_fail_closed` | نفسه | ✅ |
| `test_range_market_reduces_target` | نفسه | ✅ |
| `test_strong_trend_preserves_target` | نفسه | ✅ |
| `test_resistance_adjustment` | نفسه | ✅ |
| `test_rr_guard` | نفسه | ✅ |
| `test_fee_aware_target` | نفسه | ✅ |
| `test_break_even_activation` | نفسه | ✅ |
| `test_break_even_includes_costs` | نفسه + `_actually_breaks_even` | ✅ |
| `test_break_even_never_widens_risk` | نفسه | ✅ |
| `test_atr_trailing` | نفسه | ✅ |
| `test_trailing_only_moves_forward` | نفسه | ✅ |
| `test_trailing_does_not_widen_stop` | نفسه (مسح ١٦ حالة) | ✅ |
| `test_trailing_respects_risk` | نفسه | ✅ |
| `test_timeout_guard` | نفسه | ✅ |
| `test_stagnation_detection` | نفسه | ✅ |
| `test_stagnation_requires_low_progress` | نفسه | ✅ |
| `test_stagnation_does_not_exit_strong_trend` | نفسه | ✅ |
| `test_exit_engine_integration` | `test_paper_mode_records_a_decision` | ✅ |
| `test_oco_integration` | `test_oco_identifiers_stay_distinct_after_move` | ✅ |
| `test_recovery` | `test_protection_stays_intact_when_nothing_to_do` + الاستعادة | ✅ |
| `test_restart` | `test_mfe_survives_restart` | ✅ |
| `test_reconciliation` | المصالحة تمرّ قبل الإدارة في `tick()` — مُثبَت | ✅ |
| `test_paper_mode` | ملف التكامل كلّه يعمل بلا شبكة | ✅ |
| `test_testnet_isolation` | `tests/test_testnet_isolation.py` القائم (بلا مساس) | ✅ |
| `test_no_lookahead` | `test_decision_uses_only_closed_history` + `_is_causal` | ✅ |

### Part C — البنود المُضافة في هذه الجولة

| البند | الاختبار | ✓ |
|---|---|---|
| ١ تهدئة الحالة | `test_hysteresis_*` (أربعة) | ✅ |
| ٢ هدف واعٍ بالتنفيذ | `test_fee_aware_target`, `test_rr_guard` | ✅ |
| ٤ تبريد ما بعد الخروج | `Test07_PostExitCooldown` (سبعة) | ✅ |
| ٦ بوابة جودة البيانات | `test_bad_data_quality_skips_management` | ✅ |
| ١١ تحليلات MFE/MAE | `test_mfe_survives_restart` + تقرير المقارنة | ✅ |
| ١٤ إعادة تشغيل القرار | `test_decision_uses_only_closed_history` | ✅ |
| ١٦ لا زيادة للمخاطرة | `test_widening_stop_is_refused_at_order_layer` | ✅ |

---

## ٢. الاختبارات التي تكشف فعلاً (لا التي تؤكد الوجود)

**`test_break_even_actually_breaks_even`** — لا يفحص أن الدالة تُرجع
رقماً أكبر من الدخول، بل **ينفّذ الخروج** بنموذج التكاليف نفسه ويطالب
بربح صافٍ ≥ ٠، ثم يطالب بأن الطريقة الساذجة **تخسر**. الشرط الثاني
ضروري: بدونه يمرّ الاختبار حتى لو عُطِّل نموذج التكاليف كلّه.

**`test_paper_mode_records_a_decision`** — يُسقِط البناء إن لم يُسجَّل
قرار داخل `tick()` حقيقية. هذا الحارس ضد النمط الذي تكرّر ست مرّات في
هذا المستودع. ويقابله ضابط سالب
(`test_decision_is_skipped_when_adaptive_disabled`) كي لا يمرّ الاختبار
لأن كل شيء يُسجِّل دائماً.

**`test_widening_stop_is_refused_at_order_layer`** — يتجاوز المحرك
ويستدعي طبقة الأوامر مباشرةً بوقف أوسع. الحارس مكرَّر عمداً عند حدود
المال: المحرك قد يُستبدَل، وهذه آخر نقطة قبل أمر حقيقي.

**`test_guard_actually_detects_arithmetic`** — اختبار سالب للحارس
النصّي نفسه. حارس لا يُثبَت أنه يكشف شيئاً ليس حارساً.

**`test_schema_version_tracks_engine`** — انحدار حقيقي وقع أثناء هذا
العمل (النتيجة #٢٢)، ومُقيَّد الآن باختبار.

---

## ٣. ثلاث فرضيات اختبار كانت خاطئة — والكود كان صحيحاً

١. **`test_range_market_reduces_target`** سقط أولاً. السبب لم يكن بقاً:
   هدف ١٫٥٪ مقابل وقف ٢٪ عائد/مخاطرة ٠٫٥٤، وحارس البند ٢٠ يرفضه بحق.
   صُحِّحت الفرضية (وقف ضيّق)، وأُضيف اختباران يوثّقان الحدّ صراحةً:
   `test_wide_stop_blocks_target_reduction` و
   `test_breakeven_stop_unlocks_target_reduction`.

٢. **`test_bad_data_quality_skips_management`** سقط لأن العتبة
   `0.999999` لا تتجاوز درجة جودة ١٫٠ الكاملة. عتبة فوق أي درجة ممكنة.

٣. **`test_same_bar_decision_is_not_duplicated`** افترض «صف واحد مهما
   حدث». القرار الثاني يختلف بحق (التعادل تحقَّق في الدورة الأولى).
   أُعيدت صياغته إلى الثبات الصحيح: **الأثر** لا يتكرّر — لا وقف يتحرك
   مرّتين، ولا صفّان بنفس (الشمعة، القرار).

ولم يُحذف اختبار واحد ولم تُخفَّف عتبة واحدة (البند ٤٠).

---

## ٣ب. ثلاثة بقول في كودي أنا — واحد كشفه اختبار قائم

| # | البق | من كشفه |
|---|---|---|
| ٢٣ | R تُحسب من وقف متحرّك ⇒ موت التتبّع الحي | مراجعة ذاتية للفرق |
| ٢٤ | تذبذب التعادل ⇒ أوامر بلا طائل | قياس ذاتي |
| — | `continue` يتخطّى `equity[i+1]` ⇒ إفساد المنحنى | مراجعة ذاتية |
| ٢٦ | ترحيل يُجهِض ترقية قاعدة v4 | **`test_opportunity_scan_migration.py` القائم** |

الأخير هو الأهم منهجياً: كشفه اختبار **كُتب قبل هذه الجولة** يبني
قاعدة v4 حقيقية. لا يمكن لاختبار أكتبه أنا لميزتي أن يعوّضه، لأنني
أبني قاعدة حديثة بطبيعة الحال. قيمة مجموعة اختبارات متراكمة تظهر هنا
بالضبط: تحرس ما لا يخطر لكاتب الميزة الجديدة أن يحرسه.

## ٤. خطأ في الوصل كشفه الفحص قبل التشغيل

`_manage_open_position()` تستخدم `np.isfinite` و`np.searchsorted`،
و`live_trader.py` **لم يكن يستورد numpy إطلاقاً**. أول تنفيذ حقيقي كان
سيرفع `NameError` داخل دورة تداول. كُشف بفحص `live_trader.np`
صراحةً بعد الوصل، لا بالثقة في أن الاستيراد موجود — وهو بالضبط نوع
الخلل الذي لا يكشفه أي اختبار وحدة لا يمرّ بالمسار الحقيقي.

---

## ٥. ما لم يُختبَر — ويُذكر صراحةً

- **نقل وقف على منصة حقيقية.** لا شبكة ولا مفاتيح.
  `PAPER VERIFIED / LIVE NOT VERIFIED`.
- **سلوك بينانس عند الرفض**: وقف قريب جداً من السعر، حدود المعدّل،
  سباق بين الإلغاء والتنفيذ. محاكاة `PaperBroker` لا تغطّيها.
- **أثر الطبقة على الربحية.** غير مُثبَت، ولم يُدَّع.
