"""交易服务：委托校验、资金/持仓冻结、撮合与结算。

撮合流程（对应 A 股连续竞价规则）：
1. 校验：交易时段、代码存在、价格在涨跌停内、数量符合最小交易单位、资金/持仓充足；
2. 冻结：买入冻结「委托价×数量+预估费用」，卖出冻结持仓数量（T+1）；
3. 撮合：以行情引擎盘口逐档吃单（限价单受限于委托价，市价单按五档保护价）；
4. 结算：按实际成交价重算费用，多冻结资金退回，更新持仓成本与已实现盈亏；
5. 未成交部分：限价单保留挂单（状态 pending/partial），市价单剩余自动撤销。

T+1 规则：当日买入的股票要到下一交易日才能卖出，故持仓拆分为 quantity / available。
"""
from __future__ import annotations

import secrets
import time
from datetime import datetime

from sqlmodel import Session, select

from ..engine.book import Level, walk_book, vwap
from ..engine.market import market_engine
from ..engine.session import current_session
from ..models import (
    AccountStatus,
    CashFlow,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Stock,
    Trade,
    User,
)
from .fees import calc_fee, max_buy_quantity, resolve_commission, round_money


class TradeError(Exception):
    """业务校验失败（对客户端返回 400）。"""

    def __init__(self, message: str, code: str = "TRADE_REJECTED") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def gen_no(prefix: str) -> str:
    return f"{prefix}{datetime.now().strftime('%y%m%d%H%M%S')}{secrets.token_hex(3).upper()}"


def _get_position(session: Session, user_id: int, code: str) -> Position | None:
    return session.exec(
        select(Position).where(Position.user_id == user_id, Position.code == code)
    ).first()


def _add_flow(
    session: Session, user: User, biz_type: str, amount: float, before: float, ref: str, remark: str
) -> None:
    session.add(
        CashFlow(
            user_id=user.id or 0,
            biz_type=biz_type,
            amount=round_money(amount),
            balance_before=round_money(before),
            balance_after=round_money(user.cash),
            ref_no=ref,
            remark=remark,
        )
    )


def _book_levels(code: str, side: str) -> list[Level]:
    depth = market_engine.depth(code) or {"bids": [], "asks": []}
    raw = depth["asks"] if side == "buy" else depth["bids"]
    return [Level(price=float(x["price"]), volume=int(x["volume"])) for x in raw]


def place_order(
    session: Session,
    user: User,
    *,
    code: str,
    side: str,
    quantity: int,
    price: float | None = None,
    order_type: str = "limit",
    source: str = "client",
) -> Order:
    """提交委托并立即撮合。返回落库后的订单。"""
    if user.status != AccountStatus.ACTIVE:
        raise TradeError("账户状态异常，无法交易", "ACCOUNT_LOCKED")

    session_state = current_session()
    if not session_state.can_trade:
        raise TradeError(f"当前为{session_state.label}，不接受委托", "MARKET_CLOSED")

    stock = session.get(Stock, code)
    if stock is None:
        raise TradeError("标的不存在", "UNKNOWN_SYMBOL")

    side_enum = OrderSide(side)
    type_enum = OrderType(order_type)

    quote = market_engine.quote(code)
    if quote is None:
        raise TradeError("行情未就绪，请稍后重试", "QUOTE_NOT_READY")

    last_price = float(quote["price"])
    tick = stock.tick_size
    lot = stock.lot_size

    # ---- 数量校验 ----
    if quantity <= 0:
        raise TradeError("委托数量必须大于 0", "INVALID_QUANTITY")
    if side_enum == OrderSide.BUY and quantity % lot != 0:
        raise TradeError(f"买入数量必须为 {lot} 股的整数倍", "INVALID_QUANTITY")
    if side_enum == OrderSide.SELL and quantity % lot != 0:
        pos = _get_position(session, user.id or 0, code)
        available = pos.available if pos else 0
        # 允许一次性卖出全部零股
        if not (pos and quantity == pos.quantity and quantity <= available):
            raise TradeError(f"卖出数量必须为 {lot} 股的整数倍（零股需一次性卖出）", "INVALID_QUANTITY")

    # ---- 价格校验 ----
    if type_enum == OrderType.LIMIT:
        if price is None or price <= 0:
            raise TradeError("限价委托必须提供有效价格", "INVALID_PRICE")
        price = round(price, 2)
        if abs(round(price / tick) * tick - price) > 1e-9:
            raise TradeError(f"委托价格必须是最小报价单位 {tick} 的整数倍", "INVALID_PRICE")
        if price > stock.limit_up + 1e-9:
            raise TradeError(f"委托价高于涨停价 {stock.limit_up:.2f}", "PRICE_OUT_OF_LIMIT")
        if price < stock.limit_down - 1e-9:
            raise TradeError(f"委托价低于跌停价 {stock.limit_down:.2f}", "PRICE_OUT_OF_LIMIT")
    else:
        price = last_price

    # ---- 涨跌停不可成交校验 ----
    if side_enum == OrderSide.BUY and last_price >= stock.limit_up - 1e-9:
        raise TradeError("该标的已涨停，暂时无法买入", "LIMIT_UP")
    if side_enum == OrderSide.SELL and last_price <= stock.limit_down + 1e-9:
        raise TradeError("该标的已跌停，暂时无法卖出", "LIMIT_DOWN")

    # ---- 冻结 ----
    frozen_amount = 0.0
    if side_enum == OrderSide.BUY:
        need = price * quantity
        fee_est = calc_fee(need, "buy", *resolve_commission(user)).total
        available_cash = user.cash + (user.credit_limit if user.role.value == "institution" else 0.0)
        if need + fee_est > available_cash + 1e-6:
            raise TradeError(
                f"可用资金不足：需要 {need + fee_est:,.2f} 元，可动用 {available_cash:,.2f} 元",
                "INSUFFICIENT_FUNDS",
            )
        before = user.cash
        frozen_amount = round_money(need + fee_est)
        user.cash = round_money(user.cash - frozen_amount)
        user.frozen_cash = round_money(user.frozen_cash + frozen_amount)
        user.updated_at = time.time()
        session.add(user)
        _add_flow(session, user, "freeze", -frozen_amount, before, "", f"{code} 买入冻结")
    else:
        pos = _get_position(session, user.id or 0, code)
        if pos is None or pos.available < quantity:
            have = pos.available if pos else 0
            raise TradeError(f"可卖数量不足：需要 {quantity} 股，可卖 {have} 股", "INSUFFICIENT_POSITION")
        pos.available -= quantity
        pos.frozen += quantity
        pos.updated_at = time.time()
        session.add(pos)

    order = Order(
        order_no=gen_no("O"),
        user_id=user.id or 0,
        code=code,
        side=side_enum,
        order_type=type_enum,
        price=price,
        quantity=quantity,
        status=OrderStatus.PENDING,
        frozen_amount=frozen_amount,
        source=source,
        created_at=time.time(),
        updated_at=time.time(),
    )
    session.add(order)
    session.flush()

    # ---- 撮合 ----
    _match_order(session, user, order, stock, quote)
    session.commit()
    session.refresh(order)
    return order


def _match_order(session: Session, user: User, order: Order, stock: Stock, quote: dict) -> None:
    """按盘口逐档撮合。limit_price 为 None 表示市价单（五档保护）。"""
    levels = _book_levels(stock.code, "buy" if order.side == OrderSide.BUY else "sell")
    if order.order_type == OrderType.LIMIT:
        limit_price = order.price
        fills = walk_book(levels, order.quantity, limit_price, order.side.value)
    else:
        # 市价单：以五档最深价为保护价，逐档吃单
        if not levels:
            fills = []
        else:
            protect = levels[min(4, len(levels) - 1)].price
            fills = walk_book(levels, order.quantity, protect, order.side.value)

    filled_qty = sum(f.quantity for f in fills)
    if filled_qty <= 0:
        if order.order_type == OrderType.MARKET:
            order.status = OrderStatus.CANCELLED
            order.reject_reason = "市价委托无对手盘，已撤销"
            order.updated_at = time.time()
            session.add(order)
            _release_freeze(session, user, order, stock, 0)
        return

    total_amount = 0.0
    total_fee = 0.0
    total_tax = 0.0
    total_transfer = 0.0
    realized = 0.0
    # 再撮合时订单可能已有部分成交，成交均价必须按「累计」加权，而不是只算这一批
    prev_filled = order.filled_quantity
    prev_avg = order.avg_fill_price or 0.0
    pos = _get_position(session, user.id or 0, stock.code)

    for fill in fills:
        amount = fill.price * fill.quantity
        fee = calc_fee(amount, order.side.value, *resolve_commission(user))
        total_amount += amount
        total_fee += fee.commission
        total_tax += fee.stamp_tax
        total_transfer += fee.transfer_fee

        if order.side == OrderSide.BUY:
            if pos is None:
                pos = Position(user_id=user.id or 0, code=stock.code, quantity=0, available=0, frozen=0)
            new_qty = pos.quantity + fill.quantity
            cost_prev = pos.avg_cost * pos.quantity
            cost_new = cost_prev + amount + fee.total
            pos.avg_cost = round(cost_new / new_qty, 4) if new_qty else 0.0
            pos.quantity = new_qty
            # T+1：当日买入不计入可卖
            pos.last_price = fill.price
            pos.updated_at = time.time()
            session.add(pos)
            user.cash = round_money(user.cash)  # 资金已在冻结时扣除
        else:
            if pos is None:
                continue
            pnl = (fill.price - pos.avg_cost) * fill.quantity - fee.total
            realized += pnl
            pos.quantity -= fill.quantity
            pos.frozen = max(0, pos.frozen - fill.quantity)
            pos.last_price = fill.price
            if pos.quantity == 0:
                pos.avg_cost = 0.0
            pos.updated_at = time.time()
            session.add(pos)
            credit = amount - fee.total
            before = user.cash
            user.cash = round_money(user.cash + credit)
            _add_flow(session, user, "trade", credit, before, order.order_no, f"{stock.code} 卖出成交")

        session.add(
            Trade(
                trade_no=gen_no("T"),
                order_id=order.id or 0,
                order_no=order.order_no,
                user_id=user.id or 0,
                code=stock.code,
                side=order.side,
                price=fill.price,
                quantity=fill.quantity,
                amount=round_money(amount),
                fee=fee.commission,
                tax=fee.stamp_tax,
                transfer_fee=fee.transfer_fee,
                realized_pnl=round_money(realized),
                created_at=time.time(),
            )
        )

    batch_avg = vwap([f for f in fills])
    total_filled = prev_filled + filled_qty
    order.avg_fill_price = (
        round((prev_avg * prev_filled + batch_avg * filled_qty) / total_filled, 4)
        if total_filled
        else batch_avg
    )
    order.filled_quantity = total_filled
    order.fee = round_money((order.fee or 0.0) + total_fee + total_tax + total_transfer)
    order.updated_at = time.time()

    keep_frozen = 0.0
    if order.filled_quantity >= order.quantity:
        order.status = OrderStatus.FILLED
    else:
        order.status = OrderStatus.PARTIAL
        if order.order_type == OrderType.MARKET:
            order.reject_reason = "市价委托剩余部分自动撤销"
            order.quantity = order.filled_quantity
            order.status = OrderStatus.FILLED
        else:
            # 限价单还有剩余：这部分继续挂着，冻结资金必须留在冻结池里
            remain_qty = order.quantity - order.filled_quantity
            remain_amt = order.price * remain_qty
            keep_frozen = round_money(remain_amt + calc_fee(remain_amt, "buy", *resolve_commission(user)).total)

    session.add(order)
    _release_freeze(session, user, order, stock, total_amount + total_fee + total_tax + total_transfer, keep_frozen)

    user.realized_pnl = round_money(user.realized_pnl + realized)
    user.fee_paid = round_money(user.fee_paid + order.fee)
    user.updated_at = time.time()
    session.add(user)


def _release_freeze(
    session: Session,
    user: User,
    order: Order,
    stock: Stock,
    spent: float,
    keep_frozen: float = 0.0,
) -> None:
    """成交后结算冻结账户：已花费部分核销出冻结池，未花费部分退回可用资金。

    `keep_frozen` 是「这笔委托还要继续挂着，必须留在冻结池里的钱」。
    部分成交时如果不保留它，剩余未成交部分就等于凭空多出可用资金 ——
    用户在下一次再撮合之前把这笔钱花掉，成交时就会造成资金透支。
    """
    if order.side != OrderSide.BUY or order.frozen_amount <= 0:
        return
    frozen = order.frozen_amount
    consumed = min(frozen, max(0.0, spent))
    keep = round_money(min(frozen - consumed, max(0.0, keep_frozen)))
    refund = round_money(frozen - consumed - keep)

    # 1) 已花费部分从冻结池核销（这部分资金已转化为持仓，不再属于任何资金池）
    user.frozen_cash = round_money(max(0.0, user.frozen_cash - consumed))

    # 2) 未花费且不再需要的差额退回可用资金
    if refund > 0:
        before = user.cash
        user.cash = round_money(user.cash + refund)
        user.frozen_cash = round_money(max(0.0, user.frozen_cash - refund))
        _add_flow(session, user, "unfreeze", refund, before, order.order_no, "成交后退回冻结资金")

    # 3) 继续挂单的部分保持冻结，供后续再撮合使用
    order.frozen_amount = keep
    user.updated_at = time.time()
    session.add(user)


def rematch_pending(session: Session) -> int:
    """把还挂着的限价委托重新撮合一次。

    为什么必须有这一步
    -----------------
    `place_order` 只在「下单那一瞬间」撮合。限价单如果当时价格没到，
    就会一直停在 pending —— 之后行情真的走到委托价，也没有任何人去重新撮合它，
    用户看到的就是「股价已经到我委托的价了，但就是不成交」。

    这里由后台行情循环在每个 tick 之后调用：把 pending / partial 的限价单
    全部按最新盘口再过一遍，价格够就成交，不够就原样留着。
    只处理限价单：市价单在下单时未成交的部分已经按规则撤销。
    """
    if not current_session().can_trade:
        return 0

    orders = session.exec(
        select(Order).where(
            Order.status.in_([OrderStatus.PENDING, OrderStatus.PARTIAL]),
            Order.order_type == OrderType.LIMIT,
        )
    ).all()
    if not orders:
        return 0

    filled_orders = 0
    for order in orders:
        try:
            quote = market_engine.quote(order.code)
            stock = session.get(Stock, order.code)
            user = session.get(User, order.user_id)
            if quote is None or stock is None or user is None:
                continue

            last_price = float(quote["price"])
            # 涨跌停时无法成交，跳过即可（不要动冻结，避免资金来回搬）
            if order.side == OrderSide.BUY and last_price >= stock.limit_up - 1e-9:
                continue
            if order.side == OrderSide.SELL and last_price <= stock.limit_down + 1e-9:
                continue

            before = order.filled_quantity
            _match_order(session, user, order, stock, quote)
            session.commit()
            if order.filled_quantity > before:
                filled_orders += 1
        except Exception:  # 单笔异常不能拖垮整个再撮合批次
            session.rollback()
            continue

    return filled_orders


def cancel_order(session: Session, user: User, order_id: int) -> Order:
    order = session.get(Order, order_id)
    if order is None or order.user_id != (user.id or 0):
        raise TradeError("委托不存在", "ORDER_NOT_FOUND")
    if order.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
        raise TradeError("该委托已终结，无法撤单", "ORDER_FINAL")

    if order.side == OrderSide.BUY:
        before = user.cash
        remain = order.frozen_amount
        if remain > 0:
            user.cash = round_money(user.cash + remain)
            user.frozen_cash = round_money(max(0.0, user.frozen_cash - remain))
            user.updated_at = time.time()
            session.add(user)
            _add_flow(session, user, "unfreeze", remain, before, order.order_no, "撤单解冻")
        order.frozen_amount = 0.0
    else:
        pos = _get_position(session, user.id or 0, order.code)
        remain_qty = order.quantity - order.filled_quantity
        if pos and remain_qty > 0:
            pos.available += remain_qty
            pos.frozen = max(0, pos.frozen - remain_qty)
            pos.updated_at = time.time()
            session.add(pos)

    order.status = OrderStatus.CANCELLED
    order.updated_at = time.time()
    session.add(order)
    session.commit()
    session.refresh(order)
    return order


def settle_positions(session: Session, user: User) -> dict:
    """按最新行情刷新持仓市值与总资产（供查询接口调用）。"""
    positions = session.exec(select(Position).where(Position.user_id == user.id)).all()
    market_value = 0.0
    for pos in positions:
        quote = market_engine.quote(pos.code)
        price = float(quote["price"]) if quote else pos.last_price
        pos.last_price = price
        pos.market_value = round_money(pos.quantity * price)
        pos.unrealized_pnl = round_money((price - pos.avg_cost) * pos.quantity)
        market_value += pos.market_value
        session.add(pos)
    user.market_value = round_money(market_value)
    user.total_asset = round_money(user.cash + user.frozen_cash + market_value)
    user.updated_at = time.time()
    session.add(user)
    return {"market_value": user.market_value, "total_asset": user.total_asset}


def settle_all(session: Session) -> None:
    """日终结转：把当日买入的持仓转为可卖（T+1），并把日线落库。"""
    for pos in session.exec(select(Position)).all():
        pos.available = pos.quantity - pos.frozen
        session.add(pos)
    session.commit()
