"""管理端 · 交易账户（零售 + 机构，同一张表）。"""
from __future__ import annotations

from ._common import *  # noqa: F401,F403
from ._common import router


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
    # P0-5：关键词与持仓数都必须下推到 SQL。
    # 旧实现先 limit 再在 Python 里 continue —— 关键词只在「当页」生效，
    # 命中的账户排在第 limit 条之后就永远搜不到；而且每行额外发一次 count（N+1）。
    stmt = select(User, Account).join(Account, Account.user_id == User.id).where(User.role == target)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(or_(User.username.like(like), User.display_name.like(like)))
    stmt = stmt.order_by(User.created_at.desc()).limit(limit)

    # 持仓数用一次 GROUP BY 聚合，再按 user_id 取用
    pos_counts = {
        int(uid): int(cnt or 0)
        for uid, cnt in session.exec(
            select(Position.user_id, func.count())
            .where(Position.quantity > 0)
            .group_by(Position.user_id)
        ).all()
    }

    items = []
    for u, a in session.exec(stmt).all():
        money = accounts.snapshot(a)
        items.append(
            {
                "id": u.id,
                "username": u.username,
                "display_name": u.display_name or u.username,
                "role": u.role.value,
                "status": u.status.value,
                "cash": money["cash"],
                "frozen_cash": money["frozen_cash"],
                "market_value": money["market_value"],
                "total_asset": money["total_asset"],
                "realized_pnl": money["realized_pnl"],
                "credit_limit": money["credit_limit"],
                "commission_rate": money["commission_rate"],
                "commission_min": money["commission_min"],
                "risk_level": u.risk_level,
                "risk_assessed": u.risk_assessed_at > 0,
                "positions": pos_counts.get(int(u.id or 0), 0),
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
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    # 资金行独立写入：初始资金即 total_asset 的起点
    acct = accounts.of(session, user)
    acct.cash = float(payload.cash)
    acct.credit_limit = payload.credit_limit if payload.role == "institution" else 0.0
    accounts.recompute(acct)
    session.add(acct)
    session.commit()
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

    for model in (Position, Order, Trade, CashFlow, Account):
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
    """编辑交易账户：一次提交可改多项，只处理显式传入的字段。

    为什么用「显式传入才改」而不是整行覆盖：管理端表单通常只回填一部分字段，
    整行覆盖会把没展示的字段（如 commission_rate）当成 null 悄悄清空。
    这里靠 `model_dump(exclude_unset=True)` 区分「没传」与「传了 null」，
    传 null 才代表「改回跟随角色默认」。
    """

    display_name: Optional[str] = Field(default=None, max_length=40)
    status: Optional[str] = Field(default=None, max_length=16)
    risk_level: Optional[int] = Field(default=None, ge=0, le=3)
    credit_limit: Optional[float] = Field(default=None, ge=0)
    commission_rate: Optional[float] = Field(default=None, ge=0, le=0.01)
    commission_min: Optional[float] = Field(default=None, ge=0, le=1000)
    password: Optional[str] = Field(default=None, min_length=6, max_length=64)
    cash_delta: Optional[float] = Field(
        default=None, description="可选：同时调整可用资金，正数入金、负数出金"
    )
    remark: str = Field(default="", max_length=80)


@router.put("/accounts/{user_id}")
def edit_account(
    user_id: int,
    payload: AccountEditIn,
    session: Session = Depends(get_session),
    admin: Admin = Depends(admin_user),
) -> dict:
    """编辑交易账户资料，可顺带调整资金。

    `role` 与 `username` 不在此处修改：前者会影响授信、费率默认与撮合口径，
    后者是登录凭据与所有流水的关联键，都不适合在「编辑资料」里顺手改。
    资金调整仍写一条 `adjust` 流水，保持与旧「调资金」一致的审计口径。
    """
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(404, detail={"code": "USER_NOT_FOUND", "message": "账户不存在"})

    data = payload.model_dump(exclude_unset=True)
    changed: list[str] = []

    if "display_name" in data:
        target.display_name = (data["display_name"] or "").strip() or target.username
        changed.append("display_name")

    if "status" in data and data["status"] is not None:
        try:
            target.status = AccountStatus(data["status"])
        except ValueError:
            raise HTTPException(
                400,
                detail={"code": "BAD_STATUS", "message": "状态仅支持 active/frozen/closed"},
            )
        changed.append("status")

    if "risk_level" in data and data["risk_level"] is not None:
        target.risk_level = int(data["risk_level"])
        # 0 表示「从未测评」，此时清掉测评时间；改为具体等级则记为刚测评过。
        target.risk_assessed_at = 0.0 if target.risk_level == 0 else time.time()
        changed.append("risk_level")

    acct = accounts.of(session, target)

    if "credit_limit" in data:
        # 授信只对机构有意义；零售账户恒为 0，避免出现「零售也能融资」的脏数据。
        acct.credit_limit = (
            0.0 if target.role != Role.INSTITUTION else round(float(data["credit_limit"] or 0.0), 2)
        )
        changed.append("credit_limit")

    for key in ("commission_rate", "commission_min"):
        if key in data:
            val = data[key]
            setattr(acct, key, None if val is None else round(float(val), 6))
            changed.append(key)

    if "password" in data and data["password"]:
        target.password_hash = hash_password(data["password"])
        target.fail_count = 0
        target.locked_until = 0.0
        changed.append("password")

    if data.get("cash_delta"):
        before = acct.cash
        after = round(before + float(data["cash_delta"]), 4)
        if after < 0:
            raise HTTPException(
                400, detail={"code": "INVALID_AMOUNT", "message": "调整后可用资金不能为负"}
            )
        acct.cash = after
        accounts.recompute(acct)
        session.add(acct)
        session.add(
            CashFlow(
                user_id=target.id or 0,
                biz_type="adjust",
                amount=round(float(data["cash_delta"]), 4),
                balance_before=round(before, 4),
                balance_after=after,
                remark=payload.remark or "管理端资金调整",
            )
        )
        changed.append("cash")

    accounts.recompute(acct)
    target.updated_at = time.time()
    session.add(target)
    session.commit()
    session.refresh(target)
    return {
        "ok": True,
        "id": target.id,
        "changed": changed,
        "cash": round(acct.cash, 2),
        "total_asset": round(acct.total_asset, 2),
    }


class AdjustIn(BaseModel):
    amount: float = Field(description="正数入金，负数出金")
    remark: str = Field(default="", max_length=80)
    # 幂等键：也可用 Idempotency-Key 请求头。弱网重试不会重复入账。
    client_request_id: str = Field(default="", max_length=80)


@router.post("/accounts/{user_id}/adjust")
def adjust_cash(
    user_id: int,
    payload: AdjustIn,
    idempotency_key: str = Header(default=""),
    session: Session = Depends(get_session),
    admin: Admin = Depends(admin_user),
) -> dict:
    """调整账户可用资金（入金/出金），并写一条流水。

    幂等：这是「动钱」的接口，弱网重试必须安全。调用方带
    ``Idempotency-Key`` 头或请求体 ``client_request_id`` 时，同一键只会生效一次，
    重试请求原样回放首次结果（不会二次入账）。
    """
    crid = idempotency.client_request_id(idempotency_key, payload.client_request_id)
    scope = "admin.adjust"
    prior = idempotency.lookup(session, scope=scope, key=crid)
    if prior:
        return prior

    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(404, detail={"code": "USER_NOT_FOUND", "message": "账户不存在"})

    acct = accounts.of(session, target)
    before = acct.cash
    new_cash = round(acct.cash + payload.amount, 4)
    if new_cash < 0:
        raise HTTPException(400, detail={"code": "INVALID_AMOUNT", "message": "调整后可用资金不能为负"})
    acct.cash = new_cash
    accounts.recompute(acct)
    session.add(acct)
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

    result = {"ok": True, "cash": round(acct.cash, 2), "total_asset": round(acct.total_asset, 2)}
    idempotency.remember(
        session, scope=scope, key=crid, actor_id=int(admin.id or 0), response=result
    )
    session.commit()
    return result
