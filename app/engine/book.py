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

档位量为什么必须随 tick 变化
--------------------------
上一版 ``build_levels`` 只按 ``seed_volume`` 做固定衰减，同一价位的档位量
在整段行情里一模一样。价格每 tick 都在动、盘口数字却纹丝不动，
看起来就像「五档坏了」。真实盘口的两条经验规律这里都要体现：

1. **档位量随机波动**：任一时刻各档挂单量随委托流进出来回抖动；
2. **买卖盘不平衡随价格方向倾斜**：上涨时买盘（承接）更厚、卖盘（抛压）更薄，
   下跌时相反 —— 这就是盘口"买一厚/卖一薄"的方向感。

因此 ``build_levels`` 接受三个可选调节量：``rng``（随机源，保证可复现）、
``intensity``（成交活跃度，放量时整体加深）、``skew``（方向偏移，-0.45~0.45）。
"""
from __future__ import annotations

import random
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


def _qty(value: float) -> int:
    """档位量取整到 100 股的整数倍，并保证至少 100 股。"""
    return max(100, int(round(value / 100.0)) * 100)


def build_levels(
    mid: float,
    tick: float,
    depth: int,
    spread_ticks: int,
    seed_volume: float,
    *,
    rng: random.Random | None = None,
    intensity: float = 1.0,
    skew: float = 0.0,
) -> tuple[list[Level], list[Level]]:
    """依据中间价生成对称五档盘口。

    * ``seed_volume`` —— 单档基准挂单量（由标的日均成交量与流动性决定）；
    * ``intensity``   —— 活跃度系数：放量时盘口整体加深，缩量时变薄；
    * ``skew``        —— 方向偏移：>0 买盘更厚（上涨承接强），<0 卖盘更厚；
    * ``rng``         —— 随机源；传入引擎的 rng 可保证同一 seed 结果可复现。

    每一档、每一侧都独立抖动，所以连续两个 tick 的盘口一定不同 ——
    这正是真实盘口的样子，也是「五档不更新」问题的修复点。
    """
    _rng = rng or random
    spread_ticks = max(1, int(spread_ticks))
    depth = max(1, int(depth))
    intensity = max(0.2, min(4.0, float(intensity)))
    skew = max(-0.45, min(0.45, float(skew)))

    bids: list[Level] = []
    asks: list[Level] = []
    half = spread_ticks / 2.0
    for i in range(depth):
        bid_px = snap(mid - (half + i) * tick, tick)
        ask_px = snap(mid + (half + i) * tick, tick)
        # 档位越深挂单越多（累积簿的自然形态）
        decay = 1.0 + 0.45 * i
        # 抖动幅度随档位变大：越深的档位越不稳定
        jitter = 0.22 + 0.06 * i
        bid_vol = seed_volume * decay * intensity * (1.0 + skew) * (1.0 + _rng.uniform(-jitter, jitter))
        ask_vol = seed_volume * decay * intensity * (1.0 - skew) * (1.0 + _rng.uniform(-jitter, jitter))
        bids.append(Level(price=bid_px, volume=_qty(bid_vol)))
        asks.append(Level(price=ask_px, volume=_qty(ask_vol)))
    return bids, asks


def best_bid(bids: list[Level]) -> float:
    return bids[0].price if bids else 0.0


def best_ask(asks: list[Level]) -> float:
    return asks[0].price if asks else 0.0
