"""账务账户：读写一个交易账户的资金状态。

这一层存在的唯一目的，是让「拿账户的钱」这件事只有一个入口。
重构前 `user.cash` / `user.frozen_cash` / `user.total_asset` 散落在交易、路由、
对账、后台统计里各写各的，口径稍有差异就会出现「接口显示的余额和对账器算的不一致」。

这里的约定
----------
* ``of(session, user)`` —— 取（必要时惰性创建）账户的资金行，永远不为 None；
* ``recompute(acct)`` —— 改完 ``cash`` / ``frozen_cash`` / ``market_value`` 后
  统一重算 ``total_asset``，避免每个调用点各写一遍加法；
* ``snapshot(acct)`` —— 对外序列化，统一保留 2 位小数。

惰性创建是有意的：老库里已有的 `user` 行没有对应 `account` 行，
第一次被访问时补一行比要求运维跑迁移脚本更省事，也天然兼容测试中
「只建 User 不建 Account」的写法（测试里那一行会被自动补上）。
"""
from __future__ import annotations

import time

from sqlmodel import Session, select

from ..models import Account, User


def of(session: Session, user: User | int) -> Account:
    """取账户资金行；不存在则按「初始 0 元」补一行并落库。"""
    user_id = user if isinstance(user, int) else (user.id or 0)
    acct = session.exec(select(Account).where(Account.user_id == user_id)).first()
    if acct is None:
        acct = Account(user_id=user_id)
        session.add(acct)
        session.flush()
    return acct


def find(session: Session, user_id: int) -> Account | None:
    """只查不建，用于「读不到就当空账户」的展示型场景。"""
    return session.exec(select(Account).where(Account.user_id == user_id)).first()


def recompute(acct: Account) -> Account:
    """重算总资产快照。任何改动 cash / frozen_cash / market_value 之后都要调。"""
    acct.total_asset = round(acct.cash + acct.frozen_cash + acct.market_value, 4)
    acct.updated_at = time.time()
    return acct


def touch(acct: Account) -> Account:
    """仅刷新修改时间（金额未变但需要标记脏行的场景）。"""
    acct.updated_at = time.time()
    return acct


def snapshot(acct: Account | None) -> dict:
    """对外可见的资金视图。``acct`` 为 None 时返回全零，避免调用方判空。"""
    if acct is None:
        return {
            "cash": 0.0,
            "frozen_cash": 0.0,
            "market_value": 0.0,
            "total_asset": 0.0,
            "realized_pnl": 0.0,
            "fee_paid": 0.0,
            "credit_limit": 0.0,
            "commission_rate": None,
            "commission_min": None,
        }
    return {
        "cash": round(acct.cash, 2),
        "frozen_cash": round(acct.frozen_cash, 2),
        "market_value": round(acct.market_value, 2),
        "total_asset": round(acct.total_asset, 2),
        "realized_pnl": round(acct.realized_pnl, 2),
        "fee_paid": round(acct.fee_paid, 2),
        "credit_limit": round(acct.credit_limit, 2),
        "commission_rate": acct.commission_rate,
        "commission_min": acct.commission_min,
    }


def available_cash(acct: Account, role_value: str) -> float:
    """可动用资金 = 可用资金 + 机构授信额度（零售没有额度）。"""
    extra = acct.credit_limit if role_value == "institution" else 0.0
    return acct.cash + extra
