"""费用与账务核算。

A 股实际收费规则（2023 年后）：
* 佣金：双向收取，万 2.5（可配），单笔最低 5 元；
* 印花税：仅卖出收取，万 5；
* 过户费：双向收取，万 0.1（沪深两市统一按成交金额收取）；
* 交易单位：买入最小 100 股（1 手）且为 100 股整数倍；卖出可零股（一次性卖出零股）。

账务要点：买入先按「委托价 × 数量 + 预估费用」冻结，成交后按实际价差退回；
卖出冻结的是持仓（T+1 可卖数量），成交后释放冻结并增加资金。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import settings


@dataclass
class FeeBreakdown:
    commission: float
    stamp_tax: float
    transfer_fee: float

    @property
    def total(self) -> float:
        return round(self.commission + self.stamp_tax + self.transfer_fee, 4)


def calc_fee(amount: float, side: str) -> FeeBreakdown:
    """amount 为成交金额；side=buy/sell。"""
    if amount <= 0:
        return FeeBreakdown(0.0, 0.0, 0.0)
    commission = max(amount * settings.commission_rate, settings.commission_min)
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


def max_buy_quantity(available_cash: float, price: float, lot: int = 100) -> int:
    """在给定资金与价格下可买入的最大整手数量（含费用缓冲）。"""
    if price <= 0 or available_cash <= 0:
        return 0
    gross = available_cash / (price * (1 + settings.commission_rate + settings.transfer_fee_rate))
    lots = int(gross // lot)
    # 逐手回退，保证含最低佣金后仍可负担
    while lots > 0:
        qty = lots * lot
        amount = qty * price
        fee = calc_fee(amount, "buy").total
        if amount + fee <= available_cash + 1e-6:
            return qty
        lots -= 1
    return 0
