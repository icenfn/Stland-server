"""依赖注入：令牌鉴权、角色校验、请求签名校验。"""
from __future__ import annotations

import time

from fastapi import Depends, Header, HTTPException, Request, status
from sqlmodel import Session, select

from ..config import settings
from ..db import get_session
from ..models import AccountStatus, RevokedToken, User
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


async def current_user(
    request: Request,
    authorization: str = Header(default=""),
    session: Session = Depends(get_session),
) -> User:
    """解析 Bearer 令牌，校验吊销状态与账户状态。"""
    if not authorization.lower().startswith("bearer "):
        raise _unauthorized("缺少访问令牌")
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = decode_token(token)
    except TokenError as exc:
        raise _unauthorized(str(exc)) from exc

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


async def signed_request(
    request: Request,
    x_stland_ts: str = Header(default=""),
    x_stland_nonce: str = Header(default=""),
    x_stland_sign: str = Header(default=""),
    x_stland_user: str = Header(default=""),
) -> None:
    """校验请求签名（防篡改 + 防重放）。

    客户端对 `method\npath\nts\nnonce\nsha256(body)\nuser_id` 使用共享密钥做 HMAC-SHA256。
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
    """角色守卫：零售 / 机构 / 管理端共用一套接口，按角色放开能力。"""

    async def _guard(user: User = Depends(current_user)) -> User:
        if roles and user.role.value not in roles:
            raise _forbidden("当前账户角色无权访问该接口")
        return user

    return _guard


async def admin_user(user: User = Depends(current_user)) -> User:
    if user.role.value != "admin":
        raise _forbidden("仅管理端可访问")
    return user


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
