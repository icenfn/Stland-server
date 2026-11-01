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
from ..engine import orderbook
from ..engine.market import market_engine
from ..engine.periods import LONG_PERIODS, aggregate_daily
from ..engine.session import current_session
from ..models import DailyBar, Stock, TradeSource
from ..services.deps import optional_signature

router = APIRouter(prefix="/api/market", tags=["market"])


def _with_today(code: str, daily: list[dict]) -> list[dict]:
    """把「今天」这根实时日线接到日线序列末尾。

    ``daily_bar`` 只在跨日时落库，所以收盘前今天的柱子并不在库里。
    不补它，用户当天成交了却看不到今天的 K 线 —— 图停在昨天，
    看起来像行情没跟上交易。已经落库过（同一交易日）就不重复追加。
    """
    bar = market_engine.today_bar(code)
    if bar is None:
        return daily
    if daily and daily[-1].get("time") == bar["time"]:
        daily[-1] = bar
        return daily
    return daily + [bar]


def _with_index_today(quote: dict, daily: list[dict]) -> list[dict]:
    """给指数日线补上当日这根实时柱。

    指数点位由成分实时合成，收盘前同样不存在于 ``daily_bar``（它存的是个股日线）。
    统一在接口层补，指数与个股的日线口径就一致了。
    """
    if not quote.get("has_data") or not quote.get("price"):
        return daily
    today = current_session().trade_date
    bar = {
        "time": today,
        "open": quote.get("open") or quote["price"],
        "high": quote.get("high") or quote["price"],
        "low": quote.get("low") or quote["price"],
        "close": quote["price"],
        "volume": int(quote.get("volume") or 0),
        "amount": round(float(quote.get("amount") or 0.0), 2),
    }
    if daily and daily[-1].get("time") == today:
        daily[-1] = bar
        return daily
    return daily + [bar]


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
        # 完整时钟快照：包含结算模式（T+1/T+0/调试）、时段预设、粗粒度 segment
        # 与两个能力位，客户端状态栏一次拿全，不必再按 phase 硬编码文案。
        "session": st.to_dict(),
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
def index_minutes(
    code: str,
    limit: int = Query(default=240, ge=1, le=1440),
    days: int = Query(default=1, ge=1, le=5),
    session: Session = Depends(get_session),
) -> dict:
    """指数分时。days=5 时由成分股的五日分钟序列等权合成。"""
    meta = index_engine.get_index(code)
    return {"code": meta.code, "items": index_engine.index_minutes(session, meta, limit=limit, days=days)}


@router.get("/index/{code}/kline")
def index_kline(
    code: str,
    period: str = Query(
        default="1min",
        pattern="^(1min|5min|15min|30min|60min|120min|day|week|month|quarter|year)$",
    ),
    limit: int = Query(default=120, ge=10, le=500),
    session: Session = Depends(get_session),
) -> dict:
    """指数 K 线。日线由成分股日线等权合成，分钟级由合成分时聚合。"""
    meta = index_engine.get_index(code)
    # 周/月/季/年：取日线后按自然周期聚合，不必给指数单独维护长周期表
    if period in LONG_PERIODS:
        daily = _with_index_today(
            index_engine.index_quote(session, meta),
            index_engine.index_daily(session, meta, limit=limit * 40),
        )
        if not daily:
            daily = index_engine.index_kline(session, meta, period="60min", limit=limit)
        return {"code": meta.code, "period": period, "items": aggregate_daily(daily, period)[-limit:]}

    if period == "day":
        items = _with_index_today(
            index_engine.index_quote(session, meta),
            index_engine.index_daily(session, meta, limit=limit),
        )
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
def depth(code: str, session: Session = Depends(get_session)) -> dict:
    """五档盘口：默认只显示**真实用户挂单**。

    原来这里直接返回 market_engine.depth()，那是 tick 里用随机数造出来的
    档位 —— 前端看到的买卖五档没有任何一个用户真实挂过单。用户据此下单，
    成交结果自然对不上，这才是「关掉引擎就露馅」的根因之一。

    现在改为聚合 orderbook 里真实的 resting 委托：
      · 买盘按价格从高到低，卖盘从低到高（与行情软件一致）；
      · 没有挂单时返回空数组，前端显示「无挂单」而不是假档位；
      · 仅当显式开启 hybrid（模拟做市商）时，才在空档位上补模拟盘口，
        并标 source=simulated，让前端与调用方都能分辨。
    """
    quote = market_engine.quote(code)
    if quote is None and not market_engine.has(code):
        raise HTTPException(404, detail={"code": "UNKNOWN_SYMBOL", "message": "标的不存在"})

    bids = orderbook.quote_levels(session, code, "buy", limit=5)
    asks = orderbook.quote_levels(session, code, "sell", limit=5)
    source = "real"

    # 兼容演示盘：仅 hybrid 模式且真实档位为空时，补模拟盘口
    from ..config import settings as _settings

    if not bids and not asks and _settings.match_mode == "hybrid":
        sim = market_engine.depth(code) or {}
        bids = [{"price": l["price"], "volume": l["volume"]} for l in sim.get("bids", [])]
        asks = [{"price": l["price"], "volume": l["volume"]} for l in sim.get("asks", [])]
        source = "simulated"

    return {
        "code": code,
        "bids": bids,
        "asks": asks,
        # 让调用方知道这份盘口是用户真实挂单还是模拟兜底，别再混着用
        "source": source,
    }


@router.get("/minutes/{code}")
def minutes(
    code: str,
    limit: int = Query(default=240, ge=1, le=1440),
    days: int = Query(default=1, ge=1, le=5),
) -> dict:
    """分时序列。days=1 只看当天；days=5 取最近五个自然日首尾相接。"""
    return {"code": code, "items": market_engine.minute_series(code, limit, days=days)}


@router.get("/kline/{code}")
def kline(
    code: str,
    period: str = Query(
        default="1min",
        pattern="^(1min|5min|15min|30min|60min|120min|day|week|month|quarter|year)$",
    ),
    limit: int = Query(default=120, ge=10, le=500),
    include_demo: bool = Query(
        default=False,
        description="是否包含模拟/回填数据。默认 false：只返回真实用户成交形成的 K 线",
    ),
    session: Session = Depends(get_session),
) -> dict:
    # 周/月/季/年不落库：读日线后按自然周期聚合（纯函数，见 engine/periods.py），
    # 交易日历变化不需要回补历史，也不必多出四张要与日线对齐的冗余表
    if period in LONG_PERIODS:
        stmt = select(DailyBar).where(DailyBar.code == code)
        if not include_demo:
            # 默认只给真实成交：初始化回填的随机日线与做市商成交量，
            # 混进来的话图表形状是假的，指标（均线、涨幅）全不可信
            stmt = stmt.where(DailyBar.source == TradeSource.REAL)
        rows = session.exec(
            stmt.order_by(DailyBar.trade_date.desc()).limit(limit * 40)
        ).all()
        daily = [
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
        daily = _with_today(code, daily)
        return {"code": code, "period": period, "items": aggregate_daily(daily, period)[-limit:]}

    if period == "day":
        stmt = select(DailyBar).where(DailyBar.code == code)
        if not include_demo:
            stmt = stmt.where(DailyBar.source == TradeSource.REAL)
        rows = session.exec(
            stmt.order_by(DailyBar.trade_date.desc()).limit(limit)
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
        return {"code": code, "period": period, "items": _with_today(code, items)[-limit:]}

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
