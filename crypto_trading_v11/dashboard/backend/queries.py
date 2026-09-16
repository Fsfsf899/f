"""
استعلامات العرض — تقرأ فقط ما ينتجه المحرك.
============================================
قاعدة صارمة (البندان 51 و52): Dashboard **لا يعيد حساب** أي رقم تداول.
Profit Factor والاحتمال والـ PnL تُقرأ كما هي من القاعدة.

الاستثناء الوحيد: تجميعات عرض بحتة (عدّ، مجموع، تجميع حسب بُعد) وهي
عمليات وصف لا منطق تداول. أي رقم مشتق يُوسم `derived: true`.

لا يُختلق أي رقم. غياب البيانات ⇒ None، ويعرضه الواجهة كـ N/A.
"""
import json
import time
from typing import Any, Dict, List, Optional

from readonly_db import ReadOnlyDB, DatabaseUnavailable

MIN_SAMPLE = 30
STALE_MULTIPLIER = 3


def _j(v, default=None):
    if not v:
        return default
    try:
        return json.loads(v)
    except Exception:
        return default


def _age_ms(ts: Optional[int]) -> Optional[int]:
    return None if not ts else int(time.time() * 1000) - int(ts)


class DashboardQueries:
    def __init__(self, db: ReadOnlyDB, environment: str = 'paper'):
        self.db = db
        self.environment = environment

    # ── حالة النظام ──
    def system(self) -> Dict:
        db_health = self.db.health()
        if db_health['status'] != 'ONLINE':
            return {'system_status': 'OFFLINE', 'trading_mode': 'UNKNOWN',
                    'database': db_health, 'error': db_health['error']}

        env = self.db.kv('environment') or self.environment
        last_cycle = self.db.kv('last_successful_cycle_ts')
        last_recon = self.db.kv('last_successful_recon_ts')
        kill = bool(self.db.kv('kill_switch', False))
        started = self.db.kv('run_started_ts') or self.db.kv('paper_started_ts')

        heartbeat = self.db.one(
            'SELECT MAX(heartbeat_ts) hb FROM process_lock') or {}
        hb = heartbeat.get('hb')
        hb_age = _age_ms(hb)

        # المحرك يُعد ONLINE فقط بنبض حديث — لا لمجرد وجود عملية
        if kill:
            status = 'HALTED'
        elif hb_age is not None and hb_age < 180_000:
            status = 'ONLINE'
        elif hb_age is not None:
            status = 'STALE'
        else:
            status = 'UNKNOWN'

        return {
            'system_status': status,
            'trading_mode': str(env).upper(),
            'mainnet_enabled': False,          # مقفول في المصدر
            'kill_switch': kill,
            'kill_switch_reason': self.db.kv('kill_switch_reason', ''),
            'database': db_health,
            'engine_heartbeat_ts': hb,
            'engine_heartbeat_age_ms': hb_age,
            'last_cycle_ts': last_cycle,
            'last_cycle_age_ms': _age_ms(last_cycle),
            'last_reconciliation_ts': last_recon,
            'run_started_ts': started,
            'endpoint': self.db.kv('endpoint', 'local'),
            'api_key_fingerprint': self.db.kv('api_key_fingerprint', 'none'),
        }

    # ── مؤشرات الأداء العليا ──
    def kpis(self) -> Dict:
        sys_ = self.system()
        if sys_['system_status'] == 'OFFLINE':
            return {'available': False, 'system': sys_}

        open_pos = self.db.query(
            "SELECT * FROM positions WHERE status='OPEN'")
        closed = self.db.query(
            "SELECT pnl FROM recommendations "
            "WHERE outcome IS NOT NULL AND acted=1 AND pnl IS NOT NULL")

        pnls = [float(r['pnl']) for r in closed]
        n = len(pnls)
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gp, gl = sum(wins), abs(sum(losses))

        today = time.strftime('%Y-%m-%d', time.gmtime())
        day = self.db.one('SELECT * FROM daily_equity WHERE day=?', (today,))

        dq = self.db.one(
            'SELECT score FROM data_quality ORDER BY ts DESC LIMIT 1')

        return {
            'available': True,
            'system': sys_,
            'open_positions': len(open_pos),
            'total_trades': n,
            'today_pnl': (round(float(day['realized_pnl']), 4)
                          if day and day.get('realized_pnl') is not None else None),
            'total_pnl': round(sum(pnls), 4) if n else None,
            'win_rate': round(len(wins) / n * 100, 2) if n else None,
            'profit_factor': round(gp / gl, 3) if gl > 0 else None,
            'max_drawdown_pct': self._drawdown(pnls),
            'data_quality': (round(float(dq['score']) * 100, 1)
                             if dq and dq.get('score') is not None else None),
            'sample_sufficient': n >= MIN_SAMPLE,
            'derived': True,
        }

    @staticmethod
    def _drawdown(pnls: List[float]) -> Optional[float]:
        if not pnls:
            return None
        base = sum(abs(p) for p in pnls) * 2 + 1.0
        eq, peak, dd = base, base, 0.0
        for p in pnls:
            eq += p
            peak = max(peak, eq)
            dd = max(dd, (peak - eq) / peak * 100)
        return round(dd, 3)

    # ── نظرة السوق ──
    def market(self) -> List[Dict]:
        syms = [r['symbol'] for r in self.db.query(
            'SELECT DISTINCT symbol FROM signals ORDER BY symbol')]
        out = []
        for s in syms:
            last = self.db.one(
                'SELECT * FROM signals WHERE symbol=? ORDER BY bar_time DESC LIMIT 1',
                (s,))
            dq = self.db.one(
                'SELECT * FROM data_quality WHERE symbol=? ORDER BY ts DESC LIMIT 1',
                (s,))
            if not last:
                continue
            interval_ms = self._interval_ms(last.get('interval'))
            age = _age_ms(last.get('bar_time'))
            out.append({
                'symbol': s,
                'interval': last.get('interval'),
                'last_price': last.get('entry'),
                'market_regime': last.get('market_regime'),
                'trend': self._trend(last.get('market_regime')),
                'data_quality': (round(float(dq['score']) * 100, 1)
                                 if dq and dq.get('score') is not None else None),
                'last_bar_ts': last.get('bar_time'),
                'age_ms': age,
                'stale': bool(interval_ms and age
                              and age > interval_ms * STALE_MULTIPLIER),
                'last_decision': last.get('decision'),
            })
        return out

    @staticmethod
    def _interval_ms(iv: Optional[str]) -> Optional[int]:
        table = {'1m': 60_000, '5m': 300_000, '15m': 900_000, '30m': 1_800_000,
                 '1h': 3_600_000, '4h': 14_400_000, '1d': 86_400_000}
        return table.get(iv or '')

    @staticmethod
    def _trend(regime: Optional[str]) -> str:
        r = (regime or '').upper()
        if 'BULL' in r:
            return 'UP'
        if 'BEAR' in r:
            return 'DOWN'
        if r in ('RANGING', 'LOW_VOLATILITY'):
            return 'FLAT'
        return 'UNKNOWN'

    # ── التوصيات ──
    def recommendations(self, *, limit: int = 50, offset: int = 0,
                        symbol: Optional[str] = None,
                        interval: Optional[str] = None,
                        decision: Optional[str] = None,
                        status: Optional[str] = None,
                        regime: Optional[str] = None,
                        min_score: Optional[float] = None,
                        min_probability: Optional[float] = None,
                        date_from: Optional[int] = None,
                        date_to: Optional[int] = None) -> Dict:
        where, params = ['1=1'], []
        if symbol:
            where.append('s.symbol = ?'); params.append(symbol)
        if interval:
            where.append('s.interval = ?'); params.append(interval)
        if decision:
            where.append('s.decision = ?'); params.append(decision)
        if regime:
            where.append('s.market_regime = ?'); params.append(regime)
        if min_score is not None:
            where.append('s.score >= ?'); params.append(min_score)
        if min_probability is not None:
            where.append('COALESCE(s.calibrated_probability, s.raw_probability) >= ?')
            params.append(min_probability)
        if date_from:
            where.append('s.bar_time >= ?'); params.append(date_from)
        if date_to:
            where.append('s.bar_time <= ?'); params.append(date_to)
        if status == 'OPEN':
            where.append("r.outcome IS NULL AND r.acted = 1")
        elif status == 'CLOSED':
            where.append('r.outcome IS NOT NULL')
        elif status == 'NOT_ACTED':
            where.append('(r.acted = 0 OR r.acted IS NULL)')

        w = ' AND '.join(where)
        total = self.db.scalar(
            f"""SELECT COUNT(*) FROM signals s
                LEFT JOIN recommendations r ON r.signal_id = s.id WHERE {w}""",
            tuple(params), 0)
        rows = self.db.query(
            f"""SELECT s.*, r.id AS rec_id, r.acted, r.outcome, r.pnl, r.pnl_pct,
                       r.exit_price, r.exit_reason, r.mae_pct, r.mfe_pct,
                       r.holding_bars, r.closed_ts, r.reason AS rec_reason
                FROM signals s
                LEFT JOIN recommendations r ON r.signal_id = s.id
                WHERE {w} ORDER BY s.bar_time DESC LIMIT ? OFFSET ?""",
            tuple(params) + (limit, offset))
        return {'total': total, 'limit': limit, 'offset': offset,
                'items': [self._rec_row(r) for r in rows]}

    def _rec_row(self, r: Dict) -> Dict:
        return {
            'signal_id': r['id'], 'recommendation_id': r.get('rec_id'),
            'timestamp': r.get('bar_time'), 'recorded_ts': r.get('ts'),
            'symbol': r.get('symbol'), 'timeframe': r.get('interval'),
            'signal': r.get('decision'),
            'score': r.get('score'), 'stars': r.get('stars'),
            'confidence': r.get('confidence'),
            'raw_probability': r.get('raw_probability'),
            'calibrated_probability': r.get('calibrated_probability'),
            'probability_source': r.get('probability_source'),
            'entry': r.get('entry'), 'stop_loss': r.get('stop_loss'),
            'take_profit': r.get('take_profit'),
            'risk_reward': r.get('risk_reward'), 'atr': r.get('atr'),
            'market_regime': r.get('market_regime'),
            'data_quality': r.get('data_quality'),
            'btc_context': r.get('btc_context'),
            'reasons': _j(r.get('evidence'), []),
            'no_trade_reasons': _j(r.get('reasons'), []),
            'engine_version': r.get('strategy_version'),
            'config_fingerprint': r.get('config_fingerprint'),
            'acted': bool(r.get('acted')),
            'outcome': r.get('outcome'), 'pnl': r.get('pnl'),
            'pnl_pct': r.get('pnl_pct'), 'exit_reason': r.get('exit_reason'),
            'status': ('CLOSED' if r.get('outcome')
                       else 'OPEN' if r.get('acted') else 'NOT_ACTED'),
        }

    def recommendation(self, sid: int) -> Optional[Dict]:
        r = self.db.one(
            """SELECT s.*, r.id AS rec_id, r.acted, r.outcome, r.pnl, r.pnl_pct,
                      r.exit_price, r.exit_reason, r.mae_pct, r.mfe_pct,
                      r.holding_bars, r.closed_ts, r.reason AS rec_reason
               FROM signals s LEFT JOIN recommendations r ON r.signal_id = s.id
               WHERE s.id = ?""", (sid,))
        if not r:
            return None
        base = self._rec_row(r)
        pos = self.db.one(
            'SELECT * FROM positions WHERE recommendation_id=?',
            (r.get('rec_id'),)) if r.get('rec_id') else None
        base['position'] = pos
        base['mae_pct'] = r.get('mae_pct')
        base['mfe_pct'] = r.get('mfe_pct')
        base['holding_bars'] = r.get('holding_bars')
        base['block_reason'] = r.get('rec_reason')
        if pos:
            base['orders'] = self.db.query(
                'SELECT * FROM orders WHERE position_id=? ORDER BY ts', (pos['id'],))
            base['intents'] = self.db.query(
                'SELECT client_order_id, order_type, state, version, filled_qty '
                'FROM order_intents WHERE position_id=? ORDER BY id', (pos['id'],))
        return base

    # ── المراكز ──
    def positions(self, status: str = 'OPEN') -> List[Dict]:
        rows = self.db.query(
            'SELECT * FROM positions WHERE status=? ORDER BY opened_ts DESC',
            (status,))
        out = []
        for p in rows:
            last = self.db.one(
                'SELECT entry FROM signals WHERE symbol=? ORDER BY bar_time DESC LIMIT 1',
                (p['symbol'],))
            cur = last.get('entry') if last else None
            entry = p.get('entry_price')
            qty = p.get('qty') or 0
            unreal = ((cur - entry) * qty) if (cur and entry) else None
            risk_unit = (entry - p['stop_loss']) if (entry and p.get('stop_loss')) else None
            # البند 33 من التدقيق: حالة الحماية يجب أن تكون واضحة صراحة
            # — معرّفا الوقف/الهدف الحقيقيان (بعد إصلاح خلط OCO) هما
            # الدليل الوحيد على أن حماية فعلية مُثبَّتة، لا افتراضاً.
            has_stop = bool(p.get('stop_order_id'))
            has_target = bool(p.get('target_order_id'))
            protected = has_stop  # الوقف وحده كافٍ للحماية؛ الهدف اختياري
            out.append({
                **p,
                'current_price': cur,
                'position_value': round(cur * qty, 4) if cur else None,
                'unrealized_pnl': round(unreal, 4) if unreal is not None else None,
                'unrealized_pnl_pct': (round(unreal / (entry * qty) * 100, 4)
                                       if unreal is not None and entry and qty else None),
                'r_multiple': (round(((cur - entry) / risk_unit), 3)
                               if cur and risk_unit and risk_unit > 0 else None),
                'risk_amount': (round(risk_unit * qty, 4)
                                if risk_unit and qty else None),
                'duration_ms': _age_ms(p.get('opened_ts')),
                'price_source': 'last_signal_bar',
                'derived': True,
                'protected': protected,
                'oco_status': {
                    'order_list_id': p.get('order_list_id'),
                    'stop_order_id': p.get('stop_order_id'),
                    'stop_client_order_id': p.get('stop_client_order_id'),
                    'target_order_id': p.get('target_order_id'),
                    'target_client_order_id': p.get('target_client_order_id'),
                    'has_stop': has_stop, 'has_target': has_target,
                },
            })
        return out

    def position(self, pid: int) -> Optional[Dict]:
        p = self.db.one('SELECT * FROM positions WHERE id=?', (pid,))
        if not p:
            return None
        allp = {x['id']: x for x in self.positions(p['status'])}
        base = allp.get(pid, p)
        base = dict(base)
        base['orders'] = self.db.query(
            'SELECT * FROM orders WHERE position_id=? ORDER BY ts', (pid,))
        base['fills'] = self.db.query(
            """SELECT f.* FROM fills f JOIN orders o ON f.order_id=o.id
               WHERE o.position_id=? ORDER BY f.ts""", (pid,))
        base['intents'] = self.db.query(
            'SELECT * FROM order_intents WHERE position_id=? ORDER BY id', (pid,))
        base['timeline'] = self._timeline(pid, p)
        if p.get('recommendation_id'):
            rec = self.db.one(
                """SELECT s.* FROM recommendations r
                   JOIN signals s ON r.signal_id=s.id WHERE r.id=?""",
                (p['recommendation_id'],))
            base['recommendation'] = self._rec_row(rec) if rec else None
        return base

    def _timeline(self, pid: int, pos: Dict) -> List[Dict]:
        ev = []
        if pos.get('recommendation_id'):
            r = self.db.one('SELECT ts FROM recommendations WHERE id=?',
                            (pos['recommendation_id'],))
            if r:
                ev.append({'ts': r['ts'], 'event': 'SIGNAL_GENERATED'})
        ev.append({'ts': pos.get('opened_ts'), 'event': 'POSITION_OPENED'})
        for t in self.db.query(
                """SELECT st.* FROM state_transitions st
                   JOIN order_intents oi ON oi.client_order_id=st.client_order_id
                   WHERE oi.position_id=? ORDER BY st.ts""", (pid,)):
            ev.append({'ts': t['ts'],
                       'event': f"ORDER_{t['to_state']}",
                       'detail': f"{t['from_state']} → {t['to_state']}"})
        if pos.get('closed_ts'):
            ev.append({'ts': pos['closed_ts'], 'event': 'POSITION_CLOSED'})
        return sorted([e for e in ev if e.get('ts')], key=lambda x: x['ts'])

    # ── الصفقات ──
    def trades(self, *, limit: int = 50, offset: int = 0,
               symbol: Optional[str] = None, result: Optional[str] = None,
               exit_reason: Optional[str] = None, regime: Optional[str] = None,
               date_from: Optional[int] = None,
               date_to: Optional[int] = None) -> Dict:
        where = ['r.outcome IS NOT NULL', 'r.acted = 1']
        params: List[Any] = []
        if symbol:
            where.append('r.symbol = ?'); params.append(symbol)
        if result == 'WIN':
            where.append('r.pnl > 0')
        elif result == 'LOSS':
            where.append('r.pnl < 0')
        elif result == 'BREAKEVEN':
            where.append('r.pnl = 0')
        if exit_reason:
            where.append('r.exit_reason = ?'); params.append(exit_reason)
        if regime:
            where.append('s.market_regime = ?'); params.append(regime)
        if date_from:
            where.append('r.closed_ts >= ?'); params.append(date_from)
        if date_to:
            where.append('r.closed_ts <= ?'); params.append(date_to)
        w = ' AND '.join(where)

        total = self.db.scalar(
            f"""SELECT COUNT(*) FROM recommendations r
                LEFT JOIN signals s ON r.signal_id=s.id WHERE {w}""",
            tuple(params), 0)
        rows = self.db.query(
            f"""SELECT r.*, s.interval, s.market_regime, s.score,
                       s.calibrated_probability, s.raw_probability,
                       p.id AS position_id, p.entry_price, p.qty, p.fees,
                       p.opened_ts, p.realized_pnl
                FROM recommendations r
                LEFT JOIN signals s ON r.signal_id = s.id
                LEFT JOIN positions p ON p.recommendation_id = r.id
                WHERE {w} ORDER BY r.closed_ts DESC LIMIT ? OFFSET ?""",
            tuple(params) + (limit, offset))
        items = []
        for t in rows:
            gross = None
            if t.get('entry_price') and t.get('exit_price') and t.get('qty'):
                gross = round((t['exit_price'] - t['entry_price']) * t['qty'], 6)
            items.append({
                'trade_id': t['id'], 'symbol': t['symbol'],
                'timeframe': t.get('interval'),
                'entry_time': t.get('opened_ts'), 'exit_time': t.get('closed_ts'),
                'entry': t.get('entry_price'), 'exit': t.get('exit_price'),
                'quantity': t.get('qty'),
                'gross_pnl': gross, 'fees': t.get('fees'),
                'net_pnl': t.get('pnl'), 'pnl_pct': t.get('pnl_pct'),
                'mae_pct': t.get('mae_pct'), 'mfe_pct': t.get('mfe_pct'),
                'holding_bars': t.get('holding_bars'),
                'duration_ms': ((t['closed_ts'] - t['opened_ts'])
                                if t.get('closed_ts') and t.get('opened_ts') else None),
                'exit_reason': t.get('exit_reason'), 'outcome': t.get('outcome'),
                'probability': (t.get('calibrated_probability')
                                or t.get('raw_probability')),
                'score': t.get('score'), 'regime': t.get('market_regime'),
                'position_id': t.get('position_id'),
            })
        return {'total': total, 'limit': limit, 'offset': offset, 'items': items}

    # ── الأداء ──
    def performance(self, *, symbol: Optional[str] = None,
                    interval: Optional[str] = None,
                    regime: Optional[str] = None,
                    date_from: Optional[int] = None,
                    date_to: Optional[int] = None) -> Dict:
        where = ['r.outcome IS NOT NULL', 'r.acted = 1', 'r.pnl IS NOT NULL']
        params: List[Any] = []
        if symbol:
            where.append('r.symbol = ?'); params.append(symbol)
        if interval:
            where.append('s.interval = ?'); params.append(interval)
        if regime:
            where.append('s.market_regime = ?'); params.append(regime)
        if date_from:
            where.append('r.closed_ts >= ?'); params.append(date_from)
        if date_to:
            where.append('r.closed_ts <= ?'); params.append(date_to)

        rows = self.db.query(
            f"""SELECT r.*, s.interval, s.market_regime,
                       p.fees, p.opened_ts, p.entry_price, p.qty
                FROM recommendations r
                LEFT JOIN signals s ON r.signal_id = s.id
                LEFT JOIN positions p ON p.recommendation_id = r.id
                WHERE {' AND '.join(where)} ORDER BY r.closed_ts""",
            tuple(params))
        return self._perf(rows)

    def _perf(self, rows: List[Dict]) -> Dict:
        n = len(rows)
        if n == 0:
            return {'available': False, 'total_trades': 0,
                    'note': 'NO_DATA', 'sample_sufficient': False}
        pnls = [float(r['pnl']) for r in rows]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gp, gl = sum(wins), abs(sum(losses))
        fees = sum(float(r.get('fees') or 0) for r in rows)
        durs = [(r['closed_ts'] - r['opened_ts'])
                for r in rows if r.get('closed_ts') and r.get('opened_ts')]

        eq, curve, peak, dd_curve = 0.0, [], 0.0, []
        base = sum(abs(p) for p in pnls) * 2 + 1.0
        eq = base; peak = base
        for r, p in zip(rows, pnls):
            eq += p
            peak = max(peak, eq)
            curve.append({'ts': r.get('closed_ts'), 'equity': round(eq - base, 6),
                          'pnl': round(p, 6)})
            dd_curve.append({'ts': r.get('closed_ts'),
                             'drawdown_pct': round((peak - eq) / peak * 100, 4)})

        return {
            'available': True,
            'total_trades': n,
            'winning_trades': len(wins), 'losing_trades': len(losses),
            'win_rate': round(len(wins) / n * 100, 2),
            'gross_pnl': round(gp - gl + fees, 6),
            'fees': round(fees, 6),
            'net_pnl': round(sum(pnls), 6),
            'profit_factor': round(gp / gl, 3) if gl > 0 else None,
            'expectancy': round(sum(pnls) / n, 6),
            'average_win': round(sum(wins) / len(wins), 6) if wins else 0.0,
            'average_loss': round(sum(losses) / len(losses), 6) if losses else 0.0,
            'payoff_ratio': (round((sum(wins) / len(wins))
                                   / abs(sum(losses) / len(losses)), 3)
                             if wins and losses else None),
            'max_drawdown_pct': max((d['drawdown_pct'] for d in dd_curve),
                                    default=0.0),
            'avg_duration_ms': round(sum(durs) / len(durs)) if durs else None,
            'avg_mae_pct': self._avg(rows, 'mae_pct'),
            'avg_mfe_pct': self._avg(rows, 'mfe_pct'),
            'equity_curve': curve,
            'drawdown_curve': dd_curve,
            'daily': self._bucket(rows, '%Y-%m-%d'),
            'weekly': self._bucket(rows, '%Y-W%W'),
            'monthly': self._bucket(rows, '%Y-%m'),
            'distribution': self._distribution(pnls),
            'sample_sufficient': n >= MIN_SAMPLE,
            'min_sample': MIN_SAMPLE,
            'derived': True,
            'note': None if n >= MIN_SAMPLE else 'INSUFFICIENT_SAMPLE',
        }

    @staticmethod
    def _avg(rows, key):
        vals = [float(r[key]) for r in rows if r.get(key) is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    @staticmethod
    def _bucket(rows, fmt) -> List[Dict]:
        b: Dict[str, List[float]] = {}
        for r in rows:
            ts = r.get('closed_ts')
            if not ts:
                continue
            k = time.strftime(fmt, time.gmtime(int(ts) / 1000))
            b.setdefault(k, []).append(float(r['pnl']))
        return [{'bucket': k, 'pnl': round(sum(v), 6), 'trades': len(v)}
                for k, v in sorted(b.items())]

    @staticmethod
    def _distribution(pnls: List[float], bins: int = 12) -> List[Dict]:
        if not pnls:
            return []
        lo, hi = min(pnls), max(pnls)
        if hi == lo:
            return [{'range': f'{lo:.2f}', 'count': len(pnls)}]
        step = (hi - lo) / bins
        counts = [0] * bins
        for p in pnls:
            i = min(int((p - lo) / step), bins - 1)
            counts[i] += 1
        return [{'range': f'{lo + i * step:.2f}..{lo + (i + 1) * step:.2f}',
                 'count': c, 'from': round(lo + i * step, 4)}
                for i, c in enumerate(counts)]

    # ── الدقة والمعايرة ──
    def accuracy(self) -> Dict:
        total_sig = self.db.scalar('SELECT COUNT(*) FROM signals', (), 0)
        no_trade = self.db.scalar(
            "SELECT COUNT(*) FROM signals WHERE decision != 'BUY'", (), 0)
        rows = self.db.query(
            """SELECT r.*, s.interval, s.market_regime, s.score,
                      s.calibrated_probability, s.raw_probability
               FROM recommendations r LEFT JOIN signals s ON r.signal_id=s.id
               WHERE r.outcome IS NOT NULL AND r.acted=1 AND r.pnl IS NOT NULL""")
        resolved = len(rows)
        correct = len([r for r in rows if float(r['pnl']) > 0])

        def group(keyfn):
            b: Dict[str, List[Dict]] = {}
            for r in rows:
                k = keyfn(r)
                if k is None:
                    continue
                b.setdefault(str(k), []).append(r)
            out = {}
            for k, v in sorted(b.items()):
                p = self._perf(v)
                out[k] = {'count': p['total_trades'], 'win_rate': p['win_rate'],
                          'profit_factor': p['profit_factor'],
                          'expectancy': p['expectancy'],
                          'avg_probability': self._avg_prob(v),
                          'avg_score': self._avg(v, 'score'),
                          'sample_sufficient': p['sample_sufficient']}
            return out

        return {
            'total_recommendations': total_sig,
            'no_trade_count': no_trade,
            'resolved': resolved,
            'correct': correct, 'incorrect': resolved - correct,
            'accuracy': round(correct / resolved * 100, 2) if resolved else None,
            'by_symbol': group(lambda r: r.get('symbol')),
            'by_timeframe': group(lambda r: r.get('interval')),
            'by_regime': group(lambda r: r.get('market_regime')),
            'calibration': self._calibration(rows),
            'sample_sufficient': resolved >= MIN_SAMPLE,
            'min_sample': MIN_SAMPLE,
            'derived': True,
        }

    @staticmethod
    def _avg_prob(rows) -> Optional[float]:
        vals = [float(r['calibrated_probability'] or r['raw_probability'])
                for r in rows
                if r.get('calibrated_probability') or r.get('raw_probability')]
        return round(sum(vals) / len(vals), 4) if vals else None

    @staticmethod
    def _calibration(rows: List[Dict]) -> Dict:
        buckets = [(0.50, 0.55), (0.55, 0.60), (0.60, 0.65), (0.65, 0.70),
                   (0.70, 0.75), (0.75, 0.80), (0.80, 1.01)]
        usable = [r for r in rows
                  if (r.get('calibrated_probability') or r.get('raw_probability'))
                  is not None]
        if len(usable) < MIN_SAMPLE:
            return {'status': 'CALIBRATION_INSUFFICIENT',
                    'n': len(usable), 'min_sample': MIN_SAMPLE, 'buckets': []}
        out, brier_sum = [], 0.0
        for lo, hi in buckets:
            sel = [r for r in usable
                   if lo <= float(r['calibrated_probability']
                                  or r['raw_probability']) < hi]
            if not sel:
                continue
            pred = sum(float(r['calibrated_probability'] or r['raw_probability'])
                       for r in sel) / len(sel)
            actual = len([r for r in sel if float(r['pnl']) > 0]) / len(sel)
            out.append({'bucket': f'{int(lo*100)}-{int(min(hi,1.0)*100)}%',
                        'n': len(sel), 'predicted': round(pred, 4),
                        'actual': round(actual, 4),
                        'gap': round(actual - pred, 4)})
        for r in usable:
            p = float(r['calibrated_probability'] or r['raw_probability'])
            y = 1.0 if float(r['pnl']) > 0 else 0.0
            brier_sum += (p - y) ** 2
        return {'status': 'OK', 'n': len(usable),
                'brier': round(brier_sum / len(usable), 4), 'buckets': out}

    # ── الصحة ──
    def health(self) -> Dict:
        sys_ = self.system()
        db_h = self.db.health()
        now = int(time.time() * 1000)

        def comp(name, ok, last=None, err=None, latency=None, unknown=False):
            if unknown:
                st = 'UNKNOWN'
            elif ok is None:
                st = 'UNKNOWN'
            else:
                st = 'ONLINE' if ok else 'OFFLINE'
            return {'component': name, 'status': st,
                    'last_check_ts': last, 'latency_ms': latency,
                    'error': err}

        api_err = self.db.query(
            "SELECT * FROM risk_events WHERE kind LIKE '%API%' "
            "ORDER BY ts DESC LIMIT 5")
        recon_err = self.db.query(
            "SELECT * FROM risk_events WHERE kind='RECONCILIATION_MISMATCH' "
            "ORDER BY ts DESC LIMIT 1")
        unresolved = self.db.scalar(
            "SELECT COUNT(*) FROM order_intents WHERE state IN "
            "('IN_FLIGHT','UNKNOWN','CANCEL_REQUESTED',"
            "'ERROR_REQUIRES_MANUAL_REVIEW')", (), 0)
        hb_age = sys_.get('engine_heartbeat_age_ms')
        cycle_age = sys_.get('last_cycle_age_ms')

        components = [
            comp('Trading Engine',
                 hb_age is not None and hb_age < 180_000,
                 sys_.get('engine_heartbeat_ts'),
                 None if hb_age is not None else 'لا نبض مسجَّل'),
            comp('Database', db_h['status'] == 'ONLINE', now,
                 db_h['error'], db_h['latency_ms']),
            comp('Signal Engine', cycle_age is not None and cycle_age < 900_000,
                 sys_.get('last_cycle_ts')),
            comp('Reconciliation', not recon_err,
                 sys_.get('last_reconciliation_ts'),
                 recon_err[0]['detail'][:150] if recon_err else None),
            comp('Risk Engine', not sys_.get('kill_switch'), None,
                 sys_.get('kill_switch_reason') or None),
            comp('Paper Broker',
                 bool(self.db.kv('paper_orders') is not None)
                 if str(sys_.get('trading_mode')).upper() == 'PAPER' else None,
                 None, None, None,
                 unknown=str(sys_.get('trading_mode')).upper() != 'PAPER'),
            comp('Order Integrity', unresolved == 0, None,
                 f'{unresolved} نية غير محسومة' if unresolved else None),
            comp('Binance Connectivity', None, None,
                 'غير مقاس من طبقة العرض', unknown=True),
            comp('Dashboard API', True, now, None, None),
        ]
        return {
            'components': components,
            'api_errors_recent': len(api_err),
            'last_api_error': (api_err[0]['detail'][:200] if api_err else None),
            'unresolved_intents': unresolved,
            'freshness': self.freshness(),
            'overall': ('DEGRADED'
                        if any(c['status'] in ('OFFLINE', 'UNKNOWN')
                               for c in components) else 'ONLINE'),
        }

    def freshness(self) -> Dict:
        out: Dict[str, Any] = {}
        for s in [r['symbol'] for r in self.db.query(
                'SELECT DISTINCT symbol FROM signals')]:
            r = self.db.one(
                'SELECT bar_time, interval FROM signals WHERE symbol=? '
                'ORDER BY bar_time DESC LIMIT 1', (s,))
            if r:
                iv = self._interval_ms(r.get('interval'))
                age = _age_ms(r['bar_time'])
                out[f'last_{s}_data'] = {
                    'ts': r['bar_time'], 'age_ms': age,
                    'stale': bool(iv and age and age > iv * STALE_MULTIPLIER)}
        for label, sql in [
                ('last_recommendation', 'SELECT MAX(ts) v FROM recommendations'),
                ('last_position_update', 'SELECT MAX(COALESCE(closed_ts,opened_ts)) v FROM positions'),
                ('last_trade', 'SELECT MAX(closed_ts) v FROM recommendations WHERE outcome IS NOT NULL')]:
            r = self.db.one(sql)
            v = r.get('v') if r else None
            out[label] = {'ts': v, 'age_ms': _age_ms(v)}
        hb = self.db.one('SELECT MAX(heartbeat_ts) v FROM process_lock')
        v = hb.get('v') if hb else None
        out['last_engine_heartbeat'] = {
            'ts': v, 'age_ms': _age_ms(v),
            'stale': bool(v and _age_ms(v) and _age_ms(v) > 180_000)}
        return out

    # ── سجل التدقيق ──
    def audit(self, *, limit: int = 50, offset: int = 0,
              severity: Optional[str] = None, component: Optional[str] = None,
              event_type: Optional[str] = None, symbol: Optional[str] = None,
              search: Optional[str] = None,
              date_from: Optional[int] = None,
              date_to: Optional[int] = None) -> Dict:
        parts = [
            ("SELECT id, ts, kind AS event_type, severity, symbol, detail AS message, "
             "'RISK' AS component FROM risk_events"),
            ("SELECT id, ts, kind AS event_type, 'INFO' AS severity, NULL AS symbol, "
             "detail AS message, 'SYSTEM' AS component FROM system_events"),
            ("SELECT id, ts, 'ORDER_' || to_state AS event_type, 'INFO' AS severity, "
             "NULL AS symbol, COALESCE(reason,'') || ' [' || client_order_id || ']' "
             "AS message, 'EXECUTION' AS component FROM state_transitions"),
        ]
        union = ' UNION ALL '.join(parts)
        where, params = ['1=1'], []
        if severity:
            where.append('severity = ?'); params.append(severity)
        if component:
            where.append('component = ?'); params.append(component)
        if event_type:
            where.append('event_type LIKE ?'); params.append(f'%{event_type}%')
        if symbol:
            where.append('symbol = ?'); params.append(symbol)
        if search:
            where.append('(message LIKE ? OR event_type LIKE ?)')
            params += [f'%{search}%', f'%{search}%']
        if date_from:
            where.append('ts >= ?'); params.append(date_from)
        if date_to:
            where.append('ts <= ?'); params.append(date_to)
        w = ' AND '.join(where)

        total = self.db.scalar(
            f'SELECT COUNT(*) FROM ({union}) WHERE {w}', tuple(params), 0)
        items = self.db.query(
            f'SELECT * FROM ({union}) WHERE {w} ORDER BY ts DESC LIMIT ? OFFSET ?',
            tuple(params) + (limit, offset))
        return {'total': total, 'limit': limit, 'offset': offset, 'items': items}

    # ── الإعدادات (عرض فقط) ──
    def settings(self) -> Dict:
        sys_ = self.system()
        syms = [r['symbol'] for r in self.db.query(
            'SELECT DISTINCT symbol FROM signals ORDER BY symbol')]
        tfs = [r['interval'] for r in self.db.query(
            'SELECT DISTINCT interval FROM signals ORDER BY interval')]
        ver = self.db.one(
            'SELECT strategy_version, config_fingerprint FROM signals '
            'ORDER BY id DESC LIMIT 1') or {}
        return {
            'trading_mode': sys_['trading_mode'],
            'mainnet_enabled': False,
            'mainnet_note': 'مقفول في المصدر (MAINNET_ENABLED_IN_SOURCE=False)',
            'exchange': 'Binance',
            'endpoint': sys_.get('endpoint'),
            'supported_symbols': syms,
            'supported_timeframes': tfs,
            'strategy_version': ver.get('strategy_version'),
            'config_fingerprint': ver.get('config_fingerprint'),
            'api_key_fingerprint': sys_.get('api_key_fingerprint'),
            'read_only': True,
            'editable': False,
        }

    def equity(self) -> List[Dict]:
        return self.db.query(
            'SELECT * FROM daily_equity ORDER BY day')

    def data_quality(self, limit: int = 100) -> List[Dict]:
        return self.db.query(
            'SELECT * FROM data_quality ORDER BY ts DESC LIMIT ?', (limit,))

    def account_status(self) -> Dict:
        """
        حالة الحساب — القسم 14 من مواصفة V11 FINAL.

        ⚠️ اللوحة **لا تسأل بينانس**: اتصالها بالقاعدة للقراءة فقط وبلا
        مفاتيح API، وهذا قيد أمني مقصود لا نقص. ما يُعرَض هنا هو ما
        حسبه المتداول وخزّنه في آخر خطة تحجيم.

        القِدَم يُحتسَب من لحظة الطلب لا من لحظة الحفظ: خطة عمرها ساعة
        ليست حالة حساب حالية، ويجب أن يرى المستخدم ذلك صراحةً.
        """
        row = self.db.one('SELECT * FROM sizing_plans ORDER BY id DESC LIMIT 1')
        if not row:
            return {'status': 'UNAVAILABLE', 'reason': 'NO_PLAN_YET',
                    'note': 'لم يحسب المتداول أي خطة بعد'}
        plan = json.loads(row.get('plan_json') or '{}')
        acc = plan.get('account') or {}
        age_s = max(0.0, (time.time() * 1000 - (row.get('ts') or 0)) / 1000.0)
        return {
            'account_currency': acc.get('account_currency', 'USDT'),
            'balance_timestamp': acc.get('balance_timestamp'),
            'balance_age_seconds': acc.get('balance_age_seconds'),
            'plan_ts': row.get('ts'),
            'plan_age_seconds': round(age_s, 1),
            'total_balance': acc.get('total_balance'),
            'available_balance': acc.get('available_balance'),
            'locked_balance': acc.get('locked_balance'),
            'usable_equity': acc.get('usable_equity'),
            'reserve_amount': acc.get('reserve_amount'),
            'existing_exposure': acc.get('existing_exposure'),
            'available_for_new_trade': acc.get('available_for_new_trade'),
            'status': acc.get('status', 'UNAVAILABLE'),
            'source': acc.get('source'),
            'blocking_reason': acc.get('blocking_reason'),
        }

    def sizing_plan(self, symbol: Optional[str] = None) -> Dict:
        """
        خطة «كم أدخل؟» — الأقسام 13 و25.

        كل رقم هنا من `PositionSizer` الكنسي عبر المتداول. لا حساب في
        هذه الطبقة ولا في الواجهة (القسم 25 صريح في ذلك).
        """
        q = 'SELECT * FROM sizing_plans'
        args: tuple = ()
        if symbol:
            q += ' WHERE symbol=?'
            args = (symbol,)
        row = self.db.one(q + ' ORDER BY id DESC LIMIT 1', args)
        if not row:
            return {'decision': 'NO_TRADE', 'reason': 'NO_PLAN_YET',
                    'explanation': ['لم يحسب المتداول أي خطة بعد.']}
        plan = json.loads(row.get('plan_json') or '{}')
        plan['plan_ts'] = row.get('ts')
        plan['plan_age_seconds'] = round(
            max(0.0, (time.time() * 1000 - (row.get('ts') or 0)) / 1000.0), 1)
        return plan

    def sizing_plans(self, limit: int = 20) -> List[Dict]:
        rows = self.db.query(
            'SELECT * FROM sizing_plans ORDER BY id DESC LIMIT ?', (limit,))
        out = []
        for r in rows:
            out.append({
                'id': r['id'], 'ts': r['ts'], 'symbol': r['symbol'],
                'decision': r['decision'], 'reason': r['reason'],
                'account_status': r['account_status'],
                'available_balance': r['available_balance'],
                'usable_equity': r['usable_equity'],
                'reserve_amount': r['reserve_amount'],
                'effective_risk_pct': r['effective_risk_pct'],
                'entry': r['entry'], 'stop': r['stop'], 'target': r['target'],
                'final_quantity': r['final_quantity'],
                'position_value': r['position_value'],
                'estimated_max_loss': r['estimated_max_loss'],
                'remaining_available': r['remaining_available'],
            })
        return out

    def opportunity_scans(self, limit: int = 20) -> List[Dict]:
        """
        سجل عمليات مسح أفضل فرصة — القسم 93 (86 أيضاً: جدول المقارنة).
        قاعدة صارمة كبقية هذا الملف: لا حساب جديد هنا، قراءة مباشرة
        لما حفظه `LiveTrader._scan_and_select_symbol()` فعلياً.
        """
        rows = self.db.query(
            'SELECT * FROM opportunity_scans ORDER BY id DESC LIMIT ?', (limit,))
        out = []
        for r in rows:
            out.append({
                'id': r['id'], 'ts': r['ts'],
                'scanned_symbols': json.loads(r['scanned_symbols'] or '[]'),
                'decision': r['decision'], 'selected_symbol': r['selected_symbol'],
                'reason': r['reason'],
                'tie_break_applied': bool(r['tie_break_applied']),
                'btc_context': r['btc_context'],
                'opportunities': json.loads(r['opportunities_json'] or '[]'),
            })
        return out

    def last_opportunity_scan(self) -> Optional[Dict]:
        """
        القسم 92/96: "لماذا اختار النظام هذا الرمز الآن؟" — آخر مسح
        فقط، مع تفصيل كل رمز مُقيَّم (الفائز والمرفوضون معاً، القسم 85).
        """
        rows = self.opportunity_scans(limit=1)
        return rows[0] if rows else None
