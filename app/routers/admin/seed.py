"""管理端 · 数据初始化（手动注入，幂等）。"""
from __future__ import annotations

from ._common import *  # noqa: F401,F403
from ._common import router


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
    from ...bootstrap import init_seed_data

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
