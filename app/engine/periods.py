"""日线到长周期的聚合（周 / 月 / 季 / 年）。

为什么读时聚合而不是落库：
  · 规则是纯函数——区间内「首开、最高、最低、末收、量额累加」，
    读一次算一遍的成本，远低于维护四套冗余表并保证它们与日线同步；
  · 交易日历若调整（临时休市、节假日），聚合结果跟着日线一起变，
    不需要回头补写历史。

分桶口径与主流行情软件一致：周按 ISO 周（周一为起点），
季按 1–3 / 4–6 / 7–9 / 10–12 月，年按自然年。
"""
from __future__ import annotations

from datetime import date as _date

LONG_PERIODS = ("week", "month", "quarter", "year")

# 这些周期走「日线聚合」分支，不走分钟聚合
MINUTE_PERIODS = ("1min", "5min", "15min", "30min", "60min", "120min")


def _parse(d) -> _date | None:
    try:
        return _date.fromisoformat(str(d)[:10])
    except (TypeError, ValueError):
        return None


def bucket_of(trade_date, period: str) -> tuple[int, int]:
    """返回 (年, 桶序号)：同一桶内的日线合成一根长周期柱。"""
    d = _parse(trade_date)
    if d is None:
        return (0, 0)
    if period == "week":
        iso = d.isocalendar()
        return (iso[0], iso[1])
    if period == "month":
        return (d.year, d.month)
    if period == "quarter":
        return (d.year, (d.month - 1) // 3 + 1)
    if period == "year":
        return (d.year, 0)
    return (0, 0)


def aggregate_daily(items: list[dict], period: str) -> list[dict]:
    """把按日期升序排列的日线合并成长周期柱。

    入参每项需含 time/open/high/low/close/volume/amount；
    输出的 time 取该桶内**第一个**交易日，与行情软件的习惯一致。
    """
    if period not in LONG_PERIODS or not items:
        return items

    out: list[dict] = []
    cur_key: tuple[int, int] | None = None
    cur: dict | None = None

    for it in items:
        key = bucket_of(it.get("time"), period)
        if cur is None or key != cur_key:
            if cur is not None:
                out.append(cur)
            cur_key = key
            cur = {
                "time": it["time"],
                "open": it["open"],
                "high": it["high"],
                "low": it["low"],
                "close": it["close"],
                "volume": it.get("volume") or 0,
                "amount": it.get("amount") or 0.0,
            }
            continue
        cur["high"] = max(cur["high"], it["high"])
        cur["low"] = min(cur["low"], it["low"])
        cur["close"] = it["close"]
        cur["volume"] += it.get("volume") or 0
        cur["amount"] += it.get("amount") or 0.0

    if cur is not None:
        out.append(cur)

    for b in out:
        b["amount"] = round(float(b.get("amount") or 0.0), 2)
    return out
