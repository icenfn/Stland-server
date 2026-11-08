"""管理端路由的公共底座：唯一的 router 实例 + 共享依赖。

拆分前 `admin.py` 是一个 1226 行、40 个端点的文件。现在按域拆进同包的子模块，
但**所有子模块共享这一个 router**，因此 `/api/v1/admin` 的路径前缀、标签、
鉴权依赖和对外契约都不变，`main.py` 只 include 一次即可。

子模块通过 ``from ._common import *`` 复用这里的导入（模型、会话、引擎、服务），
好处是依赖只有一处定义：加一个模型不用改十个文件；代价是子模块里出现
未使用名字的概率变高，交由 ruff 的 F401 统一收敛。
"""
from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlmodel import Session, func, select

from ... import runtime_config
from ...config import settings
from ...db import get_session
from ...engine import index as index_engine
from ...engine.market import market_engine
from ...engine.session import (
    FILLABLE_PRESETS,
    HOLIDAY_PRESETS,
    PRESETS,
    current_session,
    holiday_preset_json,
    preset_slots_json,
)
from ...models import (
    Account,
    AccountStatus,
    Admin,
    CashFlow,
    DailyBar,
    Order,
    OrderStatus,
    Position,
    Role,
    Board,
    Industry,
    Stock,
    Trade,
    User,
    WatchGroup,
    WatchItem,
)
from ...security import hash_password, verify_password
from ...services import accounts, admin_session, idempotency, reconcile
from ...services.deps import admin_user, issue_admin_token

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

LOCK_SECONDS = 300


