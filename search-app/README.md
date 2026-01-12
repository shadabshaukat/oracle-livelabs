# Enterprise Search App (Gradio + FastAPI + OCI PostgreSQL + pgvector)

An enterprise-grade, self-hosted search and RAG application featuring:
- Gradio UI for uploads and search
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

This starts FastAPI at http://0.0.0.0:8000 and Gradio UI at http://0.0.0.0:8000/ui

## Oracle Linux 8 prerequisites and firewall

```bash
# Install OS packages
sudo dnf install -y curl git unzip firewalld oraclelinux-developer-release-el10 python3-oci-cli

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
- Security: BASIC_AUTH_USER, BASIC_AUTH_PASSWORD (protects /api and /ui)
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

Gradio UI is available at /ui with Upload and Search tabs.

## Security Notes

- All API and UI routes are protected with HTTP Basic Auth via BASIC_AUTH_USER/PASSWORD (including /docs, /openapi.json, /redoc).
- CORS is enabled by default; restrict ALLOW_CORS in production.
- The upload endpoint writes files to storage/uploads; ensure filesystem quotas and scanning if needed.
- Use SSL to your PostgreSQL (sslmode=require by default).

## Chunking strategy

- Uses a recursive character splitter inspired by LangChain's RecursiveCharacterTextSplitter with separators (\n\n, \n, ". ", " ", "").
- Configurable chunk_size and chunk_overlap in the UI for ingestion.
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
