"""启动装配：内置标的注册、历史回填、种子账户、后台行情循环。"""
from __future__ import annotations

import threading
import time

from sqlmodel import Session, select

from .config import settings
from .db import engine, init_db, session_scope
from .engine.market import market_engine
from .engine.seed import LIMIT_BY_BOARD, SEED_STOCKS, SEED_USERS
from .engine.session import current_session
from .models import AccountStatus, DailyBar, Role, Stock, SystemEvent, User
from .security import hash_password
from .services.trading import settle_all


def ensure_stocks(session: Session) -> int:
    """把 28 支内置标的作为「默认标的」注入数据库（幂等，且绝不覆盖已有记录）。

    为什么改成这样
    --------------
    旧实现每次启动都会把 init_price / 涨跌停 / vol / beta 等参数按种子文件重写一遍。
    后果是：运营在服务端后台改过的参数，重启一次就被打回原形，
    客户端看到的永远只有「写死的 28 支」，等于标的被硬编码锁死。

    现在的语义是「默认值」而不是「真值」：
      · 库里没有该代码 → 按种子插入（首次部署即得到 28 支可直接交易的标的）；
      · 库里已有该代码 → 原样保留，运营改过的名字、价格、参数、上下架状态都不会被动；
      · 运营删除过的代码 → 记在 SystemEvent 里，不自动复活。
    """
    existing = {s.code for s in session.exec(select(Stock)).all()}

    # 已经被运营删除的标的不要偷偷加回来
    removed_codes: set[str] = set()
    try:
        for ev in session.exec(select(SystemEvent).where(SystemEvent.kind == "stock_removed")).all():
            if ev.message:
                removed_codes.add(ev.message.strip().split(" ")[0])
    except Exception:
        removed_codes = set()

    added = 0
    for seed in SEED_STOCKS:
        if seed.code in existing or seed.code in removed_codes:
            continue
        pct = LIMIT_BY_BOARD.get(seed.board, 0.10)
        session.add(
            Stock(
                code=seed.code,
                name=seed.name,
                board=seed.board,
                industry=seed.industry,
                hotword=seed.hotword,
                theme=seed.theme,
                prev_close=seed.init_price,
                init_price=seed.init_price,
                limit_up=round(seed.init_price * (1 + pct), 2),
                limit_down=round(seed.init_price * (1 - pct), 2),
                tick_size=0.01,
                lot_size=100,
                total_shares=seed.float_shares * 1.55,
                float_shares=seed.float_shares,
                drift=seed.drift,
                vol=seed.vol,
                beta=seed.beta,
                liquidity=seed.liquidity,
            )
        )
        added += 1
    session.commit()
    return added


def ensure_users(session: Session) -> int:
    existing = {u.username for u in session.exec(select(User)).all()}
    added = 0
    for seed in SEED_USERS:
        if seed.username in existing:
            continue
        session.add(
            User(
                username=seed.username,
                display_name=seed.display_name,
                password_hash=hash_password(seed.password),
                role=Role(seed.role),
                status=AccountStatus.ACTIVE,
                cash=seed.cash,
                total_asset=seed.cash,
                credit_limit=seed.credit_limit,
                risk_level=seed.risk_level,
            )
        )
        added += 1
    session.commit()
    return added


def ensure_history(session: Session) -> int:
    """为「库内所有缺少日线」的标的回填历史 K 线（含运营后来新增的标的）。"""
    have = {row.code for row in session.exec(select(DailyBar)).all()}
    added = 0
    for stock in session.exec(select(Stock)).all():
        if stock.code in have:
            continue
        bars = market_engine.backfill_daily(stock.code, settings.history_days)
        for b in bars:
            session.add(DailyBar(**b))
        added += len(bars)
    session.commit()
    return added


def register_engine() -> None:
    """把库内标的注册到内存行情引擎。"""
    with Session(engine) as session:
        for s in session.exec(select(Stock)).all():
            market_engine.register(
                code=s.code,
                name=s.name,
                init_price=s.prev_close or s.init_price,
                tick_size=s.tick_size,
                limit_up_pct=max(
                    0.01,
                    round((s.limit_up / (s.prev_close or s.init_price)) - 1, 4),
                ),
                beta=s.beta,
                drift=s.drift,
                vol=s.vol,
                liquidity=s.liquidity,
                float_shares=s.float_shares,
            )


def bootstrap() -> dict:
    """完整冷启动：建表 → 标的 → 账户 → 历史 → 引擎注册。"""
    init_db()
    with session_scope() as session:
        stocks_added = ensure_stocks(session)
        users_added = ensure_users(session)
    register_engine()
    with session_scope() as session:
        bars_added = ensure_history(session)
        session.add(SystemEvent(kind="boot", message="系统启动：内置标的与账户初始化完成"))
    return {
        "stocks_added": stocks_added,
        "users_added": users_added,
        "daily_bars_added": bars_added,
        "stocks_total": len(SEED_STOCKS),
    }


_engine_thread: threading.Thread | None = None
_stop = threading.Event()


def _loop() -> None:
    """后台行情推进线程：每 tick_seconds 推进一次，跨日自动结转。"""
    last_date = current_session().trade_date
    while not _stop.is_set():
        try:
            if settings.engine_enabled:
                market_engine.tick()
                today = current_session().trade_date
                if today != last_date:
                    last_date = today
                    with session_scope() as session:
                        settle_all(session)
                        session.add(SystemEvent(kind="new_day", message=f"{today} 开盘，T+1 持仓已解锁"))
        except Exception as exc:  # 后台线程不可因单次异常退出
            try:
                with session_scope() as session:
                    session.add(SystemEvent(kind="error", message=f"行情循环异常：{exc}"[:190]))
            except Exception:
                pass
        time.sleep(max(0.2, settings.tick_seconds))


def start_engine() -> None:
    global _engine_thread
    if _engine_thread and _engine_thread.is_alive():
        return
    _stop.clear()
    _engine_thread = threading.Thread(target=_loop, name="stland-market", daemon=True)
    _engine_thread.start()


def stop_engine() -> None:
    _stop.set()
