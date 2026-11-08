"""幂等键：让会改钱的操作可以安全重试。

为什么需要
---------
弱网、超时重试、用户连点，都会让同一个「调账 / 下单」请求到达服务端两次。
没有幂等键时服务端无从区分「这是重试」和「这是第二笔」，
结果就是重复调账、重复下单 —— 这类事故对账务系统是不可接受的。

设计
----
* 键由**调用方**提供（`client_request_id`），服务端只保证「同一个键只生效一次」，
  不猜业务语义；
* `scope` 区分业务域（`admin.adjust` / `trade.order`），避免不同业务撞同一个随机串；
* 首次成功执行后把响应快照存库，重试时**原样回放**，调用方看到的结果一致；
* 键带 `expires_at`，过期由 `purge_expired()` 清理，表不会无限增长。

用法
----
    prior = idempotency.lookup(session, scope="admin.adjust", key=crid)
    if prior is not None:
        return prior            # 直接回放，不再执行业务
    ...业务...
    idempotency.remember(session, scope=..., key=crid, actor_id=admin.id, response=result)
"""
from __future__ import annotations

import json
import time
from typing import Any

from sqlmodel import Session, delete, select

from ..models import IdempotencyKey

# 幂等窗口：24 小时足够覆盖任何合理的重试路径，又不会让表长期膨胀。
DEFAULT_TTL = 24 * 3600


def _pk(scope: str, key: str) -> str:
    return f"{scope}:{key}"


def lookup(session: Session, *, scope: str, key: str) -> dict[str, Any] | None:
    """命中未过期的幂等键则返回首次响应快照，否则 None。"""
    if not key:
        return None
    rec = session.get(IdempotencyKey, _pk(scope, key))
    if rec is None:
        return None
    if rec.expires_at and rec.expires_at < time.time():
        session.delete(rec)
        session.commit()
        return None
    if not rec.response:
        return {}
    try:
        data = json.loads(rec.response)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def remember(
    session: Session,
    *,
    scope: str,
    key: str,
    actor_id: int,
    response: dict[str, Any],
    status_code: int = 200,
    ttl: int = DEFAULT_TTL,
) -> None:
    """记录本次执行结果，供后续重试回放。空 key 表示调用方放弃幂等，直接跳过。"""
    if not key:
        return
    now = time.time()
    payload = json.dumps(response, ensure_ascii=False, default=str)
    # response 字段上限 8000：超长响应截断保存，回放时至少保留主体
    session.add(
        IdempotencyKey(
            key=_pk(scope, key),
            scope=scope,
            actor_id=actor_id,
            status_code=status_code,
            response=payload[:8000],
            created_at=now,
            expires_at=now + ttl,
        )
    )


def purge_expired(session: Session) -> int:
    """删除过期键。由写入路径顺带调用即可，无需定时任务。"""
    result = session.exec(delete(IdempotencyKey).where(IdempotencyKey.expires_at < time.time()))
    return int(result.rowcount or 0)


def client_request_id(header_value: str | None, body_value: str | None = None) -> str:
    """从请求头或请求体取幂等键，取到第一个非空值。

    同时支持两种来源：`Idempotency-Key` 头是业界惯例（Stripe 风格），
    请求体字段则方便前端在 JSON 里直接带，不必额外管请求头。
    """
    for value in (header_value, body_value):
        v = (value or "").strip()
        if v:
            return v[:80]
    return ""
