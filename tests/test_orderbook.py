"""用户订单簿撮合测试。

核心断言：一笔成交的两端必须是**两个真实账户**，股票与资金在用户之间转移。
原实现里对手盘来自 ``market_engine.depth()`` 的随机五档，机构卖出的股票
凭空生成、零售买入的凭空消失 —— 这组测试就是为了钉住「不再凭空造股票」。
"""
from __future__ import annotations

import time

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.engine import orderbook
from app.models import Order, OrderSide, OrderStatus, OrderType


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _order(
    user_id: int,
    side: OrderSide,
    price: float,
    quantity: int,
    *,
    code: str = "600000.SH",
    order_type: OrderType = OrderType.LIMIT,
    created_at: float | None = None,
    status: OrderStatus = OrderStatus.PENDING,
    filled: int = 0,
) -> Order:
    return Order(
        order_no=f"O{user_id}{int(price * 100)}{quantity}",
        user_id=user_id,
        code=code,
        side=side,
        order_type=order_type,
        price=price,
        quantity=quantity,
        filled_quantity=filled,
        status=status,
        created_at=created_at if created_at is not None else time.time(),
    )


def test_matches_lowest_ask_first(session):
    """买单先吃最便宜的卖单 —— 价格优先。"""
    cheap = _order(2, OrderSide.SELL, 10.00, 100)
    pricey = _order(3, OrderSide.SELL, 10.10, 100)
    session.add(cheap)
    session.add(pricey)
    session.commit()

    plan = orderbook.match_against_book(
        session, "600000.SH", OrderSide.BUY, 150, limit_price=10.20
    )

    assert [m.quantity for m in plan] == [100, 50]
    assert [m.price for m in plan] == [10.00, 10.10]


def test_time_priority_on_same_price(session):
    """同价按时间先后成交 —— 先挂的先成交。"""
    early = _order(2, OrderSide.SELL, 10.00, 100, created_at=1000.0)
    late = _order(3, OrderSide.SELL, 10.00, 100, created_at=2000.0)
    session.add(early)
    session.add(late)
    session.commit()

    plan = orderbook.match_against_book(
        session, "600000.SH", OrderSide.BUY, 100, limit_price=10.00
    )

    assert len(plan) == 1
    assert plan[0].order.id == early.id


def test_limit_price_stops_matching(session):
    """限价 10.00 的买单不能去吃 10.10 的卖单。"""
    session.add(_order(2, OrderSide.SELL, 10.10, 100))
    session.commit()

    plan = orderbook.match_against_book(
        session, "600000.SH", OrderSide.BUY, 100, limit_price=10.00
    )

    assert plan == []


def test_market_order_has_no_limit(session):
    """市价单不设价格上限，一路吃。"""
    session.add(_order(2, OrderSide.SELL, 10.50, 100))
    session.commit()

    plan = orderbook.match_against_book(
        session, "600000.SH", OrderSide.BUY, 100, limit_price=None
    )

    assert len(plan) == 1
    assert plan[0].price == 10.50


def test_sell_takes_highest_bid_first(session):
    """卖单先吃出价最高的买单。"""
    low = _order(2, OrderSide.BUY, 9.90, 100)
    high = _order(3, OrderSide.BUY, 10.00, 100)
    session.add(low)
    session.add(high)
    session.commit()

    plan = orderbook.match_against_book(
        session, "600000.SH", OrderSide.SELL, 100, limit_price=9.80
    )

    assert plan[0].price == 10.00


def test_self_trade_excluded(session):
    """同一账户的买卖单不能互吃 —— 否则只是左手倒右手还白交手续费。"""
    session.add(_order(7, OrderSide.SELL, 10.00, 100))
    session.commit()

    plan = orderbook.match_against_book(
        session, "600000.SH", OrderSide.BUY, 100,
        limit_price=10.00, exclude_user_id=7,
    )

    assert plan == []


def test_market_orders_never_rest(session):
    """市价单不挂盘：它自己不该成为别人的对手盘。"""
    session.add(_order(2, OrderSide.SELL, 10.00, 100, order_type=OrderType.MARKET))
    session.commit()

    plan = orderbook.match_against_book(
        session, "600000.SH", OrderSide.BUY, 100, limit_price=10.00
    )

    assert plan == []


def test_partially_filled_order_only_offers_remainder(session):
    """已部分成交的挂单，盘上只剩余量。"""
    session.add(_order(2, OrderSide.SELL, 10.00, 100, filled=60,
                       status=OrderStatus.PARTIAL))
    session.commit()

    plan = orderbook.match_against_book(
        session, "600000.SH", OrderSide.BUY, 100, limit_price=10.00
    )

    assert sum(m.quantity for m in plan) == 40


def test_quote_levels_aggregated_and_ordered(session):
    """档位聚合：同价合并，买盘从高到低、卖盘从低到高。"""
    session.add(_order(2, OrderSide.SELL, 10.00, 100))
    session.add(_order(3, OrderSide.SELL, 10.00, 50))
    session.add(_order(4, OrderSide.SELL, 10.05, 100))
    session.add(_order(5, OrderSide.BUY, 9.95, 100))
    session.commit()

    asks = orderbook.quote_levels(session, "600000.SH", "sell")
    bids = orderbook.quote_levels(session, "600000.SH", "buy")

    assert asks[0] == {"price": 10.00, "volume": 150}
    assert asks[1]["price"] == 10.05
    assert bids[0]["price"] == 9.95


def test_other_codes_isolated(session):
    """不同标的的委托不能互相撮合。"""
    session.add(_order(2, OrderSide.SELL, 10.00, 100, code="000001.SZ"))
    session.commit()

    plan = orderbook.match_against_book(
        session, "600000.SH", OrderSide.BUY, 100, limit_price=10.00
    )

    assert plan == []
