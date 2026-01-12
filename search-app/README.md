# Enterprise Search App (FastAPI + OCI PostgreSQL + pgvector)

An enterprise-grade, self-hosted search and RAG application featuring:
- Minimal FastAPI + Jinja UI for uploads and search
- FastAPI backend
- OCI PostgreSQL with pgvector for embeddings and GIN for full-text
- Multi-mode retrieval: Semantic, Full-text, Hybrid, and RAG
- Designed to scale to ~10M embeddings with IVFFlat and tunable params
- One-command deployment using uv (creates/uses a virtual environment)

## Features

- Upload PDF, HTML, TXT, DOCX. The app extracts, cleans, chunks, embeds, and stores content.
- Search modes:
  - Semantic (pgvector cosine/L2/IP)
  - Full-text (PostgreSQL FTS using GIN index)
  - Hybrid (RRF fusion over semantic + FTS)
  - RAG (optional LLM synthesis; OpenAI or OCI GenAI supported)
- Robust schema and indexes:
  - documents(id, source_path, source_type, title, metadata)
  - chunks(id, document_id, chunk_index, content, content_tsv, content_chars, embedding, embedding_model)
  - Indexes: GIN(content_tsv), IVFFlat(embedding) with opclass per metric, unique(doc_id, chunk_index)

## Requirements

- Linux x86_64 (Oracle Linux 8 recommended)
- Python 3.10+
- uv package manager (https://docs.astral.sh/uv/)
- OCI PostgreSQL reachable from the host
- pgvector extension enabled (the app will create it if permitted)

## Quick Start (One Command)

1) Copy environment template and edit values:

```bash
cp .env.example .env
# Edit DB connection and BASIC_AUTH/OCI values
```

2) Install deps and run server (uv will create/use a project virtual environment):

```bash
uv sync && uv run searchapp
```

This starts FastAPI at http://0.0.0.0:8000. The UI is available at http://0.0.0.0:8000/

## Oracle Linux 8 prerequisites and firewall

```bash
# Install OS packages
sudo dnf install -y curl git unzip firewalld oraclelinux-developer-release-el10 python3-oci-cli postgresql16

# Install uv (user-local) and add to PATH
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

# Enable firewall and open port 8000/tcp for the app
sudo systemctl enable --now firewalld
sudo firewall-cmd --permanent --add-port=8000/tcp
sudo firewall-cmd --reload
```

## Configuration

Environment variables (see .env.example):
- DATABASE_URL or DB_HOST/DB_NAME/DB_USER/DB_PASSWORD
- Security: BASIC_AUTH_USER, BASIC_AUTH_PASSWORD (protects / and /api)
- EMBEDDING_MODEL, EMBEDDING_DIM (default MiniLM 384)
- PGVECTOR_METRIC (cosine|l2|ip), PGVECTOR_LISTS (~sqrt(n)), PGVECTOR_PROBES (runtime probes)
- FTS_CONFIG (default english)
- RAG LLM provider:
  - OpenAI: set LLM_PROVIDER=openai and OPENAI_API_KEY
  - OCI GenAI: set LLM_PROVIDER=oci and configure OCI_REGION, OCI_COMPARTMENT_OCID, OCI_GENAI_ENDPOINT, OCI_GENAI_MODEL_ID plus either OCI_CONFIG_FILE/PROFILE or API key envs (TENANCY/USER/FINGERPRINT/PRIVATE_KEY_PATH)

## Scaling to 10M vectors

- Choose a higher-dimension model if quality demands (adjust EMBEDDING_DIM accordingly).
- Increase PGVECTOR_LISTS as the number of vectors grows (~sqrt(n) guideline). Reindex as needed:
  - ALTER INDEX idx_chunks_embedding_ivfflat SET (lists = <new_lists>);
  - REINDEX INDEX CONCURRENTLY idx_chunks_embedding_ivfflat; (may require maintenance window)
- Tune ivfflat.probes per query (PGVECTOR_PROBES); higher improves recall at more CPU.
- Use batched ingestion; this app uses executemany to reduce round-trips. For massive imports, consider COPY.
- Ensure adequate CPU/RAM, and enable autovacuum and regular ANALYZE on chunks.

## Endpoints

- GET /api/health
- GET /api/ready (DB readiness: checks extensions, tables, and indexes)
- POST /api/upload (multipart) files[]
- POST /api/search { query, mode: semantic|fulltext|hybrid|rag, top_k }

Minimal UI is available at / (root) with Search, Upload and Status sections.

## Security Notes

- All API and UI routes are protected with HTTP Basic Auth via BASIC_AUTH_USER/PASSWORD (including /docs, /openapi.json, /redoc).
- CORS is enabled by default; restrict ALLOW_CORS in production.
- The upload endpoint writes files to storage/uploads; ensure filesystem quotas and scanning if needed.
- Use SSL to your PostgreSQL (sslmode=require by default).

## Chunking strategy

- Uses a recursive character splitter inspired by LangChain's RecursiveCharacterTextSplitter with separators (\n\n, \n, ". ", " ", "").
- Defaults: chunk_size=1000 and chunk_overlap=200 (adjust in code if needed).
- Supports PDF, HTML, TXT, and DOCX extraction.

## Idempotent schema

- On startup, the app runs CREATE EXTENSION/TABLE/INDEX IF NOT EXISTS. Subsequent runs will not recreate the schema.

## Systemd unit (optional)

```ini
[Unit]
Description=Enterprise Search App
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/search-app
EnvironmentFile=/opt/search-app/.env
ExecStart=/usr/bin/env uv run searchapp
Restart=always
User=searchapp
Group=searchapp

[Install]
WantedBy=multi-user.target
```

## Troubleshooting

- Database configuration missing at startup:
  - Ensure a .env file exists at search-app/.env (same folder as pyproject.toml) and contains either DATABASE_URL or DB_HOST/DB_NAME/DB_USER/DB_PASSWORD.
  - The app now auto-loads .env on startup via python-dotenv. No need to export variables manually when using `uv run searchapp`.
  - If you prefer shell environment variables, ensure they are exported in the same shell that runs the app.

- Embedding dimension mismatch errors during ingestion (e.g., 384 vs 768):
  - EMBEDDING_DIM must match the chosen EMBEDDING_MODEL. For the default `sentence-transformers/all-MiniLM-L6-v2`, set EMBEDDING_DIM=384.
  - If you previously created the schema with the wrong dimension, you can fix it by recreating or altering the column and index:
    ```sql
    -- Option A: Drop and recreate for a clean slate (will remove data)
    DROP INDEX IF EXISTS idx_chunks_embedding_ivfflat;
    ALTER TABLE chunks ALTER COLUMN embedding TYPE vector(384) USING embedding;
    CREATE INDEX idx_chunks_embedding_ivfflat ON chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 1000);
    -- Ensure documents/chunks tables, extensions, and other indexes exist (app’s startup ensures IF NOT EXISTS)
    ```
  - Alternatively, start fresh by dropping tables if you’re not preserving data:
    ```sql
    DROP TABLE IF EXISTS chunks CASCADE;
    DROP TABLE IF EXISTS documents CASCADE;
    ```
    Then restart the app to have it recreate the schema.

- Connectivity/SSL issues to PostgreSQL:
  - Default is `DB_SSLMODE=require`. Adjust `DB_SSLMODE` if your environment needs `verify-ca` or `disable` (not recommended for production).
  - Verify networking/firewalls allow connections from the app host to the DB host/port.

- Basic Auth credentials:
  - All API and UI endpoints are protected. Set BASIC_AUTH_USER and BASIC_AUTH_PASSWORD in .env.

- PDF extraction quality:
  - Set `USE_PYMUPDF=true` to prefer PyMuPDF if installed (also enable the optional `pdf` dependency group).
