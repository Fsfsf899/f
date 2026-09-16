"""
مقارنة Paper بالباكتست — البند 26.
==================================
الباكتست يفترض نموذج تنفيذ. الورقي يقيسه على أسعار حقيقية.
فجوة كبيرة بينهما تعني أن نموذج التكلفة خاطئ، وأن كل نتائج
الباكتست مبنية على افتراض لا يصمد.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional
import numpy as np

MATCH = 'EXECUTION_MODEL_MATCH'
MISMATCH = 'EXECUTION_MODEL_MISMATCH'
INSUFFICIENT = 'INSUFFICIENT_SAMPLE'


@dataclass
class GapResult:
    status: str
    comparisons: List[Dict] = field(default_factory=list)
    n_backtest: int = 0
    n_paper: int = 0
    note: str = ''

    def to_dict(self) -> Dict:
        return {'status': self.status, 'comparisons': self.comparisons,
                'n_backtest': self.n_backtest, 'n_paper': self.n_paper,
                'note': self.note}

    def render(self) -> str:
        L = ['', '=' * 66, '  الباكتست مقابل الورقي', '=' * 66,
             f"  {'المقياس':<22}{'باكتست':>12}{'ورقي':>12}{'الفرق':>12}{'':>4}"]
        for c in self.comparisons:
            flag = '⚠️' if c['exceeds_tolerance'] else ''
            L.append(f"  {c['metric']:<22}{str(c['backtest']):>12}"
                     f"{str(c['paper']):>12}{str(c['diff_pct']):>12} {flag}")
        L += ['', f"  الحالة: {self.status}", f"  {self.note}", '=' * 66]
        return '\n'.join(L)


TOLERANCE = {'win_rate': 25.0, 'profit_factor': 40.0, 'expectancy': 50.0,
             'avg_fee_per_trade': 30.0, 'avg_slippage_per_trade': 60.0,
             'max_drawdown_pct': 50.0}
MIN_SAMPLE = 30


def _pct_diff(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None:
        return None
    if abs(a) < 1e-12:
        return None if abs(b) < 1e-12 else float('inf')
    return round((b - a) / abs(a) * 100, 2)


def compare(backtest_metrics: Dict, paper_report: Dict) -> GapResult:
    bt, pp = backtest_metrics, (paper_report or {}).get('overall', {})
    n_bt = bt.get('total_trades', 0)
    n_pp = pp.get('n', 0)

    def add(metric, a, b, out):
        d = _pct_diff(a, b)
        tol = TOLERANCE.get(metric)
        out.append({'metric': metric, 'backtest': a, 'paper': b,
                    'diff_pct': d,
                    'exceeds_tolerance': bool(
                        d is not None and tol is not None and abs(d) > tol)})

    rows: List[Dict] = []
    add('win_rate', bt.get('win_rate'), pp.get('win_rate'), rows)
    add('profit_factor', bt.get('profit_factor'), pp.get('profit_factor'), rows)
    add('expectancy', bt.get('expectancy'), pp.get('expectancy'), rows)
    add('max_drawdown_pct', bt.get('max_drawdown_pct'),
        pp.get('max_drawdown_pct'), rows)

    bt_fee = (bt.get('total_fees', 0) / n_bt) if n_bt else None
    pp_fee = ((paper_report or {}).get('costs', {}).get('fees_from_fills', 0) / n_pp
              if n_pp else None)
    add('avg_fee_per_trade', round(bt_fee, 6) if bt_fee else None,
        round(pp_fee, 6) if pp_fee else None, rows)

    bt_slip = (bt.get('total_slippage', 0) / n_bt) if n_bt else None
    add('avg_slippage_per_trade', round(bt_slip, 6) if bt_slip else None,
        None, rows)

    if n_bt < MIN_SAMPLE or n_pp < MIN_SAMPLE:
        return GapResult(INSUFFICIENT, rows, n_bt, n_pp,
                         f'عينة غير كافية (باكتست {n_bt}, ورقي {n_pp}، '
                         f'المطلوب {MIN_SAMPLE} لكل منهما)')

    breaches = [c['metric'] for c in rows if c['exceeds_tolerance']]
    if breaches:
        return GapResult(MISMATCH, rows, n_bt, n_pp,
                         f"نموذج التنفيذ لا يطابق الواقع في: "
                         f"{', '.join(breaches)} — نتائج الباكتست مشكوك فيها")
    return GapResult(MATCH, rows, n_bt, n_pp,
                     'نموذج التنفيذ متسق مع القياس الورقي ضمن الحدود')
