/* 独立登录页脚本：只负责提交与跳转，不复用后台面板的 app.js */
(function () {
  var form = document.getElementById('loginForm');
  var err = document.getElementById('loginErr');
  var btn = document.getElementById('loginBtn');

  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    err.textContent = '';
    btn.disabled = true;

    var payload = {
      username: document.getElementById('loginUser').value.trim(),
      password: document.getElementById('loginPass').value,
    };

    fetch('/api/admin/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
      .then(function (r) {
        return r.json().then(function (d) {
          if (!r.ok) throw new Error(d.detail || '登录失败');
          return d;
        });
      })
      .then(function (d) {
        // token 可能是字符串，也可能是 {access_token}，两种都兼容
        var tok = typeof d.token === 'string' ? d.token : (d.access_token || (d.token && d.token.access_token) || '');
        if (!tok) throw new Error('未拿到登录凭证');
        localStorage.setItem('stland_admin_token', tok);
        location.href = '/admin';
      })
      .catch(function (e) {
        err.textContent = e.message || '登录失败';
      })
      .then(function () {
        btn.disabled = false;
      });
  });
})();
