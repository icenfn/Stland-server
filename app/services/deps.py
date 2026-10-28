"""依赖注入：令牌鉴权、管理员鉴权、请求签名校验。

本版把「管理员」从交易账户里彻底拆开：
* `current_user` 只解析**交易账户**（User），令牌 role 为 retail/institution；
* `admin_user` 只解析**管理员**（Admin），令牌 role 固定为 admin。

这样两条链路的鉴权互不干扰：管理员令牌拿去调交易接口会因找不到 User 而 401，
交易账户令牌拿去调后台接口会因找不到 Admin 而 403 —— 不需要在业务代码里到处判断角色。
"""
from __future__ import annotations

import time

from fastapi import Depends, Header, HTTPException, Request, status
from sqlmodel import Session, select

from ..config import settings
from ..db import get_session
from ..models import AccountStatus, Admin, RevokedToken, User
from ..security import TokenError, body_digest, decode_token, verify_signature


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
    """解析交易账户令牌，校验吊销状态与账户状态。"""
    token = _bearer_token(authorization)
    try:
        payload = decode_token(token)
    except TokenError as exc:
        raise _unauthorized(str(exc)) from exc

    if payload.get("role") == "admin":
        raise _forbidden("管理员账户不能访问交易接口")

    jti = payload.get("jti", "")
    if jti and session.exec(select(RevokedToken).where(RevokedToken.jti == jti)).first():
        raise _unauthorized("令牌已失效，请重新登录")

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
    authorization: str = Header(default=""),
    session: Session = Depends(get_session),
) -> Admin:
    """解析管理员令牌，只认 `admin` 表。"""
    token = _bearer_token(authorization)
    try:
        payload = decode_token(token)
    except TokenError as exc:
        raise _unauthorized(str(exc)) from exc

    if payload.get("role") != "admin":
        raise _forbidden("仅管理端可访问")

    jti = payload.get("jti", "")
    if jti and session.exec(select(RevokedToken).where(RevokedToken.jti == jti)).first():
        raise _unauthorized("令牌已失效，请重新登录")

    admin = session.get(Admin, int(payload.get("sub", 0)))
    if admin is None or not admin.is_active:
        raise _unauthorized("管理员账户不存在或已停用")
    request.state.token_payload = payload
    return admin


async def signed_request(
    request: Request,
    x_stland_ts: str = Header(default=""),
    x_stland_nonce: str = Header(default=""),
    x_stland_sign: str = Header(default=""),
    x_stland_user: str = Header(default=""),
) -> None:
    """校验请求签名（防篡改 + 防重放）。

    客户端对 `method\\npath\\nts\\nnonce\\nsha256(body)\\nuser_id` 做 HMAC-SHA256。
    """
    if not settings.signature_required:
        return
    body = await request.body()
    ok, reason = verify_signature(
        method=request.method,
        path=request.url.path,
        ts=x_stland_ts,
        nonce=x_stland_nonce,
        body_sha256=body_digest(body),
        signature=x_stland_sign,
        user_id=x_stland_user,
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "BAD_SIGNATURE", "message": reason},
        )


def require_role(*roles: str):
    """角色守卫：零售 / 机构共用一套接口，按角色放开能力。"""

    async def _guard(user: User = Depends(current_user)) -> User:
        if roles and user.role.value not in roles:
            raise _forbidden("当前账户角色无权访问该接口")
        return user

    return _guard


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


def issue_admin_token(admin: Admin) -> dict:
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
