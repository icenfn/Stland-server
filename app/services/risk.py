"""委托风控：单笔金额上限与单日累计成交上限。

为什么放在服务层而不是撮合里：撮合只该回答「这笔委托能不能和盘口对上」；
「这个账户今天还能下多少」是账户维度的规则，与订单簿无关。两者混在一起，
被真实对手吃掉多少（撮合）和允不允许下单（风控）就纠缠不清了。

按账户类型分档：机构的单笔/单日额度天然比零售高一个量级，共用一个阈值
等于要么卡死机构（按零售算）、要么放过零售（按机构算）。额度填 0 表示该档
不限制 —— 用 0 而不是「空」是因为配置项统一走 float 解析，0 能表达「关掉」。

被拒时抛 :class:`RiskError` 而不是直接抛 ``TradeError``：本模块不该反向依赖
下单链路，否则 trading → risk → trading 形成循环导入。由调用方转成对外错误码。
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime

from sqlalchemy import func
from sqlmodel import Session, select

from ..config import settings
from ..engine.session import CN_ZONE, current_session
from ..models import Order, OrderStatus, Role, Trade

#: 仍占用额度的委托状态：已报与部分成交的剩余量还没真正成交，但钱/券已锁定，
#: 按「在途」计入当日用量。否则同一笔钱可以在限额内反复挂单，上限形同虚设。
OPEN_STATUSES = (OrderStatus.PENDING, OrderStatus.PARTIAL)


class RiskError(Exception):
    """风控拒绝。``code`` 供接口层转成对外的错误码。"""

    def __init__(self, message: str, code: str = "RISK_REJECTED") -> None:
        super().__init__(message)
        self.code = code


def limits_for(role: Role | str | None) -> tuple[float, float]:
    """该账户类型生效的（单笔上限, 单日上限），单位元；0 表示不限制。"""
    institution = str(getattr(role, "value", role) or "") == Role.INSTITUTION.value
    if institution:
        return (
            float(settings.risk_max_order_institution),
            float(settings.risk_daily_limit_institution),
        )
    return (
        float(settings.risk_max_order_retail),
        float(settings.risk_daily_limit_retail),
    )


def _day_start_ts() -> float:
    """当前交易日的起点时间戳。

    用 ``current_session().trade_date`` 而不是「现在减 24 小时」：交易日不等于
    自然日，隔夜与跨周末会算错边界，凌晨的委托会被算进前一天。
    """
    state = current_session()
    try:
        day = date.fromisoformat(state.trade_date)
    except (ValueError, TypeError):
        day = datetime.now(CN_ZONE).date()
    return datetime.combine(day, dtime(0, 0), tzinfo=CN_ZONE).timestamp()


def daily_used(session: Session, user_id: int) -> float:
    """当日已占用额度 = 已成交金额 + 在途委托金额。"""
    start = _day_start_ts()

    filled = session.exec(
        select(func.coalesce(func.sum(Trade.amount), 0.0))
        .where(Trade.user_id == user_id)
        .where(Trade.created_at >= start)
    ).one()

    open_orders = session.exec(
        select(Order)
        .where(Order.user_id == user_id)
        .where(Order.created_at >= start)
        .where(Order.status.in_(OPEN_STATUSES))
    ).all()
    # 在途只算未成交部分：已成交的量上面已经按金额统计过，
    # 再按整笔委托算一遍等于把同一笔钱数两次。
    resting = sum(
        float(o.price) * max(0, int(o.quantity) - int(o.filled_quantity or 0))
        for o in open_orders
    )
    return float(filled or 0.0) + resting


def check_order(
    session: Session,
    user: object,
    *,
    price: float,
    quantity: int,
) -> None:
    """下单前校验风控限额，超限抛 :class:`RiskError`。

    只校验「金额」维度。数量取整、价格格子、涨跌停、资金与持仓充足性都在
    下单链路里各自判过了，这里不重复。
    """
    notional = float(price) * int(quantity)
    if notional <= 0:
        return

    max_order, daily_limit = limits_for(getattr(user, "role", None))

    if max_order > 0 and notional > max_order + 1e-6:
        raise RiskError(
            f"单笔委托金额 {notional:,.2f} 元超过上限 {max_order:,.0f} 元",
            "ORDER_LIMIT_EXCEEDED",
        )

    if daily_limit > 0:
        user_id = int(getattr(user, "id", 0) or 0)
        used = daily_used(session, user_id)
        if used + notional > daily_limit + 1e-6:
            raise RiskError(
                f"当日累计金额将达 {used + notional:,.2f} 元，超过上限 {daily_limit:,.0f} 元"
                f"（已用 {used:,.2f} 元）",
                "DAILY_LIMIT_EXCEEDED",
            )
