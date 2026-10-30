"""自选股路由：分组管理与成分维护。

设计取舍
--------
* **分组必存在于每个账户**：新账户首次访问时自动创建「默认分组」，
  避免客户端要先判断「有没有分组」再决定要不要建，减少一次往返与一类空态分支。
* **一支股票只在一个分组里**：同账户下重复添加会「移动到目标分组」而不是报错，
  这符合用户直觉（我把它从自选1挪到自选2），也免去客户端先删后加。
* **删除分组不删股票**：分组内成分被移到默认分组，防止误删自选。
* 列表返回时**顺带把实时行情拼好**，客户端一个请求就能渲染带涨跌幅的自选列表，
  不需要再按代码逐个查详情（N+1 请求在移动端很致命）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from ..db import get_session
from ..engine.market import market_engine
from ..models import Stock, User, WatchGroup, WatchItem
from ..services.deps import current_user, signed_request

router = APIRouter(prefix="/api/watch", tags=["watch"])

DEFAULT_GROUP_NAME = "默认分组"


def _ensure_default_group(session: Session, user_id: int) -> WatchGroup:
    """保证账户至少有一个分组。"""
    group = session.exec(
        select(WatchGroup).where(WatchGroup.user_id == user_id).order_by(WatchGroup.sort, WatchGroup.id)
    ).first()
    if group is None:
        group = WatchGroup(user_id=user_id, name=DEFAULT_GROUP_NAME, sort=0)
        session.add(group)
        session.commit()
        session.refresh(group)
    return group


def _group_view(g: WatchGroup, count: int) -> dict:
    return {"id": g.id, "name": g.name, "sort": g.sort, "count": count}


@router.get("/groups", dependencies=[Depends(signed_request)])
def list_groups(session: Session = Depends(get_session), user: User = Depends(current_user)) -> dict:
    """分组列表（含每组的自选数量）。账号首次访问自动建默认分组。"""
    _ensure_default_group(session, user.id or 0)
    groups = session.exec(
        select(WatchGroup).where(WatchGroup.user_id == user.id).order_by(WatchGroup.sort, WatchGroup.id)
    ).all()
    counts: dict[int, int] = {}
    for item in session.exec(select(WatchItem).where(WatchItem.user_id == user.id)).all():
        counts[item.group_id] = counts.get(item.group_id, 0) + 1
    return {"items": [_group_view(g, counts.get(g.id or 0, 0)) for g in groups]}


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=12)


@router.post("/groups", dependencies=[Depends(signed_request)])
def create_group(
    payload: GroupIn,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> dict:
    """新建分组。同名分组视为「已存在」，直接返回既有分组而不是报错。"""
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, detail={"code": "BAD_NAME", "message": "分组名称不能为空"})
    exists = session.exec(
        select(WatchGroup).where(WatchGroup.user_id == user.id, WatchGroup.name == name)
    ).first()
    if exists:
        return {"ok": True, "group": _group_view(exists, 0), "existed": True}

    total = len(session.exec(select(WatchGroup).where(WatchGroup.user_id == user.id)).all())
    group = WatchGroup(user_id=user.id or 0, name=name, sort=total)
    session.add(group)
    session.commit()
    session.refresh(group)
    return {"ok": True, "group": _group_view(group, 0), "existed": False}


@router.patch("/groups/{group_id}", dependencies=[Depends(signed_request)])
def rename_group(
    group_id: int,
    payload: GroupIn,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> dict:
    """重命名分组。与原组同名视为无操作，与其他分组重名则拒绝。"""
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, detail={"code": "BAD_NAME", "message": "分组名称不能为空"})

    group = session.get(WatchGroup, group_id)
    if group is None or group.user_id != user.id:
        raise HTTPException(404, detail={"code": "GROUP_NOT_FOUND", "message": "分组不存在"})

    if group.name == name:
        return {"ok": True, "group": _group_view(group, 0), "changed": False}

    clash = session.exec(
        select(WatchGroup).where(
            WatchGroup.user_id == user.id, WatchGroup.name == name, WatchGroup.id != group_id
        )
    ).first()
    if clash is not None:
        raise HTTPException(400, detail={"code": "NAME_TAKEN", "message": "已存在同名分组"})

    group.name = name
    session.add(group)
    session.commit()
    session.refresh(group)
    count = len(
        session.exec(select(WatchItem).where(WatchItem.group_id == group.id)).all()
    )
    return {"ok": True, "group": _group_view(group, count), "changed": True}


@router.delete("/groups/{group_id}", dependencies=[Depends(signed_request)])
def delete_group(
    group_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> dict:
    """删除分组：成分先迁移到默认分组，不丢股票。至少保留一个分组。"""
    group = session.get(WatchGroup, group_id)
    if group is None or group.user_id != user.id:
        raise HTTPException(404, detail={"code": "GROUP_NOT_FOUND", "message": "分组不存在"})

    groups = session.exec(select(WatchGroup).where(WatchGroup.user_id == user.id)).all()
    if len(groups) <= 1:
        raise HTTPException(400, detail={"code": "LAST_GROUP", "message": "至少保留一个分组"})

    fallback = next((g for g in groups if g.id != group_id), None)
    moved = 0
    if fallback is not None:
        for item in session.exec(select(WatchItem).where(WatchItem.group_id == group_id)).all():
            item.group_id = fallback.id or 0
            session.add(item)
            moved += 1
    session.delete(group)
    session.commit()
    return {"ok": True, "moved": moved, "fallback_group_id": fallback.id if fallback else 0}


class ItemIn(BaseModel):
    code: str = Field(min_length=6, max_length=12)
    group_id: int | None = None


@router.post("/items", dependencies=[Depends(signed_request)])
def add_item(
    payload: ItemIn,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> dict:
    """加为自选。已存在则移动到目标分组（幂等，客户端可以放心重复点）。"""
    if session.get(Stock, payload.code) is None:
        raise HTTPException(404, detail={"code": "UNKNOWN_SYMBOL", "message": "标的不存在"})

    group = _ensure_default_group(session, user.id or 0)
    if payload.group_id:
        target = session.get(WatchGroup, payload.group_id)
        if target is None or target.user_id != user.id:
            raise HTTPException(404, detail={"code": "GROUP_NOT_FOUND", "message": "分组不存在"})
        group = target

    existing = session.exec(
        select(WatchItem).where(WatchItem.user_id == user.id, WatchItem.code == payload.code)
    ).first()
    if existing is not None:
        if existing.group_id != group.id:
            existing.group_id = group.id or 0
            session.add(existing)
            session.commit()
        return {"ok": True, "watching": True, "group_id": group.id, "moved": True}

    session.add(WatchItem(user_id=user.id or 0, group_id=group.id or 0, code=payload.code))
    session.commit()
    return {"ok": True, "watching": True, "group_id": group.id, "moved": False}


@router.delete("/items/{code}", dependencies=[Depends(signed_request)])
def remove_item(
    code: str,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> dict:
    """取消自选。不存在也返回成功（幂等）。"""
    item = session.exec(
        select(WatchItem).where(WatchItem.user_id == user.id, WatchItem.code == code)
    ).first()
    if item is not None:
        session.delete(item)
        session.commit()
    return {"ok": True, "watching": False}


@router.get("/items", dependencies=[Depends(signed_request)])
def list_items(
    group_id: int | None = Query(default=None),
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> dict:
    """自选列表（默认只返回指定分组；不传 group_id 则返回全部分组的扁平结果）。

    返回里已拼好实时行情，客户端直接渲染即可。
    """
    rows = session.exec(select(WatchItem).where(WatchItem.user_id == user.id)).all()
    if group_id:
        rows = [r for r in rows if r.group_id == group_id]
    stocks = {s.code: s for s in session.exec(select(Stock)).all()}
    quotes = market_engine.quotes()

    items = []
    for r in rows:
        meta = stocks.get(r.code)
        q = quotes.get(r.code)
        if meta is None:
            continue
        items.append(
            {
                "code": r.code,
                "name": meta.name,
                "group_id": r.group_id,
                "industry": meta.industry,
                "board": meta.board,
                # 行情可能因引擎未注册而缺失，此时给出价格占位而不是丢行
                "price": (q or {}).get("price", meta.prev_close),
                "change": (q or {}).get("change", 0.0),
                "change_pct": (q or {}).get("change_pct", 0.0),
                "amount": (q or {}).get("amount", 0.0),
                "prev_close": (q or {}).get("prev_close", meta.prev_close),
            }
        )
    return {"items": items, "total": len(items)}


@router.get("/status/{code}", dependencies=[Depends(signed_request)])
def watch_status(
    code: str,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> dict:
    """查询单个标的是否已在自选（详情页「加为自选」按钮的初始状态）。"""
    item = session.exec(
        select(WatchItem).where(WatchItem.user_id == user.id, WatchItem.code == code)
    ).first()
    return {"code": code, "watching": item is not None, "group_id": item.group_id if item else 0}
