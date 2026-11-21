from __future__ import annotations

from functools import lru_cache

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from . import config
from . import models  # noqa: F401  (registers tables)


@lru_cache(maxsize=None)
def _engine(url: str) -> Engine:
    engine = create_engine(url)
    SQLModel.metadata.create_all(engine)
    _add_missing_columns(engine)
    return engine


def _add_missing_columns(engine: Engine) -> None:
    """Tiny forward-only migration: add columns that newer versions of a model introduced."""
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl_type = col.type.compile(dialect=engine.dialect)
                default = col.default.arg if col.default is not None and col.default.is_scalar else None
                clause = ""
                if default is not None:
                    clause = f" DEFAULT {int(default) if isinstance(default, bool) else repr(default)}"
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl_type}{clause}'))


def engine() -> Engine:
    path = config.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return _engine(f"sqlite:///{path}")


def session() -> Session:
    return Session(engine())
