"""交易时钟：结算模式（T+1 / T+0 / 调试）与可配置交易时段。

为什么把「结算模式」和「交易时段」拆成两件事
----------------------------------------
旧版只有一个 `trade_mode`（A股 / 美股 / 连续竞价），把三件互不相干的东西
压在同一个字段里：

1. **结算规则**（当日买入能不能当日卖）—— 这是账户与撮合的语义；
2. **时段表**（几点到几点能申报、几点到几点价格会动）—— 这是市场惯例；
3. **调试开关**（不看时段、全天可交易）—— 这是运营与联调用的后门。

三者混在一起后，运营想让模拟盘跑「T+0 + A股时段」就没有取值可选，
只能改代码；想加一段「盘后固定价格交易」也要动 if/else 链。

新版把它们正交拆开：

* **结算模式** ``trade_mode``：``t1`` / ``t0`` / ``debug``。只决定「当日买入的
  持仓何时变成可卖」，与时段无关；
* **交易时段** ``session_slots``（一张 JSON 时段表，唯一事实来源）。
  后台的上方按钮「A股」「美股」只是把内置模板填进这张表，保存的仍是 JSON。
  每段显式声明两个能力：

  * ``match`` —— 是否允许连续撮合（价格会不会推进）；
  * ``trade`` —— 是否接受委托申报。

  集合竞价、盘后固定价格交易这类「能报单但价格不动」的时段，就是
  ``match=False, trade=True``；午间休市是两者都为 False。

``debug`` 模式下时段设置整体失效：全天 ``match=True, trade=True``，按 T+0 结算。
这样联调时不必等开盘，也不会因为「配置了 A 股时段」而在夜里报 MARKET_CLOSED。

时段与阶段
----------
每个时刻同时给出三种粒度，供不同层使用：

* ``phase``   —— 细粒度阶段（``open_auction`` / ``continuous`` / ``close_auction`` …），
  给撮合与状态栏判断用；
* ``segment`` —— 粗粒度归属（``pre`` / ``auction`` / ``continuous`` / ``lunch`` /
  ``post`` / ``fixed`` / ``closed``），作为 K 线每根柱子的时段标签；
* ``is_open`` / ``can_trade`` —— 两个能力位，直接决定引擎与交易服务的行为。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone

from ..config import settings

CN_TZ = timezone(timedelta(hours=8))

try:  # 有 tzdata 时用真实的美东时区，自动处理夏令时
    from zoneinfo import ZoneInfo

    US_TZ = ZoneInfo("America/New_York")
    _HAS_TZDATA = True
except Exception:  # pragma: no cover - 极简镜像里可能没有 tzdata
    US_TZ = timezone(timedelta(hours=-4))
    _HAS_TZDATA = False

CN_ZONE = ZoneInfo("Asia/Shanghai") if _HAS_TZDATA else CN_TZ


# --------------------------------------------------------------------------- #
# 结算模式
# --------------------------------------------------------------------------- #
#: 结算模式及其展示名（后台下拉框直接读它，避免前后端两处硬编码）
TRADE_MODES: dict[str, str] = {
    "t1": "T+1",
    "t0": "T+0",
    "debug": "调试模式",
}

#: 兼容旧值：旧版 a_share/us 是「A股/美股」，本质上都是 T+1；continuous 是
#: 全天连续竞价，等价于 T+0。映射后不需要人工清库。
MODE_ALIASES: dict[str, str] = {
    "a_share": "t1",
    "ashare": "t1",
    "a-share": "t1",
    "cn": "t1",
    "real": "t1",
    "us": "t1",
    "t1_": "t1",
    "us_stock": "t1",
    "continuous": "t0",
    "always": "t0",
    "t+0": "t0",
    "t+1": "t1",
    "t 0": "t0",
    "debug": "debug",
    "test": "debug",
}

DEFAULT_MODE = "t1"

#: 结算模式 → 当日买入的持仓是否立即可卖
SETTLE_BY_MODE: dict[str, str] = {"t1": "t1", "t0": "t0", "debug": "t0"}

SETTLE_LABELS: dict[str, str] = {"t1": "T+1", "t0": "T+0"}


def normalize_mode(raw: str | None) -> str:
    """把任意历史值/大小写规范成受支持的结算模式，无法识别时回落 T+1。"""
    text = str(raw or "").strip()
    if text in TRADE_MODES:
        return text
    return MODE_ALIASES.get(text.lower(), DEFAULT_MODE)


def settle_of(mode: str) -> str:
    return SETTLE_BY_MODE.get(normalize_mode(mode), "t1")


# --------------------------------------------------------------------------- #
# 时段表
# --------------------------------------------------------------------------- #
#: 粗粒度时段 → 展示名（K 线图例、客户端着色说明、后台表格都用它）
SEGMENT_LABELS: dict[str, str] = {
    "pre": "盘前",
    "auction": "集合竞价",
    "continuous": "连续竞价",
    "lunch": "午间休市",
    "post": "盘后",
    "fixed": "盘后固定价格",
    "closed": "休市",
}

#: 时段表允许出现的 segment（写错时直接拒绝，避免写进 K 线标签里）
ALLOWED_SEGMENTS = tuple(SEGMENT_LABELS.keys())

#: 没写 phase 时按 segment 兜底，保证状态栏永远有值
SEGMENT_DEFAULT_PHASE: dict[str, str] = {
    "pre": "pre_market",
    "auction": "auction",
    "continuous": "continuous",
    "lunch": "lunch",
    "post": "post_market",
    "fixed": "fixed_price",
    "closed": "closed",
}


@dataclass(frozen=True)
class Slot:
    """一段交易时段。

    ``start`` / ``end`` 为当地时区的 ``HH:MM``，左闭右开；
    ``match`` 控制价格是否推进（连续撮合），``trade`` 控制是否受理申报。
    """

    start: str
    end: str
    label: str
    segment: str
    match: bool
    trade: bool
    phase: str = ""

    def to_dict(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "label": self.label,
            "segment": self.segment,
            "segment_label": SEGMENT_LABELS.get(self.segment, self.segment),
            "match": self.match,
            "trade": self.trade,
            "phase": self.phase or SEGMENT_DEFAULT_PHASE.get(self.segment, "continuous"),
        }


#: 内置预设。时段表是纯数据，改预设不必动任何 if/else。
PRESETS: dict[str, dict] = {
    "a_share": {
        "label": "A股",
        "timezone": "Asia/Shanghai",
        "tz_label": "北京时间",
        "slots": [
            # 开盘集合竞价：只报不受（9:15-9:25），9:25 一次性撮合出开盘价
            Slot("09:15", "09:25", "开盘集合竞价", "auction", False, True, "open_auction"),
            # 9:25-9:30 只接受申报，不再撮合
            Slot("09:25", "09:30", "待开盘", "auction", False, True, "pre_continuous"),
            Slot("09:30", "11:30", "上午连续竞价", "continuous", True, True, "morning"),
            Slot("11:30", "13:00", "午间休市", "lunch", False, False, "lunch"),
            Slot("13:00", "14:57", "下午连续竞价", "continuous", True, True, "afternoon"),
            Slot("14:57", "15:00", "收盘集合竞价", "auction", False, True, "close_auction"),
            # 盘后固定价格交易：以收盘价成交，价格不动但可以报单成交
            Slot("15:05", "15:30", "盘后固定价格交易", "fixed", False, True, "fixed_price"),
        ],
    },
    "us": {
        "label": "美股",
        "timezone": "America/New_York",
        "tz_label": "美东时间",
        "slots": [
            Slot("04:00", "09:25", "盘前交易", "pre", True, True, "pre_market"),
            Slot("09:25", "09:30", "盘前集合竞价", "auction", False, True, "open_auction"),
            Slot("09:30", "15:50", "连续交易", "continuous", True, True, "regular"),
            Slot("15:50", "16:00", "收盘集合竞价", "auction", False, True, "close_auction"),
            Slot("16:00", "20:00", "盘后交易", "post", True, True, "post_market"),
        ],
    },
}

#: 可一键填入的预设（后台「交易时段」上方的按钮就按这个顺序渲染）。
#: 注意：预设不再是「运行模式」，只是给运营省去手写 JSON 的模板。
FILLABLE_PRESETS: tuple[str, ...] = ("a_share", "us")

#: 休市日历模板（后台「休市日期」上方的一键填入按钮）。
#: 只列**非周末**的法定休市日：周六/周日本来就休市，重复登记会让列表
#: 里塞满日期，运营看不出哪些才是真正需要关注的节假日。
#: 数据来源：上交所《关于上海证券交易所 2026 年部分节假日休市安排的通知》
#: （上证公告〔2025〕45 号）—— 元旦 1/1-1/3、春节 2/15-2/23、清明 4/4-4/6、
#: 劳动节 5/1-5/5、端午 6/19-6/21、中秋 9/25-9/27、国庆 10/1-10/7，
#: 其中落在周末的日期已剔除。
HOLIDAY_PRESETS: dict[str, dict] = {
    "a_share_2026": {
        "label": "A股 2026",
        "dates": [
            "2026-01-01", "2026-01-02",                                # 元旦
            "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
            "2026-02-20", "2026-02-23",                                # 春节
            "2026-04-06",                                              # 清明
            "2026-05-01", "2026-05-04", "2026-05-05",                  # 劳动节
            "2026-06-19",                                              # 端午
            "2026-09-25",                                              # 中秋
            "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06",
            "2026-10-07",                                              # 国庆
        ],
    },
}


def holiday_preset_json(key: str) -> str:
    """把内置休市日历导出成 JSON 文本，供后台按钮一键填入。"""
    conf = HOLIDAY_PRESETS.get(key) or {}
    return json.dumps(list(conf.get("dates") or []), ensure_ascii=False, indent=2)


def parse_holidays(raw: str | None) -> set[str]:
    """把 ``holidays`` 的 JSON 文本解析成休市日期集合。

    格式（日期字符串数组，``YYYY-MM-DD``）：``["2026-01-01", "2026-02-16"]``。
    空串 / 空数组返回空集合。解析失败抛 ValueError，由配置层转成「拒绝保存」——
    日历写错一天，整天都不能报单，比拒绝保存更难排查。
    落在周末的日期会被丢掉：周末本来就休市，留着只是噪音。
    """
    text = str(raw or "").strip()
    if not text:
        return set()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"休市日期不是合法 JSON：{exc.msg}") from exc
    if not isinstance(data, list):
        raise ValueError('休市日期应为 JSON 数组，例如 ["2026-01-01"]')

    dates: set[str] = set()
    for item in data:
        try:
            day = date.fromisoformat(str(item).strip())
        except ValueError as exc:
            raise ValueError(f"日期格式应为 YYYY-MM-DD，收到 {item!r}") from exc
        if day.weekday() >= 5:
            continue
        dates.add(day.isoformat())
    return dates


# 每个 tick 都会问一次「今天休不休市」，而 parse_holidays 要跑 json.loads。
# 用最近一次的原文做缓存，配置一改原文就变，缓存自然失效。
_HOLIDAY_CACHE: dict[str, frozenset[str]] = {}


def active_holidays() -> frozenset[str]:
    """当前生效的休市日期集合；配置写坏时按「没有额外休市日」处理。

    这里不抛异常：时钟查询被引擎每个 tick 调用，为了一个坏配置把整个行情
    循环打断，比按周末规则正常跑更糟。坏数据在保存时就已被拒。
    """
    raw = str(getattr(settings, "holidays", "") or "")
    hit = _HOLIDAY_CACHE.get(raw)
    if hit is not None:
        return hit
    try:
        parsed = frozenset(parse_holidays(raw))
    except ValueError:
        parsed = frozenset()
    _HOLIDAY_CACHE.clear()
    _HOLIDAY_CACHE[raw] = parsed
    return parsed


def is_holiday(day: date) -> bool:
    """该自然日是否休市：周六周日一律休市，或登记在休市日历里。"""
    return day.weekday() >= 5 or day.isoformat() in active_holidays()


def next_trading_day(day: date) -> date:
    """返回 day 之后（不含当天）的第一个交易日。"""
    cursor = day + timedelta(days=1)
    for _ in range(400):
        if not is_holiday(cursor):
            return cursor
        cursor += timedelta(days=1)
    return cursor


def _parse_hm(text: str) -> dtime:
    """``"09:30"`` → ``time(9, 30)``。非法值抛 ValueError（调用方负责兜底）。"""
    raw = str(text or "").strip()
    parts = raw.replace("：", ":").split(":")
    if len(parts) != 2:
        raise ValueError(f"时间格式应为 HH:MM，收到 {text!r}")
    hh, mm = int(parts[0]), int(parts[1])
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"时间越界：{text!r}")
    return dtime(hour=hh, minute=mm)


def parse_slots(raw: str | None) -> list[Slot]:
    """把 ``session_slots`` 的 JSON 文本解析成时段表。

    格式（数组，按时间升序）：

    .. code-block:: json

        [{"start": "09:30", "end": "11:30", "label": "上午连续竞价",
          "segment": "continuous", "match": true, "trade": true}]

    空串 / 空数组返回 ``[]``，表示「没有自定义时段」。解析失败抛 ValueError，
    由运行期配置层转成「拒绝保存」的提示 —— 宁可拒绝，也不要把坏数据落库。
    """
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"时段表不是合法 JSON：{exc.msg}") from exc
    if not isinstance(data, list):
        raise ValueError("时段表必须是数组")

    slots: list[Slot] = []
    for idx, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {idx} 段必须是对象")
        try:
            start = _parse_hm(item.get("start"))
            end = _parse_hm(item.get("end"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"第 {idx} 段时间非法：{exc}") from exc
        if start >= end:
            raise ValueError(f"第 {idx} 段开始时间必须早于结束时间")
        segment = str(item.get("segment") or "continuous").strip()
        if segment not in ALLOWED_SEGMENTS:
            raise ValueError(
                f"第 {idx} 段 segment 只能是 {'/'.join(ALLOWED_SEGMENTS)}，收到 {segment!r}"
            )
        slots.append(
            Slot(
                start=start.strftime("%H:%M"),
                end=end.strftime("%H:%M"),
                label=str(item.get("label") or SEGMENT_LABELS.get(segment, segment))[:20],
                segment=segment,
                match=bool(item.get("match", True)),
                trade=bool(item.get("trade", True)),
                phase=str(item.get("phase") or SEGMENT_DEFAULT_PHASE.get(segment, "continuous")),
            )
        )

    slots.sort(key=lambda s: s.start)
    for prev, cur in zip(slots, slots[1:]):
        if cur.start < prev.end:
            raise ValueError(f"时段「{prev.label}」与「{cur.label}」有重叠")
    return slots


def preset_slots_json(key: str) -> str:
    """把内置预设导出成规整的 JSON 文本，供后台按钮一键填入。"""
    conf = PRESETS.get(key) or PRESETS["a_share"]
    rows = [
        {
            "start": s.start,
            "end": s.end,
            "label": s.label,
            "segment": s.segment,
            "match": s.match,
            "trade": s.trade,
        }
        for s in conf["slots"]
    ]
    return json.dumps(rows, ensure_ascii=False, indent=2)


def match_preset(slots: list[Slot]) -> str:
    """判断一份时段表是否与某个内置预设完全一致，一致就返回该预设 key。

    只用于展示（「当前时段」标签）与保存校验，不参与择时计算 ——
    交易时段只有「保存下来的那张表」这一个事实来源。
    """
    if not slots:
        return ""
    signature = [(s.start, s.end, s.segment, s.match, s.trade) for s in slots]
    for key in FILLABLE_PRESETS:
        conf = PRESETS[key]
        if signature == [(s.start, s.end, s.segment, s.match, s.trade) for s in conf["slots"]]:
            return key
    return "custom"


def resolve_plan() -> tuple[str, object, list[Slot], str]:
    """返回 ``(preset_key, tzinfo, slots, tz_label)``。

    时段表的唯一来源是 ``settings.session_slots`` 的 JSON。preset_key 只是
    这份表「刚好等于哪个内置预设」的判断结果，供状态栏显示。

    JSON 为空或解析失败时回落 A 股内置时段 —— 否则一整天都是「休市」，
    运营会以为服务挂了。
    """
    try:
        slots = parse_slots(getattr(settings, "session_slots", ""))
    except ValueError:
        slots = []
    if not slots:
        conf = PRESETS["a_share"]
        return "a_share", CN_ZONE, list(conf["slots"]), "北京时间"

    key = match_preset(slots)
    if key in PRESETS:
        tz, tz_label = _resolve_tz(PRESETS[key]["timezone"])
        return key, tz, slots, tz_label
    # 自定义表没有时区信息，按 A 股/北京时间处理
    return "custom", CN_ZONE, slots, "北京时间"


def _resolve_tz(name: str) -> tuple[object, str]:
    text = str(name or "").strip()
    if text in {"Asia/Shanghai", "CN", "cn"}:
        return CN_ZONE, "北京时间"
    if text in {"America/New_York", "US", "us"}:
        return US_TZ, "美东时间"
    try:  # 允许运营填任意 IANA 时区
        from zoneinfo import ZoneInfo as _ZI

        return _ZI(text), text
    except Exception:
        return CN_ZONE, "北京时间"


# --------------------------------------------------------------------------- #
# 时钟快照
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SessionState:
    """某一时刻的交易时钟快照。"""

    mode: str            # t1 / t0 / debug
    settle: str          # t1 / t0 —— 当日买入是否可当日卖
    preset: str          # a_share / us / custom
    phase: str           # 细粒度阶段
    segment: str         # pre / auction / continuous / lunch / post / fixed / closed
    is_open: bool        # 是否允许连续撮合（价格推进）
    can_trade: bool      # 是否允许委托申报
    label: str
    trade_date: str
    seconds_left: int
    timezone_label: str = "北京时间"

    @property
    def is_auction(self) -> bool:
        return self.segment == "auction"

    @property
    def is_debug(self) -> bool:
        return self.mode == "debug"

    @property
    def mode_label(self) -> str:
        return TRADE_MODES.get(self.mode, self.mode)

    @property
    def settle_label(self) -> str:
        return SETTLE_LABELS.get(self.settle, self.settle)

    @property
    def preset_label(self) -> str:
        return PRESETS.get(self.preset, {}).get("label", self.preset)

    @property
    def segment_label(self) -> str:
        return SEGMENT_LABELS.get(self.segment, self.segment)

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "mode_label": self.mode_label,
            "settle": self.settle,
            "settle_label": self.settle_label,
            "preset": self.preset,
            "preset_label": self.preset_label,
            "phase": self.phase,
            "segment": self.segment,
            "segment_label": self.segment_label,
            "label": self.label,
            "is_open": self.is_open,
            "can_trade": self.can_trade,
            "trade_date": self.trade_date,
            "seconds_left": self.seconds_left,
            "timezone_label": self.timezone_label,
        }


def _now(tz) -> datetime:
    return datetime.now(tz)


def _seconds_between(now: datetime, target: dtime) -> int:
    dt = datetime.combine(now.date(), target, tzinfo=now.tzinfo)
    return max(0, int((dt - now).total_seconds()))


def _seconds_until(day: date, now: datetime) -> int:
    """距目标日 00:00 还有多少秒（休市日用来显示「何时恢复」）。"""
    target = datetime.combine(day, dtime(0, 0), tzinfo=now.tzinfo)
    return max(0, int((target - now).total_seconds()))


def _closed_state(mode: str, preset: str, now: datetime, label: str, segment: str = "closed",
                  tz_label: str = "北京时间", phase: str = "closed", seconds_left: int = 0) -> SessionState:
    return SessionState(
        mode=mode,
        settle=settle_of(mode),
        preset=preset,
        phase=phase,
        segment=segment,
        is_open=False,
        can_trade=False,
        label=label,
        trade_date=now.strftime("%Y-%m-%d"),
        seconds_left=seconds_left,
        timezone_label=tz_label,
    )


def _slot_state(mode: str, preset: str, slot: Slot, now: datetime, tz_label: str,
                seconds_left: int, label: str | None = None) -> SessionState:
    return SessionState(
        mode=mode,
        settle=settle_of(mode),
        preset=preset,
        phase=slot.phase or SEGMENT_DEFAULT_PHASE.get(slot.segment, "continuous"),
        segment=slot.segment,
        is_open=bool(slot.match),
        can_trade=bool(slot.trade),
        label=label or slot.label,
        trade_date=now.strftime("%Y-%m-%d"),
        seconds_left=max(0, seconds_left),
        timezone_label=tz_label,
    )


def _plan_session(mode: str, preset: str, slots: list[Slot], tz, tz_label: str, now: datetime) -> SessionState:
    """在时段表里定位当前时刻。表内无命中即为休市（盘前/盘后空窗、周末）。

    休市日先于时段表判定：节假日/周末整天都不该被时段表命中，否则
    国庆假期里配一段 09:30-11:30 就会把假期中的同一天也算成连续竞价，
    价格照涨、委托照收。
    """
    if now.weekday() >= 5:
        return _closed_state(mode, preset, now, "周末休市", tz_label=tz_label)
    if now.date().isoformat() in active_holidays():
        return _closed_state(
            mode, preset, now, "节假日休市",
            tz_label=tz_label, phase="holiday",
            seconds_left=_seconds_until(next_trading_day(now.date()), now),
        )

    t = now.time()
    for slot in slots:
        start = _parse_hm(slot.start)
        end = _parse_hm(slot.end)
        if start <= t < end:
            return _slot_state(mode, preset, slot, now, tz_label, _seconds_between(now, end))
        if t < start:
            # 处于两段之间（或首段之前）的空窗：给出到下一段开始的时间
            return _closed_state(
                mode, preset, now,
                label="未开盘" if not slots or slot is slots[0] else "时段间隙",
                segment="closed",
                tz_label=tz_label,
                phase="closed",
                seconds_left=_seconds_between(now, start),
            )

    # 最后一段已结束
    return _closed_state(mode, preset, now, "已收盘", tz_label=tz_label, phase="closed")


def current_session(ts: float | None = None) -> SessionState:
    """返回当前（或指定时间戳）的交易时钟状态。"""
    mode = normalize_mode(getattr(settings, "trade_mode", None))

    if mode == "debug":
        # 调试模式：时段设置整体失效。全天可申报可撮合，按 T+0 结算。
        now = datetime.fromtimestamp(ts, CN_ZONE) if ts is not None else _now(CN_ZONE)
        return SessionState(
            mode="debug",
            settle="t0",
            preset="custom",
            phase="debug",
            segment="continuous",
            is_open=True,
            can_trade=True,
            label="调试模式（全天交易）",
            trade_date=now.strftime("%Y-%m-%d"),
            seconds_left=0,
            timezone_label="北京时间",
        )

    preset, tz, slots, tz_label = resolve_plan()
    now = datetime.fromtimestamp(ts, tz) if ts is not None else _now(tz)
    return _plan_session(mode, preset, slots, tz, tz_label, now)


def is_trading_time(ts: float | None = None) -> bool:
    """是否允许下单（集合竞价也允许申报）。"""
    return current_session(ts).can_trade


def is_matching(ts: float | None = None) -> bool:
    """是否允许连续撮合（价格推进）。"""
    return current_session(ts).is_open


def sessions_payload() -> dict:
    """给客户端/后台的完整时段配置：模式、预设、各自时段表。

    客户端只用 ``current`` 就够渲染状态栏；「交易时段」设置页再用 ``presets``
    展示各预设的时段表，避免前端把时间写死一份。
    """
    current = current_session()
    preset, _tz, slots, tz_label = resolve_plan()
    return {
        "current": current.to_dict(),
        "presets": [
            {
                "key": key,
                "label": conf["label"],
                "timezone": conf["timezone"],
                "timezone_label": conf["tz_label"],
                "slots": [s.to_dict() for s in conf["slots"]],
            }
            for key, conf in PRESETS.items()
            if key != "custom"
        ],
        "active": {
            "preset": preset,
            "preset_label": PRESETS.get(preset, {}).get("label", preset),
            "timezone_label": tz_label,
            "slots": [s.to_dict() for s in slots],
        },
        "modes": [{"value": k, "label": v} for k, v in TRADE_MODES.items()],
    }


def trading_days(count: int, end: datetime | None = None) -> list[str]:
    """回溯最近 count 个交易日（跳过周末与休市日历里的节假日）。"""
    end = end or cn_now()
    days: list[str] = []
    cursor = end.date()
    while len(days) < count:
        if not is_holiday(cursor):
            days.append(cursor.strftime("%Y-%m-%d"))
        cursor -= timedelta(days=1)
    return list(reversed(days))


def cn_now() -> datetime:
    return datetime.now(CN_ZONE)


def now_epoch() -> float:
    return time.time()
