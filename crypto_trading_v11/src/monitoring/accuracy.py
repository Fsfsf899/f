"""
تتبع دقة التوصيات — البند 26.
==============================
يقيس الأداء الفعلي للتوصيات المنفَّذة، مقسّماً على أبعاد متعددة.

نسبة الفوز وحدها لا تكفي — تُعرض دائماً مع Expectancy و Profit Factor
و MAE/MFE. توصية بفوز 70% ومتوسط خسارة ضِعف متوسط الربح خاسرة.
"""
import numpy as np
from typing import List, Dict, Optional
from ..storage.database import Database


def _stats(rows: List[Dict]) -> Dict:
    if not rows:
        return {'n': 0}
    pnl = np.array([r['pnl'] for r in rows if r.get('pnl') is not None], float)
    if len(pnl) == 0:
        return {'n': len(rows)}
    w, l = pnl[pnl > 0], pnl[pnl < 0]
    gp, gl = float(w.sum()), float(abs(l.sum()))
    mae = [r['mae_pct'] for r in rows if r.get('mae_pct') is not None]
    mfe = [r['mfe_pct'] for r in rows if r.get('mfe_pct') is not None]
    return {
        'n': len(pnl),
        'win_rate': round(float(len(w) / len(pnl) * 100), 2),
        'expectancy': round(float(pnl.mean()), 4),
        'profit_factor': round(gp / gl, 3) if gl > 0 else None,
        'net_pnl': round(float(pnl.sum()), 2),
        'avg_win': round(float(w.mean()), 2) if len(w) else 0.0,
        'avg_loss': round(float(l.mean()), 2) if len(l) else 0.0,
        'max_drawdown_pct': round(_dd(pnl), 3),
        'avg_mae_pct': round(float(np.mean(mae)), 3) if mae else None,
        'avg_mfe_pct': round(float(np.mean(mfe)), 3) if mfe else None,
        'reliable': len(pnl) >= 30,
    }


def _dd(pnl: np.ndarray) -> float:
    eq = np.cumsum(pnl)
    peak = np.maximum.accumulate(np.maximum(eq, 0) + 1e-9)
    return float((np.maximum(peak - eq, 0) / peak * 100).max()) if len(eq) else 0.0


class AccuracyTracker:
    def __init__(self, db: Database):
        self.db = db

    def _closed(self, symbol: Optional[str] = None) -> List[Dict]:
        sql = """SELECT r.*, s.stars, s.score, s.market_regime, s.interval,
                        s.calibrated_probability, s.raw_probability, s.bar_time
                 FROM recommendations r LEFT JOIN signals s ON r.signal_id = s.id
                 WHERE r.outcome IS NOT NULL AND r.acted = 1"""
        p = ()
        if symbol:
            sql += ' AND r.symbol = ?'; p = (symbol,)
        return self.db.query(sql, p)

    def report(self, symbol: Optional[str] = None) -> Dict:
        rows = self._closed(symbol)
        if not rows:
            return {'overall': {'n': 0},
                    'note': 'لا توصيات مغلقة بعد — لا يمكن قياس الدقة'}

        def group(key_fn) -> Dict:
            buckets: Dict[str, List[Dict]] = {}
            for r in rows:
                k = key_fn(r)
                if k is None: continue
                buckets.setdefault(str(k), []).append(r)
            return {k: _stats(v) for k, v in sorted(buckets.items())}

        def prob_bucket(r):
            p = r.get('calibrated_probability') or r.get('raw_probability')
            if p is None: return None
            lo = int(np.clip(p, 0, 0.999) * 5) / 5
            return f'{lo:.1f}-{lo+0.2:.1f}'

        def hour(r):
            t = r.get('bar_time')
            return None if not t else f"{int((t // 3600000) % 24):02d}:00"

        return {
            'overall': _stats(rows),
            'by_symbol': group(lambda r: r.get('symbol')),
            'by_interval': group(lambda r: r.get('interval')),
            'by_regime': group(lambda r: r.get('market_regime')),
            'by_stars': group(lambda r: r.get('stars')),
            'by_probability': group(prob_bucket),
            'by_hour': group(hour),
            'by_exit_reason': group(lambda r: r.get('exit_reason')),
            'warning': ('عينة أقل من 30 — النتائج ضجيج إحصائي'
                        if len(rows) < 30 else None),
        }

    def calibration_check(self) -> Dict:
        """هل الاحتمالات المعروضة تطابق الواقع؟"""
        rows = [r for r in self._closed()
                if r.get('calibrated_probability') is not None]
        if len(rows) < 30:
            return {'n': len(rows), 'status': 'INSUFFICIENT_SAMPLE'}
        p = np.array([r['calibrated_probability'] for r in rows], float)
        y = np.array([1.0 if r['pnl'] > 0 else 0.0 for r in rows], float)
        from ..validation.calibration import brier, reliability_table
        rel, err = reliability_table(p, y)
        return {'n': len(rows), 'brier': round(brier(p, y), 4),
                'calibration_error': round(err, 4), 'reliability': rel,
                'status': 'OK' if err < 0.10 else 'MISCALIBRATED'}
