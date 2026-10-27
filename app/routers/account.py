"""账户路由：资产总览、持仓、资金流水、业绩曲线、风控设置。"""
from __future__ import annotations

import json
import math
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from ..db import get_session
from ..engine.market import market_engine
from ..models import CashFlow, Position, RiskAssessment, Stock, Trade, User
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
    """风险测评提交：客户端传原始答案与问卷版本，服务端自己算分并落档。

    为什么不让客户端直接传 level
    --------------------------
    客户端传 level 等于「让被评人自己宣布结论」，既无法复核也无法审计。
    改为传 answers + version，服务端用同一份评分口径复算：
      · 分数、等级由服务端产生，客户端只负责呈现；
      · 每次提交写一条 RiskAssessment，历史留痕；
      · User.risk_level 冗余保存最新等级，供资产页与后台快速查询。
    """

    version: int = Field(default=1, ge=1, le=99)
    answers: list[int] = Field(default_factory=list, max_length=32)
    score: int | None = Field(default=None, ge=0, le=100)


# 评分口径（与客户端问卷一一对应）：每题选项分值与各档阈值
RISK_OPTION_SCORES: tuple[tuple[int, ...], ...] = (
    (0, 1, 2, 4),
    (0, 1, 2, 4),
    (0, 1, 2, 4),
    (0, 1, 2, 4),
    (0, 1, 2, 4),
)


def score_to_level(total: int) -> int:
    if total <= 7:
        return 1
    if total <= 13:
        return 2
    return 3


def compute_risk(answers: list[int]) -> tuple[int, int]:
    """按题目下标与选项下标复算总分与等级（越界选项按 0 分处理，不报错）。"""
    total = 0
    for qi, oi in enumerate(answers):
        if qi >= len(RISK_OPTION_SCORES):
            break
        row = RISK_OPTION_SCORES[qi]
        if 0 <= oi < len(row):
            total += row[oi]
    return total, score_to_level(total)


@router.post("/risk", dependencies=[Depends(signed_request)])
def set_risk(
    payload: RiskIn,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> dict:
    """提交风险测评：服务端复算分数 → 归档 → 更新账户当前等级。"""
    total, level = compute_risk(payload.answers)
    # 答案为空但给了分数（兼容旧客户端直接赋值），退回按分数折算
    if not payload.answers and payload.score is not None:
        total = int(payload.score)
        level = score_to_level(total)

    rec = RiskAssessment(
        user_id=user.id,
        version=payload.version,
        score=total,
        level=level,
        answers=json.dumps(payload.answers[:32], separators=(",", ":")),
        source="client",
        assessed_at=time.time(),
    )
    session.add(rec)

    user.risk_level = level
    user.updated_at = time.time()
    session.add(user)
    session.commit()
    session.refresh(rec)
    return {
        "risk_level": level,
        "score": total,
        "assessed_at": rec.assessed_at,
        "assessment_id": rec.id,
    }


@router.get("/risk", dependencies=[Depends(signed_request)])
def get_risk(session: Session = Depends(get_session), user: User = Depends(current_user)) -> dict:
    """读取当前风险画像：最新一条测评 + 历史条数。

    未测评时返回 assessed=False，且不编造等级，
    客户端据此决定「展示结论」还是「引导去做测评」。
    """
    rows = session.exec(
        select(RiskAssessment)
        .where(RiskAssessment.user_id == user.id)
        .order_by(RiskAssessment.assessed_at.desc())
    ).all()
    if not rows:
        return {"assessed": False, "risk_level": 0, "history_count": 0}
    latest = rows[0]
    return {
        "assessed": True,
        "risk_level": latest.level,
        "score": latest.score,
        "version": latest.version,
        "assessed_at": latest.assessed_at,
        "history_count": len(rows),
    }
