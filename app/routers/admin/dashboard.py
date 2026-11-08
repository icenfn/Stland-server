"""管理端 · 总览。"""
from __future__ import annotations

from ._common import *  # noqa: F401,F403
from ._common import router


# --------------------------------------------------------------------------- #
# 总览
# --------------------------------------------------------------------------- #
@router.get("/dashboard")
def dashboard(session: Session = Depends(get_session), admin: Admin = Depends(admin_user)) -> dict:
    users = session.exec(select(User)).all()
    admins = session.exec(select(Admin)).all()
    orders = session.exec(select(Order)).all()
    trades = session.exec(select(Trade)).all()
    positions = session.exec(select(Position)).all()
    today_start = time.time() - 86400

    # 总资产从 account 表聚合：User 上已不再有资金字段
    total_asset = float(
        session.exec(
            select(func.coalesce(func.sum(Account.total_asset), 0.0))
        ).one()
        or 0.0
    )
    today_orders = [o for o in orders if o.created_at >= today_start]
    today_trades = [t for t in trades if t.created_at >= today_start]

    st = current_session()
    return {
        "kpi": {
            "user_count": len(users),
            "retail_count": sum(1 for u in users if u.role == Role.RETAIL),
            "institution_count": sum(1 for u in users if u.role == Role.INSTITUTION),
            "admin_count": len(admins),
            "active_users": sum(1 for u in users if u.status == AccountStatus.ACTIVE),
            "frozen_users": sum(1 for u in users if u.status == AccountStatus.FROZEN),
            "total_asset": round(total_asset, 2),
            "today_orders": len(today_orders),
            "today_trades": len(today_trades),
            "today_turnover": round(sum(t.amount for t in today_trades), 2),
            "open_orders": sum(1 for o in orders if o.status in (OrderStatus.PENDING, OrderStatus.PARTIAL)),
            "position_rows": len(positions),
            "watch_items": session.exec(select(func.count()).select_from(WatchItem)).one(),
        },
        "market": market_engine.stats(),
        # 完整时钟快照：结算模式 + 交易时段
        "session": st.to_dict(),
        # 总资产排名并入总览：它本身就是一张排行榜，
        # 不值得单独占一个导航项，而应该一进后台就看到。
        "leaderboard": _leaderboard(session),
    }


def _leaderboard(session: Session, limit: int = 20) -> list[dict]:
    """按总资产排名（零售 + 机构）。

    排序键在 account 表上，所以这里 JOIN 一次取「身份 + 资金」，
    避免为了排序去读全表再在 Python 里 sort。
    """
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
