"""SQLModel 数据模型。

表结构（本轮重构，不再兼容旧版本数据库）
------------------------------------------------
· ``user``   —— 零售 + 机构共用的**一张**交易账户表，靠 ``role`` 区分；
· ``admin``  —— 管理员**独立**的一张表，不参与交易、没有资金与持仓；
· 交易类表（订单/成交/持仓/流水）只关联 ``user``，管理员天然不产生交易数据；
· 删除审计日志（AuditLog）与风险测评历史档案（RiskAssessment）：
  风险偏好直接记在 ``user`` 上（等级 + 总分 + 测评时间），不再单独留档。
· 新增 ``setting``：后台可在线修改的运行期配置；
· 新增 ``watch_group`` / ``watch_item``：自选股分组与成分。

金额一律以「元」为单位的 float，内部统一 4 位小数四舍五入存储，
避免 SQLite 浮点误差在账务上累积。
"""
from __future__ import annotations

import time
from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import Index
from sqlmodel import Field, SQLModel


def now_ts() -> float:
    return time.time()


class Role(str, Enum):
    """交易账户类型：只有零售与机构两种。管理员已迁到独立表。"""

    RETAIL = "retail"            # 普通零售
    INSTITUTION = "institution"  # 机构专业


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
    """交易账户（零售 + 机构共用）。

    为什么合成一张表：零售与机构除了 ``credit_limit``（融资额度）之外，
    字段、账务、撮合、结算流程完全一致，拆成两张表只会让交易链路到处 if/else。
    差异用一个可空语义的默认值 + ``role`` 表达即可。

    风险偏好：只存 ``risk_level`` / ``risk_assessed_at`` 两个字段，直接落在账户行上。
    不存总分、不存答题明细、不建测评历史表 —— 业务只需要「当前偏好是什么、
    什么时候测的」，其余都是自己造出来的数据负担。
    """

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

    # 佣金条件（逐账户可配）
    # None 表示「跟随角色默认」：零售万3不免5、机构万3免5（见 services/fees.py）。
    # 为什么拆成两个字段：「费率」与「最低佣金」是两个独立维度 —— 小额委托时
    # 零售会被 5 元下限抬高实际成本，机构不会；只存费率无法表达「免不免 5」。
    commission_rate: Optional[float] = Field(default=None)
    commission_min: Optional[float] = Field(default=None)

    # 机构账户专有额度（零售恒为 0）
    credit_limit: float = Field(default=0.0)

    # 风险偏好（0 = 从未测评）
    # 只存「等级 + 测评时间」：总分是问卷内部口径，对业务和用户都没有意义，
    # 存下来反而容易被误解成「分数越高越好」的评分体系；答题明细一律不落库。
    risk_level: int = Field(default=0)              # 0 未测评 / 1 保守 / 2 稳健 / 3 进取
    risk_assessed_at: float = Field(default=0.0)    # 最近一次测评时间（0 表示未测评）

    fail_count: int = Field(default=0)          # 连续登录失败次数
    locked_until: float = Field(default=0.0)    # 锁定到期时间戳
    last_login_at: float = Field(default=0.0)

    created_at: float = Field(default_factory=now_ts)
    updated_at: float = Field(default_factory=now_ts)


class Admin(SQLModel, table=True):
    """管理员账户（独立表）。

    与交易账户彻底分开的三个理由：
      1) 管理员不参与撮合、没有资金/持仓/流水，混在 ``user`` 里会让每个查询
         都要额外过滤 ``role != admin``，容易漏；
      2) 管理员登录后只调 ``/api/admin/*``，与交易链路的鉴权完全隔离，
         即使交易侧出问题也不会把后台带崩；
      3) 运营看「账户列表」时不需要再屏蔽管理员。
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True, max_length=40)
    display_name: str = Field(default="", max_length=40)
    password_hash: str = Field(default="")
    is_active: bool = Field(default=True)

    fail_count: int = Field(default=0)
    locked_until: float = Field(default=0.0)
    last_login_at: float = Field(default=0.0)

    created_at: float = Field(default_factory=now_ts)
    updated_at: float = Field(default_factory=now_ts)


class Setting(SQLModel, table=True):
    """运行期配置项：后台面板可改，改完立即生效（不需重启）。

    只存「允许运营调整」的白名单键，具体清单与校验见 ``app/runtime_config.py``。
    存字符串，读取时按目标类型转换，避免 SQLite 类型混乱。
    """

    key: str = Field(primary_key=True, max_length=40)
    # 长度放宽到 8000：`session_slots`（交易时段）是一段 JSON，
    # 200 字符只够存两三个时段，运营一改多段就会被静默截断。
    value: str = Field(default="", max_length=8000)
    updated_at: float = Field(default_factory=now_ts)
    updated_by: str = Field(default="", max_length=40)


class WatchGroup(SQLModel, table=True):
    """自选股分组（每个账户私有）。"""

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    name: str = Field(max_length=20)
    sort: int = Field(default=0)
    created_at: float = Field(default_factory=now_ts)


class WatchItem(SQLModel, table=True):
    """自选股成分：一支股票在同一账户下只属于一个分组（唯一约束见服务层）。"""

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    group_id: int = Field(index=True)
    code: str = Field(index=True, max_length=12)
    created_at: float = Field(default_factory=now_ts)


class Board(SQLModel, table=True):
    """板块（主板 / 创业板 / 科创板 / 北交所 …）。

    板块承载的是**规则**：涨跌幅上限、最小报价单位、申报数量单位。
    因此它不与 ``Stock`` 建外键，而是按名称关联 —— 标的改板只需换一个名字，
    涨跌停由板块表推算，不必在标的上再抄一份比例。
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True, max_length=12)
    limit_pct: float = Field(default=0.10)   # 涨跌幅上限，0.10 = ±10%
    tick_size: float = Field(default=0.01)   # 最小报价单位
    lot_size: int = Field(default=100)       # 申报数量单位（股）
    description: str = Field(default="", max_length=120)
    sort: int = Field(default=0)
    created_at: float = Field(default_factory=now_ts)


class Industry(SQLModel, table=True):
    """行业分类。与 ``Stock.industry`` 按名称关联，用于行业分布与筛选。"""

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True, max_length=24)
    description: str = Field(default="", max_length=120)
    sort: int = Field(default=0)
    created_at: float = Field(default_factory=now_ts)


class Stock(SQLModel, table=True):
    """标的元数据。价格上限/下限与最小报价单位决定价格格子（tick grid）。"""

    code: str = Field(primary_key=True, max_length=12)
    name: str = Field(max_length=24, index=True)
    board: str = Field(default="主板", max_length=12)   # 主板 / 创业板 / 科创板
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


class QuoteSnapshot(SQLModel, table=True):
    """行情状态的落库快照（每个标的一行，随时覆盖）。

    为什么必须有这张表：``SecurityState`` 全在内存里，进程一重启，昨收、
    最新价、当日高低、累计成交量全部回到注册时的初值。真实交易下这等于
    「重启一次，所有已发生的成交痕迹消失」——用户昨天在 10.50 成交的记录
    还在，行情却显示 10.00，账实直接对不上。

    每支标的只留最新一行（``code`` 作主键），写的是「当下这一天的状态」，
    不是历史序列：历史由 ``DailyBar``（日）和 ``MinuteFrame``（分时）承载。
    这样表行数恒等于标的数量，不会随时间膨胀。
    """

    code: str = Field(primary_key=True, max_length=12)
    trade_date: str = Field(default="", index=True, max_length=10)
    prev_close: float = 0.0
    price: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    volume: int = 0
    amount: float = 0.0
    updated_at: float = Field(default_factory=now_ts)


class Order(SQLModel, table=True):
    # 订单簿每次撮合都要按「标的 + 方向 + 状态」捞可成交挂单，rematch_pending
    # 每轮还会全表扫一遍在途单。单列索引只能命中标的，剩下两个条件仍要逐行判；
    # 复合索引让这条最热的查询直接走索引定位。
    __table_args__ = (
        Index("ix_order_book_lookup", "code", "side", "status", "order_type"),
        Index("ix_order_open_scan", "status", "order_type"),
    )

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
    # 对账按 order_id 分组求成交合计；风控按 user_id + 时间统计当日用量。
    __table_args__ = (
        Index("ix_trade_order", "order_id"),
        Index("ix_trade_user_time", "user_id", "created_at"),
        Index("ix_trade_user_code", "user_id", "code"),
    )

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
    # 对手方信息：一笔成交两端都是真实账户时，双方都要能查到「我是跟谁成交的」。
    # 对做市商成交（合成盘口）时为空 —— 没有真实对手方。
    counterparty_user_id: Optional[int] = Field(default=None, index=True)
    counterparty_order_id: Optional[int] = Field(default=None)
    counterparty_order_no: str = Field(default="", max_length=32)
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


class RevokedToken(SQLModel, table=True):
    """登出后的令牌黑名单（按 jti 精确吊销）。"""

    id: Optional[int] = Field(default=None, primary_key=True)
    jti: str = Field(index=True, unique=True, max_length=40)
    expire_at: float = 0.0
    created_at: float = Field(default_factory=now_ts)


def utc_str(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or now_ts()).strftime("%Y-%m-%d %H:%M:%S")


class MinuteFrame(SQLModel, table=True):
    """已收好的分钟线（持久化）。

    为什么必须落库：``SecurityState.day_frames`` 只是内存字典，进程重启即清空，
    而「五日分时」需要跨日拼接近五个交易日的分钟线。只靠内存时，重启后
    ``minute_series(days=5)`` 只能拿到当天那一段，图上就退化成「只有今天」。

    主键用 ``code:ts`` 复合键字符串，天然幂等：同一分钟重复归档只会覆盖同一行，
    不会产生重复柱。
    """

    __tablename__ = "minute_frame"

    key: str = Field(primary_key=True)      # f"{code}:{ts}"
    code: str = Field(index=True)
    ts: int = Field(index=True)             # 分钟起点（epoch 秒）
    day: str = Field(index=True)            # YYYY-MM-DD，跨日分组用
    open: float
    high: float
    low: float
    close: float
    volume: int
    amount: float
    segment: str = "continuous"
