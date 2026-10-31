"""费用与账务核算。

A 股实际收费规则（2023 年后）：
* 佣金：双向收取，费率与最低佣金**逐账户可配**；单笔不足最低佣金时按最低收；
* 印花税：仅卖出收取，万 5；
* 过户费：双向收取，万 0.1（沪深两市统一按成交金额收取）；
* 交易单位：买入最小 100 股（1 手）且为 100 股整数倍；卖出可零股。

佣金分级的默认口径（本轮新增）
--------------------------
券商对不同客户给不同条件，这是真实业务里最容易被忽略的差异：

* 零售：万 3 费率，**不免 5**（单笔佣金不足 5 元按 5 元收）；
* 机构：万 3 费率，**免 5**（按实际费率收取，无 5 元下限）。

「免不免 5」不是费率高低的差别，而是**最低收费**的差别：小额委托时，
零售每笔被最低佣金抬高实际成本，机构不会。因此账户上必须分别存
「费率」和「最低佣金」两个值，只存费率无法表达这件事。

存储约定：账户上这两个字段为 `None` 时表示「按角色默认」，
赋值后以账户自身为准 —— 这样既能让运营给单个客户单独议价，
又不会把默认值固化到历史数据里（改默认口径时老账户跟着变）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..config import settings
from ..models import Role

# 角色默认佣金口径 -----------------------------------------------------------
# 万 3 = 0.0003
ROLE_COMMISSION: dict[Role, tuple[float, float]] = {
    # 零售：万 3，最低 5 元（不免 5）
    Role.RETAIL: (0.0003, 5.0),
    # 机构：万 3，最低 0 元（免 5）
    Role.INSTITUTION: (0.0003, 0.0),
}


def role_commission(role: Optional[Role]) -> tuple[float, float]:
    """角色默认（费率, 最低佣金）。未知角色按零售处理，避免算成零佣金。"""
    if role is None:
        return ROLE_COMMISSION[Role.RETAIL]
    return ROLE_COMMISSION.get(role, ROLE_COMMISSION[Role.RETAIL])


def resolve_commission(account: object | None) -> tuple[float, float]:
    """从账户上取出实际生效的（费率, 最低佣金）。

    账户字段为 None 表示「跟随角色默认」；显式赋值则以账户为准。
    传入 None（未登录/无账户上下文）时退回全局配置，保持旧调用兼容。
    """
    if account is None:
        return (float(settings.commission_rate), float(settings.commission_min))
    role = getattr(account, "role", None)
    base_rate, base_min = role_commission(role if isinstance(role, Role) else None)
    rate = getattr(account, "commission_rate", None)
    min_fee = getattr(account, "commission_min", None)
    return (
        float(base_rate if rate is None else rate),
        float(base_min if min_fee is None else min_fee),
    )


@dataclass
class FeeBreakdown:
    commission: float
    stamp_tax: float
    transfer_fee: float

    @property
    def total(self) -> float:
        return round(self.commission + self.stamp_tax + self.transfer_fee, 4)


def calc_fee(
    amount: float,
    side: str,
    rate: float | None = None,
    min_fee: float | None = None,
) -> FeeBreakdown:
    """amount 为成交金额；side=buy/sell。

    rate / min_fee 由调用方从账户上取（见 ``resolve_commission``）；
    不传则退回全局配置，保证既有调用点与测试无需改动即可运行。
    """
    if amount <= 0:
        return FeeBreakdown(0.0, 0.0, 0.0)
    use_rate = float(settings.commission_rate if rate is None else rate)
    use_min = float(settings.commission_min if min_fee is None else min_fee)
    commission = amount * use_rate
    # 「免 5」等价于最低佣金为 0；只有最低佣金大于 0 时才托底
    if use_min > 0:
        commission = max(commission, use_min)
    stamp_tax = amount * settings.stamp_tax_rate if side == "sell" else 0.0
    transfer_fee = amount * settings.transfer_fee_rate
    return FeeBreakdown(
        commission=round(commission, 4),
        stamp_tax=round(stamp_tax, 4),
        transfer_fee=round(transfer_fee, 4),
    )


def round_money(value: float) -> float:
    """金额统一保留 4 位小数，避免浮点漂移累积。"""
    return round(value + 1e-12, 4)


def max_buy_quantity(
    available_cash: float,
    price: float,
    lot: int = 100,
    rate: float | None = None,
    min_fee: float | None = None,
) -> int:
    """在给定资金与价格下可买入的最大整手数量（含费用缓冲）。"""
    if price <= 0 or available_cash <= 0:
        return 0
    use_rate = float(settings.commission_rate if rate is None else rate)
    gross = available_cash / (price * (1 + use_rate + settings.transfer_fee_rate))
    lots = int(gross // lot)
    # 逐手回退，保证含最低佣金后仍可负担
    while lots > 0:
        qty = lots * lot
        amount = qty * price
        fee = calc_fee(amount, "buy", rate=rate, min_fee=min_fee).total
        if amount + fee <= available_cash + 1e-6:
            return qty
        lots -= 1
    return 0
