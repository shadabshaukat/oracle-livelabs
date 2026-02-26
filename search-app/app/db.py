from __future__ import annotations

import contextlib
import logging
import time
from typing import Optional

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from .config import Settings, build_database_url, settings
from .embeddings import get_text_embedding_dim
from .vision_embeddings import get_image_embedding_dim, VisionModelUnavailable

logger = logging.getLogger(__name__)

_pool: Optional[ConnectionPool] = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        dsn = build_database_url(settings)
        connect_timeout = max(1, int(settings.db_connect_timeout_seconds))
        _pool = ConnectionPool(
            conninfo=dsn,
            min_size=settings.db_pool_min_size,
            max_size=settings.db_pool_max_size,
            kwargs={"autocommit": True, "connect_timeout": connect_timeout},
        )
        logger.info(
            "Initialized PostgreSQL connection pool (min=%s, max=%s, conn_timeout=%ss)",
            settings.db_pool_min_size,
            settings.db_pool_max_size,
            connect_timeout,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is None:
        return
    try:
        _pool.close()
    except Exception:
        logger.exception("Failed closing PostgreSQL connection pool")
    finally:
        _pool = None


@contextlib.contextmanager
def get_conn():
    pool = get_pool()
    timeout = float(settings.db_pool_timeout_seconds)
    timeout = timeout if timeout > 0 else 30.0
    with pool.connection(timeout=timeout) as conn:
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
                CREATE TABLE IF NOT EXISTS search_sessions (
                    id BIGSERIAL,
                    session_id TEXT NOT NULL,
                    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    space_id BIGINT REFERENCES spaces(id) ON DELETE SET NULL,
                    name TEXT,
                    last_ip TEXT,
                    last_user_agent TEXT,
                    first_activity_at TIMESTAMPTZ DEFAULT now(),
                    last_activity_at TIMESTAMPTZ DEFAULT now(),
                    created_at TIMESTAMPTZ DEFAULT now(),
                    PRIMARY KEY (session_id)
                ) PARTITION BY HASH (session_id);
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS search_sessions_p0
                  PARTITION OF search_sessions FOR VALUES WITH (MODULUS 4, REMAINDER 0);
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS search_sessions_p1
                  PARTITION OF search_sessions FOR VALUES WITH (MODULUS 4, REMAINDER 1);
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS search_sessions_p2
                  PARTITION OF search_sessions FOR VALUES WITH (MODULUS 4, REMAINDER 2);
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS search_sessions_p3
                  PARTITION OF search_sessions FOR VALUES WITH (MODULUS 4, REMAINDER 3);
                """
            )
            cur.execute("ALTER TABLE search_sessions ADD COLUMN IF NOT EXISTS last_ip TEXT")
            cur.execute("ALTER TABLE search_sessions ADD COLUMN IF NOT EXISTS last_user_agent TEXT")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_search_sessions_user_time ON search_sessions(user_id, last_activity_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_search_sessions_space_time ON search_sessions(space_id, last_activity_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_search_sessions_id ON search_sessions(id)")

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS search_activity (
                    id BIGSERIAL,
                    session_id TEXT NOT NULL,
                    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    space_id BIGINT REFERENCES spaces(id) ON DELETE SET NULL,
                    activity_type TEXT NOT NULL,
                    request_payload JSONB DEFAULT '{}'::jsonb,
                    response_payload JSONB DEFAULT '{}'::jsonb,
                    summary TEXT,
                    client_ip TEXT,
                    user_agent TEXT,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    PRIMARY KEY (id, created_at)
                ) PARTITION BY RANGE (created_at);
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS search_activity_default
                  PARTITION OF search_activity DEFAULT;
                """
            )
            cur.execute("ALTER TABLE search_activity ADD COLUMN IF NOT EXISTS client_ip TEXT")
            cur.execute("ALTER TABLE search_activity ADD COLUMN IF NOT EXISTS user_agent TEXT")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_search_activity_session_time ON search_activity(session_id, created_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_search_activity_user_time ON search_activity(user_id, created_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_search_activity_space_time ON search_activity(space_id, created_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_search_activity_type_time ON search_activity(activity_type, created_at DESC)")

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

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS deep_research_conversations (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    space_id BIGINT REFERENCES spaces(id) ON DELETE SET NULL,
                    conversation_id TEXT UNIQUE NOT NULL,
                    title TEXT,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    updated_at TIMESTAMPTZ DEFAULT now()
                );
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_dr_conversations_user_time
                  ON deep_research_conversations(user_id, updated_at DESC);
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_dr_conversations_space_time
                  ON deep_research_conversations(space_id, updated_at DESC);
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS deep_research_steps (
                    id BIGSERIAL PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES deep_research_conversations(conversation_id) ON DELETE CASCADE,
                    step_index INT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    context_refs JSONB DEFAULT '[]'::jsonb,
                    metadata JSONB DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    UNIQUE(conversation_id, step_index)
                );
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_dr_steps_convo_time
                  ON deep_research_steps(conversation_id, created_at DESC);
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS deep_research_notebook_entries (
                    id BIGSERIAL PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES deep_research_conversations(conversation_id) ON DELETE CASCADE,
                    title TEXT,
                    content TEXT NOT NULL,
                    source JSONB DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    updated_at TIMESTAMPTZ DEFAULT now()
                );
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_dr_notebook_convo_time
                  ON deep_research_notebook_entries(conversation_id, created_at DESC);
                """
            )

            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS conversation_external_docs (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    space_id BIGINT REFERENCES spaces(id) ON DELETE SET NULL,
                    conversation_id TEXT NOT NULL,
                    url TEXT NOT NULL,
                    parent_url TEXT,
                    depth INT DEFAULT 0,
                    chunk_index INT NOT NULL,
                    title TEXT,
                    content TEXT NOT NULL,
                    snippet TEXT,
                    content_hash TEXT,
                    metadata JSONB DEFAULT '{{}}'::jsonb,
                    embedding vector({dim}),
                    created_at TIMESTAMPTZ DEFAULT now(),
                    updated_at TIMESTAMPTZ DEFAULT now(),
                    UNIQUE(user_id, conversation_id, url, chunk_index)
                );
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_conversation_external_docs_user_convo
                  ON conversation_external_docs(user_id, conversation_id, created_at DESC);
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_conversation_external_docs_space
                  ON conversation_external_docs(space_id, conversation_id, created_at DESC);
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_conversation_external_docs_url
                  ON conversation_external_docs(url);
                """
            )
            cur.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_conversation_external_docs_embedding_ivfflat
                  ON conversation_external_docs USING ivfflat (embedding {opclass})
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


def init_db_with_retry(s: Settings = settings) -> None:
    if not bool(s.db_startup_retry_enabled):
        init_db(s)
        return

    max_wait = max(0, int(s.db_startup_max_wait_seconds))
    initial_delay = max(0.5, float(s.db_startup_initial_retry_delay_seconds))
    max_delay = max(initial_delay, float(s.db_startup_max_retry_delay_seconds))
    backoff = max(1.0, float(s.db_startup_backoff_multiplier))
    deadline = time.monotonic() + max_wait
    delay = initial_delay
    attempt = 0

    while True:
        attempt += 1
        try:
            init_db(s)
            if attempt > 1:
                logger.info("Database startup succeeded after %s attempts", attempt)
            return
        except (PoolTimeout, psycopg.OperationalError, psycopg.Error, OSError, RuntimeError, ValueError) as exc:
            close_pool()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.exception(
                    "Database startup failed after %s attempts and waiting up to %ss",
                    attempt,
                    max_wait,
                )
                raise
            sleep_for = min(delay, remaining)
            logger.warning(
                "Database not ready at startup (attempt %s): %s. Retrying in %.1fs (%.1fs remaining)",
                attempt,
                exc,
                sleep_for,
                remaining,
            )
            time.sleep(sleep_for)
            delay = min(max_delay, delay * backoff)


def set_search_runtime(cur: psycopg.Cursor, probes: int):
    # SET LOCAL cannot use bind parameters for the value; interpolate safely as a literal
    from psycopg import sql
    cur.execute(sql.SQL("SET LOCAL ivfflat.probes = {}" ).format(sql.Literal(int(probes))))
