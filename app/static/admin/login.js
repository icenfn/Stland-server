/* 独立登录页脚本：提交凭据并跳转后台。
   登录凭证由服务端写入 HttpOnly Cookie，前端不再保存 token，
   避免 XSS 直接读到管理凭证。 */
(function () {
  var form = document.getElementById('loginForm');
  var err = document.getElementById('loginErr');
  var btn = document.getElementById('loginBtn');
  if (!form) return;

  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    if (err) err.textContent = '';
    if (btn) btn.disabled = true;

    fetch('/api/admin/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({
        username: (document.getElementById('loginUser') || {}).value || '',
        password: (document.getElementById('loginPass') || {}).value || '',
      }),
    })
      .then(function (r) {
        return r.json().then(function (d) {
          if (!r.ok) {
            var detail = d && d.detail;
            throw new Error((detail && detail.message) || (typeof detail === 'string' ? detail : '') || '登录失败');
          }
          return d;
        });
      })
      .then(function () {
        location.href = '/admin';
      })
      .catch(function (e) {
        if (err) err.textContent = e.message || '登录失败';
        if (btn) btn.disabled = false;
      });
  });
})();
