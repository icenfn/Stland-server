"""行情源抽象：把「价格从哪来」和「交易怎么做」拆开。

原代码把这两件事焊在一起 —— ``market_engine`` 既是唯一的报价来源，
又是撮合时的对手盘（``_book_levels()`` 直接读它的 ``depth()``）。
于是关闭模拟引擎就等于关闭整个市场：没有价格，也没有对手盘。

这里定义 ``QuoteSource`` 协议，两个实现：

* ``SimulatedSource``：现有 GBM 引擎，行情与盘口都由它生成；
* ``ExternalSource``：接真实行情的占位实现，行情来自外部推送，
  对手盘来自 ``orderbook``（用户订单簿）。

切换只改 ``settings.quote_source``，交易链路不需要感知差别。

注意：``ExternalSource`` 目前只搭了骨架。真正落地还需要一个行情接入
客户端（轮询或 WebSocket），把价格写进 ``publish()``。
之所以先落协议而不是直接写死模拟引擎，是因为「用户订单簿撮合」本身
并不依赖行情来自哪里 —— 只要 ``last_price`` 有值就能撮。
"""
from __future__ import annotations

import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class QuoteSource(Protocol):
    """行情源协议。交易链路只依赖这几个查询。"""

    name: str

    def code_exists(self, code: str) -> bool:
        """这个标的当前是否有报价（没有就不能下单）。"""
        ...

    def last_price(self, code: str) -> float:
        """最新价；无报价时返回 0。"""
        ...

    def depth(self, code: str, limit: int = 5) -> dict:
        """盘口 ``{"bids": [...], "asks": [...]}``，无数据时返回空列表。"""
        ...

    def quote_any(self, code: str) -> dict | None:
        """完整报价字典，无报价时返回 ``None``。"""
        ...


class SimulatedSource:
    """模拟行情源：包一层现有 ``market_engine``，接口对齐 ``QuoteSource``。"""

    name = "simulated"

    def code_exists(self, code: str) -> bool:
        from .market import market_engine

        return market_engine.quote(code) is not None

    def last_price(self, code: str) -> float:
        from .market import market_engine

        quote = market_engine.quote(code)
        return float(quote.get("price") or 0.0) if quote else 0.0

    def depth(self, code: str, limit: int = 5) -> dict:
        from .market import market_engine

        return market_engine.depth(code) or {"bids": [], "asks": []}

    def quote_any(self, code: str) -> dict | None:
        from .market import market_engine

        return market_engine.quote(code)


class ExternalSource:
    """外部真实行情源：行情由 feeder 推送，对手盘来自用户订单簿。

    盘口不由行情源提供，而是由 ``orderbook`` 按用户委托现算 —— 这正是
    「仅靠零售/机构真实交易」与模拟盘的根本区别。

    为什么必须有过期保护，而不能只存一个最新价：行情停更时（网络断、
    feeder 崩、交易所休市），若继续用最后那个价格撮合，用户会以「已经
    不存在的价格」成交 —— 看起来一切正常，实际成交价偏离真实市场。宁可
    让 ``code_exists`` 变 False（下单被明确拒绝），也不要静默用陈旧价。
    """

    name = "external"

    #: 报价有效期（秒）。超过这个时间没更新，视为无报价。
    #: 15 秒对 A 股 3 秒快照 + 抖动留了余量，又不至于把正常延迟判成过期。
    DEFAULT_TTL = 15.0

    def __init__(self) -> None:
        self._prices: dict[str, float] = {}
        # 每个标的最近一次推送的时间戳，用于过期判断与心跳监控
        self._seen: dict[str, float] = {}
        # feeder 自身的最后活动时间：即使没有标的更新，也能据此判断
        # 「是行情没变」还是「feeder 已经死了」。
        self._last_beat: float = 0.0

    # ---------------- feeder 侧 ----------------
    def publish(self, code: str, price: float, ts: float | None = None) -> None:
        """外部行情 feeder 调用：写入单个标的最新价。"""
        now = time.time() if ts is None else float(ts)
        self._prices[code] = float(price)
        self._seen[code] = now
        self._last_beat = now

    def publish_batch(self, quotes: dict[str, float], ts: float | None = None) -> int:
        """批量写入（一次快照推多个标的，减少锁竞争与调用次数）。"""
        now = time.time() if ts is None else float(ts)
        for code, price in quotes.items():
            self._prices[code] = float(price)
            self._seen[code] = now
        if quotes:
            self._last_beat = now
        return len(quotes)

    def heartbeat(self) -> None:
        """feeder 存活信号：即使本轮到没有价格更新也要打点。"""
        self._last_beat = time.time()

    # ---------------- 状态查询 ----------------
    def is_fresh(self, code: str, ttl: float | None = None) -> bool:
        """该标的报价是否仍在有效期内。"""
        seen = self._seen.get(code)
        if seen is None:
            return False
        limit = self.DEFAULT_TTL if ttl is None else float(ttl)
        return (time.time() - seen) <= limit

    def health(self, ttl: float | None = None) -> dict:
        """行情源健康状况，给后台「行情源」面板与告警用。"""
        limit = self.DEFAULT_TTL if ttl is None else float(ttl)
        now = time.time()
        fresh = [c for c in self._prices if self.is_fresh(c, limit)]
        stale = [c for c in self._prices if c not in set(fresh)]
        beat_age = (now - self._last_beat) if self._last_beat else None
        return {
            "name": self.name,
            "symbols": len(self._prices),
            "fresh": len(fresh),
            "stale": len(stale),
            "stale_codes": stale[:20],
            "ttl": limit,
            "last_beat_age": None if beat_age is None else round(beat_age, 1),
            # feeder 超过 3 个 TTL 没动静就认为掉线
            "feeder_alive": bool(beat_age is not None and beat_age <= limit * 3),
        }

    # ---------------- QuoteSource 协议 ----------------
    def code_exists(self, code: str) -> bool:
        # 过期等于没有：不能拿陈旧价撮合
        return self.is_fresh(code)

    def last_price(self, code: str) -> float:
        if not self.is_fresh(code):
            return 0.0
        return self._prices.get(code, 0.0)

    def quote_any(self, code: str) -> dict | None:
        if not self.is_fresh(code):
            return None
        return {"code": code, "price": float(self._prices[code])}

    def depth(self, code: str, limit: int = 5) -> dict:
        from . import orderbook

        # 外部源没有合成盘口：真正的对手盘只能是其他用户的委托
        from ..db import session_scope

        with session_scope() as session:
            bids = orderbook.quote_levels(session, code, "buy", limit)
            asks = orderbook.quote_levels(session, code, "sell", limit)
        return {"bids": bids, "asks": asks}


def get_source() -> QuoteSource:
    """按配置返回当前行情源。配置写错时回落到模拟源而不是抛异常 ——
    配置错误不该让整个交易入口 500。"""
    from ..config import settings

    mode = str(getattr(settings, "quote_source", "simulated") or "simulated").lower()
    if mode == "external":
        return _external
    return _simulated


_simulated = SimulatedSource()
_external = ExternalSource()
