"""数据库会话与初始化。SQLite + WAL，便于单实例高并发读。

本版对表结构做了一次不兼容重构（管理员独立成表、删除审计与风险档案表），
因此 `init_db` 里带了一步「旧结构清理」：不再保留历史数据兼容逻辑，
直接删除已经废弃的表与不合法的行，避免旧数据把新枚举读崩。
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from .config import settings

connect_args = {"check_same_thread": False} if settings.db_url.startswith("sqlite") else {}
engine = create_engine(
    settings.db_url,
    echo=False,
    connect_args=connect_args,
    pool_pre_ping=True,
)


if settings.db_url.startswith("sqlite"):

    @event.listens_for(Engine, "connect")
    def _sqlite_pragma(dbapi_connection, _record):  # pragma: no cover - 驱动回调
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()


# 已废弃的表：审计日志、风险测评档案、系统事件（功能已全部删除）
_LEGACY_TABLES = ("auditlog", "riskassessment", "systemevent")


def _purge_legacy() -> None:
    """清理旧结构残留。

    1) 删除已废弃的表（功能已移除，留着只会误导后来人）；
    2) 删除 `user` 表里 role=admin 的历史行 —— 管理员已迁到独立的 `admin` 表，
       而新 Role 枚举只有 retail/institution，旧行留着会在反序列化时报枚举错误。
       这些旧管理员账户没有资金也没有持仓，直接丢弃最安全。
    """
    with engine.begin() as conn:
        for table in _LEGACY_TABLES:
            conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
        try:
            conn.execute(text("DELETE FROM user WHERE role = 'admin'"))
        except Exception:  # 表还不存在（全新库）时忽略
            pass


def _align_columns() -> None:
    """给已存在的表补上模型里新增的列（只加不删，SQLite 的安全子集）。

    为什么需要这一步：`create_all` 只建「不存在的表」，对「已存在但少了列」的表
    什么都不做。上一版新增 `stock.delisted` 后，线上旧库启动即报
    `no such column: stock.delisted`，服务直接起不来 —— 这种崩溃完全可以在
    启动阶段自愈，不必依赖手工删库。

    这里只做「加列」这一种安全变更：缺失的列按模型定义补上（NOT NULL 带默认值时
    用 DEFAULT 填充存量行）。删列/改类型不在此处理 —— 那属于真正的不兼容变更，
    应由 `_purge_legacy` 显式的重建逻辑承担。
    """
    from sqlalchemy import inspect

    from . import models  # noqa: F401  触发元数据注册

    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())

    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for column in table.columns:
                if column.name in have:
                    continue
                ddl_type = column.type.compile(dialect=engine.dialect)
                default = ""
                if column.default is not None and getattr(column.default, "is_scalar", False):
                    value = column.default.arg
                    if isinstance(value, str):
                        default = f" DEFAULT '{value}'"
                    elif isinstance(value, bool):
                        default = f" DEFAULT {int(value)}"
                    elif isinstance(value, (int, float)):
                        default = f" DEFAULT {value}"
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl_type}{default}'))


def init_db() -> None:
    """建表（幂等）、对齐新增列，并清理旧结构。"""
    from . import models  # noqa: F401  触发元数据注册

    _purge_legacy()
    SQLModel.metadata.create_all(engine)
    _align_columns()


def get_session() -> Iterator[Session]:
    """FastAPI 依赖：每请求一个会话。"""
    with Session(engine) as session:
        yield session


@contextmanager
def session_scope() -> Iterator[Session]:
    """后台任务使用的事务上下文。"""
    with Session(engine) as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
