"""统一主体（Principal）抽象。

背景
----
重构前 `User` 与 `Admin` 各自定义了一遍 `username / password_hash / fail_count /
locked_until / last_login_at / is_active`，而 `auth.login` 与 `admin.login` 又各自
抄了一遍「计数失败 → 到阈值就锁定 → 成功就清零」的逻辑。两处复制意味着
改一次阈值要改两处，且很容易一边改了另一边忘。

这里做两件事：
1) `Principal` —— 一个轻量包装，把「当前是谁、是什么身份、有哪些权力」统一成
   一个对象，路由里不再区分「拿到的是 User 还是 Admin」；
2) `login_guard` —— 把防爆破的状态机收敛成三个函数，两条登录链路共用。

为什么不把 `User` 与 `Admin` 合并成一张表：见 `models.py` 里 `Admin` 的文档 ——
管理员不参与撮合、没有资金，混表会让每个交易查询都要额外过滤一次 role。
所以「表分开、抽象统一」才是这里的正确取舍。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from sqlmodel import Session

from ..config import settings

Kind = Literal["user", "admin"]

# 锁定 5 分钟。与原先两处硬编码保持一致，只是现在只有一个定义。
LOCK_SECONDS = 300


@dataclass(frozen=True)
class Principal:
    """统一的「已认证主体」视图。

    `raw` 保留原始 ORM 对象（路由里偶尔仍需要具体字段，比如账户的资金行），
    `kind` 表达它属于哪条链路，`scopes` 用来说明它能调用哪些域。
    """

    kind: Kind
    id: int
    username: str
    display_name: str
    role: str
    raw: Any

    @property
    def is_admin(self) -> bool:
        return self.kind == "admin"

    @property
    def scopes(self) -> frozenset[str]:
        """能力域。交易账户 → trade；管理员 → 按角色给后台能力。"""
        if self.kind == "user":
            return frozenset({"trade"})
        return frozenset({"admin", "admin." + self.role})

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


def as_principal(obj: Any) -> Principal:
    """把 ORM 对象包装成 Principal（按类名判断，避免循环导入）。"""
    cls = type(obj).__name__
    if cls == "Admin":
        return Principal(
            kind="admin",
            id=int(obj.id or 0),
            username=obj.username,
            display_name=obj.display_name or obj.username,
            role="admin",
            raw=obj,
        )
    return Principal(
        kind="user",
        id=int(obj.id or 0),
        username=obj.username,
        display_name=obj.display_name or obj.username,
        role=getattr(getattr(obj, "role", None), "value", "retail"),
        raw=obj,
    )


# --------------------------------------------------------------------------- #
# 登录防爆破（两条登录链路共用同一套状态机）
# --------------------------------------------------------------------------- #
def is_locked(subject: Any) -> int:
    """返回剩余锁定秒数；未锁定返回 0。"""
    import time

    locked_until = float(getattr(subject, "locked_until", 0.0) or 0.0)
    remain = int(locked_until - time.time())
    return remain if remain > 0 else 0


def register_failure(session: Session, subject: Any) -> None:
    """记一次失败；到阈值就锁定并清零计数（与旧行为一致）。"""
    import time

    subject.fail_count = int(getattr(subject, "fail_count", 0) or 0) + 1
    if subject.fail_count >= settings.login_fail_limit:
        subject.locked_until = time.time() + LOCK_SECONDS
        subject.fail_count = 0
    session.add(subject)
    session.commit()


def register_success(session: Session, subject: Any) -> None:
    """登录成功：清零失败计数、解除锁定、记录时间。"""
    import time

    subject.fail_count = 0
    subject.locked_until = 0.0
    subject.last_login_at = time.time()
    session.add(subject)
    session.commit()
    session.refresh(subject)
