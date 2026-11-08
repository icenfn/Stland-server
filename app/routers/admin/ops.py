"""管理端 · 撮合 / 业绩 / 系统事件 / 结算。"""
from __future__ import annotations

from ._common import *  # noqa: F401,F403
from ._common import router


# --------------------------------------------------------------------------- #
# 撮合 / 业绩 / 系统事件 / 结算
# --------------------------------------------------------------------------- #
@router.get("/matching")
def matching_stats(session: Session = Depends(get_session), admin: Admin = Depends(admin_user)) -> dict:
    """撮合质量：成交率、撤单率、平均单笔金额、成交额 Top 标的。"""
    orders = session.exec(select(Order)).all()
    trades = session.exec(select(Trade)).all()
    filled = sum(1 for o in orders if o.status == OrderStatus.FILLED)
    cancelled = sum(1 for o in orders if o.status == OrderStatus.CANCELLED)
    by_code: dict[str, dict] = {}
    for t in trades:
        b = by_code.setdefault(t.code, {"code": t.code, "trades": 0, "amount": 0.0, "volume": 0})
        b["trades"] += 1
        b["amount"] += t.amount
        b["volume"] += t.quantity

    stocks = {s.code: s.name for s in session.exec(select(Stock)).all()}
    top = sorted(by_code.values(), key=lambda x: x["amount"], reverse=True)[:10]
    for b in top:
        b["name"] = stocks.get(b["code"], b["code"])
        b["amount"] = round(b["amount"], 2)

    avg_amount = round(sum(t.amount for t in trades) / len(trades), 2) if trades else 0.0
    return {
        "order_count": len(orders),
        "trade_count": len(trades),
        "fill_rate_pct": round(filled / len(orders) * 100, 2) if orders else 0.0,
        "cancel_rate_pct": round(cancelled / len(orders) * 100, 2) if orders else 0.0,
        "avg_trade_amount": avg_amount,
        "top_symbols": top,
    }
