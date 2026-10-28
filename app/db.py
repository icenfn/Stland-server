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


# 已废弃的表：审计日志、风险测评档案（本轮删除相关功能）
_LEGACY_TABLES = ("auditlog", "riskassessment")


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


def init_db() -> None:
    """建表（幂等）并清理旧结构。"""
    from . import models  # noqa: F401  触发元数据注册

    _purge_legacy()
    SQLModel.metadata.create_all(engine)


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
