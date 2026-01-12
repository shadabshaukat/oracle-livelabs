from __future__ import annotations

import contextlib
import logging
from typing import Optional

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import Settings, build_database_url, settings

logger = logging.getLogger(__name__)

_pool: Optional[ConnectionPool] = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        dsn = build_database_url(settings)
        _pool = ConnectionPool(
            conninfo=dsn,
            min_size=settings.db_pool_min_size,
            max_size=settings.db_pool_max_size,
            kwargs={"autocommit": True},
        )
        logger.info("Initialized PostgreSQL connection pool (min=%s, max=%s)", settings.db_pool_min_size, settings.db_pool_max_size)
    return _pool


@contextlib.contextmanager
def get_conn():
    pool = get_pool()
    with pool.connection() as conn:
        yield conn


@contextlib.contextmanager
def get_cursor(row_factory=dict_row):
    with get_conn() as conn:
        with conn.cursor(row_factory=row_factory) as cur:
            yield cur


def init_db(s: Settings = settings) -> None:
    """
    Initialize database: create extensions, tables, and indexes if they do not exist.
    Uses settings.embedding_dim, pgvector metric/lists configuration, and FTS config.
    """
    dim = s.embedding_dim
    metric = s.pgvector_metric.lower()
    if metric not in {"cosine", "l2", "ip"}:
        raise ValueError("PGVECTOR_METRIC must be one of: cosine, l2, ip")
    opclass = {
        "cosine": "vector_cosine_ops",
        "l2": "vector_l2_ops",
        "ip": "vector_ip_ops",
    }[metric]

    with get_conn() as conn:
        # Ensure extensions
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

        # Create tables
        with conn.cursor() as cur:
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS documents (
                    id BIGSERIAL PRIMARY KEY,
                    source_path TEXT,
                    source_type TEXT NOT NULL,
                    title TEXT,
                    metadata JSONB DEFAULT '{{}}'::jsonb,
                    created_at TIMESTAMPTZ DEFAULT now()
                );
                """
            )

            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS chunks (
                    id BIGSERIAL PRIMARY KEY,
                    document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    chunk_index INT NOT NULL,
                    content TEXT NOT NULL,
                    content_tsv tsvector GENERATED ALWAYS AS (to_tsvector('{s.fts_config}', content)) STORED,
                    content_chars INT,
                    embedding vector({dim}),
                    embedding_model TEXT,
                    created_at TIMESTAMPTZ DEFAULT now()
                );
                """
            )

            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_chunks_doc_chunk ON chunks(document_id, chunk_index);
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_chunks_tsv ON chunks USING GIN (content_tsv);
                """
            )

            cur.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_chunks_embedding_ivfflat
                ON chunks USING ivfflat (embedding {opclass})
                WITH (lists = {s.pgvector_lists});
                """
            )

        logger.info("Database initialized with vector dim=%s, metric=%s, lists=%s", dim, metric, s.pgvector_lists)


def set_search_runtime(cur: psycopg.Cursor, probes: int):
    # SET LOCAL cannot use bind parameters for the value; interpolate safely as a literal
    from psycopg import sql
    cur.execute(sql.SQL("SET LOCAL ivfflat.probes = {}" ).format(sql.Literal(int(probes))))
