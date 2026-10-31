"""启动装配：建表、加载运行期配置、注册行情引擎、引导管理员、后台行情循环。

与上一版的差别：
* 管理员独立成表后，「引导管理员」写入 `admin` 表，不再往交易账户表塞一条 role=admin；
* 启动时额外把 `setting` 表里的运行期配置覆盖加载进内存；
* 种子账户只剩零售与机构两类。
"""
from __future__ import annotations

import logging
import os
import threading
import time

from sqlmodel import Session, select

from . import runtime_config
from .config import settings
from .db import engine, init_db, session_scope
from .engine.market import market_engine
from .engine.seed import LIMIT_BY_BOARD, SEED_STOCKS, SEED_USERS
from .engine.session import current_session
from .models import Admin, DailyBar, Role, Stock, User
from .security import hash_password
from .services.trading import rematch_pending, settle_all

# 初始化时回填的日线根数（约一年交易日）。
# 以前这里是可配置项 `settings.history_days`，但它只在「数据初始化」这一次被读到，
# 属于一次性的建库参数，不是运营会反复调的旋钮 —— 放在系统配置页里反而让人以为
# 改完能影响既有数据。改为常量后语义清楚：只决定新建库时补多少历史。
SEED_DAILY_BARS = 240

logger = logging.getLogger("stland.bootstrap")


def ensure_stocks(session: Session) -> int:
    """把内置标的作为「默认标的」注入（幂等，绝不覆盖运营已改的记录）。

    「默认值」而不是「真值」：
      · 库里没有该代码 → 按种子插入；
      · 库里已有 → 原样保留（名字、参数、上下架状态都不动）；
      · 运营删除过的代码 → 行已不存在，但会被这里重新注入（这是刻意取舍：
        种子只是「默认值」，需要彻底删掉某支标的请在删除后不再改动种子）。
    """
    existing = {s.code for s in session.exec(select(Stock)).all()}

    added = 0
    for seed in SEED_STOCKS:
        if seed.code in existing:
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
    """注入零售 + 机构种子账户（管理员不在其中，它走 admin 表）。"""
    existing = {u.username for u in session.exec(select(User)).all()}
    added = 0
    now = time.time()
    for seed in SEED_USERS:
        if seed.username in existing:
            continue
        session.add(
            User(
                username=seed.username,
                display_name=seed.display_name,
                password_hash=hash_password(seed.password),
                role=Role(seed.role),
                cash=seed.cash,
                total_asset=seed.cash,
                credit_limit=seed.credit_limit,
                risk_level=seed.risk_level,
                # 种子账户若自带等级，视为「已完成测评」，否则客户端会一直提示去测评
                risk_assessed_at=now if seed.risk_level > 0 else 0.0,
            )
        )
        added += 1
    session.commit()
    return added


def _broken_codes(session: Session) -> set[str]:
    """找出历史日线明显异常（上一版生成器缺陷留下的）标的。

    判定只用两条与业务无关的硬事实：收盘价必须为正，且整段的量级不能离谱。
    正常个股 240 天内不会出现「首日 1e15、末日 10 元」这种跨度，
    出现即说明数据是坏的，重建比留着更安全 —— 留着会让指数/图表直接归零。
    """
    broken: set[str] = set()
    rows = session.exec(select(DailyBar)).all()
    if not rows:
        return broken
    by_code: dict[str, list[DailyBar]] = {}
    for r in rows:
        by_code.setdefault(r.code, []).append(r)
    for code, bars in by_code.items():
        closes = sorted(((b.trade_date, b.close) for b in bars), key=lambda x: x[0])
        first, last = closes[0][1], closes[-1][1]
        if first <= 0 or last <= 0:
            broken.add(code)
            continue
        if first > 1e5 or last > 1e5:
            broken.add(code)
            continue
        # 首末价跨度过大（>20 倍或 <1/20）同样视为坏数据
        if first / last > 20 or last / first > 20:
            broken.add(code)
    return broken


def ensure_history(session: Session) -> int:
    """为「库内缺日线」或「日线明显异常」的标的回填历史 K 线。"""
    broken = _broken_codes(session)
    if broken:
        for code in broken:
            for row in session.exec(select(DailyBar).where(DailyBar.code == code)).all():
                session.delete(row)
        session.commit()

    have = {row.code for row in session.exec(select(DailyBar)).all()}
    added = 0
    for stock in session.exec(select(Stock)).all():
        if stock.code in have:
            continue
        bars = market_engine.backfill_daily(stock.code, SEED_DAILY_BARS)
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
                limit_up_pct=max(0.01, round((s.limit_up / (s.prev_close or s.init_price)) - 1, 4)),
                beta=s.beta,
                drift=s.drift,
                vol=s.vol,
                liquidity=s.liquidity,
                float_shares=s.float_shares,
            )


def ensure_admin(session: Session) -> bool:
    """首次运行引导：`admin` 表为空时创建 1 个可登录后台的引导账户。

    为什么这个不交给「手动注入」：取消启动注入后全新数据库是空的，
    没有任何管理员就没人能进后台点那个按钮，形成死锁。
    这里只建管理员，不建标的、不建交易账户、不建日线。
    """
    if session.exec(select(Admin)).first() is not None:
        return False

    password = os.getenv("STLAND_BOOTSTRAP_ADMIN_PASSWORD", "stland-admin")
    admin = Admin(
        username=os.getenv("STLAND_BOOTSTRAP_ADMIN_USERNAME", "admin"),
        display_name="系统管理员",
        password_hash=hash_password(password),
        is_active=True,
    )
    session.add(admin)
    session.commit()
    logger.warning("已创建引导管理员 %s（请立即登录后台修改密码）", admin.username)
    return True


def bootstrap() -> dict:
    """冷启动：建表 + 加载配置 + 注册引擎 + 引导管理员，不写业务数据。"""
    init_db()
    with session_scope() as session:
        runtime_config.load_overrides(session)
    register_engine()
    with session_scope() as session:
        ensure_admin(session)
        stocks = len(session.exec(select(Stock)).all())
        users = len(session.exec(select(User)).all())
        admins = len(session.exec(select(Admin)).all())
    return {"stocks_total": stocks, "users_total": users, "admins_total": admins, "seeded": False}


def init_seed_data(include_stocks: bool = True, include_users: bool = True, include_history: bool = True) -> dict:
    """后台「初始化数据」按钮的实现：显式注入标准数据集（幂等）。"""
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
                # 行情推进后再撮合一次挂单：限价单只有在价格走到委托价时才该成交，
                # 下单瞬间撮合不到并不代表它永远不成交。
                with session_scope() as session:
                    rematch_pending(session)
                today = current_session().trade_date
                if today != last_date:
                    last_date = today
                    with session_scope() as session:
                        settle_all(session)
        except Exception:  # 后台线程不可因单次异常退出
            logger.exception("行情循环异常")
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
