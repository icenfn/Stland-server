/* ==================================================================
 * Stland 运营后台脚本
 *
 * 界面全部由 mdui 2 组件构成：顶栏、抽屉、列表、卡片、表格、对话框、
 * 输入框、开关都直接使用 <mdui-*> 标签与它自带的 .mdui-table 皮肤，
 * 不再手写任何配色/圆角/阴影样式，页面里只剩分区显隐和间距。
 *
 * 这里只负责三件事：取数、把数据塞进组件、把用户操作发给接口。
 * ================================================================== */

const TOKEN_KEY = 'stland.admin.token';
const USER_KEY = 'stland.admin.user';

/** 组件实例要等自定义元素升级完成才能取到属性，先统一拿元素 */
const $ = (id) => document.getElementById(id);

const state = {
  page: 'overview',
  accounts: {},
  cfg: [],
  cfgDirty: {},
  engine: null,
  adjustAccount: null,
  editStock: null,
};

const token = () => localStorage.getItem(TOKEN_KEY) || '';

/* ------------------------------------------------------------------ *
 * 取数与提示
 * ------------------------------------------------------------------ */
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

/** 提示：用 mdui 的 snackbar；万一组件没起来就退回原生 alert */
function notify(text) {
  try {
    if (window.mdui && typeof mdui.snackbar === 'function') {
      mdui.snackbar({ message: String(text) });
      return;
    }
  } catch { /* 落到兜底 */ }
  window.alert(String(text));
}

/** 确认框：mdui.confirm 返回 Promise，原生 confirm 是同步值，这里统一成 Promise<boolean> */
function ask(text, headline = '请确认') {
  try {
    if (window.mdui && typeof mdui.confirm === 'function') {
      return Promise.resolve(mdui.confirm({
        headline,
        description: String(text),
        confirmText: '确定',
        cancelText: '取消',
      })).then((v) => !!v).catch(() => false);
    }
  } catch { /* 落到兜底 */ }
  return Promise.resolve(window.confirm(String(text)));
}

/* ------------------------------------------------------------------ *
 * 格式化
 * ------------------------------------------------------------------ */
const num = (v, d = 2) => (v == null || isNaN(v) ? '--' : Number(v).toLocaleString('zh-CN', {
  minimumFractionDigits: d, maximumFractionDigits: d,
}));
const int = (v) => (v == null || isNaN(v) ? '--' : Number(v).toLocaleString('zh-CN'));
const pct = (v) => (v == null || isNaN(v) ? '--' : (Number(v) > 0 ? '+' : '') + Number(v).toFixed(2) + '%');
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
const ROLE_LABEL = { retail: '零售', institution: '机构' };

/** 指标小卡：mdui-card 承载，样式全部来自组件 */
const kpiCard = (label, value) => `
  <mdui-card style="padding:12px">
    <div style="font-size:12px; opacity:.7">${esc(label)}</div>
    <div style="font-size:18px; font-weight:600; margin-top:4px">${esc(value ?? '--')}</div>
  </mdui-card>`;

/** 表格里的行内操作按钮 */
const rowBtn = (text, attr, extra = '', danger = false) =>
  `<mdui-button variant="text" ${danger ? 'style="--mdui-color-primary: var(--mdui-color-error)"' : ''} ${attr} ${extra}>${esc(text)}</mdui-button>`;

/** 空态/加载态占位行 */
const emptyRow = (cols, text) =>
  `<tr><td colspan="${cols}" style="text-align:center; opacity:.6; padding:24px">${esc(text)}</td></tr>`;

/* ------------------------------------------------------------------ *
 * 登录 / 登出
 * ------------------------------------------------------------------ */
$('loginForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = $('loginBtn');
  const err = $('loginErr');
  err.textContent = '';
  btn.disabled = true;
  try {
    // 管理员走独立接口与独立表
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
  } finally {
    btn.disabled = false;
  }
});

function enterApp(admin) {
  $('login').hidden = true;
  $('app').hidden = false;
  const a = admin || JSON.parse(localStorage.getItem(USER_KEY) || '{}');
  $('who').textContent = a.display_name || a.username || '';
  // 刷新页面时 hash 还在，就该停在上次看的分区，而不是被拉回总览
  const first = location.hash.replace(/^#/, '');
  showPage(PAGES[first] ? first : 'overview', false);
}

function logout() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
  $('app').hidden = true;
  $('login').hidden = false;
}

$('logoutBtn').addEventListener('click', async () => {
  if (await ask('确认退出登录？', '退出')) logout();
});

/* ------------------------------------------------------------------ *
 * 抽屉导航
 *
 * 十个分区排成横向页签时，窄屏会挤成两行小字，也看不出分组；
 * 换成纵向抽屉后宽屏常驻、窄屏滑出，正文宽度不被挤压。
 * 单一状态源 state.page：列表高亮、分区显隐、URL hash 都由它派生。
 * ------------------------------------------------------------------ */
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

const PAGE_TITLE = {
  overview: '总览', retail: '零售账户', institution: '机构账户', admins: '管理员',
  stocks: '标的与指数', engine: '模拟引擎', risk: '风险偏好', config: '系统配置',
  data: '数据初始化', ops: '撮合质量',
};

const navEl = () => $('nav');

function showPage(key, syncHash = true) {
  if (!PAGES[key]) key = 'overview';
  state.page = key;

  document.querySelectorAll('.page').forEach((el) => {
    el.classList.toggle('on', el.id === 'page-' + key);
  });
  navEl().querySelectorAll('mdui-list-item').forEach((el) => {
    el.active = el.dataset.page === key;
  });
  $('pageTitle').textContent = PAGE_TITLE[key] || '';

  // 窄屏点完就收起抽屉；宽屏保持常驻侧栏
  if (window.matchMedia('(max-width: 900px)').matches) navEl().open = false;

  if (syncHash) history.replaceState(null, '', '#' + key);
  PAGES[key]();
}

navEl().addEventListener('click', (e) => {
  const item = e.target.closest('mdui-list-item');
  if (!item || !item.dataset.page) return;
  showPage(item.dataset.page);
});

$('navToggle').addEventListener('click', () => {
  const n = navEl();
  n.open = !n.open;
});

window.addEventListener('hashchange', () => {
  const key = location.hash.replace(/^#/, '');
  if (PAGES[key] && key !== state.page) showPage(key, false);
});

/* ------------------------------------------------------------------ *
 * 总览
 * ------------------------------------------------------------------ */
async function loadOverview() {
  $('ovKpi').innerHTML = emptyRow(1, '加载中…');
  try {
    const d = await api('/api/admin/dashboard');
    const k = d.kpi || {};
    const items = [
      ['交易账户', k.user_count], ['零售', k.retail_count], ['机构', k.institution_count],
      ['管理员', k.admin_count], ['正常', k.active_users], ['冻结', k.frozen_users],
      ['总资产', num(k.total_asset)], ['今日委托', k.today_orders], ['今日成交', k.today_trades],
      ['今日成交额', num(k.today_turnover)], ['挂单中', k.open_orders], ['自选条数', k.watch_items],
    ];
    $('ovKpi').innerHTML = items.map(([a, v]) => kpiCard(a, v ?? 0)).join('');

    const s = d.session || {};
    const market = [
      ['交易模式', s.mode_label || s.mode || '--'],
      ['当前时段', s.label || '--'],
      ['交易日', s.trade_date || '--'],
      ['可申报', s.can_trade ? '是' : '否'],
      ['可撮合', s.is_open ? '是' : '否'],
    ];
    $('ovMarket').innerHTML = market.map(([a, v]) => kpiCard(a, v)).join('');
  } catch (e) {
    $('ovKpi').innerHTML = `<div style="opacity:.6">${esc(e.message)}</div>`;
  }
}
$('refreshOverview').addEventListener('click', loadOverview);

/* ------------------------------------------------------------------ *
 * 账户（零售 / 机构）
 * ------------------------------------------------------------------ */
async function loadAccounts(role) {
  const isInst = role === 'institution';
  const body = $(isInst ? 'instBody' : 'retailBody');
  const countEl = $(isInst ? 'instTotal' : 'retailTotal');
  const cols = isInst ? 8 : 9;
  body.innerHTML = emptyRow(cols, '加载中…');
  try {
    const d = await api(`/api/admin/accounts?role=${role}`);
    state.accounts[role] = d.items || [];
    countEl.textContent = `共 ${d.total ?? 0} 个`;
    if (!d.items.length) {
      body.innerHTML = emptyRow(cols, '该分类下暂无账户');
      return;
    }
    body.innerHTML = d.items.map((a) => {
      const pnl = a.floating_pnl;
      const pnlStyle = pnl > 0 ? 'color: var(--mdui-color-error)'
        : pnl < 0 ? 'color: var(--mdui-color-tertiary)' : '';
      const actions = `
        ${rowBtn('调资金', `data-fund="${a.id}"`, `data-name="${esc(a.username)}"`)}
        ${rowBtn('重置口令', `data-pwd="${a.id}"`, `data-name="${esc(a.username)}"`)}
        ${a.status === 'active'
          ? rowBtn('冻结', `data-status="${a.id}"`, 'data-next="frozen"', true)
          : rowBtn('解冻', `data-status="${a.id}"`, 'data-next="active"')}
      `;
      if (isInst) {
        return `<tr>
          <td>${esc(a.display_name || a.username)}<div style="font-size:11px;opacity:.6">${esc(a.username)}</div></td>
          <td>${STATUS_LABEL[a.status] || a.status}</td>
          <td class="num">${num(a.credit_limit)}</td>
          <td class="num">${num(a.cash)}</td>
          <td class="num">${num(a.market_value)}</td>
          <td class="num">${num(a.total_asset)}</td>
          <td>${actions}</td>
        </tr>`;
      }
      return `<tr>
        <td>${esc(a.display_name || a.username)}<div style="font-size:11px;opacity:.6">${esc(a.username)}</div></td>
        <td>${esc(a.display_name ? a.username : '')}</td>
        <td>${STATUS_LABEL[a.status] || a.status}</td>
        <td>${RISK_LABEL[a.risk_level] || '未测评'}</td>
        <td class="num">${num(a.cash)}</td>
        <td class="num">${num(a.market_value)}</td>
        <td class="num">${num(a.total_asset)}</td>
        <td class="num" style="${pnlStyle}">${num(pnl)}</td>
        <td>${actions}</td>
      </tr>`;
    }).join('');
  } catch (e) {
    body.innerHTML = emptyRow(cols, e.message);
  }
}
$('refreshRetail').addEventListener('click', () => loadAccounts('retail'));
$('refreshInstitution').addEventListener('click', () => loadAccounts('institution'));

/* 行内操作统一用事件委托：表格是整块重绘的，逐个绑定会被冲掉 */
document.addEventListener('click', async (e) => {
  const btn = e.target.closest('mdui-button');
  if (!btn) return;
  const ds = btn.dataset || {};

  if (ds.fund) {
    state.adjustAccount = ds.fund;
    $('ajCash').value = '';
    $('dlgAdjust').open = true;
    return;
  }
  if (ds.pwd) {
    const pw = window.prompt(`为 ${ds.name} 设置新口令（至少 8 位）`);
    if (!pw) return;
    try {
      await api(`/api/admin/accounts/${ds.pwd}/password`, {
        method: 'PUT', body: JSON.stringify({ password: pw }),
      });
      notify('口令已重置');
    } catch (err) { notify(err.message); }
    return;
  }
  if (ds.status) {
    try {
      await api(`/api/admin/accounts/${ds.status}/status`, {
        method: 'PUT', body: JSON.stringify({ status: ds.next }),
      });
      notify(ds.next === 'frozen' ? '已冻结' : '已解冻');
      loadAccounts(state.page);
    } catch (err) { notify(err.message); }
    return;
  }
  if (ds.deladm) {
    if (!(await ask(`确认删除管理员 ${ds.name}？`, '删除管理员'))) return;
    try {
      await api(`/api/admin/admins/${ds.deladm}`, { method: 'DELETE' });
      notify('已删除');
      loadAdmins();
    } catch (err) { notify(err.message); }
    return;
  }
  if (ds.admstat) {
    try {
      await api(`/api/admin/admins/${ds.admstat}/status`, {
        method: 'PUT', body: JSON.stringify({ is_active: ds.active !== 'true' }),
      });
      notify('状态已更新');
      loadAdmins();
    } catch (err) { notify(err.message); }
    return;
  }
  if (ds.dels) {
    if (!(await ask(`确认删除标的 ${ds.name}？`, '删除标的'))) return;
    try {
      await api(`/api/admin/stocks/${encodeURIComponent(ds.dels)}`, { method: 'DELETE' });
      notify('已删除');
      loadStocks();
    } catch (err) { notify(err.message); }
    return;
  }
  if (ds.edits) {
    const s = (state.stocks || []).find((x) => x.code === ds.edits);
    if (!s) return;
    state.editStock = s;
    $('stCode').value = s.code || '';
    $('stName').value = s.name || '';
    $('stBoard').value = s.board || '';
    $('stIndustry').value = s.industry || '';
    $('stPrevClose').value = s.prev_close ?? '';
    $('stLimitUp').value = s.limit_up ?? '';
    $('stLimitDown').value = s.limit_down ?? '';
    $('dlgStock').open = true;
  }
});

/* 新增账户 */
$('addRetail').addEventListener('click', () => openAccountDialog('retail'));
$('addInstitution').addEventListener('click', () => openAccountDialog('institution'));

function openAccountDialog(role) {
  $('acUsername').value = '';
  $('acDisplay').value = '';
  $('acCash').value = role === 'institution' ? '50000000' : '1000000';
  $('acPassword').value = '';
  $('acRole').value = role;
  $('dlgAccount').open = true;
}

$('acCancel').addEventListener('click', () => { $('dlgAccount').open = false; });

$('acSubmit').addEventListener('click', async () => {
  const payload = {
    username: $('acUsername').value.trim(),
    display_name: $('acDisplay').value.trim(),
    initial_cash: Number($('acCash').value || 0),
    role: $('acRole').value || 'retail',
    password: $('acPassword').value,
  };
  if (!payload.username) { notify('请填写用户名'); return; }
  if (payload.password.length < 8) { notify('口令至少 8 位'); return; }
  try {
    await api('/api/admin/accounts', { method: 'POST', body: JSON.stringify(payload) });
    $('dlgAccount').open = false;
    notify('账户已创建');
    loadAccounts(payload.role === 'institution' ? 'institution' : 'retail');
  } catch (e) { notify(e.message); }
});

/* 调整可用资金 */
$('ajCancel').addEventListener('click', () => { $('dlgAdjust').open = false; });

$('ajSubmit').addEventListener('click', async () => {
  const cash = Number($('ajCash').value);
  if (!state.adjustAccount || isNaN(cash)) { notify('请填写金额'); return; }
  try {
    await api(`/api/admin/accounts/${state.adjustAccount}/funding`, {
      method: 'PUT', body: JSON.stringify({ cash }),
    });
    $('dlgAdjust').open = false;
    notify('资金已调整');
    loadAccounts(state.page);
  } catch (e) { notify(e.message); }
});

/* ------------------------------------------------------------------ *
 * 管理员
 * ------------------------------------------------------------------ */
async function loadAdmins() {
  const body = $('adminsBody');
  body.innerHTML = emptyRow(5, '加载中…');
  try {
    const d = await api('/api/admin/admins');
    if (!d.items.length) { body.innerHTML = emptyRow(5, '暂无管理员'); return; }
    body.innerHTML = d.items.map((a) => `
      <tr>
        <td>${esc(a.username)}</td>
        <td>${esc(a.display_name || '--')}</td>
        <td>${a.is_active ? '启用' : '停用'}</td>
        <td>${dt(a.created_at)}</td>
        <td>
          ${rowBtn(a.is_active ? '停用' : '启用', `data-admstat="${a.id}"`, `data-active="${a.is_active}"`)}
          ${rowBtn('删除', `data-deladm="${a.id}"`, `data-name="${esc(a.username)}"`, true)}
        </td>
      </tr>`).join('');
  } catch (e) {
    body.innerHTML = emptyRow(5, e.message);
  }
}
$('refreshAdmins').addEventListener('click', loadAdmins);

$('addAdmin').addEventListener('click', () => {
  $('adUsername').value = '';
  $('adDisplay').value = '';
  $('adPassword').value = '';
  $('dlgAdmin').open = true;
});
$('adCancel').addEventListener('click', () => { $('dlgAdmin').open = false; });

$('adSubmit').addEventListener('click', async () => {
  const payload = {
    username: $('adUsername').value.trim(),
    display_name: $('adDisplay').value.trim(),
    password: $('adPassword').value,
  };
  if (!payload.username) { notify('请填写用户名'); return; }
  if (payload.password.length < 8) { notify('口令至少 8 位'); return; }
  try {
    await api('/api/admin/admins', { method: 'POST', body: JSON.stringify(payload) });
    $('dlgAdmin').open = false;
    notify('管理员已创建');
    loadAdmins();
  } catch (e) { notify(e.message); }
});

/* ------------------------------------------------------------------ *
 * 标的与指数
 * ------------------------------------------------------------------ */
async function loadStocks() {
  const body = $('stocksBody');
  body.innerHTML = emptyRow(9, '加载中…');
  try {
    const d = await api('/api/admin/stocks');
    const rows = d.items || [];
    state.stocks = rows;
    if (!rows.length) {
      body.innerHTML = emptyRow(9, '库中暂无标的，去「数据初始化」注入默认标的');
      return;
    }
    body.innerHTML = rows.map((s) => `
      <tr>
        <td>${esc(s.code)}</td>
        <td>${esc(s.name)}</td>
        <td>${esc(s.board || '--')}</td>
        <td>${esc(s.industry || '--')}</td>
        <td class="num">${s.price != null ? num(s.price) : num(s.prev_close)}</td>
        <td class="num">${num(s.limit_up)}</td>
        <td class="num">${num(s.limit_down)}</td>
        <td class="num">${num(s.amount)}</td>
        <td>
          ${rowBtn('编辑', `data-edits="${esc(s.code)}"`)}
          ${rowBtn('删除', `data-dels="${esc(s.code)}"`, `data-name="${esc(s.name)}"`, true)}
        </td>
      </tr>`).join('');
  } catch (e) {
    body.innerHTML = emptyRow(9, e.message);
  }
}

async function loadIndices() {
  const body = $('indicesBody');
  body.innerHTML = emptyRow(4, '加载中…');
  try {
    const d = await api('/api/admin/indices');
    const rows = d.items || [];
    if (!rows.length) { body.innerHTML = emptyRow(4, '暂无指数'); return; }
    body.innerHTML = rows.map((i) => `
      <tr>
        <td>${esc(i.code)}</td>
        <td>${esc(i.name)}</td>
        <td class="num">${num(i.prev_close)}</td>
        <td class="num">${pct(i.change_pct)}</td>
      </tr>`).join('');
  } catch (e) {
    body.innerHTML = emptyRow(4, e.message);
  }
}
$('refreshStocks').addEventListener('click', () => { loadStocks(); loadIndices(); });

$('addStock').addEventListener('click', () => {
  state.editStock = null;
  ['stCode', 'stName', 'stBoard', 'stIndustry', 'stPrevClose', 'stLimitUp', 'stLimitDown']
    .forEach((id) => { $(id).value = ''; });
  $('dlgStock').open = true;
});
$('stCancel').addEventListener('click', () => { $('dlgStock').open = false; });

$('stSubmit').addEventListener('click', async () => {
  const code = $('stCode').value.trim();
  if (!code) { notify('请填写代码'); return; }
  const payload = {
    code,
    name: $('stName').value.trim(),
    board: $('stBoard').value.trim(),
    industry: $('stIndustry').value.trim(),
    prev_close: Number($('stPrevClose').value || 0),
    limit_up: Number($('stLimitUp').value || 0),
    limit_down: Number($('stLimitDown').value || 0),
  };
  try {
    // 编辑走 PUT，新增走 POST；两者都提交同一份字段
    await api(state.editStock ? `/api/admin/stocks/${encodeURIComponent(code)}` : '/api/admin/stocks', {
      method: state.editStock ? 'PUT' : 'POST',
      body: JSON.stringify(payload),
    });
    $('dlgStock').open = false;
    notify('已保存');
    loadStocks();
  } catch (e) { notify(e.message); }
});

/* ------------------------------------------------------------------ *
 * 模拟引擎
 * ------------------------------------------------------------------ */
async function loadEngine() {
  $('engineKpis').innerHTML = emptyRow(1, '加载中…');
  try {
    const d = await api('/api/admin/engine');
    state.engine = d;
    const s = d.session || {};
    $('toggleEngine').textContent = d.enabled ? '关闭引擎' : '开启引擎';

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
    ];
    $('engineKpis').innerHTML = items.map(([a, v]) => kpiCard(a, v)).join('');
  } catch (e) {
    $('engineKpis').innerHTML = `<div style="opacity:.6">${esc(e.message)}</div>`;
  }
}
$('refreshEngine').addEventListener('click', loadEngine);

$('toggleEngine').addEventListener('click', async () => {
  try {
    await api('/api/admin/engine/toggle', { method: 'POST' });
    notify('引擎状态已切换');
    loadEngine();
  } catch (e) { notify(e.message); }
});

$('resetEngine').addEventListener('click', async () => {
  if (!(await ask('确认把全部行情重置到昨收？', '重置行情'))) return;
  try {
    await api('/api/admin/engine/reset', { method: 'POST' });
    notify('行情已重置');
    loadEngine();
  } catch (e) { notify(e.message); }
});

/* ------------------------------------------------------------------ *
 * 风险偏好
 * ------------------------------------------------------------------ */
function renderRiskDist(target, d) {
  const items = [
    ['账户总数', d.total], ['未测评', d.unassessed], ['保守型', d.conservative],
    ['稳健型', d.balanced], ['进取型', d.aggressive],
  ];
  $(target).innerHTML = items.map(([a, v]) => kpiCard(a, v ?? 0)).join('');
}

async function loadRisk() {
  try {
    const d = await api('/api/admin/risk-summary');
    renderRiskDist('riskDist', d);
  } catch (e) {
    $('riskDist').innerHTML = `<div style="opacity:.6">${esc(e.message)}</div>`;
  }
  await loadLeaderboard();
}

async function loadLeaderboard() {
  const body = $('leaderboardBody');
  body.innerHTML = emptyRow(6, '加载中…');
  try {
    const d = await api('/api/admin/leaderboard');
    const rows = d.items || [];
    if (!rows.length) { body.innerHTML = emptyRow(6, '暂无数据'); return; }
    body.innerHTML = rows.map((u) => `
      <tr>
        <td>${esc(u.username)}</td>
        <td>${esc(u.display_name || '--')}</td>
        <td>${ROLE_LABEL[u.role] || u.role || '--'}</td>
        <td class="num">${num(u.total_asset)}</td>
        <td class="num">${num(u.realized_pnl)}</td>
        <td class="num">${pct(u.return_pct)}</td>
      </tr>`).join('');
  } catch (e) {
    body.innerHTML = emptyRow(6, e.message);
  }
}
$('refreshRisk').addEventListener('click', () => { loadRisk(); });

/* ------------------------------------------------------------------ *
 * 系统配置
 * ------------------------------------------------------------------ */
async function loadConfig() {
  const grid = $('cfgGrid');
  grid.innerHTML = '<div style="opacity:.6">加载中…</div>';
  try {
    const d = await api('/api/admin/settings');
    state.cfg = d.items || [];
    state.cfgDirty = {};
    if (!state.cfg.length) { grid.innerHTML = '<div style="opacity:.6">暂无可配置项</div>'; return; }
    grid.innerHTML = state.cfg.map((c) => {
      let control = '';
      if (c.kind === 'bool') {
        control = `<mdui-select data-cfg="${c.key}" value="${c.value ? 'true' : 'false'}">
          <mdui-menu-item value="true">开启</mdui-menu-item>
          <mdui-menu-item value="false">关闭</mdui-menu-item>
        </mdui-select>`;
      } else if (c.kind === 'choice') {
        const opts = (c.choice_labels && c.choice_labels.length)
          ? c.choice_labels
          : (c.choices || []).map((o) => ({ value: o, label: o }));
        control = `<mdui-select data-cfg="${c.key}" value="${esc(String(c.value))}">
          ${opts.map((o) => `<mdui-menu-item value="${esc(String(o.value))}">${esc(o.label)}</mdui-menu-item>`).join('')}
        </mdui-select>`;
      } else {
        control = `<mdui-text-field data-cfg="${c.key}" type="number"
          step="${c.kind === 'int' ? 1 : 'any'}" value="${c.value}"></mdui-text-field>`;
      }
      const range = (c.kind === 'float' || c.kind === 'int')
        ? `${c.min} ~ ${c.max}${c.unit ? ' ' + c.unit : ''}` : '';
      return `<mdui-card style="padding:12px">
        <div style="font-weight:600">${esc(c.label)}</div>
        <div style="font-size:11px;opacity:.6;margin:2px 0 8px">${esc(c.key)}</div>
        ${control}
        <div style="font-size:11px;opacity:.6;margin-top:6px">
          ${esc(c.note || '')}${range ? ' · 范围 ' + esc(range) : ''} · 默认 ${esc(c.default)}
        </div>
      </mdui-card>`;
    }).join('');

    grid.querySelectorAll('[data-cfg]').forEach((el) => {
      const mark = () => {
        state.cfgDirty[el.dataset.cfg] = el.value;
        el.style.setProperty('--mdui-color-primary', 'var(--mdui-color-error)');
      };
      el.addEventListener('input', mark);
      el.addEventListener('change', mark);
    });
  } catch (e) {
    grid.innerHTML = `<div style="opacity:.6">${esc(e.message)}</div>`;
  }
}
$('refreshCfg').addEventListener('click', loadConfig);

$('saveCfg').addEventListener('click', async () => {
  const vals = state.cfgDirty;
  if (!Object.keys(vals).length) { notify('没有需要保存的修改'); return; }
  try {
    const d = await api('/api/admin/settings', {
      method: 'PUT', body: JSON.stringify({ values: vals }),
    });
    const okN = (d.updated || []).length;
    const badN = (d.rejected || []).length;
    notify(badN
      ? `已保存 ${okN} 项；${badN} 项被拒绝：` + d.rejected.map((r) => r.reason).join('；')
      : `已保存 ${okN} 项配置，已立即生效`);
    loadConfig();
  } catch (err) { notify(err.message); }
});

$('resetCfg').addEventListener('click', async () => {
  if (!(await ask('确认把全部运行期配置恢复为代码默认值？', '恢复默认'))) return;
  try {
    await api('/api/admin/settings/reset', { method: 'POST' });
    notify('已恢复默认配置');
    loadConfig();
  } catch (err) { notify(err.message); }
});

/* ------------------------------------------------------------------ *
 * 数据初始化
 * ------------------------------------------------------------------ */
async function loadSeedStatus() {
  try {
    const d = await api('/api/admin/seed/status');
    const items = [
      ['标的数', d.stocks], ['交易账户', d.users], ['管理员', d.admins],
      ['日线条数', int(d.daily_bars)], ['是否已初始化', d.seeded ? '是' : '否'],
    ];
    $('seedStatus').innerHTML = items.map(([a, v]) => kpiCard(a, v ?? 0)).join('');
  } catch (e) { notify(e.message); }
}
$('refreshSeed').addEventListener('click', loadSeedStatus);

$('seedBtn').addEventListener('click', async (e) => {
  const btn = e.currentTarget;
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
    notify(`注入完成：标的 +${d.stocks_added}、账户 +${d.users_added}、日线 +${d.daily_bars_added}`);
    loadSeedStatus();
  } catch (err) { notify(err.message); }
  finally { btn.disabled = false; }
});

/* ------------------------------------------------------------------ *
 * 撮合质量
 * ------------------------------------------------------------------ */
async function loadOps() {
  try {
    const m = await api('/api/admin/matching');
    const kpi = [
      ['委托总数', m.order_count], ['成交笔数', m.trade_count],
      ['撮成率', (m.fill_rate_pct ?? 0) + '%'], ['撤单率', (m.cancel_rate_pct ?? 0) + '%'],
      ['平均单笔成交额', num(m.avg_trade_amount)],
    ];
    $('matchKpi').innerHTML = kpi.map(([a, v]) => kpiCard(a, v ?? 0)).join('');

    const rows = m.top_symbols || [];
    $('matchBody').innerHTML = rows.length ? rows.map((o) => `
      <tr>
        <td>${esc(o.code)}</td>
        <td>${esc(o.name || '')}</td>
        <td class="num">${o.trades}</td>
        <td class="num">${num(o.amount)}</td>
        <td class="num">${int(o.volume)}</td>
      </tr>`).join('') : emptyRow(5, '暂无成交');
  } catch (e) {
    $('matchBody').innerHTML = emptyRow(5, e.message);
  }
}
$('refreshOps').addEventListener('click', loadOps);

$('settleBtn').addEventListener('click', async () => {
  if (!(await ask('确认执行日终结转？将解锁全部 T+1 持仓。', '日终结转'))) return;
  try {
    await api('/api/admin/settle', { method: 'POST' });
    notify('结转完成');
    loadOps();
  } catch (e) { notify(e.message); }
});

/* ------------------------------------------------------------------ *
 * 启动
 * ------------------------------------------------------------------ */
if (token()) {
  enterApp(null);
  // 令牌可能已过期：静默校验一次，失效会自动回到登录页
  api('/api/admin/me').catch(() => {});
} else {
  $('login').hidden = false;
}
