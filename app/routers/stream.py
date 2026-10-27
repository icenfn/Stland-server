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
from ..engine.market import market_engine
from ..engine.session import current_session
from ..models import Stock

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
    return {
        "index": market_engine.index_quote(),
        "index_minutes": market_engine.index_minutes()[-120:],
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
