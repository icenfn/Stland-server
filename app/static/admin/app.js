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
    boards: [],
    industries: [],
    accounts: {},
    editingStock: null,
    editingBoard: null,
    editingIndustry: null,
    editTarget: null,
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
          // 必须显式关掉 mdui 的 history 联动：默认 history=true 时，对话框
          // 关闭会调一次 window.history.back()，而这次历史回退是异步发生的，
          // 会把紧接着执行的 location 跳转覆盖掉 —— 浏览器停回原页面，
          // 表现就是「点了确认没反应」（退出登录踩的就是这个坑）。
          mdui.confirm(text, title || '请确认',
            function () { resolve(true); },
            function () { resolve(false); },
            { history: false });
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
        // 会话失效就回独立登录页：面板里已经没有登录框，不能再往 showLogin 走
        window.location.href = '/admin/login';
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

  /* 抽屉开关一律交给 mdui 自己的状态机。
     mdui.Drawer 内部维护开关状态并提供 open/close/toggle/getState，
     自己再存一个布尔值必然与它不同步 —— 用户点遮罩关闭时 mdui 收起了抽屉，
     我们那份状态还停在「打开」，下一次点击算出来就是「关 → 开」= 关，
     表现就是要点两次才有反应。这里改成直接调原生 toggle()。 */
  function drawerState() {
    var d = navDrawer();
    if (!d || typeof d.getState !== 'function') return false;
    return !!d.getState();
  }

  /* 宽屏时正文要让出 240px（mdui 的让位类），窄屏则铺满。
     这个类只跟「宽屏 + 抽屉已开」有关，绝不能拿它当抽屉开合的依据。 */
  function syncBodyOffset() {
    document.body.classList.toggle('mdui-drawer-body-left', drawerState() && isWide());
  }

  function setDrawer(open) {
    var d = navDrawer();
    if (!d) return;
    if (open) d.open();
    else d.close();
    syncBodyOffset();
  }

  function toggleDrawer() {
    var d = navDrawer();
    if (!d) return;
    // 原生 toggle：mdui 自己读当前状态再切换，不存在镜像不同步的问题
    d.toggle();
    syncBodyOffset();
  }

  /* ---------------------------------------------------------------- 登录 / 退出 */
  /* 登录已经搬到独立的 /admin/login 页面，凭证放在 HttpOnly Cookie 里，
     面板本身不再有登录框。原先这里的 showLogin / hideLogin / doLogin 会去操作
     #login / #loginErr / #loginUser 这些面板里已经不存在的元素，
     于是面板一加载就抛 "Cannot set properties of null (setting 'textContent')"。 */
  function doLogout() {
    ask('确认退出当前管理员账号？', '退出登录').then(function (ok) {
      if (!ok) return;
      // 会话在服务端，必须走 /admin/logout 由服务端撤销并清 Cookie。
      // 等对话框关闭流程走完再跳转，避免被对话框的收尾动作打断；
      // 用 replace 而不是 href，退出后按「后退」不会回到面板。
      setTimeout(function () { window.location.replace('/admin/logout'); }, 80);
    });
  }

  /* ------------------------------------------------------------------ 导航 */
  var NAV_LOADER = {
    overview: loadOverview,
    retail: function () { return loadAccounts('retail'); },
    institution: function () { return loadAccounts('institution'); },
    admins: loadAdmins,
    stocks: loadStocks,
    indices: loadIndices,
    // 引擎页只管运行状态与重置；引擎参数（间隔/波动/漂移/开关）已归入模拟配置
    engine: loadEngine,
    simcfg: loadSimCfg,
    boards: loadBoards,
    industries: loadIndustries,
    config: loadSystemCfg,
    data: loadSeedStatus,
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
    // 子项被激活时把它所属的分组展开，否则收起的分组里看不到高亮项
    var items2 = document.querySelectorAll('.nav-item');
    for (var n = 0; n < items2.length; n += 1) {
      if (items2[n].dataset.page === key) {
        var grp = items2[n].closest ? items2[n].closest('.nav-group') : null;
        if (grp) grp.classList.add('is-open');
        break;
      }
    }
    if ($('pageTitle')) $('pageTitle').textContent = label;
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
    var board = null;
    api('/api/admin/dashboard').then(function (d) {
      var k = d.kpi || {};
      board = d.leaderboard || null;
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

    }).catch(function (e) {
      $('ovKpi').innerHTML = '<div class="app-error">' + esc(e.message) + '</div>';
    });
    // 撮合质量并入总览：统计与成交额 Top10 与总览同屏刷新
    loadMatchingStats();
    // 排名与总览同屏：转结与风险页撤掉后，总资产排名就落在这里
    return loadLeaderboard(board);
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
      var cntEl = $(isRetail ? 'retailCount' : 'instCount');
      if (cntEl) cntEl.textContent =
        '共 ' + (d.total === undefined ? rows.length : d.total) + ' 个账户';

      if (!rows.length) {
        body.innerHTML = emptyRow(cols, kw ? '没有匹配的账户' : '暂无账户，可点「新建账户」');
        return;
      }

      body.innerHTML = rows.map(function (u) {
        var ops = rowBtn('编辑', 'data-act="editaccount" data-id="' + u.id + '"') +
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
    if ($('acTitle')) $('acTitle').textContent = role === 'institution' ? '新建机构账户' : '新建零售账户';
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

  /** 打开「编辑账户」：回填当前值，提交时只发真正改过的字段 */
  function openAccountEdit(id) {
    var role = state.page === 'institution' ? 'institution' : 'retail';
    var rows = state.accounts[role] || [];
    var u = rows.filter(function (x) { return String(x.id) === String(id); })[0];
    if (!u) { notify('账户数据已过期，请刷新后重试'); return; }
    state.editTarget = u;

    var isInst = u.role === 'institution';
    $('aeTitle').textContent = '编辑账户 ' + u.username;
    $('aeWho').textContent = '账户：' + u.username + '（' + (isInst ? '机构' : '零售') + '）';
    $('aeDisplay').value = u.display_name || '';
    $('aeStatus').value = u.status || 'active';
    $('aeRisk').value = String(u.risk_level == null ? 0 : u.risk_level);
    $('aeCredit').value = u.credit_limit == null ? '0' : String(u.credit_limit);
    $('aeRate').value = u.commission_rate == null ? '' : String(u.commission_rate);
    $('aeMin').value = u.commission_min == null ? '' : String(u.commission_min);
    $('aeCash').value = '';
    $('aePassword').value = '';
    // 风险偏好只对零售有意义，授信额度只对机构有意义，各自隐藏不相关的项
    $('aeRiskField').style.display = isInst ? 'none' : '';
    $('aeCreditField').style.display = isInst ? '' : 'none';
    openDlg('dlgAccountEdit');
  }

  function submitAccountEdit() {
    var u = state.editTarget;
    if (!u) { notify('请重新选择账户'); return; }
    var payload = {};

    var display = $('aeDisplay').value.trim();
    if (display !== (u.display_name || '')) payload.display_name = display;

    var status = $('aeStatus').value;
    if (status !== u.status) payload.status = status;

    if (u.role === 'institution') {
      var credit = Number($('aeCredit').value || 0);
      if (credit !== Number(u.credit_limit || 0)) payload.credit_limit = credit;
    } else {
      var risk = Number($('aeRisk').value);
      if (risk !== Number(u.risk_level == null ? 0 : u.risk_level)) payload.risk_level = risk;
    }

    // 留空 = 跟随角色默认（null），与「填 0」是两种不同语义，必须区分开
    var rateRaw = $('aeRate').value.trim();
    var rate = rateRaw === '' ? null : Number(rateRaw);
    if (rate !== (u.commission_rate == null ? null : Number(u.commission_rate))) {
      payload.commission_rate = rate;
    }
    var minRaw = $('aeMin').value.trim();
    var cmin = minRaw === '' ? null : Number(minRaw);
    if (cmin !== (u.commission_min == null ? null : Number(u.commission_min))) {
      payload.commission_min = cmin;
    }

    var cashRaw = $('aeCash').value.trim();
    var delta = cashRaw === '' ? 0 : Number(cashRaw);
    if (!isFinite(delta)) { notify('资金变动必须是数字'); return; }
    if (delta !== 0) {
      payload.cash_delta = delta;
      payload.remark = '管理端编辑账户时调整资金';
    }

    var pwd = $('aePassword').value;
    if (pwd) {
      if (pwd.length < 6) { notify('口令至少 6 位'); return; }
      payload.password = pwd;
    }

    if (!Object.keys(payload).length) { notify('没有需要保存的改动'); return; }

    api('/api/admin/accounts/' + u.id, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }).then(function (d) {
      closeDlg('dlgAccountEdit');
      var changed = (d && d.changed && d.changed.length) ? '：' + d.changed.join('、') : '';
      notify('已保存' + changed);
      loadAccounts(u.role === 'institution' ? 'institution' : 'retail');
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
      if ($('stockCount')) $('stockCount').textContent = '共 ' + rows.length + ' 只标的';
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
    body.innerHTML = emptyRow(7, '加载中…');
    return api('/api/admin/indices').then(function (d) {
      var rows = d.items || [];
      if ($('indexCount')) $('indexCount').textContent = '共 ' + rows.length + ' 个指数';
      if (!rows.length) {
        body.innerHTML = emptyRow(7, '暂无指数');
        return;
      }
      body.innerHTML = rows.map(function (i) {
        // 指数没有成分股时点位为空（has_data=false），必须显示「--」而不是 0
        var has = i.has_data !== false;
        var chg = i.change_pct;
        var cls = !has || chg == null ? '' : (chg > 0 ? 'is-pos' : (chg < 0 ? 'is-neg' : ''));
        return '<tr>' +
          '<td>' + esc(i.code) + '</td>' +
          '<td>' + esc(i.name) + '</td>' +
          '<td class="num">' + (has ? num(i.price) : '--') + '</td>' +
          '<td class="num ' + cls + '">' + (has ? pct(chg) : '--') + '</td>' +
          '<td class="num">' + int(i.members) + '</td>' +
          '<td class="num is-pos">' + int(i.up) + '</td>' +
          '<td class="num is-neg">' + int(i.down) + '</td>' +
          '</tr>';
      }).join('');
    }).catch(function (e) {
      body.innerHTML = emptyRow(7, e.message);
    });
  }

  function openStockDialog(code) {
    state.editingStock = code || null;
    if (code) {
      var s = state.stocks.filter(function (x) { return x.code === code; })[0] || {};
      if ($('stTitle')) $('stTitle').textContent = '编辑标的 ' + code;
      $('stCode').value = s.code || code;
      $('stCode').readOnly = true;
      $('stName').value = s.name || '';
      $('stBoard').value = s.board || '主板';
      $('stIndustry').value = s.industry || '';
      $('stPrice').value = s.prev_close || '';
      $('stFloat').value = s.float_shares || 500000000;
    } else {
      if ($('stTitle')) $('stTitle').textContent = '新增标的';
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
      if ($('toggleEngine')) $('toggleEngine').textContent = d.enabled ? '关闭引擎' : '开启引擎';
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

  /* -------------------------------------------------------------- 总资产排名 */
  /* 排名并入总览后不再单独请求：dashboard 已经带了 leaderboard 字段，
     再发一次请求只会让首屏多一个来回。传了数据就直接渲染。 */
  function loadLeaderboard(prefetched) {
    var body = $('leaderboardBody');
    if (!body) return Promise.resolve();

    function render(list) {
      var rows = list || [];
      if (!rows.length) {
        body.innerHTML = emptyRow(7, '暂无数据');
        return;
      }
      body.innerHTML = rows.map(function (u, i) {
        var cls = Number(u.realized_pnl) > 0 ? 'is-pos' : (Number(u.realized_pnl) < 0 ? 'is-neg' : '');
        return '<tr>' +
          '<td class="num">' + (i + 1) + '</td>' +
          '<td>' + esc(u.username) + '</td>' +
          '<td>' + esc(u.display_name || '--') + '</td>' +
          '<td>' + esc(ROLE_LABEL[u.role] || u.role || '--') + '</td>' +
          '<td class="num">' + money(u.total_asset) + '</td>' +
          '<td class="num ' + cls + '">' + num(u.realized_pnl) + '</td>' +
          '<td class="num ' + cls + '">' + pct(u.return_pct) + '</td>' +
          '</tr>';
      }).join('');
    }

    if (prefetched) { render(prefetched); return Promise.resolve(); }

    body.innerHTML = emptyRow(7, '加载中…');
    return api('/api/admin/dashboard').then(function (d) {
      render(d.leaderboard);
    }).catch(function (e) {
      body.innerHTML = emptyRow(7, e.message);
    });
  }

  /* -------------------------------------------------------------- 板块管理 */
  /* 板块承载规则：涨跌幅上限、最小报价单位、申报单位。
     标的只记板块名，涨跌停由后端按板块表推算，所以改名必须连带改标的归属
     （后端已处理），前端只需要把新的名字传上去。 */
  function loadBoards() {
    var body = $('boardsBody');
    if (!body) return Promise.resolve();
    body.innerHTML = emptyRow(8, '加载中…');
    return api('/api/admin/boards').then(function (d) {
      var rows = d.items || [];
      state.boards = rows;
      if ($('boardCount')) $('boardCount').textContent = '共 ' + rows.length + ' 个板块';
      if (!rows.length) {
        body.innerHTML = emptyRow(8, '暂无板块');
        return;
      }
      body.innerHTML = rows.map(function (b) {
        return '<tr>' +
          '<td>' + esc(b.name) + '</td>' +
          '<td class="num">±' + (Math.round(Number(b.limit_pct) * 1000) / 10) + '%</td>' +
          '<td class="num">' + num(b.tick_size) + '</td>' +
          '<td class="num">' + int(b.lot_size) + '</td>' +
          '<td class="num">' + int(b.stock_count) + '</td>' +
          '<td class="num">' + int(b.sort) + '</td>' +
          '<td class="app-cell-sub">' + esc(b.description || '--') + '</td>' +
          '<td>' +
            rowBtn('编辑', 'data-act="editboard" data-name="' + esc(b.name) + '"') +
            rowBtn('删除', 'data-act="delboard" data-name="' + esc(b.name) + '"', true) +
          '</td>' +
          '</tr>';
      }).join('');
    }).catch(function (e) {
      body.innerHTML = emptyRow(8, e.message);
    });
  }

  function openBoardDialog(name) {
    var b = null;
    if (name) {
      b = state.boards.filter(function (x) { return x.name === name; })[0] || {};
    }
    state.editingBoard = name || null;
    if ($('bdTitle')) $('bdTitle').textContent = name ? '编辑板块 ' + name : '新增板块';
    $('bdName').value = b ? (b.name || name) : '';
    $('bdName').readOnly = !!name;
    $('bdLimit').value = b && b.limit_pct !== undefined
      ? Math.round(Number(b.limit_pct) * 1000) / 10 : 10;
    $('bdTick').value = b && b.tick_size !== undefined ? b.tick_size : 0.01;
    $('bdLot').value = b && b.lot_size !== undefined ? b.lot_size : 100;
    $('bdSort').value = b && b.sort !== undefined ? b.sort : 0;
    $('bdDesc').value = b ? (b.description || '') : '';
    openDlg('dlgBoard');
  }

  function submitBoard() {
    var name = $('bdName').value.trim();
    if (!name) { notify('请填写板块名称'); return; }
    var pct = Number($('bdLimit').value);
    if (!(pct > 0 && pct <= 100)) { notify('涨跌幅百分比需在 0 到 100 之间'); return; }
    var tick = Number($('bdTick').value);
    var lot = Number($('bdLot').value);
    if (!(tick > 0) || !(lot > 0)) { notify('最小报价与申报单位必须为正数'); return; }

    var payload = {
      name: name,
      limit_pct: pct / 100,          // 界面按百分数填，接口收 0.10 这种比例
      tick_size: tick,
      lot_size: Math.round(lot),
      description: $('bdDesc').value.trim(),
      sort: Math.round(Number($('bdSort').value) || 0),
    };
    var editing = !!state.editingBoard;
    var url = editing
      ? '/api/admin/boards/' + encodeURIComponent(state.editingBoard)
      : '/api/admin/boards';
    api(url, { method: editing ? 'PUT' : 'POST', body: JSON.stringify(payload) })
      .then(function () {
        closeDlg('dlgBoard');
        notify('已保存');
        loadBoards();
        loadStocks();
      })
      .catch(function (e) { notify(e.message); });
  }

  function deleteBoard(name) {
    ask('删除板块「' + name + '」？其下还有标的时必须先改归属。', '删除板块').then(function (ok) {
      if (!ok) return;
      return api('/api/admin/boards/' + encodeURIComponent(name), { method: 'DELETE' })
        .then(function () {
          notify('板块已删除');
          loadBoards();
        });
    }).catch(function (e) { notify(e.message); });
  }

  /* -------------------------------------------------------------- 行业管理 */
  function loadIndustries() {
    var body = $('industriesBody');
    if (!body) return Promise.resolve();
    body.innerHTML = emptyRow(5, '加载中…');
    return api('/api/admin/industries').then(function (d) {
      var rows = d.items || [];
      state.industries = rows;
      if ($('industryCount')) $('industryCount').textContent = '共 ' + rows.length + ' 个行业';
      if (!rows.length) {
        body.innerHTML = emptyRow(5, '暂无行业');
        return;
      }
      body.innerHTML = rows.map(function (it) {
        return '<tr>' +
          '<td>' + esc(it.name) + '</td>' +
          '<td class="num">' + int(it.stock_count) + '</td>' +
          '<td class="num">' + int(it.sort) + '</td>' +
          '<td class="app-cell-sub">' + esc(it.description || '--') + '</td>' +
          '<td>' +
            rowBtn('编辑', 'data-act="editindustry" data-name="' + esc(it.name) + '"') +
            rowBtn('删除', 'data-act="delindustry" data-name="' + esc(it.name) + '"', true) +
          '</td>' +
          '</tr>';
      }).join('');
    }).catch(function (e) {
      body.innerHTML = emptyRow(5, e.message);
    });
  }

  function openIndustryDialog(name) {
    var it = null;
    if (name) {
      it = state.industries.filter(function (x) { return x.name === name; })[0] || {};
    }
    state.editingIndustry = name || null;
    if ($('inTitle')) $('inTitle').textContent = name ? '编辑行业 ' + name : '新增行业';
    $('inName').value = it ? (it.name || name) : '';
    $('inName').readOnly = !!name;
    $('inSort').value = it && it.sort !== undefined ? it.sort : 0;
    $('inDesc').value = it ? (it.description || '') : '';
    openDlg('dlgIndustry');
  }

  function submitIndustry() {
    var name = $('inName').value.trim();
    if (!name) { notify('请填写行业名称'); return; }
    var payload = {
      name: name,
      description: $('inDesc').value.trim(),
      sort: Math.round(Number($('inSort').value) || 0),
    };
    var editing = !!state.editingIndustry;
    var url = editing
      ? '/api/admin/industries/' + encodeURIComponent(state.editingIndustry)
      : '/api/admin/industries';
    api(url, { method: editing ? 'PUT' : 'POST', body: JSON.stringify(payload) })
      .then(function () {
        closeDlg('dlgIndustry');
        notify('已保存');
        loadIndustries();
        loadStocks();
      })
      .catch(function (e) { notify(e.message); });
  }

  function deleteIndustry(name) {
    ask('删除行业「' + name + '」？其下还有标的时必须先改归属。', '删除行业').then(function (ok) {
      if (!ok) return;
      return api('/api/admin/industries/' + encodeURIComponent(name), { method: 'DELETE' })
        .then(function () {
          notify('行业已删除');
          loadIndustries();
        });
    }).catch(function (e) { notify(e.message); });
  }

  /* -------------------------------------------------------------- 运行期配置 */
  /* 运行期配置分两个域：
   *   system（系统配置）—— 服务自身的安全开关，与股市无关；
   *   sim（模拟配置）—— 这个模拟股市的市场规则：行情引擎、交易时段、结算、
   *                        交易费用、新账户初始资金。
   * 两个页面各自只取自己那一域，保存与恢复默认也只作用于本域。
   * 这一点必须严格：如果在「模拟配置」页点恢复默认把「请求签名校验」
   * 也一并重置回 true，整个客户端立刻因为签名被重新要求而整片报错。
   */
  state.cfgByGroup = {};
  state.cfgDirty = {};

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
    if (c.kind === 'text') {
      // 自由文本（交易时段 JSON）：用 textarea 而不是单行 input，
      // 一张时段表有七八段，单行框里改 JSON 基本没法编辑。
      // 上方给几个内置模板按钮，点一下就把模板填进框里再由运营自己改；
      // 保存的始终是这张表本身，不存在「预设」这个独立状态。
      // 多行 JSON 文本项（交易时段 / 休市日期）都配一组模板按钮：
      // 点一下把模板填进框里再自行修改，保存的始终是框里的文本本身。
      // 模板数据由后端下发，前端不硬编码时间表或节假日表。
      var fillCfg = {
        session_slots: { presets: state.slotPresets, hint: '点击填入内置时段模板' },
        holidays: { presets: state.holidayPresets, hint: '点击填入内置休市日历' }
      }[c.key];
      var fills = '';
      if (fillCfg) {
        fills = '<div class="app-cfg-fill">' +
          (fillCfg.presets || []).map(function (p) {
            return '<button type="button" class="mdui-btn mdui-ripple app-fill-btn" ' +
              'data-fill-preset="' + esc(p.key) + '" data-fill-target="' + esc(c.key) + '">' +
              esc(p.label) + '</button>';
          }).join('') +
          '<span class="app-fill-hint">' + esc(fillCfg.hint) + '</span></div>';
      }
      return fills + '<textarea class="app-select app-textarea" rows="10" ' + key + '>' +
        esc(String(c.value == null ? '' : c.value)) + '</textarea>';
    }
    return '<input class="app-select" ' + key + ' type="number" step="' +
      (c.kind === 'int' ? '1' : 'any') + '" value="' + esc(String(c.value)) + '" />';
  }

  function cfgFieldHtml(c) {
    var range = (c.kind === 'float' || c.kind === 'int')
      ? (c.min + ' ~ ' + c.max + (c.unit ? ' ' + c.unit : '')) : '';
    // 多行文本（交易时段 JSON）的默认值就是整张表，铺在说明里会淹没其它信息
    var showDefault = !(c.kind === 'text' && String(c.default || '').length > 60);
    return '<div class="app-cfg" data-cfgbox="' + esc(c.key) + '">' +
      '<div class="app-cfg-label">' + esc(c.label) + '</div>' +
      '<div class="app-cfg-key">' + esc(c.key) + '</div>' +
      cfgControl(c) +
      '<div class="app-cfg-note">' + esc(c.note || '') +
      (range ? ' · 范围 ' + esc(range) : '') +
      (showDefault ? ' · 默认 ' + esc(c.default) : '') +
      '</div>' +
      '</div>';
  }

  function bindCfgInputs(host, group) {
    // 内置时段模板按钮：把对应预设的 JSON 填进 textarea，并标记为已修改，
    // 让运营能直接保存，也还能在填入后继续手改。
    // 用 dispatchEvent 触发一次 input，让下面统一注册的 mark 处理器接管
    // 「记入待保存值 + 高亮」这件事，按钮自己不再重复维护状态。
    host.querySelectorAll('[data-fill-preset]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var targetKey = btn.dataset.fillTarget || 'session_slots';
        var el = host.querySelector('[data-cfg="' + targetKey + '"]');
        if (!el) return;
        var pool = targetKey === 'holidays' ? (state.holidayPresets || []) : (state.slotPresets || []);
        var key = btn.dataset.fillPreset;
        var hit = pool.filter(function (p) { return p.key === key; })[0];
        if (!hit) return;
        el.value = hit.json;
        el.dispatchEvent(new Event('input', { bubbles: true }));
      });
    });
    host.querySelectorAll('[data-cfg]').forEach(function (el) {
      var mark = function () {
        if (!state.cfgDirty[group]) state.cfgDirty[group] = {};
        state.cfgDirty[group][el.dataset.cfg] = el.value;
        var box = el.closest('.app-cfg');
        if (box) box.classList.add('app-cfg-dirty');
      };
      el.addEventListener('input', mark);
      el.addEventListener('change', mark);
    });
  }

  function loadCfgGroup(group, hostId, withCards) {
    var host = $(hostId);
    if (!host) return Promise.resolve();
    host.innerHTML = '<div class="app-loading">加载中…</div>';
    return api('/api/admin/settings?group=' + encodeURIComponent(group)).then(function (d) {
      var items = d.items || [];
      state.cfgByGroup[group] = items;
      state.cfgDirty[group] = {};
      // 时段模板由后端下发（session.py 的 PRESETS），前端不硬编码时间表
      state.slotPresets = d.slot_presets || [];
      state.holidayPresets = d.holiday_presets || [];
      if (!items.length) {
        host.innerHTML = '<div class="app-loading">暂无可配置项</div>';
        return;
      }
      if (!withCards) {
        host.innerHTML = items.map(cfgFieldHtml).join('');
      } else {
        // 分区卡片：顺序由后端 GROUP_SECTIONS 决定，前端不另维护一份映射。
        // 以后新增配置项只要在 runtime_config.py 里标好 section 就自动归位。
        var sections = d.sections || [];
        host.innerHTML = sections.map(function (sec) {
          var secItems = items.filter(function (c) { return c.section === sec.key; });
          if (!secItems.length) return '';
          return '<div class="mdui-card app-card">' +
            '<div class="app-card-head">' +
              '<div class="app-card-title">' + esc(sec.label) + '</div>' +
            '</div>' +
            '<div class="app-card-body">' +
              '<div class="app-cfg-grid">' + secItems.map(cfgFieldHtml).join('') + '</div>' +
            '</div>' +
          '</div>';
        }).join('');
      }
      bindCfgInputs(host, group);
    }).catch(function (e) {
      host.innerHTML = '<div class="app-error">' + esc(e.message) + '</div>';
    });
  }

  function loadSystemCfg() { return loadCfgGroup('system', 'cfgSections', true); }
  function loadSimCfg() { return loadCfgGroup('sim', 'simCfgSections', true); }

  function saveConfig(group) {
    var dirty = state.cfgDirty[group] || {};
    var keys = Object.keys(dirty);
    if (!keys.length) { notify('没有需要保存的修改'); return; }
    var items = state.cfgByGroup[group] || [];
    var values = {};
    keys.forEach(function (key) {
      var item = items.filter(function (c) { return c.key === key; })[0] || {};
      var raw = dirty[key];
      if (item.kind === 'bool') values[key] = raw === 'true' || raw === true;
      else if (item.kind === 'int') values[key] = parseInt(raw, 10);
      else if (item.kind === 'float') values[key] = Number(raw);
      else values[key] = raw;
    });
    // 带上 group：后端只接受本域的键，前端状态串了也改不到另一域
    api('/api/admin/settings?group=' + encodeURIComponent(group), {
      method: 'PUT', body: JSON.stringify({ values: values }),
    }).then(function (d) {
      var okN = (d.updated || []).length;
      var bad = d.rejected || [];
      notify(bad.length
        ? '已保存 ' + okN + ' 项；' + bad.length + ' 项被拒绝：' +
          bad.map(function (r) { return r.reason || r.key; }).join('；')
        : '已保存 ' + okN + ' 项配置，已立即生效');
      if (group === 'sim') loadSimCfg(); else loadSystemCfg();
    }).catch(function (e) { notify(e.message); });
  }

  function resetConfig(group) {
    var label = group === 'sim' ? '模拟配置' : '系统设置';
    ask('确认把「' + label + '」恢复为默认值？另一域的配置不受影响。', '恢复默认')
      .then(function (ok) {
        if (!ok) return;
        return api('/api/admin/settings/reset?group=' + encodeURIComponent(group),
          { method: 'POST' }).then(function (d) {
          notify('已恢复 ' + (d.count || 0) + ' 项默认值');
          if (group === 'sim') loadSimCfg(); else loadSystemCfg();
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
  /** 撮合质量：原独立页已并入总览，这里把统计与成交额 Top10 写到总览的元素上 */
  function loadMatchingStats() {
    $('ovMatchKpi').innerHTML = '<div class="app-loading">加载中…</div>';
    return api('/api/admin/matching').then(function (m) {
      $('ovMatchKpi').innerHTML = [
        kpi('委托总数', int(m.order_count)),
        kpi('成交笔数', int(m.trade_count)),
        kpi('撮成率', (m.fill_rate_pct === undefined ? '--' : m.fill_rate_pct + '%')),
        kpi('撤单率', (m.cancel_rate_pct === undefined ? '--' : m.cancel_rate_pct + '%')),
        kpi('平均单笔成交额', money(m.avg_trade_amount)),
      ].join('');

      var rows = m.top_symbols || [];
      $('ovMatchBody').innerHTML = rows.length ? rows.map(function (o) {
        return '<tr>' +
          '<td>' + esc(o.code) + '</td>' +
          '<td>' + esc(o.name || '--') + '</td>' +
          '<td class="num">' + int(o.trades) + '</td>' +
          '<td class="num">' + num(o.amount) + '</td>' +
          '<td class="num">' + int(o.volume) + '</td>' +
          '</tr>';
      }).join('') : emptyRow(5, '暂无成交');
    }).catch(function (e) {
      $('ovMatchKpi').innerHTML = '<div class="app-error">' + esc(e.message) + '</div>';
      $('ovMatchBody').innerHTML = emptyRow(5, e.message);
    });
  }

  /* ------------------------------------------------------------------ 绑定 */
  function bind() {
    $('navToggle').addEventListener('click', toggleDrawer);

    document.querySelectorAll('.nav-item').forEach(function (el) {
      el.addEventListener('click', function () { showPage(el.dataset.page); });
    });

    /* 分组头只管展开/收起，不切页面 */
    document.querySelectorAll('.nav-group-head').forEach(function (head) {
      head.addEventListener('click', function () {
        head.parentNode.classList.toggle('is-open');
      });
    });

    $('logoutBtn').addEventListener('click', doLogout);

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
    $('aeCancel').addEventListener('click', function () { closeDlg('dlgAccountEdit'); });
    $('aeSubmit').addEventListener('click', submitAccountEdit);

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
    $('refreshStocks').addEventListener('click', loadStocks);
    $('refreshIndices').addEventListener('click', loadIndices);
    $('addStock').addEventListener('click', function () { openStockDialog(null); });
    $('stCancel').addEventListener('click', function () { closeDlg('dlgStock'); });
    $('stSubmit').addEventListener('click', submitStock);

    /* 引擎 */
    $('refreshEngine').addEventListener('click', loadEngine);
    $('toggleEngine').addEventListener('click', toggleEngine);
    $('resetEngine').addEventListener('click', resetEngine);

    /* 配置 */
  // 两页各自的按钮只作用于自己的域
  var _refreshCfg = $('refreshCfg');
  if (_refreshCfg) _refreshCfg.addEventListener('click', loadSystemCfg);
  var _saveCfg = $('saveCfg');
  if (_saveCfg) _saveCfg.addEventListener('click', function () { saveConfig('system'); });
  var _resetCfg = $('resetCfg');
  if (_resetCfg) _resetCfg.addEventListener('click', function () { resetConfig('system'); });

  var _refreshSimCfg = $('refreshSimCfg');
  if (_refreshSimCfg) _refreshSimCfg.addEventListener('click', loadSimCfg);
  var _saveSimCfg = $('saveSimCfg');
  if (_saveSimCfg) _saveSimCfg.addEventListener('click', function () { saveConfig('sim'); });
  var _resetSimCfg = $('resetSimCfg');
  if (_resetSimCfg) _resetSimCfg.addEventListener('click', function () { resetConfig('sim'); });

    /* 数据 */
    $('refreshSeed').addEventListener('click', loadSeedStatus);
    $('seedBtn').addEventListener('click', runSeed);

    /* 撮合 */

    /* 板块 */
    $('refreshBoards').addEventListener('click', loadBoards);
    $('addBoard').addEventListener('click', function () { openBoardDialog(null); });

    /* 行业 */
    $('refreshIndustries').addEventListener('click', loadIndustries);
    $('addIndustry').addEventListener('click', function () { openIndustryDialog(null); });

    $('bdCancel').addEventListener('click', function () { closeDlg('dlgBoard'); });
    $('bdSubmit').addEventListener('click', submitBoard);
    $('inCancel').addEventListener('click', function () { closeDlg('dlgIndustry'); });
    $('inSubmit').addEventListener('click', submitIndustry);

    /* 表格内的行内按钮统一在这里委托，只认 .row-btn，
       避免误吃顶栏或对话框里的 mdui 按钮 */
    document.addEventListener('click', function (e) {
      var btn = e.target.closest ? e.target.closest('.row-btn') : null;
      if (!btn) return;
      var act = btn.dataset.act;
      if (act === 'editaccount') openAccountEdit(btn.dataset.id);
      else if (act === 'status') toggleAccountStatus(btn.dataset.id, btn.dataset.status);
      else if (act === 'del') deleteAccount(btn.dataset.id, btn.dataset.name);
      else if (act === 'editstock') openStockDialog(btn.dataset.code);
      else if (act === 'editboard') openBoardDialog(btn.dataset.name);
      else if (act === 'delboard') deleteBoard(btn.dataset.name);
      else if (act === 'editindustry') openIndustryDialog(btn.dataset.name);
      else if (act === 'delindustry') deleteIndustry(btn.dataset.name);
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
        if (state.admin && $('who')) $('who').textContent = state.admin.display_name || state.admin.username;
      }).catch(function () { /* 401 已由 api() 统一处理 */ });
    } else {
      if ($('who')) $('who').textContent = state.admin.display_name || state.admin.username;
    }

    setDrawer(isWide());

    var hash = (window.location.hash || '').replace('#', '');
    showPage(NAV_LOADER[hash] ? hash : 'overview');
  }

  function boot() {
    bind();
    // 是否已登录由服务端判定：/admin 未登录会 302 到 /admin/login，
    // 能走到这里就说明会话有效，前端不必（也不该）再判一次 token。
    enterApp();

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
