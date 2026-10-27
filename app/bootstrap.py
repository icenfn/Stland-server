"""启动装配：内置标的注册、历史回填、种子账户、后台行情循环。"""
from __future__ import annotations

import logging
import os
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

logger = logging.getLogger("stland.bootstrap")


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
    """冷启动：只建表 + 把库里已有标的注册进行情引擎，绝不写入任何业务数据。

    为什么不再自动注入
    ----------------
    旧实现每次启动都会把 28 支标的、3 个种子账户和历史 K 线写进库里。
    这让「标准数据集」变成一个不可见的副作用：
      · 运营刚在后台删掉的账户，重启又被加回来；
      · 想从一个空库开始搭建自己的标的池，做不到；
      · 也无法区分「系统自带的数据」与「运营自己录入的数据」。
    现在启动只做两件必需的事：建表（幂等）、把库里已有标的注册进内存引擎。
    数据初始化成为后台面板上一个显式按钮（见 admin.init_seed_data），
    点一次注入一次，重复点也不会产生重复记录。
    """
    init_db()
    register_engine()
    with session_scope() as session:
        # 唯一的启动写入：库中无管理员时创建引导管理员（否则无人能进后台）
        ensure_admin(session)
        stocks = len(session.exec(select(Stock)).all())
        users = len(session.exec(select(User)).all())
        session.add(SystemEvent(kind="boot", message=f"系统启动：当前 {stocks} 支标的、{users} 个账户"))
    return {"stocks_total": stocks, "users_total": users, "seeded": False}


def ensure_admin(session: Session) -> bool:
    """首次运行引导：库里没有任何管理员时，创建一个可登录后台的引导账户。

    为什么这个不能也交给「手动注入」
    ----------------------------
    取消启动注入以后，全新数据库是空的 —— 没有任何账户，也就没人能登录后台
    去点那个「注入数据」按钮，形成死锁。因此这里保留唯一一个最小化的启动写入：
    仅当「所有管理员都不存在」时创建 1 个引导管理员，供运营首次进入后台。
    它不同于自动注入数据集：不创建标的、不创建零售/机构账户、不建日线，
    而且一旦库里已有管理员就什么都不做。账号密码可用环境变量覆盖。
    """
    admins = session.exec(select(User).where(User.role == Role.ADMIN)).all()
    if admins:
        return False
    from .security import hash_password

    password = os.getenv("STLAND_BOOTSTRAP_ADMIN_PASSWORD", "stland-admin")
    user = User(
        username=os.getenv("STLAND_BOOTSTRAP_ADMIN_USERNAME", "admin"),
        display_name="系统管理员",
        password_hash=hash_password(password),
        role=Role.ADMIN,
        status=AccountStatus.ACTIVE,
        cash=0.0,
        total_cash_in=0.0,
        total_asset=0.0,
        credit_limit=0.0,
        risk_level=1,
    )
    session.add(user)
    session.add(
        SystemEvent(
            kind="bootstrap_admin",
            message="首次启动：创建引导管理员，用于进入后台并手动初始化数据",
        )
    )
    session.commit()
    logger.warning("已创建引导管理员 %s（请立即登录后台修改密码）", user.username)
    return True


def init_seed_data(include_stocks: bool = True, include_users: bool = True, include_history: bool = True) -> dict:
    """后台「初始化数据」按钮的实现：显式注入标准数据集（幂等）。

    放在这里而不是启动流程里，是因为它是一次**管理动作**：
    运营决定什么时候、注入哪一部分数据，并能在审计日志里看到谁点的。
    """
    result = {"stocks_added": 0, "users_added": 0, "daily_bars_added": 0}
    with session_scope() as session:
        if include_stocks:
            result["stocks_added"] = ensure_stocks(session)
        if include_users:
            result["users_added"] = ensure_users(session)
    register_engine()
    with session_scope() as session:
        if include_history:
            result["daily_bars_added"] = ensure_history(session)
        session.add(
            SystemEvent(
                kind="seed",
                message=(
                    "手动初始化数据："
                    f"标的 +{result['stocks_added']}、账户 +{result['users_added']}、"
                    f"日线 +{result['daily_bars_added']}"
                ),
            )
        )
    return result


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
