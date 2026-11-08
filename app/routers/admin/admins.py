"""管理端 · 管理员账户（独立表）。"""
from __future__ import annotations

from ._common import *  # noqa: F401,F403
from ._common import router


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
