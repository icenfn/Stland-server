"""鉴权路由：注册、登录、刷新、登出、当前用户。"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlmodel import Session, select

from ..config import settings
from ..db import get_session
from ..models import AccountStatus, AuditLog, RevokedToken, Role, User
from ..security import (
    TokenError,
    create_token,
    decode_token,
    hash_password,
    random_secret,
    verify_password,
)
from ..services.deps import current_user, issue_access_token

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterIn(BaseModel):
    username: str = Field(min_length=3, max_length=20)
    password: str = Field(min_length=8, max_length=64)
    display_name: str = Field(default="", max_length=20)
    role: Role = Role.RETAIL

    @field_validator("username")
    @classmethod
    def _username_charset(cls, v: str) -> str:
        if not all(c.isalnum() or c == "_" for c in v):
            raise ValueError("用户名只允许字母、数字与下划线")
        return v.lower()

    @field_validator("password")
    @classmethod
    def _password_strength(cls, v: str) -> str:
        has_digit = any(c.isdigit() for c in v)
        has_alpha = any(c.isalpha() for c in v)
        if not (has_digit and has_alpha):
            raise ValueError("口令需同时包含字母与数字")
        return v


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=20)
    password: str = Field(min_length=1, max_length=64)


class RefreshIn(BaseModel):
    refresh_token: str


def public_user(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name or user.username,
        "role": user.role.value,
        "status": user.status.value,
        "cash": round(user.cash, 2),
        "frozen_cash": round(user.frozen_cash, 2),
        "market_value": round(user.market_value, 2),
        "total_asset": round(user.total_asset, 2),
        "realized_pnl": round(user.realized_pnl, 2),
        "credit_limit": round(user.credit_limit, 2),
        "risk_level": user.risk_level,
        "created_at": user.created_at,
    }


@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(payload: RegisterIn, session: Session = Depends(get_session)) -> dict:
    exists = session.exec(select(User).where(User.username == payload.username)).first()
    if exists:
        raise HTTPException(409, detail={"code": "USERNAME_TAKEN", "message": "用户名已被占用"})
    if payload.role == Role.ADMIN:
        raise HTTPException(403, detail={"code": "ROLE_FORBIDDEN", "message": "管理端账户需由管理员创建"})

    user = User(
        username=payload.username,
        display_name=payload.display_name or payload.username,
        password_hash=hash_password(payload.password),
        role=payload.role,
        cash=float(settings.seed_cash),
        total_asset=float(settings.seed_cash),
        credit_limit=0.0 if payload.role == Role.RETAIL else 5_000_000.0,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return {"user": public_user(user), "token": issue_access_token(user)}


@router.post("/login")
def login(payload: LoginIn, request: Request, session: Session = Depends(get_session)) -> dict:
    user = session.exec(select(User).where(User.username == payload.username.lower())).first()
    ip = request.client.host if request.client else ""

    if user is None:
        raise HTTPException(401, detail={"code": "BAD_CREDENTIALS", "message": "用户名或口令错误"})

    now = time.time()
    if user.locked_until > now:
        wait = int(user.locked_until - now)
        raise HTTPException(
            423,
            detail={"code": "ACCOUNT_LOCKED", "message": f"连续登录失败，账户已临时锁定，请 {wait} 秒后重试"},
        )

    if not verify_password(payload.password, user.password_hash):
        user.fail_count += 1
        if user.fail_count >= settings.login_fail_limit:
            user.locked_until = now + 300
            user.fail_count = 0
        session.add(user)
        session.add(AuditLog(user_id=user.id, username=user.username, action="login_failed", detail="口令错误", ip=ip))
        session.commit()
        raise HTTPException(401, detail={"code": "BAD_CREDENTIALS", "message": "用户名或口令错误"})

    if user.status == AccountStatus.CLOSED:
        raise HTTPException(403, detail={"code": "ACCOUNT_CLOSED", "message": "账户已注销"})

    user.fail_count = 0
    user.locked_until = 0.0
    user.last_login_at = now
    session.add(user)
    session.add(AuditLog(user_id=user.id, username=user.username, action="login", detail="登录成功", ip=ip))
    session.commit()
    session.refresh(user)

    refresh = create_token(
        {"sub": str(user.id), "scope": "refresh"}, ttl=settings.refresh_token_ttl
    )
    return {"user": public_user(user), "token": issue_access_token(user), "refresh_token": refresh}


@router.post("/refresh")
def refresh_token(payload: RefreshIn, session: Session = Depends(get_session)) -> dict:
    try:
        data = decode_token(payload.refresh_token)
    except TokenError as exc:
        raise HTTPException(401, detail={"code": "BAD_REFRESH", "message": str(exc)}) from exc
    if data.get("scope") != "refresh":
        raise HTTPException(401, detail={"code": "BAD_REFRESH", "message": "令牌类型错误"})
    user = session.get(User, int(data.get("sub", 0)))
    if user is None or user.status != AccountStatus.ACTIVE:
        raise HTTPException(401, detail={"code": "BAD_REFRESH", "message": "账户状态异常"})
    return {"user": public_user(user), "token": issue_access_token(user)}


@router.post("/logout")
def logout(request: Request, session: Session = Depends(get_session), user: User = Depends(current_user)) -> dict:
    payload = getattr(request.state, "token_payload", {}) or {}
    jti = payload.get("jti", "")
    if jti:
        if not session.exec(select(RevokedToken).where(RevokedToken.jti == jti)).first():
            session.add(RevokedToken(jti=jti, expire_at=float(payload.get("exp", 0))))
    session.add(AuditLog(user_id=user.id, username=user.username, action="logout", detail="退出登录"))
    session.commit()
    return {"ok": True}


@router.get("/me")
def me(user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    from ..services.trading import settle_positions

    settle_positions(session, user)
    session.commit()
    session.refresh(user)
    return {"user": public_user(user)}


@router.get("/handshake")
def handshake() -> dict:
    """客户端启动时获取服务端信息与会话状态（无需鉴权）。"""
    from ..engine.session import current_session

    st = current_session()
    return {
        "app": "Stland",
        "version": settings.app_version,
        "server_time": int(time.time()),
        "session": {
            "phase": st.phase,
            "label": st.label,
            "is_open": st.is_open,
            "can_trade": st.can_trade,
            "trade_date": st.trade_date,
            "seconds_left": st.seconds_left,
        },
        "signature_required": settings.signature_required,
        "nonce": random_secret(8),
    }
