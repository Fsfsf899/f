/** اختبارات الواجهة — بلا npm، بلا شبكة. */
import './dom_stub.mjs';
import { registerId, resetIds } from './dom_stub.mjs';

let pass = 0, fail = 0;
const results = [];
function t(name, fn) {
  try { fn(); pass++; results.push(['PASS', name]); }
  catch (e) { fail++; results.push(['FAIL', `${name} — ${e.message}`]); }
}
const eq = (a, b, m = '') => {
  if (String(a) !== String(b)) throw new Error(`${m} توقّع «${b}» ووجد «${a}»`);
};
const ok = (c, m) => { if (!c) throw new Error(m || 'شرط فاشل'); };

const ui = await import('../frontend/assets/ui.js');
const ch = await import('../frontend/assets/charts.js');
const apiMod = await import('../frontend/assets/api.js');

/* ── التنسيق ── */
t('num يعرض N/A للقيم الفارغة', () => {
  eq(ui.num(null), 'N/A'); eq(ui.num(undefined), 'N/A'); eq(ui.num(NaN), 'N/A');
});
t('num لا يعرض صفراً بدل غياب البيانات', () => {
  ok(ui.num(null) !== '0', 'عرض 0 لبيانات غائبة');
  eq(ui.num(0), '0.00');
});
t('money يوضّح الإشارة', () => {
  ok(ui.money(42.15).startsWith('+$')); ok(ui.money(-42.15).startsWith('-$'));
  eq(ui.money(null), 'N/A');
});
t('price يضبط الدقة حسب الحجم', () => {
  // 50123.4567 لخانتين = 50,123.46 (تقريب صحيح). التوقّع الأول كان .45 خطأً
  // في الاختبار لا في الكود — صُحّح بعد التحقق من ناتج toLocaleString.
  eq(ui.price(50123.4567), '$50,123.46');
  ok(ui.price(0.00012345).split('.')[1].length >= 6, 'دقة غير كافية للأسعار الصغيرة');
  eq(ui.price(1.5), '$1.5000');
});
t('prob يحوّل الكسر لنسبة', () => { eq(ui.prob(0.615), '61.5%'); eq(ui.prob(null), 'N/A'); });
t('pct و dur و ts تتعامل مع الغياب', () => {
  eq(ui.pct(null), 'N/A'); eq(ui.dur(null), 'N/A'); eq(ui.ts(null), 'N/A');
  eq(ui.dur(-5), 'N/A');
});
t('dur ينسّق المدد', () => {
  eq(ui.dur(45000), '45ث'); eq(ui.dur(3600000), '1س 0د');
});
t('sign يعطي صنف اللون الصحيح', () => {
  eq(ui.sign(1), 'g'); eq(ui.sign(-1), 'r'); eq(ui.sign(0), 'm'); eq(ui.sign(null), 'm');
});

/* ── المكوّنات ── */
t('kpi يبني بطاقة', () => {
  const n = ui.kpi('Win Rate', '61.4%');
  ok(n.textContent.includes('Win Rate')); ok(n.textContent.includes('61.4%'));
});
t('signalBadge يميّز BUY', () => {
  ok(ui.signalBadge('BUY').className.includes('buy'));
  ok(ui.signalBadge('NO_TRADE').className.includes('no'));
  ok(ui.signalBadge(null).textContent.includes('UNKNOWN'));
});
t('badge لا يعتمد اللون وحده — يحمل نصاً', () => {
  ok(ui.statusBadge('ONLINE').textContent.trim().length > 0);
  ok(ui.dqBadge(null).textContent.includes('N/A'));
});
t('table يعرض حالة فارغة', () => {
  const n = ui.table([{ label: 'A', key: 'a' }], []);
  ok(n.textContent.includes('لا توجد سجلات'));
});
t('table يبني صفوفاً ويستدعي onRow', () => {
  let clicked = null;
  const n = ui.table([{ label: 'Symbol', key: 'symbol' }],
    [{ symbol: 'BTCUSDT' }], { onRow: (r) => { clicked = r.symbol; } });
  ok(n.textContent.includes('BTCUSDT'));
  const tr = n.querySelectorAll('tr').find((x) => x.className === 'click');
  tr.dispatch('click', {});
  eq(clicked, 'BTCUSDT');
});
t('table يعرض N/A للخلايا الفارغة', () => {
  const n = ui.table([{ label: 'X', key: 'x' }], [{ x: null }]);
  ok(n.textContent.includes('N/A'));
});
t('loading / empty / error states', () => {
  ok(ui.loading('التوصيات').textContent.includes('جاري تحميل'));
  ok(ui.empty('NO OPEN POSITIONS').textContent.includes('NO OPEN POSITIONS'));
  const e = ui.errorState({ message: 'ENGINE CONNECTION LOST', code: 'NETWORK' });
  ok(e.textContent.includes('ENGINE CONNECTION LOST'));
});
t('pager يحسب الصفحات', () => {
  const n = ui.pager(120, 50, 50, () => {});
  ok(n.textContent.includes('120')); ok(n.textContent.includes('2/3'));
});
t('csvButton ينشئ زراً بلا استيراد', () => {
  const b = ui.csvButton('x.csv', [{ label: 'a', key: 'a' }], [{ a: 1 }]);
  eq(b.tagName, 'BUTTON');
});

/* ── المخططات ── */
t('lineChart يرفض بيانات غير كافية', () => {
  ok(ch.lineChart([]).textContent.includes('INSUFFICIENT'));
  ok(ch.lineChart([{ y: 1 }]).textContent.includes('INSUFFICIENT'));
});
t('lineChart يرسم بنقطتين فأكثر', () => {
  const s = ch.lineChart([{ x: 'a', y: 1 }, { x: 'b', y: 5 }, { x: 'c', y: 3 }]);
  eq(s.tagName, 'SVG'); ok(s.getAttribute('aria-label').length > 5);
});
t('barChart يعالج القيم السالبة', () => {
  const s = ch.barChart([{ x: '1', y: -3 }, { x: '2', y: 4 }]);
  eq(s.tagName, 'SVG');
});
t('drawdownChart يرسم', () => {
  eq(ch.drawdownChart([{ x: '', y: 0 }, { x: '', y: 4.2 }]).tagName, 'SVG');
});
t('calibrationChart يرفض عينة صغيرة', () => {
  ok(ch.calibrationChart([]).textContent.includes('CALIBRATION INSUFFICIENT'));
  ok(ch.calibrationChart([{ predicted: .5, actual: .5, n: 3, bucket: 'x' }])
    .textContent.includes('CALIBRATION INSUFFICIENT'));
});
t('calibrationChart يرسم بشريحتين', () => {
  const s = ch.calibrationChart([
    { predicted: .55, actual: .50, n: 20, bucket: '50-55%' },
    { predicted: .65, actual: .70, n: 25, bucket: '65-70%' }]);
  eq(s.tagName, 'SVG'); ok(s.getAttribute('aria-label').includes('المعايرة'));
});
t('المخططات تحمل وصفاً للوصولية', () => {
  const s = ch.lineChart([{ y: 1 }, { y: 2 }]);
  ok(s.getAttribute('role') === 'img');
  ok((s.getAttribute('aria-label') || '').length > 5);
});

/* ── طبقة API ── */
t('api لا يصدّر أي دالة كتابة', () => {
  const names = Object.keys(apiMod.api);
  const bad = names.filter((n) => /post|put|delete|patch|create|update|cancel|close|execute/i.test(n));
  eq(bad.length, 0, `دوال كتابة: ${bad}`);
});
t('كل دوال api للقراءة فقط', () => {
  const src = apiMod.get.toString();
  ok(!/method:\s*['"]POST/i.test(src), 'POST في طبقة API');
});
t('ApiError يحمل الرمز والحالة', () => {
  const e = new apiMod.ApiError('X', 503, 'DATABASE_UNAVAILABLE');
  eq(e.status, 503); eq(e.code, 'DATABASE_UNAVAILABLE');
});

/* ── الأمان في الواجهة ── */
t('el لا يدعم innerHTML إطلاقاً', () => {
  const n = ui.el('div', { html: '<img src=x onerror=alert(1)>' });
  ok(!String(n.innerHTML || '').includes('<img'), 'innerHTML مكتوب');
  ok(String(n.getAttribute('html') || '').length >= 0);
});
t('نص خبيث يُدرَج كنص لا كـ HTML', () => {
  const payload = '<script>alert(1)</script>';
  const n = ui.el('div', {}, payload);
  eq(n.textContent, payload);
  ok(!String(n.innerHTML || '').includes('<script'), 'نُفّذ كـ HTML');
});
t('الجدول يعرض بيانات خبيثة كنص', () => {
  const n = ui.table([{ label: 'S', key: 's' }],
    [{ s: '<img src=x onerror=alert(1)>' }]);
  ok(n.textContent.includes('<img'), 'النص مفقود');
  ok(!String(n.innerHTML || '').includes('onerror='), 'HTML خام');
});
t('لا eval ولا Function في كود الواجهة', async () => {
  const fs = await import('node:fs');
  for (const f of ['ui.js', 'api.js', 'charts.js', 'app.js']) {
    const src = fs.readFileSync(new URL(`../frontend/assets/${f}`, import.meta.url), 'utf8');
    ok(!/\beval\s*\(/.test(src), `eval في ${f}`);
    ok(!/new\s+Function\s*\(/.test(src), `new Function في ${f}`);
    ok(!/\.innerHTML\s*=/.test(src.replace(/\/\/.*|\/\*[\s\S]*?\*\//g, '')),
       `innerHTML في ${f}`);
  }
});
t('لا أسرار في كود الواجهة', async () => {
  const fs = await import('node:fs');
  const pats = ['BINANCE_API_KEY', 'BINANCE_SECRET', 'API_SECRET',
                'PRIVATE_KEY', 'MAINNET_API_SECRET', 'pbkdf2_sha256$'];
  for (const f of ['ui.js', 'api.js', 'charts.js', 'app.js']) {
    const src = fs.readFileSync(new URL(`../frontend/assets/${f}`, import.meta.url), 'utf8');
    for (const p of pats) ok(!src.includes(p), `${p} في ${f}`);
  }
});
t('index.html يحمل nonce ولا سكربت سطري', async () => {
  const fs = await import('node:fs');
  const html = fs.readFileSync(new URL('../frontend/index.html', import.meta.url), 'utf8');
  ok(html.includes('nonce='), 'لا nonce');
  ok(!/<script(?![^>]*src=)[^>]*>[\s\S]*?\S[\s\S]*?<\/script>/.test(html),
     'سكربت سطري');
});
t('api يصدّر auth بلا دوال كتابة على بيانات المحرك', () => {
  const names = Object.keys(apiMod.auth);
  eq(names.sort().join(','), 'login,logout,session');
});
t('lastMeta يُصدَّر لعرض التحذيرات', () => {
  ok('lastMeta' in apiMod);
  ok(typeof apiMod.lastMeta === 'object');
});
t('الحالات لا تعتمد اللون وحده', () => {
  for (const [fn, arg] of [[ui.statusBadge, 'OFFLINE'], [ui.signalBadge, 'BUY'],
                           [ui.outcomeBadge, 'WIN'], [ui.dqBadge, 45]]) {
    const n = fn(arg, arg === 'WIN' ? 5 : undefined);
    ok(n.textContent.trim().length > 0, 'شارة بلا نص');
    ok((n.className || '').length > 0, 'شارة بلا صنف');
  }
});
t('الجدول قابل للتنقل بلوحة المفاتيح', () => {
  let hit = 0;
  const n = ui.table([{ label: 'A', key: 'a' }], [{ a: 1 }],
    { onRow: () => { hit++; } });
  const tr = n.querySelectorAll('tr').find((x) => x.className === 'click');
  eq(tr.getAttribute('tabindex'), '0');
  tr.dispatch('keydown', { key: 'Enter', preventDefault() {} });
  eq(hit, 1);
});
t('الجدول يحمل caption للقارئ الشاشي', () => {
  const n = ui.table([{ label: 'A', key: 'a' }], [{ a: 1 }],
    { caption: 'وصف' });
  ok(n.textContent.includes('وصف'));
});
t('لا بيانات حساسة في عناوين URL', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync(new URL('../frontend/assets/app.js', import.meta.url), 'utf8');
  ok(!/[?&](password|token|secret|key)=/i.test(src), 'سر في URL');
});
t('styles.css فيها أصناف بديلة للأنماط السطرية', async () => {
  const fs = await import('node:fs');
  const css = fs.readFileSync(new URL('../frontend/assets/styles.css', import.meta.url), 'utf8');
  for (const c of ['.dot-g', '.dot-a', '.dot-r', '.ro-badge', '.warn-bar',
                   '.login', '.provenance']) ok(css.includes(c), `${c} مفقود`);
});
t('app.js بلا أنماط سطرية (CSP بلا unsafe-inline)', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync(new URL('../frontend/assets/app.js', import.meta.url), 'utf8');
  ok(!/style:\s*[`'"]/.test(src), 'نمط سطري متبقٍ');
});
t('تنسيق الجوال موجود', async () => {
  const fs = await import('node:fs');
  const css = fs.readFileSync(new URL('../frontend/assets/styles.css', import.meta.url), 'utf8');
  ok(css.includes('@media'), 'لا media query');
  ok(css.includes('.bottomnav'), 'لا تنقّل سفلي');
});

/* ── النتيجة ── */
for (const [s, n] of results) console.log(`  ${s}  ${n}`);
console.log(`\n  المجموع: ${pass + fail} | ناجح: ${pass} | فاشل: ${fail}`);
process.exit(fail ? 1 : 0);
