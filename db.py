"""Day 8：数据库连接层。

三个决定，都跟踩过的坑有关：

1. 【时区必须自己管】SQLite 的 DATETIME 不存时区。用 SQLAlchemy 的
   DateTime(timezone=True) 写进去是 2026-11-20 18:00+08:00，读出来变成
   2026-11-20 18:00（naive）。Day 12 的提醒靠时间比较，naive 混进来就是
   8 小时误差。所以这里用 TZAwareDateTime 把时间存成带偏移的 ISO 字符串。

2. 【外键默认是关的】SQLite 每条连接都要手动 PRAGMA foreign_keys=ON，
   否则 FOREIGN KEY 只是一句装饰，删了父表子表还留着。

3. 【WAL + check_same_thread=False】长连接的回调线程和主线程不是同一个，
   SQLite 默认禁止跨线程复用连接。

迁移策略：Day 8 先用 create_all，表结构稳定后再接 Alembic。
现在表还在改，上 Alembic 只会变成每天写两份 schema。
"""

import os
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.types import String, TypeDecorator

from models import TZ_SHANGHAI

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(BASE_DIR, "task_agent.db")


def now() -> datetime:
    """系统当前时间，带时区。全项目统一从这里取 now，别到处 datetime.now()。"""
    return datetime.now(tz=TZ_SHANGHAI)


class TZAwareDateTime(TypeDecorator):
    """存成 ISO8601 字符串（含 +08:00），读回来还是 aware datetime。

    坑：TypeDecorator 默认 cache_ok=False 会让 SQLAlchemy 每次重新编译，
    显式声明 cache_ok=True。
    """

    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=TZ_SHANGHAI)
        return value.astimezone(TZ_SHANGHAI).isoformat(timespec="seconds")

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=TZ_SHANGHAI)
        return dt.astimezone(TZ_SHANGHAI)


class Base(DeclarativeBase):
    pass


_engine: Optional[Engine] = None
_session_factory: Optional[sessionmaker] = None
_current_path: str = DEFAULT_DB


def _build_engine(db_path: str) -> Engine:
    engine = create_engine(
        f"sqlite:///{db_path}",
        future=True,
        echo=False,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    return engine


def configure(db_path: Optional[str] = None) -> None:
    """切换数据库文件。测试用它指向临时库，生产不调用。"""
    global _engine, _session_factory, _current_path
    path = db_path or os.getenv("TASK_AGENT_DB") or DEFAULT_DB
    if _engine is not None:
        _engine.dispose()
    _current_path = path
    _engine = _build_engine(path)
    _session_factory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)


def get_engine() -> Engine:
    if _engine is None:
        configure()
    assert _engine is not None
    return _engine


def get_session_factory() -> sessionmaker:
    if _session_factory is None:
        configure()
    assert _session_factory is not None
    return _session_factory


@contextmanager
def session_scope():
    """一个事务边界。正常结束提交，异常回滚，永远关闭。"""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _upgrade(engine: Engine) -> None:
    """给已经存在的表补新列。

    create_all 只建不改：老库里 notify_outbox 不会有后来加的 task_uid 列，
    直接查会报 "no such column: notify_outbox.task_uid"。
    新表（task_card）create_all 会自己建，不用管。

    SQLite 的 ADD COLUMN 是原地加、不重写表、不动数据，几毫秒的事，
    拿来应付首期改表足够了。真上生产还是要 Alembic —— 这里只保证
    「升级代码不会让老数据打不开」。
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "notify_outbox" not in tables:
        return

    have = {c["name"] for c in inspector.get_columns("notify_outbox")}
    wanted = {
        "task_uid": "VARCHAR(32)",
        "task_version": "INTEGER DEFAULT 0",
    }
    missing = [(name, ddl) for name, ddl in wanted.items() if name not in have]
    if not missing:
        return

    with engine.begin() as conn:
        for name, ddl in missing:
            conn.execute(text(f"ALTER TABLE notify_outbox ADD COLUMN {name} {ddl}"))
            print(f"[DB] 已补列 notify_outbox.{name}")


def init_db(db_path: Optional[str] = None) -> None:
    """建表。幂等，重复调用没有副作用。"""
    if db_path:
        configure(db_path)
    import models_db  # noqa: F401  触发 ORM 类注册到 Base.metadata

    engine = get_engine()
    Base.metadata.create_all(engine)
    _upgrade(engine)


def current_db_path() -> str:
    return _current_path
