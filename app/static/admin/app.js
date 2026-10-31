/* ================================================================== *
 * 基础工具
 * ================================================================== */
const TOKEN_KEY = 'stland.admin.token';
const USER_KEY = 'stland.admin.user';
const $ = (id) => document.getElementById(id);
const state = { tab: 'overview', accounts: {}, cfg: [], cfgDirty: {} };

const token = () => localStorage.getItem(TOKEN_KEY) || '';

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: {
      'Content-Type': 'application/json',
      ...(token() ? { Authorization: 'Bearer ' + token() } : {}),
      ...(opts.headers || {}),
    },
  });
  let body = null;
  try { body = await res.json(); } catch { body = null; }
  if (!res.ok) {
    if (res.status === 401 && path !== '/api/admin/login') logout();
    const d = body && body.detail;
    const msg = typeof d === 'string' ? d : (d && d.message) || ('请求失败 ' + res.status);
    throw new Error(msg);
  }
  return body;
}

function banner(text, type = 'ok') {
  const el = $('banner');
  el.textContent = text;
  el.className = 'banner show ' + type;
  clearTimeout(banner._t);
  banner._t = setTimeout(() => { el.className = 'banner'; }, 4000);
}

const num = (v, d = 2) => (v == null || isNaN(v) ? '--' : Number(v).toLocaleString('zh-CN', {
  minimumFractionDigits: d, maximumFractionDigits: d,
}));
const int = (v) => (v == null ? '--' : Number(v).toLocaleString('zh-CN'));
const pct = (v) => (v == null ? '--' : (Number(v) > 0 ? '+' : '') + Number(v).toFixed(2) + '%');
const dt = (ts) => {
  if (!ts) return '--';
  const d = new Date(ts < 1e11 ? ts * 1000 : ts);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
};
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));

const RISK_LABEL = { 0: '未测评', 1: '保守型', 2: '稳健型', 3: '进取型' };
const STATUS_LABEL = { active: '正常', frozen: '冻结', closed: '注销' };
const STATUS_CLASS = { active: 'ok', frozen: 'warn', closed: 'bad' };
const ROLE_LABEL = { retail: '零售', institution: '机构' };

/* ================================================================== *
 * 登录 / 登出
 * ================================================================== */
$('loginForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = $('loginBtn');
  const err = $('loginErr');
  err.style.opacity = 0;
  btn.disabled = true;
  try {
    // 管理员走独立接口与独立表，不再复用 /api/auth/login
    const res = await api('/api/admin/login', {
      method: 'POST',
      body: JSON.stringify({ username: $('username').value.trim(), password: $('password').value }),
    });
    const t = res && res.token;
    const accessToken = typeof t === 'string' ? t : (t && t.access_token);
    if (!accessToken) throw new Error('登录响应异常：未拿到访问令牌');
    localStorage.setItem(TOKEN_KEY, accessToken);
    localStorage.setItem(USER_KEY, JSON.stringify(res.admin || {}));
    enterApp(res.admin);
  } catch (ex) {
    err.textContent = ex.message;
    err.style.opacity = 1;
  } finally {
    btn.disabled = false;
  }
});

function enterApp(admin) {
  $('login').style.display = 'none';
  $('app').style.display = 'block';
  const a = admin || JSON.parse(localStorage.getItem(USER_KEY) || '{}');
  $('who').textContent = (a.display_name || a.username || '') + (a.username ? ' · 管理员' : '');
  // 登录后回到「上次看的分区」：直接刷新页面时 hash 还在，不该被拉回总览
  const first = location.hash.replace(/^#/, '');
  showPage(PAGES[first] ? first : 'overview', false);
}

function logout() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
  $('app').style.display = 'none';
  $('login').style.display = 'grid';
}
$('logoutBtn').addEventListener('click', logout);

/* ================================================================== *
 * 抽屉导航
 *
 * 为什么把「横向页签」换成「侧边抽屉」：
 *   十个分区平铺成一行，宽度不够时会挤成两行小字，分组关系也看不出来；
 *   抽屉是纵向列表，分区天然有上下顺序，窄屏时按需滑出、不占正文宽度。
 *
 * 状态只有一个来源 state.tab：菜单高亮、分区显隐、URL hash 都从它派生，
 * 避免「点了一处、另外两处没跟着变」。
 * ================================================================== */
const PAGES = {
  overview: loadOverview,
  retail: () => loadAccounts('retail'),
  institution: () => loadAccounts('institution'),
  admins: loadAdmins,
  stocks: () => { loadStocks(); loadIndices(); },
  engine: loadEngine,
  risk: loadRisk,
  config: loadConfig,
  data: loadSeedStatus,
  ops: loadOps,
};

/** 窄屏判定：与 app.css 的断点保持一致 */
const isNarrow = () => window.matchMedia('(max-width: 900px)').matches;

function openSide() {
  $('shell')?.show('start');
  $('app')?.classList.add('side-open');
}

function closeSide() {
  $('shell')?.close('start');
  $('app')?.classList.remove('side-open');
}

/**
 * 切到某个分区。
 * @param {string} key   分区标识
 * @param {boolean} sync true 表示用户主动切换，需要同步地址栏 hash；
 *                       false 表示从 hash 还原，不再写回，避免压出多余历史。
 */
function showPage(key, sync = true) {
  if (!PAGES[key]) key = 'overview';
  state.tab = key;

  // 菜单高亮：Sober 的 s-menu-item 用 checked 属性表示选中，不是 class
  document.querySelectorAll('s-menu-item[data-page]').forEach((el) => {
    if (el.dataset.page === key) el.setAttribute('checked', '');
    else el.removeAttribute('checked');
  });

  document.querySelectorAll('section.page').forEach((s) => {
    s.classList.toggle('on', s.dataset.page === key);
  });

  // 顶栏标题跟着变；窄屏收起侧栏后，这是唯一能看出「在哪一页」的地方
  const cur = document.querySelector(`s-menu-item[data-page="${key}"]`);
  if (cur && $('pageTitle')) $('pageTitle').textContent = cur.textContent.trim();

  if (sync && location.hash !== '#' + key) {
    // replaceState 而非 pushState：后台是单页工具，浏览器「后退」应留给
    // 用户自己的导航意图，不该被十次点菜单塞满历史
    history.replaceState(null, '', '#' + key);
  }

  // 窄屏侧栏是覆盖层，选完自动收起，否则会一直盖住正文
  if (isNarrow()) closeSide();

  PAGES[key]?.();
}

$('navToggle').addEventListener('click', () => {
  if ($('shell')?.hasAttribute('data-open')) closeSide();
  else openSide();
});

// 直接用 click 监听：不同版本的 Sober 自定义事件名不一致，
// 而 data-page 是唯一真相，比依赖事件名稳
document.querySelector('s-menu')?.addEventListener('click', (e) => {
  const item = e.target.closest('s-menu-item[data-page]');
  if (item) showPage(item.dataset.page);
});

// 手改地址栏 hash 或用前进后退时跟着切页
window.addEventListener('hashchange', () => {
  const key = location.hash.replace(/^#/, '');
  if (key && key !== state.tab && PAGES[key]) showPage(key, false);
});

/* ================================================================== *
 * 总览
 * ================================================================== */
async function loadOverview() {
  try {
    const d = await api('/api/admin/dashboard');
    const k = d.kpi || {};
    $('sessionInfo').textContent = d.session
      ? `${d.session.label} · ${d.session.trade_date} · ${d.session.can_trade ? '可交易' : '不可交易'}`
      : '';
    const items = [
      ['交易账户', k.user_count], ['零售', k.retail_count], ['机构', k.institution_count],
      ['管理员', k.admin_count], ['正常', k.active_users], ['冻结', k.frozen_users],
      ['总资产', num(k.total_asset)], ['今日委托', k.today_orders], ['今日成交', k.today_trades],
      ['今日成交额', num(k.today_turnover)], ['挂单中', k.open_orders], ['自选条数', k.watch_items],
    ];
    $('kpis').innerHTML = items.map(([a, v]) =>
      `<div class="kpi"><div class="k">${a}</div><div class="v">${v ?? '--'}</div></div>`).join('');

    $('roleDist').innerHTML = [
      ['零售账户', k.retail_count], ['机构账户', k.institution_count], ['管理员', k.admin_count],
    ].map(([a, v]) => `<div class="dist-item"><div class="k">${a}</div><div class="v">${v ?? 0}</div></div>`).join('');

    renderRiskDist('ovRisk', d.risk_distribution || {});
  } catch (e) { banner(e.message, 'err'); }
}
$('refreshOv').addEventListener('click', loadOverview);

function renderRiskDist(target, d) {
  const items = [
    ['账户总数', d.total], ['未测评', d.unassessed], ['保守型', d.conservative],
    ['稳健型', d.balanced], ['进取型', d.aggressive],
  ];
  $(target).innerHTML = items.map(([a, v]) =>
    `<div class="dist-item"><div class="k">${a}</div><div class="v">${v ?? 0}</div></div>`).join('');
}

/* ================================================================== *
 * 交易账户（零售 / 机构共用一张表，按 role 过滤）
 * ================================================================== */
async function loadAccounts(role) {
  const isInst = role === 'institution';
  const body = $(isInst ? 'instBody' : 'retailBody');
  const searchEl = $(isInst ? 'instSearch' : 'retailSearch');
  const countEl = $(isInst ? 'instCount' : 'retailCount');
  body.innerHTML = '<tr><td colspan="8" class="empty">加载中…</td></tr>';
  try {
    const kw = searchEl.value.trim();
    const d = await api(`/api/admin/accounts?role=${role}&keyword=${encodeURIComponent(kw)}`);
    state.accounts[role] = d.items || [];
    countEl.textContent = `共 ${d.total ?? 0} 个`;
    if (!d.items.length) {
      body.innerHTML = '<tr><td colspan="8" class="empty">该分类下暂无账户</td></tr>';
      return;
    }
    body.innerHTML = d.items.map((a) => `
      <tr>
        <td><strong>${esc(a.display_name || a.username)}</strong>
          <span class="muted" style="font-size:10px"> · ${esc(a.username)}</span></td>
        <td><span class="pill ${STATUS_CLASS[a.status] || ''}">${STATUS_LABEL[a.status] || a.status}</span></td>
        <td class="num">${num(a.cash)}</td>
        <td class="num">${isInst ? num(a.credit_limit) : num(a.market_value)}</td>
        <td class="num">${num(a.total_asset)}</td>
        <td class="num">${a.positions ?? 0}</td>
        <td><span class="pill ${a.risk_assessed ? 'info' : ''}">${a.risk_assessed ? RISK_LABEL[a.risk_level] : '未测评'}</span></td>
        <td>
          <button class="btn btn-ghost" data-adjust="${a.id}" data-name="${esc(a.username)}">调资金</button>
          <button class="btn btn-ghost" data-frozen="${a.id}" data-name="${esc(a.username)}"
            data-status="${a.status}">${a.status === 'frozen' ? '解冻' : '冻结'}</button>
          <button class="btn btn-danger" data-delacc="${a.id}" data-name="${esc(a.username)}">删除</button>
        </td>
      </tr>`).join('');
  } catch (e) {
    body.innerHTML = `<tr><td colspan="8" class="empty">${esc(e.message)}</td></tr>`;
  }
}

$('retailSearch').addEventListener('input', () => {
  clearTimeout($('retailSearch')._t);
  $('retailSearch')._t = setTimeout(() => loadAccounts('retail'), 350);
});
$('instSearch').addEventListener('input', () => {
  clearTimeout($('instSearch')._t);
  $('instSearch')._t = setTimeout(() => loadAccounts('institution'), 350);
});

document.querySelectorAll('button[data-newacc]').forEach((b) => {
  b.addEventListener('click', () => {
    const role = b.dataset.newacc;
    $('nRole').value = role;
    $('accModalTitle').textContent = role === 'institution' ? '新建机构账户' : '新建零售账户';
    $('creditField').style.display = role === 'institution' ? '' : 'none';
    $('accModal').classList.add('show');
  });
});

['retailBody', 'instBody'].forEach((id) => {
  $(id).addEventListener('click', async (e) => {
    const role = id === 'instBody' ? 'institution' : 'retail';

    const adj = e.target.closest('button[data-adjust]');
    if (adj) {
      const raw = prompt(`给 ${adj.dataset.name} 调整可用资金\n正数入金，负数出金（例如 10000 或 -5000）`, '10000');
      if (raw === null) return;
      const amount = Number(raw);
      if (!amount || isNaN(amount)) { banner('请输入有效的调整金额', 'err'); return; }
      try {
        await api(`/api/admin/accounts/${adj.dataset.adjust}/adjust`, {
          method: 'POST', body: JSON.stringify({ amount, remark: '管理端调整' }),
        });
        banner('资金已调整');
        loadAccounts(role);
      } catch (err) { banner(err.message, 'err'); }
      return;
    }

    const frz = e.target.closest('button[data-frozen]');
    if (frz) {
      const next = frz.dataset.status === 'frozen' ? 'active' : 'frozen';
      try {
        await api(`/api/admin/accounts/${frz.dataset.frozen}/status`, {
          method: 'POST', body: JSON.stringify({ status: next }),
        });
        banner(next === 'frozen' ? '账户已冻结' : '账户已解冻');
        loadAccounts(role);
      } catch (err) { banner(err.message, 'err'); }
      return;
    }

    const del = e.target.closest('button[data-delacc]');
    if (del) {
      if (!confirm(`确认删除账户 ${del.dataset.name}？\n会一并清除其持仓、委托、流水与自选，且不可撤销。`)) return;
      try {
        await api(`/api/admin/accounts/${del.dataset.del}`, { method: 'DELETE' });
        banner('账户已删除');
        loadAccounts(role);
      } catch (err) { banner(err.message, 'err'); }
    }
  });
});

$('accCancel').addEventListener('click', () => $('accModal').classList.remove('show'));
$('accForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const role = $('nRole').value;
  try {
    await api('/api/admin/accounts', {
      method: 'POST',
      body: JSON.stringify({
        username: $('nUsername').value.trim(),
        display_name: $('nDisplay').value.trim(),
        password: $('nPassword').value,
        role,
        cash: Number($('nCash').value || 0),
        credit_limit: role === 'institution' ? Number($('nCredit').value || 0) : 0,
      }),
    });
    $('accModal').classList.remove('show');
    $('accForm').reset();
    banner('账户已创建');
    loadAccounts(role);
  } catch (err) { banner(err.message, 'err'); }
});

/* ================================================================== *
 * 管理员（独立表）
 * ================================================================== */
async function loadAdmins() {
  const body = $('adminBody');
  body.innerHTML = '<tr><td colspan="6" class="empty">加载中…</td></tr>';
  try {
    const d = await api('/api/admin/admins');
    $('adminCount').textContent = `共 ${d.total ?? 0} 个`;
    if (!d.items.length) {
      body.innerHTML = '<tr><td colspan="6" class="empty">暂无管理员</td></tr>';
      return;
    }
    body.innerHTML = d.items.map((a) => `
      <tr>
        <td><strong>${esc(a.username)}</strong></td>
        <td>${esc(a.display_name || '--')}</td>
        <td><span class="pill ${a.is_active ? 'ok' : 'bad'}">${a.is_active ? '启用' : '停用'}</span></td>
        <td class="muted">${dt(a.last_login_at)}</td>
        <td class="muted">${dt(a.created_at)}</td>
        <td>
          <button class="btn btn-ghost" data-admstat="${a.id}" data-active="${a.is_active}">
            ${a.is_active ? '停用' : '启用'}</button>
          <button class="btn btn-danger" data-deladm="${a.id}" data-name="${esc(a.username)}">删除</button>
        </td>
      </tr>`).join('');
  } catch (e) {
    body.innerHTML = `<tr><td colspan="6" class="empty">${esc(e.message)}</td></tr>`;
  }
}

$('adminBody').addEventListener('click', async (e) => {
  const st = e.target.closest('button[data-admstat]');
  if (st) {
    const next = st.dataset.active !== 'true';
    try {
      await api(`/api/admin/admins/${st.dataset.admstat}/status`, {
        method: 'POST', body: JSON.stringify({ is_active: next }),
      });
      banner(next ? '管理员已启用' : '管理员已停用');
      loadAdmins();
    } catch (err) { banner(err.message, 'err'); }
    return;
  }
  const del = e.target.closest('button[data-deladm]');
  if (del) {
    if (!confirm(`确认删除管理员 ${del.dataset.name}？`)) return;
    try {
      await api(`/api/admin/admins/${del.dataset.del}`, { method: 'DELETE' });
      banner('管理员已删除');
      loadAdmins();
    } catch (err) { banner(err.message, 'err'); }
  }
});

$('newAdminBtn').addEventListener('click', () => $('adminModal').classList.add('show'));
$('adminCancel').addEventListener('click', () => $('adminModal').classList.remove('show'));
$('adminForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  try {
    await api('/api/admin/admins', {
      method: 'POST',
      body: JSON.stringify({
        username: $('aUsername').value.trim(),
        display_name: $('aDisplay').value.trim(),
        password: $('aPassword').value,
      }),
    });
    $('adminModal').classList.remove('show');
    $('adminForm').reset();
    banner('管理员已创建');
    loadAdmins();
  } catch (err) { banner(err.message, 'err'); }
});

$('pwdForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  try {
    await api('/api/admin/password', {
      method: 'POST',
      body: JSON.stringify({ old_password: $('pwdOld').value, new_password: $('pwdNew').value }),
    });
    $('pwdForm').reset();
    banner('口令已更新');
  } catch (err) { banner(err.message, 'err'); }
});

/* ================================================================== *
 * 标的 / 指数
 * ================================================================== */
async function loadStocks() {
  const body = $('stockBody');
  body.innerHTML = '<tr><td colspan="11" class="empty">加载中…</td></tr>';
  try {
    const d = await api('/api/admin/stocks');
    const rows = d.items || [];
    $('stockCount').textContent = `共 ${rows.length} 支`;
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="11" class="empty">库中暂无标的，去「数据初始化」注入默认标的</td></tr>';
      return;
    }
    body.innerHTML = rows.map((s) => {
      const chg = s.change_pct;
      const cls = chg > 0 ? 'ok' : chg < 0 ? 'bad' : '';
      return `<tr>
        <td>${esc(s.code)}</td><td>${esc(s.name)}</td>
        <td>${esc(s.board || '--')}</td><td>${esc(s.industry || '--')}</td>
        <td class="num">${s.price != null ? num(s.price) : num(s.prev_close)}</td>
        <td class="num"><span class="pill ${cls}">${chg != null ? pct(chg) : '--'}</span></td>
        <td class="num">${num(s.limit_up)}</td><td class="num">${num(s.limit_down)}</td>
        <td class="num">${num(s.vol)}</td><td class="num">${num(s.beta)}</td>
        <td><button class="btn btn-danger" data-dels="${esc(s.code)}" data-name="${esc(s.name)}">删除</button></td>
      </tr>`;
    }).join('');
  } catch (e) {
    body.innerHTML = `<tr><td colspan="11" class="empty">${esc(e.message)}</td></tr>`;
  }
}

async function loadIndices() {
  const body = $('idxBody');
  body.innerHTML = '<tr><td colspan="9" class="empty">加载中…</td></tr>';
  try {
    const d = await api('/api/admin/indices');
    const rows = d.items || [];
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="9" class="empty">暂无指数</td></tr>';
      return;
    }
    body.innerHTML = rows.map((i) => {
      const cls = i.change_pct > 0 ? 'ok' : i.change_pct < 0 ? 'bad' : '';
      return `<tr>
        <td>${esc(i.code)}</td><td>${esc(i.name)}</td>
        <td class="num">${num(i.price)}</td>
        <td class="num"><span class="pill ${cls}">${pct(i.change_pct)}</span></td>
        <td class="num">${i.members ?? 0}</td>
        <td class="num">${i.up ?? 0}</td><td class="num">${i.flat ?? 0}</td><td class="num">${i.down ?? 0}</td>
        <td class="num">${num(i.amount)}</td>
      </tr>`;
    }).join('');
  } catch (e) {
    body.innerHTML = `<tr><td colspan="9" class="empty">${esc(e.message)}</td></tr>`;
  }
}

$('refreshStocks').addEventListener('click', loadStocks);
$('refreshIdx').addEventListener('click', loadIndices);

$('stockBody').addEventListener('click', async (e) => {
  const b = e.target.closest('button[data-dels]');
  if (!b) return;
  if (!confirm(`确认删除标的 ${b.dataset.dels} ${b.dataset.name}？\n会一并清除该标的的行情与自选成分，不可撤销；有持仓时会被拒绝，请先处理持仓。`)) return;
  try {
    await api(`/api/admin/stocks/${encodeURIComponent(b.dataset.dels)}`, { method: 'DELETE' });
    banner('标的已删除');
    loadStocks();
  } catch (err) { banner(err.message, 'err'); }
});

$('newStockBtn').addEventListener('click', () => $('stockModal').classList.add('show'));
$('stockCancel').addEventListener('click', () => $('stockModal').classList.remove('show'));
$('stockForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  try {
    await api('/api/admin/stocks', {
      method: 'POST',
      body: JSON.stringify({
        code: $('sCode').value.trim(),
        name: $('sName').value.trim(),
        board: $('sBoard').value,
        industry: $('sIndustry').value.trim(),
        init_price: Number($('sPrice').value || 10),
        vol: Number($('sVol').value || 0.35),
      }),
    });
    $('stockModal').classList.remove('show');
    $('stockForm').reset();
    banner('标的已创建');
    loadStocks();
  } catch (err) { banner(err.message, 'err'); }
});

/* ================================================================== *
 * 风险偏好（只读分布）
 * ================================================================== */
async function loadRisk() {
  try {
    const d = await api('/api/admin/risk-summary');
    renderRiskDist('riskDist', d);
  } catch (e) { banner(e.message, 'err'); }
}
$('refreshRisk').addEventListener('click', loadRisk);

/* ================================================================== *
 * 模拟引擎（运行态 + 启停 + 重置）
 * ================================================================== */
async function loadEngine() {
  $('engineKpis').innerHTML = '<div class="empty">加载中…</div>';
  try {
    const d = await api('/api/admin/engine');
    state.engine = d;
    const s = d.session || {};
    $('engineModeTag').textContent = `${s.mode_label || s.mode || ''} · ${s.label || ''}`;

    const toggleTxt = d.enabled ? '关闭引擎' : '开启引擎';
    $('toggleEngine').textContent = toggleTxt;
    $('toggleEngine').classList.toggle('btn-danger', d.enabled);
    $('toggleEngine').classList.toggle('btn-primary', !d.enabled);

    const items = [
      ['引擎开关', d.enabled ? '已开启' : '已关闭'],
      ['价格推进', d.advancing ? '推进中' : '未推进'],
      ['交易模式', s.mode_label || s.mode || '--'],
      ['当前时段', s.label || '--'],
      ['可撮合', s.is_open ? '是' : '否'],
      ['可申报', s.can_trade ? '是' : '否'],
      ['推进间隔', `${d.tick_seconds ?? '--'} 秒`],
      ['推进次数', d.tick_count ?? '--'],
      ['标的数量', d.listed ?? '--'],
      ['本时段剩余', s.seconds_left ? `${Math.floor(s.seconds_left / 60)} 分钟` : '不限'],
    ];
    $('engineKpis').innerHTML = items.map(([a, v]) =>
      `<div class="kpi"><div class="k">${a}</div><div class="v">${v ?? '--'}</div></div>`).join('');

    const st = d.stats || {};
    $('engineSession').innerHTML = [
      ['交易日期', s.trade_date || '--'],
      ['阶段标识', s.phase || '--'],
      ['K线时段', s.segment || '--'],
      ['上涨', st.up ?? 0], ['下跌', st.down ?? 0],
      ['累计成交额', num(st.total_amount)],
    ].map(([a, v]) => `<div class="dist-item"><div class="k">${a}</div><div class="v">${v}</div></div>`).join('');

    // 参数速览：直接从系统配置里挑出与引擎相关的几项，避免再多发一次请求
    let cfg = state.cfg || [];
    if (!cfg.length) {
      try { const c = await api('/api/admin/settings'); state.cfg = c.items || []; cfg = state.cfg; } catch (e) { /* 忽略 */ }
    }
    const keys = ['trade_mode', 'tick_seconds', 'engine_enabled', 'market_volatility', 'market_drift'];
    const picked = cfg.filter((c) => keys.includes(c.key));
    $('engineParams').innerHTML = picked.length
      ? picked.map((c) => {
          let v = c.value;
          if (c.kind === 'bool') v = v ? '开启' : '关闭';
          else if (c.kind === 'choice') {
            const hit = (c.choice_labels || []).find((o) => o.value === c.value);
            v = hit ? hit.label : c.value;
          }
          return `<div class="dist-item"><div class="k">${esc(c.label)}</div><div class="v">${esc(String(v))}</div></div>`;
        }).join('')
      : '<div class="empty">暂无可展示参数</div>';
  } catch (e) {
    $('engineKpis').innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  }
}
$('refreshEngine').addEventListener('click', loadEngine);

$('toggleEngine').addEventListener('click', async () => {
  const cur = state.engine || {};
  const next = !cur.enabled;
  if (!confirm(next ? '开启行情引擎？价格将按当前交易模式的时段推进。' : '关闭行情引擎？价格会立即冻结，账户与持仓数据不受影响。')) return;
  try {
    const d = await api('/api/admin/engine/toggle', {
      method: 'POST', body: JSON.stringify({ enabled: next }),
    });
    banner(next ? '引擎已开启' : '引擎已关闭', 'ok');
    state.engine = d;
    loadEngine();
  } catch (e) { banner(e.message, 'err'); }
});

$('resetEngine').addEventListener('click', async () => {
  if (!confirm('重置行情快照？所有标的将回到昨收价，分钟线与累计成交清空。标的参数、账户资金与持仓不会变动。')) return;
  try {
    await api('/api/admin/engine/reset', { method: 'POST', body: '{}' });
    banner('行情已重置到昨收基准', 'ok');
    loadEngine();
  } catch (e) { banner(e.message, 'err'); }
});

/* ================================================================== *
 * 系统配置（运行期可改）
 * ================================================================== */
async function loadConfig() {
  const grid = $('cfgGrid');
  grid.innerHTML = '<div class="empty">加载中…</div>';
  try {
    const d = await api('/api/admin/settings');
    state.cfg = d.items || [];
    state.cfgDirty = {};
    if (!state.cfg.length) { grid.innerHTML = '<div class="empty">暂无可配置项</div>'; return; }
    grid.innerHTML = state.cfg.map((c) => {
      let control = '';
      if (c.kind === 'bool') {
        control = `<select data-cfg="${c.key}">
          <option value="true" ${c.value ? 'selected' : ''}>开启</option>
          <option value="false" ${!c.value ? 'selected' : ''}>关闭</option></select>`;
      } else if (c.kind === 'choice') {
        const opts = (c.choice_labels && c.choice_labels.length)
          ? c.choice_labels.map((o) => ({ value: o.value, label: o.label }))
          : c.choices.map((o) => ({ value: o, label: o }));
        control = `<select data-cfg="${c.key}">${opts.map((o) =>
          `<option value="${esc(o.value)}" ${o.value === c.value ? 'selected' : ''}>${esc(o.label)}</option>`).join('')}</select>`;
      } else {
        const step = c.kind === 'int' ? '1' : 'any';
        control = `<input type="number" step="${step}" data-cfg="${c.key}" value="${c.value}" />`;
      }
      const range = (c.kind === 'float' || c.kind === 'int')
        ? `${c.min} ~ ${c.max}${c.unit ? ' ' + c.unit : ''}` : '';
      return `<div class="cfg" id="cfg-${c.key}">
        <div class="t"><span class="lbl">${esc(c.label)}</span><span class="key">${esc(c.key)}</span></div>
        ${control}
        <div class="note">${esc(c.note || '')}${range ? ' · 范围 ' + esc(range) : ''} · 默认 ${esc(c.default)}</div>
      </div>`;
    }).join('');
    grid.querySelectorAll('[data-cfg]').forEach((el) => {
      el.addEventListener('input', () => {
        state.cfgDirty[el.dataset.cfg] = el.value;
        $('cfg-' + el.dataset.cfg).classList.add('dirty');
      });
      el.addEventListener('change', () => {
        state.cfgDirty[el.dataset.cfg] = el.value;
        $('cfg-' + el.dataset.cfg).classList.add('dirty');
      });
    });
  } catch (e) {
    grid.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  }
}

$('saveCfg').addEventListener('click', async () => {
  const vals = state.cfgDirty;
  if (!Object.keys(vals).length) { banner('没有需要保存的修改', 'err'); return; }
  try {
    const d = await api('/api/admin/settings', {
      method: 'PUT', body: JSON.stringify({ values: vals }),
    });
    const okN = (d.updated || []).length;
    const badN = (d.rejected || []).length;
    if (badN) {
      banner(`已保存 ${okN} 项；${badN} 项被拒绝：` + d.rejected.map((r) => r.reason).join('；'), 'err');
    } else {
      banner(`已保存 ${okN} 项配置，已立即生效`);
    }
    loadConfig();
  } catch (err) { banner(err.message, 'err'); }
});

$('resetCfg').addEventListener('click', async () => {
  if (!confirm('确认把全部运行期配置恢复为代码默认值？')) return;
  try {
    await api('/api/admin/settings/reset', { method: 'POST' });
    banner('已恢复默认配置');
    loadConfig();
  } catch (err) { banner(err.message, 'err'); }
});

/* ================================================================== *
 * 数据初始化
 * ================================================================== */
async function loadSeedStatus() {
  try {
    const d = await api('/api/admin/seed/status');
    const items = [
      ['标的数', d.stocks], ['交易账户', d.users], ['管理员', d.admins],
      ['日线条数', int(d.daily_bars)], ['是否已初始化', d.seeded ? '是' : '否'],
    ];
    $('seedStatus').innerHTML = items.map(([a, v]) =>
      `<div class="dist-item"><div class="k">${a}</div><div class="v">${v ?? 0}</div></div>`).join('');
  } catch (e) { banner(e.message, 'err'); }
}
$('refreshSeed').addEventListener('click', loadSeedStatus);

$('seedBtn').addEventListener('click', async (e) => {
  const btn = e.target;
  btn.disabled = true;
  try {
    const d = await api('/api/admin/seed', {
      method: 'POST',
      body: JSON.stringify({
        stocks: $('seedStocks').checked,
        users: $('seedUsers').checked,
        history: $('seedHistory').checked,
      }),
    });
    banner(`注入完成：标的 +${d.stocks_added}、账户 +${d.users_added}、日线 +${d.daily_bars_added}`);
    loadSeedStatus();
  } catch (err) { banner(err.message, 'err'); }
  finally { btn.disabled = false; }
});

/* ================================================================== *
 * 撮合质量
 * ================================================================== */
async function loadOps() {
  try {
    const m = await api('/api/admin/matching');
    const kpi = [
      ['委托总数', m.order_count], ['成交笔数', m.trade_count],
      ['撮成率', (m.fill_rate_pct ?? 0) + '%'], ['撤单率', (m.cancel_rate_pct ?? 0) + '%'],
      ['平均单笔成交额', num(m.avg_trade_amount)],
    ];
    $('matchKpi').innerHTML = kpi.map(([a, v]) =>
      `<div class="dist-item"><div class="k">${a}</div><div class="v">${v ?? 0}</div></div>`).join('');

    const rows = m.top_symbols || [];
    $('matchBody').innerHTML = rows.length ? rows.map((o) => `
      <tr><td>${esc(o.code)}</td><td>${esc(o.name || '')}</td>
      <td class="num">${o.trades}</td><td class="num">${num(o.amount)}</td>
      <td class="num">${int(o.volume)}</td></tr>`).join('')
      : '<tr><td colspan="5" class="empty">暂无成交</td></tr>';
  } catch (e) {
    $('matchBody').innerHTML = `<tr><td colspan="5" class="empty">${esc(e.message)}</td></tr>`;
  }
}
$('refreshOps').addEventListener('click', loadOps);

$('settleBtn').addEventListener('click', async () => {
  if (!confirm('确认执行日终结转？将解锁全部 T+1 持仓。')) return;
  try {
    await api('/api/admin/settle', { method: 'POST' });
    banner('结转完成');
    loadOps();
  } catch (e) { banner(e.message, 'err'); }
});

/* ================================================================== *
 * 启动
 * ================================================================== */
if (token()) {
  enterApp(null);
  // 校验令牌是否还有效：失效会自动回到登录页
  api('/api/admin/me').catch(() => {});
} else {
  $('login').style.display = 'grid';
}
