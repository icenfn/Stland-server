"""交易时段时钟。

股票市场的行为高度依赖交易时段结构：开盘集合竞价、连续竞价、午间休市、收盘集合竞价。
本项目为「自包含模拟交易所」，默认 `session_mode="always"` 表示模拟撮合全天连续进行，
便于任何时间部署都能演示；`session_mode="real"` 则严格按 A 股时段运行，
并启用开盘集合竞价（9:15-9:25，按最大成交量原则定开盘价）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone

from ..config import settings

CN_TZ = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class SessionState:
    phase: str        # pre_open / call_auction / morning / lunch / afternoon / closed
    is_open: bool     # 是否允许连续撮合
    can_trade: bool   # 是否允许委托申报
    label: str
    trade_date: str
    seconds_left: int


def cn_now() -> datetime:
    return datetime.now(CN_TZ)


def _t(h: int, m: int) -> dtime:
    return dtime(hour=h, minute=m)


OPEN_AUCTION_START = _t(9, 15)
OPEN_AUCTION_END = _t(9, 25)
MORNING_OPEN = _t(9, 30)
MORNING_CLOSE = _t(11, 30)
AFTERNOON_OPEN = _t(13, 0)
AFTERNOON_CLOSE = _t(15, 0)


def _seconds_until(target: dtime, now: datetime) -> int:
    dt = datetime.combine(now.date(), target, tzinfo=CN_TZ)
    return max(0, int((dt - now).total_seconds()))


def current_session(ts: float | None = None) -> SessionState:
    now = cn_now() if ts is None else datetime.fromtimestamp(ts, CN_TZ)
    trade_date = now.strftime("%Y-%m-%d")
    weekday_ok = now.weekday() < 5
    t = now.time()

    if settings.session_mode == "always" or not weekday_ok:
        # 模拟交易所全天连续竞价（文档中明示为自包含模拟市场）
        return SessionState("continuous", True, True, "连续竞价", trade_date, 0)

    if t < OPEN_AUCTION_START:
        return SessionState("pre_open", False, False, "未开盘", trade_date, _seconds_until(OPEN_AUCTION_START, now))
    if t < OPEN_AUCTION_END:
        return SessionState("call_auction", False, True, "开盘集合竞价", trade_date, _seconds_until(OPEN_AUCTION_END, now))
    if t < MORNING_OPEN:
        return SessionState("pre_open", False, True, "待开盘", trade_date, _seconds_until(MORNING_OPEN, now))
    if t < MORNING_CLOSE:
        return SessionState("morning", True, True, "上午连续竞价", trade_date, _seconds_until(MORNING_CLOSE, now))
    if t < AFTERNOON_OPEN:
        return SessionState("lunch", False, False, "午间休市", trade_date, _seconds_until(AFTERNOON_OPEN, now))
    if t < AFTERNOON_CLOSE:
        return SessionState("afternoon", True, True, "下午连续竞价", trade_date, _seconds_until(AFTERNOON_CLOSE, now))
    return SessionState("closed", False, False, "已收盘", trade_date, 0)


def is_trading_time(ts: float | None = None) -> bool:
    st = current_session(ts)
    return st.can_trade


def trading_days(count: int, end: datetime | None = None) -> list[str]:
    """回溯最近 count 个工作日（不含周末；节假日按工作日近似）。"""
    end = end or cn_now()
    days: list[str] = []
    cursor = end.date()
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor.strftime("%Y-%m-%d"))
        cursor -= timedelta(days=1)
    return list(reversed(days))


def now_epoch() -> float:
    return time.time()
