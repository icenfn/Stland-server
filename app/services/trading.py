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
import logging
import time
from datetime import datetime

from sqlmodel import Session, select

from ..engine.book import Level, walk_book, vwap
from ..engine import orderbook, quote_source
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
from . import risk
from .events import hub, order_event

logger = logging.getLogger("stland.trading")
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
    """合成盘口（来自行情引擎）。hybrid 模式下用作「做市商」那一侧的兜底。"""
    depth = market_engine.depth(code) or {"bids": [], "asks": []}
    raw = depth["asks"] if side == "buy" else depth["bids"]
    return [Level(price=float(x["price"]), volume=int(x["volume"])) for x in raw]


def _book_levels_from_users(session: Session, code: str, side: str) -> list[Level]:
    """真实对手盘：其他用户挂在盘上的限价委托。"""
    rows = orderbook.quote_levels(session, code, side)
    return [Level(price=float(x["price"]), volume=int(x["volume"])) for x in rows]


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

    # ---- 风控限额 ----
    # 放在冻结之前：额度不够就不该先把钱冻上再回滚，冻结-解冻本身要写流水，
    # 白记两条账比直接拒绝代价大得多。
    try:
        risk.check_order(session, user, price=price, quantity=quantity)
    except risk.RiskError as exc:
        raise TradeError(str(exc), exc.code) from exc

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
    # 下单这一笔的结果也要推：客户端看到「部分成交」才知道还剩多少挂在盘上，
    # 否则它会以为整笔都成交了。
    hub.publish(
        user.id or 0,
        order_event(order, "filled" if order.filled_quantity else "accepted"),
    )
    return order


def match_mode() -> str:
    """当前撮合模式。每次现读配置，改完配置不必重启服务。"""
    from ..config import settings

    mode = str(getattr(settings, "match_mode", "hybrid") or "hybrid").lower()
    return mode if mode in ("book", "hybrid") else "hybrid"


def _settle_resting(
    session: Session,
    stock: Stock,
    resting: Order,
    price: float,
    quantity: int,
    settle_mode: str,
) -> None:
    """结算被吃掉的真实挂单（对手方）。

    这一步是「真实交易」和「跟随机数对赌」的分水岭：成交不再凭空生成
    一份持仓，而是把股票从卖方账户划到买方账户，资金反向划过去，
    双方成交价一致、手续费各收各的。
    """
    other = session.get(User, resting.user_id)
    if other is None:
        return

    amount = price * quantity
    pos = _get_position(session, other.id or 0, stock.code)

    if resting.side == OrderSide.SELL:
        if pos is None or pos.quantity < quantity:
            # 挂单时已冻结持仓，理论上不会走到这里；真发生说明数据不一致，
            # 宁可跳过这笔划转，也不能把持仓算成负数。
            return
        fee = calc_fee(amount, "sell", *resolve_commission(other))
        prev_cost = pos.avg_cost
        pos.quantity -= quantity
        pos.frozen = max(0, pos.frozen - quantity)
        pos.last_price = price
        if pos.quantity == 0:
            pos.avg_cost = 0.0
        pos.updated_at = time.time()
        session.add(pos)
        credit = amount - fee.total
        before = other.cash
        other.cash = round_money(other.cash + credit)
        _add_flow(
            session, other, "trade", credit, before, resting.order_no,
            f"{stock.code} 卖出成交",
        )
        pnl = (price - prev_cost) * quantity - fee.total
    else:
        if pos is None:
            pos = Position(
                user_id=other.id or 0, code=stock.code, quantity=0, available=0, frozen=0
            )
        fee = calc_fee(amount, "buy", *resolve_commission(other))
        new_qty = pos.quantity + quantity
        cost_new = pos.avg_cost * pos.quantity + amount + fee.total
        pos.avg_cost = round(cost_new / new_qty, 4) if new_qty else 0.0
        pos.quantity = new_qty
        # 与主动方同一套结算规则：T+0 买入即可卖，T+1 等日切解冻
        if settle_mode == "t0":
            pos.available += quantity
        pos.last_price = price
        pos.updated_at = time.time()
        session.add(pos)
        # 挂单时已冻结资金，这里把实际花费从冻结池核销（现金早已扣除）
        other.frozen_cash = round_money(
            max(0.0, other.frozen_cash - (amount + fee.total))
        )
        other.updated_at = time.time()
        session.add(other)
        pnl = 0.0

    session.add(
        Trade(
            trade_no=gen_no("T"),
            order_id=resting.id or 0,
            order_no=resting.order_no,
            user_id=other.id or 0,
            code=stock.code,
            side=resting.side,
            price=price,
            quantity=quantity,
            amount=round_money(amount),
            fee=fee.commission,
            tax=fee.stamp_tax,
            transfer_fee=fee.transfer_fee,
            realized_pnl=round_money(pnl),
            created_at=time.time(),
        )
    )

    filled = int(resting.filled_quantity or 0) + quantity
    prev_avg = resting.avg_fill_price or 0.0
    resting.avg_fill_price = (
        round((prev_avg * (filled - quantity) + price * quantity) / filled, 4)
        if filled
        else price
    )
    resting.filled_quantity = filled
    resting.fee = round_money((resting.fee or 0.0) + fee.total)
    resting.status = OrderStatus.FILLED if filled >= resting.quantity else OrderStatus.PARTIAL
    resting.updated_at = time.time()
    session.add(resting)

    # 被动成交的账户完全不知道自己的挂单被吃掉了 —— 它的下单请求早就返回了。
    # 这条事件是它唯一的实时感知途径。
    hub.publish(
        other.id or 0,
        order_event(
            resting,
            "filled" if resting.status == OrderStatus.FILLED else "partial",
            trigger="passive",
            last_price=price,
            last_quantity=quantity,
        ),
    )


def _match_order(session: Session, user: User, order: Order, stock: Stock, quote: dict) -> None:
    """撮合：先吃用户订单簿，剩余量再按撮合模式处理。

    原实现只吃 ``market_engine.depth()`` 的合成盘口 —— 对手盘是
    ``register()`` 时生成的随机五档，``walk_book()`` 又只读不扣减，
    等于无限流动性。也就是说用户是在和一段随机数对赌：机构卖出的股票
    凭空生成，零售买入的凭空消失，双方永远不可能真正成交。

    现在先撮真实挂单，一笔成交两端都是真实账户。剩余量由
    ``settings.match_mode`` 决定：

    * ``book``   —— 留在盘上等对手方，没人接就挂着（真实市场行为）；
    * ``hybrid`` —— 交给做市商（原合成盘口）成交，保持模拟盘原有手感。
    """
    settle_mode = current_session().settle
    taker_side = order.side
    limit_price = float(order.price) if order.order_type == OrderType.LIMIT else None

    # ---- 1) 真实对手盘：用户订单簿 ----
    plan = orderbook.match_against_book(
        session,
        stock.code,
        taker_side,
        order.quantity,
        limit_price,
        exclude_user_id=user.id,
        exclude_order_id=order.id,
    )
    fills: list[dict] = [
        {"price": m.price, "quantity": m.quantity, "resting": m.order} for m in plan
    ]
    got = sum(f["quantity"] for f in fills)

    # ---- 2) 剩余量：只有 hybrid 才找做市商 ----
    left = int(order.quantity) - got
    if left > 0 and match_mode() == "hybrid":
        levels = _book_levels(stock.code, "buy" if taker_side == OrderSide.BUY else "sell")
        if order.order_type == OrderType.LIMIT:
            took = walk_book(levels, left, float(order.price), taker_side.value)
        elif levels:
            # 市价单：以五档最深价为保护价，避免无限滑点
            protect = levels[min(4, len(levels) - 1)].price
            took = walk_book(levels, left, protect, taker_side.value)
        else:
            took = []
        for f in took:
            fills.append({"price": f.price, "quantity": f.quantity, "resting": None})

    filled_qty = sum(f["quantity"] for f in fills)
    if filled_qty <= 0:
        # 市价单在盘上没有对手就是废单，撤销并释放冻结；
        # 限价单则留在盘上等对手方（book 模式下这是常态）。
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

    for f in fills:
        price = float(f["price"])
        quantity = int(f["quantity"])
        amount = price * quantity
        fee = calc_fee(amount, taker_side.value, *resolve_commission(user))
        total_amount += amount
        total_fee += fee.commission
        total_tax += fee.stamp_tax
        total_transfer += fee.transfer_fee

        if taker_side == OrderSide.BUY:
            if pos is None:
                pos = Position(
                    user_id=user.id or 0, code=stock.code, quantity=0, available=0, frozen=0
                )
            new_qty = pos.quantity + quantity
            cost_prev = pos.avg_cost * pos.quantity
            cost_new = cost_prev + amount + fee.total
            pos.avg_cost = round(cost_new / new_qty, 4) if new_qty else 0.0
            pos.quantity = new_qty
            # 结算模式决定「当日买入能不能当日卖」：
            # · T+1 → 不计入可卖，次日日切时由 settle_all 解冻；
            # · T+0 / 调试 → 直接计入可卖，买入后立刻能挂卖单。
            if settle_mode == "t0":
                pos.available += quantity
            pos.last_price = price
            pos.updated_at = time.time()
            session.add(pos)
            user.cash = round_money(user.cash)  # 资金已在冻结时扣除
        else:
            if pos is None:
                continue
            pnl = (price - pos.avg_cost) * quantity - fee.total
            realized += pnl
            pos.quantity -= quantity
            pos.frozen = max(0, pos.frozen - quantity)
            pos.last_price = price
            if pos.quantity == 0:
                pos.avg_cost = 0.0
            pos.updated_at = time.time()
            session.add(pos)
            credit = amount - fee.total
            before = user.cash
            user.cash = round_money(user.cash + credit)
            _add_flow(
                session, user, "trade", credit, before, order.order_no,
                f"{stock.code} 卖出成交",
            )

        resting = f["resting"]
        session.add(
            Trade(
                trade_no=gen_no("T"),
                order_id=order.id or 0,
                order_no=order.order_no,
                user_id=user.id or 0,
                code=stock.code,
                side=taker_side,
                price=price,
                quantity=quantity,
                amount=round_money(amount),
                fee=fee.commission,
                tax=fee.stamp_tax,
                transfer_fee=fee.transfer_fee,
                realized_pnl=round_money(realized),
                # 成交对手方：对做市商成交时为空，对真人挂单时记录对方
                counterparty_user_id=(resting.user_id if resting is not None else None),
                counterparty_order_id=(resting.id if resting is not None else None),
                counterparty_order_no=(resting.order_no if resting is not None else ""),
                created_at=time.time(),
            )
        )

        # 成交回写行情：让价格由真实成交推动，而不是只等引擎 tick
        market_engine.apply_trade(stock.code, price, quantity)

        # 对手方是真实挂单时，同步完成它的入账与成交进度
        if resting is not None:
            _settle_resting(session, stock, resting, price, quantity, settle_mode)

    batch_avg = vwap([Level(price=f["price"], volume=f["quantity"]) for f in fills])
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
            keep_frozen = round_money(
                remain_amt + calc_fee(remain_amt, "buy", *resolve_commission(user)).total
            )

    session.add(order)
    _release_freeze(
        session, user, order, stock,
        total_amount + total_fee + total_tax + total_transfer, keep_frozen,
    )


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

    # 只捞出「还有量可撮」的委托：部分成交且已满量的单子状态可能还是 PARTIAL，
    # 但剩余为 0，扫进来只是白白多一轮查询与撮合。
    orders = [
        o for o in session.exec(
            select(Order).where(
                Order.status.in_([OrderStatus.PENDING, OrderStatus.PARTIAL]),
                Order.order_type == OrderType.LIMIT,
            )
        ).all()
        if int(o.quantity) - int(o.filled_quantity or 0) > 0
    ]
    if not orders:
        return 0

    filled_orders = 0
    mode = match_mode()
    # 行情源对象在循环外取一次：每单都重建一次纯属浪费，
    # 且同一批次内换源会让前后两单看到不同的盘口。
    source = quote_source.get_source()
    for order in orders:
        try:
            quote = source.quote_any(order.code)
            stock = session.get(Stock, order.code)
            user = session.get(User, order.user_id)
            if stock is None or user is None:
                continue

            # 涨跌停时不撮合，跳过即可（不要动冻结，避免资金来回搬）
            if quote is not None:
                last_price = float(quote["price"])
                if order.side == OrderSide.BUY and last_price >= stock.limit_up - 1e-9:
                    continue
                if order.side == OrderSide.SELL and last_price <= stock.limit_down + 1e-9:
                    continue
            elif mode == "hybrid":
                # hybrid 需要合成盘口，没有报价就撮不了
                continue
            # book 模式没有最新价也能撮：限价单之间只要价格交叉就该成交，
            # 这也是真实市场里价格发现的过程 —— 不需要外部行情先动。

            before = order.filled_quantity
            _match_order(session, user, order, stock, quote)
            if order.filled_quantity > before:
                # 只有真的成交才提交：没成交却每单一提交，等于行情循环每转一圈
                # 就写一遍库，在途单一多就把 SQLite 压满。
                session.commit()
                filled_orders += 1
            else:
                session.rollback()
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
    hub.publish(user.id or 0, order_event(order, "cancelled"))
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
    """日终结转：把持仓的可卖额度对齐到「总持仓 − 已冻结」。

    两种结算模式在这里统一收口：

    * **T+1**：日切正是「解锁」的时刻 —— 昨日买入的部分在这一刻变为可卖，
      所以用 ``available = quantity - frozen`` 一次性重算是对的；
    * **T+0**：买入时已经在撮合里把数量加进 ``available``，日切时同样重算
      也无害（结果与增量维护一致），只是把可能存在的漂移纠正回来。

    换句话说，日切永远做「可卖 = 总持仓 − 冻结」的全量重算，是幂等且安全的；
    T+0 与 T+1 的差别只体现在「买入当天」而不是日切。
    """
    fixed = 0
    broken = 0
    for pos in session.exec(select(Position)).all():
        try:
            target = max(0, pos.quantity - pos.frozen)
            if pos.available != target:
                pos.available = target
                session.add(pos)
                fixed += 1
            # 逐条提交：一条脏数据抛错时回滚只影响它自己，
            # 不会把已处理好的持仓一起回退。
            session.commit()
        except Exception:  # noqa: BLE001 - 单个持仓异常不能拖垮日终结算
            session.rollback()
            broken += 1
            logger.exception("日终结算失败：position id=%s", getattr(pos, "id", None))
    if fixed or broken:
        logger.info("日终结算：修正 %d 条持仓，跳过 %d 条异常", fixed, broken)
