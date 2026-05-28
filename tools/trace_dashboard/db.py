from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


def load_dashboard_env() -> None:
    explicit = os.getenv("TRACE_DASHBOARD_DOTENV")
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.extend([Path(".env"), Path("docker/.env"), Path("../docker/.env")])
    for candidate in candidates:
        if candidate.exists():
            load_dotenv(candidate, override=False)


def build_database_url() -> str:
    load_dashboard_env()
    direct_url = os.getenv("TRACE_DASHBOARD_DATABASE_URL") or os.getenv("DATABASE_URL")
    if direct_url:
        return direct_url

    user = quote_plus(os.getenv("POSTGRES_USER", "openrag"))
    password = quote_plus(os.getenv("POSTGRES_PASSWORD", "openrag_pass"))
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    database = os.getenv("POSTGRES_DATABASE") or os.getenv("POSTGRES_DB", "openrag")
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{database}"


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    return create_engine(build_database_url(), pool_pre_ping=True)


def read_df(sql: str, params: dict | None = None, engine: Engine | None = None) -> pd.DataFrame:
    active_engine = engine or get_engine()
    with active_engine.connect() as conn:
        return pd.read_sql_query(text(sql), conn, params=params or {})
