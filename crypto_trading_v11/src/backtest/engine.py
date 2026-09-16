"""
محرك الباكتست الوحيد — البند 3.
================================
Event-driven: يمرّ على الشموع بالترتيب، ويتخذ القرار بمعلومات تلك
اللحظة فقط. لا حلقة ثانية ولا تمرير للخلف.

ضمانات:
  • الإشارة تُقرأ عند الشمعة i، والتنفيذ عند فتح i+1  (لا look-ahead)
  • الخروج يُكتشف من High/Low عبر execution.py
  • أي مركز مفتوح في النهاية يُغلق صراحةً بسبب BACKTEST_END (البند 5)
  • التكاليف مطبّقة على كل تنفيذ
"""
import numpy as np
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from ..core.config import Config, DEFAULT
from ..data.types import OHLCV
from ..signals.engine import SignalEngine, BUY
from ..risk.position_sizing import PositionSizer
from ..risk.risk_guard import RiskGuard
from .costs import CostModel
from ..account.providers import SimulatedAccountProvider
from .execution import (Bar, resolve_long_exit, STOP_LOSS, TAKE_PROFIT,
                        TRAILING_STOP, BREAK_EVEN, SIGNAL_EXIT, TIME_EXIT,
                        BACKTEST_END)


@dataclass
class Position:
    entry_index: int
    entry_time: int
    entry_price: float
    qty: float
    stop: float
    target: float
    initial_stop: float
    stars: int
    score: float
    entry_fee: float
    entry_slippage: float
    regime: str = 'UNKNOWN'
    raw_probability: Optional[float] = None
    mae: float = 0.0     # أقصى تحرك ضدنا
    mfe: float = 0.0     # أقصى تحرك لصالحنا
    breakeven_done: bool = False
    trailing_active: bool = False   # ميّز عن breakeven — لتصنيف سبب الخروج بدقة


@dataclass
class BacktestResult:
    metrics: Dict[str, Any]
    trades: List[Dict]
    equity: np.ndarray
    signals_evaluated: int
    signals_buy: int
    rejections: Dict[str, int]
    config_fingerprint: str
    strategy_version: str
    symbol: str
    interval: str
    period: Dict[str, Any]
    open_at_end_closed: bool = False


class BacktestEngine:
    """المحرك المركزي. يستخدم نفس SignalEngine المستخدم حياً."""

    def __init__(self, cfg: Optional[Config] = None,
                 signal_engine: Optional[SignalEngine] = None,
                 initial_capital: float = 10_000.0):
        self.cfg = cfg or DEFAULT
        self.engine = signal_engine or SignalEngine(self.cfg)
        self.costs = CostModel(self.cfg.costs)
        self.sizer = PositionSizer(self.cfg.risk, self.costs)
        self.account = SimulatedAccountProvider(self.cfg.risk)
        self.initial = initial_capital

    def run(self, data: OHLCV, *, data_quality: float = 1.0,
            btc: Optional[OHLCV] = None, verbose: bool = False) -> BacktestResult:
        cfg = self.cfg
        n = len(data)
        o, h, l, c = data.open, data.high, data.low, data.close
        prep = self.engine.prepare(data)
        atr_series = prep['ind']['atr']

        guard = RiskGuard(cfg.risk)
        equity = np.full(n + 1, self.initial, dtype=float)
        cash = self.initial
        pos: Optional[Position] = None
        pending: Optional[Dict] = None       # إشارة تنتظر فتح الشمعة التالية
        trades: List[Dict] = []
        rejections: Dict[str, int] = {}
        n_eval = n_buy = 0
        total_fees = total_slip = 0.0
        bars_per_day = max(1, int(86_400_000 / data.interval_ms))

        for i in range(n):
            bar = Bar(float(o[i]), float(h[i]), float(l[i]), float(c[i]), float(data.volume[i]))

            if i % bars_per_day == 0:
                guard.new_day(cash if pos is None else cash + pos.qty * bar.open)

            # ── 1) تنفيذ إشارة معلّقة عند الفتح (لا look-ahead)
            if pending is not None and pos is None:
                sig = pending; pending = None
                atr_v = sig['atr']
                stop_dist = sig['stop_dist']
                target_dist = sig['target_dist']
                # القسم 23: نفس مسار التحجيم الحيّ بالضبط، بحساب محاكى
                # بدل رصيد منصة. بلا هذا كان الباكتست يفتح مراكز أكبر
                # بـ 11.1% (الاحتياطي النقدي غير مُطبَّق) فيُبالغ في
                # العوائد بلا سبب استراتيجي.
                acct = self.account.snapshot(available=cash)
                sized = self.sizer.calculate(
                    equity=cash, entry=bar.open,
                    stop=bar.open - stop_dist,
                    stars=sig['stars'],
                    consecutive_losses=guard.consecutive_losses,
                    account=acct, fee_rate=cfg.costs.taker_fee)
                if sized['qty'] > 0 and sized['notional'] <= cash:
                    vol_mult = max(atr_v / max(bar.open, 1e-9) * 100 / 1.0, 0.5)
                    fill = self.costs.buy(bar.open, sized['qty'], vol_mult)
                    if fill.notional + fill.fee <= cash:
                        cash -= fill.notional + fill.fee
                        total_fees += fill.fee; total_slip += fill.slippage_cost
                        stop = fill.price - stop_dist
                        tgt = fill.price + target_dist
                        pos = Position(i, int(data.open_time[i]), fill.price,
                                       sized['qty'], stop, tgt, stop,
                                       sig['stars'], sig['score'],
                                       fill.fee, fill.slippage_cost,
                                       sig['regime'], sig['raw_probability'])
                        guard.record_open()

            # ── 2) إدارة المركز المفتوح
            if pos is not None:
                pos.mae = min(pos.mae, (bar.low - pos.entry_price) / pos.entry_price * 100)
                pos.mfe = max(pos.mfe, (bar.high - pos.entry_price) / pos.entry_price * 100)

                exit_res = resolve_long_exit(
                    bar, pos.stop, pos.target,
                    policy=cfg.execution.same_candle_policy,
                    allow_gap=cfg.execution.allow_gap_fills)

                # البند 8C: خروج زمني — يُفحص فقط إن لم يُصب الوقف/الهدف
                # هذه الشمعة، ولا يُستخدم إلا بتفعيل صريح (معطَّل افتراضياً)
                if (exit_res is None and cfg.signal.max_holding_bars is not None
                        and (i - pos.entry_index) >= cfg.signal.max_holding_bars):
                    exit_res = (TIME_EXIT, bar.close)

                if exit_res is not None:
                    reason, px = exit_res
                    is_stop = reason == STOP_LOSS
                    if reason != TIME_EXIT:
                        # trailing_active لا يعني فقط breakeven — يميّز
                        # حركة التتبّع الفعلية (إن فُعِّلت) عن حركة
                        # التعادل الأحادية، حتى مع فجوة سعرية تحت نقطة
                        # التعادل. الترتيب هنا يحدد الأولوية عند تعدّدها.
                        if is_stop and pos.trailing_active:
                            reason = TRAILING_STOP
                        elif is_stop and pos.breakeven_done:
                            reason = BREAK_EVEN
                    atr_v = atr_series[i] if np.isfinite(atr_series[i]) else 0.0
                    vol_mult = max(atr_v / max(bar.close, 1e-9) * 100, 0.5)
                    fill = self.costs.sell(px, pos.qty, vol_mult, is_stop=is_stop)
                    cash += fill.notional - fill.fee
                    total_fees += fill.fee; total_slip += fill.slippage_cost
                    trades.append(self._record(pos, i, data, fill, reason, total=cash))
                    guard.record_trade(trades[-1]['pnl'])
                    pos = None
                else:
                    # نقل الوقف لنقطة التعادل بعد 1R
                    r_unit = pos.entry_price - pos.initial_stop
                    if (not pos.breakeven_done and r_unit > 0
                            and bar.high >= pos.entry_price + r_unit):
                        pos.stop = max(pos.stop, pos.entry_price)
                        pos.breakeven_done = True

                    # البند 8B: Trailing Stop — معطَّل افتراضياً. يُحدَّث
                    # الوقف بناءً على إغلاق **هذه** الشمعة فيُستخدَم في فحص
                    # الشمعة **التالية** فقط — لا نستخدم High/Low هذه
                    # الشمعة نفسها لتحريك الوقف ثم اختبار الإصابة بها،
                    # فذلك يكرر منطق نفس الشمعة على نفسها بترتيب غامض.
                    # القاعدة الصارمة: لا يتراجع الوقف أبداً (max فقط).
                    if cfg.signal.trailing_stop_enabled and r_unit > 0:
                        atr_v = atr_series[i] if np.isfinite(atr_series[i]) else 0.0
                        activated = (bar.high >= pos.entry_price
                                    + cfg.signal.trailing_activate_at_r * r_unit)
                        if activated and atr_v > 0:
                            candidate = bar.close - cfg.signal.trailing_atr_mult * atr_v
                            if candidate > pos.stop:
                                pos.stop = candidate
                                pos.trailing_active = True

            # ── 3) تقييم إشارة جديدة (تُنفَّذ الشمعة القادمة)
            if pos is None and pending is None and i < n - 1:
                n_eval += 1
                acc = {'open_positions': 0, 'daily_trades': guard.daily_trades,
                       'daily_loss_hit': guard.daily_loss_hit(cash),
                       'consecutive_losses': guard.consecutive_losses,
                       'exposure_ok': True}
                s = self.engine.evaluate(data, i, data_quality=data_quality,
                                         account_state=acc, prep=prep)
                if s.decision == BUY and s.atr and s.atr > 0:
                    n_buy += 1
                    # نحفظ **مسافة** الوقف والهدف من الإشارة، لا السعر
                    # المطلق — فالتنفيذ الفعلي يقع عند فتح الشمعة التالية
                    # لا عند سعر مرجع الإشارة. حفظ المسافة يحافظ على أثر
                    # stop_method (atr/structure/hybrid) بدل إعادة حساب
                    # وقف ATR بحت هنا كما كان يحدث سابقاً — خطأ كان يُلغي
                    # أي أثر لطريقة الوقف المختارة على نتيجة الباكتست.
                    stop_dist = s.entry - s.stop_loss
                    target_dist = s.take_profit - s.entry
                    pending = {'atr': float(s.atr), 'stars': s.stars,
                               'score': s.score, 'regime': s.regime,
                               'raw_probability': s.raw_probability,
                               'stop_dist': float(stop_dist),
                               'target_dist': float(target_dist)}
                else:
                    for r in s.reasons:
                        rejections[r] = rejections.get(r, 0) + 1

            equity[i + 1] = cash + (pos.qty * bar.close if pos else 0.0)

        # ── 4) البند 5: إغلاق أي مركز مفتوح صراحةً
        closed_at_end = False
        if pos is not None:
            last = Bar(float(o[-1]), float(h[-1]), float(l[-1]), float(c[-1]))
            fill = self.costs.sell(last.close, pos.qty, 1.0, is_stop=False)
            cash += fill.notional - fill.fee
            total_fees += fill.fee; total_slip += fill.slippage_cost
            trades.append(self._record(pos, n - 1, data, fill, BACKTEST_END, total=cash))
            guard.record_trade(trades[-1]['pnl'])
            equity[-1] = cash
            closed_at_end = True
            pos = None

        from . import metrics as M
        m = M.compute(trades, equity, self.initial, data.interval,
                      total_fees=total_fees, total_slippage=total_slip)
        m['breakeven_move_pct'] = round(self.costs.breakeven_move_pct(), 4)

        return BacktestResult(
            metrics=m, trades=trades, equity=equity,
            signals_evaluated=n_eval, signals_buy=n_buy,
            rejections=dict(sorted(rejections.items(), key=lambda x: -x[1])),
            config_fingerprint=cfg.fingerprint(),
            strategy_version=cfg.version,
            symbol=data.symbol, interval=data.interval,
            period={'from_ms': int(data.open_time[0]), 'to_ms': int(data.open_time[-1]),
                    'bars': n},
            open_at_end_closed=closed_at_end)

    @staticmethod
    def _record(pos: Position, exit_i: int, data: OHLCV, fill, reason: str,
                total: float) -> Dict:
        gross = (fill.price - pos.entry_price) * pos.qty
        pnl = gross - pos.entry_fee - fill.fee
        return {
            'entry_index': pos.entry_index, 'exit_index': exit_i,
            'entry_time': pos.entry_time, 'exit_time': int(data.open_time[exit_i]),
            'entry_price': round(pos.entry_price, 8),
            'exit_price': round(fill.price, 8),
            'qty': round(pos.qty, 10),
            'gross_pnl': round(gross, 4),
            'fees': round(pos.entry_fee + fill.fee, 4),
            'slippage': round(pos.entry_slippage + fill.slippage_cost, 4),
            'pnl': round(pnl, 4),
            'pnl_pct': round(pnl / max(pos.entry_price * pos.qty, 1e-9) * 100, 4),
            'exit_reason': reason, 'stars': pos.stars, 'score': pos.score,
            'regime': pos.regime, 'raw_probability': pos.raw_probability,
            'mae_pct': round(pos.mae, 4), 'mfe_pct': round(pos.mfe, 4),
            'bars_held': exit_i - pos.entry_index,
            'equity_after': round(total, 2),
        }
