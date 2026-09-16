"""
وضع الظل — المرحلة الحادية عشرة.
=================================
يسجّل ما **كان سيحدث** بلا أي تنفيذ، ثم يقيس لاحقاً الفرق بين
السعر المتوقَّع والسعر الفعلي في السوق.

الغرض: فصل جودة الإشارة عن تأثير التنفيذ. لو كانت الإشارة سيئة،
لا فائدة من تحسين التنفيذ.
"""
import time
from typing import Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL, environment TEXT NOT NULL,
  symbol TEXT NOT NULL, interval TEXT NOT NULL, bar_time INTEGER NOT NULL,
  decision TEXT NOT NULL, signal_id INTEGER,
  expected_entry REAL, expected_qty REAL, expected_notional REAL,
  expected_stop REAL, expected_target REAL, expected_rr REAL,
  score REAL, stars INTEGER, regime TEXT, data_quality REAL,
  block_reasons TEXT,
  actual_price_1 REAL, actual_price_5 REAL, actual_price_20 REAL,
  slippage_estimate_bps REAL, evaluated_ts INTEGER,
  UNIQUE(symbol, interval, bar_time, environment)
);
CREATE INDEX IF NOT EXISTS ix_shadow_eval ON shadow_signals(evaluated_ts);
"""


class ShadowRecorder:
    def __init__(self, db, environment: str = 'shadow'):
        self.db = db
        self.env = environment
        self.db.execute_script(SCHEMA)

    def record(self, sig, sized: Optional[Dict] = None,
               signal_id: Optional[int] = None) -> int:
        return self.db.insert_ignore('shadow_signals', {
            'ts': int(time.time() * 1000), 'environment': self.env,
            'symbol': sig.symbol, 'interval': sig.interval,
            'bar_time': sig.timestamp, 'decision': sig.decision,
            'signal_id': signal_id,
            'expected_entry': sig.entry, 'expected_stop': sig.stop_loss,
            'expected_target': sig.take_profit, 'expected_rr': sig.risk_reward,
            'expected_qty': (sized or {}).get('qty'),
            'expected_notional': (sized or {}).get('notional'),
            'score': sig.score, 'stars': sig.stars, 'regime': sig.regime,
            'data_quality': sig.data_quality,
            'block_reasons': ';'.join(sig.reasons)})

    def evaluate(self, data) -> int:
        """
        يقيس الفرق بين السعر المتوقَّع والفعلي بعد 1 و5 و20 شمعة.
        يُستخدم فقط لتقدير أثر التنفيذ، لا لاتخاذ أي قرار.
        """
        import numpy as np
        rows = self.db.query(
            """SELECT * FROM shadow_signals
               WHERE evaluated_ts IS NULL AND decision='BUY'
                 AND symbol=? AND interval=? ORDER BY bar_time""",
            (data.symbol, data.interval))
        if not rows:
            return 0
        times = np.asarray(data.open_time)
        n = 0
        for r in rows:
            idx = int(np.searchsorted(times, r['bar_time']))
            if idx >= len(times) or times[idx] != r['bar_time']:
                continue
            if idx + 20 >= len(data):
                continue        # لم تنضج بعد
            p1 = float(data.open[idx + 1])
            p5 = float(data.close[min(idx + 5, len(data) - 1)])
            p20 = float(data.close[min(idx + 20, len(data) - 1)])
            exp = r['expected_entry'] or 0.0
            slip = ((p1 - exp) / exp * 10000) if exp > 0 else None
            self.db.execute(
                """UPDATE shadow_signals SET actual_price_1=?, actual_price_5=?,
                   actual_price_20=?, slippage_estimate_bps=?, evaluated_ts=?
                   WHERE id=?""",
                (p1, p5, p20, slip, int(time.time() * 1000), r['id']))
            n += 1
        return n

    def report(self) -> Dict:
        import numpy as np
        rows = self.db.query('SELECT * FROM shadow_signals')
        if not rows:
            return {'n_signals': 0, 'note': 'لا إشارات مسجَّلة'}
        buys = [r for r in rows if r['decision'] == 'BUY']
        ev = [r for r in buys if r['evaluated_ts']]
        blocks: Dict[str, int] = {}
        for r in rows:
            for b in (r['block_reasons'] or '').split(';'):
                if b:
                    blocks[b] = blocks.get(b, 0) + 1
        out = {'n_signals': len(rows), 'n_buy': len(buys),
               'n_evaluated': len(ev),
               'block_reasons': dict(sorted(blocks.items(), key=lambda x: -x[1])[:10])}
        if ev:
            slips = [r['slippage_estimate_bps'] for r in ev
                     if r['slippage_estimate_bps'] is not None]
            fwd = [(r['actual_price_20'] - r['expected_entry']) / r['expected_entry'] * 100
                   for r in ev if r['expected_entry']]
            if slips:
                out['avg_entry_slippage_bps'] = round(float(np.mean(slips)), 2)
            if fwd:
                out['avg_20bar_move_pct'] = round(float(np.mean(fwd)), 3)
                out['pct_positive_20bar'] = round(
                    float(np.mean([1.0 if x > 0 else 0.0 for x in fwd]) * 100), 1)
        if len(ev) < 30:
            out['warning'] = 'INSUFFICIENT SAMPLE — لا يمكن استنتاج الربحية'
        return out
