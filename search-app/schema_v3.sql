-- Consolidated schema for a fresh V3 database
-- Includes OCR + NL2SQL role support in a single create script

-- Optional env-style parameters (psql \set) you can override before running:
-- \set EMBEDDING_DIM 1024
-- \set IMAGE_EMBED_DIM 768
-- \set PGVECTOR_METRIC cosine
-- \set PGVECTOR_LISTS 100
-- \set FTS_CONFIG english

-- Apply defaults if not provided by psql \set
\if :{?EMBEDDING_DIM}
\else
  \set EMBEDDING_DIM 1024
\endif
\if :{?IMAGE_EMBED_DIM}
\else
  \set IMAGE_EMBED_DIM 768
\endif
\if :{?PGVECTOR_METRIC}
\else
  \set PGVECTOR_METRIC cosine
\endif
\if :{?PGVECTOR_LISTS}
\else
  \set PGVECTOR_LISTS 100
\endif
\if :{?FTS_CONFIG}
\else
  \set FTS_CONFIG english
\endif

-- Choose vector opclass based on PGVECTOR_METRIC
\if :PGVECTOR_METRIC == 'cosine'
  \set PGVECTOR_OPCLASS vector_cosine_ops
\elif :PGVECTOR_METRIC == 'l2'
  \set PGVECTOR_OPCLASS vector_l2_ops
\else
  \set PGVECTOR_OPCLASS vector_ip_ops
\endif

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;

CREATE TABLE IF NOT EXISTS roles (
  id BIGSERIAL PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  description TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);

INSERT INTO roles (name, description)
VALUES
  ('user', 'Default user, no SQL access'),
  ('analyst', 'Analyst: NL2SQL across all spaces'),
  ('admin', 'Admin: NL2SQL + system queries')
ON CONFLICT (name) DO NOTHING;

CREATE TABLE IF NOT EXISTS users (
  id BIGSERIAL PRIMARY KEY,
  email CITEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  role_id BIGINT REFERENCES roles(id),
  created_at TIMESTAMPTZ DEFAULT now(),
  last_login_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_users_role_id ON users(role_id);

CREATE TABLE IF NOT EXISTS spaces (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  is_default BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(user_id, name)
);

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
  metadata JSONB DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_documents_user_space ON documents(user_id, space_id, created_at DESC);

CREATE TABLE IF NOT EXISTS chunks (
  id BIGSERIAL PRIMARY KEY,
  document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  chunk_index INT NOT NULL,
  content TEXT NOT NULL,
  content_tsv tsvector GENERATED ALWAYS AS (to_tsvector(:'FTS_CONFIG', content)) STORED,
  content_chars INT,
  embedding vector(:EMBEDDING_DIM),
  embedding_model TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_chunks_doc_chunk ON chunks(document_id, chunk_index);
CREATE INDEX IF NOT EXISTS idx_chunks_tsv ON chunks USING GIN (content_tsv);
CREATE INDEX IF NOT EXISTS idx_chunks_embedding_ivfflat
  ON chunks USING ivfflat (embedding :PGVECTOR_OPCLASS)
  WITH (lists = :PGVECTOR_LISTS);

CREATE TABLE IF NOT EXISTS user_activity (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  activity_type TEXT NOT NULL,
  details JSONB DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_user_activity_user_time ON user_activity(user_id, created_at DESC);

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
  embedding vector(:IMAGE_EMBED_DIM),
  embedding_model TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_image_assets_user_space ON image_assets(user_id, space_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_image_assets_embedding_ivfflat
  ON image_assets USING ivfflat (embedding :PGVECTOR_OPCLASS)
  WITH (lists = :PGVECTOR_LISTS);

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
  metadata JSONB DEFAULT '{}'::jsonb,
  rating SMALLINT,
  summary TEXT,
  embedding vector(:EMBEDDING_DIM),
  embedding_model TEXT,
  created_at TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);

CREATE TABLE IF NOT EXISTS memory_events_default
  PARTITION OF memory_events DEFAULT;

CREATE INDEX IF NOT EXISTS idx_memory_events_space_time
  ON memory_events(space_id, memory_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_events_space_rating
  ON memory_events(space_id, memory_type, rating, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_events_id
  ON memory_events(id);
CREATE INDEX IF NOT EXISTS idx_memory_events_embedding_ivfflat
  ON memory_events USING ivfflat (embedding :PGVECTOR_OPCLASS)
  WITH (lists = :PGVECTOR_LISTS);

-- Note: override the \set values above to match your environment settings