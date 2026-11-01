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
from .engine.seed import SEED_BOARDS, SEED_STOCKS, SEED_USERS, LIMIT_BY_BOARD
from .engine.session import current_session
from .models import Admin, Board, DailyBar, Industry, Role, Stock, User
from .security import hash_password
from .services import reconcile
from .services.trading import rematch_pending, settle_all

# 新建库时回填的日线根数（约一年交易日）。
# 只在此处生效，不放进运行期配置：它决定建库补多少历史，改动也不会影响既有数据。
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
    # 板块规则以库里为准：种子标的的涨跌停必须跟着运营改过的板块规则走，
    # 否则「板块管理」里把创业板改成别的比例后，重新初始化数据又会打回 20%。
    rules = {b.name: (b.limit_pct, b.tick_size, b.lot_size)
             for b in session.exec(select(Board)).all()}

    added = 0
    for seed in SEED_STOCKS:
        if seed.code in existing:
            continue
        pct = rules.get(seed.board, (LIMIT_BY_BOARD.get(seed.board, 0.10), 0.01, 100))[0]
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


def ensure_boards(session: Session) -> int:
    """注入默认板块（幂等，绝不覆盖运营改过的规则）。

    板块是标的涨跌停的唯一来源：表为空时 `_board_rules()` 会回落到主板口径，
    创业板的 ±20% 会悄悄变成 ±10% —— 这类静默降级只能靠建库时补齐数据避免。
    """
    existing = {b.name for b in session.exec(select(Board)).all()}
    added = 0
    for seed in SEED_BOARDS:
        if seed.name in existing:
            continue
        session.add(
            Board(
                name=seed.name,
                limit_pct=seed.limit_pct,
                tick_size=seed.tick_size,
                lot_size=seed.lot_size,
                description=seed.description,
                sort=seed.sort,
            )
        )
        added += 1
    session.commit()
    return added


def ensure_industries(session: Session) -> int:
    """把种子标的用到的行业补进 industry 表（幂等）。

    行业只是分类，不承载规则；漏建不影响交易，但会让「行业管理」页缺项，
    与标的列表里出现的行业名对不上。
    """
    existing = {it.name for it in session.exec(select(Industry)).all()}
    wanted: list[str] = []
    for seed in SEED_STOCKS:
        if seed.industry and seed.industry not in existing and seed.industry not in wanted:
            wanted.append(seed.industry)
    for i, name in enumerate(wanted):
        session.add(Industry(name=name, description="", sort=(i + 1) * 10))
    if wanted:
        session.commit()
    return len(wanted)


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


def register_engine(hydrate: bool = False) -> None:
    """把库内标的注册到内存行情引擎。

    ``hydrate=True`` 时接着从库内快照恢复价格与成交量：只在启动阶段用，
    因为注册刚把状态清成初值，此时恢复才有意义。后台「初始化数据」
    重复调用时保持 ``False``，避免把运行中的行情打回几秒前的快照。
    """
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
    if hydrate:
        # 先恢复快照，再归档可能已跨日的昨日状态：
        # 反过来会把恢复出来的「今天」也当成跨日清掉。
        market_engine.hydrate()
        market_engine.roll_day()


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


def startup_selfcheck() -> dict:
    """启动自检：对账一遍，并把可以安全纠正的漂移修掉。

    为什么在启动时做：崩溃/断电的痕迹（未退的冻结、没回写的持仓）只在
    「上一次进程异常结束」时留下。放这里跑，等于每次上线都先把上次的残局
    收拾干净再开门营业，而不是等用户发现钱不对才追查。

    只自动修**可以证明是安全的**一类：``available = quantity - frozen``
    这个等式本身就是 T+1 结算的定义，重算一遍不会改变任何真实持仓。
    其余差异（成交金额、双边配对、冻结资金）只报不修 —— 自动改写真实金额
    等于把「有人对不上账」这个事实抹掉，比留着差异危险得多。
    """
    summary = {"repaired": 0, "issues": 0, "errors": 0}
    with session_scope() as session:
        try:
            # 幂等重放日终结算：修正可卖数量漂移
            settle_all(session)
        except Exception:  # noqa: BLE001 - 自检失败不能挡住启动
            logger.exception("启动自检：日终结算重放失败")
        try:
            report = reconcile.run_all(session)
        except Exception:  # noqa: BLE001
            logger.exception("启动自检：对账失败")
            return summary
        summary["issues"] = report["counts"]["total"]
        summary["errors"] = report["counts"]["error"]
        if report["counts"]["total"]:
            # 只记摘要不刷屏：逐条差异由后台 /reconcile 页面查看
            logger.warning(
                "启动自检发现 %d 项差异（其中 %d 项为错误级），详见后台对账页",
                report["counts"]["total"], report["counts"]["error"],
            )
        for item in report["findings"]:
            logger.info("对账差异 %s %s：%s", item["level"], item["subject"], item["detail"])
    return summary


def bootstrap() -> dict:
    """冷启动：建表 + 加载配置 + 注册引擎 + 引导管理员 + 启动自检，不写业务数据。"""
    init_db()
    with session_scope() as session:
        runtime_config.load_overrides(session)
    register_engine(hydrate=True)
    selfcheck = startup_selfcheck()
    with session_scope() as session:
        ensure_admin(session)
        # 板块是标的规则的来源，必须先于任何标的写入存在
        ensure_boards(session)
        ensure_industries(session)
        stocks = len(session.exec(select(Stock)).all())
        users = len(session.exec(select(User)).all())
        admins = len(session.exec(select(Admin)).all())
    return {
        "stocks_total": stocks,
        "users_total": users,
        "admins_total": admins,
        "seeded": False,
        "selfcheck": selfcheck,
    }


def init_seed_data(include_stocks: bool = True, include_users: bool = True, include_history: bool = True) -> dict:
    """后台「初始化数据」按钮的实现：显式注入标准数据集（幂等）。"""
    result = {"boards_added": 0, "industries_added": 0, "stocks_added": 0, "users_added": 0, "daily_bars_added": 0}
    with session_scope() as session:
        # 老库升级上来时 board 表是空的，先补齐规则再写标的，
        # 否则种子的涨跌停会按回落口径算错
        result["boards_added"] = ensure_boards(session)
        result["industries_added"] = ensure_industries(session)
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
    """后台主循环：行情推进 + 挂单重撮 + 日终结算。

    这三件事原先都写在 ``if settings.engine_enabled:`` 里面，等于「关掉
    模拟引擎就关掉整个市场」：

    * 不 ``rematch_pending`` → 限价单下单瞬间没成交就永远等不到；
    * 不 ``settle_all`` → T+1 买入的股份永不解冻，次日开盘也卖不掉。

    但「行情从哪来」和「交易怎么结算」本来是两件事。真实行情接入后
    引擎不再 tick，结算却必须照常跑。所以这里按职责拆开：
    只有 ``market_engine.tick()`` 受 ``engine_enabled`` 控制，
    撮合与结算无条件执行。
    """
    last_date = current_session().trade_date
    while not _stop.is_set():
        try:
            # 1) 行情推进：只有模拟引擎需要，外部行情源由 feeder 写入
            if settings.engine_enabled:
                market_engine.tick()

            # 1b) 换日归档：把上一交易日收成日线并归零。
            #     与 tick 无关 —— 关掉模拟引擎后新的一天照样要开始。
            market_engine.roll_day()

            # 1c) 行情快照落库（内部节流到 5 秒一次），保证重启后价格不倒退
            market_engine.persist_quotes()

            # 2) 挂单重撮：价格变化后，之前挂着的限价单可能已经够得着了
            with session_scope() as session:
                rematch_pending(session)

            # 3) 日终结算：无论行情来自哪里，T+1 解冻与费用结算都必须发生
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
    """停掉行情循环，并把最后一次行情状态落库。"""
    _stop.set()
    try:
        market_engine.persist_quotes(force=True)
    except Exception:  # noqa: BLE001 - 退出路径上失败只损失最后一次快照
        logger.exception("退出时保存行情快照失败")
