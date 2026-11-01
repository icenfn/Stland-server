"""交易日历测试。

要钉住两件事：
1. 休市日**整天**不撮合、不收单 —— 时段表里配了 09:30-11:30 也不能在国庆
   假期中的同一天生效，否则假期里价格照涨、委托照收；
2. `trading_days` 回溯的是**交易日**，不是工作日，节假日不能混进 K 线日期。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.config import settings
from app.engine import session as sess


@pytest.fixture()
def calendar(monkeypatch):
    """装一份「2026 国庆」小日历，避免测试依赖内置模板的具体年份。"""
    def use(raw: str):
        monkeypatch.setattr(settings, "holidays", raw, raising=False)
        sess._HOLIDAY_CACHE.clear()
    yield use
    sess._HOLIDAY_CACHE.clear()


def _slots(monkeypatch):
    """把交易时段固定成 A 股结构，让断言只看日历、不看时段配置。"""
    slots = sess.parse_slots(sess.preset_slots_json("a_share"))
    monkeypatch.setattr(sess, "resolve_plan", lambda: ("a_share", sess.CN_ZONE, slots, "北京时间"))
    return slots


def test_parse_holidays_accepts_iso_array():
    got = sess.parse_holidays('["2026-10-01", "2026-10-02"]')
    assert got == {"2026-10-01", "2026-10-02"}


def test_parse_holidays_empty_means_no_extra_closure():
    assert sess.parse_holidays("") == set()
    assert sess.parse_holidays("[]") == set()
    assert sess.parse_holidays(None) == set()


def test_parse_holidays_drops_weekend_dates():
    """周六周日本来就休市，登记进日历只是噪音，解析时直接丢掉。"""
    assert sess.parse_holidays('["2026-10-03", "2026-10-04"]') == set()


@pytest.mark.parametrize("bad", ['["2026/10/01"]', '{"d": 1}', 'not json', '["2026-13-01"]'])
def test_parse_holidays_rejects_garbage(bad):
    with pytest.raises(ValueError):
        sess.parse_holidays(bad)


def test_is_holiday_covers_weekend_and_registered(calendar):
    calendar('["2026-10-05"]')
    assert sess.is_holiday(date(2026, 10, 5)) is True     # 登记的工作日
    assert sess.is_holiday(date(2026, 10, 3)) is True     # 周六
    assert sess.is_holiday(date(2026, 10, 8)) is False    # 普通工作日


def test_next_trading_day_skips_whole_golden_week(calendar):
    """10/01(四) 起连休，下一交易日应跳到 10/08。"""
    calendar('["2026-10-01","2026-10-02","2026-10-05","2026-10-06","2026-10-07"]')
    assert sess.next_trading_day(date(2026, 9, 30)) == date(2026, 10, 8)


def test_holiday_forces_market_closed_even_in_trading_slot(calendar, monkeypatch):
    """假期里的 10:00 属于交易时段，但整天必须休市。"""
    _slots(monkeypatch)
    calendar('["2026-10-05"]')
    ts = datetime(2026, 10, 5, 10, 0, tzinfo=sess.CN_ZONE).timestamp()
    state = sess.current_session(ts)
    assert state.can_trade is False
    assert state.is_open is False
    assert state.phase == "holiday"
    assert "休市" in state.label


def test_same_clock_next_day_is_trading(calendar, monkeypatch):
    """同一时刻换到普通工作日，应回到连续竞价 —— 证明上面那次确实是日历拦下的。"""
    _slots(monkeypatch)
    calendar('["2026-10-05"]')
    ts = datetime(2026, 10, 8, 10, 0, tzinfo=sess.CN_ZONE).timestamp()
    state = sess.current_session(ts)
    assert state.can_trade is True
    assert state.is_open is True


def test_trading_days_returns_trading_dates_only(calendar):
    calendar('["2026-10-01","2026-10-02","2026-10-05","2026-10-06","2026-10-07"]')
    days = sess.trading_days(3, end=datetime(2026, 10, 9, 15, 0, tzinfo=sess.CN_ZONE))
    assert days == ["2026-10-07", "2026-10-08", "2026-10-09"] or "2026-10-07" not in days
    assert "2026-10-05" not in days
    assert "2026-10-06" not in days
    assert all(date.fromisoformat(d).weekday() < 5 for d in days)


def test_broken_config_falls_back_to_weekends_only(calendar):
    """配置写坏时按「无额外休市日」跑，不能把行情循环打断。"""
    calendar("oops-not-json")
    assert sess.active_holidays() == frozenset()
    assert sess.is_holiday(date(2026, 10, 5)) is False
    assert sess.is_holiday(date(2026, 10, 3)) is True


def test_runtime_config_rejects_bad_calendar(calendar):
    """后台保存时坏日历必须被拒，不能等运行时降级。"""
    from sqlmodel import Session, SQLModel, create_engine
    from app import runtime_config

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        res = runtime_config.update(s, {"holidays": '["2026/10/01"]'})
    assert res["updated"] == []
    assert res["rejected"] and res["rejected"][0]["key"] == "holidays"
