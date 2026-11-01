"""用户订单簿：把其他用户未成交的委托当作真实对手盘。

原实现的对手盘来自 ``market_engine.depth()`` —— 由 ``register()`` 时
``build_levels(prev_close, ..., rng=...)`` 生成的五档随机数，且
``walk_book()`` 只读不扣减，等于无限流动性。结果是「用户在和一段随机数
对赌」：机构卖出的股票凭空生成、零售买入的股票凭空消失，双方永远不会
真正成交 —— 这不是市场。

这里把 resting 委托（``PENDING`` / ``PARTIAL`` 的限价单）按
**价格优先、时间优先** 排成盘口，撮合时逐档吃单并扣减对手方剩余量。
一笔成交的两端都是真实账户，股票与资金在用户之间转移。

设计取舍
--------
* 只把 **限价单** 挂上盘口。市价单的语义是「立刻成交或撤销」，它自己
  不会在簿上等待 —— 否则一个没人接的市价单会永久占据一个价位，
  后来者以为那里有可成交的流动性。
* 盘口按需现算，不在内存里维护常驻簿。委托状态本来就在 SQLite 里，
  再复制一份内存簿，就得同步两处状态（撤单、改单、日切、重启），
  错一处就是「盘口显示有单、实际撮不到」。委托量级下现算足够快。
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlmodel import Session, select

from ..models import Order, OrderSide, OrderStatus, OrderType

#: 挂在盘上、还能被对手方吃到的状态。
RESTING_STATUS = (OrderStatus.PENDING, OrderStatus.PARTIAL)


@dataclass
class RestingMatch:
    """一笔可以成交的对手方委托。"""

    order: Order
    price: float
    quantity: int


def remaining(order: Order) -> int:
    """这笔委托还能成交多少股。"""
    return max(0, int(order.quantity) - int(order.filled_quantity or 0))


def resting_orders(
    session: Session,
    code: str,
    taker_side: OrderSide,
    exclude_user_id: int | None = None,
    exclude_order_id: int | None = None,
) -> list[Order]:
    """对手方 resting 委托，已按价格优先、时间优先排好序。

    买盘要吃的是卖方的挂单（价格低的先成交）；卖盘要吃买方的挂单
    （价格高的先成交）。同价一律按 ``created_at``、再用 id 兜底，
    保证同一 tick 内下的单也有确定的先后 —— 否则两次撮合结果可能不同。

    ``exclude_user_id`` / ``exclude_order_id`` 用来排除自成交：同一账户
    自己挂的卖单不该被自己的买单吃掉，否则只是把钱从左手倒到右手，
    还会虚增成交量和手续费。
    """
    opposite = OrderSide.SELL if taker_side == OrderSide.BUY else OrderSide.BUY
    stmt = select(Order).where(
        Order.code == code,
        Order.side == opposite,
        Order.order_type == OrderType.LIMIT,
        Order.status.in_(RESTING_STATUS),
    )
    if exclude_user_id is not None:
        stmt = stmt.where(Order.user_id != exclude_user_id)
    if exclude_order_id is not None:
        stmt = stmt.where(Order.id != exclude_order_id)
    rows = session.exec(stmt).all()

    live = [o for o in rows if remaining(o) > 0]
    # 卖方：价低优先（升序）；买方：价高优先（降序）
    sign = 1 if opposite == OrderSide.SELL else -1
    live.sort(key=lambda o: (sign * float(o.price), float(o.created_at), o.id or 0))
    return live


def match_against_book(
    session: Session,
    code: str,
    taker_side: OrderSide,
    quantity: int,
    limit_price: float | None,
    exclude_user_id: int | None = None,
    exclude_order_id: int | None = None,
) -> list[RestingMatch]:
    """按盘口逐档吃 resting 委托，返回成交计划（**不修改任何状态**）。

    ``limit_price`` 为 ``None`` 表示市价单：不设价格约束，一路吃到没量为止。
    限价单则在对手价越过限价时停止 —— 再往下吃就是让用户以更差的价格成交。

    为什么不在这里直接扣减：撮合要在「有成交」之后才决定怎么入账
    （部分成交要保留冻结、市价单剩余要撤销）。先出计划、再统一落账，
    中间任何一步失败都不会留下「扣了量却没记账」的半截状态。
    """
    matches: list[RestingMatch] = []
    left = int(quantity)
    if left <= 0:
        return matches

    for order in resting_orders(session, code, taker_side, exclude_user_id, exclude_order_id):
        if left <= 0:
            break
        price = float(order.price)
        if limit_price is not None:
            if taker_side == OrderSide.BUY and price > limit_price:
                break
            if taker_side == OrderSide.SELL and price < limit_price:
                break
        available = remaining(order)
        if available <= 0:
            continue
        take = min(left, available)
        matches.append(RestingMatch(order=order, price=price, quantity=take))
        left -= take

    return matches


def quote_levels(
    session: Session, code: str, side: str, limit: int = 5
) -> list[dict]:
    """把 resting 委托聚合成五档盘口，供前端展示。

    ``side="buy"`` 返回买盘（价格从高到低），``side="sell"`` 返回卖盘
    （价格从低到高）—— 与真实行情软件的档位顺序一致。
    """
    taker = OrderSide.BUY if side == "sell" else OrderSide.SELL
    buckets: dict[float, int] = {}
    for order in resting_orders(session, code, taker):
        price = round(float(order.price), 4)
        buckets[price] = buckets.get(price, 0) + remaining(order)

    rows = sorted(buckets.items(), key=lambda kv: kv[0], reverse=(side == "buy"))
    return [{"price": price, "volume": vol} for price, vol in rows[:limit]]
