"""数据库会话与初始化。SQLite + WAL，便于单实例高并发读。"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event
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


def init_db() -> None:
    """建表（幂等）。导入 models 以完成 SQLModel 元数据注册。"""
    from . import models  # noqa: F401

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
