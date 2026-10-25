"""账户路由：资产总览、持仓、资金流水、业绩曲线、风控设置。"""
from __future__ import annotations

import math
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from ..db import get_session
from ..engine.market import market_engine
from ..models import CashFlow, Position, Stock, Trade, User
from ..services.deps import current_user, signed_request
from ..services.trading import settle_positions

router = APIRouter(prefix="/api/account", tags=["account"])


@router.get("/summary", dependencies=[Depends(signed_request)])
def summary(session: Session = Depends(get_session), user: User = Depends(current_user)) -> dict:
    settle_positions(session, user)
    session.commit()
    positions = session.exec(select(Position).where(Position.user_id == user.id)).all()

    # 持仓集中度（HHI）与组合贝塔，作为风控展示指标
    values = [p.market_value for p in positions if p.market_value > 0]
    total_mv = sum(values)
    hhi = round(sum((v / total_mv) ** 2 for v in values) * 10000, 2) if total_mv > 0 else 0.0

    stocks = {s.code: s for s in session.exec(select(Stock)).all()}
    beta = 0.0
    if total_mv > 0:
        beta = sum(p.market_value * stocks[p.code].beta for p in positions if p.code in stocks) / total_mv

    # 当日盈亏：持仓浮动 + 已实现（简化口径，按最新价对昨收的差）
    day_pnl = 0.0
    for p in positions:
        q = market_engine.quote(p.code)
        if q:
            day_pnl += (float(q["price"]) - float(q["prev_close"])) * p.quantity

    return {
        "user": {
            "id": user.id,
            "username": user.username,
            "display_name": user.display_name or user.username,
            "role": user.role.value,
            "risk_level": user.risk_level,
        },
        "asset": {
            "cash": round(user.cash, 2),
            "frozen_cash": round(user.frozen_cash, 2),
            "market_value": round(user.market_value, 2),
            "total_asset": round(user.total_asset, 2),
            "credit_limit": round(user.credit_limit, 2),
            "realized_pnl": round(user.realized_pnl, 2),
            "fee_paid": round(user.fee_paid, 2),
            "day_pnl": round(day_pnl, 2),
        },
        "risk": {
            "position_count": len(positions),
            "concentration_hhi": hhi,
            "portfolio_beta": round(beta, 3),
            "cash_ratio": round(user.cash / user.total_asset * 100, 2) if user.total_asset else 0.0,
        },
    }


@router.get("/positions", dependencies=[Depends(signed_request)])
def positions(session: Session = Depends(get_session), user: User = Depends(current_user)) -> dict:
    settle_positions(session, user)
    session.commit()
    rows = session.exec(select(Position).where(Position.user_id == user.id)).all()
    stocks = {s.code: s for s in session.exec(select(Stock)).all()}
    items = []
    for p in rows:
        q = market_engine.quote(p.code) or {}
        cost = p.avg_cost * p.quantity
        items.append(
            {
                "code": p.code,
                "name": stocks[p.code].name if p.code in stocks else p.code,
                "quantity": p.quantity,
                "available": p.available,
                "frozen": p.frozen,
                "avg_cost": round(p.avg_cost, 4),
                "last_price": p.last_price,
                "market_value": round(p.market_value, 2),
                "unrealized_pnl": round(p.unrealized_pnl, 2),
                "unrealized_pct": round(p.unrealized_pnl / cost * 100, 2) if cost > 0 else 0.0,
                "day_change_pct": q.get("change_pct", 0.0),
            }
        )
    items.sort(key=lambda x: x["market_value"], reverse=True)
    return {"items": items}


@router.get("/flows", dependencies=[Depends(signed_request)])
def flows(
    limit: int = Query(default=50, ge=1, le=300),
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> dict:
    rows = session.exec(
        select(CashFlow).where(CashFlow.user_id == user.id).order_by(CashFlow.created_at.desc()).limit(limit)
    ).all()
    return {
        "items": [
            {
                "biz_type": r.biz_type,
                "amount": round(r.amount, 2),
                "balance_before": round(r.balance_before, 2),
                "balance_after": round(r.balance_after, 2),
                "ref_no": r.ref_no,
                "remark": r.remark,
                "created_at": r.created_at,
            }
            for r in rows
        ]
    }


@router.get("/performance", dependencies=[Depends(signed_request)])
def performance(session: Session = Depends(get_session), user: User = Depends(current_user)) -> dict:
    """业绩曲线：由成交记录重建每日资产快照，并计算夏普、最大回撤等指标。"""
    settle_positions(session, user)
    trades = session.exec(select(Trade).where(Trade.user_id == user.id).order_by(Trade.created_at)).all()

    start_equity = user.total_asset - user.realized_pnl
    if start_equity <= 0:
        start_equity = max(user.total_asset, 1.0)

    # 累计已实现盈亏曲线（按成交序号采样），并叠加当前浮盈作为末点
    curve: list[dict] = [{"t": int((trades[0].created_at if trades else time.time()) - 86400), "equity": round(start_equity, 2)}]
    cum = 0.0
    for t in trades:
        cum += t.realized_pnl
        curve.append({"t": int(t.created_at), "equity": round(start_equity + cum, 2)})
    curve.append({"t": int(time.time()), "equity": round(user.total_asset, 2)})

    equity = [p["equity"] for p in curve]
    rets = [
        equity[i] / equity[i - 1] - 1 for i in range(1, len(equity)) if equity[i - 1] > 0
    ]
    mean_r = sum(rets) / len(rets) if rets else 0.0
    var = sum((r - mean_r) ** 2 for r in rets) / len(rets) if len(rets) > 1 else 0.0
    std = math.sqrt(var)
    sharpe = round(mean_r / std * math.sqrt(252), 3) if std > 1e-9 else 0.0

    peak = -1e18
    mdd = 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1)

    # 胜率
    wins = sum(1 for t in trades if t.realized_pnl > 0)
    sells = sum(1 for t in trades if t.realized_pnl != 0)

    return {
        "curve": curve[-200:],
        "metrics": {
            "total_return_pct": round((user.total_asset / start_equity - 1) * 100, 2),
            "realized_pnl": round(user.realized_pnl, 2),
            "fee_paid": round(user.fee_paid, 2),
            "sharpe": sharpe,
            "max_drawdown_pct": round(mdd * 100, 2),
            "win_rate_pct": round(wins / sells * 100, 2) if sells else 0.0,
            "trade_count": len(trades),
        },
    }


class RiskIn(BaseModel):
    risk_level: int = Field(ge=1, le=3)


@router.post("/risk", dependencies=[Depends(signed_request)])
def set_risk(
    payload: RiskIn,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> dict:
    user.risk_level = payload.risk_level
    user.updated_at = time.time()
    session.add(user)
    session.commit()
    return {"risk_level": user.risk_level}


@router.post("/reset")
def reset_account(session: Session = Depends(get_session), user: User = Depends(current_user)) -> dict:
    """演示用：清空该账户的持仓/委托/成交并恢复初始资金。"""
    from ..models import Order, OrderStatus
    from ..config import settings

    for p in session.exec(select(Position).where(Position.user_id == user.id)).all():
        session.delete(p)
    for t in session.exec(select(Trade).where(Trade.user_id == user.id)).all():
        session.delete(t)
    for o in session.exec(select(Order).where(Order.user_id == user.id)).all():
        session.delete(o)
    for f in session.exec(select(CashFlow).where(CashFlow.user_id == user.id)).all():
        session.delete(f)

    base = float(settings.seed_cash) if user.role.value == "retail" else user.total_asset or 50_000_000
    user.cash = base
    user.frozen_cash = 0.0
    user.market_value = 0.0
    user.total_asset = base
    user.realized_pnl = 0.0
    user.fee_paid = 0.0
    user.updated_at = time.time()
    session.add(user)
    session.commit()
    return {"ok": True, "cash": user.cash}
