"""SQLModel 数据模型：账户、股票、行情、订单、成交、持仓、资金流水、风控、审计。

所有金额一律使用「分」为最小单位的 float（内部统一 4 位小数四舍五入存储），
避免 SQLite 浮点误差在账务上累积。
"""
from __future__ import annotations

import time
from datetime import datetime
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel


def now_ts() -> float:
    return time.time()


class Role(str, Enum):
    """客户端三种形态共用一套账号体系。"""

    RETAIL = "retail"          # 普通零售
    INSTITUTION = "institution"  # 机构专业
    ADMIN = "admin"            # 管理端


class AccountStatus(str, Enum):
    ACTIVE = "active"
    FROZEN = "frozen"   # 被风控冻结
    CLOSED = "closed"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    LIMIT = "limit"
    MARKET = "market"


class OrderStatus(str, Enum):
    PENDING = "pending"          # 已报，等待撮合
    PARTIAL = "partial"          # 部分成交
    FILLED = "filled"            # 全部成交
    CANCELLED = "cancelled"      # 已撤单
    REJECTED = "rejected"        # 废单


class User(SQLModel, table=True):
    """统一账户。资金/持仓归属账户，管理员亦为账户（role=admin）。"""

    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True, max_length=40)
    display_name: str = Field(default="", max_length=40)
    password_hash: str = Field(default="")
    role: Role = Field(default=Role.RETAIL, index=True)
    status: AccountStatus = Field(default=AccountStatus.ACTIVE, index=True)

    cash: float = Field(default=0.0)            # 可用资金
    frozen_cash: float = Field(default=0.0)     # 挂单冻结资金
    market_value: float = Field(default=0.0)    # 持仓市值（快照）
    total_asset: float = Field(default=0.0)     # 总资产（快照）
    realized_pnl: float = Field(default=0.0)    # 已实现盈亏
    fee_paid: float = Field(default=0.0)        # 累计费用

    # 机构账户专有额度
    credit_limit: float = Field(default=0.0)    # 可用融资额度
    risk_level: int = Field(default=1)          # 1 保守 / 2 稳健 / 3 进取

    fail_count: int = Field(default=0)          # 连续登录失败次数
    locked_until: float = Field(default=0.0)    # 锁定到期时间戳
    last_login_at: float = Field(default=0.0)

    created_at: float = Field(default_factory=now_ts)
    updated_at: float = Field(default_factory=now_ts)


class Stock(SQLModel, table=True):
    """标的元数据。价格上限/下限与最小报价单位决定价格格子（tick grid）。"""

    code: str = Field(primary_key=True, max_length=12)
    name: str = Field(max_length=24, index=True)
    board: str = Field(default="主板", max_length=12)   # 主板 / 创业板 / 科创板 / 北交所
    industry: str = Field(default="", max_length=24)
    hotword: str = Field(default="", max_length=40)     # 对应的抖音热词
    theme: str = Field(default="", max_length=80)

    prev_close: float = Field(default=10.0)
    limit_up: float = Field(default=11.0)
    limit_down: float = Field(default=9.0)
    tick_size: float = Field(default=0.01)
    lot_size: int = Field(default=100)
    total_shares: float = Field(default=1e9)   # 总股本（股）
    float_shares: float = Field(default=5e8)   # 流通股本（股）
    is_st: bool = Field(default=False)

    # 模拟器参数（研究结论落到参数上）
    init_price: float = Field(default=10.0)
    drift: float = Field(default=0.0)         # 年化漂移
    vol: float = Field(default=0.35)          # 年化波动率
    beta: float = Field(default=1.0)          # 对市场因子的敏感度
    liquidity: float = Field(default=1.0)     # 相对流动性（影响挂单深度）


class QuoteTick(SQLModel, table=True):
    """逐笔行情快照（tick 级），支撑分时与 K 线合成。"""

    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True, max_length=12)
    ts: float = Field(default_factory=now_ts, index=True)
    price: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    prev_close: float = 0.0
    volume: int = 0
    amount: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    bid_volume: int = 0
    ask_volume: int = 0
    turnover_rate: float = 0.0


class DailyBar(SQLModel, table=True):
    """日线（含历史回填与实时合成），用于 K 线接口。"""

    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True, max_length=12)
    trade_date: str = Field(index=True, max_length=10)
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    prev_close: float = 0.0
    volume: int = 0
    amount: float = 0.0


class Order(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    order_no: str = Field(index=True, unique=True, max_length=32)
    user_id: int = Field(index=True)
    code: str = Field(index=True, max_length=12)
    side: OrderSide
    order_type: OrderType = OrderType.LIMIT
    price: float = 0.0
    quantity: int = 0
    filled_quantity: int = 0
    avg_fill_price: float = 0.0
    status: OrderStatus = Field(default=OrderStatus.PENDING, index=True)
    frozen_amount: float = 0.0
    fee: float = 0.0
    reject_reason: str = Field(default="", max_length=120)
    source: str = Field(default="client", max_length=16)  # client / engine / system
    created_at: float = Field(default_factory=now_ts, index=True)
    updated_at: float = Field(default_factory=now_ts)


class Trade(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    trade_no: str = Field(index=True, unique=True, max_length=32)
    order_id: int = Field(index=True)
    order_no: str = Field(default="", max_length=32)
    user_id: int = Field(index=True)
    code: str = Field(index=True, max_length=12)
    side: OrderSide
    price: float = 0.0
    quantity: int = 0
    amount: float = 0.0
    fee: float = 0.0
    tax: float = 0.0
    transfer_fee: float = 0.0
    realized_pnl: float = 0.0
    created_at: float = Field(default_factory=now_ts, index=True)


class Position(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    code: str = Field(index=True, max_length=12)
    quantity: int = 0            # 总持仓
    available: int = 0           # T+1 可卖数量
    frozen: int = 0              # 挂单冻结
    avg_cost: float = 0.0
    last_price: float = 0.0
    market_value: float = 0.0
    unrealized_pnl: float = 0.0
    updated_at: float = Field(default_factory=now_ts)


class CashFlow(SQLModel, table=True):
    """资金流水（含冻结/解冻/成交/费用/分红），用于对账。"""

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    biz_type: str = Field(max_length=20)   # deposit/freeze/unfreeze/trade/fee/tax/adjust
    amount: float = 0.0
    balance_before: float = 0.0
    balance_after: float = 0.0
    ref_no: str = Field(default="", max_length=32)
    remark: str = Field(default="", max_length=120)
    created_at: float = Field(default_factory=now_ts, index=True)


class AuditLog(SQLModel, table=True):
    """管理端与安全相关操作留痕，满足上线审计需求。"""

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, index=True)
    username: str = Field(default="", max_length=40)
    action: str = Field(max_length=40, index=True)
    detail: str = Field(default="", max_length=400)
    ip: str = Field(default="", max_length=60)
    created_at: float = Field(default_factory=now_ts, index=True)


class SystemEvent(SQLModel, table=True):
    """系统级事件（开盘/收盘/熔断/风控冻结），供管理端时间线使用。"""

    id: Optional[int] = Field(default=None, primary_key=True)
    kind: str = Field(max_length=24, index=True)
    message: str = Field(default="", max_length=200)
    created_at: float = Field(default_factory=now_ts)


class RevokedToken(SQLModel, table=True):
    """登出后的令牌黑名单（按 jti 精确吊销）。"""

    id: Optional[int] = Field(default=None, primary_key=True)
    jti: str = Field(index=True, unique=True, max_length=40)
    expire_at: float = 0.0
    created_at: float = Field(default_factory=now_ts)


def utc_str(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or now_ts()).strftime("%Y-%m-%d %H:%M:%S")
