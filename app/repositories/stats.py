"""统计仓储：后台看板与撮合质量所需的全部聚合。

关键改动（P1-8）：原实现把 `Order` / `Trade` / `Position` 整表 `select().all()`
读进 Python 再 `sum` / `len`。后台首屏随数据量线性劣化，几万条成交后必然卡死。
这里全部改成 SQL 聚合，数据库只回一行数字。
"""
from __future__ import annotations

import time

from sqlalchemy import case, func
from sqlmodel import Session, select

from ..models import (
    Account,
    AccountStatus,
    Admin,
    IdempotencyKey,
    Order,
    OrderStatus,
    Position,
    Role,
    Trade,
    User,
    WatchItem,
)


def account_kpi(session: Session) -> dict:
    """账户维度 KPI：一次条件聚合全拿，避免 6 次分别 count。"""

    def _count(cond) -> int:
        return int(session.exec(select(func.count()).select_from(User).where(cond)).one() or 0)

    total_users = int(session.exec(select(func.count()).select_from(User)).one() or 0)
    # 管理员已独立成表，这里单独统计
    admin_count = int(session.exec(select(func.count()).select_from(Admin)).one() or 0)
    asset_rows = session.exec(
        select(
            func.coalesce(func.sum(Account.total_asset), 0.0),
            func.coalesce(func.sum(Account.cash), 0.0),
            func.coalesce(func.sum(Account.market_value), 0.0),
        )
    ).one()
    # total_asset 允许为 0（尚未结算过的账户），此时用 cash+market_value 兜底
    declared, cash_sum, mv_sum = (float(x or 0.0) for x in asset_rows)
    total_asset = declared or (cash_sum + mv_sum)

    return {
        "user_count": total_users,
        "retail_count": _count(User.role == Role.RETAIL),
        "institution_count": _count(User.role == Role.INSTITUTION),
        "admin_count": admin_count,
        "active_users": _count(User.status == AccountStatus.ACTIVE),
        "frozen_users": _count(User.status == AccountStatus.FROZEN),
        "total_asset": round(total_asset, 2),
    }


def order_kpi(session: Session, since: float | None = None) -> dict:
    """委托/成交 KPI，可按时间窗口切片。"""
    o_count = select(func.count()).select_from(Order)
    t_count = select(func.count()).select_from(Trade)
    if since is not None:
        o_count = o_count.where(Order.created_at >= since)
        t_count = t_count.where(Trade.created_at >= since)

    open_orders = int(
        session.exec(
            select(func.count())
            .select_from(Order)
            .where(Order.status.in_([OrderStatus.PENDING, OrderStatus.PARTIAL]))
        ).one()
        or 0
    )
    turnover_stmt = select(func.coalesce(func.sum(Trade.amount), 0.0))
    if since is not None:
        turnover_stmt = turnover_stmt.where(Trade.created_at >= since)

    return {
        "orders": int(session.exec(o_count).one() or 0),
        "trades": int(session.exec(t_count).one() or 0),
        "open_orders": open_orders,
        "turnover": round(float(session.exec(turnover_stmt).one() or 0.0), 2),
    }


def position_rows(session: Session) -> int:
    return int(session.exec(select(func.count()).select_from(Position)).one() or 0)


def watch_items(session: Session) -> int:
    return int(session.exec(select(func.count()).select_from(WatchItem)).one() or 0)


def dashboard(session: Session) -> dict:
    """看板所需的全部聚合，替代原先 5 次整表加载。"""
    today = time.time() - 86400
    return {
        "accounts": account_kpi(session),
        "today": order_kpi(session, since=today),
        "all_time": order_kpi(session),
        "position_rows": position_rows(session),
        "watch_items": watch_items(session),
    }


def leaderboard(session: Session, limit: int = 20) -> list[dict]:
    """按总资产排名，SQL 排序 + 截断。"""
    # 排序键在 account 表上：JOIN 一次取回身份 + 资金，避免按「有资金行的账户」
    # 排序后再逐个回查身份（那会变成 N+1）。
    rows = session.exec(
        select(User, Account)
        .join(Account, Account.user_id == User.id)
        .order_by(Account.total_asset.desc())
        .limit(limit)
    ).all()
    return [
        {
            "username": u.username,
            "display_name": u.display_name or u.username,
            "role": u.role.value,
            "total_asset": round(a.total_asset, 2),
            "realized_pnl": round(a.realized_pnl, 2),
            "return_pct": round(a.realized_pnl / a.total_asset * 100, 2) if a.total_asset else 0.0,
        }
        for u, a in rows
    ]


def matching_quality(session: Session, top_limit: int = 10) -> dict:
    """撮合质量：状态计数与成交额 Top 标的全部交给 SQL。"""
    status_rows = session.exec(
        select(Order.status, func.count()).group_by(Order.status)
    ).all()
    by_status = {str(s): int(c) for s, c in status_rows}
    order_count = sum(by_status.values())
    filled = by_status.get(OrderStatus.FILLED.value, 0)
    cancelled = by_status.get(OrderStatus.CANCELLED.value, 0)

    trade_rows = session.exec(
        select(
            func.count(),
            func.coalesce(func.sum(Trade.amount), 0.0),
        ).select_from(Trade)
    ).one()
    trade_count, total_amount = int(trade_rows[0] or 0), float(trade_rows[1] or 0.0)

    top = session.exec(
        select(
            Trade.code,
            func.count().label("trades"),
            func.coalesce(func.sum(Trade.amount), 0.0).label("amount"),
            func.coalesce(func.sum(Trade.quantity), 0).label("volume"),
        )
        .group_by(Trade.code)
        .order_by(func.sum(Trade.amount).desc())
        .limit(top_limit)
    ).all()
    names = {
        s.code: s.name
        for s in session.exec(select(*_stock_cols())).all()
    }
    top_symbols = [
        {
            "code": code,
            "name": names.get(code, code),
            "trades": int(cnt),
            "amount": round(float(amount), 2),
            "volume": int(volume),
        }
        for code, cnt, amount, volume in top
    ]

    return {
        "order_count": order_count,
        "trade_count": trade_count,
        "fill_rate_pct": round(filled / order_count * 100, 2) if order_count else 0.0,
        "cancel_rate_pct": round(cancelled / order_count * 100, 2) if order_count else 0.0,
        "avg_trade_amount": round(total_amount / trade_count, 2) if trade_count else 0.0,
        "top_symbols": top_symbols,
    }


def _stock_cols():
    from ..models import Stock

    return (Stock.code, Stock.name)


def idempotency_stats(session: Session) -> dict:
    """幂等键表的观测量：方便确认重试保护是否真的在工作。"""
    now = time.time()
    total = int(session.exec(select(func.count()).select_from(IdempotencyKey)).one() or 0)
    expired = int(
        session.exec(
            select(func.count()).select_from(IdempotencyKey).where(IdempotencyKey.expires_at < now)
        ).one()
        or 0
    )
    rows = session.exec(
        select(IdempotencyKey.scope, func.count()).group_by(IdempotencyKey.scope)
    ).all()
    return {
        "total": total,
        "expired": expired,
        "by_scope": {str(scope): int(cnt) for scope, cnt in rows},
    }
