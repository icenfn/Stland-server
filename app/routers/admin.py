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
from typing import Optional

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
    Board,
    BoardMember,
    CashFlow,
    DailyBar,
    Order,
    OrderStatus,
    Position,
    Role,
    Stock,
    Trade,
    User,
    WatchGroup,
    WatchItem,
)
from ..security import hash_password, verify_password
from ..services.deps import admin_user, issue_admin_token

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
    }


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


class AccountEditIn(BaseModel):
    """账户编辑请求体。

    可用资金用「目标值」而不是增减量：编辑弹窗里运营看到的是当前余额，
    填多少就是改成多少，比让他自己算差额直观，也避免填错正负号。
    差额会写一条流水，账面变化照样可回溯。
    """

    display_name: Optional[str] = Field(default=None, max_length=40)
    status: Optional[AccountStatus] = None
    cash: Optional[float] = Field(default=None, ge=0)
    credit_limit: Optional[float] = Field(default=None, ge=0)
    remark: str = Field(default="", max_length=80)


@router.put("/accounts/{user_id}")
def edit_account(
    user_id: int,
    payload: AccountEditIn,
    session: Session = Depends(get_session),
    admin: Admin = Depends(admin_user),
) -> dict:
    """编辑交易账户：昵称、状态、可用资金、授信额度一次提交。

    资金改动同样写一条 `cash_flow`，与原来的「调资金」保持同一套账。
    """
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(404, detail={"code": "USER_NOT_FOUND", "message": "账户不存在"})

    if payload.display_name is not None:
        name = payload.display_name.strip()
        target.display_name = name or target.username
    if payload.status is not None:
        target.status = payload.status
    if payload.credit_limit is not None:
        # 授信只有机构账户有意义，零售账户写 0，避免出现「零售也能融资」的错觉
        target.credit_limit = payload.credit_limit if target.role == Role.INSTITUTION else 0.0

    if payload.cash is not None:
        now_cash = round(payload.cash, 4)
        delta = round(now_cash - target.cash, 4)
        if delta != 0:
            before = target.cash
            target.cash = now_cash
            session.add(
                CashFlow(
                    user_id=target.id or 0,
                    biz_type="adjust",
                    amount=delta,
                    balance_before=round(before, 4),
                    balance_after=now_cash,
                    remark=payload.remark or "管理端资金调整",
                )
            )

    target.total_asset = round(target.cash + target.frozen_cash + target.market_value, 4)
    target.updated_at = time.time()
    session.add(target)
    session.commit()
    return {
        "ok": True,
        "id": target.id,
        "display_name": target.display_name,
        "status": target.status.value,
        "cash": round(target.cash, 2),
        "credit_limit": round(target.credit_limit, 2),
        "total_asset": round(target.total_asset, 2),
    }


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


LIMIT_BY_BOARD = {"主板": 0.10, "创业板": 0.20, "科创板": 0.20}


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
    # 先清掉依附在这支标的上的行情、自选成分与分类归属 —— 委托与成交流水属于
    # 账户侧历史，保留不动，删标的不能让别人的对账记录跟着消失。
    for model in (DailyBar, WatchItem):
        for row in session.exec(select(model).where(model.code == code)).all():
            session.delete(row)
    for row in session.exec(select(BoardMember).where(BoardMember.code == code)).all():
        session.delete(row)

    session.delete(stock)
    session.commit()
    market_engine.unregister(code)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# 板块与行业（自由维护的业务分类）
# --------------------------------------------------------------------------- #
BOARD_KINDS = {"theme": "板块", "sector": "行业"}


class BoardIn(BaseModel):
    kind: str = Field(default="theme", max_length=8)
    key: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=32)
    desc: str = Field(default="", max_length=200)
    sort: int = Field(default=0)
    enabled: bool = True


def _board_public(session: Session, b: Board) -> dict:
    members = session.exec(select(BoardMember).where(BoardMember.board_id == (b.id or 0))).all()
    return {
        "id": b.id,
        "kind": b.kind,
        "kind_label": BOARD_KINDS.get(b.kind, b.kind),
        "key": b.key,
        "name": b.name,
        "desc": b.desc,
        "sort": b.sort,
        "enabled": b.enabled,
        "members": len(members),
        "created_at": b.created_at,
        "updated_at": b.updated_at,
    }


@router.get("/boards")
def list_boards(
    kind: str = Query(default="", max_length=8),
    session: Session = Depends(get_session),
    admin: Admin = Depends(admin_user),
) -> dict:
    """列出全部板块/行业分类，可按 kind 过滤。

    成分股数量在这里一次算好，前端不必再对每个分类回查一遍。
    """
    stmt = select(Board)
    if kind:
        if kind not in BOARD_KINDS:
            raise HTTPException(400, detail={"code": "BAD_KIND", "message": "kind 仅支持 theme/sector"})
        stmt = stmt.where(Board.kind == kind)
    rows = session.exec(stmt.order_by(Board.kind, Board.sort, Board.id)).all()
    items = [_board_public(session, b) for b in rows]
    themes = [i for i in items if i["kind"] == "theme"]
    sectors = [i for i in items if i["kind"] == "sector"]
    return {
        "total": len(items),
        "theme_count": len(themes),
        "sector_count": len(sectors),
        "members": sum(i["members"] for i in items),
        "items": items,
    }


def _slug_key(kind: str, session: Session, seed: str) -> str:
    """按名称生成稳定 key：优先用调用方给的 key，缺省时用 pinyin 不可得则退化成序号。"""
    base = (seed or "").strip()
    if base:
        return base
    n = session.exec(select(func.count()).select_from(Board).where(Board.kind == kind)).one()
    return f"{kind}-{n + 1}"


@router.post("/boards")
def create_board(
    payload: BoardIn,
    session: Session = Depends(get_session),
    admin: Admin = Depends(admin_user),
) -> dict:
    if payload.kind not in BOARD_KINDS:
        raise HTTPException(400, detail={"code": "BAD_KIND", "message": "kind 仅支持 theme/sector"})
    key = _slug_key(payload.kind, session, payload.key)
    dup = session.exec(
        select(Board).where(Board.kind == payload.kind, Board.key == key)
    ).first()
    if dup is not None:
        raise HTTPException(400, detail={"code": "DUP_KEY", "message": f"标识 {key} 已存在"})
    board = Board(
        kind=payload.kind,
        key=key,
        name=payload.name,
        desc=payload.desc,
        sort=payload.sort,
        enabled=payload.enabled,
    )
    session.add(board)
    session.commit()
    session.refresh(board)
    return {"ok": True, "id": board.id}


@router.put("/boards/{board_id}")
def update_board(
    board_id: int,
    payload: BoardIn,
    session: Session = Depends(get_session),
    admin: Admin = Depends(admin_user),
) -> dict:
    board = session.get(Board, board_id)
    if board is None:
        raise HTTPException(404, detail={"code": "BOARD_NOT_FOUND", "message": "分类不存在"})
    if payload.kind not in BOARD_KINDS:
        raise HTTPException(400, detail={"code": "BAD_KIND", "message": "kind 仅支持 theme/sector"})
    if payload.kind != board.kind:
        raise HTTPException(400, detail={"code": "KIND_IMMUTABLE", "message": "类型不可修改，如需更换请删除后新建"})
    if payload.key != board.key:
        dup = session.exec(
            select(Board).where(Board.kind == board.kind, Board.key == payload.key, Board.id != board_id)
        ).first()
        if dup is not None:
            raise HTTPException(400, detail={"code": "DUP_KEY", "message": f"标识 {payload.key} 已存在"})
    board.key = payload.key
    board.name = payload.name
    board.desc = payload.desc
    board.sort = payload.sort
    board.enabled = payload.enabled
    board.updated_at = time.time()
    session.add(board)
    session.commit()
    return {"ok": True}


@router.delete("/boards/{board_id}")
def delete_board(
    board_id: int,
    session: Session = Depends(get_session),
    admin: Admin = Depends(admin_user),
) -> dict:
    """删除分类本身，成分关联一并清掉；标的本身不受影响。"""
    board = session.get(Board, board_id)
    if board is None:
        raise HTTPException(404, detail={"code": "BOARD_NOT_FOUND", "message": "分类不存在"})
    for row in session.exec(select(BoardMember).where(BoardMember.board_id == board_id)).all():
        session.delete(row)
    session.delete(board)
    session.commit()
    return {"ok": True}


@router.get("/boards/{board_id}/members")
def list_board_members(
    board_id: int,
    session: Session = Depends(get_session),
    admin: Admin = Depends(admin_user),
) -> dict:
    board = session.get(Board, board_id)
    if board is None:
        raise HTTPException(404, detail={"code": "BOARD_NOT_FOUND", "message": "分类不存在"})
    rows = session.exec(select(BoardMember).where(BoardMember.board_id == board_id)).all()
    codes = [r.code for r in rows]
    name_map = {}
    if codes:
        for st in session.exec(select(Stock).where(Stock.code.in_(codes))).all():
            name_map[st.code] = st.name
    items = [{"code": c, "name": name_map.get(c, "")} for c in sorted(codes)]
    return {"board": _board_public(session, board), "total": len(items), "items": items}


class BoardMemberIn(BaseModel):
    codes: list[str] = Field(default_factory=list)


@router.post("/boards/{board_id}/members")
def add_board_members(
    board_id: int,
    payload: BoardMemberIn,
    session: Session = Depends(get_session),
    admin: Admin = Depends(admin_user),
) -> dict:
    """批量加入成分股。已存在的跳过，未知代码直接拒绝，避免脏关联。"""
    board = session.get(Board, board_id)
    if board is None:
        raise HTTPException(404, detail={"code": "BOARD_NOT_FOUND", "message": "分类不存在"})
    wanted = [c.strip() for c in payload.codes if c and c.strip()]
    if not wanted:
        raise HTTPException(400, detail={"code": "EMPTY_CODES", "message": "请至少提供一个代码"})
    known = {st.code for st in session.exec(select(Stock).where(Stock.code.in_(wanted))).all()}
    unknown = [c for c in wanted if c not in known]
    if unknown:
        raise HTTPException(
            400,
            detail={"code": "UNKNOWN_CODE", "message": "以下代码不存在：" + "、".join(unknown[:5])},
        )
    existing = {
        r.code for r in session.exec(select(BoardMember).where(BoardMember.board_id == board_id)).all()
    }
    added = 0
    for code in wanted:
        if code in existing:
            continue
        session.add(BoardMember(board_id=board_id, code=code))
        existing.add(code)
        added += 1
    session.commit()
    return {"ok": True, "added": added, "skipped": len(wanted) - added}


@router.delete("/boards/{board_id}/members/{code}")
def remove_board_member(
    board_id: int,
    code: str,
    session: Session = Depends(get_session),
    admin: Admin = Depends(admin_user),
) -> dict:
    rows = session.exec(
        select(BoardMember).where(BoardMember.board_id == board_id, BoardMember.code == code)
    ).all()
    for row in rows:
        session.delete(row)
    session.commit()
    return {"ok": True, "removed": len(rows)}


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
# 模拟引擎（后台「模拟引擎」页签）
# --------------------------------------------------------------------------- #
class EngineControlIn(BaseModel):
    """引擎开关请求体。"""

    enabled: bool


@router.get("/engine")
def engine_status(admin: Admin = Depends(admin_user)) -> dict:
    """引擎运行态：开关、推进节奏、交易模式、当前时段、标的统计。

    与「系统配置」页签的分工：那边改的是**参数**（费率、tick 间隔等），
    这里给的是**运行态**，并额外提供启停与重置两个动作，方便运营看到
    「现在是 A 股模式、处于午间休市、价格没有推进」这类因果。
    """
    from ..engine.session import TRADE_MODES

    status = market_engine.status()
    status["modes"] = [{"value": k, "label": v} for k, v in TRADE_MODES.items()]
    return status


@router.post("/engine/toggle")
def engine_toggle(
    payload: EngineControlIn,
    session: Session = Depends(get_session),
    admin: Admin = Depends(admin_user),
) -> dict:
    """启停行情引擎。关闭后价格冻结，但账户、持仓与撮合数据都不动。"""
    result = runtime_config.update(
        session, {"engine_enabled": payload.enabled}, operator=admin.username
    )
    if result["rejected"]:
        raise HTTPException(400, detail=result["rejected"][0])
    status = market_engine.status()
    status["modes"] = []
    return status


@router.post("/engine/reset")
def engine_reset(admin: Admin = Depends(admin_user)) -> dict:
    """重置内存行情：所有标的回到昨收，清空分钟线与累计成交。

    只影响行情快照，不动标的参数、账户资金与持仓，因此不会破坏对账。
    """
    status = market_engine.reset()
    status["modes"] = []
    return status


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


