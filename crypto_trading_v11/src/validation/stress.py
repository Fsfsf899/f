"""
اختبار الإجهاد — البند 35.
==========================
حافة تنهار عند +25% تكاليف ليست حافة — هي ضجيج فوق عتبة التكلفة.

يشغّل نفس الباكتست بنماذج تكلفة متصاعدة ويقيس متى تختفي الربحية.
لا يغيّر الاستراتيجية ولا المعاملات.
"""
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Dict, List, Optional
import numpy as np

FRAGILE = 'FRAGILE_EDGE'
ROBUST = 'ROBUST_TO_COSTS'
NO_EDGE = 'NO_EDGE_AT_BASE'


@dataclass
class StressScenario:
    name: str
    cost_multiplier: float = 1.0
    slippage_multiplier: float = 1.0
    spread_multiplier: float = 1.0


DEFAULT_SCENARIOS = [
    StressScenario('base', 1.0, 1.0, 1.0),
    StressScenario('costs_+25%', 1.25, 1.0, 1.0),
    StressScenario('costs_+50%', 1.50, 1.0, 1.0),
    StressScenario('slippage_2x', 1.0, 2.0, 1.0),
    StressScenario('spread_2x', 1.0, 1.0, 2.0),
    StressScenario('all_adverse', 1.50, 2.0, 2.0),
]


@dataclass
class StressResult:
    scenarios: List[Dict] = field(default_factory=list)
    verdict: str = NO_EDGE
    note: str = ''
    breakeven_multiplier: Optional[float] = None

    def to_dict(self) -> Dict:
        return {'scenarios': self.scenarios, 'verdict': self.verdict,
                'note': self.note,
                'breakeven_multiplier': self.breakeven_multiplier}

    def render(self) -> str:
        L = ['', '=' * 68, '  اختبار الإجهاد على التكاليف', '=' * 68,
             f"  {'السيناريو':<16}{'صفقات':>7}{'PF':>8}{'صافي %':>10}"
             f"{'تراجع %':>10}{'توقع':>10}"]
        for s in self.scenarios:
            L.append(f"  {s['name']:<16}{s['trades']:>7}"
                     f"{str(s['profit_factor']):>8}{s['net_return_pct']:>10}"
                     f"{s['max_drawdown_pct']:>10}{s['expectancy']:>10}")
        L += ['', f"  الحكم: {self.verdict}", f"  {self.note}", '=' * 68]
        return '\n'.join(L)


def apply(cfg, sc: StressScenario):
    c = deepcopy(cfg)
    c.costs.maker_fee *= sc.cost_multiplier
    c.costs.taker_fee *= sc.cost_multiplier
    c.costs.slippage_bps_entry *= sc.slippage_multiplier
    c.costs.slippage_bps_exit *= sc.slippage_multiplier
    c.costs.slippage_bps_stop *= sc.slippage_multiplier
    c.costs.spread_bps *= sc.spread_multiplier
    return c


def run(data, base_cfg, capital: float = 10_000.0,
        scenarios: Optional[List[StressScenario]] = None,
        data_quality: float = 0.95) -> StressResult:
    from ..backtest.engine import BacktestEngine
    from ..signals.engine import SignalEngine

    scenarios = scenarios or DEFAULT_SCENARIOS
    rows: List[Dict] = []
    for sc in scenarios:
        cfg = apply(base_cfg, sc)
        m = BacktestEngine(cfg, SignalEngine(cfg), capital).run(
            data, data_quality=data_quality).metrics
        rows.append({
            'name': sc.name, 'trades': m['total_trades'],
            'profit_factor': m['profit_factor'],
            'net_return_pct': m['net_return_pct'],
            'max_drawdown_pct': m['max_drawdown_pct'],
            'expectancy': m['expectancy'],
            'win_rate': m['win_rate'], 'fees': m['total_fees'],
            'slippage': m['total_slippage']})

    base = rows[0]
    base_pf = base['profit_factor']
    res = StressResult(rows)

    if base['trades'] < 30:
        res.verdict = NO_EDGE
        res.note = (f"عينة غير كافية ({base['trades']} صفقة) — "
                    f"لا يمكن الحكم على المتانة")
        return res

    if base_pf is None or base_pf < 1.0:
        res.verdict = NO_EDGE
        res.note = 'لا حافة عند التكاليف الأساسية أصلاً — الإجهاد بلا معنى'
        return res

    survivors = [r for r in rows[1:]
                 if r['profit_factor'] is not None and r['profit_factor'] >= 1.0]
    if len(survivors) == len(rows) - 1:
        res.verdict = ROBUST
        res.note = 'الحافة تصمد في كل السيناريوهات المختبَرة'
    else:
        dead = [r['name'] for r in rows[1:] if r not in survivors]
        res.verdict = FRAGILE
        res.note = f"تنهار عند: {', '.join(dead)}"

    for r in rows:
        if r['name'].startswith('costs_') and (
                r['profit_factor'] is None or r['profit_factor'] < 1.0):
            res.breakeven_multiplier = float(
                r['name'].split('+')[1].rstrip('%')) / 100 + 1
            break
    return res
