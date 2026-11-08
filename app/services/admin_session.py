"""管理后台的登录会话：数据库会话表 + Cookie 读写。

为什么单独成一个模块：
    原先会话字典写在 main.py，而真正校验身份的 `admin_user` 依赖在
    services/deps.py。两处各写一份就会出现「登录页写 Cookie、接口只认
    Bearer 令牌」的断层 —— 用户登录成功后进面板，面板里每个请求都因为
    没有 Authorization 头而被判成「缺少访问令牌」。

    把「会话存哪里、Cookie 叫什么」收敛到这一个模块后，
    main.py 只负责登录成功时下发会话，deps.py 只负责按同一份会话表校验，
    两边共用同一张表，不会再各自记一份状态。

安全取舍：
    后台只需要「已登录 / 未登录」两态，用不透明随机会话号即可；
    Cookie 走 HttpOnly，页面脚本读不到，XSS 也偷不走管理权限。

存储取舍（本轮重构）：
    会话从「进程内 dict」迁移到 `admin_session` 表。原来的字典在
    `uvicorn --workers > 1` 下不共享（登录落在 A worker、下个请求落到
    B worker 即变未登录），且重启即全员掉线。落库后这两个问题一起消失，
    也不需要为一个内网后台额外引入 Redis。
"""
from __future__ import annotations

import secrets
import time

from fastapi import Request, Response
from sqlmodel import Session, delete, select

from ..db import engine
from ..models import AdminSession

# 后台会话 Cookie 名。同一站点下其他服务的 Cookie 不会覆盖它。
ADMIN_COOKIE = "stland_admin"

# 12 小时。运营后台是低频使用的内部工具，长一点省去反复登录。
ADMIN_SESSION_TTL = 12 * 3600


def _client_ip(request: Request | None) -> str:
    if request is None:
        return ""
    # 反代后面取真实来源；直接连时退化为 peer addr
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64]


def create_session(
    admin_id: int,
    *,
    request: Request | None = None,
    ttl: int = ADMIN_SESSION_TTL,
) -> str:
    """签发一个后台会话号并登记到库，顺手清理已过期条目。"""
    sid = secrets.token_urlsafe(32)
    now = time.time()
    with Session(engine) as session:
        session.add(
            AdminSession(
                sid=sid,
                admin_id=admin_id,
                issued_at=now,
                expires_at=now + ttl,
                last_seen_at=now,
                user_agent=(request.headers.get("user-agent", "") if request else "")[:200],
                client_ip=_client_ip(request),
            )
        )
        # 表不能无限增长：每次登录清一遍过期会话就够了（无需额外定时任务）
        session.exec(delete(AdminSession).where(AdminSession.expires_at < now))
        session.commit()
    return sid


def session_admin_id(sid: str | None) -> int | None:
    """会话号 → 管理员 id；无效或已过期返回 None。"""
    if not sid:
        return None
    now = time.time()
    with Session(engine) as session:
        rec = session.get(AdminSession, sid)
        if rec is None:
            return None
        if rec.expires_at < now:
            session.delete(rec)
            session.commit()
            return None
        # 滑动续期：活跃用户不必在 12 小时整点被踢下线
        if now - rec.last_seen_at > 60:
            rec.last_seen_at = now
            session.add(rec)
            session.commit()
        return rec.admin_id


def session_valid(sid: str | None) -> bool:
    """会话号是否仍然有效（存在且未过期）。"""
    return session_admin_id(sid) is not None


def drop_session(sid: str | None) -> None:
    """撤销会话（退出登录 / Cookie 失效时调用）。"""
    if not sid:
        return
    with Session(engine) as session:
        rec = session.get(AdminSession, sid)
        if rec is not None:
            session.delete(rec)
            session.commit()


def drop_admin_sessions(admin_id: int) -> int:
    """撤销某个管理员的全部会话（改密 / 停用 / 删除时调用）。"""
    with Session(engine) as session:
        rows = session.exec(select(AdminSession).where(AdminSession.admin_id == admin_id)).all()
        for rec in rows:
            session.delete(rec)
        session.commit()
        return len(rows)


def request_sid(request: Request) -> str | None:
    """从请求里取出后台会话号。"""
    return request.cookies.get(ADMIN_COOKIE)


def logged_in(request: Request) -> bool:
    """该请求是否带着有效的后台登录态。"""
    return session_valid(request_sid(request))


def attach_cookie(response: Response, sid: str, *, secure: bool = False) -> None:
    """把会话号写进 HttpOnly Cookie。"""
    response.set_cookie(
        ADMIN_COOKIE,
        sid,
        max_age=ADMIN_SESSION_TTL,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def clear_cookie(response: Response) -> None:
    """清除后台会话 Cookie。"""
    response.delete_cookie(ADMIN_COOKIE, path="/")
