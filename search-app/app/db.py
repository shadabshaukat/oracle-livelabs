from __future__ import annotations

import contextlib
import logging
from typing import Optional

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import Settings, build_database_url, settings
from .embeddings import get_text_embedding_dim
from .vision_embeddings import get_image_embedding_dim, VisionModelUnavailable

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
    try:
        dim = get_text_embedding_dim()
    except Exception as exc:
        logger.warning("Falling back to EMBEDDING_DIM=%s due to error: %s", s.embedding_dim, exc)
        dim = s.embedding_dim
    try:
        image_dim = get_image_embedding_dim()
    except VisionModelUnavailable as exc:
        logger.warning("Vision model unavailable, using IMAGE_EMBED_DIM=%s (%s)", s.image_embed_dim, exc)
        image_dim = s.image_embed_dim
    except Exception as exc:
        logger.warning("Falling back to IMAGE_EMBED_DIM=%s due to error: %s", s.image_embed_dim, exc)
        image_dim = s.image_embed_dim
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
            cur.execute("CREATE EXTENSION IF NOT EXISTS citext")

        # Create tables
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id BIGSERIAL PRIMARY KEY,
                    email CITEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    role_id BIGINT,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    last_login_at TIMESTAMPTZ
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS roles (
                    id BIGSERIAL PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    description TEXT,
                    created_at TIMESTAMPTZ DEFAULT now()
                );
                """
            )

            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS role_id BIGINT")
            cur.execute(
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1
                        FROM pg_constraint
                        WHERE conname = 'users_role_id_fkey'
                    ) THEN
                        ALTER TABLE users
                        ADD CONSTRAINT users_role_id_fkey FOREIGN KEY (role_id) REFERENCES roles(id);
                    END IF;
                END $$;
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_users_role_id ON users(role_id)")

            cur.execute(
                """
                INSERT INTO roles (name, description)
                VALUES
                    ('user', 'Default user, no SQL access'),
                    ('analyst', 'Analyst: NL2SQL across all spaces'),
                    ('admin', 'Admin: NL2SQL + system queries')
                ON CONFLICT (name) DO NOTHING
                """
            )

            cur.execute(
                """
                UPDATE users
                SET role_id = (SELECT id FROM roles WHERE name = 'user')
                WHERE role_id IS NULL
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS spaces (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    is_default BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    UNIQUE(user_id, name)
                );
                """
            )

            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS documents (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    space_id BIGINT REFERENCES spaces(id) ON DELETE SET NULL,
                    source_path TEXT,
                    source_type TEXT NOT NULL,
                    title TEXT,
                    object_provider TEXT,
                    object_bucket TEXT,
                    object_name TEXT,
                    thumbnail_object_name TEXT,
                    metadata JSONB DEFAULT '{{}}'::jsonb,
                    created_at TIMESTAMPTZ DEFAULT now()
                );
                """
            )

            cur.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS user_id BIGINT")
            cur.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS space_id BIGINT")
            cur.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS object_provider TEXT")
            cur.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS object_bucket TEXT")
            cur.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS object_name TEXT")
            cur.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS thumbnail_object_name TEXT")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_documents_user_space ON documents(user_id, space_id, created_at DESC)")

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

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS user_activity (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    activity_type TEXT NOT NULL,
                    details JSONB DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ DEFAULT now()
                );
                """
            )

            cur.execute("CREATE INDEX IF NOT EXISTS idx_user_activity_user_time ON user_activity(user_id, created_at DESC)")

            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS image_assets (
                    id BIGSERIAL PRIMARY KEY,
                    document_id BIGINT REFERENCES documents(id) ON DELETE CASCADE,
                    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    space_id BIGINT REFERENCES spaces(id) ON DELETE SET NULL,
                    file_path TEXT,
                    thumbnail_path TEXT,
                    width INT,
                    height INT,
                    tags JSONB DEFAULT '[]'::jsonb,
                    caption TEXT,
                    ocr_text TEXT,
                    embedding vector({image_dim}),
                    embedding_model TEXT,
                    created_at TIMESTAMPTZ DEFAULT now()
                );
                """
            )

            cur.execute("ALTER TABLE image_assets ADD COLUMN IF NOT EXISTS ocr_text TEXT")

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_image_assets_user_space ON image_assets(user_id, space_id, created_at DESC);
                """
            )

            cur.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_image_assets_embedding_ivfflat
                ON image_assets USING ivfflat (embedding {opclass})
                WITH (lists = {s.pgvector_lists});
                """
            )

            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS memory_events (
                    id BIGSERIAL,
                    space_id BIGINT REFERENCES spaces(id) ON DELETE CASCADE,
                    user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
                    memory_type TEXT NOT NULL,
                    query_text TEXT,
                    response_text TEXT,
                    generated_sql TEXT,
                    columns JSONB DEFAULT '[]'::jsonb,
                    result_sample JSONB DEFAULT '[]'::jsonb,
                    metadata JSONB DEFAULT '{{}}'::jsonb,
                    rating SMALLINT,
                    summary TEXT,
                    embedding vector({dim}),
                    embedding_model TEXT,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    PRIMARY KEY (id, created_at)
                ) PARTITION BY RANGE (created_at);
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_events_default
                  PARTITION OF memory_events DEFAULT;
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memory_events_space_time
                  ON memory_events(space_id, memory_type, created_at DESC);
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memory_events_space_rating
                  ON memory_events(space_id, memory_type, rating, created_at DESC);
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memory_events_id
                  ON memory_events(id);
                """
            )
            cur.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_memory_events_embedding_ivfflat
                  ON memory_events USING ivfflat (embedding {opclass})
                  WITH (lists = {s.pgvector_lists});
                """
            )

        logger.info(
            "Database initialized with text_dim=%s image_dim=%s metric=%s lists=%s",
            dim,
            image_dim,
            metric,
            s.pgvector_lists,
        )


def set_search_runtime(cur: psycopg.Cursor, probes: int):
    # SET LOCAL cannot use bind parameters for the value; interpolate safely as a literal
    from psycopg import sql
    cur.execute(sql.SQL("SET LOCAL ivfflat.probes = {}" ).format(sql.Literal(int(probes))))
