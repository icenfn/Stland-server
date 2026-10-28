"""管理端路由：管理员登录、运营总览、配置管理、交易账户、管理员账户、标的、撮合。

本版三处结构性变化：
1. **管理员独立登录**：`/api/admin/login` 只查 `admin` 表，签发的令牌 role 固定为
   `admin`；交易账户的令牌在 `admin_user` 里会被直接拒绝。两条链路彻底隔离。
2. **不做任何记录型留痕**：审计日志（`AuditLog`）与系统事件（`SystemEvent`）两张表、
   对应接口与全部写入点都已删除。运营动作不留痕是本产品的明确取舍 —— 模拟盘由
   单人/小团队运营，留痕表只会持续吃存储，并在后台堆出一份没人看的列表。
3. **账户拆两张表展示**：交易账户（零售 + 机构，同一张 `user` 表按 role 过滤）
   与管理员（独立的 `admin` 表）分开返回，不再混在一个列表里靠前端过滤。
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import Session, func, select

from .. import runtime_config
from ..config import settings
from ..db import get_session
from ..engine import index as index_engine
from ..engine.market import market_engine
from ..engine.session import current_session
from ..models import (
    AccountStatus,
    Admin,
    CashFlow,
    DailyBar,
    Order,
    OrderStatus,
    Position,
    QuoteTick,
    Role,
    Stock,
    Trade,
    User,
    WatchGroup,
    WatchItem,
)
from ..security import hash_password, verify_password
from ..services.deps import admin_user, issue_admin_token
from ..services.trading import settle_all

router = APIRouter(prefix="/api/admin", tags=["admin"])

LOCK_SECONDS = 300


# --------------------------------------------------------------------------- #
# 管理员登录 / 身份
# --------------------------------------------------------------------------- #
class AdminLoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=64)


def admin_public(admin: Admin) -> dict:
    return {
        "id": admin.id,
        "username": admin.username,
        "display_name": admin.display_name or admin.username,
        "is_active": admin.is_active,
        "last_login_at": admin.last_login_at,
        "created_at": admin.created_at,
    }


@router.post("/login")
def admin_login(payload: AdminLoginIn, session: Session = Depends(get_session)) -> dict:
    """管理员登录：只查 `admin` 表，与交易账户完全无关。"""
    admin = session.exec(select(Admin).where(Admin.username == payload.username.lower())).first()
    if admin is None or not admin.is_active:
        raise HTTPException(401, detail={"code": "BAD_CREDENTIALS", "message": "用户名或口令错误"})

    now = time.time()
    if admin.locked_until > now:
        wait = int(admin.locked_until - now)
        raise HTTPException(
            423, detail={"code": "LOCKED", "message": f"连续失败过多，请 {wait} 秒后重试"}
        )

    if not verify_password(payload.password, admin.password_hash):
        admin.fail_count += 1
        if admin.fail_count >= settings.login_fail_limit:
            admin.locked_until = now + LOCK_SECONDS
            admin.fail_count = 0
        session.add(admin)
        session.commit()
        raise HTTPException(401, detail={"code": "BAD_CREDENTIALS", "message": "用户名或口令错误"})

    admin.fail_count = 0
    admin.locked_until = 0.0
    admin.last_login_at = now
    session.add(admin)
    session.commit()
    session.refresh(admin)
    return {"admin": admin_public(admin), "token": issue_admin_token(admin)}


@router.get("/me")
def admin_me(admin: Admin = Depends(admin_user)) -> dict:
    return {"admin": admin_public(admin)}


class PasswordIn(BaseModel):
    old_password: str = Field(min_length=1, max_length=64)
    new_password: str = Field(min_length=8, max_length=64)


@router.post("/password")
def change_password(
    payload: PasswordIn,
    session: Session = Depends(get_session),
    admin: Admin = Depends(admin_user),
) -> dict:
    """修改自己的口令。首次登录后应立即改掉引导密码。"""
    if not verify_password(payload.old_password, admin.password_hash):
        raise HTTPException(400, detail={"code": "BAD_PASSWORD", "message": "原口令错误"})
    admin.password_hash = hash_password(payload.new_password)
    admin.updated_at = time.time()
    session.add(admin)
    session.commit()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# 总览
# --------------------------------------------------------------------------- #
@router.get("/dashboard")
def dashboard(session: Session = Depends(get_session), admin: Admin = Depends(admin_user)) -> dict:
    users = session.exec(select(User)).all()
    admins = session.exec(select(Admin)).all()
    orders = session.exec(select(Order)).all()
    trades = session.exec(select(Trade)).all()
    positions = session.exec(select(Position)).all()
    today_start = time.time() - 86400

    total_asset = sum(u.total_asset or (u.cash + u.market_value) for u in users)
    today_orders = [o for o in orders if o.created_at >= today_start]
    today_trades = [t for t in trades if t.created_at >= today_start]

    st = current_session()
    return {
        "kpi": {
            "user_count": len(users),
            "retail_count": sum(1 for u in users if u.role == Role.RETAIL),
            "institution_count": sum(1 for u in users if u.role == Role.INSTITUTION),
            "admin_count": len(admins),
            "active_users": sum(1 for u in users if u.status == AccountStatus.ACTIVE),
            "frozen_users": sum(1 for u in users if u.status == AccountStatus.FROZEN),
            "total_asset": round(total_asset, 2),
            "today_orders": len(today_orders),
            "today_trades": len(today_trades),
            "today_turnover": round(sum(t.amount for t in today_trades), 2),
            "open_orders": sum(1 for o in orders if o.status in (OrderStatus.PENDING, OrderStatus.PARTIAL)),
            "position_rows": len(positions),
            "watch_items": session.exec(select(func.count()).select_from(WatchItem)).one(),
        },
        "market": market_engine.stats(),
        "session": {
            "phase": st.phase,
            "label": st.label,
            "can_trade": st.can_trade,
            "trade_date": st.trade_date,
        },
        "risk_distribution": _risk_distribution(session),
    }


def _risk_distribution(session: Session) -> dict:
    """风险偏好分布：多少账户未测评、多少是保守/稳健/进取（只看交易账户）。"""
    rows = session.exec(select(User)).all()
    dist = {"unassessed": 0, "conservative": 0, "balanced": 0, "aggressive": 0}
    for u in rows:
        if u.risk_assessed_at <= 0 or u.risk_level <= 0:
            dist["unassessed"] += 1
        elif u.risk_level <= 1:
            dist["conservative"] += 1
        elif u.risk_level == 2:
            dist["balanced"] += 1
        else:
            dist["aggressive"] += 1
    return {"total": len(rows), **dist}


# --------------------------------------------------------------------------- #
# 运行期配置
# --------------------------------------------------------------------------- #
@router.get("/settings")
def get_settings(admin: Admin = Depends(admin_user)) -> dict:
    """全部可调配置项及其当前值（含类型、范围、说明，前端据此渲染表单）。"""
    return {"items": runtime_config.snapshot()}


class SettingsIn(BaseModel):
    """批量更新：只传要改的键，未传的保持原值。"""

    values: dict[str, object] = Field(default_factory=dict)


@router.put("/settings")
def put_settings(
    payload: SettingsIn,
    session: Session = Depends(get_session),
    admin: Admin = Depends(admin_user),
) -> dict:
    """修改运行期配置：校验通过后立即写到内存 settings，下一次请求即生效。"""
    if not payload.values:
        raise HTTPException(400, detail={"code": "EMPTY", "message": "没有需要修改的配置"})
    return runtime_config.update(session, payload.values, operator=admin.username)


@router.post("/settings/reset")
def reset_settings(session: Session = Depends(get_session), admin: Admin = Depends(admin_user)) -> dict:
    """恢复全部配置为代码默认值。"""
    return runtime_config.reset_defaults(session, operator=admin.username)


# --------------------------------------------------------------------------- #
# 交易账户（零售 + 机构，同一张表）
# --------------------------------------------------------------------------- #
@router.get("/accounts")
def list_accounts(
    role: str = Query(default="retail", max_length=16),
    keyword: str = Query(default="", max_length=20),
    limit: int = Query(default=200, ge=1, le=500),
    session: Session = Depends(get_session),
    admin: Admin = Depends(admin_user),
) -> dict:
    """按角色列出交易账户。零售与机构共用 `user` 表，这里只做 role 过滤。

    管理员已独立成表，因此本接口不会返回管理员 —— 想看管理员请用 `/admins`。
    """
    if role not in {"retail", "institution"}:
        raise HTTPException(
            400, detail={"code": "BAD_ROLE", "message": "role 仅支持 retail/institution"}
        )
    target = Role(role)
    rows = session.exec(
        select(User).where(User.role == target).order_by(User.created_at.desc()).limit(limit)
    ).all()
    items = []
    for u in rows:
        if keyword and keyword not in u.username and keyword not in (u.display_name or ""):
            continue
        pos_count = session.exec(
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
                "risk_assessed": u.risk_assessed_at > 0,
                "positions": pos_count,
                "last_login_at": u.last_login_at,
                "created_at": u.created_at,
            }
        )
    return {"role": role, "total": len(items), "items": items}


class AccountIn(BaseModel):
    username: str = Field(min_length=2, max_length=40)
    display_name: str = Field(default="", max_length=40)
    password: str = Field(min_length=6, max_length=64)
    role: str = Field(default="retail", max_length=16)
    cash: float = Field(default=1_000_000.0, ge=0)
    credit_limit: float = Field(default=0.0, ge=0)


@router.post("/accounts")
def create_account(
    payload: AccountIn,
    session: Session = Depends(get_session),
    admin: Admin = Depends(admin_user),
) -> dict:
    """新建交易账户（零售或机构）。管理员请用 `/admins`。"""
    if payload.role not in {"retail", "institution"}:
        raise HTTPException(
            400,
            detail={"code": "BAD_ROLE", "message": "role 仅支持 retail/institution；管理员请用管理员接口"},
        )
    if session.exec(select(User).where(User.username == payload.username)).first() is not None:
        raise HTTPException(400, detail={"code": "DUP_USERNAME", "message": f"用户名 {payload.username} 已存在"})

    user = User(
        username=payload.username,
        display_name=payload.display_name or payload.username,
        password_hash=hash_password(payload.password),
        role=Role(payload.role),
        status=AccountStatus.ACTIVE,
        cash=payload.cash,
        total_asset=payload.cash,
        credit_limit=payload.credit_limit if payload.role == "institution" else 0.0,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    # 新账户落一个默认自选分组，客户端首屏不必处理"无分组"分支
    session.add(WatchGroup(user_id=user.id or 0, name="默认分组", sort=0))
    session.commit()
    return {"ok": True, "id": user.id, "username": user.username, "role": user.role.value}


@router.delete("/accounts/{user_id}")
def delete_account(
    user_id: int,
    session: Session = Depends(get_session),
    admin: Admin = Depends(admin_user),
) -> dict:
    """删除交易账户，连同其持仓、挂单、流水、自选一并清理。"""
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(404, detail={"code": "USER_NOT_FOUND", "message": "账户不存在"})

    for model in (Position, Order, Trade, CashFlow):
        for row in session.exec(select(model).where(model.user_id == user_id)).all():
            session.delete(row)
    for model in (WatchItem, WatchGroup):
        for row in session.exec(select(model).where(model.user_id == user_id)).all():
            session.delete(row)
    session.delete(target)
    session.commit()
    return {"ok": True}


class UserStatusIn(BaseModel):
    status: AccountStatus


@router.post("/accounts/{user_id}/status")
def update_account_status(
    user_id: int,
    payload: UserStatusIn,
    session: Session = Depends(get_session),
    admin: Admin = Depends(admin_user),
) -> dict:
    """冻结 / 解冻 / 注销交易账户。"""
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(404, detail={"code": "USER_NOT_FOUND", "message": "账户不存在"})
    target.status = payload.status
    target.updated_at = time.time()
    session.add(target)
    session.commit()
    return {"ok": True, "status": target.status.value}


class AdjustIn(BaseModel):
    amount: float = Field(description="正数入金，负数出金")
    remark: str = Field(default="", max_length=80)


@router.post("/accounts/{user_id}/adjust")
def adjust_cash(
    user_id: int,
    payload: AdjustIn,
    session: Session = Depends(get_session),
    admin: Admin = Depends(admin_user),
) -> dict:
    """调整账户可用资金（入金/出金），并写一条流水。"""
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(404, detail={"code": "USER_NOT_FOUND", "message": "账户不存在"})
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
    session.commit()
    return {"ok": True, "cash": target.cash, "total_asset": target.total_asset}


# --------------------------------------------------------------------------- #
# 管理员账户（独立表）
# --------------------------------------------------------------------------- #
@router.get("/admins")
def list_admins(session: Session = Depends(get_session), admin: Admin = Depends(admin_user)) -> dict:
    rows = session.exec(select(Admin).order_by(Admin.created_at.desc())).all()
    return {"total": len(rows), "items": [admin_public(a) for a in rows]}


class AdminIn(BaseModel):
    username: str = Field(min_length=2, max_length=40)
    display_name: str = Field(default="", max_length=40)
    password: str = Field(min_length=8, max_length=64)


@router.post("/admins")
def create_admin(
    payload: AdminIn,
    session: Session = Depends(get_session),
    admin: Admin = Depends(admin_user),
) -> dict:
    """新增管理员。用户名在 admin 表内唯一（与交易账户同名不冲突，是两套体系）。"""
    username = payload.username.lower()
    if session.exec(select(Admin).where(Admin.username == username)).first() is not None:
        raise HTTPException(400, detail={"code": "DUP_USERNAME", "message": f"管理员 {username} 已存在"})
    row = Admin(
        username=username,
        display_name=payload.display_name or username,
        password_hash=hash_password(payload.password),
        is_active=True,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return {"ok": True, "admin": admin_public(row)}


@router.delete("/admins/{admin_id}")
def delete_admin(
    admin_id: int,
    session: Session = Depends(get_session),
    admin: Admin = Depends(admin_user),
) -> dict:
    """删除管理员。保护：不能删自己，也不能删最后一个管理员。"""
    target = session.get(Admin, admin_id)
    if target is None:
        raise HTTPException(404, detail={"code": "ADMIN_NOT_FOUND", "message": "管理员不存在"})
    if target.id == admin.id:
        raise HTTPException(400, detail={"code": "SELF_DELETE", "message": "不能删除当前登录的管理员"})
    total = session.exec(select(func.count()).select_from(Admin)).one()
    if total <= 1:
        raise HTTPException(400, detail={"code": "LAST_ADMIN", "message": "至少保留一个管理员"})
    session.delete(target)
    session.commit()
    return {"ok": True}


class AdminStatusIn(BaseModel):
    is_active: bool


@router.post("/admins/{admin_id}/status")
def set_admin_status(
    admin_id: int,
    payload: AdminStatusIn,
    session: Session = Depends(get_session),
    admin: Admin = Depends(admin_user),
) -> dict:
    target = session.get(Admin, admin_id)
    if target is None:
        raise HTTPException(404, detail={"code": "ADMIN_NOT_FOUND", "message": "管理员不存在"})
    if target.id == admin.id and not payload.is_active:
        raise HTTPException(400, detail={"code": "SELF_DISABLE", "message": "不能停用当前登录的管理员"})
    target.is_active = payload.is_active
    target.updated_at = time.time()
    session.add(target)
    session.commit()
    return {"ok": True, "is_active": target.is_active}


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


LIMIT_BY_BOARD = {"主板": 0.10, "创业板": 0.20, "科创板": 0.20, "北交所": 0.30}


def _apply(stock: Stock, payload: StockIn) -> Stock:
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
    session: Session = Depends(get_session),
    admin: Admin = Depends(admin_user),
) -> dict:
    if session.get(Stock, payload.code) is not None:
        raise HTTPException(400, detail={"code": "DUP_CODE", "message": f"标的 {payload.code} 已存在"})
    stock = _apply(Stock(code=payload.code, tick_size=0.01, lot_size=100), payload)
    session.add(stock)
    session.commit()
    _register(stock)
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
    _apply(stock, payload)
    session.add(stock)
    session.commit()
    _register(stock)
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
    for model in (DailyBar, QuoteTick):
        for row in session.exec(select(model).where(model.code == code)).all():
            session.delete(row)
    for row in session.exec(select(WatchItem).where(WatchItem.code == code)).all():
        session.delete(row)

    session.delete(stock)
    session.commit()
    market_engine.unregister(code)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# 数据初始化（手动注入，幂等）
# --------------------------------------------------------------------------- #
class SeedIn(BaseModel):
    stocks: bool = True
    users: bool = True
    history: bool = True


@router.post("/seed")
def seed_data(
    payload: SeedIn,
    session: Session = Depends(get_session),
    admin: Admin = Depends(admin_user),
) -> dict:
    """手动注入标准数据集。`ensure_*` 全部幂等，重复点击不会产生副本。"""
    from ..bootstrap import init_seed_data

    result = init_seed_data(
        include_stocks=payload.stocks,
        include_users=payload.users,
        include_history=payload.history,
    )
    return {"ok": True, **result}


@router.get("/seed/status")
def seed_status(session: Session = Depends(get_session), admin: Admin = Depends(admin_user)) -> dict:
    stocks = session.exec(select(func.count()).select_from(Stock)).one()
    users = session.exec(select(func.count()).select_from(User)).one()
    admins = session.exec(select(func.count()).select_from(Admin)).one()
    bars = session.exec(select(func.count()).select_from(DailyBar)).one()
    return {"stocks": stocks, "users": users, "admins": admins, "daily_bars": bars, "seeded": stocks > 0}


# --------------------------------------------------------------------------- #
# 撮合 / 业绩 / 系统事件 / 结算
# --------------------------------------------------------------------------- #
@router.get("/matching")
def matching_stats(session: Session = Depends(get_session), admin: Admin = Depends(admin_user)) -> dict:
    """撮合质量：成交率、撤单率、平均单笔金额、成交额 Top 标的。"""
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


@router.get("/leaderboard")
def leaderboard(session: Session = Depends(get_session), admin: Admin = Depends(admin_user)) -> dict:
    """按总资产排名（交易账户）。"""
    rows = session.exec(select(User)).all()
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
    ]
    items.sort(key=lambda x: x["total_asset"], reverse=True)
    return {"items": items}


@router.post("/settle")
def settle(session: Session = Depends(get_session), admin: Admin = Depends(admin_user)) -> dict:
    """执行日终结转（T+1 解锁）。"""
    settle_all(session)
    return {"ok": True, "at": int(time.time())}


# --------------------------------------------------------------------------- #
# 风险偏好（只读汇总，无历史档案）
# --------------------------------------------------------------------------- #
@router.get("/risk-summary")
def risk_summary(session: Session = Depends(get_session), admin: Admin = Depends(admin_user)) -> dict:
    """风险偏好分布（零售 + 机构）。历史档案功能已移除，这里只给当前分布。"""
    return _risk_distribution(session)
