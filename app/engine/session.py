"""交易时钟：交易模式（A股 / 美股 / 连续竞价）与时段判定。

为什么从「交易时段模式」改成「交易模式」
------------------------------------
旧版只有两个值：`always`（全天连续竞价）和 `real`（严格按 A 股时段）。
问题在于这组命名把「市场」和「时段规则」混在了一起 —— 运营想让模拟盘跑美股
时段时无值可选，只能去改环境变量重启。新版把语义收敛成三种**交易模式**：

* `a_share`（默认）：A 股。开盘集合竞价 9:15-9:25、连续竞价 9:30-11:30 /
  13:00-15:00、收盘集合竞价 14:57-15:00；
* `us`：美股。盘前 04:00-09:30、连续交易 09:30-16:00、盘后 16:00-20:00
  （美东时间，自动跟随夏令时）；
* `continuous`：连续竞价。24 小时不间断撮合，适合持续演示与压测。

时段（segment）与阶段（phase）
----------------------------
K 线要能区分「盘前 / 集合竞价 / 连续竞价 / 盘后」，因此每个时刻同时给出：

* `phase`   —— 细粒度阶段，给行情状态栏与撮合逻辑用；
* `segment` —— 粗粒度归属，作为 K 线每根柱子的时段标签；
* `is_open`  —— 是否允许连续撮合（价格推进）；
* `can_trade` —— 是否允许委托申报。

旧部署库里的 `always` / `real` 通过 `normalize_mode` 自动映射，不需要人工清库。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone

from ..config import settings

CN_TZ = timezone(timedelta(hours=8))

try:  # 有 tzdata 时用真实的美东时区，自动处理夏令时
    from zoneinfo import ZoneInfo

    US_TZ = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - 极简镜像里可能没有 tzdata
    US_TZ = timezone(timedelta(hours=-4))

#: 交易模式及其展示名（后台下拉框直接读它，避免前后端两处硬编码）
TRADE_MODES: dict[str, str] = {
    "a_share": "A股",
    "us": "美股",
    "continuous": "连续竞价",
}

#: 兼容旧值：always -> continuous，real -> a_share
MODE_ALIASES: dict[str, str] = {
    "always": "continuous",
    "real": "a_share",
    "cn": "a_share",
    "ashare": "a_share",
    "a-share": "a_share",
    "us_stock": "us",
}

DEFAULT_MODE = "a_share"


def normalize_mode(raw: str | None) -> str:
    """把任意历史值/大小写规范成受支持的交易模式，无法识别时回落 A 股。"""
    text = str(raw or "").strip()
    if text in TRADE_MODES:
        return text
    return MODE_ALIASES.get(text.lower(), DEFAULT_MODE)


@dataclass(frozen=True)
class SessionState:
    """某一时刻的交易时钟快照。"""

    mode: str         # a_share / us / continuous
    phase: str        # pre_open / open_auction / morning / lunch / afternoon / close_auction / closed ...
    segment: str      # pre / auction / continuous / lunch / post / closed（K 线标签）
    is_open: bool     # 是否允许连续撮合
    can_trade: bool   # 是否允许委托申报
    label: str
    trade_date: str
    seconds_left: int

    @property
    def is_auction(self) -> bool:
        return self.segment == "auction"

    @property
    def mode_label(self) -> str:
        return TRADE_MODES.get(self.mode, self.mode)


def _t(h: int, m: int) -> dtime:
    return dtime(hour=h, minute=m)


# ---- A 股时间点（北京时间） ----
A_OPEN_AUCTION_START = _t(9, 15)
A_OPEN_AUCTION_END = _t(9, 25)
A_MORNING_OPEN = _t(9, 30)
A_MORNING_CLOSE = _t(11, 30)
A_AFTERNOON_OPEN = _t(13, 0)
A_CLOSE_AUCTION_START = _t(14, 57)
A_AFTERNOON_CLOSE = _t(15, 0)

# ---- 美股时间点（美东时间） ----
US_PRE_OPEN = _t(4, 0)
US_OPEN = _t(9, 30)
US_CLOSE = _t(16, 0)
US_POST_CLOSE = _t(20, 0)


def _now(tz) -> datetime:
    return datetime.now(tz)


def _seconds_until(target: dtime, now: datetime) -> int:
    dt = datetime.combine(now.date(), target, tzinfo=now.tzinfo)
    return max(0, int((dt - now).total_seconds()))


def _a_share_session(now: datetime) -> SessionState:
    trade_date = now.strftime("%Y-%m-%d")
    t = now.time()

    if t < A_OPEN_AUCTION_START:
        return SessionState("a_share", "pre_open", "pre", False, False, "未开盘", trade_date,
                            _seconds_until(A_OPEN_AUCTION_START, now))
    if t < A_OPEN_AUCTION_END:
        return SessionState("a_share", "open_auction", "auction", False, True, "开盘集合竞价", trade_date,
                            _seconds_until(A_OPEN_AUCTION_END, now))
    if t < A_MORNING_OPEN:
        return SessionState("a_share", "pre_continuous", "pre", False, True, "待开盘", trade_date,
                            _seconds_until(A_MORNING_OPEN, now))
    if t < A_MORNING_CLOSE:
        return SessionState("a_share", "morning", "continuous", True, True, "上午连续竞价", trade_date,
                            _seconds_until(A_MORNING_CLOSE, now))
    if t < A_AFTERNOON_OPEN:
        return SessionState("a_share", "lunch", "lunch", False, False, "午间休市", trade_date,
                            _seconds_until(A_AFTERNOON_OPEN, now))
    if t < A_CLOSE_AUCTION_START:
        return SessionState("a_share", "afternoon", "continuous", True, True, "下午连续竞价", trade_date,
                            _seconds_until(A_CLOSE_AUCTION_START, now))
    if t < A_AFTERNOON_CLOSE:
        return SessionState("a_share", "close_auction", "auction", False, True, "收盘集合竞价", trade_date,
                            _seconds_until(A_AFTERNOON_CLOSE, now))
    return SessionState("a_share", "closed", "post", False, False, "已收盘", trade_date, 0)


def _us_session(now: datetime) -> SessionState:
    trade_date = now.strftime("%Y-%m-%d")
    t = now.time()

    if t < US_PRE_OPEN:
        return SessionState("us", "closed", "closed", False, False, "已收盘", trade_date,
                            _seconds_until(US_PRE_OPEN, now))
    if t < US_OPEN:
        # 美股盘前：允许申报与撮合，但用盘前标识，客户端可单独着色
        return SessionState("us", "pre_market", "pre", True, True, "美股盘前", trade_date,
                            _seconds_until(US_OPEN, now))
    if t < US_CLOSE:
        return SessionState("us", "regular", "continuous", True, True, "美股连续交易", trade_date,
                            _seconds_until(US_CLOSE, now))
    if t < US_POST_CLOSE:
        return SessionState("us", "post_market", "post", True, True, "美股盘后", trade_date,
                            _seconds_until(US_POST_CLOSE, now))
    return SessionState("us", "closed", "closed", False, False, "已收盘", trade_date, 0)


def current_session(ts: float | None = None) -> SessionState:
    """返回当前（或指定时间戳）的交易时钟状态。"""
    mode = normalize_mode(getattr(settings, "trade_mode", None))

    if mode == "continuous":
        # 连续竞价：24 小时不间断，不区分工作日
        tz_now = datetime.fromtimestamp(ts, CN_TZ) if ts is not None else _now(CN_TZ)
        return SessionState("continuous", "continuous", "continuous", True, True, "连续竞价",
                            tz_now.strftime("%Y-%m-%d"), 0)

    if mode == "us":
        now = datetime.fromtimestamp(ts, US_TZ) if ts is not None else _now(US_TZ)
        if now.weekday() >= 5:  # 周末休市（节假日按工作日近似）
            return SessionState("us", "closed", "closed", False, False, "周末休市",
                                now.strftime("%Y-%m-%d"), 0)
        return _us_session(now)

    now = datetime.fromtimestamp(ts, CN_TZ) if ts is not None else _now(CN_TZ)
    if now.weekday() >= 5:
        return SessionState("a_share", "closed", "closed", False, False, "周末休市",
                            now.strftime("%Y-%m-%d"), 0)
    return _a_share_session(now)


def is_trading_time(ts: float | None = None) -> bool:
    """是否允许下单（集合竞价也允许申报）。"""
    return current_session(ts).can_trade


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


def cn_now() -> datetime:
    return datetime.now(CN_TZ)


def now_epoch() -> float:
    return time.time()
