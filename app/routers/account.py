"""账户路由：资产总览、持仓、资金流水、风险偏好。

本轮的两处口径调整：
1. **删除「风险画像」衍生指标**（持仓集中度 HHI、组合贝塔、现金比）。
   这些是研究者视角的组合分析，对散户产品只增加噪音；资产页真正该展示的是
   「我有多少钱、持仓赚亏多少」。
2. **「风险画像」改名为「风险偏好」，并删除历史档案表**。
   业务只需要知道「当前偏好是什么、什么时候测的」，不需要合规式的全量留痕。
   因此等级/总分/测评时间直接落在 `user` 行上，测评时覆盖更新。

同时删除 `/performance`（业绩表现）接口：它的实现是从成交记录反推资产曲线，
在模拟盘里既不准也没有产品意义。
"""
from __future__ import annotations

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

# 风险偏好等级：0 未测评 / 1 保守 / 2 稳健 / 3 进取
RISK_LEVEL_LABELS = {0: "未测评", 1: "保守型", 2: "稳健型", 3: "进取型"}


def risk_payload(user: User) -> dict:
    """统一的「风险偏好」视图：客户端只看这几个字段就知道该显示什么。"""
    assessed = user.risk_assessed_at > 0 and user.risk_level > 0
    return {
        "assessed": assessed,
        "level": user.risk_level if assessed else 0,
        "label": RISK_LEVEL_LABELS.get(user.risk_level if assessed else 0, "未测评"),
        "score": user.risk_score if assessed else 0,
        "assessed_at": user.risk_assessed_at if assessed else 0.0,
    }


@router.get("/summary", dependencies=[Depends(signed_request)])
def summary(session: Session = Depends(get_session), user: User = Depends(current_user)) -> dict:
    settle_positions(session, user)
    session.commit()
    session.refresh(user)
    positions = session.exec(select(Position).where(Position.user_id == user.id)).all()

    # 当日盈亏：持仓浮动（最新价对昨收）
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
        "risk": risk_payload(user),
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


class RiskIn(BaseModel):
    """风险偏好测评提交：客户端传原始答案与问卷版本，服务端复算分数。

    为什么不让客户端直接提交等级：那等于「让被评人自己宣布结论」，既无法复核
    也不一致。改为传 answers + version，服务端用同一份口径算分，客户端只负责呈现。
    """

    version: int = Field(default=1, ge=1, le=99)
    answers: list[int] = Field(default_factory=list, max_length=32)


# 评分口径（与客户端问卷一一对应）：每题选项分值
RISK_OPTION_SCORES: tuple[tuple[int, ...], ...] = (
    (0, 2, 4, 6),
    (0, 2, 4, 6),
    (0, 2, 4, 6),
    (0, 2, 4, 6),
    (0, 2, 4, 6),
)
RISK_MAX_SCORE = sum(max(row) for row in RISK_OPTION_SCORES)  # 30


def score_to_level(total: int) -> int:
    """分档：<1/3 保守，中间稳健，>2/3 进取 —— 按满分比例切，避免满分变化后阈值失效。"""
    third = RISK_MAX_SCORE / 3
    if total < third:
        return 1
    if total < third * 2:
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
    """提交风险偏好测评：服务端复算 → 直接覆盖账户上的偏好字段。

    写入 `risk_assessed_at` 是本轮修复「测评后重进仍显示开始测评」的关键：
    客户端判断是否已测评，只认这个时间戳，不依赖任何前端缓存或历史表。
    """
    total, level = compute_risk(payload.answers)
    now = time.time()

    user.risk_level = level
    user.risk_score = total
    user.risk_assessed_at = now
    user.updated_at = now
    session.add(user)
    session.commit()
    session.refresh(user)

    return {"risk": risk_payload(user), "version": payload.version}


@router.get("/risk", dependencies=[Depends(signed_request)])
def get_risk(session: Session = Depends(get_session), user: User = Depends(current_user)) -> dict:
    """读取当前风险偏好。未测评时 assessed=False，客户端据此引导去做测评。"""
    session.refresh(user)
    return risk_payload(user)
