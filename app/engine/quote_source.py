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
    """外部真实行情源（骨架）。

    行情由外部推送写入 ``_prices``；盘口不由行情源提供，而是由
    ``orderbook`` 依据用户委托现算 —— 这正是「仅靠零售/机构真实交易」
    与模拟盘的根本区别。

    目前 ``_prices`` 需要外部 feeder 调用 ``publish()`` 填充，
    未接入 feeder 时 ``code_exists`` 恒为 False，下单会被拒。
    这样默认状态是「明确没有行情」，而不是「假装有行情」。
    """

    name = "external"

    def __init__(self) -> None:
        self._prices: dict[str, float] = {}

    def publish(self, code: str, price: float) -> None:
        """外部行情 feeder 调这个接口写入最新价。"""
        self._prices[code] = float(price)

    def code_exists(self, code: str) -> bool:
        return code in self._prices

    def last_price(self, code: str) -> float:
        return self._prices.get(code, 0.0)

    def quote_any(self, code: str) -> dict | None:
        price = self._prices.get(code)
        if price is None:
            return None
        return {"code": code, "price": float(price)}

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
