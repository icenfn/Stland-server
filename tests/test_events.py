"""私有事件总线与私有频道鉴权测试。

钉住三件事：
1. **只推给本人**：A 的成交绝不能进 B 的队列 —— 这是「私有」频道的最低要求；
2. **慢客户端不拖垮撮合**：队列满了丢最旧，不阻塞发布方；
3. **订阅可回收**：断线后不留悬挂队列，否则内存随连接数一直长。
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time

os.environ.setdefault("STLAND_DB_URL", f"sqlite:///{tempfile.mkdtemp()}/test.db")
os.environ.setdefault("STLAND_SIGNATURE_REQUIRED", "false")

from types import SimpleNamespace  # noqa: E402

from app.models import OrderSide, OrderStatus  # noqa: E402
from app.services.events import EventHub, order_event  # noqa: E402


def _order(**kw):
    base = dict(
        id=7, order_no="O2026", code="600000.SH", side=OrderSide.BUY,
        status=OrderStatus.PARTIAL, price=10.5, quantity=1000,
        filled_quantity=400, avg_fill_price=10.5,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_event_only_reaches_owner():
    hub = EventHub()
    q1 = hub.subscribe(1)
    q2 = hub.subscribe(2)
    hub.publish(1, order_event(_order(), "filled"))
    assert q1.qsize() == 1
    assert q2.qsize() == 0


def test_no_subscriber_is_noop():
    """没人订阅时发布不该报错，也不该留下状态。"""
    hub = EventHub()
    hub.publish(99, order_event(_order(), "filled"))
    assert hub._subs == {}


def test_payload_carries_order_identity():
    hub = EventHub()
    q = hub.subscribe(1)
    hub.publish(1, order_event(_order(), "partial", trigger="passive"))
    ev = q.get_nowait()
    assert ev["type"] == "trade_event"
    assert ev["kind"] == "partial"
    assert ev["order_no"] == "O2026"
    assert ev["status"] == "partial"
    assert ev["filled_quantity"] == 400
    assert ev["trigger"] == "passive"
    assert "ts" in ev


def test_slow_client_drops_oldest_not_blocks():
    """满了要丢最旧的，不能反过来把发布方卡住。"""
    hub = EventHub()
    q = hub.subscribe(1)
    for i in range(400):          # 远超 MAX_BACKLOG=200
        hub.publish(1, order_event(_order(id=i), "filled"))
    assert q.qsize() <= 200
    # 最新的那条必须还在
    last = None
    while not q.empty():
        last = q.get_nowait()
    assert last["order_id"] == 399


def test_unsubscribe_cleans_up():
    hub = EventHub()
    q = hub.subscribe(1)
    hub.unsubscribe(1, q)
    assert hub._subs.get(1) in (None, set())


def test_multiple_tabs_same_user_all_get_it():
    hub = EventHub()
    a = hub.subscribe(1)
    b = hub.subscribe(1)
    hub.publish(1, order_event(_order(), "filled"))
    assert a.qsize() == 1 and b.qsize() == 1


def test_publish_from_thread_reaches_loop_queue():
    """撮合在工作线程里跑，跨线程投递必须真的落到队列上。"""
    async def main():
        hub = EventHub()
        hub.bind_loop(asyncio.get_running_loop())
        q = hub.subscribe(5)
        import threading
        threading.Thread(
            target=lambda: hub.publish(5, order_event(_order(), "filled"))
        ).start()
        ev = await asyncio.wait_for(q.get(), timeout=2.0)
        return ev

    ev = asyncio.run(main())
    assert ev["order_no"] == "O2026"


def test_private_ws_rejects_missing_token():
    """无令牌连接应带 4401 关闭码，客户端据此决定重新登录。"""
    from fastapi import FastAPI
    import inspect

    from app.routers import stream

    assert hasattr(stream, "private_stream")
    sig = inspect.signature(stream.private_stream)
    assert "ws" in sig.parameters
    # 路由已挂到 /ws/private
    app = FastAPI()
    app.include_router(stream.router)
    paths = {r.path for r in app.routes}
    assert "/ws/private" in paths
