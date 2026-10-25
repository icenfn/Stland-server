"""撮合与账务单元测试：无需网络，直接调用服务层。

运行：pytest -q
"""
from __future__ import annotations

import os
import tempfile

os.environ.setdefault("STLAND_DB_URL", f"sqlite:///{tempfile.mkdtemp()}/test.db")
os.environ.setdefault("STLAND_SIGNATURE_REQUIRED", "false")
os.environ.setdefault("STLAND_TICK_SECONDS", "0.2")

from app.engine.book import Level, snap, walk_book, vwap  # noqa: E402
from app.engine.market import market_engine  # noqa: E402
from app.services.fees import calc_fee, max_buy_quantity  # noqa: E402


# --------------------------------------------------------------------------- #
# 撮合核心
# --------------------------------------------------------------------------- #
def test_snap_aligns_to_tick():
    assert snap(10.004, 0.01) == 10.0
    assert snap(10.006, 0.01) == 10.01
    assert snap(123.456, 0.01) == 123.46


def test_walk_book_price_time_priority():
    asks = [Level(10.01, 100), Level(10.02, 200), Level(10.03, 300)]
    fills = walk_book(asks, 250, None, "buy")
    assert [ (f.price, f.quantity) for f in fills ] == [(10.01, 100), (10.02, 150)]
    assert vwap(fills) == round((10.01 * 100 + 10.02 * 150) / 250, 4)


def test_walk_book_respects_limit_price():
    asks = [Level(10.01, 100), Level(10.02, 200)]
    fills = walk_book(asks, 300, 10.01, "buy")
    assert sum(f.quantity for f in fills) == 100


def test_walk_book_sell_side():
    bids = [Level(9.99, 100), Level(9.98, 100)]
    fills = walk_book(bids, 150, 9.99, "sell")
    assert [(f.price, f.quantity) for f in fills] == [(9.99, 100)]
    fills2 = walk_book(bids, 150, None, "sell")
    assert sum(f.quantity for f in fills2) == 150


# --------------------------------------------------------------------------- #
# 费用
# --------------------------------------------------------------------------- #
def test_fee_buy_has_no_stamp_tax():
    fee = calc_fee(100_000, "buy")
    assert fee.stamp_tax == 0.0
    assert fee.commission == 25.0          # 万 2.5
    assert fee.transfer_fee == 1.0          # 万 0.1


def test_fee_sell_includes_stamp_tax():
    fee = calc_fee(100_000, "sell")
    assert fee.stamp_tax == 50.0            # 万 5


def test_minimum_commission():
    fee = calc_fee(1_000, "buy")
    assert fee.commission == 5.0


def test_max_buy_quantity_respects_fees():
    qty = max_buy_quantity(10_000, 10.0, 100)
    assert qty % 100 == 0
    amount = qty * 10.0
    assert amount + calc_fee(amount, "buy").total <= 10_000


# --------------------------------------------------------------------------- #
# 行情引擎
# --------------------------------------------------------------------------- #
def test_engine_tick_moves_price_within_limits():
    market_engine.register(
        code="600000",
        name="测试股",
        init_price=10.0,
        tick_size=0.01,
        limit_up_pct=0.10,
        beta=1.0,
        drift=0.1,
        vol=0.4,
        liquidity=1.0,
        float_shares=1e9,
    )
    for _ in range(50):
        market_engine.tick()
    q = market_engine.quote("600000")
    assert q is not None
    assert 9.0 - 1e-9 <= q["price"] <= 11.0 + 1e-9
    assert q["high"] >= q["price"] >= q["low"]


def test_engine_quote_aligned_to_tick():
    market_engine.register(
        code="600001",
        name="测试股2",
        init_price=25.0,
        tick_size=0.01,
        limit_up_pct=0.20,
        beta=1.0,
        drift=0.0,
        vol=0.3,
        liquidity=1.0,
        float_shares=5e8,
    )
    for _ in range(30):
        market_engine.tick()
    price = market_engine.quote("600001")["price"]
    assert abs(round(price / 0.01) * 0.01 - price) < 1e-6


def test_engine_depth_five_levels():
    depth = market_engine.depth("600000")
    assert depth is not None
    assert len(depth["bids"]) == 5 and len(depth["asks"]) == 5
    assert depth["bids"][0]["price"] < depth["asks"][0]["price"]
