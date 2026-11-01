"""当日实时日线测试。

``daily_bar`` 只在跨日时落库，所以「今天」这根日线在收盘前不存在。
这些测试钉住：当天成交后 K 线要立刻能看到今天这根，且没成交时不许造柱。
"""
from __future__ import annotations

import os
import tempfile

os.environ.setdefault("STLAND_DB_URL", f"sqlite:///{tempfile.mkdtemp()}/test.db")
os.environ.setdefault("STLAND_SIGNATURE_REQUIRED", "false")
os.environ.setdefault("STLAND_TICK_SECONDS", "0.2")

import pytest  # noqa: E402

from app.engine.market import market_engine  # noqa: E402

CODE = "TST002.SH"


@pytest.fixture()
def stock():
    market_engine.register(
        code=CODE, name="当日线测试", init_price=10.0, tick_size=0.01,
        limit_up_pct=0.10, beta=1.0, drift=0.0, vol=0.3,
        liquidity=1.0, float_shares=1e8,
    )
    st = market_engine._states[CODE]
    st.volume = 0
    yield st
    market_engine.unregister(CODE)


def test_no_bar_before_any_trade(stock):
    """开盘前不该先画一根 OHLC 全等于昨收的柱子。"""
    assert stock.volume == 0
    assert market_engine.today_bar(CODE) is None


def test_bar_appears_after_trade(stock):
    """成交之后当日柱立刻存在，且价格来自成交。"""
    market_engine.apply_trade(CODE, 10.35, 500)
    bar = market_engine.today_bar(CODE)
    assert bar is not None
    assert bar["close"] == pytest.approx(10.35)
    assert bar["high"] >= 10.35
    assert bar["volume"] == 500


def test_bar_tracks_further_trades(stock):
    """同一交易日内继续成交，柱子跟着更新而不是新开一根。"""
    market_engine.apply_trade(CODE, 10.20, 100)
    first_date = market_engine.today_bar(CODE)["time"]
    market_engine.apply_trade(CODE, 10.60, 200)
    bar = market_engine.today_bar(CODE)
    assert bar["time"] == first_date
    assert bar["close"] == pytest.approx(10.60)
    assert bar["volume"] == 300


def test_unknown_code_returns_none():
    assert market_engine.today_bar("NOPE.SH") is None
