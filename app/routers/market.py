"""行情路由：列表、详情、五档、分时、K 线、日线、指数、搜索。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlmodel import Session, select

from ..db import get_session
from ..engine.market import market_engine
from ..engine.session import current_session
from ..models import DailyBar, Stock, User
from ..services.deps import current_user, signed_request

router = APIRouter(prefix="/api/market", tags=["market"])


@router.get("/overview")
def overview(_: None = Depends(signed_request)) -> dict:
    """首页聚合：指数、涨跌分布、榜单、交易时段。"""
    quotes = list(market_engine.quotes().values())
    stats = market_engine.stats()
    gainers = sorted(quotes, key=lambda q: q["change_pct"], reverse=True)[:5]
    losers = sorted(quotes, key=lambda q: q["change_pct"])[:5]
    active = sorted(quotes, key=lambda q: q["amount"], reverse=True)[:5]
    st = current_session()
    return {
        "index": market_engine.index_quote(),
        "index_minutes": market_engine.index_minutes()[-120:],
        "stats": stats,
        "session": {
            "phase": st.phase,
            "label": st.label,
            "is_open": st.is_open,
            "can_trade": st.can_trade,
            "trade_date": st.trade_date,
        },
        "rank": {
            "gainers": gainers,
            "losers": losers,
            "active": active,
        },
    }


@router.get("/list")
def list_quotes(
    request: Request,
    session: Session = Depends(get_session),
    sort: str = Query(default="change_pct", pattern="^(change_pct|amount|turnover_rate|price|code)$"),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    keyword: str = Query(default="", max_length=20),
    industry: str = Query(default="", max_length=20),
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
        if keyword and keyword not in meta.name and keyword not in code:
            continue
        rows.append(
            {
                **q,
                "industry": meta.industry,
                "board": meta.board,
                "hotword": meta.hotword,
            }
        )
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
        b = buckets.setdefault(s.industry or "其他", {"industry": s.industry or "其他", "count": 0, "change_sum": 0.0})
        b["count"] += 1
        b["change_sum"] += q["change_pct"]
    items = [
        {"industry": b["industry"], "count": b["count"], "change_pct": round(b["change_sum"] / b["count"], 2)}
        for b in buckets.values()
    ]
    items.sort(key=lambda x: x["change_pct"], reverse=True)
    return {"items": items}


@router.get("/stock/{code}")
def stock_detail(
    code: str, request: Request, session: Session = Depends(get_session)
) -> dict:
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
        },
    }


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


@router.get("/index")
def index() -> dict:
    return {
        "quote": market_engine.index_quote(),
        "minutes": market_engine.index_minutes(),
        "stats": market_engine.stats(),
    }


@router.get("/search")
def search(q: str = Query(min_length=1, max_length=20), session: Session = Depends(get_session)) -> dict:
    rows = session.exec(select(Stock)).all()
    kw = q.strip()
    hits = [
        {"code": s.code, "name": s.name, "industry": s.industry, "hotword": s.hotword}
        for s in rows
        if kw in s.name or kw in s.code or kw in s.hotword or kw in s.theme
    ][:20]
    return {"items": hits}
