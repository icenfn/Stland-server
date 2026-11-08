"""依赖注入：交易账户令牌鉴权、管理员 Cookie 鉴权。

本版把「管理员」从交易账户里彻底拆开：
* `current_user` 只解析**交易账户**（User），令牌 role 为 retail/institution；
* `admin_user` 只解析**管理员**（Admin），凭据是后台会话 Cookie。

后台凭据单一化（本轮重构）：
    上一版 `admin_user` 同时接受 Cookie 与 `Authorization: Bearer`，而前端又把
    登录返回的 token 存进 localStorage 再逐请求带回来 —— 两条凭据并存意味着
    两套失效路径、两处 XSS 暴露面。现在后台网页**只用 HttpOnly Cookie**：
    页面脚本读不到凭据，XSS 也偷不走管理权限，管理员令牌一共只签发一处失败机会。

这样两条链路的鉴权互不干扰：管理员 Cookie 拿去调交易接口没有 Bearer 头，
直接 401；交易账户令牌拿去调后台接口没有后台会话，直接 401。
"""
from __future__ import annotations

import time

from fastapi import Depends, Header, HTTPException, Request, status
from sqlmodel import Session

from ..config import settings
from ..db import get_session
from ..models import AccountStatus, Admin, User
from ..security import TokenError, decode_token
from . import admin_session


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "UNAUTHORIZED", "message": detail},
        headers={"WWW-Authenticate": "Bearer"},
    )


def _forbidden(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": "FORBIDDEN", "message": detail},
    )


def _bearer_token(authorization: str) -> str:
    if not authorization.lower().startswith("bearer "):
        raise _unauthorized("缺少访问令牌")
    return authorization.split(" ", 1)[1].strip()


async def current_user(
    request: Request,
    authorization: str = Header(default=""),
    session: Session = Depends(get_session),
) -> User:
    """解析交易账户令牌，校验账户状态。"""
    token = _bearer_token(authorization)
    try:
        payload = decode_token(token)
    except TokenError as exc:
        raise _unauthorized(str(exc)) from exc

    if payload.get("role") == "admin":
        raise _forbidden("管理员账户不能访问交易接口")

    user = session.get(User, int(payload.get("sub", 0)))
    if user is None:
        raise _unauthorized("账户不存在")
    if user.status == AccountStatus.CLOSED:
        raise _forbidden("账户已注销")
    if user.status == AccountStatus.FROZEN:
        raise _forbidden("账户已被风控冻结，请联系管理员")
    request.state.token_payload = payload
    return user


async def admin_user(
    request: Request,
    session: Session = Depends(get_session),
) -> Admin:
    """解析管理端身份：**只认后台会话 Cookie**。

    Cookie 命中后还要再查一次 `admin` 表，确认账号仍然是启用状态 ——
    会话表只说明「这个浏览器登录过」，不说明「这个人现在还有权限」。
    """
    sid = admin_session.request_sid(request)
    admin_id = admin_session.session_admin_id(sid)
    if admin_id is None:
        raise _unauthorized("缺少访问令牌")

    admin = session.get(Admin, admin_id)
    if admin is None or not admin.is_active:
        # 会话还在但账号被停用 / 删除：顺手撤销，让浏览器回登录页
        admin_session.drop_session(sid)
        raise _unauthorized("管理员账户不存在或已停用")
    request.state.admin_via = "cookie"
    return admin


def require_role(*roles: str):
    """角色守卫：零售 / 机构共用一套接口，按角色放开能力。"""

    async def _guard(user: User = Depends(current_user)) -> User:
        if roles and user.role.value not in roles:
            raise _forbidden("当前账户角色无权访问该接口")
        return user

    return _guard


def issue_admin_token(admin: Admin) -> dict:
    """给脚本 / CI 换发的管理员令牌（后台网页本身只用 Cookie，不消费它）。

    保留这个签发点是为了让自动化脚本有一条不依赖浏览器 Cookie 的路径；
    但 `admin_user` 仍然只认 Cookie —— 两条凭据不会再混用在同一个入口。
    """
    from ..security import create_token

    token = create_token(
        {"sub": str(admin.id), "role": "admin", "username": admin.username},
        ttl=settings.access_token_ttl,
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": settings.access_token_ttl,
        "issued_at": int(time.time()),
    }


def issue_access_token(user: User) -> dict:
    from ..security import create_token

    token = create_token(
        {"sub": str(user.id), "role": user.role.value, "username": user.username},
        ttl=settings.access_token_ttl,
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": settings.access_token_ttl,
        "issued_at": int(time.time()),
    }
