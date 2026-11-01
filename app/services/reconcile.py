"""账务对账：把「订单 / 成交 / 持仓 / 资金」四张表相互校验。

为什么需要它
-----------
真实交易里资金与持仓是「多步改写」出来的：下单冻结、成交核销、撤单解冻、
被动成交划转，每一步都改动不同表的字段。任何一步漏写（异常中断、并发覆盖、
补丁改错），单看某一张表都自洽，只有把几张表放一起才能看出对不上。

四条校验线（每条都是一个必须恒成立的等式）
----------------------------------------
1. **委托成交量 = 该委托的成交明细之和**
   ``order.filled_quantity == Σ Trade.quantity (by order_id)``
   对不上说明有成交没回写进度，或进度被重复累加。

2. **持仓数量 = 该标的成交净额**
   ``pos.quantity == Σ 买入量 − Σ 卖出量``
   对不上说明股票没有真正从卖方划到买方（凭空生成或多划）。

3. **冻结资金 = 在途买单的冻结额之和**
   ``user.frozen_cash == Σ open_buy_order.frozen_amount``
   对不上说明有委托终结却忘了解冻，或解冻了还在挂的单。

4. **成交金额 = 价 × 量**，且**双面成交必须配对**
   同一笔撮合的两端应有同 code / 同价 / 同量、方向相反的成交记录。
   配对不上说明只有一方入了账 —— 这是最严重的一类：钱或券凭空增减。

关于容差：金额按分位比较（``AMOUNT_TOL``），数量必须严格相等。
浮点加减必然有尾差，用绝对相等去比一万条数据只会全是误报。
"""
from __future__ import annotations

import logging
from collections import defaultdict

from sqlalchemy import func
from sqlmodel import Session, select

from ..models import CashFlow, Order, OrderSide, OrderStatus, Position, Trade, User

logger = logging.getLogger("stland.reconcile")

#: 金额比较容差（元）。一分钱以内视为一致。
AMOUNT_TOL = 0.011

#: 仍会占用资金/持仓的委托状态
OPEN_STATUSES = (OrderStatus.PENDING, OrderStatus.PARTIAL)


class Finding(dict):
    """一条对账差异。用 dict 子类是为了直接丢进 JSON 响应。"""

    def __init__(self, check: str, level: str, subject: str, detail: str, **extra) -> None:
        super().__init__(
            check=check, level=level, subject=subject, detail=detail, **extra
        )


def _q(value: float) -> float:
    return round(float(value or 0.0), 2)


def check_order_fills(session: Session) -> list[Finding]:
    """1) 每笔委托的成交进度是否等于其成交明细之和。"""
    sums = {
        int(oid): (int(qty or 0), float(amt or 0.0))
        for oid, qty, amt in session.exec(
            select(Trade.order_id, func.sum(Trade.quantity), func.sum(Trade.amount))
            .group_by(Trade.order_id)
        ).all()
    }
    out: list[Finding] = []
    for order in session.exec(select(Order)).all():
        got_qty, got_amt = sums.get(int(order.id or 0), (0, 0.0))
        if int(order.filled_quantity or 0) != got_qty:
            out.append(
                Finding(
                    "order_fill_mismatch", "error", f"order:{order.order_no}",
                    f"委托记录成交 {int(order.filled_quantity or 0)} 股，"
                    f"成交明细合计 {got_qty} 股",
                    order_no=order.order_no, order_qty=int(order.filled_quantity or 0),
                    trade_qty=got_qty,
                )
            )
        # 委托上累计的费用（佣金+印花税+过户费）应与成交明细一致
        order_fee = float(order.fee or 0.0)
        if order_fee and got_amt and abs(order_fee) > 0:
            fees = session.exec(
                select(
                    func.coalesce(func.sum(Trade.fee), 0.0)
                    + func.coalesce(func.sum(Trade.tax), 0.0)
                    + func.coalesce(func.sum(Trade.transfer_fee), 0.0)
                ).where(Trade.order_id == (order.id or 0))
            ).one()
            if abs(float(fees or 0.0) - order_fee) > AMOUNT_TOL:
                out.append(
                    Finding(
                        "order_fee_mismatch", "warning", f"order:{order.order_no}",
                        f"委托费用 {order_fee:.2f} 元，成交明细费用合计 {float(fees or 0):.2f} 元",
                        order_no=order.order_no,
                    )
                )
    return out


def check_positions(session: Session) -> list[Finding]:
    """2) 持仓数量是否等于该账户在该标的上的成交净额。"""
    net: dict[tuple[int, str], int] = defaultdict(int)
    for t in session.exec(select(Trade)).all():
        sign = 1 if t.side == OrderSide.BUY else -1
        net[(int(t.user_id), t.code)] += sign * int(t.quantity or 0)

    out: list[Finding] = []
    positions = session.exec(select(Position)).all()
    seen: set[tuple[int, str]] = set()
    for pos in positions:
        key = (int(pos.user_id), pos.code)
        seen.add(key)
        if int(pos.quantity or 0) != net.get(key, 0):
            out.append(
                Finding(
                    "position_mismatch", "error", f"user:{pos.user_id}/{pos.code}",
                    f"持仓 {int(pos.quantity or 0)} 股，成交净额 {net.get(key, 0)} 股",
                    user_id=int(pos.user_id), code=pos.code,
                    position_qty=int(pos.quantity or 0), traded_qty=net.get(key, 0),
                )
            )
    # 有成交却没有持仓行：股票划出去了但持仓行被删/没建
    for key, qty in net.items():
        if key not in seen and qty != 0:
            out.append(
                Finding(
                    "position_missing", "error", f"user:{key[0]}/{key[1]}",
                    f"有成交净额 {qty} 股，但没有对应持仓记录",
                    user_id=key[0], code=key[1], traded_qty=qty,
                )
            )
    return out


def check_frozen_cash(session: Session) -> list[Finding]:
    """3) 冻结资金是否等于在途买单的冻结额之和。"""
    sums = {
        int(uid): float(amt or 0.0)
        for uid, amt in session.exec(
            select(Order.user_id, func.sum(Order.frozen_amount))
            .where(Order.side == OrderSide.BUY)
            .where(Order.status.in_(OPEN_STATUSES))
            .group_by(Order.user_id)
        ).all()
    }
    out: list[Finding] = []
    for user in session.exec(select(User)).all():
        expect = sums.get(int(user.id or 0), 0.0)
        actual = float(user.frozen_cash or 0.0)
        if abs(_q(actual) - _q(expect)) > AMOUNT_TOL:
            out.append(
                Finding(
                    "frozen_cash_mismatch", "warning", f"user:{user.username}",
                    f"冻结资金 {actual:.2f} 元，在途买单冻结额合计 {expect:.2f} 元",
                    user_id=int(user.id or 0), frozen_cash=_q(actual), expected=_q(expect),
                )
            )
    return out


def check_frozen_position(session: Session) -> list[Finding]:
    """3b) 持仓冻结是否等于在途卖单的未成交余量之和。"""
    need: dict[tuple[int, str], int] = defaultdict(int)
    for o in session.exec(
        select(Order).where(Order.side == OrderSide.SELL).where(Order.status.in_(OPEN_STATUSES))
    ).all():
        remain = max(0, int(o.quantity or 0) - int(o.filled_quantity or 0))
        need[(int(o.user_id), o.code)] += remain

    out: list[Finding] = []
    for pos in session.exec(select(Position)).all():
        key = (int(pos.user_id), pos.code)
        expect = need.get(key, 0)
        if int(pos.frozen or 0) != expect:
            out.append(
                Finding(
                    "frozen_qty_mismatch", "warning", f"user:{pos.user_id}/{pos.code}",
                    f"持仓冻结 {int(pos.frozen or 0)} 股，在途卖单未成交 {expect} 股",
                    user_id=int(pos.user_id), code=pos.code,
                    frozen_qty=int(pos.frozen or 0), expected=expect,
                )
            )
    return out


def check_trade_amounts(session: Session) -> list[Finding]:
    """4a) 每笔成交的金额是否等于价 × 量。"""
    out: list[Finding] = []
    for t in session.exec(select(Trade)).all():
        expect = float(t.price or 0.0) * int(t.quantity or 0)
        if abs(_q(float(t.amount or 0.0)) - _q(expect)) > AMOUNT_TOL:
            out.append(
                Finding(
                    "trade_amount_mismatch", "error", f"trade:{t.trade_no}",
                    f"成交金额 {float(t.amount or 0):.2f} 元，价×量 {expect:.2f} 元",
                    trade_no=t.trade_no,
                )
            )
    return out


def check_double_sided(session: Session) -> list[Finding]:
    """4b) 双面成交是否配对：买卖两端应有同价同量、方向相反的记录。

    这是最该盯的一条。只有一方入账时，钱或券就凭空增减了 —— 而且两张表
    各自都自洽，不做配对校验永远发现不了。
    """
    out: list[Finding] = []
    trades = session.exec(select(Trade)).all()

    # 按 (order_id 对) 聚合：主动方带 counterparty_order_id，被动方 order_id 就是对方
    by_order: dict[int, list[Trade]] = defaultdict(list)
    for t in trades:
        by_order[int(t.order_id or 0)].append(t)

    for t in trades:
        cp_oid = t.counterparty_order_id
        cp_uid = t.counterparty_user_id
        if not cp_oid or not cp_uid:
            continue  # 与做市商成交，没有真实对手方，不参与配对
        mates = [
            m for m in by_order.get(int(cp_oid), ())
            if int(m.user_id) == int(cp_uid)
            and m.code == t.code
            and m.side != t.side
            and int(m.quantity or 0) == int(t.quantity or 0)
            and abs(_q(float(m.price or 0)) - _q(float(t.price or 0))) <= AMOUNT_TOL
        ]
        if not mates:
            out.append(
                Finding(
                    "double_sided_missing", "error", f"trade:{t.trade_no}",
                    f"成交 {t.trade_no} 记录了对手方 {cp_uid}/#{cp_oid}，"
                    f"但对方没有同价同量的反向成交",
                    trade_no=t.trade_no, counterparty_user_id=int(cp_uid),
                    counterparty_order_id=int(cp_oid),
                )
            )
    return out


def check_cashflow_continuity(session: Session) -> list[Finding]:
    """5) 资金流水是否首尾相接：每条流水的期初余额应等于上一条的期末余额。"""
    out: list[Finding] = []
    rows = session.exec(
        select(CashFlow).order_by(CashFlow.user_id, CashFlow.created_at, CashFlow.id)
    ).all()
    last: dict[int, float] = {}
    for f in rows:
        uid = int(f.user_id)
        if f.biz_type in ("freeze", "unfreeze"):
            # 冻结/解冻是「可用 ↔ 冻结」的内部搬移，可用资金确实在变，
            # 所以它们也参与衔接；但不改变「可用 + 冻结」这个总量。
            pass
        prev = last.get(uid)
        if prev is not None and abs(_q(float(f.balance_before or 0)) - _q(prev)) > AMOUNT_TOL:
            out.append(
                Finding(
                    "cashflow_break", "warning", f"flow:{f.id}",
                    f"流水 #{f.id}（{f.biz_type}）期初 {float(f.balance_before or 0):.2f} 元，"
                    f"上一笔期末 {prev:.2f} 元，中间有未记账的资金变动",
                    flow_id=int(f.id or 0), user_id=uid, biz_type=f.biz_type,
                )
            )
        last[uid] = float(f.balance_after or 0.0)
    return out


CHECKS = (
    check_order_fills,
    check_positions,
    check_frozen_cash,
    check_frozen_position,
    check_trade_amounts,
    check_double_sided,
    check_cashflow_continuity,
)


def run_all(session: Session) -> dict:
    """跑全部校验，返回汇总报告。

    单项校验抛异常不中断整体：对账的价值就在于「其余项还能跑出来」，
    让一个脏数据点把整份报告打断，反而看不到全貌。
    """
    findings: list[Finding] = []
    stats: dict[str, int] = {}
    for fn in CHECKS:
        name = fn.__name__.removeprefix("check_")
        try:
            got = fn(session)
        except Exception as exc:  # noqa: BLE001 - 单项失败不拖垮整体
            logger.exception("对账项 %s 执行失败", name)
            findings.append(
                Finding(name, "error", "system", f"校验执行失败：{exc}")
            )
            stats[name] = -1
            continue
        stats[name] = len(got)
        findings.extend(got)

    errors = sum(1 for f in findings if f.get("level") == "error")
    warnings = sum(1 for f in findings if f.get("level") == "warning")
    return {
        "ok": errors == 0,
        "checked_at": __import__("time").time(),
        "counts": {"error": errors, "warning": warnings, "total": len(findings)},
        "per_check": stats,
        "findings": findings[:200],
        "truncated": len(findings) > 200,
    }
