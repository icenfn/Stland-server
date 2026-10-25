"""交易路由：下单、撤单、委托列表、成交列表、预估。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from ..db import get_session
from ..engine.book import Level, walk_book, vwap
from ..engine.market import market_engine
from ..models import Order, OrderSide, OrderStatus, Stock, Trade, User
from ..services import trading
from ..services.deps import current_user, signed_request
from ..services.fees import calc_fee, max_buy_quantity

router = APIRouter(prefix="/api/trade", tags=["trade"])


class OrderIn(BaseModel):
    code: str = Field(min_length=6, max_length=6)
    side: OrderSide
    quantity: int = Field(gt=0, le=10_000_000)
    price: float | None = Field(default=None, ge=0)
    order_type: str = Field(default="limit", pattern="^(limit|market)$")


class CancelIn(BaseModel):
    order_id: int


def order_view(o: Order) -> dict:
    return {
        "id": o.id,
        "order_no": o.order_no,
        "code": o.code,
        "side": o.side.value,
        "order_type": o.order_type.value,
        "price": o.price,
        "quantity": o.quantity,
        "filled_quantity": o.filled_quantity,
        "avg_fill_price": o.avg_fill_price,
        "status": o.status.value,
        "fee": round(o.fee, 2),
        "reject_reason": o.reject_reason,
        "created_at": o.created_at,
        "updated_at": o.updated_at,
    }


@router.post("/order", dependencies=[Depends(signed_request)])
def create_order(
    payload: OrderIn,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> dict:
    try:
        order = trading.place_order(
            session,
            user,
            code=payload.code,
            side=payload.side.value,
            quantity=payload.quantity,
            price=payload.price,
            order_type=payload.order_type,
        )
    except trading.TradeError as exc:
        raise HTTPException(400, detail={"code": exc.code, "message": exc.message}) from exc

    trades = session.exec(select(Trade).where(Trade.order_id == order.id)).all()
    return {
        "order": order_view(order),
        "trades": [
            {
                "trade_no": t.trade_no,
                "price": t.price,
                "quantity": t.quantity,
                "amount": round(t.amount, 2),
                "fee": round(t.fee + t.tax + t.transfer_fee, 2),
                "created_at": t.created_at,
            }
            for t in trades
        ],
    }


@router.post("/cancel", dependencies=[Depends(signed_request)])
def cancel(
    payload: CancelIn,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> dict:
    try:
        order = trading.cancel_order(session, user, payload.order_id)
    except trading.TradeError as exc:
        raise HTTPException(400, detail={"code": exc.code, "message": exc.message}) from exc
    return {"order": order_view(order)}


@router.get("/orders")
def orders(
    status_filter: str = Query(default="all", alias="status", pattern="^(all|open|filled|cancelled)$"),
    limit: int = Query(default=50, ge=1, le=300),
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> dict:
    stmt = select(Order).where(Order.user_id == user.id)
    if status_filter == "open":
        stmt = stmt.where(Order.status.in_([OrderStatus.PENDING, OrderStatus.PARTIAL]))
    elif status_filter == "filled":
        stmt = stmt.where(Order.status == OrderStatus.FILLED)
    elif status_filter == "cancelled":
        stmt = stmt.where(Order.status.in_([OrderStatus.CANCELLED, OrderStatus.REJECTED]))
    rows = session.exec(stmt.order_by(Order.created_at.desc()).limit(limit)).all()
    return {"items": [order_view(o) for o in rows]}


@router.get("/trades")
def trades(
    limit: int = Query(default=50, ge=1, le=300),
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> dict:
    rows = session.exec(
        select(Trade).where(Trade.user_id == user.id).order_by(Trade.created_at.desc()).limit(limit)
    ).all()
    return {
        "items": [
            {
                "trade_no": t.trade_no,
                "order_no": t.order_no,
                "code": t.code,
                "side": t.side.value,
                "price": t.price,
                "quantity": t.quantity,
                "amount": round(t.amount, 2),
                "fee": round(t.fee + t.tax + t.transfer_fee, 2),
                "realized_pnl": round(t.realized_pnl, 2),
                "created_at": t.created_at,
            }
            for t in rows
        ]
    }


@router.get("/preview")
def preview(
    code: str,
    side: OrderSide,
    quantity: int = Query(gt=0),
    price: float | None = None,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> dict:
    """下单前预估：预计成交均价、费用与资金占用（市价单按盘口估算滑点）。"""
    stock = session.get(Stock, code)
    if stock is None:
        raise HTTPException(404, detail={"code": "UNKNOWN_SYMBOL", "message": "标的不存在"})
    depth = market_engine.depth(code) or {"bids": [], "asks": []}
    levels = depth["asks"] if side == OrderSide.BUY else depth["bids"]
    book = [Level(price=float(x["price"]), volume=int(x["volume"])) for x in levels]

    if price and price > 0:
        eff_price = price
        fills = [f for f in walk_book(book, quantity, price, side.value)]
    else:
        fills = walk_book(book, quantity, None, side.value)
        eff_price = vwap(fills) if fills else market_engine.price_of(code)

    fillable = sum(f.quantity for f in fills) if fills else (quantity if price else 0)
    avg_price = vwap(fills) if fills else eff_price
    amount = avg_price * quantity
    fee = calc_fee(amount, side.value)
    return {
        "code": code,
        "side": side.value,
        "quantity": quantity,
        "effective_price": round(eff_price, 2),
        "estimated_avg_price": round(avg_price, 4),
        "fillable_quantity": fillable,
        "slippage_pct": round((avg_price / market_engine.price_of(code) - 1) * 100, 4)
        if market_engine.price_of(code)
        else 0.0,
        "amount": round(amount, 2),
        "fee": {
            "commission": fee.commission,
            "stamp_tax": fee.stamp_tax,
            "transfer_fee": fee.transfer_fee,
            "total": fee.total,
        },
        "max_buy_quantity": max_buy_quantity(user.cash, market_engine.price_of(code), stock.lot_size),
        "available_cash": round(user.cash, 2),
    }
