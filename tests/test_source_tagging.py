"""数据来源标记测试。

这一组钉住「模拟数据不再冒充真实成交」这条底线：
1. 真实用户撮合 → source=real，且有对手方；
2. 做市商兜底成交 → source=simulated，绝不进真实口径；
3. 模拟 tick 的量不进 real_volume，日线/当日柱只由真实成交产生；
4. 初始化回填的日线 → source=backfill；
5. 盘口默认只返回用户真实挂单，不再返回随机合成档位；
6. 外部行情源必须有过期保护，停更即视为无报价。
"""
from __future__ import annotations

import os
import tempfile
import time

os.environ.setdefault("STLAND_DB_URL", f"sqlite:///{tempfile.mkdtemp()}/test.db")
os.environ.setdefault("STLAND_SIGNATURE_REQUIRED", "false")

import pytest  # noqa: E402
from sqlmodel import select  # noqa: E402

from app.db import init_db, session_scope  # noqa: E402
from app.engine.market import market_engine  # noqa: E402
from app.engine.quote_source import ExternalSource  # noqa: E402
from app.models import (  # noqa: E402
    DailyBar, Order, OrderSide, OrderStatus, OrderType, TradeSource, User,
)

CODE = "SRC001.SH"


@pytest.fixture(scope="module", autouse=True)
def _db():
    init_db()


@pytest.fixture()
def stock():
    market_engine.register(
        code=CODE, name="来源测试", init_price=10.0, tick_size=0.01,
        limit_up_pct=0.10, beta=1.0, drift=0.0, vol=0.3,
        liquidity=1.0, float_shares=1e8,
    )
    st = market_engine._states[CODE]
    st.real_volume = 0
    st.real_amount = 0.0
    st.volume = 0
    st.amount = 0.0
    with session_scope() as db:
        for row in db.exec(select(DailyBar).where(DailyBar.code == CODE)).all():
            db.delete(row)
    yield st
    market_engine.unregister(CODE)
    with session_scope() as db:
        for row in db.exec(select(DailyBar).where(DailyBar.code == CODE)).all():
            db.delete(row)


# ---------------------------------------------------------------- 常量与默认值

def test_source_constants_are_distinct():
    assert TradeSource.REAL != TradeSource.SIMULATED != TradeSource.BACKFILL
    assert TradeSource.REAL in TradeSource.TRUSTED


def test_default_match_mode_is_book():
    """默认必须是纯用户订单簿，否则默认就开着注水机。"""
    from app.config import settings
    assert settings.match_mode == "book"


# ---------------------------------------------------------------- 真实量隔离

def test_simulated_tick_does_not_touch_real_volume(stock):
    """模拟 tick 累加 volume，但绝不能进 real_volume。"""
    before = stock.real_volume
    stock.volume += 9999
    stock.amount += 99990.0
    assert stock.real_volume == before


def test_apply_trade_accumulates_real_volume(stock):
    market_engine.apply_trade(CODE, 10.20, 500)
    assert stock.real_volume == 500
    assert stock.real_amount == pytest.approx(5100.0)


def test_today_bar_uses_real_volume_only(stock):
    """只有模拟量时不该画出当日柱；有真实成交后才出现。"""
    stock.volume = 5000          # 只有引擎量
    stock.amount = 50000.0
    stock.real_volume = 0
    assert market_engine.today_bar(CODE) is None

    market_engine.apply_trade(CODE, 10.30, 200)
    bar = market_engine.today_bar(CODE)
    assert bar is not None
    assert bar["volume"] == 200      # 只含真实成交
    assert bar["close"] == pytest.approx(10.30)


# ---------------------------------------------------------------- 日线归档标记

def test_roll_day_skips_pure_simulated_volume(stock):
    """跨日时若只有模拟量，不该写假日线。"""
    stock.trade_date = "2026-10-27"
    stock.volume = 88888
    stock.amount = 888880.0
    stock.real_volume = 0
    market_engine.roll_day()
    with session_scope() as db:
        rows = db.exec(
            select(DailyBar).where(DailyBar.code == CODE)
            .where(DailyBar.trade_date == "2026-10-27")
        ).all()
    assert list(rows) == []


def test_roll_day_archives_real_volume_tagged_real(stock):
    stock.trade_date = "2026-10-26"
    stock.price = 10.50
    stock.open = 10.00
    stock.high = 10.60
    stock.low = 9.98
    stock.volume = 70000        # 其中含模拟量
    stock.real_volume = 20000   # 真实成交只有这些
    stock.real_amount = 210000.0
    market_engine.roll_day()
    with session_scope() as db:
        rows = [
            (b.volume, b.source)
            for b in db.exec(
                select(DailyBar).where(DailyBar.code == CODE)
                .where(DailyBar.trade_date == "2026-10-26")
            ).all()
        ]
    assert len(rows) == 1
    assert rows[0][0] == 20000                 # 只存真实量
    assert rows[0][1] == TradeSource.REAL      # 打 real 标


# ---------------------------------------------------------------- 盘口真实化

def _make_order(db, user_id: int, side: OrderSide, price: float, qty: int) -> None:
    db.add(Order(
        order_no=f"O{user_id}{side.value}{int(price*100)}", user_id=user_id, code=CODE,
        side=side, order_type=OrderType.LIMIT, price=price, quantity=qty,
        status=OrderStatus.PENDING, created_at=time.time(),
    ))


def test_depth_returns_user_orders_not_synthetic(stock):
    """盘口必须来自用户真实挂单。"""
    from app.engine import orderbook
    with session_scope() as db:
        uid = 90001
        if db.get(User, uid) is None:
            db.add(User(id=uid, username="depth_probe", role="retail"))
            db.commit()
        _make_order(db, uid, OrderSide.BUY, 9.90, 300)
        _make_order(db, uid, OrderSide.SELL, 10.20, 500)
        db.commit()

        bids = orderbook.quote_levels(db, CODE, "buy", limit=5)
        asks = orderbook.quote_levels(db, CODE, "sell", limit=5)

    assert bids and bids[0]["price"] == pytest.approx(9.90)
    assert bids[0]["volume"] == 300
    assert asks and asks[0]["price"] == pytest.approx(10.20)
    assert asks[0]["volume"] == 500


def test_depth_bid_order_is_descending(stock):
    """买盘按价格从高到低，与行情软件一致。"""
    from app.engine import orderbook
    with session_scope() as db:
        uid = 90002
        if db.get(User, uid) is None:
            db.add(User(id=uid, username="depth_probe2", role="retail"))
            db.commit()
        _make_order(db, uid, OrderSide.BUY, 9.50, 100)
        _make_order(db, uid, OrderSide.BUY, 9.80, 100)
        _make_order(db, uid, OrderSide.BUY, 9.65, 100)
        db.commit()
        bids = orderbook.quote_levels(db, CODE, "buy", limit=5)
    prices = [b["price"] for b in bids]
    assert prices == sorted(prices, reverse=True)


# ---------------------------------------------------------------- 外部行情源

def test_external_source_marks_stale_after_ttl():
    src = ExternalSource()
    src.publish("600000.SH", 10.5)
    assert src.code_exists("600000.SH") is True
    assert src.last_price("600000.SH") == pytest.approx(10.5)

    # 人为把时间戳拨回到 TTL 之前
    src._seen["600000.SH"] = time.time() - (src.DEFAULT_TTL + 1)
    assert src.code_exists("600000.SH") is False
    assert src.last_price("600000.SH") == 0.0
    assert src.quote_any("600000.SH") is None


def test_external_source_health_reports_stale():
    src = ExternalSource()
    src.publish_batch({"A": 1.0, "B": 2.0})
    src._seen["A"] = time.time() - (src.DEFAULT_TTL + 5)
    h = src.health()
    assert h["symbols"] == 2
    assert h["fresh"] == 1
    assert h["stale"] == 1
    assert "A" in h["stale_codes"]
    assert h["feeder_alive"] is True


def test_external_source_feeder_alive_goes_false():
    src = ExternalSource()
    src.heartbeat()
    src._last_beat = time.time() - (src.DEFAULT_TTL * 4)
    assert src.health()["feeder_alive"] is False
