"""账户私有事件总线：委托状态变更与成交回报。

为什么需要它
-----------
HTTP 下单接口只返回「这一笔委托此刻的结果」。之后发生的三件事它都推不到：
  · 挂着的限价单被行情走到、由 ``rematch_pending`` 撮合成交；
  · 自己挂在盘上的单被别人的委托吃掉（``_settle_resting``）；
  · 委托被撤单或日终终结。
这三件都发生在「下单请求已经返回之后」，客户端只能靠轮询发现，延迟且费流量。

这里用进程内发布/订阅：交易链路 publish，WebSocket 按 user_id 订阅。
不落库、不做重放 —— 事件只是「催一下」，客户端收到后回查委托列表拿真值。
这样即使事件丢了（进程重启、订阅者断线），数据也不会不一致；反过来若把事件本身
当账本，就得为它做持久化、去重、补偿，代价远高于让客户端回查一次。

多实例部署时需换成 Redis Pub/Sub 等跨进程通道，订阅接口保持不变即可。
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any

logger = logging.getLogger("stland.events")

#: 每个订阅者最多积压多少条事件。慢客户端不该拖垮撮合线程：
#: 队列满了就丢最旧的 —— 事件只是「去回查」的提示，丢掉只多一次轮询。
MAX_BACKLOG = 200


class EventHub:
    """按 user_id 分组的进程内事件广播。"""

    def __init__(self) -> None:
        self._subs: dict[int, set[asyncio.Queue]] = defaultdict(set)
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """记录事件循环，供工作线程跨线程投递。

        撮合跑在后台线程 / 请求线程里，而 Queue 只能在事件循环线程内安全操作，
        所以投递一律走 ``loop.call_soon_threadsafe``。
        """
        self._loop = loop

    def subscribe(self, user_id: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=MAX_BACKLOG)
        self._subs[int(user_id)].add(q)
        return q

    def unsubscribe(self, user_id: int, q: asyncio.Queue) -> None:
        bucket = self._subs.get(int(user_id))
        if not bucket:
            return
        bucket.discard(q)
        if not bucket:
            self._subs.pop(int(user_id), None)

    def _deliver(self, user_id: int, event: dict) -> None:
        for q in list(self._subs.get(int(user_id), ())):
            if q.full():
                # 丢最旧的：慢客户端醒来后只关心「最新发生了什么」
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover - 竞争兜底
                    pass
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover - 上面已腾位
                pass

    def publish(self, user_id: int, event: dict) -> None:
        """投递一条事件给该账户的所有订阅者。无订阅者时是空操作。"""
        uid = int(user_id or 0)
        if not uid or not self._subs.get(uid):
            return
        payload = {"type": "trade_event", "ts": __import__("time").time(), **event}
        loop = self._loop
        if loop is None or not loop.is_running():
            # 不在事件循环内（如单元测试直接调服务层）：同步投递，
            # 此时队列只可能在同一线程里被消费。
            self._deliver(uid, payload)
            return
        try:
            loop.call_soon_threadsafe(self._deliver, uid, payload)
        except RuntimeError:  # 循环已关闭
            logger.debug("事件循环已关闭，丢弃 %s 的私有事件", uid)


hub = EventHub()


def order_event(order: Any, kind: str, **extra: Any) -> dict:
    """把委托对象拍成一条推送事件（只用订单表上确实存在的字段）。

    刻意不在这里查库拼成交明细：事件只是提示，客户端收到后回查
    ``/api/trade/orders`` 与 ``/api/trade/trades`` 拿完整数据。
    """
    return {
        "kind": kind,
        "order_id": getattr(order, "id", None),
        "order_no": getattr(order, "order_no", ""),
        "code": getattr(order, "code", ""),
        "side": getattr(getattr(order, "side", None), "value", ""),
        "status": getattr(getattr(order, "status", None), "value", ""),
        "price": float(getattr(order, "price", 0.0) or 0.0),
        "quantity": int(getattr(order, "quantity", 0) or 0),
        "filled_quantity": int(getattr(order, "filled_quantity", 0) or 0),
        "avg_fill_price": float(getattr(order, "avg_fill_price", 0.0) or 0.0),
        **extra,
    }
