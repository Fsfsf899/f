"""
البوت الحي — يربط الإشارات بحساب بينانس
==========================================
ثلاثة أوضاع:
  paper   — يحسب الصفقات محلياً، لا يرسل أوامر (افتراضي)
  testnet — أوامر حقيقية بأموال وهمية على testnet.binance.vision
  live    — 🔴 أموال حقيقية

الوضع live مقفول خلف بوابة تحقق. اقرأ VALIDATION_GATE أدناه.
"""

import json, os, time, sys
from datetime import datetime
import numpy as np

from binance_api import BinanceClient, BinanceError, diagnose
from final_engine import FinalSignalEngine, FinalBacktest, WalkForward, ema, rsi, atr, adx
from execution_engine import CostModel, PositionSizer, RiskGuard
from market_structure import MarketRegime

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, 'bot_state.json')


# ═══════════════════════════════════════════════
class ValidationGate:
    """
    الشروط التي يجب أن تتحقق قبل السماح بالمال الحقيقي.
    ليست تعجيزية — هي الحد الأدنى الذي يفصل الاستراتيجية عن القمار.
    """
    MIN_TRADES = 100          # أقل من هذا = ضجيج إحصائي
    MIN_PROFIT_FACTOR = 1.3   # 1.0 = تعادل؛ الهامش للأمان
    MIN_CONSISTENCY = 0.75    # 3 نوافذ رابحة من 4

    @staticmethod
    def check(symbol, interval, days=730):
        """يشغّل باكتست على بيانات حقيقية ويحكم"""
        import real_data
        raw = real_data.get_data(symbol, interval, days)
        o = np.array(raw['open']); h = np.array(raw['high'])
        l = np.array(raw['low']);  c = np.array(raw['close'])
        v = np.array(raw['volume'])

        bt = FinalBacktest(10000, cost_model=CostModel(), engine=FinalSignalEngine())
        st = bt.run(o, h, l, c, v)
        wf = WalkForward.run(o, h, l, c, v, n_windows=4)

        n = st.get('total_trades', 0)
        pf = st.get('profit_factor') or 0
        cons = wf.get('consistency', 0)

        checks = [
            (n >= ValidationGate.MIN_TRADES,
             f"عدد الصفقات {n} (المطلوب ≥{ValidationGate.MIN_TRADES})"),
            (pf >= ValidationGate.MIN_PROFIT_FACTOR,
             f"Profit Factor {pf} (المطلوب ≥{ValidationGate.MIN_PROFIT_FACTOR})"),
            (cons >= ValidationGate.MIN_CONSISTENCY,
             f"ثبات Walk-Forward {cons} (المطلوب ≥{ValidationGate.MIN_CONSISTENCY})"),
        ]
        passed = all(ok for ok, _ in checks)

        print(f"\n{'='*54}\n  بوابة التحقق — {symbol} {interval}\n{'='*54}")
        for ok, msg in checks:
            print(f"  {'✅' if ok else '❌'} {msg}")
        print(f"\n  النتيجة: {'✅ مسموح' if passed else '❌ مرفوض'}")
        if not passed:
            print("  الاستراتيجية لم تثبت ربحيتها. تشغيلها بمال حقيقي")
            print("  ليس مخاطرة محسوبة — هو خسارة متوقعة.")
        print('='*54)
        return passed, {'stats': st, 'wf': wf}


# ═══════════════════════════════════════════════
class LiveBot:
    def __init__(self, api_key='', api_secret='', mode='paper',
                 symbol='BTCUSDT', interval='4h',
                 capital=1000, risk_pct=1.0,
                 max_position_usd=100,
                 place_stop_on_exchange=True):

        assert mode in ('paper', 'testnet', 'live'), 'وضع غير صالح'
        self.mode = mode
        self.symbol = symbol
        self.interval = interval
        self.capital = capital
        self.max_position_usd = max_position_usd
        self.place_stop_on_exchange = place_stop_on_exchange

        self.engine = FinalSignalEngine()
        self.sizer = PositionSizer(base_risk_pct=risk_pct, max_risk_pct=risk_pct*1.5)
        self.guard = RiskGuard(daily_loss_limit_pct=3.0, max_concurrent=1,
                               max_consecutive_losses=4, max_daily_trades=5)
        self.cost = CostModel()

        self.client = None
        if mode in ('testnet', 'live'):
            self.client = BinanceClient(api_key, api_secret, testnet=(mode == 'testnet'))
            self.client.sync_time()
            if not self.client.can_trade():
                raise BinanceError("المفتاح لا يملك صلاحية التداول")

        self.position = None
        self.history = []
        self._load()

    # ── الحالة ──
    def _load(self):
        if os.path.exists(STATE_FILE):
            try:
                s = json.load(open(STATE_FILE))
                self.position = s.get('position')
                self.history = s.get('history', [])
                if self.history:
                    print(f"💾 استُعيدت الحالة ({len(self.history)} صفقة سابقة)")
            except Exception:
                pass

    def _save(self):
        json.dump({'position': self.position, 'history': self.history,
                   'mode': self.mode, 'updated': datetime.now().isoformat()},
                  open(STATE_FILE, 'w'), ensure_ascii=False, indent=2)

    # ── البيانات ──
    def _fetch(self, limit=400):
        if self.client:
            kl = self.client.klines(self.symbol, self.interval, limit)
        else:
            import real_data
            d = real_data.get_data(self.symbol, self.interval, 400)
            n = min(limit, len(d['close']))
            return (np.array(d['open'][-n:]), np.array(d['high'][-n:]),
                    np.array(d['low'][-n:]), np.array(d['close'][-n:]),
                    np.array(d['volume'][-n:]))
        return (np.array([float(k[1]) for k in kl]),
                np.array([float(k[2]) for k in kl]),
                np.array([float(k[3]) for k in kl]),
                np.array([float(k[4]) for k in kl]),
                np.array([float(k[5]) for k in kl]))

    def _balance(self):
        if self.mode == 'paper':
            return self.capital
        b = self.client.balances()
        return b.get('USDT', {}).get('free', 0.0)

    # ── دورة واحدة ──
    def tick(self, verbose=True):
        o, h, l, c, v = self._fetch()
        i = len(c) - 1
        px = float(c[i])
        ind = {'ema_f': ema(c,20), 'ema_s': ema(c,50), 'rsi': rsi(c,14),
               'atr': atr(h,l,c,14), 'adx': adx(h,l,c,14)}
        ts = datetime.now().strftime('%H:%M:%S')

        # ── مركز مفتوح؟
        if self.position:
            p = self.position
            if px <= p['stop']:
                self._close(px, 'وقف خسارة')
            elif px >= p['target']:
                self._close(px, 'هدف ربح')
            else:
                r = (px - p['entry']) / (p['entry'] - p['stop'])
                if verbose:
                    pnl = (px - p['entry']) * p['qty']
                    print(f"[{ts}] مفتوح | ${px:,.2f} | {r:+.2f}R | ${pnl:+,.2f}")
            self._save()
            return

        # ── حواجز الأمان
        ok = self.guard.can_trade(self._balance(), 0)
        if not ok['allowed']:
            if verbose: print(f"[{ts}] ⛔ {ok['reason']}")
            return

        # ── إشارة؟
        sig = self.engine.evaluate(i, o, h, l, c, v, ind)
        if not sig['enter']:
            if verbose: print(f"[{ts}] ⚪ ${px:,.2f} — {sig['reason']}")
            return

        self._open(sig, px, verbose)
        self._save()

    # ── فتح ──
    def _open(self, sig, px, verbose=True):
        bal = self._balance()
        entry = self.cost.entry_price(px, 'long')
        sz = self.sizer.calculate(bal, entry, sig['stop'],
                                  signal_stars=sig['stars'],
                                  consecutive_losses=self.guard.consecutive_losses,
                                  regime_confidence=sig['regime_conf'])
        notional = min(sz['notional'], self.max_position_usd, bal * 0.95)
        if notional < 11:
            print(f"⚠️ المبلغ ${notional:.2f} أقل من الحد الأدنى لبينانس (~$10)")
            return
        qty = notional / entry

        print(f"\n{'='*54}")
        print(f"  🟢 إشارة دخول {'★'*sig['stars']}{'☆'*(5-sig['stars'])}  [{self.mode.upper()}]")
        print(f"{'='*54}")
        print(f"  الدخول ${entry:,.2f} | الوقف ${sig['stop']:,.2f} | الهدف ${sig['target']:,.2f}")
        print(f"  الكمية {qty:.6f} | القيمة ${notional:.2f} | المخاطرة {sz['risk_pct']}%")
        for x in sig['confirms'][:5]:
            print(f"    • {x}")

        order_id = None
        if self.mode == 'paper':
            print("  📝 محاكاة — لم يُرسل أمر")
        else:
            try:
                r = self.client.market_buy(self.symbol, round(notional, 2))
                order_id = r.get('orderId')
                fills = r.get('fills', [])
                if fills:
                    entry = sum(float(f['price'])*float(f['qty']) for f in fills) / \
                            sum(float(f['qty']) for f in fills)
                    qty = sum(float(f['qty']) for f in fills)
                print(f"  ✅ نُفّذ #{order_id} @ ${entry:,.2f}")

                if self.place_stop_on_exchange:
                    try:
                        sl = self.client.stop_loss_limit(
                            self.symbol, qty, sig['stop'], sig['stop']*0.997)
                        print(f"  🛡️ وقف على المنصة #{sl.get('orderId')}")
                    except BinanceError as e:
                        print(f"  ⚠️ فشل وضع الوقف: {e} — سيُدار محلياً")
            except BinanceError as e:
                print(f"  ❌ فشل الأمر: {e}")
                return
        print('='*54)

        self.position = {
            'entry': float(entry), 'stop': float(sig['stop']),
            'target': float(sig['target']), 'qty': float(qty),
            'stars': sig['stars'], 'order_id': order_id,
            'time': datetime.now().isoformat()
        }

    # ── إغلاق ──
    def _close(self, px, reason):
        p = self.position
        exit_px = self.cost.exit_price(px, 'long', is_stop=('وقف' in reason))

        if self.mode != 'paper':
            try:
                self.client.cancel_all(self.symbol)
                r = self.client.market_sell(self.symbol, p['qty'])
                fills = r.get('fills', [])
                if fills:
                    exit_px = sum(float(f['price'])*float(f['qty']) for f in fills) / \
                              sum(float(f['qty']) for f in fills)
            except BinanceError as e:
                print(f"  ❌ فشل الإغلاق: {e}")
                return

        gross = (exit_px - p['entry']) * p['qty']
        fee = self.cost.round_trip_cost(exit_px * p['qty'])
        pnl = gross - fee

        self.guard.record_trade(pnl)
        self.history.append({**p, 'exit': float(exit_px), 'pnl': round(pnl, 2),
                             'reason': reason, 'closed': datetime.now().isoformat()})
        self.position = None

        icon = '✅' if pnl > 0 else '❌'
        print(f"\n{icon} إغلاق — {reason} @ ${exit_px:,.2f} | "
              f"صافي ${pnl:+,.2f} (رسوم ${fee:.2f})")
        self.summary()

    def summary(self):
        if not self.history:
            return
        pnls = [t['pnl'] for t in self.history]
        w = [p for p in pnls if p > 0]; ls = [p for p in pnls if p < 0]
        gp, gl = sum(w), abs(sum(ls))
        print(f"   الإجمالي: {len(pnls)} صفقة | فوز {len(w)/len(pnls)*100:.0f}% | "
              f"صافي ${sum(pnls):+,.2f} | PF {gp/gl if gl else float('inf'):.2f}")

    # ── الحلقة ──
    def run(self, poll_seconds=300):
        icon = {'paper':'📝 محاكاة','testnet':'🧪 TESTNET','live':'🔴 مال حقيقي'}[self.mode]
        print(f"\n{'='*54}")
        print(f"  البوت يعمل — {icon}")
        print(f"  {self.symbol} {self.interval} | فحص كل {poll_seconds//60} دقيقة")
        print(f"  حد المركز ${self.max_position_usd} | Ctrl+C للإيقاف")
        print('='*54 + '\n')
        try:
            while True:
                try:
                    self.tick()
                except BinanceError as e:
                    print(f"⚠️ خطأ بينانس: {e}")
                except Exception as e:
                    print(f"⚠️ {type(e).__name__}: {e}")
                time.sleep(poll_seconds)
        except KeyboardInterrupt:
            print("\n⏹️ إيقاف")
            if self.position:
                print(f"⚠️ يوجد مركز مفتوح على المنصة — الوقف الموضوع يحميه")
            self.summary()
            self._save()


# ═══════════════════════════════════════════════
def load_env():
    p = os.path.join(HERE, '.env')
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, val = line.split('=', 1)
                os.environ.setdefault(k.strip(), val.strip())


if __name__ == '__main__':
    load_env()
    mode = sys.argv[1] if len(sys.argv) > 1 else 'paper'
    key = os.getenv('BINANCE_API_KEY', '')
    sec = os.getenv('BINANCE_API_SECRET', '')
    symbol = os.getenv('SYMBOL', 'BTCUSDT')
    interval = os.getenv('INTERVAL', '4h')

    if mode == 'check':
        diagnose(key, sec, testnet=True); sys.exit()

    if mode == 'validate':
        ValidationGate.check(symbol, interval); sys.exit()

    if mode == 'live':
        print("\n🔴 طلبت وضع المال الحقيقي. تشغيل بوابة التحقق أولاً…")
        try:
            passed, _ = ValidationGate.check(symbol, interval)
        except Exception as e:
            print(f"❌ تعذّر التحقق: {e}")
            print("   بدون تحقق على بيانات حقيقية، لا يُسمح بالوضع الحقيقي.")
            sys.exit(1)
        if not passed:
            print("\n⛔ الوضع الحقيقي مقفول.")
            print("   لتجاوز البوابة رغم ذلك (على مسؤوليتك):")
            print("   أضف السطر التالي في .env")
            print("   I_ACCEPT_LOSING_REAL_MONEY=yes")
            if os.getenv('I_ACCEPT_LOSING_REAL_MONEY', '') != 'yes':
                sys.exit(1)
            print("\n⚠️ تم التجاوز يدوياً. حد المركز مُثبّت على $25.")

    bot = LiveBot(key, sec, mode=mode, symbol=symbol, interval=interval,
                  capital=float(os.getenv('CAPITAL', '1000')),
                  risk_pct=float(os.getenv('RISK_PCT', '1.0')),
                  max_position_usd=25 if mode == 'live'
                                   else float(os.getenv('MAX_POSITION_USD', '100')))
    bot.run(poll_seconds=int(os.getenv('POLL_SECONDS', '300')))
