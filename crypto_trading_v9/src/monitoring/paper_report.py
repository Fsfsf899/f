"""
تقارير Paper Trading — المرحلة الخامسة.
=======================================
لا يُعرض Win Rate وحده أبداً. عينة أقل من الحد ⇒ تحذير صريح.
"""
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional
import numpy as np

MIN_SAMPLE = 30
WARN = 'INSUFFICIENT SAMPLE — لا يمكن استنتاج الربحية'


def _agg(rows: List[Dict]) -> Dict:
    if not rows:
        return {'n': 0}
    pnl = np.array([r['pnl'] for r in rows if r.get('pnl') is not None], float)
    if len(pnl) == 0:
        return {'n': len(rows)}
    w, l = pnl[pnl > 0], pnl[pnl < 0]
    gp, gl = float(w.sum()), float(abs(l.sum()))
    # التراجع يُقاس على منحنى حقوق يبدأ من رأس مال مرجعي، لا من صفر.
    # القسمة على قمة قريبة من الصفر كانت تُنتج نسباً بالتريليونات.
    base = float(np.abs(pnl).sum()) * 2 + 1.0
    eq = base + np.cumsum(pnl)
    peak = np.maximum.accumulate(eq)
    dd = float((np.maximum(peak - eq, 0) / np.maximum(peak, 1e-9) * 100).max()) \
        if len(eq) else 0.0
    hold = [r['holding_bars'] for r in rows if r.get('holding_bars')]
    mae = [r['mae_pct'] for r in rows if r.get('mae_pct') is not None]
    mfe = [r['mfe_pct'] for r in rows if r.get('mfe_pct') is not None]
    return {
        'n': len(pnl),
        'win_rate': round(float(len(w) / len(pnl) * 100), 2),
        'net_pnl': round(float(pnl.sum()), 4),
        'profit_factor': round(gp / gl, 3) if gl > 0 else None,
        'expectancy': round(float(pnl.mean()), 4),
        'avg_win': round(float(w.mean()), 4) if len(w) else 0.0,
        'avg_loss': round(float(l.mean()), 4) if len(l) else 0.0,
        'max_drawdown_pct': round(dd, 3),
        'avg_holding_bars': round(float(np.mean(hold)), 1) if hold else None,
        'avg_mae_pct': round(float(np.mean(mae)), 3) if mae else None,
        'avg_mfe_pct': round(float(np.mean(mfe)), 3) if mfe else None,
        'reliable': len(pnl) >= MIN_SAMPLE,
    }


class PaperReport:
    def __init__(self, db, environment: str = 'paper'):
        self.db = db
        self.env = environment

    def _closed(self) -> List[Dict]:
        return self.db.query("""
            SELECT r.*, s.stars, s.score, s.market_regime, s.interval, s.bar_time,
                   s.calibrated_probability, s.raw_probability
            FROM recommendations r LEFT JOIN signals s ON r.signal_id = s.id
            WHERE r.outcome IS NOT NULL AND r.acted = 1""")

    def _costs(self) -> Dict:
        """التكاليف الفعلية من التعبئات المسجَّلة، لا من تقدير."""
        fills = self.db.query('SELECT SUM(commission) c, COUNT(*) n FROM fills')
        pos = self.db.query('SELECT SUM(fees) f FROM positions')
        model = self.db.get_kv('paper_costs', {}) or {}
        return {
            'fees_from_fills': round(float((fills[0].get('c') if fills else 0) or 0), 6),
            'n_fills': int((fills[0].get('n') if fills else 0) or 0),
            'fees_from_positions': round(float((pos[0].get('f') if pos else 0) or 0), 6),
            'cost_model': model,
        }

    def full(self) -> Dict:
        rows = self._closed()
        sigs = self.db.query('SELECT * FROM signals')
        blocked: Dict[str, int] = {}
        for s in sigs:
            import json as _j
            try:
                for r in _j.loads(s.get('reasons') or '[]'):
                    blocked[r] = blocked.get(r, 0) + 1
            except Exception:
                pass

        def group(fn):
            b: Dict[str, List[Dict]] = {}
            for r in rows:
                k = fn(r)
                if k is not None:
                    b.setdefault(str(k), []).append(r)
            return {k: _agg(v) for k, v in sorted(b.items())}

        started = self.db.get_kv('paper_started_ts') or self.db.get_kv('run_started_ts')
        days = ((int(time.time() * 1000) - int(started)) / 86_400_000) if started else 0.0

        unknown = len([r for r in self.db.query(
            "SELECT state FROM order_intents") if r['state'] in
            ('UNKNOWN', 'IN_FLIGHT', 'ERROR_REQUIRES_MANUAL_REVIEW')])
        api_err = len(self.db.query(
            "SELECT 1 FROM risk_events WHERE kind LIKE '%API%'"))
        kills = len(self.db.query(
            "SELECT 1 FROM risk_events WHERE kind='KILL_SWITCH'"))
        recon = len(self.db.query(
            "SELECT 1 FROM risk_events WHERE kind='RECONCILIATION_MISMATCH'"))

        out = {
            'environment': self.env,
            'runtime_days': round(days, 2),
            'n_signals': len(sigs),
            'n_buy_signals': len([s for s in sigs if s['decision'] == 'BUY']),
            'n_trades': len(rows),
            'rejected_reasons': dict(sorted(blocked.items(), key=lambda x: -x[1])[:12]),
            'overall': _agg(rows),
            'costs': self._costs(),
            'by_symbol': group(lambda r: r.get('symbol')),
            'by_regime': group(lambda r: r.get('market_regime')),
            'by_hour': group(lambda r: (f"{int((r['bar_time'] // 3600000) % 24):02d}:00"
                                        if r.get('bar_time') else None)),
            'by_exit_reason': group(lambda r: r.get('exit_reason')),
            'by_stars': group(lambda r: r.get('stars')),
            'unknown_orders': unknown,
            'api_errors': api_err,
            'kill_switch_activations': kills,
            'reconciliation_mismatches': recon,
        }
        if out['overall'].get('n', 0) < MIN_SAMPLE:
            out['warning'] = WARN
        return out

    def daily(self) -> List[Dict]:
        rows = self._closed()
        by_day: Dict[str, List[Dict]] = {}
        for r in rows:
            ts = r.get('closed_ts') or r.get('ts')
            if not ts:
                continue
            d = datetime.fromtimestamp(int(ts) / 1000, timezone.utc).strftime('%Y-%m-%d')
            by_day.setdefault(d, []).append(r)
        return [{'day': d, **_agg(v)} for d, v in sorted(by_day.items())]

    def render(self) -> str:
        f = self.full()
        o = f['overall']
        L = ['', '=' * 64, f"  تقرير {f['environment'].upper()} — "
             f"{f['runtime_days']} يوم", '=' * 64]
        L.append(f"  إشارات {f['n_signals']} | BUY {f['n_buy_signals']} | "
                 f"صفقات {f['n_trades']}")
        if o.get('n'):
            L += ['',
                  f"  Net PnL          {o['net_pnl']}",
                  f"  Profit Factor    {o['profit_factor']}",
                  f"  Expectancy       {o['expectancy']}",
                  f"  Win Rate         {o['win_rate']}%   ← لا تُقرأ وحدها",
                  f"  Max Drawdown     {o['max_drawdown_pct']}%",
                  f"  MAE / MFE        {o['avg_mae_pct']} / {o['avg_mfe_pct']}",
                  f"  متوسط الاحتفاظ    {o['avg_holding_bars']} شمعة",
                  f"  رسوم مسجَّلة       {f['costs']['fees_from_fills']}"]
        L += ['',
              f"  أوامر UNKNOWN     {f['unknown_orders']}",
              f"  أخطاء API         {f['api_errors']}",
              f"  Kill Switch       {f['kill_switch_activations']}",
              f"  فروقات المصالحة    {f['reconciliation_mismatches']}"]
        if f['rejected_reasons']:
            L += ['', '  أسباب الرفض:']
            for k, v in list(f['rejected_reasons'].items())[:6]:
                L.append(f"    {k:36s} {v}")
        if f.get('warning'):
            L += ['', f"  ⚠️  {f['warning']}"]
        L.append('=' * 64)
        return '\n'.join(L)
