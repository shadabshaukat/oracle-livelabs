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

CREATE TABLE IF NOT EXISTS search_sessions_p0
  PARTITION OF search_sessions FOR VALUES WITH (MODULUS 4, REMAINDER 0);
CREATE TABLE IF NOT EXISTS search_sessions_p1
  PARTITION OF search_sessions FOR VALUES WITH (MODULUS 4, REMAINDER 1);
CREATE TABLE IF NOT EXISTS search_sessions_p2
  PARTITION OF search_sessions FOR VALUES WITH (MODULUS 4, REMAINDER 2);
CREATE TABLE IF NOT EXISTS search_sessions_p3
  PARTITION OF search_sessions FOR VALUES WITH (MODULUS 4, REMAINDER 3);

CREATE INDEX IF NOT EXISTS idx_search_sessions_user_time ON search_sessions(user_id, last_activity_at DESC);
CREATE INDEX IF NOT EXISTS idx_search_sessions_space_time ON search_sessions(space_id, last_activity_at DESC);
CREATE INDEX IF NOT EXISTS idx_search_sessions_id ON search_sessions(id);

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

CREATE TABLE IF NOT EXISTS search_activity_default
  PARTITION OF search_activity DEFAULT;

CREATE INDEX IF NOT EXISTS idx_search_activity_session_time ON search_activity(session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_search_activity_user_time ON search_activity(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_search_activity_space_time ON search_activity(space_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_search_activity_type_time ON search_activity(activity_type, created_at DESC);

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

-- Deep Research (agentic AI)
CREATE TABLE IF NOT EXISTS deep_research_conversations (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  space_id BIGINT REFERENCES spaces(id) ON DELETE SET NULL,
  conversation_id TEXT UNIQUE NOT NULL,
  title TEXT,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dr_conversations_user_time
  ON deep_research_conversations(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_dr_conversations_space_time
  ON deep_research_conversations(space_id, updated_at DESC);

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

CREATE INDEX IF NOT EXISTS idx_dr_steps_convo_time
  ON deep_research_steps(conversation_id, created_at DESC);

CREATE TABLE IF NOT EXISTS deep_research_notebook_entries (
  id BIGSERIAL PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES deep_research_conversations(conversation_id) ON DELETE CASCADE,
  title TEXT,
  content TEXT NOT NULL,
  source JSONB DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dr_notebook_convo_time
  ON deep_research_notebook_entries(conversation_id, created_at DESC);

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
  metadata JSONB DEFAULT '{}'::jsonb,
  embedding vector(:EMBEDDING_DIM),
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(user_id, conversation_id, url, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_conversation_external_docs_user_convo
  ON conversation_external_docs(user_id, conversation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_conversation_external_docs_space
  ON conversation_external_docs(space_id, conversation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_conversation_external_docs_url
  ON conversation_external_docs(url);
CREATE INDEX IF NOT EXISTS idx_conversation_external_docs_embedding_ivfflat
  ON conversation_external_docs USING ivfflat (embedding :PGVECTOR_OPCLASS)
  WITH (lists = :PGVECTOR_LISTS);

-- Note: override the \set values above to match your environment settings