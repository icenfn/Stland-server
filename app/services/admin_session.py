"""管理后台的登录会话：内存会话表 + Cookie 读写。

为什么单独成一个模块：
    原先会话字典写在 main.py，而真正校验身份的 `admin_user` 依赖在
    services/deps.py。两处各写一份就会出现「登录页写 Cookie、接口只认
    Bearer 令牌」的断层 —— 用户登录成功后进面板，面板里每个请求都因为
    没有 Authorization 头而被判成「缺少访问令牌」。

    把「会话存哪里、Cookie 叫什么」收敛到这一个模块后，
    main.py 只负责登录成功时下发会话，deps.py 只负责按同一份会话表校验，
    两边共用同一张字典，不会再各自记一份状态。

安全取舍：
    后台只需要「已登录 / 未登录」两态，用不透明随机会话号即可；
    Cookie 走 HttpOnly，页面脚本读不到，XSS 也偷不走管理权限。

会话表存 `sid -> (admin_id, 过期时间戳)`：
    只存过期时间的话，校验时无从知道这个会话属于哪个管理员，
    也就没法再查一次 `admin` 表确认账号是否仍启用 / 是否被停用。
"""
from __future__ import annotations

import secrets
import time

from fastapi import Request, Response

# 后台会话 Cookie 名。同一站点下其他服务的 Cookie 不会覆盖它。
ADMIN_COOKIE = "stland_admin"

# 12 小时。运营后台是低频使用的内部工具，长一点省去反复登录。
ADMIN_SESSION_TTL = 12 * 3600

# 进程内会话表：sid -> (admin_id, 过期时间戳)。
# 模拟盘是单进程部署，重启即失效、需要重新登录，这是可接受的取舍：
# 换成 Redis / 数据库会为一个单人运营的后台引入额外依赖。
_ADMIN_SESSIONS: dict[str, tuple[int, float]] = {}


def create_session(admin_id: int) -> str:
    """签发一个后台会话号并登记管理员与过期时间，顺手清理已过期条目。"""
    sid = secrets.token_urlsafe(32)
    now = time.time()
    _ADMIN_SESSIONS[sid] = (admin_id, now + ADMIN_SESSION_TTL)
    # 字典不能无限增长：每次登录清一遍过期项就够了
    for old in [k for k, v in _ADMIN_SESSIONS.items() if v[1] < now]:
        _ADMIN_SESSIONS.pop(old, None)
    return sid


def session_admin_id(sid: str | None) -> int | None:
    """会话号 → 管理员 id；无效或已过期返回 None。"""
    if not sid:
        return None
    rec = _ADMIN_SESSIONS.get(sid)
    if rec is None:
        return None
    admin_id, exp = rec
    if exp < time.time():
        _ADMIN_SESSIONS.pop(sid, None)
        return None
    return admin_id


def session_valid(sid: str | None) -> bool:
    """会话号是否仍然有效（存在且未过期）。"""
    return session_admin_id(sid) is not None


def drop_session(sid: str | None) -> None:
    """撤销会话（退出登录 / Cookie 失效时调用）。"""
    if sid:
        _ADMIN_SESSIONS.pop(sid, None)


def request_sid(request: Request) -> str | None:
    """从请求里取出后台会话号。"""
    return request.cookies.get(ADMIN_COOKIE)


def logged_in(request: Request) -> bool:
    """该请求是否带着有效的后台登录态。"""
    return session_valid(request_sid(request))


def attach_cookie(response: Response, sid: str) -> None:
    """把会话号写进 HttpOnly Cookie。"""
    response.set_cookie(
        ADMIN_COOKIE,
        sid,
        max_age=ADMIN_SESSION_TTL,
        httponly=True,
        samesite="lax",
        path="/",
    )


def clear_cookie(response: Response) -> None:
    """清除后台会话 Cookie。"""
    response.delete_cookie(ADMIN_COOKIE, path="/")
