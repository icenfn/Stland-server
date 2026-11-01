"""实时行情推送：WebSocket 单通道多路复用。

为什么要有它
-----------
客户端此前只能靠 HTTP 轮询（1.5s 一次 overview + list），弱网下延迟抖动大，
而且指数与榜单分属两个请求，可能出现「指数是这一秒、榜单是上一秒」的不一致。
这里用一条 WebSocket 连接按引擎 tick 推送聚合快照，客户端一次拿到的数据天然同 tick。

同时保留「连不上就退回轮询」的兼容策略：
  · 服务端未开启（如老版本部署）→ 握手失败，客户端自动轮询；
  · 中间设备拦截 WS → 同上；
  · 连接中断 → 客户端指数退避重连，期间轮询持续兜底。
"""
from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlmodel import Session, select

from ..config import settings
from ..db import engine
from ..engine import index as index_engine
from ..engine.market import market_engine
from ..engine.session import current_session
from ..models import Stock
from ..security import TokenError, decode_token
from ..services.events import hub

router = APIRouter(tags=["stream"])


def _snapshot() -> dict:
    """一次组装首页所需的全部数据，保证指数 / 分布 / 榜单同 tick。"""
    quotes = list(market_engine.quotes().values())
    st = current_session()
    with Session(engine) as session:
        metas = {s.code: s for s in session.exec(select(Stock)).all()}
    rows = [
        {**q, "industry": getattr(metas.get(q["code"]), "industry", ""), "board": getattr(metas.get(q["code"]), "board", "")}
        for q in quotes
        if q["code"] in metas
    ]
    # 指数统一由 engine/index.py 计算，保证 WS 推送与 HTTP /api/market/index 口径一致
    indices = index_engine.index_list(session)
    return {
        "index": indices[0] if indices else {},
        "indices": indices,
        # 取满一天 240 个分钟位：客户端按「时间位」画横轴，
        # 只给 120 个点的话分时线会停在半幅宽，看起来像被截断了
        "index_minutes": index_engine.index_minutes(session, index_engine.INDEX_LIST[0], limit=240),
        "stats": market_engine.stats(),
        "session": {
            "phase": st.phase,
            "label": st.label,
            "is_open": st.is_open,
            "can_trade": st.can_trade,
            "trade_date": st.trade_date,
        },
        "rank": {
            "gainers": sorted(rows, key=lambda r: r["change_pct"], reverse=True)[:5],
            "losers": sorted(rows, key=lambda r: r["change_pct"])[:5],
            "active": sorted(rows, key=lambda r: r["amount"], reverse=True)[:5],
        },
        "items": rows,
        "ts": int(time.time()),
    }


@router.websocket("/ws/market")
async def market_stream(ws: WebSocket) -> None:
    """按 tick_seconds 推送行情快照；客户端可用 {"type":"ping"} 保活。

    说明：WS 不走 HTTP 中间件，因此不做签名校验；它只推送公开行情，
    不涉及任何账户或交易数据，敏感操作仍走带签名的 HTTP 接口。
    """
    await ws.accept()
    try:
        while True:
            payload = {"type": "snapshot", "data": _snapshot()}
            await ws.send_text(json.dumps(payload, ensure_ascii=False, default=str))
            # 在等待期间也响应客户端的 ping，否则长连接会被中间设备判定空闲
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=max(1.0, settings.tick_seconds))
                try:
                    msg = json.loads(raw)
                except Exception:
                    msg = {}
                if msg.get("type") == "ping":
                    await ws.send_text(json.dumps({"type": "pong", "ts": int(time.time())}))
            except asyncio.TimeoutError:
                continue
    except WebSocketDisconnect:
        return
    except Exception:
        return


@router.websocket("/ws/private")
async def private_stream(ws: WebSocket) -> None:
    """账户私有频道：委托状态变更与成交回报。

    鉴权走 URL 查询参数 ``?token=<访问令牌>``：浏览器 WebSocket 构造函数
    不能自定义请求头，Authorization 头这条路在浏览器里走不通，
    查询参数是浏览器客户端唯一可用的载体。

    只推「发生了什么」的轻量事件，不带金额与持仓明细 —— 事件丢了不影响正确性，
    客户端收到后回查 REST 接口拿真值（见 services/events.py 的说明）。
    """
    token = ws.query_params.get("token", "")
    user_id = 0
    try:
        payload = decode_token(token)
        if payload.get("role") == "admin":
            await ws.close(code=4403)
            return
        user_id = int(payload.get("sub", 0))
    except (TokenError, ValueError):
        # 在 accept 之前关闭会变成 HTTP 403；先 accept 再带码关闭，
        # 客户端才能区分「令牌过期」与「服务不存在」并决定是否重登。
        await ws.accept()
        await ws.close(code=4401)
        return
    if not user_id:
        await ws.accept()
        await ws.close(code=4401)
        return

    await ws.accept()
    queue = hub.subscribe(user_id)
    try:
        await ws.send_text(json.dumps({"type": "ready", "ts": int(time.time())}))
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=25.0)
            except asyncio.TimeoutError:
                # 心跳：私有频道平时可能长时间没有事件，中间设备会把静默连接掐掉
                await ws.send_text(json.dumps({"type": "pong", "ts": int(time.time())}))
                continue
            await ws.send_text(json.dumps(event, ensure_ascii=False, default=str))
    except WebSocketDisconnect:
        return
    except Exception:
        return
    finally:
        hub.unsubscribe(user_id, queue)
