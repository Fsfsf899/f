/**
 * CRYPTO TRADING INTELLIGENCE DASHBOARD — V1
 * طبقة عرض فقط. لا يوجد في هذا الملف أي إجراء تداول.
 */
import { api, auth, ApiError, clearCache, lastMeta } from './api.js';
import {
  el, kpi, badge, signalBadge, statusBadge, outcomeBadge, dqBadge,
  table, pager, loading, empty, errorState, details, field, select,
  csvButton, num, money, price, pct, prob, ts, dur, sign, tzName, isNil, NA,
} from './ui.js';
import { lineChart, barChart, drawdownChart, calibrationChart, insufficient } from './charts.js';

const ROUTES = [
  { id: 'dashboard',       title: 'Dashboard',        icon: '▣' },
  { id: 'opportunity',     title: 'Best Opportunity', icon: '🎯' },
  { id: 'sizing',          title: 'How Much To Enter', icon: '💰' },
  { id: 'recommendations', title: 'Recommendations',  icon: '◆' },
  { id: 'positions',       title: 'Open Positions',   icon: '▤' },
  { id: 'trades',          title: 'Trade History',    icon: '≡' },
  { id: 'performance',     title: 'Performance',      icon: '📈' },
  { id: 'accuracy',        title: 'Accuracy',         icon: '◎' },
  { id: 'health',          title: 'System Health',    icon: '⛨' },
  { id: 'audit',           title: 'Audit Log',        icon: '🗒' },
  { id: 'settings',        title: 'Settings',         icon: '⚙' },
];

const S = {
  route: 'dashboard', param: null, meta: null, system: null, user: null,
  filters: {}, offsets: {}, timer: null, lastUpdate: null,
};

const REFRESH = { dashboard: 7000, positions: 7000, health: 12000,
  audit: 10000, recommendations: 10000, opportunity: 8000, sizing: 7000 };

/* ═══ التوجيه ═══ */
function parseHash() {
  const h = (location.hash || '#/dashboard').replace(/^#\/?/, '');
  const [id, param] = h.split('/');
  return { route: ROUTES.some((r) => r.id === id) ? id : 'dashboard',
           param: param || null };
}
function go(path) { location.hash = `#/${path}`; }

/* ═══ الهيكل ═══ */
function shell() {
  const side = el('aside', { class: 'side', id: 'side' },
    el('div', { class: 'brand' },
      el('b', {}, 'TRADING INTELLIGENCE'),
      el('span', {}, `dashboard v${S.meta?.data?.dashboard_version || '2'} · read-only`)),
    el('nav', { class: 'nav', 'aria-label': 'الأقسام' },
      ROUTES.map((r) => el('a', {
        href: `#/${r.id}`, 'aria-current': S.route === r.id ? 'page' : null,
        onClick: () => closeDrawer(),
      }, el('span', { class: 'ic' }, r.icon), r.title))),
    el('div', { class: 'side-foot' },
      el('div', {}, `engine ${S.system?.strategy_version || '—'}`),
      el('div', {}, `api ${S.meta?.data?.api_version || '—'}`),
      el('div', {}, `schema ${S.meta?.data?.schema_version ?? '—'}`),
      el('div', {}, `env ${S.system?.trading_mode || 'UNKNOWN'}`),
      el('div', {}, 'CANNOT TRADE')));

  const scrim = el('div', { class: 'scrim', id: 'scrim',
    onClick: () => closeDrawer() });

  const mode = (S.system?.trading_mode || 'UNKNOWN').toUpperCase();
  const bannerCls = mode === 'PAPER' ? 'paper'
    : mode === 'LIVE' ? 'live' : 'unknown';
  const bannerTxt = mode === 'PAPER' ? '📄 PAPER TRADING — لا أموال حقيقية'
    : mode === 'LIVE' ? '🔴 LIVE TRADING'
    : `⚠ UNKNOWN MODE — الحالة غير محدَّدة (${mode})`;

  const dataState = dataFreshness();
  const main = el('div', { class: 'main' },
    el('div', { class: `banner ${bannerCls}`, role: 'status' }, bannerTxt),
    el('header', { class: 'top' },
      el('button', { class: 'burger', 'aria-label': 'القائمة',
        onClick: () => openDrawer() }, '☰'),
      el('h1', {}, ROUTES.find((r) => r.id === S.route)?.title || ''),
      el('span', { class: 'chip' },
        el('span', { class: `dot ${statusDotClass(S.system?.system_status)}` }),
        `SYSTEM ${S.system?.system_status || 'UNKNOWN'}`),
      el('span', { class: 'chip' }, `MODE ${mode}`),
      el('span', { class: 'chip' },
        el('span', { class: `dot ${dataState === 'LIVE' ? 'dot-g' : 'dot-a'}` }),
        `DATA ${dataState}`),
      el('span', { class: 'chip' },
        `UPDATED ${S.lastUpdate ? new Date(S.lastUpdate).toLocaleTimeString(undefined, { hour12: false }) : '—'}`),
      el('span', { class: 'ro-badge', title: 'لا تنفّذ تداولاً' },
        '🔒 READ-ONLY · CANNOT TRADE'),
      S.user ? el('button', { onClick: async () => { await auth.logout(); boot(); },
        title: `خروج (${S.user})` }, '⏻') : null,
      el('button', { onClick: () => { clearCache(); render(); },
        title: 'تحديث' }, '↻')),
    el('main', { class: 'content', id: 'view' }, loading()));

  const bottom = el('nav', { class: 'bottomnav', 'aria-label': 'تنقّل' },
    ROUTES.slice(0, 5).map((r) => el('a', {
      href: `#/${r.id}`, 'aria-current': S.route === r.id ? 'page' : null },
      el('span', { class: 'ic' }, r.icon), r.title.split(' ')[0])));

  return el('div', { class: 'shell' }, side, scrim, main, bottom);
}

const openDrawer = () => {
  document.getElementById('side')?.classList.add('open');
  document.getElementById('scrim')?.classList.add('on');
};
const closeDrawer = () => {
  document.getElementById('side')?.classList.remove('open');
  document.getElementById('scrim')?.classList.remove('on');
};

function dataFreshness() {
  const age = S.system?.engine_heartbeat_age_ms;
  if (isNil(age)) return 'UNKNOWN';
  return age < 180000 ? 'LIVE' : 'STALE';
}

/* ═══ العرض ═══ */
function mount(node) {
  const v = document.getElementById('view');
  if (!v) return;
  const w = warningBar();
  v.replaceChildren(...(w ? [w, node] : [node]));
  S.lastUpdate = Date.now();
}

async function render() {
  const { route, param } = parseHash();
  S.route = route; S.param = param;
  document.getElementById('app').replaceChildren(shell());
  try {
    const h = await api.dashboard().catch(() => null);
    S.system = h?.data?.kpis?.system || S.system;
  } catch { /* تُعالج في الصفحة */ }
  try {
    await PAGES[route](param);
  } catch (e) {
    mount(errorState(e, () => { clearCache(); render(); }));
  }
  scheduleRefresh(route);
}

function scheduleRefresh(route) {
  clearTimeout(S.timer);
  const ms = REFRESH[route];
  if (!ms) return;
  S.timer = setTimeout(async () => {
    if (document.hidden) return scheduleRefresh(route);
    try { await PAGES[route](S.param); } catch { /* تجاهل دورة */ }
    scheduleRefresh(route);
  }, ms);
}

/* ═══ الصفحات ═══ */
const PAGES = {};

PAGES.dashboard = async () => {
  let d;
  try { d = (await api.dashboard()).data; }
  catch (e) { return mount(errorState(e, render)); }
  // ملخص مختصر لآخر مسح فرصة — القسم 96: "The main dashboard must
  // show BEST CURRENT OPPORTUNITY". اختياري تماماً — فشله لا يكسر
  // بقية اللوحة (قد يكون المحرك غير مُفعَّل لهذا التشغيل).
  const lastScan = await api.lastOpportunityScan().then((r) => r.data).catch(() => null);

  const k = d.kpis;
  if (!k.available) {
    return mount(el('div', {},
      el('div', { class: 'card' }, el('div', { class: 'state err' },
        el('div', { class: 'big' }, 'DATABASE UNAVAILABLE'),
        k.system?.error || 'تعذّر قراءة قاعدة المحرك'))));
  }
  const sys = k.system;

  const kpis = el('div', { class: 'grid kpis' },
    kpi('System', sys.system_status,
        sys.system_status === 'ONLINE' ? 'g' : sys.system_status === 'HALTED' ? 'r' : 'a'),
    kpi('Mode', sys.trading_mode, 'b'),
    kpi('Open Positions', k.open_positions ?? 0),
    kpi("Today's PnL", isNil(k.today_pnl) ? NA : money(k.today_pnl),
        sign(k.today_pnl)),
    kpi('Total PnL', isNil(k.total_pnl) ? NA : money(k.total_pnl),
        sign(k.total_pnl), `${k.total_trades} صفقة`),
    kpi('Win Rate', isNil(k.win_rate) ? NA : pct(k.win_rate, 1), '',
        k.sample_sufficient ? '' : 'عينة صغيرة'),
    kpi('Profit Factor', isNil(k.profit_factor) ? NA : num(k.profit_factor, 3),
        isNil(k.profit_factor) ? '' : k.profit_factor >= 1 ? 'g' : 'r'),
    kpi('Max Drawdown', isNil(k.max_drawdown_pct) ? NA : pct(k.max_drawdown_pct),
        isNil(k.max_drawdown_pct) ? '' : 'a'),
    kpi('Data Quality', isNil(k.data_quality) ? NA : pct(k.data_quality, 0)));

  const marketCard = el('section', { class: 'card' },
    el('h2', {}, 'Market Overview'),
    d.market.length === 0
      ? el('div', { class: 'state' }, 'NO MARKET DATA')
      : table([
          { label: 'Symbol', key: 'symbol' },
          { label: 'TF', key: 'interval' },
          { label: 'Price', num: true, render: (r) => price(r.last_price) },
          { label: 'Regime', render: (r) => badge(r.market_regime || 'UNKNOWN', 'unk') },
          { label: 'Trend', render: (r) => badge(r.trend,
              r.trend === 'UP' ? 'buy' : r.trend === 'DOWN' ? 'loss' : 'unk') },
          { label: 'Data Q', render: (r) => dqBadge(r.data_quality) },
          { label: 'Last Bar', render: (r) => el('span', {},
              ts(r.last_bar_ts), r.stale ? ' ' : '',
              r.stale ? badge('STALE', 'stale') : '') },
        ], d.market));

  const recsCard = el('section', { class: 'card' },
    el('h2', {}, 'Latest Recommendations',
      el('a', { href: '#/recommendations' }, 'الكل ←')),
    recTable(d.recommendations));

  const posCard = el('section', { class: 'card' },
    el('h2', {}, 'Open Positions', el('a', { href: '#/positions' }, 'الكل ←')),
    d.positions.length === 0
      ? el('div', { class: 'state' }, 'NO OPEN POSITIONS')
      : posTable(d.positions));

  const bestOpp = lastScan && lastScan.opportunities && lastScan.opportunities.length
    ? el('section', { class: 'card' },
        el('h2', {}, '🎯 Best Current Opportunity',
          el('a', { href: '#/opportunity' }, 'التفاصيل ←')),
        lastScan.decision === 'BUY' && lastScan.selected_symbol
          ? details([
              field('ASSET', el('b', {}, lastScan.selected_symbol)),
              field('DECISION', badge('BUY', 'buy')),
              field('WHY', lastScan.reason),
            ])
          : el('div', { class: 'state' },
              el('div', { class: 'big' }, 'NO TRADE'),
              lastScan.reason || 'لا فرصة مؤهَّلة حالياً'))
    : null;

  mount(el('div', { class: 'grid', class: 'gap-14' },
    kpis, bestOpp, marketCard, recsCard, posCard,
    k.sample_sufficient ? null : el('div', { class: 'note' },
      'عينة الصفقات أقل من 30 — الأرقام غير كافية لأي استنتاج عن الربحية.')));
};

function recTable(items, onRow) {
  return table([
    { label: 'Time', render: (r) => ts(r.timestamp) },
    { label: 'Symbol', key: 'symbol' },
    { label: 'TF', key: 'timeframe' },
    { label: 'Signal', render: (r) => signalBadge(r.signal) },
    { label: 'Score', num: true, render: (r) => num(r.score, 2) },
    { label: 'Conf', num: true, render: (r) => prob(r.confidence) },
    { label: 'Prob', num: true, render: (r) =>
        isNil(r.calibrated_probability)
          ? el('span', { class: 'm', title: 'غير معاير' }, NA)
          : prob(r.calibrated_probability) },
    { label: 'Entry', num: true, render: (r) => price(r.entry) },
    { label: 'Stop', num: true, render: (r) => price(r.stop_loss) },
    { label: 'Target', num: true, render: (r) => price(r.take_profit) },
    { label: 'R/R', num: true, render: (r) => num(r.risk_reward, 2) },
    { label: 'Regime', render: (r) => r.market_regime || NA },
    { label: 'Data Q', render: (r) => dqBadge(
        isNil(r.data_quality) ? null : r.data_quality * 100) },
    { label: 'Status', render: (r) => badge(r.status,
        r.status === 'CLOSED' ? 'unk' : r.status === 'OPEN' ? 'on' : 'no') },
  ], items, { onRow: onRow || ((r) => go(`recommendations/${r.signal_id}`)),
              caption: 'قائمة التوصيات' });
}

function posTable(items) {
  return table([
    { label: 'Symbol', key: 'symbol' },
    { label: 'Side', render: (r) => badge(r.side || 'LONG', 'buy') },
    { label: 'Entry', num: true, render: (r) => price(r.entry_price) },
    { label: 'Current', num: true, render: (r) => price(r.current_price) },
    { label: 'Qty', num: true, render: (r) => num(r.qty, 6) },
    { label: 'Value', num: true, render: (r) => isNil(r.position_value)
        ? NA : price(r.position_value) },
    { label: 'Stop', num: true, render: (r) => price(r.stop_loss) },
    { label: 'Target', num: true, render: (r) => price(r.take_profit) },
    { label: 'uPnL', num: true, render: (r) => el('span',
        { class: sign(r.unrealized_pnl) }, money(r.unrealized_pnl)) },
    { label: 'uPnL %', num: true, render: (r) => el('span',
        { class: sign(r.unrealized_pnl_pct) }, pct(r.unrealized_pnl_pct)) },
    { label: 'R', num: true, render: (r) => isNil(r.r_multiple)
        ? NA : `${num(r.r_multiple, 2)}R` },
    { label: 'Duration', render: (r) => dur(r.duration_ms) },
  ], items, { onRow: (r) => go(`positions/${r.id}`), caption: 'المراكز المفتوحة' });
}

/* ── أفضل فرصة (الأقسام 78-98) ── */
/* ═══ كم أدخل؟ — الأقسام 13 و25 و34 من مواصفة V11 FINAL ═══
   قاعدة صارمة: **لا حساب في هذا الملف إطلاقاً.** كل رقم معروض قادم
   كما هو من `PositionSizer` الكنسي عبر الخلفية. القسم 25 صريح:
   "must come from the backend PositionSizer ... not from frontend
   arithmetic". أي ضرب أو قسمة هنا يجعل اللوحة مصدر حقيقة ثانياً
   يخالف الخلفية بصمت. */
PAGES.sizing = async () => {
  let plan, account;
  try {
    [plan, account] = await Promise.all([
      api.sizingPlan().then((r) => r.data),
      api.account().then((r) => r.data),
    ]);
  } catch (e) { return mount(errorState(e, render)); }

  if (!plan || plan.reason === 'NO_PLAN_YET') {
    return mount(el('section', { class: 'card' },
      el('h2', {}, '💰 كم أستطيع أن أدخل؟'),
      el('div', { class: 'state' },
        el('div', { class: 'big' }, 'لم تُحسَب أي خطة بعد'),
        'شغّل المتداول (paper/testnet) أو نفّذ '
        + '`python3 live_trader.py sizing` لتوليد أول خطة.')));
  }

  const enter = plan.decision === 'ENTER';
  const cur = account?.account_currency || 'USDT';
  const stale = (plan.plan_age_seconds ?? 0) > 300;

  const decisionCard = el('section', { class: 'card' },
    el('h2', {}, '💰 القرار', badge(enter ? 'ENTER' : 'NO TRADE',
                                    enter ? 'buy' : 'no')),
    el('div', { class: 'state' },
      el('div', { class: 'big' },
        enter ? `${money(plan.position_value)} ${cur}` : 'لا تدخل'),
      enter ? `${num(plan.final_quantity, 8)} وحدة من ${plan.symbol}`
            : (plan.reason || 'غير محدَّد')),
    stale ? el('div', { class: 'warn-bar' },
      `⚠️ عمر هذه الخطة ${dur((plan.plan_age_seconds || 0) * 1000)} — `
      + 'قد لا تعكس حالة حسابك الآن.') : null);

  const balanceCard = el('section', { class: 'card' },
    el('h2', {}, '🏦 رصيد الحساب',
       statusBadge(account?.status || 'UNAVAILABLE')),
    details([
      field('الرصيد الكلي', money(account?.total_balance)),
      field('المتاح فعلاً', money(account?.available_balance), 'g'),
      field('المحجوز في أوامر', money(account?.locked_balance)),
      field('الاحتياطي', money(account?.reserve_amount)),
      field('القابل للاستخدام', el('b', {}, money(account?.usable_equity))),
      field('تعرّض قائم', money(account?.existing_exposure)),
      field('عمر قراءة الرصيد',
            isNil(account?.balance_age_seconds) ? NA
              : dur((account.balance_age_seconds || 0) * 1000)),
      field('المصدر', account?.source || NA),
    ]));

  const riskCard = el('section', { class: 'card' },
    el('h2', {}, '⚖️ المخاطرة والحجم'),
    details([
      field('المخاطرة الأساسية', pct(plan.base_risk_pct)),
      field('المخاطرة الفعّالة', pct(plan.effective_risk_pct)),
      field('المخاطرة بعد التقريب', pct(plan.actual_risk_pct)),
      field('أقصى مبلغ مخاطرة', money(plan.max_risk_amount)),
      field('الدخول', price(plan.entry)),
      field('الوقف', price(plan.stop), 'r'),
      field('الهدف', price(plan.target), 'g'),
      field('مسافة الوقف', pct(plan.stop_distance_pct)),
      field('الخسارة لكل وحدة', money(plan.risk_per_unit)),
      field('R:R', num(plan.risk_reward, 2)),
      field('الكمية الخام', num(plan.raw_quantity, 8)),
      field('الكمية النهائية', el('b', {}, num(plan.final_quantity, 8))),
      field('قيمة المركز', money(plan.position_value)),
      field('أقصى قيمة مسموحة', money(plan.max_position_value)),
      field('رسوم الدخول', money(plan.estimated_entry_fee)),
      field('رسوم الخروج', money(plan.estimated_exit_fee)),
      field('الانزلاق (bps)', num(plan.slippage_bps, 2)),
      field('الفارق السعري (bps)', num(plan.spread_bps, 2)),
      field('أقصى خسارة متوقَّعة', money(plan.estimated_max_loss), 'r'),
      field('المتبقي بعد الأمر', money(plan.remaining_available)),
      field('خُفِّض بسبب الرصيد', plan.capped_by_balance ? 'نعم' : 'لا'),
    ]));

  const whyCard = el('section', { class: 'card' },
    el('h2', {}, '❓ لماذا هذا المبلغ؟'),
    (plan.explanation && plan.explanation.length)
      ? el('ul', { class: 'why-list' },
           ...plan.explanation.map((line) => el('li', {}, line)))
      : el('div', { class: 'state' }, 'لا تفسير مُسجَّل لهذه الخطة.'));

  mount(el('div', {}, decisionCard, balanceCard, riskCard, whyCard));
};

PAGES.opportunity = async () => {
  let last, history;
  try {
    [last, history] = await Promise.all([
      api.lastOpportunityScan().then((r) => r.data),
      api.opportunityScans({ limit: 20 }).then((r) => r.data),
    ]);
  } catch (e) { return mount(errorState(e, render)); }

  if (!last || !last.opportunities || last.opportunities.length === 0) {
    return mount(el('div', {},
      el('section', { class: 'card' },
        el('h2', {}, '🎯 أفضل فرصة حالياً'),
        el('div', { class: 'state' },
          el('div', { class: 'big' }, 'لا مسح مُسجَّل بعد'),
          'محرك اختيار أفضل فرصة غير مُفعَّل لهذا التشغيل، أو لم تُنفَّذ '
          + 'أي دورة مسح بعد.'))));
  }

  const selected = last.opportunities.find((o) => o.symbol === last.selected_symbol);
  const isBuy = last.decision === 'BUY' && selected;

  const summaryCard = el('section', { class: 'card' },
    el('h2', {}, '🎯 أفضل فرصة حالياً', badge(last.decision, isBuy ? 'buy' : 'no')),
    isBuy ? details([
      field('الرمز المختار', el('b', {}, selected.symbol)),
      field('الدرجة', num(selected.score, 2)),
      field('الدخول', price(selected.signal?.entry)),
      field('الوقف', price(selected.signal?.stop_loss), 'r'),
      field('الهدف', price(selected.signal?.take_profit), 'g'),
      field('R:R', num(selected.signal?.risk_reward, 2)),
      field('لماذا', last.reason),
      field('توقيت المسح', ts(last.ts, true)),
      field('سياق BTC', last.btc_context),
      field('تحكيم عند تعادل؟', last.tie_break_applied ? 'نعم — أبجدياً' : 'لا'),
    ]) : el('div', { class: 'state' },
      el('div', { class: 'big' }, 'NO TRADE'),
      `السبب: ${last.reason === 'NO_ELIGIBLE_OPPORTUNITY'
        ? 'لا فرصة مؤهَّلة من بين الرموز الممسوحة حالياً'
        : last.reason === 'POSITION_ALREADY_OPEN'
          ? 'مركز مفتوح فعلياً — لا مسح جديد (البند 84)'
          : (last.reason || 'غير محدَّد')}`));

  const compareCols = [
    { label: 'الرمز', key: 'symbol' },
    { label: 'مؤهَّل', render: (r) => badge(r.eligible ? 'ELIGIBLE' : 'REJECTED',
                                            r.eligible ? 'on' : 'off') },
    { label: 'القرار', render: (r) => signalBadge(r.decision) },
    { label: 'الدرجة', num: true, render: (r) => r.eligible ? num(r.score, 2) : NA },
    { label: 'الثقة', num: true,
     render: (r) => (r.signal ? prob(r.signal.confidence) : NA) },
    { label: 'الدخول', num: true, render: (r) => (r.signal ? price(r.signal.entry) : NA) },
    { label: 'الوقف', num: true,
     render: (r) => (r.signal ? price(r.signal.stop_loss) : NA) },
    { label: 'الهدف', num: true,
     render: (r) => (r.signal ? price(r.signal.take_profit) : NA) },
    { label: 'R:R', num: true,
     render: (r) => (r.signal ? num(r.signal.risk_reward, 2) : NA) },
    { label: 'القرار النهائي', render: (r) => (r.symbol === last.selected_symbol
        ? badge('SELECTED', 'buy')
        : (r.eligible ? badge('NOT BEST', 'wait') : badge('REJECTED', 'off'))) },
    { label: 'السبب', render: (r) => (r.eligible ? '—'
        : (r.rejection_reasons || []).join('، ')) },
  ];
  const compareCard = el('section', { class: 'card' },
    el('h2', {}, 'جدول مقارنة الفرص'),
    table(compareCols, last.opportunities, { caption: 'مقارنة كل الرموز الممسوحة' }));

  const historyCols = [
    { label: 'الوقت', render: (r) => ts(r.ts, true) },
    { label: 'القرار', render: (r) => signalBadge(r.decision) },
    { label: 'المختار', render: (r) => r.selected_symbol || '—' },
    { label: 'السبب', key: 'reason' },
    { label: 'الرموز الممسوحة', render: (r) => (r.scanned_symbols || []).join('، ') },
  ];
  const historyCard = el('section', { class: 'card' },
    el('h2', {}, 'سجل عمليات المسح الأخيرة'),
    table(historyCols, history || [], { caption: 'آخر عمليات مسح الفرص' }));

  mount(el('div', {}, summaryCard, compareCard, historyCard));
};

/* ── التوصيات ── */
PAGES.recommendations = async (param) => {
  if (param) return recDetail(param);
  const f = S.filters.rec || (S.filters.rec = {});
  const off = S.offsets.rec || 0;
  let d;
  try { d = (await api.recommendations({ ...f, limit: 50, offset: off })).data; }
  catch (e) { return mount(errorState(e, render)); }

  const setF = (k) => (v) => {
    f[k] = v || undefined; S.offsets.rec = 0; clearCache('/recommendations');
    PAGES.recommendations();
  };
  const bar = el('div', { class: 'filters' },
    select('Symbol', f.symbol, [['', 'الكل'], ...symbolsOf(d.items)], setF('symbol')),
    select('Timeframe', f.interval, [['', 'الكل'], '5m', '15m', '1h', '4h', '1d'], setF('interval')),
    select('Signal', f.signal, [['', 'الكل'], 'BUY', 'WAIT', 'NO_TRADE'], setF('signal')),
    select('Status', f.status, [['', 'الكل'], 'OPEN', 'CLOSED', 'NOT_ACTED'], setF('status')),
    select('Min Score', f.min_score, [['', 'الكل'], '2', '4', '6'], setF('min_score')),
    csvButton('recommendations.csv', [
      { label: 'timestamp', key: 'timestamp' }, { label: 'symbol', key: 'symbol' },
      { label: 'signal', key: 'signal' }, { label: 'score', key: 'score' },
      { label: 'probability', key: 'calibrated_probability' },
      { label: 'entry', key: 'entry' }, { label: 'stop', key: 'stop_loss' },
      { label: 'target', key: 'take_profit' }, { label: 'rr', key: 'risk_reward' },
      { label: 'regime', key: 'market_regime' }, { label: 'status', key: 'status' },
    ], d.items));

  mount(el('div', {}, bar,
    el('div', { class: 'card' },
      d.items.length === 0 ? el('div', { class: 'state' },
        el('div', { class: 'big' }, 'NO ACTIVE RECOMMENDATIONS'),
        'لا توصيات مطابقة للفلاتر.') : recTable(d.items),
      d.total > d.limit ? pager(d.total, d.limit, d.offset, (o) => {
        S.offsets.rec = o; clearCache('/recommendations'); PAGES.recommendations();
      }) : null)));
};

const symbolsOf = (items) => [...new Set(items.map((i) => i.symbol))].sort();

async function recDetail(id) {
  let r;
  try { r = (await api.recommendation(id)).data; }
  catch (e) { return mount(errorState(e, () => go('recommendations'))); }

  mount(el('div', {},
    el('button', { class: 'back', onClick: () => go('recommendations') }, '← التوصيات'),
    el('section', { class: 'card' },
      el('h2', {}, `Recommendation #${r.signal_id} · ${r.symbol}`, signalBadge(r.signal)),
      details([
        field('Recommendation ID', r.recommendation_id ?? NA),
        field('Timestamp', ts(r.timestamp, true)),
        field('Timeframe', r.timeframe),
        field('Score', num(r.score, 3)),
        field('Stars', r.stars ? '★'.repeat(r.stars) + '☆'.repeat(5 - r.stars) : NA),
        field('Confidence', prob(r.confidence)),
        field('Raw Probability', prob(r.raw_probability)),
        field('Calibrated Probability', isNil(r.calibrated_probability)
          ? el('span', { class: 'm' }, `${NA} (${r.probability_source || 'UNCALIBRATED'})`)
          : prob(r.calibrated_probability)),
        field('Entry', price(r.entry)),
        field('Stop Loss', price(r.stop_loss), 'r'),
        field('Take Profit', price(r.take_profit), 'g'),
        field('Risk/Reward', num(r.risk_reward, 2)),
        field('ATR', num(r.atr, 4)),
        field('Data Quality', isNil(r.data_quality) ? NA : pct(r.data_quality * 100, 1)),
        field('Market Regime', r.market_regime),
        field('BTC Context', r.btc_context),
        field('Position Size', r.position ? num(r.position.qty, 8) : NA),
        field('Engine Version', r.engine_version),
        field('Config Fingerprint', r.config_fingerprint),
        field('Status', badge(r.status, r.status === 'OPEN' ? 'on' : 'unk')),
        field('Outcome', r.outcome ? outcomeBadge(r.outcome, r.pnl) : NA),
        field('Net PnL', isNil(r.pnl) ? NA : money(r.pnl), sign(r.pnl)),
        field('MAE / MFE', `${pct(r.mae_pct)} / ${pct(r.mfe_pct)}`),
      ])),
    el('section', { class: 'card' }, el('h2', {}, 'Reasons'),
      (r.reasons || []).length
        ? el('div', { class: 'tags' }, r.reasons.map((x) => el('span', { class: 'tag' }, x)))
        : el('div', { class: 'state' }, 'لا أدلة مسجَّلة')),
    el('section', { class: 'card' }, el('h2', {}, 'No-Trade Reasons'),
      (r.no_trade_reasons || []).length
        ? el('div', { class: 'tags' },
            r.no_trade_reasons.map((x) => el('span', { class: 'tag' }, x)))
        : el('div', { class: 'state' }, 'لا موانع')),
    r.position ? el('section', { class: 'card' },
      el('h2', {}, 'Linked Position'),
      el('button', { onClick: () => go(`positions/${r.position.id}`) },
        `عرض المركز #${r.position.id} ←`)) : null,
    el('div', { class: 'note' },
      'كل القيم مقروءة كما أنتجها المحرك — لا إعادة حساب في طبقة العرض.')));
}

/* ── المراكز ── */
PAGES.positions = async (param) => {
  if (param) return posDetail(param);
  let d;
  try { d = (await api.positions()).data; }
  catch (e) { return mount(errorState(e, render)); }
  mount(el('div', { class: 'card' }, el('h2', {}, 'Open Positions'),
    d.length === 0
      ? el('div', { class: 'state' },
          el('div', { class: 'big' }, 'NO OPEN POSITIONS'),
          'لا مراكز مفتوحة حالياً.')
      : el('div', {}, posTable(d),
          el('div', { class: 'note' },
            'السعر الحالي مقروء من آخر شمعة إشارة مسجَّلة — طبقة العرض '
            + 'لا تتصل بالمنصة ولا تجلب أسعاراً لحظية.'))));
};

async function posDetail(id) {
  let p;
  try { p = (await api.position(id)).data; }
  catch (e) { return mount(errorState(e, () => go('positions'))); }

  mount(el('div', {},
    el('button', { class: 'back', onClick: () => go('positions') }, '← المراكز'),
    el('section', { class: 'card' },
      el('h2', {}, `Position #${p.id} · ${p.symbol}`, badge(p.status,
        p.status === 'OPEN' ? 'on' : 'unk')),
      details([
        field('Side', p.side || 'LONG'),
        field('Entry', price(p.entry_price)),
        field('Current', price(p.current_price)),
        field('Quantity', num(p.qty, 8)),
        field('Stop Loss', price(p.stop_loss), 'r'),
        field('Take Profit', price(p.take_profit), 'g'),
        field('Unrealized PnL', money(p.unrealized_pnl), sign(p.unrealized_pnl)),
        field('R Multiple', isNil(p.r_multiple) ? NA : `${num(p.r_multiple, 2)}R`),
        field('Risk Amount', isNil(p.risk_amount) ? NA : price(p.risk_amount)),
        field('Opened', ts(p.opened_ts, true)),
        field('Duration', dur(p.duration_ms)),
        field('Realized PnL', isNil(p.realized_pnl) ? NA : money(p.realized_pnl)),
        field('Fees', isNil(p.fees) ? NA : num(p.fees, 6)),
        field('Recommendation', p.recommendation_id ?? NA),
      ])),
    p.recommendation ? el('section', { class: 'card' },
      el('h2', {}, 'Original Recommendation'),
      details([
        field('Score', num(p.recommendation.score, 3)),
        field('Probability', isNil(p.recommendation.calibrated_probability)
          ? NA : prob(p.recommendation.calibrated_probability)),
        field('Regime', p.recommendation.market_regime),
        field('Signal', signalBadge(p.recommendation.signal)),
      ]),
      (p.recommendation.reasons || []).length ? el('div',
        { class: 'tags', class: 'mt-11' },
        p.recommendation.reasons.map((x) => el('span', { class: 'tag' }, x))) : null,
    ) : null,
    el('section', { class: 'card' }, el('h2', {}, 'Timeline'),
      (p.timeline || []).length
        ? el('ul', { class: 'timeline' }, p.timeline.map((e) =>
            el('li', {}, el('b', {}, e.event), ' — ', ts(e.ts),
              e.detail ? el('div', { class: 'm' }, e.detail) : null)))
        : el('div', { class: 'state' }, 'لا أحداث')),
    el('section', { class: 'card' }, el('h2', {}, 'Orders'),
      table([
        { label: 'ID', key: 'id' },
        { label: 'Client Order ID', key: 'client_order_id' },
        { label: 'Type', key: 'type' },
        { label: 'Side', key: 'side' },
        { label: 'Qty', num: true, render: (r) => num(r.qty, 8) },
        { label: 'Price', num: true, render: (r) => price(r.price) },
        { label: 'Status', render: (r) => badge(r.status || 'UNKNOWN', 'unk') },
        { label: 'Time', render: (r) => ts(r.ts) },
      ], p.orders || [])),
    el('section', { class: 'card' }, el('h2', {}, 'Fills'),
      table([
        { label: 'Trade ID', key: 'exchange_trade_id' },
        { label: 'Qty', num: true, render: (r) => num(r.qty, 8) },
        { label: 'Price', num: true, render: (r) => price(r.price) },
        { label: 'Commission', num: true, render: (r) => num(r.commission, 8) },
        { label: 'Time', render: (r) => ts(r.ts) },
      ], p.fills || [])),
    el('div', { class: 'note' }, 'READ-ONLY — لا توجد أي إجراءات على هذا المركز.')));
}

/* ── الصفقات ── */
PAGES.trades = async () => {
  const f = S.filters.tr || (S.filters.tr = {});
  const off = S.offsets.tr || 0;
  let d;
  try { d = (await api.trades({ ...f, limit: 50, offset: off })).data; }
  catch (e) { return mount(errorState(e, render)); }

  const setF = (k) => (v) => {
    f[k] = v || undefined; S.offsets.tr = 0; clearCache('/trades'); PAGES.trades();
  };
  const cols = [
    { label: 'ID', key: 'trade_id' },
    { label: 'Symbol', key: 'symbol' },
    { label: 'TF', key: 'timeframe' },
    { label: 'Entry Time', render: (r) => ts(r.entry_time), raw: (r) => r.entry_time },
    { label: 'Exit Time', render: (r) => ts(r.exit_time), raw: (r) => r.exit_time },
    { label: 'Entry', num: true, render: (r) => price(r.entry), raw: (r) => r.entry },
    { label: 'Exit', num: true, render: (r) => price(r.exit), raw: (r) => r.exit },
    { label: 'Qty', num: true, render: (r) => num(r.quantity, 8), raw: (r) => r.quantity },
    { label: 'Gross', num: true, render: (r) => money(r.gross_pnl), raw: (r) => r.gross_pnl },
    { label: 'Fees', num: true, render: (r) => num(r.fees, 6), raw: (r) => r.fees },
    { label: 'Net PnL', num: true, raw: (r) => r.net_pnl,
      render: (r) => el('span', { class: sign(r.net_pnl) }, money(r.net_pnl)) },
    { label: 'MFE', num: true, render: (r) => pct(r.mfe_pct), raw: (r) => r.mfe_pct },
    { label: 'MAE', num: true, render: (r) => pct(r.mae_pct), raw: (r) => r.mae_pct },
    { label: 'Duration', render: (r) => dur(r.duration_ms), raw: (r) => r.duration_ms },
    { label: 'Exit Reason', key: 'exit_reason' },
    { label: 'Result', render: (r) => outcomeBadge(r.outcome, r.net_pnl),
      raw: (r) => r.outcome },
    { label: 'Prob', num: true, render: (r) => prob(r.probability), raw: (r) => r.probability },
    { label: 'Regime', key: 'regime' },
  ];
  mount(el('div', {},
    el('div', { class: 'filters' },
      select('Symbol', f.symbol, [['', 'الكل'], ...symbolsOf(d.items)], setF('symbol')),
      select('Result', f.result, [['', 'الكل'], 'WIN', 'LOSS', 'BREAKEVEN'], setF('result')),
      select('Exit Reason', f.exit_reason, [['', 'الكل'],
        ...new Set(d.items.map((i) => i.exit_reason).filter(Boolean))], setF('exit_reason')),
      csvButton('trades.csv', cols, d.items)),
    el('div', { class: 'card' },
      d.items.length === 0
        ? el('div', { class: 'state' }, el('div', { class: 'big' }, 'NO TRADES'),
            'لا صفقات مغلقة بعد.')
        : table(cols, d.items, { caption: 'سجل الصفقات' }),
      d.total > d.limit ? pager(d.total, d.limit, d.offset, (o) => {
        S.offsets.tr = o; clearCache('/trades'); PAGES.trades();
      }) : null)));
};

/* ── الأداء ── */
PAGES.performance = async () => {
  const f = S.filters.perf || (S.filters.perf = {});
  let d;
  try { d = (await api.performance(f)).data; }
  catch (e) { return mount(errorState(e, render)); }

  const setF = (k) => (v) => {
    f[k] = v || undefined; clearCache('/performance'); PAGES.performance();
  };
  const ranges = [['', 'الكل'], ['7', '7 أيام'], ['30', '30 يوماً'],
    ['90', '90 يوماً'], ['180', '6 أشهر'], ['365', 'سنة']];
  const setRange = (v) => {
    f.date_from = v ? String(Date.now() - Number(v) * 86400000) : undefined;
    clearCache('/performance'); PAGES.performance();
  };

  const bar = el('div', { class: 'filters' },
    select('Symbol', f.symbol, [['', 'الكل'], 'BTCUSDT', 'ETHUSDT', 'SOLUSDT'], setF('symbol')),
    select('Timeframe', f.interval, [['', 'الكل'], '5m', '15m', '1h', '4h', '1d'], setF('interval')),
    select('Regime', f.regime, [['', 'الكل'], 'TRENDING_BULLISH', 'TRENDING_BEARISH',
      'RANGING', 'HIGH_VOLATILITY', 'LOW_VOLATILITY', 'TRANSITION'], setF('regime')),
    select('Range', '', ranges, setRange));

  if (!d.available) {
    return mount(el('div', {}, bar, el('div', { class: 'card' },
      el('div', { class: 'state' }, el('div', { class: 'big' }, 'NO DATA'),
        'لا صفقات مغلقة مطابقة للفلاتر.'))));
  }

  const m = [
    ['Total Trades', d.total_trades], ['Winning', d.winning_trades],
    ['Losing', d.losing_trades], ['Win Rate', pct(d.win_rate, 1)],
    ['Profit Factor', isNil(d.profit_factor) ? NA : num(d.profit_factor, 3)],
    ['Expectancy', money(d.expectancy, 4)],
    ['Average Win', money(d.average_win, 4)],
    ['Average Loss', money(d.average_loss, 4)],
    ['Payoff Ratio', isNil(d.payoff_ratio) ? NA : num(d.payoff_ratio, 3)],
    ['Gross PnL', money(d.gross_pnl, 4)], ['Fees', num(d.fees, 6)],
    ['Net PnL', money(d.net_pnl, 4)],
    ['Max Drawdown', pct(d.max_drawdown_pct)],
    ['Avg Duration', dur(d.avg_duration_ms)],
    ['Avg MAE', pct(d.avg_mae_pct)], ['Avg MFE', pct(d.avg_mfe_pct)],
  ];

  const eqPts = d.equity_curve.map((p, i) => ({ x: '', y: p.equity }));
  const ddPts = d.drawdown_curve.map((p) => ({ x: '', y: p.drawdown_pct }));

  mount(el('div', {}, bar,
    el('div', { class: 'grid kpis' },
      m.map(([k, v]) => kpi(k, v,
        ['Net PnL', 'Gross PnL', 'Expectancy'].includes(k)
          ? sign(parseFloat(String(v).replace(/[^0-9.-]/g, ''))) : ''))),
    d.sample_sufficient ? null : el('div', { class: 'note' },
      `INSUFFICIENT SAMPLE — ${d.total_trades} صفقة أقل من ${d.min_sample}. `
      + 'لا يمكن استنتاج الربحية.'),
    el('section', { class: 'card' }, el('h2', {}, 'Equity Curve (Net, cumulative)'),
      eqPts.length >= 2 ? lineChart(eqPts, { label: 'منحنى الحقوق' })
        : insufficient()),
    el('section', { class: 'card' }, el('h2', {}, 'Drawdown Curve'),
      ddPts.length >= 2 ? drawdownChart(ddPts) : insufficient()),
    el('div', { class: 'grid cols-2' },
      el('section', { class: 'card' }, el('h2', {}, 'Daily PnL'),
        d.daily.length ? barChart(d.daily.map((b) => ({ x: b.bucket, y: b.pnl })),
          { label: 'PnL يومي' }) : insufficient()),
      el('section', { class: 'card' }, el('h2', {}, 'Monthly PnL'),
        d.monthly.length ? barChart(d.monthly.map((b) => ({ x: b.bucket, y: b.pnl })),
          { label: 'PnL شهري' }) : insufficient())),
    el('section', { class: 'card' }, el('h2', {}, 'Win/Loss Distribution'),
      d.distribution.length > 1
        ? barChart(d.distribution.map((b) => ({ x: b.range.split('..')[0], y: b.count })),
            { label: 'التوزيع', posColor: '#58a6ff' })
        : insufficient())));
};

/* ── الدقة ── */
PAGES.accuracy = async () => {
  let d;
  try { d = (await api.accuracy()).data; }
  catch (e) { return mount(errorState(e, render)); }

  const groupTable = (obj, firstLabel) => {
    const rows = Object.entries(obj || {}).map(([k, v]) => ({ key: k, ...v }));
    return rows.length === 0 ? el('div', { class: 'state' }, 'NO DATA')
      : table([
          { label: firstLabel, key: 'key' },
          { label: 'Signals', key: 'count', num: true },
          { label: 'Win Rate', num: true, render: (r) => pct(r.win_rate, 1) },
          { label: 'PF', num: true, render: (r) => isNil(r.profit_factor)
              ? NA : num(r.profit_factor, 3) },
          { label: 'Expectancy', num: true, render: (r) => money(r.expectancy, 4) },
          { label: 'Avg Prob', num: true, render: (r) => prob(r.avg_probability) },
          { label: 'Avg Score', num: true, render: (r) => num(r.avg_score, 2) },
          { label: 'Sample', render: (r) => badge(
              r.sample_sufficient ? 'OK' : 'SMALL',
              r.sample_sufficient ? 'on' : 'deg') },
        ], rows);
  };

  const cal = d.calibration;
  mount(el('div', {},
    el('div', { class: 'grid kpis' },
      kpi('Total Recommendations', d.total_recommendations),
      kpi('NO TRADE', d.no_trade_count, 'm'),
      kpi('Resolved', d.resolved),
      kpi('Correct', d.correct, 'g'),
      kpi('Incorrect', d.incorrect, 'r'),
      kpi('Accuracy', isNil(d.accuracy) ? NA : pct(d.accuracy, 1), '',
        d.sample_sufficient ? '' : 'عينة صغيرة')),
    d.sample_sufficient ? null : el('div', { class: 'note' },
      `INSUFFICIENT SAMPLE — ${d.resolved} توصية محسومة أقل من ${d.min_sample}.`),
    el('section', { class: 'card' },
      el('h2', {}, 'Probability Calibration',
        cal.status === 'OK' ? badge(`Brier ${num(cal.brier, 4)}`, 'unk') : null),
      cal.status !== 'OK'
        ? el('div', { class: 'state' },
            el('div', { class: 'big' }, 'CALIBRATION INSUFFICIENT'),
            `${cal.n} عينة قابلة للقياس، المطلوب ${cal.min_sample}.`)
        : el('div', {},
            calibrationChart(cal.buckets),
            el('div', { class: 'legend' },
              el('span', {}, el('i', { class: 'sw-purple' }), 'المقاس'),
              el('span', {}, el('i', { class: 'sw-dim' }), 'المعايرة المثالية')),
            table([
              { label: 'Bucket', key: 'bucket' },
              { label: 'N', key: 'n', num: true },
              { label: 'Predicted', num: true, render: (r) => prob(r.predicted) },
              { label: 'Actual', num: true, render: (r) => prob(r.actual) },
              { label: 'Gap', num: true, render: (r) => el('span',
                  { class: Math.abs(r.gap) > 0.1 ? 'r' : 'g' },
                  `${r.gap > 0 ? '+' : ''}${num(r.gap * 100, 1)}pp`) },
            ], cal.buckets))),
    el('section', { class: 'card' }, el('h2', {}, 'Accuracy by Symbol'),
      groupTable(d.by_symbol, 'Symbol')),
    el('section', { class: 'card' }, el('h2', {}, 'Accuracy by Timeframe'),
      groupTable(d.by_timeframe, 'Timeframe')),
    el('section', { class: 'card' }, el('h2', {}, 'Accuracy by Market Regime'),
      groupTable(d.by_regime, 'Regime'))));
};

/* ── الصحة ── */
PAGES.health = async () => {
  let d;
  try { d = (await api.health()).data; }
  catch (e) { return mount(errorState(e, render)); }

  const fr = d.freshness || {};
  const frRows = Object.entries(fr).map(([k, v]) => ({
    key: k, ts: v.ts, age: v.age_ms, stale: v.stale }));

  mount(el('div', {},
    el('div', { class: 'card' },
      el('h2', {}, 'Overall', statusBadge(d.overall)),
      table([
        { label: 'Component', key: 'component' },
        { label: 'Status', render: (r) => statusBadge(r.status) },
        { label: 'Last Check', render: (r) => ts(r.last_check_ts) },
        { label: 'Latency', num: true, render: (r) => isNil(r.latency_ms)
            ? NA : `${num(r.latency_ms, 1)}ms` },
        { label: 'Error', render: (r) => r.error
            ? el('span', { class: 'r', title: r.error },
                r.error.slice(0, 60)) : '—' },
      ], d.components)),
    el('div', { class: 'grid cols-3' },
      kpi('Unresolved Intents', d.unresolved_intents,
        d.unresolved_intents ? 'r' : 'g'),
      kpi('Recent API Errors', d.api_errors_recent,
        d.api_errors_recent ? 'a' : 'g'),
      kpi('Timezone', tzName(), 'm')),
    d.last_api_error ? el('div', { class: 'note' },
      `آخر خطأ API: ${d.last_api_error}`) : null,
    el('section', { class: 'card' }, el('h2', {}, 'Data Freshness'),
      table([
        { label: 'Source', key: 'key' },
        { label: 'Timestamp', render: (r) => ts(r.ts) },
        { label: 'Age', render: (r) => dur(r.age) },
        { label: 'State', render: (r) => isNil(r.ts) ? badge('NO DATA', 'unk')
            : badge(r.stale ? 'STALE' : 'FRESH', r.stale ? 'stale' : 'on') },
      ], frRows)),
    el('div', { class: 'note' },
      'المحرك لا يُعد ONLINE لمجرد وجود عملية — يلزم نبض حديث.')));
};

/* ── التدقيق ── */
PAGES.audit = async () => {
  const f = S.filters.au || (S.filters.au = {});
  const off = S.offsets.au || 0;
  let d;
  try { d = (await api.audit({ ...f, limit: 50, offset: off })).data; }
  catch (e) { return mount(errorState(e, render)); }

  const setF = (k) => (v) => {
    f[k] = v || undefined; S.offsets.au = 0; clearCache('/audit'); PAGES.audit();
  };
  const cols = [
    { label: 'Time', render: (r) => ts(r.ts), raw: (r) => r.ts },
    { label: 'Severity', raw: (r) => r.severity, render: (r) => badge(r.severity,
        r.severity === 'CRITICAL' ? 'off' : r.severity === 'HIGH' ? 'deg'
        : r.severity === 'WARNING' ? 'stale' : 'unk') },
    { label: 'Component', key: 'component' },
    { label: 'Event', key: 'event_type' },
    { label: 'Symbol', key: 'symbol' },
    { label: 'Message', key: 'message' },
  ];
  mount(el('div', {},
    el('div', { class: 'filters' },
      select('Severity', f.severity, [['', 'الكل'], 'CRITICAL', 'HIGH',
        'WARNING', 'INFO'], setF('severity')),
      select('Component', f.component, [['', 'الكل'], 'RISK', 'SYSTEM',
        'EXECUTION'], setF('component')),
      el('div', { class: 'fld' }, el('label', { for: 'au-s' }, 'Search'),
        el('input', { id: 'au-s', type: 'search', value: f.search || '',
          placeholder: 'نص، رمز، معرّف…',
          onChange: (e) => setF('search')(e.target.value) })),
      csvButton('audit.csv', cols, d.items)),
    el('div', { class: 'card' },
      d.items.length === 0
        ? el('div', { class: 'state' }, el('div', { class: 'big' }, 'NO EVENTS'))
        : table(cols, d.items, { caption: 'سجل التدقيق' }),
      d.total > d.limit ? pager(d.total, d.limit, d.offset, (o) => {
        S.offsets.au = o; clearCache('/audit'); PAGES.audit();
      }) : null)));
};

/* ── الإعدادات (عرض فقط) ── */
PAGES.settings = async () => {
  let d, meta;
  try {
    d = (await api.settings()).data;
    meta = (await api.meta()).data;
  } catch (e) { return mount(errorState(e, render)); }

  mount(el('div', {},
    el('div', { class: 'note' },
      '⚠ هذه الصفحة للعرض فقط. لا يمكن تعديل أي إعداد تداول من الموقع.'),
    el('section', { class: 'card' }, el('h2', {}, 'Trading Configuration'),
      details([
        field('Trading Mode', badge(d.trading_mode,
          d.trading_mode === 'PAPER' ? 'buy' : 'unk')),
        field('Mainnet Enabled', badge(String(d.mainnet_enabled), 'off')),
        field('Mainnet Note', d.mainnet_note),
        field('Exchange', d.exchange),
        field('Endpoint', d.endpoint),
        field('API Key Fingerprint', d.api_key_fingerprint),
        field('Read Only', badge(String(d.read_only), 'on')),
      ])),
    el('section', { class: 'card' }, el('h2', {}, 'Coverage'),
      details([
        field('Symbols', (d.supported_symbols || []).join(', ') || NA),
        field('Timeframes', (d.supported_timeframes || []).join(', ') || NA),
      ])),
    el('section', { class: 'card' }, el('h2', {}, 'Versions'),
      details([
        field('Strategy Version', d.strategy_version),
        field('Config Fingerprint', d.config_fingerprint),
        field('Dashboard Version', meta.dashboard_version),
        field('API Version', meta.api_version),
        field('Can Trade', badge(String(meta.can_trade), 'off')),
        field('Timezone', tzName()),
      ])),
    el('section', { class: 'card' }, el('h2', {}, 'Available Endpoints'),
      el('div', { class: 'tags' },
        (meta.endpoints || []).map((e) => el('span', { class: 'tag' }, `GET ${e}`))))));
};

/* ═══ شاشة الدخول ═══ */
function loginScreen(message = '') {
  const u = el('input', { id: 'lg-u', type: 'text', autocomplete: 'username',
    'aria-label': 'اسم المستخدم', placeholder: 'اسم المستخدم' });
  const p = el('input', { id: 'lg-p', type: 'password',
    autocomplete: 'current-password', 'aria-label': 'كلمة المرور',
    placeholder: 'كلمة المرور' });
  const msg = el('div', { class: 'msg', role: 'alert', 'aria-live': 'polite' },
    message);
  const submit = async () => {
    msg.replaceChildren('');
    try {
      await auth.login(u.value, p.value);
      await boot();
    } catch (e) {
      // لا يكشف وجود المستخدم — نفس النص لكل فشل
      msg.replaceChildren(e.message || 'فشل الدخول');
    }
  };
  const onKey = (ev) => { if (ev.key === 'Enter') submit(); };
  u.addEventListener('keydown', onKey);
  p.addEventListener('keydown', onKey);

  return el('div', { class: 'login-wrap' },
    el('form', { class: 'login', onSubmit: (e) => e.preventDefault() },
      el('h2', {}, 'Trading Intelligence'),
      el('p', {}, 'لوحة مراقبة — قراءة فقط، لا تنفّذ تداولاً'),
      el('div', { class: 'fld' },
        el('label', { for: 'lg-u' }, 'المستخدم'), u),
      el('div', { class: 'fld' },
        el('label', { for: 'lg-p' }, 'كلمة المرور'), p),
      el('button', { class: 'primary', type: 'button', onClick: submit },
        'دخول'),
      msg,
      el('div', { class: 'provenance' }, 'READ-ONLY · CANNOT TRADE')));
}

/* ═══ لافتة التحذيرات ═══ */
const WARN_TEXT = {
  STALE_DATA: 'البيانات قديمة — المحرك قد يكون متوقفاً',
  SCHEMA_MISMATCH: 'نسخة مخطط القاعدة مختلفة — الأرقام قد تكون خاطئة',
  SCHEMA_DEGRADED: 'جداول ناقصة في القاعدة — بيانات جزئية',
  INSUFFICIENT_SAMPLE: 'عينة صغيرة — لا يمكن استنتاج الربحية',
};

function warningBar() {
  const m = lastMeta.value;
  if (!m || !(m.warnings || []).length) return null;
  return el('div', { class: 'warn-bar', role: 'status' },
    el('span', {}, '⚠'),
    m.warnings.map((w) => el('span', { class: 'tag' }, WARN_TEXT[w] || w)));
}

/* ═══ الإقلاع ═══ */
window.addEventListener('hashchange', render);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) render();
});

async function boot() {
  clearTimeout(S.timer);
  let sess = null;
  try {
    sess = (await auth.session()).data;
  } catch (e) {
    if (e.status === 401) {
      document.getElementById('app').replaceChildren(loginScreen());
      return;
    }
  }
  if (sess && sess.auth_required && !sess.authenticated
      && !sess.anonymous_allowed) {
    document.getElementById('app').replaceChildren(loginScreen());
    return;
  }
  S.user = sess?.user || null;
  try { S.meta = (await api.meta()).data ? await api.meta() : null; }
  catch { /* يُعرض في الصفحة */ }
  await render();
}

boot();
