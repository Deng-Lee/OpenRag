"""Database connection and session management"""

import logging
from typing import Generator
from urllib.parse import quote_plus

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from openrag.config import get_config
from openrag.models.base import Base

logger = logging.getLogger(__name__)


def get_database_url() -> str:
    """Get database URL from config (PostgreSQL only)."""
    config = get_config()
    pg = config.postgres
    user = quote_plus(pg.user)
    password = quote_plus(pg.password)
    return f"postgresql+psycopg://{user}:{password}@{pg.host}:{pg.port}/{pg.database}"


# Lazy engine creation
_engine = None

def get_engine():
    """Get or create SQLAlchemy engine (lazy initialization)"""
    global _engine
    if _engine is None:
        cfg = get_config()
        echo = bool(cfg.postgres.sqlalchemy_echo)
        if echo:
            logger.warning("POSTGRES_SQLALCHEMY_ECHO is enabled: SQL will be logged")
        _engine = create_engine(
            get_database_url(),
            echo=echo,
            pool_pre_ping=True,
            pool_recycle=3600,
        )
    return _engine


# Create session factory (will be bound to engine on first use)
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
)


def get_db() -> Generator[Session, None, None]:
    """Get database session (dependency injection for FastAPI)"""
    SessionLocal.configure(bind=get_engine())
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Initialize database (create all tables)"""
    Base.metadata.create_all(bind=get_engine())


def drop_db() -> None:
    """Drop all tables (use with caution!)"""
    Base.metadata.drop_all(bind=get_engine())
