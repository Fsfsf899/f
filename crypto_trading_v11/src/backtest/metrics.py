"""
المقاييس — البند 24.
====================
sqrt(252) لا يُستخدم عشوائياً. عامل التسنيع يُشتق من الإطار الزمني
الفعلي ويُوثَّق في المخرجات.
"""
import numpy as np
from typing import List, Dict, Optional
from ..data.types import INTERVAL_MS

MS_YEAR = 365 * 86_400_000


def bars_per_year(interval: str) -> float:
    """الكريبتو يتداول 24/7 — 365 يوماً لا 252."""
    return MS_YEAR / INTERVAL_MS[interval]


def compute(trades: List[Dict], equity: np.ndarray, initial: float,
            interval: str, gross_profit: float = 0.0, gross_loss: float = 0.0,
            total_fees: float = 0.0, total_slippage: float = 0.0) -> Dict:
    eq = np.asarray(equity, dtype=float)
    n_t = len(trades)
    bpy = bars_per_year(interval)

    peak = np.maximum.accumulate(eq)
    dd = np.where(peak > 0, (peak - eq) / peak * 100, 0.0)
    max_dd = float(dd.max()) if len(dd) else 0.0

    rets = np.diff(eq) / np.maximum(eq[:-1], 1e-12) if len(eq) > 1 else np.array([])
    ann_factor = np.sqrt(bpy)

    def _safe(x):
        return float(x) if np.isfinite(x) else None

    sharpe = sortino = None
    if len(rets) > 2 and np.std(rets) > 0:
        sharpe = _safe(np.mean(rets) / np.std(rets) * ann_factor)
        downside = rets[rets < 0]
        if len(downside) > 1 and np.std(downside) > 0:
            sortino = _safe(np.mean(rets) / np.std(downside) * ann_factor)

    total_return_pct = (eq[-1] - initial) / initial * 100 if initial > 0 else 0.0
    years = len(eq) / bpy if bpy > 0 else 0
    cagr = None
    if years > 0 and eq[-1] > 0 and initial > 0:
        cagr = _safe(((eq[-1] / initial) ** (1 / years) - 1) * 100)
    calmar = _safe(cagr / max_dd) if (cagr is not None and max_dd > 0) else None

    pnls = np.array([t['pnl'] for t in trades]) if n_t else np.array([])
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    gp = float(wins.sum()) if len(wins) else gross_profit
    gl = float(abs(losses.sum())) if len(losses) else gross_loss

    return {
        'total_trades': n_t,
        'winning_trades': int(len(wins)),
        'losing_trades': int(len(losses)),
        'win_rate': round(float(len(wins) / n_t * 100), 2) if n_t else 0.0,
        'gross_profit': round(gp, 2),
        'gross_loss': round(gl, 2),
        'net_profit': round(float(pnls.sum()), 2) if n_t else 0.0,
        'profit_factor': round(gp / gl, 3) if gl > 0 else None,
        'expectancy': round(float(pnls.mean()), 4) if n_t else 0.0,
        'avg_win': round(float(wins.mean()), 2) if len(wins) else 0.0,
        'avg_loss': round(float(losses.mean()), 2) if len(losses) else 0.0,
        'largest_win': round(float(wins.max()), 2) if len(wins) else 0.0,
        'largest_loss': round(float(losses.min()), 2) if len(losses) else 0.0,
        'total_fees': round(total_fees, 2),
        'total_slippage': round(total_slippage, 2),
        'gross_return_pct': round(float(total_return_pct
                                        + (total_fees + total_slippage) / initial * 100), 3),
        'net_return_pct': round(float(total_return_pct), 3),
        'final_equity': round(float(eq[-1]), 2),
        'max_drawdown_pct': round(max_dd, 3),
        'cagr_pct': None if cagr is None else round(cagr, 3),
        'sharpe': None if sharpe is None else round(sharpe, 3),
        'sortino': None if sortino is None else round(sortino, 3),
        'calmar': None if calmar is None else round(calmar, 3),
        'avg_holding_bars': round(float(np.mean([t.get('bars_held', 0) for t in trades])), 1) if n_t else 0.0,
        'annualization': {'interval': interval, 'bars_per_year': round(bpy, 1),
                          'factor': round(float(ann_factor), 3),
                          'basis': '365d/24h crypto'},
    }
