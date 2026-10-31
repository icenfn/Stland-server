/* ==========================================================================
   Stland 运营后台 · 前端脚本（mdui 1.x）

   与 mdui 1.x 的对接要点：
   - 抽屉与对话框都要实例化：new mdui.Drawer(el) / new mdui.Dialog(el)
   - mdui.confirm 是回调式 (content, title, onConfirm, onCancel)，不是 Promise
   - 导航高亮用 mdui-list-item-active 类
   ========================================================================== */
(function () {
  'use strict';

  var TOKEN_KEY = 'stland.admin.token';

  var state = {
    page: 'overview',
    admin: null,
    cfg: [],
    cfgDirty: {},
    stocks: [],
    accounts: {},
    editingStock: null,
    adjustTarget: null,
    loginRequired: false,
  };

  /* ---------------------------------------------------------------- 基础工具 */
  function $(id) {
    return document.getElementById(id);
  }

  function esc(v) {
    return String(v === null || v === undefined ? '' : v).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function pick(obj, keys, fallback) {
    if (!obj) return fallback;
    for (var i = 0; i < keys.length; i += 1) {
      var v = obj[keys[i]];
      if (v !== undefined && v !== null && v !== '') return v;
    }
    return fallback;
  }

  function num(v) {
    if (v === null || v === undefined || v === '') return '--';
    var n = Number(v);
    if (!isFinite(n)) return '--';
    return n.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  /* 指标卡里的金额用短单位，避免一长串数字把卡片撑爆或折断：
     18.34 亿 / 7,341.82 万。表格里仍然用完整的 num()。 */
  function money(v) {
    if (v === null || v === undefined || v === '') return '--';
    var n = Number(v);
    if (!isFinite(n)) return '--';
    var abs = Math.abs(n);
    if (abs >= 1e8) return (n / 1e8).toFixed(2) + ' 亿';
    if (abs >= 1e4) return (n / 1e4).toFixed(2) + ' 万';
    return n.toLocaleString('zh-CN', { maximumFractionDigits: 2 });
  }

  function int(v) {
    if (v === null || v === undefined || v === '') return '--';
    var n = Number(v);
    if (!isFinite(n)) return '--';
    return Math.round(n).toLocaleString('zh-CN');
  }

  function pct(v) {
    if (v === null || v === undefined || v === '') return '--';
    var n = Number(v);
    if (!isFinite(n)) return '--';
    return (n > 0 ? '+' : '') + n.toFixed(2) + '%';
  }

  function ts(v) {
    var n = Number(v);
    if (!n) return '--';
    var d = new Date(n * 1000);
    var p = function (x) { return String(x).padStart(2, '0'); };
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
      ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }

  function kpi(label, value, cls) {
    return '<div class="mdui-card app-kpi">' +
      '<div class="app-kpi-label">' + esc(label) + '</div>' +
      '<div class="app-kpi-value' + (cls ? ' ' + cls : '') + '">' + esc(value) + '</div>' +
      '</div>';
  }

  function emptyRow(colspan, text) {
    return '<tr><td colspan="' + colspan + '"><div class="app-empty">' + esc(text) + '</div></td></tr>';
  }

  function rowBtn(text, attrs, danger) {
    return '<button class="mdui-btn mdui-btn-dense mdui-ripple row-btn' +
      (danger ? ' mdui-text-color-red' : '') + '" ' + attrs + '>' + esc(text) + '</button>';
  }

  function notify(message) {
    if (window.mdui && typeof mdui.snackbar === 'function') {
      mdui.snackbar({ message: String(message), timeout: 2600 });
    } else {
      window.alert(message);
    }
  }

  function ask(text, title) {
    return new Promise(function (resolve) {
      if (window.mdui && typeof mdui.confirm === 'function') {
        try {
          mdui.confirm(text, title || '请确认',
            function () { resolve(true); },
            function () { resolve(false); });
          return;
        } catch (e) {
          /* 落到原生确认 */
        }
      }
      resolve(window.confirm(text));
    });
  }

  /* ------------------------------------------------------------------ 令牌 */
  function token() {
    try { return window.localStorage.getItem(TOKEN_KEY) || ''; } catch (e) { return ''; }
  }

  function setToken(t) {
    try {
      if (t) window.localStorage.setItem(TOKEN_KEY, t);
      else window.localStorage.removeItem(TOKEN_KEY);
    } catch (e) { /* 隐私模式忽略 */ }
  }

  /* ------------------------------------------------------------------ 请求 */
  function api(path, options) {
    var opts = options || {};
    var headers = { Accept: 'application/json' };
    if (opts.body) headers['Content-Type'] = 'application/json';
    var t = token();
    if (t) headers.Authorization = 'Bearer ' + t;

    return fetch(path, {
      method: opts.method || 'GET',
      headers: headers,
      body: opts.body || undefined,
    }).then(function (res) {
      return res.text().then(function (raw) {
        var data = null;
        if (raw) {
          try { data = JSON.parse(raw); } catch (e) { data = null; }
        }
        if (!res.ok) {
          var detail = data && data.detail;
          var msg = '';
          if (detail && typeof detail === 'object') msg = detail.message || detail.code || '';
          else if (typeof detail === 'string') msg = detail;
          else if (data && data.message) msg = data.message;
          if (!msg) msg = '请求失败（HTTP ' + res.status + '）';
          var err = new Error(msg);
          err.status = res.status;
          throw err;
        }
        return data;
      });
    }).catch(function (err) {
      if (err.status === 401) {
        setToken('');
        showLogin('登录已过期，请重新登录');
      }
      throw err;
    });
  }

  /* -------------------------------------------------------------- mdui 封装 */
  var drawer = null;
  var dialogs = {};

  function navDrawer() {
    if (!drawer && window.mdui) drawer = new mdui.Drawer('#nav');
    return drawer;
  }

  function dlg(id) {
    if (!dialogs[id] && window.mdui) dialogs[id] = new mdui.Dialog('#' + id);
    return dialogs[id];
  }

  function openDlg(id) {
    var d = dlg(id);
    if (d) d.open();
  }

  function closeDlg(id) {
    var d = dlg(id);
    if (d) d.close();
  }

  function isWide() {
    return window.innerWidth >= 1024;
  }

  /* 抽屉的开关自己记状态：宽屏才让正文让出 240px。
     mdui 1.x 的让位靠 body 上的 mdui-drawer-body-left，必须跟着开关一起切，
     否则宽屏收起抽屉后右侧会留一条 240px 的空白。 */
  var drawerOpen = true;

  function setDrawer(open) {
    drawerOpen = !!open;
    var d = navDrawer();
    if (d) {
      if (drawerOpen) d.open();
      else d.close();
    }
    document.body.classList.toggle('mdui-drawer-body-left', drawerOpen && isWide());
  }

  function toggleDrawer() {
    setDrawer(!drawerOpen);
  }

  /* ---------------------------------------------------------------- 登录页 */
  function showLogin(message) {
    state.loginRequired = true;
    var box = $('login');
    if (box) box.hidden = false;
    if (message) $('loginErr').textContent = message;
  }

  function hideLogin() {
    state.loginRequired = false;
    var box = $('login');
    if (box) box.hidden = true;
    $('loginErr').textContent = '';
  }

  function doLogin() {
    var username = $('loginUser').value.trim();
    var password = $('loginPass').value;
    if (!username || !password) {
      $('loginErr').textContent = '请填写登录名与口令';
      return;
    }
    var btn = $('loginBtn');
    btn.disabled = true;
    $('loginErr').textContent = '';
    api('/api/admin/login', {
      method: 'POST',
      body: JSON.stringify({ username: username, password: password }),
    }).then(function (d) {
      var t = d && d.token;
      var raw = typeof t === 'string' ? t : pick(t, ['access_token', 'token'], '');
      if (!raw) throw new Error('登录响应缺少令牌');
      setToken(raw);
      state.admin = (d && d.admin) || null;
      $('loginPass').value = '';
      hideLogin();
      enterApp();
    }).catch(function (err) {
      $('loginErr').textContent = err.message;
    }).then(function () {
      btn.disabled = false;
    });
  }

  function doLogout() {
    ask('确认退出当前管理员账号？', '退出登录').then(function (ok) {
      if (!ok) return;
      setToken('');
      state.admin = null;
      showLogin('已退出登录');
    });
  }

  /* ------------------------------------------------------------------ 导航 */
  var NAV_LOADER = {
    overview: loadOverview,
    retail: function () { return loadAccounts('retail'); },
    institution: function () { return loadAccounts('institution'); },
    admins: loadAdmins,
    stocks: function () { return Promise.all([loadStocks(), loadIndices()]); },
    engine: loadEngine,
    risk: loadRisk,
    config: loadConfig,
    data: loadSeedStatus,
    ops: loadOps,
  };

  function showPage(key, skipLoad) {
    var items = document.querySelectorAll('.nav-item');
    var label = key;
    for (var i = 0; i < items.length; i += 1) {
      var el = items[i];
      var on = el.dataset.page === key;
      el.classList.toggle('mdui-list-item-active', on);
      if (on) label = el.dataset.label || key;
    }
    var pages = document.querySelectorAll('.app-page');
    for (var j = 0; j < pages.length; j += 1) {
      pages[j].classList.toggle('on', pages[j].id === 'page-' + key);
    }
    $('pageTitle').textContent = label;
    state.page = key;
    if (window.history && window.history.replaceState) {
      window.history.replaceState(null, '', '#' + key);
    }
    if (!isWide()) setDrawer(false);
    var loader = NAV_LOADER[key];
    if (!skipLoad && typeof loader === 'function') loader();
  }

  /* ------------------------------------------------------------------ 总览 */
  function loadOverview() {
    $('ovKpi').innerHTML = '<div class="app-loading">加载中…</div>';
    api('/api/admin/dashboard').then(function (d) {
      var k = d.kpi || {};
      $('ovKpi').innerHTML = [
        kpi('交易账户', int(k.user_count)),
        kpi('零售 / 机构', int(k.retail_count) + ' / ' + int(k.institution_count)),
        kpi('管理员', int(k.admin_count)),
        kpi('正常 / 冻结', int(k.active_users) + ' / ' + int(k.frozen_users)),
        kpi('总资产', money(k.total_asset)),
        kpi('今日委托', int(k.today_orders)),
        kpi('今日成交', int(k.today_trades)),
        kpi('今日成交额（元）', money(k.today_turnover)),
        kpi('挂单中', int(k.open_orders)),
        kpi('持仓行数', int(k.position_rows)),
        kpi('自选条数', int(k.watch_items)),
      ].join('');

      var s = d.session || {};
      var m = d.market || {};
      $('ovSession').innerHTML = '<div class="app-kpi-grid">' +
        kpi('交易时段', s.label || s.phase || '--') +
        kpi('交易日', s.trade_date || '--') +
        kpi('可交易', s.can_trade ? '是' : '否') +
        kpi('上涨家数', int(pick(m, ['up'], '--'))) +
        kpi('下跌家数', int(pick(m, ['down'], '--'))) +
        '</div>';

      var r = d.risk_distribution || {};
      $('ovRisk').innerHTML = [
        kpi('账户总数', int(r.total)),
        kpi('未测评', int(r.unassessed)),
        kpi('保守型', int(r.conservative)),
        kpi('稳健型', int(r.balanced)),
        kpi('进取型', int(r.aggressive)),
      ].join('');
    }).catch(function (e) {
      $('ovKpi').innerHTML = '<div class="app-error">' + esc(e.message) + '</div>';
    });
  }

  /* -------------------------------------------------------------- 交易账户 */
  var STATUS_LABEL = { active: '正常', frozen: '冻结', closed: '已注销' };
  var ROLE_LABEL = { retail: '零售', institution: '机构' };

  function riskLabel(level, assessed) {
    if (!assessed || !level) return '未测评';
    if (level <= 1) return '保守型';
    if (level === 2) return '稳健型';
    return '进取型';
  }

  function loadAccounts(role) {
    var isRetail = role === 'retail';
    var body = $(isRetail ? 'retailBody' : 'instBody');
    var cols = isRetail ? 10 : 9;
    body.innerHTML = emptyRow(cols, '加载中…');

    var kw = $(isRetail ? 'retailSearch' : 'instSearch').value.trim();
    var qs = '/api/admin/accounts?role=' + encodeURIComponent(role) +
      (kw ? '&keyword=' + encodeURIComponent(kw) : '');

    return api(qs).then(function (d) {
      var rows = d.items || [];
      state.accounts[role] = rows;
      $(isRetail ? 'retailCount' : 'instCount').textContent =
        '共 ' + (d.total === undefined ? rows.length : d.total) + ' 个账户';

      if (!rows.length) {
        body.innerHTML = emptyRow(cols, kw ? '没有匹配的账户' : '暂无账户，可点「新建账户」');
        return;
      }

      body.innerHTML = rows.map(function (u) {
        var ops = rowBtn('调资金', 'data-act="adjust" data-id="' + u.id + '"') +
          rowBtn(u.status === 'active' ? '冻结' : '启用',
            'data-act="status" data-id="' + u.id + '" data-status="' + esc(u.status) + '"') +
          rowBtn('删除', 'data-act="del" data-id="' + u.id + '" data-name="' + esc(u.username) + '"', true);

        if (isRetail) {
          return '<tr>' +
            '<td>' + esc(u.username) + '</td>' +
            '<td>' + esc(u.display_name || '--') + '</td>' +
            '<td>' + esc(STATUS_LABEL[u.status] || u.status) + '</td>' +
            '<td>' + esc(riskLabel(u.risk_level, u.risk_assessed)) + '</td>' +
            '<td class="num">' + num(u.cash) + '</td>' +
            '<td class="num">' + num(u.frozen_cash) + '</td>' +
            '<td class="num">' + num(u.market_value) + '</td>' +
            '<td class="num">' + num(u.total_asset) + '</td>' +
            '<td class="num">' + int(u.positions) + '</td>' +
            '<td class="ops">' + ops + '</td>' +
            '</tr>';
        }
        return '<tr>' +
          '<td>' + esc(u.username) + '</td>' +
          '<td>' + esc(u.display_name || '--') + '</td>' +
          '<td>' + esc(STATUS_LABEL[u.status] || u.status) + '</td>' +
          '<td class="num">' + num(u.cash) + '</td>' +
          '<td class="num">' + num(u.credit_limit) + '</td>' +
          '<td class="num">' + num(u.market_value) + '</td>' +
          '<td class="num">' + num(u.total_asset) + '</td>' +
          '<td class="num">' + num(u.realized_pnl) + '</td>' +
          '<td class="ops">' + ops + '</td>' +
          '</tr>';
      }).join('');
    }).catch(function (e) {
      body.innerHTML = emptyRow(cols, e.message);
    });
  }

  function openAccountDialog(role) {
    $('acUsername').value = '';
    $('acDisplay').value = '';
    $('acCash').value = role === 'institution' ? '50000000' : '1000000';
    $('acPassword').value = '';
    $('acRole').value = role;
    $('acTitle').textContent = role === 'institution' ? '新建机构账户' : '新建零售账户';
    openDlg('dlgAccount');
  }

  function submitAccount() {
    var payload = {
      username: $('acUsername').value.trim(),
      display_name: $('acDisplay').value.trim(),
      role: $('acRole').value,
      cash: Number($('acCash').value || 0),
      password: $('acPassword').value,
    };
    if (!payload.username) { notify('请填写登录名'); return; }
    if (payload.password.length < 6) { notify('口令至少 6 位'); return; }
    api('/api/admin/accounts', { method: 'POST', body: JSON.stringify(payload) })
      .then(function () {
        closeDlg('dlgAccount');
        notify('账户已创建');
        showPage(payload.role === 'institution' ? 'institution' : 'retail');
      })
      .catch(function (e) { notify(e.message); });
  }

  function openAdjustDialog(id, username) {
    state.adjustTarget = id;
    $('ajWho').textContent = '账户：' + username;
    $('ajAmount').value = '';
    $('ajRemark').value = '';
    openDlg('dlgAdjust');
  }

  function submitAdjust() {
    var amount = Number($('ajAmount').value);
    if (!state.adjustTarget || !isFinite(amount) || amount === 0) {
      notify('请填写非零金额');
      return;
    }
    api('/api/admin/accounts/' + state.adjustTarget + '/adjust', {
      method: 'POST',
      body: JSON.stringify({ amount: amount, remark: $('ajRemark').value.trim() }),
    }).then(function (d) {
      closeDlg('dlgAdjust');
      notify('资金已调整，可用资金 ' + num(d.cash));
      loadAccounts(state.page === 'institution' ? 'institution' : 'retail');
    }).catch(function (e) { notify(e.message); });
  }

  function toggleAccountStatus(id, status) {
    var next = status === 'active' ? 'frozen' : 'active';
    var whose = state.accounts[state.page === 'institution' ? 'institution' : 'retail'] || [];
    var row = whose.filter(function (u) { return String(u.id) === String(id); })[0] || {};
    ask((next === 'frozen' ? '冻结' : '启用') + '账户「' + (row.username || id) + '」？', '修改状态')
      .then(function (ok) {
        if (!ok) return;
        return api('/api/admin/accounts/' + id + '/status', {
          method: 'POST',
          body: JSON.stringify({ status: next }),
        }).then(function () {
          notify('状态已更新');
          loadAccounts(state.page === 'institution' ? 'institution' : 'retail');
        });
      })
      .catch(function (e) { notify(e.message); });
  }

  function deleteAccount(id, name) {
    ask('删除账户「' + name + '」？该操作不可撤销。', '删除账户').then(function (ok) {
      if (!ok) return;
      return api('/api/admin/accounts/' + id, { method: 'DELETE' }).then(function () {
        notify('账户已删除');
        loadAccounts(state.page === 'institution' ? 'institution' : 'retail');
      });
    }).catch(function (e) { notify(e.message); });
  }

  /* ---------------------------------------------------------------- 管理员 */
  function loadAdmins() {
    var body = $('adminsBody');
    body.innerHTML = emptyRow(6, '加载中…');
    return api('/api/admin/admins').then(function (d) {
      var rows = d.items || [];
      if (!rows.length) {
        body.innerHTML = emptyRow(6, '暂无管理员');
        return;
      }
      body.innerHTML = rows.map(function (a) {
        return '<tr>' +
          '<td>' + esc(a.username) + '</td>' +
          '<td>' + esc(a.display_name || '--') + '</td>' +
          '<td>' + (a.is_active ? '启用' : '停用') + '</td>' +
          '<td>' + ts(a.last_login_at) + '</td>' +
          '<td>' + ts(a.created_at) + '</td>' +
          '<td class="ops">' +
          rowBtn(a.is_active ? '停用' : '启用',
            'data-act="admstat" data-id="' + a.id + '" data-active="' + (a.is_active ? '1' : '0') + '"') +
          rowBtn('删除', 'data-act="admdel" data-id="' + a.id + '" data-name="' + esc(a.username) + '"', true) +
          '</td></tr>';
      }).join('');
    }).catch(function (e) {
      body.innerHTML = emptyRow(6, e.message);
    });
  }

  function submitAdmin() {
    var payload = {
      username: $('adUsername').value.trim(),
      display_name: $('adDisplay').value.trim(),
      password: $('adPassword').value,
    };
    if (!payload.username) { notify('请填写登录名'); return; }
    if (payload.password.length < 8) { notify('口令至少 8 位'); return; }
    api('/api/admin/admins', { method: 'POST', body: JSON.stringify(payload) })
      .then(function () {
        closeDlg('dlgAdmin');
        notify('管理员已创建');
        loadAdmins();
      })
      .catch(function (e) { notify(e.message); });
  }

  /* ------------------------------------------------------------ 标的与指数 */
  function loadStocks() {
    var body = $('stocksBody');
    body.innerHTML = emptyRow(10, '加载中…');
    return api('/api/admin/stocks').then(function (d) {
      var rows = d.items || [];
      state.stocks = rows;
      $('stockCount').textContent = '共 ' + rows.length + ' 只标的';
      if (!rows.length) {
        body.innerHTML = emptyRow(10, '库中暂无标的，去「数据初始化」注入默认标的');
        return;
      }
      body.innerHTML = rows.map(function (s) {
        var price = pick(s, ['price', 'last'], null);
        var chg = pick(s, ['change_pct', 'pct'], null);
        var cls = Number(chg) > 0 ? 'is-pos' : (Number(chg) < 0 ? 'is-neg' : '');
        return '<tr>' +
          '<td>' + esc(s.code) + '</td>' +
          '<td>' + esc(s.name) + '</td>' +
          '<td>' + esc(s.board || '--') + '</td>' +
          '<td>' + esc(s.industry || '--') + '</td>' +
          '<td class="num">' + num(price === null ? s.prev_close : price) + '</td>' +
          '<td class="num ' + cls + '">' + pct(chg) + '</td>' +
          '<td class="num">' + num(s.prev_close) + '</td>' +
          '<td class="num">' + num(s.limit_up) + '</td>' +
          '<td class="num">' + num(s.limit_down) + '</td>' +
          '<td class="ops">' +
          rowBtn('编辑', 'data-act="editstock" data-code="' + esc(s.code) + '"') +
          rowBtn('删除', 'data-act="delstock" data-code="' + esc(s.code) + '" data-name="' + esc(s.name) + '"', true) +
          '</td></tr>';
      }).join('');
    }).catch(function (e) {
      body.innerHTML = emptyRow(10, e.message);
    });
  }

  function loadIndices() {
    var body = $('indicesBody');
    body.innerHTML = emptyRow(4, '加载中…');
    return api('/api/admin/indices').then(function (d) {
      var rows = d.items || [];
      if (!rows.length) {
        body.innerHTML = emptyRow(4, '暂无指数');
        return;
      }
      body.innerHTML = rows.map(function (i) {
        var point = pick(i, ['point', 'close', 'value', 'price'], null);
        var chg = pick(i, ['change_pct', 'pct'], null);
        var cls = Number(chg) > 0 ? 'is-pos' : (Number(chg) < 0 ? 'is-neg' : '');
        return '<tr>' +
          '<td>' + esc(i.code) + '</td>' +
          '<td>' + esc(i.name) + '</td>' +
          '<td class="num">' + num(point) + '</td>' +
          '<td class="num ' + cls + '">' + pct(chg) + '</td>' +
          '</tr>';
      }).join('');
    }).catch(function (e) {
      body.innerHTML = emptyRow(4, e.message);
    });
  }

  function openStockDialog(code) {
    state.editingStock = code || null;
    if (code) {
      var s = state.stocks.filter(function (x) { return x.code === code; })[0] || {};
      $('stTitle').textContent = '编辑标的 ' + code;
      $('stCode').value = s.code || code;
      $('stCode').readOnly = true;
      $('stName').value = s.name || '';
      $('stBoard').value = s.board || '主板';
      $('stIndustry').value = s.industry || '';
      $('stPrice').value = s.prev_close || '';
      $('stFloat').value = s.float_shares || 500000000;
    } else {
      $('stTitle').textContent = '新增标的';
      $('stCode').value = '';
      $('stCode').readOnly = false;
      $('stName').value = '';
      $('stBoard').value = '主板';
      $('stIndustry').value = '';
      $('stPrice').value = '10';
      $('stFloat').value = '500000000';
    }
    openDlg('dlgStock');
  }

  function submitStock() {
    var code = $('stCode').value.trim();
    if (!code) { notify('请填写代码'); return; }
    var payload = {
      code: code,
      name: $('stName').value.trim(),
      board: $('stBoard').value,
      industry: $('stIndustry').value.trim(),
      init_price: Number($('stPrice').value || 0),
      float_shares: Number($('stFloat').value || 0),
    };
    if (!(payload.init_price > 0)) { notify('基准价必须大于 0'); return; }
    var editing = !!state.editingStock;
    api(editing ? '/api/admin/stocks/' + encodeURIComponent(code) : '/api/admin/stocks', {
      method: editing ? 'PUT' : 'POST',
      body: JSON.stringify(payload),
    }).then(function () {
      closeDlg('dlgStock');
      notify('已保存');
      loadStocks();
    }).catch(function (e) { notify(e.message); });
  }

  function deleteStock(code, name) {
    ask('删除标的「' + name + '（' + code + '）」？', '删除标的').then(function (ok) {
      if (!ok) return;
      return api('/api/admin/stocks/' + encodeURIComponent(code), { method: 'DELETE' })
        .then(function () {
          notify('标的已删除');
          loadStocks();
        });
    }).catch(function (e) { notify(e.message); });
  }

  /* -------------------------------------------------------------- 模拟引擎 */
  function loadEngine() {
    $('engineKpis').innerHTML = '<div class="app-loading">加载中…</div>';
    return api('/api/admin/engine').then(function (d) {
      var s = d.session || {};
      $('toggleEngine').textContent = d.enabled ? '关闭引擎' : '开启引擎';
      $('toggleEngine').dataset.enabled = d.enabled ? '1' : '0';
      $('engineKpis').innerHTML = [
        kpi('引擎开关', d.enabled ? '已开启' : '已关闭'),
        kpi('价格推进', d.advancing ? '推进中' : '未推进'),
        kpi('就绪', d.ready ? '是' : '否'),
        kpi('交易模式', s.mode_label || d.trade_mode || '--'),
        kpi('当前时段', s.label || '--'),
        kpi('可撮合', s.is_open ? '是' : '否'),
        kpi('可申报', s.can_trade ? '是' : '否'),
        kpi('推进间隔', (d.tick_seconds === undefined ? '--' : d.tick_seconds) + ' 秒'),
        kpi('推进次数', int(d.tick_count)),
        kpi('标的数量', int(d.listed)),
      ].join('');
    }).catch(function (e) {
      $('engineKpis').innerHTML = '<div class="app-error">' + esc(e.message) + '</div>';
    });
  }

  function toggleEngine() {
    var enabled = $('toggleEngine').dataset.enabled === '1';
    api('/api/admin/engine/toggle', {
      method: 'POST',
      body: JSON.stringify({ enabled: !enabled }),
    }).then(function () {
      notify(enabled ? '引擎已关闭' : '引擎已开启');
      loadEngine();
    }).catch(function (e) { notify(e.message); });
  }

  function resetEngine() {
    ask('确认把全部行情重置到昨收？分钟线与累计成交会清空。', '重置行情').then(function (ok) {
      if (!ok) return;
      return api('/api/admin/engine/reset', { method: 'POST' }).then(function () {
        notify('行情已重置');
        loadEngine();
      });
    }).catch(function (e) { notify(e.message); });
  }

  /* -------------------------------------------------------------- 风险偏好 */
  function loadRisk() {
    return api('/api/admin/risk-summary').then(function (d) {
      $('riskDist').innerHTML = [
        kpi('账户总数', int(d.total)),
        kpi('未测评', int(d.unassessed)),
        kpi('保守型', int(d.conservative)),
        kpi('稳健型', int(d.balanced)),
        kpi('进取型', int(d.aggressive)),
      ].join('');
    }).catch(function (e) {
      $('riskDist').innerHTML = '<div class="app-error">' + esc(e.message) + '</div>';
    }).then(loadLeaderboard);
  }

  function loadLeaderboard() {
    var body = $('leaderboardBody');
    body.innerHTML = emptyRow(6, '加载中…');
    return api('/api/admin/leaderboard').then(function (d) {
      var rows = d.items || [];
      if (!rows.length) {
        body.innerHTML = emptyRow(6, '暂无数据');
        return;
      }
      body.innerHTML = rows.map(function (u) {
        var cls = Number(u.realized_pnl) > 0 ? 'is-pos' : (Number(u.realized_pnl) < 0 ? 'is-neg' : '');
        return '<tr>' +
          '<td>' + esc(u.username) + '</td>' +
          '<td>' + esc(u.display_name || '--') + '</td>' +
          '<td>' + esc(ROLE_LABEL[u.role] || u.role || '--') + '</td>' +
          '<td class="num">' + num(u.total_asset) + '</td>' +
          '<td class="num ' + cls + '">' + num(u.realized_pnl) + '</td>' +
          '<td class="num ' + cls + '">' + pct(u.return_pct) + '</td>' +
          '</tr>';
      }).join('');
    }).catch(function (e) {
      body.innerHTML = emptyRow(6, e.message);
    });
  }

  /* -------------------------------------------------------------- 系统配置 */
  function cfgControl(c) {
    var key = 'data-cfg="' + esc(c.key) + '"';
    if (c.kind === 'bool') {
      return '<select class="app-select" ' + key + '>' +
        '<option value="true"' + (c.value ? ' selected' : '') + '>开启</option>' +
        '<option value="false"' + (c.value ? '' : ' selected') + '>关闭</option>' +
        '</select>';
    }
    if (c.kind === 'choice') {
      var opts = (c.choices || []).map(function (o) {
        if (o && typeof o === 'object') return { value: o.value, label: o.label };
        return { value: o, label: o };
      });
      if (c.choice_labels && c.choice_labels.length) {
        opts = c.choice_labels.map(function (o, i) {
          if (o && typeof o === 'object') return { value: o.value, label: o.label };
          return { value: (c.choices || [])[i], label: o };
        });
      }
      return '<select class="app-select" ' + key + '>' + opts.map(function (o) {
        return '<option value="' + esc(String(o.value)) + '"' +
          (String(o.value) === String(c.value) ? ' selected' : '') + '>' + esc(o.label) + '</option>';
      }).join('') + '</select>';
    }
    return '<input class="app-select" ' + key + ' type="number" step="' +
      (c.kind === 'int' ? '1' : 'any') + '" value="' + esc(String(c.value)) + '" />';
  }

  function loadConfig() {
    var grid = $('cfgGrid');
    grid.innerHTML = '<div class="app-loading">加载中…</div>';
    return api('/api/admin/settings').then(function (d) {
      state.cfg = d.items || [];
      state.cfgDirty = {};
      if (!state.cfg.length) {
        grid.innerHTML = '<div class="app-loading">暂无可配置项</div>';
        return;
      }
      grid.innerHTML = state.cfg.map(function (c) {
        var range = (c.kind === 'float' || c.kind === 'int')
          ? (c.min + ' ~ ' + c.max + (c.unit ? ' ' + c.unit : '')) : '';
        return '<div class="app-cfg" data-cfgbox="' + esc(c.key) + '">' +
          '<div class="app-cfg-label">' + esc(c.label) + '</div>' +
          '<div class="app-cfg-key">' + esc(c.key) + '</div>' +
          cfgControl(c) +
          '<div class="app-cfg-note">' + esc(c.note || '') +
          (range ? ' · 范围 ' + esc(range) : '') +
          ' · 默认 ' + esc(c.default) + '</div>' +
          '</div>';
      }).join('');

      grid.querySelectorAll('[data-cfg]').forEach(function (el) {
        var mark = function () {
          state.cfgDirty[el.dataset.cfg] = el.value;
          var box = el.closest('.app-cfg');
          if (box) box.classList.add('app-cfg-dirty');
        };
        el.addEventListener('input', mark);
        el.addEventListener('change', mark);
      });
    }).catch(function (e) {
      grid.innerHTML = '<div class="app-error">' + esc(e.message) + '</div>';
    });
  }

  function saveConfig() {
    var keys = Object.keys(state.cfgDirty);
    if (!keys.length) { notify('没有需要保存的修改'); return; }
    var values = {};
    keys.forEach(function (key) {
      var item = state.cfg.filter(function (c) { return c.key === key; })[0] || {};
      var raw = state.cfgDirty[key];
      if (item.kind === 'bool') values[key] = raw === 'true' || raw === true;
      else if (item.kind === 'int') values[key] = parseInt(raw, 10);
      else if (item.kind === 'float') values[key] = Number(raw);
      else values[key] = raw;
    });
    api('/api/admin/settings', { method: 'PUT', body: JSON.stringify({ values: values }) })
      .then(function (d) {
        var okN = (d.updated || []).length;
        var bad = d.rejected || [];
        notify(bad.length
          ? '已保存 ' + okN + ' 项；' + bad.length + ' 项被拒绝：' +
            bad.map(function (r) { return r.reason || r.key; }).join('；')
          : '已保存 ' + okN + ' 项配置，已立即生效');
        loadConfig();
      })
      .catch(function (e) { notify(e.message); });
  }

  function resetConfig() {
    ask('确认把全部运行期配置恢复为代码默认值？', '恢复默认').then(function (ok) {
      if (!ok) return;
      return api('/api/admin/settings/reset', { method: 'POST' }).then(function () {
        notify('已恢复默认配置');
        loadConfig();
      });
    }).catch(function (e) { notify(e.message); });
  }

  /* ------------------------------------------------------------ 数据初始化 */
  function loadSeedStatus() {
    return api('/api/admin/seed/status').then(function (d) {
      $('seedStatus').innerHTML = [
        kpi('标的数', int(d.stocks)),
        kpi('交易账户', int(d.users)),
        kpi('管理员', int(d.admins)),
        kpi('日线条数', int(d.daily_bars)),
        kpi('是否已初始化', d.seeded ? '是' : '否'),
      ].join('');
    }).catch(function (e) {
      $('seedStatus').innerHTML = '<div class="app-error">' + esc(e.message) + '</div>';
    });
  }

  function runSeed() {
    var btn = $('seedBtn');
    btn.disabled = true;
    api('/api/admin/seed', {
      method: 'POST',
      body: JSON.stringify({
        stocks: $('seedStocks').checked,
        users: $('seedUsers').checked,
        history: $('seedHistory').checked,
      }),
    }).then(function (d) {
      notify('注入完成：标的 +' + (d.stocks_added || 0) +
        '、账户 +' + (d.users_added || 0) +
        '、日线 +' + (d.daily_bars_added || 0));
      loadSeedStatus();
    }).catch(function (e) {
      notify(e.message);
    }).then(function () {
      btn.disabled = false;
    });
  }

  /* -------------------------------------------------------------- 撮合质量 */
  function loadOps() {
    $('matchKpi').innerHTML = '<div class="app-loading">加载中…</div>';
    return api('/api/admin/matching').then(function (m) {
      $('matchKpi').innerHTML = [
        kpi('委托总数', int(m.order_count)),
        kpi('成交笔数', int(m.trade_count)),
        kpi('撮成率', (m.fill_rate_pct === undefined ? '--' : m.fill_rate_pct + '%')),
        kpi('撤单率', (m.cancel_rate_pct === undefined ? '--' : m.cancel_rate_pct + '%')),
        kpi('平均单笔成交额', money(m.avg_trade_amount)),
      ].join('');

      var rows = m.top_symbols || [];
      $('matchBody').innerHTML = rows.length ? rows.map(function (o) {
        return '<tr>' +
          '<td>' + esc(o.code) + '</td>' +
          '<td>' + esc(o.name || '--') + '</td>' +
          '<td class="num">' + int(o.trades) + '</td>' +
          '<td class="num">' + num(o.amount) + '</td>' +
          '<td class="num">' + int(o.volume) + '</td>' +
          '</tr>';
      }).join('') : emptyRow(5, '暂无成交');
    }).catch(function (e) {
      $('matchKpi').innerHTML = '<div class="app-error">' + esc(e.message) + '</div>';
      $('matchBody').innerHTML = emptyRow(5, e.message);
    });
  }

  function settle() {
    ask('确认执行日终结转？将解锁全部 T+1 持仓。', '日终结转').then(function (ok) {
      if (!ok) return;
      return api('/api/admin/settle', { method: 'POST' }).then(function () {
        notify('结转完成');
        loadOps();
      });
    }).catch(function (e) { notify(e.message); });
  }

  /* ------------------------------------------------------------------ 绑定 */
  function bind() {
    $('navToggle').addEventListener('click', toggleDrawer);

    document.querySelectorAll('.nav-item').forEach(function (el) {
      el.addEventListener('click', function () { showPage(el.dataset.page); });
    });

    $('logoutBtn').addEventListener('click', doLogout);
    $('loginBtn').addEventListener('click', doLogin);
    $('loginPass').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') doLogin();
    });
    $('loginUser').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') $('loginPass').focus();
    });

    /* 总览 */
    $('refreshOverview').addEventListener('click', loadOverview);

    /* 交易账户 */
    $('refreshRetail').addEventListener('click', function () { loadAccounts('retail'); });
    $('addRetail').addEventListener('click', function () { openAccountDialog('retail'); });
    $('refreshInst').addEventListener('click', function () { loadAccounts('institution'); });
    $('addInst').addEventListener('click', function () { openAccountDialog('institution'); });
    $('retailSearch').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') loadAccounts('retail');
    });
    $('instSearch').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') loadAccounts('institution');
    });

    $('acCancel').addEventListener('click', function () { closeDlg('dlgAccount'); });
    $('acSubmit').addEventListener('click', submitAccount);
    $('ajCancel').addEventListener('click', function () { closeDlg('dlgAdjust'); });
    $('ajSubmit').addEventListener('click', submitAdjust);

    /* 管理员 */
    $('refreshAdmins').addEventListener('click', loadAdmins);
    $('addAdmin').addEventListener('click', function () {
      $('adUsername').value = '';
      $('adDisplay').value = '';
      $('adPassword').value = '';
      openDlg('dlgAdmin');
    });
    $('adCancel').addEventListener('click', function () { closeDlg('dlgAdmin'); });
    $('adSubmit').addEventListener('click', submitAdmin);

    /* 标的 */
    $('refreshStocks').addEventListener('click', function () {
      loadStocks();
      loadIndices();
    });
    $('addStock').addEventListener('click', function () { openStockDialog(null); });
    $('stCancel').addEventListener('click', function () { closeDlg('dlgStock'); });
    $('stSubmit').addEventListener('click', submitStock);

    /* 引擎 */
    $('refreshEngine').addEventListener('click', loadEngine);
    $('toggleEngine').addEventListener('click', toggleEngine);
    $('resetEngine').addEventListener('click', resetEngine);

    /* 风险 */
    $('refreshRisk').addEventListener('click', loadRisk);

    /* 配置 */
    $('refreshCfg').addEventListener('click', loadConfig);
    $('saveCfg').addEventListener('click', saveConfig);
    $('resetCfg').addEventListener('click', resetConfig);

    /* 数据 */
    $('refreshSeed').addEventListener('click', loadSeedStatus);
    $('seedBtn').addEventListener('click', runSeed);

    /* 撮合 */
    $('refreshOps').addEventListener('click', loadOps);
    $('settleBtn').addEventListener('click', settle);

    /* 表格内的行内按钮统一在这里委托，只认 .row-btn，
       避免误吃顶栏或对话框里的 mdui 按钮 */
    document.addEventListener('click', function (e) {
      var btn = e.target.closest ? e.target.closest('.row-btn') : null;
      if (!btn) return;
      var act = btn.dataset.act;
      if (act === 'adjust') openAdjustDialog(btn.dataset.id, btn.dataset.name || btn.dataset.id);
      else if (act === 'status') toggleAccountStatus(btn.dataset.id, btn.dataset.status);
      else if (act === 'del') deleteAccount(btn.dataset.id, btn.dataset.name);
      else if (act === 'editstock') openStockDialog(btn.dataset.code);
      else if (act === 'delstock') deleteStock(btn.dataset.code, btn.dataset.name);
      else if (act === 'admstat') {
        var active = btn.dataset.active === '1';
        api('/api/admin/admins/' + btn.dataset.id + '/status', {
          method: 'POST',
          body: JSON.stringify({ is_active: !active }),
        }).then(function () {
          notify('状态已更新');
          loadAdmins();
        }).catch(function (err) { notify(err.message); });
      } else if (act === 'admdel') {
        ask('删除管理员「' + btn.dataset.name + '」？', '删除管理员').then(function (ok) {
          if (!ok) return;
          return api('/api/admin/admins/' + btn.dataset.id, { method: 'DELETE' })
            .then(function () {
              notify('管理员已删除');
              loadAdmins();
            });
        }).catch(function (err) { notify(err.message); });
      }
    });
  }

  /* -------------------------------------------------------------- 进入后台 */
  function enterApp() {
    if (!state.admin) {
      api('/api/admin/me').then(function (d) {
        state.admin = d.admin || null;
        if (state.admin) $('who').textContent = state.admin.display_name || state.admin.username;
      }).catch(function () { /* 401 已由 api() 统一处理 */ });
    } else {
      $('who').textContent = state.admin.display_name || state.admin.username;
    }

    setDrawer(isWide());

    var hash = (window.location.hash || '').replace('#', '');
    showPage(NAV_LOADER[hash] ? hash : 'overview');
  }

  function boot() {
    bind();
    if (token()) {
      hideLogin();
      enterApp();
    } else {
      showLogin('');
    }

    /* 窄屏切成宽屏时把抽屉展开，反之收起，避免半开状态 */
    var wasWide = isWide();
    window.addEventListener('resize', function () {
      var now = isWide();
      if (now === wasWide) return;
      wasWide = now;
      setDrawer(now);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
