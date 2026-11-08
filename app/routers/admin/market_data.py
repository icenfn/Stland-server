"""管理端 · 标的 / 板块管理 / 行业管理。"""
from __future__ import annotations

from ._common import *  # noqa: F401,F403
from ._common import router


# --------------------------------------------------------------------------- #
# 标的
# --------------------------------------------------------------------------- #
@router.get("/stocks")
def list_stocks(session: Session = Depends(get_session), admin: Admin = Depends(admin_user)) -> dict:
    rows = session.exec(select(Stock)).all()
    quotes = market_engine.quotes()
    return {
        "items": [
            {
                "code": s.code,
                "name": s.name,
                "board": s.board,
                "industry": s.industry,
                "hotword": s.hotword,
                "limit_up": s.limit_up,
                "limit_down": s.limit_down,
                "prev_close": s.prev_close,
                "vol": s.vol,
                "beta": s.beta,
                "liquidity": s.liquidity,
                **(quotes.get(s.code) or {}),
            }
            for s in rows
        ]
    }


@router.get("/indices")
def list_indices(session: Session = Depends(get_session), admin: Admin = Depends(admin_user)) -> dict:
    """指数列表（只读）。指数由成分股合成，不可手工新建。"""
    return {"items": index_engine.index_list(session)}


class StockIn(BaseModel):
    code: str = Field(max_length=12)
    name: str = Field(max_length=24)
    board: str = Field(default="主板", max_length=12)
    industry: str = Field(default="", max_length=24)
    hotword: str = Field(default="", max_length=40)
    theme: str = Field(default="", max_length=80)
    init_price: float = Field(default=10.0, gt=0)
    vol: float = Field(default=0.35, gt=0, le=3.0)
    beta: float = Field(default=1.0, gt=0, le=5.0)
    drift: float = Field(default=0.0, ge=-0.5, le=0.5)
    liquidity: float = Field(default=1.0, gt=0, le=10.0)
    float_shares: float = Field(default=5e8, gt=0)


def _board_rules(session: Session, name: str) -> tuple[float, float, int]:
    """取板块规则 (涨跌幅上限, 最小报价单位, 申报单位)。

    板块规则是**数据**而不是常量：运营在「板块管理」里改一次，
    新建与编辑标的时立刻生效，不必改代码重新发布。
    板块不存在时回落主板口径（±10%、0.01、100 股），
    避免因为漏建板块而凭空造出「不限涨跌」的标的。
    """
    row = session.exec(select(Board).where(Board.name == name)).first()
    if row is None:
        return 0.10, 0.01, 100
    return row.limit_pct, row.tick_size, row.lot_size


def _apply(stock: Stock, payload: StockIn, session: Session) -> Stock:
    pct, tick_size, lot_size = _board_rules(session, payload.board)
    stock.tick_size = tick_size
    stock.lot_size = lot_size
    stock.name = payload.name
    stock.board = payload.board
    stock.industry = payload.industry
    stock.hotword = payload.hotword
    stock.theme = payload.theme
    stock.init_price = payload.init_price
    stock.prev_close = payload.init_price
    stock.limit_up = round(payload.init_price * (1 + pct), 2)
    stock.limit_down = round(payload.init_price * (1 - pct), 2)
    stock.vol = payload.vol
    stock.beta = payload.beta
    stock.drift = payload.drift
    stock.liquidity = payload.liquidity
    stock.float_shares = payload.float_shares
    stock.total_shares = payload.float_shares * 1.55
    return stock


def _register(stock: Stock, session: Session) -> None:
    pct, _tick, _lot = _board_rules(session, stock.board)
    market_engine.register(
        code=stock.code,
        name=stock.name,
        init_price=stock.prev_close or stock.init_price,
        tick_size=stock.tick_size,
        limit_up_pct=pct,
        beta=stock.beta,
        drift=stock.drift,
        vol=stock.vol,
        liquidity=stock.liquidity,
        float_shares=stock.float_shares,
    )


@router.post("/stocks")
def create_stock(
    payload: StockIn,
    session: Session = Depends(get_session),
    admin: Admin = Depends(admin_user),
) -> dict:
    if session.get(Stock, payload.code) is not None:
        raise HTTPException(400, detail={"code": "DUP_CODE", "message": f"标的 {payload.code} 已存在"})
    stock = _apply(Stock(code=payload.code, tick_size=0.01, lot_size=100), payload, session)
    session.add(stock)
    session.commit()
    _register(stock, session)
    return {"ok": True, "code": stock.code}


@router.put("/stocks/{code}")
def update_stock(
    code: str,
    payload: StockIn,
    session: Session = Depends(get_session),
    admin: Admin = Depends(admin_user),
) -> dict:
    stock = session.get(Stock, code)
    if stock is None:
        raise HTTPException(404, detail={"code": "STOCK_NOT_FOUND", "message": "标的不存在"})
    if payload.code != code:
        raise HTTPException(400, detail={"code": "CODE_IMMUTABLE", "message": "代码不可修改，如需换码请删除后新增"})
    _apply(stock, payload, session)
    session.add(stock)
    session.commit()
    _register(stock, session)
    return {"ok": True, "code": code}


@router.delete("/stocks/{code}")
def delete_stock(
    code: str,
    session: Session = Depends(get_session),
    admin: Admin = Depends(admin_user),
) -> dict:
    stock = session.get(Stock, code)
    if stock is None:
        raise HTTPException(404, detail={"code": "STOCK_NOT_FOUND", "message": "标的不存在"})
    held = session.exec(select(Position).where(Position.code == code, Position.quantity > 0)).all()
    if held:
        raise HTTPException(
            400,
            detail={"code": "STOCK_HELD", "message": f"仍有 {len(held)} 个账户持有该标的，请先处理持仓"},
        )
    # 真删除：这一步就是「这支不要了」，不做软删除、不留下架标记。
    # 先清掉依附在这支标的上的行情与自选成分 —— 委托与成交流水属于账户侧历史，
    # 保留不动，删标的不能让别人的对账记录跟着消失。
    # 只清日线快照：委托与成交流水属于账户侧历史，
    # 删标的不能让别人的对账记录跟着消失。
    for row in session.exec(select(DailyBar).where(DailyBar.code == code)).all():
        session.delete(row)
    for row in session.exec(select(WatchItem).where(WatchItem.code == code)).all():
        session.delete(row)

    session.delete(stock)
    session.commit()
    market_engine.unregister(code)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# 板块管理
# --------------------------------------------------------------------------- #
class BoardIn(BaseModel):
    """板块写入体。板块承载涨跌停与报价精度规则，这几项都是必填口径。"""

    name: str
    limit_pct: float = 0.10
    tick_size: float = 0.01
    lot_size: int = 100
    description: str = ""
    sort: int = 0


def _board_counts(session: Session) -> dict[str, int]:
    """每个板块下的标的数。板块规则一改会影响哪些标的，列表上要能看清。"""
    counts: dict[str, int] = {}
    for st in session.exec(select(Stock)).all():
        counts[st.board] = counts.get(st.board, 0) + 1
    return counts


@router.get("/boards")
def list_boards(session: Session = Depends(get_session), admin: Admin = Depends(admin_user)) -> dict:
    counts = _board_counts(session)
    rows = session.exec(select(Board).order_by(Board.sort, Board.name)).all()
    return {
        "items": [
            {
                "name": b.name,
                "limit_pct": b.limit_pct,
                "tick_size": b.tick_size,
                "lot_size": b.lot_size,
                "description": b.description,
                "sort": b.sort,
                "stock_count": counts.get(b.name, 0),
            }
            for b in rows
        ]
    }


@router.post("/boards")
def create_board(
    payload: BoardIn,
    session: Session = Depends(get_session),
    admin: Admin = Depends(admin_user),
) -> dict:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="板块名称不能为空")
    if not (0 < payload.limit_pct <= 1):
        raise HTTPException(status_code=400, detail="涨跌幅需在 (0, 1] 之间，如 0.10 表示 ±10%")
    if payload.tick_size <= 0 or payload.lot_size <= 0:
        raise HTTPException(status_code=400, detail="最小报价单位与申报单位必须为正数")
    if session.exec(select(Board).where(Board.name == name)).first():
        raise HTTPException(status_code=400, detail=f"板块「{name}」已存在")
    session.add(Board(**{**payload.model_dump(), "name": name}))
    session.commit()
    return {"ok": True}


@router.put("/boards/{name}")
def update_board(
    name: str,
    payload: BoardIn,
    session: Session = Depends(get_session),
    admin: Admin = Depends(admin_user),
) -> dict:
    row = session.exec(select(Board).where(Board.name == name)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="板块不存在")
    if not (0 < payload.limit_pct <= 1):
        raise HTTPException(status_code=400, detail="涨跌幅需在 (0, 1] 之间")
    new_name = payload.name.strip() or name
    if new_name != name:
        if session.exec(select(Board).where(Board.name == new_name)).first():
            raise HTTPException(status_code=400, detail=f"板块「{new_name}」已存在")
        # 改名必须同步标的归属：否则这批标的会挂在一个不存在的板块上，
        # 涨跌停推算回落到主板口径，涨跌幅会突然从 ±20% 变成 ±10%。
        for st in session.exec(select(Stock).where(Stock.board == name)).all():
            st.board = new_name
            session.add(st)
        row.name = new_name
    row.limit_pct = payload.limit_pct
    row.tick_size = payload.tick_size
    row.lot_size = payload.lot_size
    row.description = payload.description
    row.sort = payload.sort
    session.add(row)
    session.commit()
    return {"ok": True}


@router.delete("/boards/{name}")
def delete_board(
    name: str,
    session: Session = Depends(get_session),
    admin: Admin = Depends(admin_user),
) -> dict:
    row = session.exec(select(Board).where(Board.name == name)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="板块不存在")
    used = session.exec(select(func.count()).select_from(Stock).where(Stock.board == name)).one()
    if used:
        raise HTTPException(status_code=400, detail=f"该板块下还有 {used} 个标的，请先调整标的归属")
    session.delete(row)
    session.commit()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# 行业管理
# --------------------------------------------------------------------------- #
class IndustryIn(BaseModel):
    name: str
    description: str = ""
    sort: int = 0


@router.get("/industries")
def list_industries(session: Session = Depends(get_session), admin: Admin = Depends(admin_user)) -> dict:
    counts: dict[str, int] = {}
    for st in session.exec(select(Stock)).all():
        counts[st.industry] = counts.get(st.industry, 0) + 1
    rows = session.exec(select(Industry).order_by(Industry.sort, Industry.name)).all()
    return {
        "items": [
            {
                "name": it.name,
                "description": it.description,
                "sort": it.sort,
                "stock_count": counts.get(it.name, 0),
            }
            for it in rows
        ]
    }


@router.post("/industries")
def create_industry(
    payload: IndustryIn,
    session: Session = Depends(get_session),
    admin: Admin = Depends(admin_user),
) -> dict:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="行业名称不能为空")
    if session.exec(select(Industry).where(Industry.name == name)).first():
        raise HTTPException(status_code=400, detail=f"行业「{name}」已存在")
    session.add(Industry(**{**payload.model_dump(), "name": name}))
    session.commit()
    return {"ok": True}


@router.put("/industries/{name}")
def update_industry(
    name: str,
    payload: IndustryIn,
    session: Session = Depends(get_session),
    admin: Admin = Depends(admin_user),
) -> dict:
    row = session.exec(select(Industry).where(Industry.name == name)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="行业不存在")
    new_name = payload.name.strip() or name
    if new_name != name:
        if session.exec(select(Industry).where(Industry.name == new_name)).first():
            raise HTTPException(status_code=400, detail=f"行业「{new_name}」已存在")
        for st in session.exec(select(Stock).where(Stock.industry == name)).all():
            st.industry = new_name
            session.add(st)
        row.name = new_name
    row.description = payload.description
    row.sort = payload.sort
    session.add(row)
    session.commit()
    return {"ok": True}


@router.delete("/industries/{name}")
def delete_industry(
    name: str,
    session: Session = Depends(get_session),
    admin: Admin = Depends(admin_user),
) -> dict:
    row = session.exec(select(Industry).where(Industry.name == name)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="行业不存在")
    used = session.exec(select(func.count()).select_from(Stock).where(Stock.industry == name)).one()
    if used:
        raise HTTPException(status_code=400, detail=f"该行业下还有 {used} 个标的，请先调整标的归属")
    session.delete(row)
    session.commit()
    return {"ok": True}
