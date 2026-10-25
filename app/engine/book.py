"""订单簿与撮合核心。

研究依据（见 docs/research.md）：
1. 中国 A 股采用**指令驱动（order-driven）**、价格优先-时间优先的连续竞价机制
   （上证联合研究报告《交易机制与市场质量》；《管理科学技术名词》"订单驱动"词条）；
2. 最小报价单位（tick size）造成价格离散化，既影响买卖价差也影响价格发现，
   tick 过大 → 价差变宽、成本上升；tick 过小 → 深度下降、插队成本降低
   （《基金分改厘最小报价单位对基金交易影响》，上海证券报；SEC 2024 tick 试点、港交所 2025 下调最低上落价位）；
3. 市价单在订单簿各档位价差极端时会显著抬升成交成本，故本文实现"市价单按档位逐层成交 + 滑点保护"
   （上证所创新实验室《市价订单的交易风险与下单策略研究》）。

实现要点：
* 撮合严格按 (价格优先, 时间优先) 排序，逐档吃单，允许部分成交；
* 所有价格对齐 tick 网格并裁剪在涨跌停区间内；
* 提供 VWAP 估算，用于客户端"预估成交"提示与滑点控制。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


def snap(price: float, tick: float) -> float:
    """对齐到最小报价单位（四舍五入到 tick 的整数倍）。"""
    if tick <= 0:
        return round(price, 4)
    return round(round(price / tick) * tick, 4)


def clip(price: float, low: float, high: float) -> float:
    return max(low, min(high, price))


@dataclass
class Level:
    price: float
    volume: int


@dataclass
class SideBook:
    """单边订单簿（档位按从优到劣排列）。"""

    levels: list[Level] = field(default_factory=list)

    def total(self) -> int:
        return sum(lvl.volume for lvl in self.levels)


@dataclass
class Fill:
    price: float
    quantity: int


def walk_book(levels: Iterable[Level], quantity: int, limit_price: float | None, side: str) -> list[Fill]:
    """逐档吃单。

    side="buy" 时从最低卖价开始，价格不得超过 limit_price；
    side="sell" 时从最高买价开始，价格不得低于 limit_price。
    """
    fills: list[Fill] = []
    remaining = quantity
    for lvl in levels:
        if remaining <= 0:
            break
        if limit_price is not None:
            if side == "buy" and lvl.price > limit_price + 1e-9:
                break
            if side == "sell" and lvl.price < limit_price - 1e-9:
                break
        take = min(remaining, lvl.volume)
        if take > 0:
            fills.append(Fill(price=lvl.price, quantity=take))
            remaining -= take
    return fills


def vwap(fills: list[Fill]) -> float:
    qty = sum(f.quantity for f in fills)
    if qty <= 0:
        return 0.0
    return round(sum(f.price * f.quantity for f in fills) / qty, 4)


def build_levels(
    mid: float,
    tick: float,
    depth: int,
    spread_ticks: int,
    seed_volume: float,
) -> tuple[list[Level], list[Level]]:
    """依据中间价生成对称五档盘口（数量按档位递减）。"""
    spread_ticks = max(1, int(spread_ticks))
    bids: list[Level] = []
    asks: list[Level] = []
    half = spread_ticks / 2.0
    for i in range(depth):
        bid_px = snap(mid - (half + i) * tick, tick)
        ask_px = snap(mid + (half + i) * tick, tick)
        decay = 1.0 + 0.45 * i
        bids.append(Level(price=bid_px, volume=int(seed_volume * decay)))
        asks.append(Level(price=ask_px, volume=int(seed_volume * decay)))
    return bids, asks


def best_bid(bids: list[Level]) -> float:
    return bids[0].price if bids else 0.0


def best_ask(asks: list[Level]) -> float:
    return asks[0].price if asks else 0.0
