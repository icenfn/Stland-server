"""行情状态持久化测试。

钉住三件事：
1. **重启不丢价格**：进程重启后从快照恢复当日价格与成交量，
   否则昨天在 10.50 成交的记录还在、行情却显示 10.00，账实对不上；
2. **跨日先归档再归零**：上一交易日要收成一根日线，今日以昨收重启；
3. **落库幂等**：同一标的反复写快照只留一行，不随时间膨胀。
"""
from __future__ import annotations

import os
import tempfile

os.environ.setdefault("STLAND_DB_URL", f"sqlite:///{tempfile.mkdtemp()}/test.db")
os.environ.setdefault("STLAND_SIGNATURE_REQUIRED", "false")
os.environ.setdefault("STLAND_TICK_SECONDS", "0.2")

import pytest  # noqa: E402
from sqlmodel import select  # noqa: E402

from app.db import init_db, session_scope  # noqa: E402
from app.engine.market import market_engine  # noqa: E402
from app.models import DailyBar, QuoteSnapshot  # noqa: E402

CODE = "TST001.SH"


@pytest.fixture(scope="module", autouse=True)
def _db():
    init_db()


@pytest.fixture()
def stock():
    """注册一支测试标的，结束时清掉内存状态与库内痕迹。"""
    market_engine.register(
        code=CODE,
        name="测试标的",
        init_price=10.0,
        tick_size=0.01,
        limit_up_pct=0.10,
        beta=1.0,
        drift=0.0,
        vol=0.3,
        liquidity=1.0,
        float_shares=1e8,
    )
    with session_scope() as db:
        for row in db.exec(select(QuoteSnapshot).where(QuoteSnapshot.code == CODE)).all():
            db.delete(row)
        for row in db.exec(select(DailyBar).where(DailyBar.code == CODE)).all():
            db.delete(row)
    yield market_engine._states[CODE]
    market_engine.unregister(CODE)
    with session_scope() as db:
        for row in db.exec(select(QuoteSnapshot).where(QuoteSnapshot.code == CODE)).all():
            db.delete(row)
        for row in db.exec(select(DailyBar).where(DailyBar.code == CODE)).all():
            db.delete(row)


def _drop_memory():
    """模拟进程重启：摘掉内存状态，等 hydrate 从库里捞回来。"""
    market_engine.unregister(CODE)


def _reregister():
    market_engine.register(
        code=CODE, name="测试标的", init_price=10.0, tick_size=0.01,
        limit_up_pct=0.10, beta=1.0, drift=0.0, vol=0.3,
        liquidity=1.0, float_shares=1e8,
    )


def test_persist_then_hydrate_keeps_same_day_price(stock):
    """当天重启：价格、成交量、最高最低都该原样回来。"""
    stock.price = 10.58
    stock.open = 10.10
    stock.high = 10.66
    stock.low = 10.05
    stock.volume = 12300
    stock.amount = 129834.0
    assert market_engine.persist_quotes(force=True) >= 1

    _drop_memory()
    _reregister()
    restored = market_engine.hydrate()
    assert restored >= 1

    st = market_engine._states[CODE]
    assert st.price == pytest.approx(10.58)
    assert st.open == pytest.approx(10.10)
    assert st.high == pytest.approx(10.66)
    assert st.low == pytest.approx(10.05)
    assert st.volume == 12300


def test_hydrate_recomputes_limits_from_backfilled_prev_close(stock):
    """恢复后涨跌停必须按昨收重算，不能停在注册时的初值上。"""
    stock.prev_close = 20.0
    stock.price = 21.0
    market_engine.persist_quotes(force=True)

    _drop_memory()
    _reregister()
    market_engine.hydrate()
    st = market_engine._states[CODE]
    assert st.limit_up == pytest.approx(22.0)
    assert st.limit_down == pytest.approx(18.0)


def test_roll_day_archives_yesterday_and_resets(stock):
    """跨日：上一日收成日线，今日以该收盘价为昨收、成交归零。"""
    stock.trade_date = "2026-10-30"
    stock.prev_close = 10.0
    stock.price = 10.80
    stock.open = 10.05
    stock.high = 10.90
    stock.low = 10.02
    stock.volume = 50000
    stock.amount = 530000.0

    market_engine.roll_day()

    st = market_engine._states[CODE]
    assert st.trade_date != "2026-10-30"
    assert st.prev_close == pytest.approx(10.80)
    assert st.price == pytest.approx(10.80)   # 今日以昨收开盘
    assert st.volume == 0 and st.amount == 0.0
    # 涨跌停跟着新昨收走
    assert st.limit_up == pytest.approx(11.88)
    assert st.limit_down == pytest.approx(9.72)

    with session_scope() as db:
        bars = [
            (b.close, b.volume)
            for b in db.exec(
                select(DailyBar).where(DailyBar.code == CODE)
                .where(DailyBar.trade_date == "2026-10-30")
            ).all()
        ]
    assert len(bars) == 1
    assert bars[0][0] == pytest.approx(10.80)
    assert bars[0][1] == 50000


def test_roll_day_skips_days_without_trades(stock):
    """没成交过的一天不该写出一根水平假 K 线。"""
    stock.trade_date = "2026-10-29"
    stock.volume = 0
    market_engine.roll_day()
    with session_scope() as db:
        bars = db.exec(
            select(DailyBar).where(DailyBar.code == CODE)
            .where(DailyBar.trade_date == "2026-10-29")
        ).all()
    assert list(bars) == []


def test_roll_day_is_idempotent_within_same_day(stock):
    """同一天重复调用只归档一次，不会重复写库。"""
    stock.trade_date = "2026-10-28"
    stock.volume = 1000
    stock.price = 10.30
    market_engine.roll_day()
    market_engine.roll_day()
    with session_scope() as db:
        bars = db.exec(
            select(DailyBar).where(DailyBar.code == CODE)
            .where(DailyBar.trade_date == "2026-10-28")
        ).all()
    assert len(list(bars)) == 1


def test_snapshot_stays_one_row_per_code(stock):
    """反复落库只留一行 —— 快照表不会随时间膨胀。"""
    market_engine.persist_quotes(force=True)
    stock.price = 10.99
    market_engine.persist_quotes(force=True)
    with session_scope() as db:
        rows = [(r.code, r.price) for r in db.exec(select(QuoteSnapshot).where(QuoteSnapshot.code == CODE)).all()]
    assert len(rows) == 1
    assert rows[0][1] == pytest.approx(10.99)


def test_persist_is_throttled_unless_forced(stock):
    """不强制时必须节流，否则行情循环每秒写库会把 SQLite 压满。"""
    market_engine.persist_quotes(force=True)
    assert market_engine.persist_quotes() == 0
    assert market_engine.persist_quotes(force=True) >= 1
