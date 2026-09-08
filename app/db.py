"""Capa de persistencia. Por defecto SQLite (coste cero) sobre volumen de Fly.

Si algun dia se quiere Postgres/Supabase basta con cambiar DATABASE_URL:
el resto del codigo es agnostico porque va contra SQLAlchemy.
"""
import logging
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


logger = logging.getLogger("investment")


class Base(DeclarativeBase):
    pass


def _build_engine():
    settings = get_settings()
    url = settings.database_url
    kwargs: dict = {"pool_pre_ping": True, "future": True}

    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        path = url.split("sqlite:///")[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(url, **kwargs)

    if url.startswith("sqlite"):
        # WAL da lecturas concurrentes decentes sin coste ni servidor aparte.
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app import models  # noqa: F401  (registra los modelos en el metadata)

    Base.metadata.create_all(bind=engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """Anade a las tablas ya creadas las columnas nuevas del modelo.

    create_all() crea tablas que faltan, pero no toca las que existen: al
    anadir un campo, la base desplegada sobre el volumen de Fly se quedaba
    atras y cualquier consulta reventaba con "no such column". Se limita a
    columnas que admiten nulo, que es lo unico que SQLite deja anadir sin
    reescribir la tabla, y es idempotente: lo que ya esta no se toca.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    existentes = set(inspector.get_table_names())
    with engine.begin() as connection:
        for tabla in Base.metadata.sorted_tables:
            if tabla.name not in existentes:
                continue
            actuales = {c["name"] for c in inspector.get_columns(tabla.name)}
            for columna in tabla.columns:
                if columna.name in actuales or not columna.nullable:
                    continue
                tipo = columna.type.compile(engine.dialect)
                connection.execute(
                    text(f'ALTER TABLE "{tabla.name}" ADD COLUMN "{columna.name}" {tipo}')
                )
                logger.info("Columna anadida: %s.%s (%s)", tabla.name, columna.name, tipo)
