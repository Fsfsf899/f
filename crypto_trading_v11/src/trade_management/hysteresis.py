"""
تثبيت حالة السوق — Part C البند 1.

المشكلة: `regime.detect_at()` يُعيد حكماً لكل شمعة على حدة. قرب أي
عتبة يتذبذب الحكم بين شمعة وأخرى لفرق ضئيل، فلو بُني عليه تحريك
وقف لتحرّك الوقف ذهاباً وإياباً بلا سبب حقيقي.

الحل هنا **عديم الحالة عمداً**: يُشتقّ الحكم المثبَّت من سلسلة
الأحكام الخام وحدها. لا ذاكرة تُفقد عند إعادة التشغيل، ولا فرق بين
الباكتست والتنفيذ الحي ما دامت السلسلة نفسها — وهذا شرط تطابق
البيئتين الذي كسره المشروع أكثر من مرّة سابقاً.

مشية واحدة فقط (`_walk`) تخدم الاستخدامين: الحالة الآن، والحالة عند
كل شمعة. تكرار المنطق في دالتين كان سيسمح لهما بالاختلاف بصمت —
وهو نفس نمط الخلل الذي تكرّر في هذا المشروع أربع مرّات في حساب
الانخفاض الأقصى وحده.
"""
from dataclasses import dataclass
from typing import Iterator, List, Sequence, Tuple
from ..core.config import AdaptiveConfig
from ..market.regime import UNKNOWN


@dataclass
class ConfirmedRegime:
    regime: str
    bars_in_regime: int
    raw: str                 # الحكم الخام لآخر شمعة — للمقارنة والتدقيق
    pending: str = ''        # حالة تتجمّع ولم تُعتمد بعد

    def to_dict(self):
        return dict(self.__dict__)


def _walk(labels: Sequence[str], cfg: AdaptiveConfig
          ) -> Iterator[Tuple[str, int, str]]:
    """
    يُنتج (المعتمَدة، عمرها، المرشَّحة) بعد كل شمعة، بقاعدتين:

      • لا تُعتمد حالة جديدة قبل تكرارها `regime_confirmation_candles`
        شمعة متتالية.
      • الحالة المعتمَدة لا تُستبدَل قبل مرور `regime_min_duration`
        شمعة على اعتمادها، مهما بدا البديل مؤكَّداً.

    السلسلة تُقرأ يساراً→يميناً، فكل حكم يعتمد على ما قبله فقط.
    """
    n_conf = max(1, int(cfg.regime_confirmation_candles))
    min_dur = max(0, int(cfg.regime_min_duration))

    current = UNKNOWN
    bars_in = 0
    run_label = ''
    run_len = 0

    for lab in labels:
        lab = lab or UNKNOWN
        run_len = run_len + 1 if lab == run_label else 1
        run_label = lab

        if current == UNKNOWN:
            # لا حالة معتمدة بعد — أول تأكيد يفوز بلا شرط مدّة
            if run_len >= n_conf:
                current, bars_in = run_label, run_len
            else:
                bars_in += 1
        elif lab == current:
            bars_in += 1
        else:
            bars_in += 1
            if run_len >= n_conf and bars_in >= min_dur:
                current, bars_in = run_label, run_len

        yield current, bars_in, (run_label if run_label != current else '')


def confirm(labels: Sequence[str], cfg: AdaptiveConfig) -> ConfirmedRegime:
    """الحالة المثبَّتة بعد آخر شمعة في السلسلة."""
    if not labels:
        return ConfirmedRegime(UNKNOWN, 0, UNKNOWN)
    cur, bars, pend = UNKNOWN, 0, ''
    for cur, bars, pend in _walk(labels, cfg):
        pass
    return ConfirmedRegime(cur, bars, labels[-1] or UNKNOWN, pending=pend)


def confirm_series(labels: Sequence[str], cfg: AdaptiveConfig) -> List[str]:
    """
    الحالة المثبَّتة عند **كل** شمعة — للباكتست.

    نفس المشية بالضبط، فيستحيل بناءً أن تختلف عن `confirm()`، ولا أن
    تتسرّب شمعة لاحقة إلى حكم سابق.
    """
    return [cur for cur, _, _ in _walk(labels, cfg)]
