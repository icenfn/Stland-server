"""指数计算：让指数和个股一样有代码、能查详情。

指数清单里每个指数：
* 有稳定的 8 位代码（`SL` 前缀 + 6 位数字），与个股代码进入同一套搜索与路由；
* 由成分股按**等权**归一化价格合成（`Σ(price_i / prev_close_i) / n × 基准点位`）。
  等权而非市值加权：模拟盘标的的股本参数差异极大，市值加权会让单只标的过度主导；
* 分时/K 线由成分股分钟线同法合成，不额外留存时序数据。

**没有成分股就没有指数。** 基准点位只是合成时的缩放系数，不是「当前的指数点位」。
空库时若拿基准点位兜底，后台还没注入任何标的前端就会显示一个像模像样的点位，
看起来像行情已经跑起来了 —— 所以此处的约定是：`has_data=False` 且点位为 `None`，
由调用方决定展示「--」还是空状态。
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlmodel import Session, select

from ..models import DailyBar, Stock
from .market import market_engine


class IndexMeta:
    """指数定义：代码、名称、基准点位、成分筛选规则。"""

    def __init__(
        self,
        code: str,
        name: str,
        base: float,
        board: str = "",
        industry: str = "",
        note: str = "",
    ) -> None:
        self.code = code
        self.name = name
        self.base = base
        self.board = board
        self.industry = industry
        self.note = note

    def matches(self, stock: Stock) -> bool:
        if self.board and stock.board != self.board:
            return False
        if self.industry and stock.industry != self.industry:
            return False
        return True


# 指数清单：综合 + 三个板块指数。代码与个股同一套编码空间（SL 前缀表示指数）
INDEX_LIST: tuple[IndexMeta, ...] = (
    IndexMeta("SL000001", "股岛综合指数", 3200.0, note="全部在售标的等权合成"),
    IndexMeta("SL000002", "股岛主板指数", 4100.0, board="主板", note="主板成分等权合成"),
    IndexMeta("SL000003", "股岛创业板指数", 2350.0, board="创业板", note="创业板成分等权合成"),
    IndexMeta("SL000004", "股岛科创板指数", 1580.0, board="科创板", note="科创板成分等权合成"),
)

INDEX_MAP: dict[str, IndexMeta] = {i.code: i for i in INDEX_LIST}


def is_index(code: str) -> bool:
    return code.upper().startswith("SL")


def get_index(code: str) -> IndexMeta:
    meta = INDEX_MAP.get(code.upper())
    if meta is None:
        raise HTTPException(404, detail={"code": "UNKNOWN_INDEX", "message": "指数不存在"})
    return meta


def _members(session: Session, meta: IndexMeta) -> list[Stock]:
    """取出该指数的成分标的（只保留已在行情引擎注册、能报价的）。"""
    rows = [s for s in session.exec(select(Stock)).all() if meta.matches(s)]
    live = set(market_engine.all_codes())
    return [s for s in rows if s.code in live]


def index_quote(session: Session, meta: IndexMeta) -> dict:
    """指数实时快照：点位、涨跌、振幅、成交额、上涨/下跌家数。"""
    members = _members(session, meta)
    quotes = market_engine.quotes()

    ratios: list[float] = []
    opens: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    amount = 0.0
    volume = 0.0
    up = down = flat = 0
    for s in members:
        q = quotes.get(s.code)
        if not q or float(q.get("prev_close") or 0) <= 0:
            continue
        prev = float(q["prev_close"])
        ratios.append(float(q["price"]) / prev)
        # 开高低同样按「对昨收的比值」合成，这样与点位同一口径，
        # 不会出现最高价比当前点位还低的矛盾数据。
        opens.append(float(q.get("open") or prev) / prev)
        highs.append(float(q.get("high") or prev) / prev)
        lows.append(float(q.get("low") or prev) / prev)
        amount += float(q.get("amount") or 0.0)
        volume += float(q.get("volume") or 0.0)
        chg = float(q.get("change_pct") or 0.0)
        if chg > 0:
            up += 1
        elif chg < 0:
            down += 1
        else:
            flat += 1

    if not ratios:
        # 没有任何可用成分报价 → 指数不存在点位，字段留空让前端显示「--」。
        # 不能退回 meta.base：那会让空库看起来已经有行情。
        return {
            "code": meta.code,
            "name": meta.name,
            "price": None,
            "prev_close": None,
            "open": None,
            "high": None,
            "low": None,
            "change": None,
            "change_pct": None,
            "amount": 0.0,
            "volume": 0,
            "up": 0,
            "down": 0,
            "flat": 0,
            "members": 0,
            "has_data": False,
            "is_index": True,
        }

    n = len(ratios)
    level = sum(ratios) / n * meta.base
    return {
        "code": meta.code,
        "name": meta.name,
        "price": round(level, 2),
        "prev_close": meta.base,
        "open": round(sum(opens) / n * meta.base, 2),
        "high": round(sum(highs) / n * meta.base, 2),
        "low": round(sum(lows) / n * meta.base, 2),
        "change": round(level - meta.base, 2),
        "change_pct": round((level / meta.base - 1) * 100, 2),
        "amount": round(amount, 2),
        "volume": int(volume),
        "up": up,
        "down": down,
        "flat": flat,
        "members": n,
        "has_data": True,
        "is_index": True,
    }


def index_minutes(session: Session, meta: IndexMeta, limit: int = 240, days: int = 1) -> list[dict]:
    """指数分时：把各成分的分钟收盘价按「对昨收的比值」等权平均后还原成点位。

    这样合成的分时与 `index_quote` 的口径一致 —— 同一个时刻，分时末点
    与当前点位是对得上的，不会出现「图上最后一根和上面数字不符」。

    days 必须一路透传下去：路由 `/index/{code}/minutes` 支持 days=1/5，
    而成分取数走的是与个股同一个 `minute_series(code, limit, days)`。
    上一版这里少了 days 形参，路由把 days 传进来会直接 TypeError（500），
    前端拿不到任何点，指数分时于是永远是一片空白。
    """
    members = _members(session, meta)
    quotes = market_engine.quotes()
    series: list[list[tuple[int, float, float]]] = []
    for s in members:
        q = quotes.get(s.code) or {}
        prev = float(q.get("prev_close") or 0)
        if prev <= 0:
            continue
        bars = market_engine.minute_series(s.code, limit, days=days)
        if not bars:
            continue
        # 第三项是该分钟的成交量：指数没有自己的成交，量能只能由成分合计得到
        series.append(
            [(int(b["t"]), float(b["price"]) / prev, float(b.get("volume") or 0.0)) for b in bars]
        )

    if not series:
        return []

    # 以最短序列为准，保证每个时间点都有全部成分的取值（避免早期点位样本不足）
    n = min(len(x) for x in series)
    out: list[dict] = []
    for i in range(n):
        ts = series[0][i][0]
        ratio = sum(x[i][1] for x in series) / len(series)
        vol = sum(x[i][2] for x in series)
        out.append({"t": ts, "price": round(ratio * meta.base, 2), "volume": int(vol)})
    return out


def index_kline(session: Session, meta: IndexMeta, period: str = "1min", limit: int = 120) -> list[dict]:
    """指数 K 线：由合成后的分时序列再按周期聚合（指数没有真实成交，量用成分合计）。"""
    minutes = index_minutes(session, meta, limit=1440)
    if not minutes:
        return []
    step = {"1min": 1, "5min": 5, "15min": 15, "30min": 30, "60min": 60, "120min": 120}.get(period, 1)
    bars: list[dict] = []
    for i in range(0, len(minutes), step):
        chunk = minutes[i : i + step]
        if not chunk:
            continue
        prices = [c["price"] for c in chunk]
        bars.append(
            {
                "time": chunk[0]["t"],
                "open": prices[0],
                "high": max(prices),
                "low": min(prices),
                "close": prices[-1],
                # 量能随周期累加，否则成交量副图对指数恒为一条 0 轴
                "volume": int(sum(float(c.get("volume") or 0.0) for c in chunk)),
                "amount": 0.0,
            }
        )
    return bars[-limit:]


def index_daily(session: Session, meta: IndexMeta, limit: int = 120) -> list[dict]:
    """指数日线：由成分股日线等权合成。

    做法与分时一致 —— 每支成分先按「对首日收盘的比值」归一化，再取均值乘基准点位，
    这样不同价的成分不会被高价股主导。量额直接取成分合计。
    """
    members = _members(session, meta)
    if not members:
        return []
    codes = [s.code for s in members]
    rows = session.exec(
        select(DailyBar).where(DailyBar.code.in_(codes)).order_by(DailyBar.trade_date)
    ).all()
    if not rows:
        return []

    by_code: dict[str, dict[str, DailyBar]] = {}
    for r in rows:
        by_code.setdefault(r.code, {})[r.trade_date] = r

    base_close: dict[str, float] = {}
    for code, days in by_code.items():
        first = days[min(days)]
        if first.close > 0:
            base_close[code] = first.close

    dates = sorted({r.trade_date for r in rows})
    bars: list[dict] = []
    for d in dates:
        ratios: list[float] = []
        opens: list[float] = []
        highs: list[float] = []
        lows: list[float] = []
        volume = 0
        amount = 0.0
        for code, base in base_close.items():
            bar = by_code[code].get(d)
            if bar is None or bar.close <= 0:
                continue
            ratios.append(bar.close / base)
            opens.append((bar.open or bar.close) / base)
            highs.append((bar.high or bar.close) / base)
            lows.append((bar.low or bar.close) / base)
            volume += int(bar.volume or 0)
            amount += float(bar.amount or 0.0)
        if not ratios:
            continue
        k = len(ratios)
        bars.append(
            {
                "time": d,
                "open": round(sum(opens) / k * meta.base, 2),
                "high": round(sum(highs) / k * meta.base, 2),
                "low": round(sum(lows) / k * meta.base, 2),
                "close": round(sum(ratios) / k * meta.base, 2),
                "volume": volume,
                "amount": round(amount, 2),
            }
        )
    return bars[-limit:]


def index_list(session: Session) -> list[dict]:
    """全部指数快照（首页列表、搜索都用它）。"""
    return [index_quote(session, m) for m in INDEX_LIST]
