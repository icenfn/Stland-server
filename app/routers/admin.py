"""管理端路由：运营总览、用户管理、风控、审计日志、系统事件、撮合统计。

仅 role=admin 可访问，全部操作写入 AuditLog。
"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlmodel import Session, func, select

from ..db import get_session
from ..engine.market import market_engine
from ..engine.session import current_session
from ..models import (
    AccountStatus,
    AuditLog,
    CashFlow,
    DailyBar,
    Order,
    OrderStatus,
    Position,
    RiskAssessment,
    Role,
    Stock,
    SystemEvent,
    Trade,
    User,
)
from ..services.deps import admin_user
from ..services.trading import settle_all

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/dashboard")
def dashboard(session: Session = Depends(get_session), _: User = Depends(admin_user)) -> dict:
    users = session.exec(select(User)).all()
    orders = session.exec(select(Order)).all()
    trades = session.exec(select(Trade)).all()
    positions = session.exec(select(Position)).all()
    today_start = time.time() - 86400

    total_asset = sum(u.total_asset or (u.cash + u.market_value) for u in users)
    today_orders = [o for o in orders if o.created_at >= today_start]
    today_trades = [t for t in trades if t.created_at >= today_start]
    turnover = sum(t.amount for t in today_trades)

    st = current_session()
    return {
        "kpi": {
            "user_count": len(users),
            "active_users": sum(1 for u in users if u.status == AccountStatus.ACTIVE),
            "frozen_users": sum(1 for u in users if u.status == AccountStatus.FROZEN),
            "total_asset": round(total_asset, 2),
            "today_orders": len(today_orders),
            "today_trades": len(today_trades),
            "today_turnover": round(turnover, 2),
            "open_orders": sum(1 for o in orders if o.status in (OrderStatus.PENDING, OrderStatus.PARTIAL)),
            "position_rows": len(positions),
        },
        "market": market_engine.stats(),
        "session": {
            "phase": st.phase,
            "label": st.label,
            "can_trade": st.can_trade,
            "trade_date": st.trade_date,
        },
        "role_distribution": {
            r: sum(1 for u in users if u.role.value == r) for r in ("retail", "institution", "admin")
        },
    }


@router.get("/users")
def list_users(
    keyword: str = Query(default="", max_length=20),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
    _: User = Depends(admin_user),
) -> dict:
    rows = session.exec(select(User).limit(limit)).all()
    items = []
    for u in rows:
        if keyword and keyword not in u.username and keyword not in (u.display_name or ""):
            continue
        items.append(
            {
                "id": u.id,
                "username": u.username,
                "display_name": u.display_name or u.username,
                "role": u.role.value,
                "status": u.status.value,
                "cash": round(u.cash, 2),
                "frozen_cash": round(u.frozen_cash, 2),
                "market_value": round(u.market_value, 2),
                "total_asset": round(u.total_asset, 2),
                "realized_pnl": round(u.realized_pnl, 2),
                "risk_level": u.risk_level,
                "last_login_at": u.last_login_at,
                "created_at": u.created_at,
            }
        )
    return {"total": len(items), "items": items}


class UserStatusIn(BaseModel):
    status: AccountStatus


@router.post("/users/{user_id}/status")
def update_user_status(
    user_id: int,
    payload: UserStatusIn,
    request: Request,
    session: Session = Depends(get_session),
    admin: User = Depends(admin_user),
) -> dict:
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(404, detail={"code": "USER_NOT_FOUND", "message": "用户不存在"})
    if target.role.value == "admin" and payload.status != AccountStatus.ACTIVE:
        raise HTTPException(400, detail={"code": "PROTECTED", "message": "不允许冻结管理员账户"})
    target.status = payload.status
    target.updated_at = time.time()
    session.add(target)
    session.add(
        AuditLog(
            user_id=admin.id,
            username=admin.username,
            action="user_status",
            detail=f"{target.username} -> {payload.status.value}",
            ip=request.client.host if request.client else "",
        )
    )
    session.commit()
    return {"ok": True, "status": target.status.value}


class AdjustIn(BaseModel):
    amount: float = Field(description="正数入金，负数出金")
    remark: str = Field(default="", max_length=80)


@router.post("/users/{user_id}/adjust")
def adjust_cash(
    user_id: int,
    payload: AdjustIn,
    request: Request,
    session: Session = Depends(get_session),
    admin: User = Depends(admin_user),
) -> dict:
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(404, detail={"code": "USER_NOT_FOUND", "message": "用户不存在"})
    before = target.cash
    new_cash = round(target.cash + payload.amount, 4)
    if new_cash < 0:
        raise HTTPException(400, detail={"code": "INVALID_AMOUNT", "message": "调整后可用资金不能为负"})
    target.cash = new_cash
    target.total_asset = round(target.cash + target.frozen_cash + target.market_value, 4)
    target.updated_at = time.time()
    session.add(target)
    session.add(
        CashFlow(
            user_id=target.id or 0,
            biz_type="adjust",
            amount=round(payload.amount, 4),
            balance_before=round(before, 4),
            balance_after=new_cash,
            remark=payload.remark or "管理端资金调整",
        )
    )
    session.add(
        AuditLog(
            user_id=admin.id,
            username=admin.username,
            action="adjust_cash",
            detail=f"{target.username} {payload.amount:+.2f}",
            ip=request.client.host if request.client else "",
        )
    )
    session.commit()
    return {"ok": True, "cash": target.cash, "total_asset": target.total_asset}


@router.get("/stocks")
def list_stocks(session: Session = Depends(get_session), _: User = Depends(admin_user)) -> dict:
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
                **(quotes.get(s.code) or {}),
            }
            for s in rows
        ]
    }


@router.get("/audit")
def audit_logs(
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
    _: User = Depends(admin_user),
) -> dict:
    rows = session.exec(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)).all()
    return {
        "items": [
            {
                "id": r.id,
                "username": r.username,
                "action": r.action,
                "detail": r.detail,
                "ip": r.ip,
                "created_at": r.created_at,
            }
            for r in rows
        ]
    }


@router.get("/events")
def events(
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
    _: User = Depends(admin_user),
) -> dict:
    rows = session.exec(select(SystemEvent).order_by(SystemEvent.created_at.desc()).limit(limit)).all()
    return {
        "items": [
            {"kind": r.kind, "message": r.message, "created_at": r.created_at} for r in rows
        ]
    }


@router.get("/matching")
def matching_stats(session: Session = Depends(get_session), _: User = Depends(admin_user)) -> dict:
    """撮合撮成率、平均单笔金额、各标的成交分布——运营侧观察市场质量。"""
    orders = session.exec(select(Order)).all()
    trades = session.exec(select(Trade)).all()
    filled = sum(1 for o in orders if o.status == OrderStatus.FILLED)
    cancelled = sum(1 for o in orders if o.status == OrderStatus.CANCELLED)
    by_code: dict[str, dict] = {}
    for t in trades:
        b = by_code.setdefault(t.code, {"code": t.code, "trades": 0, "amount": 0.0, "volume": 0})
        b["trades"] += 1
        b["amount"] += t.amount
        b["volume"] += t.quantity

    stocks = {s.code: s.name for s in session.exec(select(Stock)).all()}
    top = sorted(by_code.values(), key=lambda x: x["amount"], reverse=True)[:10]
    for b in top:
        b["name"] = stocks.get(b["code"], b["code"])
        b["amount"] = round(b["amount"], 2)

    avg_amount = round(sum(t.amount for t in trades) / len(trades), 2) if trades else 0.0
    return {
        "order_count": len(orders),
        "trade_count": len(trades),
        "fill_rate_pct": round(filled / len(orders) * 100, 2) if orders else 0.0,
        "cancel_rate_pct": round(cancelled / len(orders) * 100, 2) if orders else 0.0,
        "avg_trade_amount": avg_amount,
        "top_symbols": top,
    }


@router.post("/settle")
def settle(request: Request, session: Session = Depends(get_session), admin: User = Depends(admin_user)) -> dict:
    """执行日终结转（T+1 解锁）+ 记录系统事件。"""
    settle_all(session)
    session.add(
        SystemEvent(kind="settle", message=f"管理员 {admin.username} 触发日终结转")
    )
    session.add(
        AuditLog(
            user_id=admin.id,
            username=admin.username,
            action="settle",
            detail="日终结转",
            ip=request.client.host if request.client else "",
        )
    )
    session.commit()
    return {"ok": True, "at": int(time.time())}


@router.get("/leaderboard")
def leaderboard(session: Session = Depends(get_session), _: User = Depends(admin_user)) -> dict:
    """按总资产收益率排名（以初始资金为基准估算）。"""
    rows = session.exec(select(User).where(User.role != "admin" if False else User.id > 0)).all()
    items = [
        {
            "username": u.username,
            "display_name": u.display_name or u.username,
            "role": u.role.value,
            "total_asset": round(u.total_asset, 2),
            "realized_pnl": round(u.realized_pnl, 2),
            "return_pct": round(u.realized_pnl / u.total_asset * 100, 2) if u.total_asset else 0.0,
        }
        for u in rows
        if u.role.value != "admin"
    ]
    items.sort(key=lambda x: x["total_asset"], reverse=True)
    return {"items": items}

class StockIn(BaseModel):
    """新增 / 修改标的的入参。仅覆盖运营真正会调的字段。"""

    code: str = Field(max_length=12, description="6 位 A 股代码")
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


LIMIT_BY_BOARD = {"主板": 0.10, "创业板": 0.20, "科创板": 0.20, "北交所": 0.30}


def _apply(stock: Stock, payload: StockIn) -> Stock:
    """把入参写进 Stock 行，并同步涨跌停与价格格子。"""
    pct = LIMIT_BY_BOARD.get(payload.board, 0.10)
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


def _register(stock: Stock) -> None:
    """把库内标的同步进内存行情引擎（新增与修改都要走一遍）。"""
    pct = LIMIT_BY_BOARD.get(stock.board, 0.10)
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
    request: Request,
    session: Session = Depends(get_session),
    admin: User = Depends(admin_user),
) -> dict:
    """新增标的：写库 + 注册到行情引擎，立即可以交易。

    内置的 28 支只是「默认标的」，运营可以随时增删改，不再被硬编码锁死。
    """
    if session.get(Stock, payload.code) is not None:
        raise HTTPException(400, detail={"code": "DUP_CODE", "message": f"标的 {payload.code} 已存在"})
    stock = _apply(Stock(code=payload.code, tick_size=0.01, lot_size=100), payload)
    session.add(stock)
    session.add(
        AuditLog(
            user_id=admin.id,
            username=admin.username,
            action="stock_create",
            detail=f"{payload.code} {payload.name}",
            ip=request.client.host if request.client else "",
        )
    )
    session.commit()
    _register(stock)
    return {"ok": True, "code": stock.code}


@router.put("/stocks/{code}")
def update_stock(
    code: str,
    payload: StockIn,
    request: Request,
    session: Session = Depends(get_session),
    admin: User = Depends(admin_user),
) -> dict:
    """修改标的参数（改名、换板块、调波动率等），改完立即在引擎生效。"""
    stock = session.get(Stock, code)
    if stock is None:
        raise HTTPException(404, detail={"code": "STOCK_NOT_FOUND", "message": "标的不存在"})
    if payload.code != code:
        raise HTTPException(400, detail={"code": "CODE_IMMUTABLE", "message": "代码不可修改，如需换码请删除后新增"})
    _apply(stock, payload)
    session.add(stock)
    session.add(
        AuditLog(
            user_id=admin.id,
            username=admin.username,
            action="stock_update",
            detail=f"{code} {payload.name}",
            ip=request.client.host if request.client else "",
        )
    )
    session.commit()
    # 引擎里已存在该代码时 register 会覆盖其状态，等价于「热更新参数」
    _register(stock)
    return {"ok": True, "code": code}


@router.delete("/stocks/{code}")
def delete_stock(
    code: str,
    request: Request,
    session: Session = Depends(get_session),
    admin: User = Depends(admin_user),
) -> dict:
    """下线标的：删库内记录 + 从行情引擎摘除。

    记一条 SystemEvent 表示「运营主动删除」，bootstrap 看到它就不会在
    下次启动时把这条种子数据又加回来。
    """
    stock = session.get(Stock, code)
    if stock is None:
        raise HTTPException(404, detail={"code": "STOCK_NOT_FOUND", "message": "标的不存在"})
    held = session.exec(select(Position).where(Position.code == code, Position.quantity > 0)).all()
    if held:
        raise HTTPException(
            400,
            detail={
                "code": "STOCK_HELD",
                "message": f"仍有 {len(held)} 个账户持有该标的，请先处理持仓后再下线",
            },
        )
    session.delete(stock)
    session.add(
        SystemEvent(kind="stock_removed", message=f"{code} 已由 {admin.username} 下线")
    )
    session.add(
        AuditLog(
            user_id=admin.id,
            username=admin.username,
            action="stock_delete",
            detail=f"{code} {stock.name}",
            ip=request.client.host if request.client else "",
        )
    )
    session.commit()
    market_engine.unregister(code)
    return {"ok": True}

# ---------------------------------------------------------------------------
# 数据初始化：从「启动时自动注入」改为「后台手动点击注入」
# ---------------------------------------------------------------------------


class SeedIn(BaseModel):
    """初始化入参：可以选择只注入其中一部分，避免重复覆盖。"""

    stocks: bool = True
    users: bool = True
    history: bool = True


@router.post("/seed")
def seed_data(
    payload: SeedIn,
    request: Request,
    session: Session = Depends(get_session),
    admin: User = Depends(admin_user),
) -> dict:
    """手动注入标准数据集（28 支标的 + 种子账户 + 历史日线）。

    为什么改成手动
    --------------
    启动时自动注入会带来三个问题：运营删掉的账户重启后复活、空库无法从零搭建、
    也无法区分「系统自带」与「运营录入」。改成后台按钮后，注入是一次
    显式、可审计的管理动作；且 ensure_* 全部幂等，重复点击不会产生副本。
    """
    from ..bootstrap import init_seed_data

    result = init_seed_data(
        include_stocks=payload.stocks,
        include_users=payload.users,
        include_history=payload.history,
    )
    session.add(
        AuditLog(
            user_id=admin.id,
            username=admin.username,
            action="seed_data",
            detail=(
                f"stocks={payload.stocks},users={payload.users},history={payload.history} "
                f"→ +{result['stocks_added']}/+{result['users_added']}/+{result['daily_bars_added']}"
            ),
            ip=request.client.host if request.client else "",
        )
    )
    session.commit()
    return {"ok": True, **result}


@router.get("/seed/status")
def seed_status(session: Session = Depends(get_session), _: User = Depends(admin_user)) -> dict:
    """初始化状态：库里已有多少标的/账户/日线，用于面板按钮的提示文案。"""
    from sqlmodel import func

    stocks = session.exec(select(func.count()).select_from(Stock)).one()
    users = session.exec(select(func.count()).select_from(User)).one()
    bars = session.exec(select(func.count()).select_from(DailyBar)).one()
    return {"stocks": stocks, "users": users, "daily_bars": bars, "seeded": stocks > 0}


# ---------------------------------------------------------------------------
# 账户：按角色分组（零售 / 机构 / 管理员），三套人群口径完全分开
# ---------------------------------------------------------------------------

ROLE_GROUPS = {
    "retail": Role.RETAIL,
    "institution": Role.INSTITUTION,
    "admin": Role.ADMIN,
}


@router.get("/accounts")
def list_accounts(
    role: str = Query(default="retail", max_length=16),
    keyword: str = Query(default="", max_length=20),
    limit: int = Query(default=200, ge=1, le=500),
    session: Session = Depends(get_session),
    _: User = Depends(admin_user),
) -> dict:
    """按角色列出账户。role=retail/institution/admin，前端用三个页签分开。

    这里刻意不复用 /users：那个接口是「全量人员搜索」，而本接口是
    「按账户类型运营」——三类账户的资金口径、额度、风控规则完全不同
    （机构有 credit_limit，管理员不参与交易），分开查更贴合后台的实际用法。
    """
    if role not in ROLE_GROUPS:
        raise HTTPException(400, detail={"code": "BAD_ROLE", "message": "role 仅支持 retail/institution/admin"})
    target = ROLE_GROUPS[role]
    rows = session.exec(
        select(User).where(User.role == target).order_by(User.created_at.desc()).limit(limit)
    ).all()
    items = []
    for u in rows:
        if keyword and keyword not in u.username and keyword not in (u.display_name or ""):
            continue
        positions = session.exec(
            select(func.count()).select_from(Position).where(Position.user_id == u.id, Position.quantity > 0)
        ).one()
        items.append(
            {
                "id": u.id,
                "username": u.username,
                "display_name": u.display_name or u.username,
                "role": u.role.value,
                "status": u.status.value,
                "cash": round(u.cash, 2),
                "frozen_cash": round(u.frozen_cash, 2),
                "market_value": round(u.market_value, 2),
                "total_asset": round(u.total_asset, 2),
                "realized_pnl": round(u.realized_pnl, 2),
                "credit_limit": round(u.credit_limit, 2),
                "risk_level": u.risk_level,
                "positions": positions,
                "last_login_at": u.last_login_at,
                "created_at": u.created_at,
            }
        )
    return {"role": role, "total": len(items), "items": items}


class AccountIn(BaseModel):
    """新建账户。密码只在这里出现一次，落库即哈希。"""

    username: str = Field(min_length=2, max_length=40)
    display_name: str = Field(default="", max_length=40)
    password: str = Field(min_length=6, max_length=64)
    role: str = Field(default="retail", max_length=16)
    cash: float = Field(default=1000000.0, ge=0)
    credit_limit: float = Field(default=0.0, ge=0)


@router.post("/accounts")
def create_account(
    payload: AccountIn,
    request: Request,
    session: Session = Depends(get_session),
    admin: User = Depends(admin_user),
) -> dict:
    """新建账户：零售 / 机构 / 管理员共用同一张表，靠 role 区分。"""
    if payload.role not in ROLE_GROUPS:
        raise HTTPException(400, detail={"code": "BAD_ROLE", "message": "role 仅支持 retail/institution/admin"})
    exists = session.exec(select(User).where(User.username == payload.username)).first()
    if exists is not None:
        raise HTTPException(400, detail={"code": "DUP_USERNAME", "message": f"用户名 {payload.username} 已存在"})
    from ..security import hash_password

    user = User(
        username=payload.username,
        display_name=payload.display_name or payload.username,
        password_hash=hash_password(payload.password),
        role=ROLE_GROUPS[payload.role],
        status=AccountStatus.ACTIVE,
        cash=payload.cash,
        total_asset=payload.cash,
        credit_limit=payload.credit_limit if payload.role == "institution" else 0.0,
        risk_level=1,
    )
    session.add(user)
    session.add(
        AuditLog(
            user_id=admin.id,
            username=admin.username,
            action="account_create",
            detail=f"{payload.role}:{payload.username} 初始资金 {payload.cash:.2f}",
            ip=request.client.host if request.client else "",
        )
    )
    session.commit()
    session.refresh(user)
    return {"ok": True, "id": user.id, "username": user.username, "role": user.role.value}


@router.delete("/accounts/{user_id}")
def delete_account(
    user_id: int,
    request: Request,
    session: Session = Depends(get_session),
    admin: User = Depends(admin_user),
) -> dict:
    """删除账户：连同其持仓、挂单、流水一并清理。

    保护规则：不能删除自己，也不能删除最后一个管理员（否则后台会没人能进）。
    """
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(404, detail={"code": "USER_NOT_FOUND", "message": "账户不存在"})
    if target.id == admin.id:
        raise HTTPException(400, detail={"code": "SELF_DELETE", "message": "不能删除当前登录的管理员账户"})
    if target.role == Role.ADMIN:
        admins = session.exec(select(func.count()).select_from(User).where(User.role == Role.ADMIN)).one()
        if admins <= 1:
            raise HTTPException(400, detail={"code": "LAST_ADMIN", "message": "至少保留一个管理员账户"})

    for model in (Position, Order, Trade, CashFlow):
        for row in session.exec(select(model).where(model.user_id == user_id)).all():
            session.delete(row)
    session.add(
        AuditLog(
            user_id=admin.id,
            username=admin.username,
            action="account_delete",
            detail=f"{target.role.value}:{target.username}",
            ip=request.client.host if request.client else "",
        )
    )
    session.delete(target)
    session.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# 风险画像：管理端按账户查看测评历史
# ---------------------------------------------------------------------------


@router.get("/risk/{user_id}")
def risk_history(
    user_id: int,
    session: Session = Depends(get_session),
    _: User = Depends(admin_user),
) -> dict:
    """查看某个账户的风险测评历史（新的在前）。

    风评结果是适当性管理的凭证，后台必须能追溯到「谁、何时、答了什么、得几分」，
    因此这里返回全部档案而不是只返回当前等级。
    """
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(404, detail={"code": "USER_NOT_FOUND", "message": "账户不存在"})
    rows = session.exec(
        select(RiskAssessment)
        .where(RiskAssessment.user_id == user_id)
        .order_by(RiskAssessment.assessed_at.desc())
    ).all()
    history = []
    for r in rows:
        try:
            answers = json.loads(r.answers or "[]")
        except Exception:
            answers = []
        history.append(
            {
                "id": r.id,
                "version": r.version,
                "score": r.score,
                "level": r.level,
                "answers": answers,
                "source": r.source,
                "assessed_at": r.assessed_at,
            }
        )
    return {
        "user_id": user_id,
        "username": target.username,
        "risk_level": target.risk_level,
        "assessed": bool(history),
        "history_count": len(history),
        "history": history,
    }


@router.get("/risk-summary")
def risk_summary(session: Session = Depends(get_session), _: User = Depends(admin_user)) -> dict:
    """风险等级分布：多少账户未测评、多少是保守/稳健/进取。"""
    rows = session.exec(select(User).where(User.role == Role.RETAIL)).all()
    assessed_ids = {
        r.user_id for r in session.exec(select(RiskAssessment)).all()
    }
    dist = {"unassessed": 0, "conservative": 0, "balanced": 0, "aggressive": 0}
    for u in rows:
        if u.id not in assessed_ids:
            dist["unassessed"] += 1
        elif u.risk_level <= 1:
            dist["conservative"] += 1
        elif u.risk_level == 2:
            dist["balanced"] += 1
        else:
            dist["aggressive"] += 1
    return {"total_retail": len(rows), **dist}
