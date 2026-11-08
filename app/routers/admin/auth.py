"""管理端 · 管理员登录 / 身份。"""
from __future__ import annotations

from ._common import *  # noqa: F401,F403
from ._common import router


# --------------------------------------------------------------------------- #
# 管理员登录 / 身份
# --------------------------------------------------------------------------- #
class AdminLoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=64)


def admin_public(admin: Admin) -> dict:
    return {
        "id": admin.id,
        "username": admin.username,
        "display_name": admin.display_name or admin.username,
        "is_active": admin.is_active,
        "last_login_at": admin.last_login_at,
        "created_at": admin.created_at,
    }


@router.post("/login")
def admin_login(
    payload: AdminLoginIn,
    response: Response,
    session: Session = Depends(get_session),
) -> dict:
    """管理员登录：只查 `admin` 表，与交易账户完全无关。

    登录成功时顺手把后台会话写进 HttpOnly Cookie。
    写在这里而不是放在中间件里，是因为「解析响应体拿管理员 id」在
    BaseHTTPMiddleware 下取不到 body（call_next 返回的是流式包装对象），
    会话号就发不出去，浏览器随后每个请求都没有凭据 —— 那正是
    「登录成功却提示缺少访问令牌」的成因。
    """
    admin = session.exec(select(Admin).where(Admin.username == payload.username.lower())).first()
    if admin is None or not admin.is_active:
        raise HTTPException(401, detail={"code": "BAD_CREDENTIALS", "message": "用户名或口令错误"})

    now = time.time()
    if admin.locked_until > now:
        wait = int(admin.locked_until - now)
        raise HTTPException(
            423, detail={"code": "LOCKED", "message": f"连续失败过多，请 {wait} 秒后重试"}
        )

    if not verify_password(payload.password, admin.password_hash):
        admin.fail_count += 1
        if admin.fail_count >= settings.login_fail_limit:
            admin.locked_until = now + LOCK_SECONDS
            admin.fail_count = 0
        session.add(admin)
        session.commit()
        raise HTTPException(401, detail={"code": "BAD_CREDENTIALS", "message": "用户名或口令错误"})

    admin.fail_count = 0
    admin.locked_until = 0.0
    admin.last_login_at = now
    session.add(admin)
    session.commit()
    session.refresh(admin)
    # 会话号只认这条 admin 记录；浏览器之后靠 Cookie 过关，不需带 Authorization
    admin_session.attach_cookie(response, admin_session.create_session(admin.id))
    return {"admin": admin_public(admin), "token": issue_admin_token(admin)}


@router.get("/me")
def admin_me(admin: Admin = Depends(admin_user)) -> dict:
    return {"admin": admin_public(admin)}


class PasswordIn(BaseModel):
    old_password: str = Field(min_length=1, max_length=64)
    new_password: str = Field(min_length=8, max_length=64)


@router.post("/password")
def change_password(
    payload: PasswordIn,
    session: Session = Depends(get_session),
    admin: Admin = Depends(admin_user),
) -> dict:
    """修改自己的口令。首次登录后应立即改掉引导密码。"""
    if not verify_password(payload.old_password, admin.password_hash):
        raise HTTPException(400, detail={"code": "BAD_PASSWORD", "message": "原口令错误"})
    admin.password_hash = hash_password(payload.new_password)
    admin.updated_at = time.time()
    session.add(admin)
    session.commit()
    return {"ok": True}
