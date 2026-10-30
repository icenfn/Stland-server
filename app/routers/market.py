"""行情路由：列表、详情、五档、分时、K 线、日线、指数、搜索。

本轮变化：
* 指数从「首页聚合里的一个字段」升级为**可浏览对象**：有代码、能搜索、能查详情、
  有分时和 K 线，路由与个股同构（`/index/{code}` 对应 `/stock/{code}`）；
* 综合指数由 `engine/index.py` 统一计算，避免指数口径散落在多个位置。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlmodel import Session, select

from ..db import get_session
from ..engine import index as index_engine
from ..engine.market import market_engine
from ..engine.session import current_session
from ..models import DailyBar, Stock
from ..services.deps import optional_signature, signed_request

router = APIRouter(prefix="/api/market", tags=["market"])


@router.get("/overview")
def overview(_: None = Depends(optional_signature), session: Session = Depends(get_session)) -> dict:
    """首页聚合：指数、涨跌分布、榜单、交易时段。"""
    quotes = list(market_engine.quotes().values())
    stats = market_engine.stats()
    gainers = sorted(quotes, key=lambda q: q["change_pct"], reverse=True)[:5]
    losers = sorted(quotes, key=lambda q: q["change_pct"])[:5]
    active = sorted(quotes, key=lambda q: q["amount"], reverse=True)[:5]
    st = current_session()
    indices = index_engine.index_list(session)
    return {
        # index 保留单个主指数（客户端旧逻辑兼容），indices 是全量指数列表
        "index": indices[0] if indices else {},
        "indices": indices,
        "index_minutes": index_engine.index_minutes(session, index_engine.INDEX_LIST[0], limit=120),
        "stats": stats,
        "session": {
            "phase": st.phase,
            "label": st.label,
            "is_open": st.is_open,
            "can_trade": st.can_trade,
            "trade_date": st.trade_date,
        },
        "rank": {"gainers": gainers, "losers": losers, "active": active},
    }


@router.get("/list")
def list_quotes(
    request: Request,
    session: Session = Depends(get_session),
    sort: str = Query(default="change_pct", pattern="^(change_pct|amount|turnover_rate|price|code)$"),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    keyword: str = Query(default="", max_length=20),
    industry: str = Query(default="", max_length=20),
    board: str = Query(default="", max_length=12),
) -> dict:
    stocks = {s.code: s for s in session.exec(select(Stock)).all()}
    quotes = market_engine.quotes()
    rows = []
    for code, q in quotes.items():
        meta = stocks.get(code)
        if meta is None:
            continue
        if industry and meta.industry != industry:
            continue
        if board and meta.board != board:
            continue
        if keyword and keyword not in meta.name and keyword not in code:
            continue
        rows.append({**q, "industry": meta.industry, "board": meta.board, "hotword": meta.hotword})
    reverse = order == "desc"
    rows.sort(key=lambda r: r.get(sort, 0) or 0, reverse=reverse)
    return {"total": len(rows), "items": rows}


@router.get("/industries")
def industries(session: Session = Depends(get_session)) -> dict:
    stocks = session.exec(select(Stock)).all()
    quotes = market_engine.quotes()
    buckets: dict[str, dict] = {}
    for s in stocks:
        q = quotes.get(s.code)
        if not q:
            continue
        key = s.industry or "其他"
        b = buckets.setdefault(key, {"industry": key, "count": 0, "change_sum": 0.0})
        b["count"] += 1
        b["change_sum"] += q["change_pct"]
    items = [
        {"industry": b["industry"], "count": b["count"], "change_pct": round(b["change_sum"] / b["count"], 2)}
        for b in buckets.values()
    ]
    items.sort(key=lambda x: x["change_pct"], reverse=True)
    return {"items": items}


@router.get("/stock/{code}")
def stock_detail(code: str, request: Request, session: Session = Depends(get_session)) -> dict:
    meta = session.get(Stock, code)
    if meta is None:
        raise HTTPException(404, detail={"code": "UNKNOWN_SYMBOL", "message": "标的不存在"})
    quote = market_engine.quote(code)
    if quote is None:
        raise HTTPException(503, detail={"code": "QUOTE_NOT_READY", "message": "行情尚未就绪"})
    return {
        "quote": quote,
        "meta": {
            "code": meta.code,
            "name": meta.name,
            "board": meta.board,
            "industry": meta.industry,
            "hotword": meta.hotword,
            "theme": meta.theme,
            "tick_size": meta.tick_size,
            "lot_size": meta.lot_size,
            "limit_up": meta.limit_up,
            "limit_down": meta.limit_down,
            "total_shares": meta.total_shares,
            "float_shares": meta.float_shares,
            "is_st": meta.is_st,
            "is_index": False,
        },
    }


# --------------------------------------------------------------------------- #
# 指数：与个股同构的详情 / 分时 / K 线
# --------------------------------------------------------------------------- #
@router.get("/index")
def index_all(session: Session = Depends(get_session)) -> dict:
    """全部指数快照。"""
    items = index_engine.index_list(session)
    return {"items": items, "quote": items[0] if items else {}}


@router.get("/index/{code}")
def index_detail(code: str, session: Session = Depends(get_session)) -> dict:
    """指数详情：点位、涨跌、成分家数与涨跌分布。

    指数不可交易，因此**没有五档盘口**，但保留 `quote/meta` 结构与个股一致，
    客户端详情页可以复用同一套骨架，只在交易区做差异处理。
    """
    meta = index_engine.get_index(code)
    quote = index_engine.index_quote(session, meta)
    members = index_engine._members(session, meta)  # noqa: SLF001 - 同包内复用以统计成分
    live = market_engine.quotes()  # 成分列表直接带报价，避免客户端逐支再请求一遍
    return {
        "quote": quote,
        "meta": {
            "code": meta.code,
            "name": meta.name,
            "board": "指数",
            "industry": meta.note or "指数",
            "hotword": "",
            "theme": meta.note,
            "tick_size": 0.01,
            "lot_size": 0,
            "limit_up": 0.0,
            "limit_down": 0.0,
            "total_shares": 0.0,
            "float_shares": 0.0,
            "is_st": False,
            "is_index": True,
            "note": meta.note,
        },
        "members": [
            {
                "code": s.code,
                "name": s.name,
                "board": s.board,
                "industry": s.industry,
                "price": round(float(live.get(s.code, {}).get("price") or 0), 2),
                "change_pct": round(float(live.get(s.code, {}).get("change_pct") or 0), 2),
            }
            for s in members[:40]
        ],
    }


@router.get("/index/{code}/minutes")
def index_minutes(code: str, limit: int = Query(default=240, ge=1, le=1440), session: Session = Depends(get_session)) -> dict:
    meta = index_engine.get_index(code)
    return {"code": meta.code, "items": index_engine.index_minutes(session, meta, limit=limit)}


@router.get("/index/{code}/kline")
def index_kline(
    code: str,
    period: str = Query(default="1min", pattern="^(1min|5min|15min|30min|60min|day)$"),
    limit: int = Query(default=120, ge=10, le=500),
    session: Session = Depends(get_session),
) -> dict:
    """指数 K 线。日线由成分股日线等权合成，分钟级由合成分时聚合。"""
    meta = index_engine.get_index(code)
    if period == "day":
        items = index_engine.index_daily(session, meta, limit=limit)
        if not items:
            # 历史日线还没回填时退回分钟聚合，保证图不是空的
            items = index_engine.index_kline(session, meta, period="60min", limit=limit)
    else:
        items = index_engine.index_kline(session, meta, period=period, limit=limit)
    return {"code": meta.code, "period": period, "items": items}


# --------------------------------------------------------------------------- #
# 个股盘口 / 分时 / K 线
# --------------------------------------------------------------------------- #
@router.get("/depth/{code}")
def depth(code: str) -> dict:
    d = market_engine.depth(code)
    if d is None:
        raise HTTPException(404, detail={"code": "UNKNOWN_SYMBOL", "message": "标的不存在"})
    return d


@router.get("/minutes/{code}")
def minutes(code: str, limit: int = Query(default=240, ge=1, le=1440)) -> dict:
    return {"code": code, "items": market_engine.minute_series(code, limit)}


@router.get("/kline/{code}")
def kline(
    code: str,
    period: str = Query(default="1min", pattern="^(1min|5min|15min|30min|60min|day)$"),
    limit: int = Query(default=120, ge=10, le=500),
    session: Session = Depends(get_session),
) -> dict:
    if period == "day":
        rows = session.exec(
            select(DailyBar).where(DailyBar.code == code).order_by(DailyBar.trade_date.desc()).limit(limit)
        ).all()
        items = [
            {
                "time": r.trade_date,
                "open": r.open,
                "high": r.high,
                "low": r.low,
                "close": r.close,
                "volume": r.volume,
                "amount": r.amount,
            }
            for r in reversed(rows)
        ]
        return {"code": code, "period": period, "items": items}

    items = market_engine.kline(code, period)
    return {"code": code, "period": period, "items": items[-limit:]}


@router.get("/search")
def search(q: str = Query(min_length=1, max_length=20), session: Session = Depends(get_session)) -> dict:
    """搜索：个股与指数在同一个结果集里返回，前端靠 `is_index` 区分跳转目标。"""
    kw = q.strip().upper()
    rows = session.exec(select(Stock)).all()
    hits = [
        {
            "code": s.code,
            "name": s.name,
            "industry": s.industry,
            "hotword": s.hotword,
            "is_index": False,
        }
        for s in rows
        if kw in s.name.upper() or kw in s.code or kw in s.hotword.upper() or kw in s.theme.upper()
    ]
    for m in index_engine.INDEX_LIST:
        if kw in m.name.upper() or kw in m.code.upper():
            hits.insert(0, {"code": m.code, "name": m.name, "industry": "指数", "hotword": m.note, "is_index": True})
    return {"items": hits[:20]}
