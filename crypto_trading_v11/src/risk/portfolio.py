"""
مخاطر المحفظة والارتباط — البند 19.
====================================
BTC و ETH و SOL تتحرك معاً في أغلب الأوقات. ثلاث صفقات فيها ليست
ثلاث مخاطر مستقلة — هي مخاطرة واحدة بحجم ثلاثي.

الحساب سببي بالكامل: مصفوفة الارتباط تُبنى من عوائد حتى اللحظة t فقط.
"""
import numpy as np
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class PortfolioConfig:
    max_total_exposure_pct: float = 60.0      # % من الحقوق
    max_per_asset_pct: float = 25.0
    max_cluster_exposure_pct: float = 35.0    # مجموعة مترابطة
    correlation_threshold: float = 0.70       # فوقها = نفس العنقود
    min_periods: int = 60                     # أقل عدد عوائد للثقة
    lookback: int = 200


@dataclass
class ExposureResult:
    allowed: bool
    total_exposure_pct: float = 0.0
    per_asset_pct: Dict[str, float] = field(default_factory=dict)
    clusters: List[List[str]] = field(default_factory=list)
    cluster_exposure_pct: Dict[str, float] = field(default_factory=dict)
    correlation_available: bool = False
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {k: (round(v, 4) if isinstance(v, float) else v)
                for k, v in self.__dict__.items()}


def returns_from_closes(closes: np.ndarray) -> np.ndarray:
    c = np.asarray(closes, dtype=float)
    if len(c) < 2:
        return np.array([])
    with np.errstate(divide='ignore', invalid='ignore'):
        r = np.diff(c) / c[:-1]
    return r[np.isfinite(r)]


def correlation_matrix(series: Dict[str, np.ndarray],
                       cfg: Optional[PortfolioConfig] = None
                       ) -> Tuple[Optional[np.ndarray], List[str], str]:
    """
    مصفوفة ارتباط من عوائد محاذاة زمنياً.
    يُرجع (المصفوفة، الرموز، ملاحظة). None عند نقص البيانات — لا تقدير.
    """
    cfg = cfg or PortfolioConfig()
    syms = sorted(series)
    if len(syms) < 2:
        return None, syms, 'رمز واحد — لا ارتباط'

    rets = {s: returns_from_closes(series[s])[-cfg.lookback:] for s in syms}
    n = min(len(r) for r in rets.values())
    if n < cfg.min_periods:
        return None, syms, f'عوائد غير كافية ({n} < {cfg.min_periods})'

    mat = np.vstack([rets[s][-n:] for s in syms])
    if np.any(np.std(mat, axis=1) == 0):
        return None, syms, 'سلسلة ثابتة — الارتباط غير معرَّف'
    with np.errstate(invalid='ignore'):
        corr = np.corrcoef(mat)
    if not np.all(np.isfinite(corr)):
        return None, syms, 'ارتباط غير محدود'
    return corr, syms, f'{n} عائد'


def cluster_by_correlation(corr: np.ndarray, syms: List[str],
                           threshold: float) -> List[List[str]]:
    """تجميع بالوصل: أي رمزين ارتباطهما فوق العتبة في نفس العنقود."""
    n = len(syms)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if abs(corr[i, j]) >= threshold:
                a, b = find(i), find(j)
                if a != b:
                    parent[a] = b

    groups: Dict[int, List[str]] = {}
    for i, s in enumerate(syms):
        groups.setdefault(find(i), []).append(s)
    return [sorted(g) for g in groups.values()]


class PortfolioRisk:
    def __init__(self, cfg: Optional[PortfolioConfig] = None):
        self.cfg = cfg or PortfolioConfig()

    def evaluate(self, *, equity: float,
                 open_notional: Dict[str, float],
                 new_symbol: Optional[str] = None,
                 new_notional: float = 0.0,
                 price_series: Optional[Dict[str, np.ndarray]] = None
                 ) -> ExposureResult:
        """
        يقرّر هل يُسمح بمركز جديد بعد احتساب الارتباط.
        بلا بيانات ارتباط: كل رمز عنقود مستقل، ويُسجَّل ذلك صراحةً
        (لا نفترض استقلالاً ولا نخترع ارتباطاً).
        """
        cfg = self.cfg
        if equity <= 0:
            return ExposureResult(False, reasons=['حقوق غير صالحة'])

        book = dict(open_notional)
        if new_symbol:
            book[new_symbol] = book.get(new_symbol, 0.0) + new_notional

        per = {s: v / equity * 100 for s, v in book.items() if v > 0}
        total = sum(per.values())
        res = ExposureResult(True, total, per)

        if total > cfg.max_total_exposure_pct:
            res.allowed = False
            res.reasons.append(
                f'TOTAL_EXPOSURE {total:.1f}% > {cfg.max_total_exposure_pct}%')

        for s, p in per.items():
            if p > cfg.max_per_asset_pct:
                res.allowed = False
                res.reasons.append(
                    f'ASSET_EXPOSURE {s} {p:.1f}% > {cfg.max_per_asset_pct}%')

        corr, syms, note = (correlation_matrix(price_series, cfg)
                            if price_series else (None, sorted(book), 'لا سلاسل'))
        if corr is not None:
            res.correlation_available = True
            clusters = cluster_by_correlation(corr, syms, cfg.correlation_threshold)
        else:
            clusters = [[s] for s in sorted(book)]
            res.reasons.append(f'CORRELATION_UNKNOWN ({note})')

        res.clusters = clusters
        for cl in clusters:
            exp = sum(per.get(s, 0.0) for s in cl)
            key = '+'.join(cl)
            res.cluster_exposure_pct[key] = round(exp, 4)
            if len(cl) > 1 and exp > cfg.max_cluster_exposure_pct:
                res.allowed = False
                res.reasons.append(
                    f'CLUSTER_EXPOSURE {key} {exp:.1f}% > '
                    f'{cfg.max_cluster_exposure_pct}%')
        return res
