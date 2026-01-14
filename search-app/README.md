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
- Storage backend:
  - STORAGE_BACKEND=local|oci|both (default local)
  - OCI_OS_BUCKET_NAME (required when STORAGE_BACKEND includes 'oci')
  - Files are saved locally under storage/uploads/YYYY/MM/DD/HHMMSS/<filename>; when using 'oci' or 'both', the same object path is used in OCI Object Storage and its URL is stored in document metadata as object_url. The UI shows a clickable link in References when available.
  - OCI-only streaming: When STORAGE_BACKEND=oci, uploads stream directly to OCI without loading the whole file in RAM. A SpooledTemporaryFile is used for ingestion (in-memory up to 2MB, then disk; auto-deleted after use) for memory safety with large files.
- RAG LLM provider:
  - OpenAI: set LLM_PROVIDER=openai and OPENAI_API_KEY
  - OCI GenAI (preferred for this app): set LLM_PROVIDER=oci and configure:
    - OCI_REGION (e.g., us-chicago-1)
    - OCI_GENAI_ENDPOINT (e.g., https://inference.generativeai.us-chicago-1.oci.oraclecloud.com)
    - OCI_COMPARTMENT_OCID
    - OCI_GENAI_MODEL_ID (chat-capable model in the chosen region)
    - Auth via either:
      - OCI_CONFIG_FILE + OCI_CONFIG_PROFILE (recommended), or
      - API key envs: OCI_TENANCY_OCID, OCI_USER_OCID, OCI_FINGERPRINT, OCI_PRIVATE_KEY_PATH, OCI_REGION

## Endpoints

- GET /api/health
- GET /api/ready (DB readiness: checks extensions, tables, and indexes)
- POST /api/upload (multipart) files[]
- POST /api/search { query, mode: semantic|fulltext|hybrid|rag, top_k }
- GET /api/llm-config (OCI LLM config snapshot – provider/region/endpoint; compartment/model presence)
- POST /api/llm-test ({question, context}) – verifies LLM connectivity; returns ok + chat_ok/text_ok
- GET/POST /api/llm-debug ({question, context}) – diagnostic shape/fields for OCI responses

UI
- Root at /. Includes: Search, Upload, Status tabs; RAG answer shows an “LLM answer” badge when the model is used.
- RAG answers include a “References” list (file name, type, and a chunk anchor). Full source paths are not exposed.

Cache busting tip: Hard refresh (Shift+Reload) or open http://0.0.0.0:8000/?v=2 if you’ve just updated templates.

## RAG and OCI GenAI

- This app uses the OCI Generative AI chat API with OnDemandServingMode(model_id=…). Requests include:
  - ChatDetails(compartment_id, serving_mode)
  - GenericChatRequest(api_format=GENERIC, messages=[SYSTEM, USER], max_tokens, temperature)
- The SYSTEM prompt enforces: “Answer directly from the provided context. If insufficient, say ‘No answer found in the provided context.’ Do not ask for more input.”
- The USER message contains both the question and context.
- The app extracts text from multiple OCI response shapes, including ChatResult.chat_response.
- A generate_text fallback is present but not required for models that prefer chat (generate_text may return 400 in those cases and is ignored).

Example LLM test (with Basic Auth):

```bash
curl -u admin:letmein -sS -X POST http://0.0.0.0:8000/api/llm-test \
  -H 'Content-Type: application/json' \
  -d '{"question":"Summarize Australia in one sentence","context":"Australia is a country and continent surrounded by the Indian and Pacific oceans."}'
```

Note: Avoid trailing characters after the JSON body (a trailing dot will cause 422 JSON decode error).

## Search Mode Curl Examples

All search endpoints require Basic Auth and a JSON body with "query" and optional "mode" (defaults to hybrid), "top_k" (defaults to 25).

- Semantic:
```bash
curl -u admin:letmein -sS -X POST http://0.0.0.0:8000/api/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"MySQL HeatWave loading tables","mode":"semantic","top_k":5}'
```

- Full-text:
```bash
curl -u admin:letmein -sS -X POST http://0.0.0.0:8000/api/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"MySQL HeatWave loading tables","mode":"fulltext","top_k":5}'
```

- Hybrid:
```bash
curl -u admin:letmein -sS -X POST http://0.0.0.0:8000/api/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"MySQL HeatWave loading tables","mode":"hybrid","top_k":5}'
```

- RAG:
```bash
curl -u admin:letmein -sS -X POST http://0.0.0.0:8000/api/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"Tell me about MySQL HeatWave","mode":"rag","top_k":10}'
```

## Chunking strategy

- Uses a recursive character splitter inspired by LangChain’s RecursiveCharacterTextSplitter with separators (\n\n, \n, ". ", " ", "").
- Defaults: chunk_size=2500 and chunk_overlap=250 (tune in code or via the UI ingest parameters).
- The order of separators ensures we prefer paragraph and sentence boundaries before falling back to word and character splits.
- Supports PDF, HTML, TXT, and DOCX extraction. For PDFs, you can set USE_PYMUPDF=true to prefer higher-quality extraction.

## Scaling to 10M vectors

- Choose a higher-dimension model if quality demands (adjust EMBEDDING_DIM accordingly).
- Increase PGVECTOR_LISTS as the number of vectors grows (~sqrt(n) guideline). Reindex as needed:
  - ALTER INDEX idx_chunks_embedding_ivfflat SET (lists = <new_lists>);
  - REINDEX INDEX CONCURRENTLY idx_chunks_embedding_ivfflat; (may require maintenance window)
- Tune ivfflat.probes per query (PGVECTOR_PROBES); higher improves recall at more CPU.
- Use batched ingestion; this app uses executemany to reduce round-trips. For massive imports, consider COPY.
- Ensure adequate CPU/RAM, and enable autovacuum and regular ANALYZE on chunks.

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

- 422 JSON decode error from curl commands:
  - Ensure there are no trailing characters (e.g., trailing dot) after the JSON body.

- LLM test ok=false or empty answer (OCI):
  - Confirm .env has: LLM_PROVIDER=oci, region + endpoint + compartment + model ID.
  - Verify OCI Generative AI is enabled for your tenancy/compartment in that region.
  - Prefer chat path (generate_text may return 400 for chat-only models; that is expected and ignored).

- Database configuration missing at startup:
  - Ensure search-app/.env contains either DATABASE_URL or DB_HOST/DB_NAME/DB_USER/DB_PASSWORD.
  - The app auto-loads .env on startup via python-dotenv.

- Embedding dimension mismatch errors during ingestion (e.g., 384 vs 768):
  - EMBEDDING_DIM must match the chosen EMBEDDING_MODEL (MiniLM-L6-v2 -> 384).
  - If you created the schema with the wrong dimension, recreate/alter the column + index, or drop tables and restart to rebuild schema.

- Connectivity/SSL issues to PostgreSQL:
  - Default is DB_SSLMODE=require. Adjust as needed for your environment.

- PDF extraction quality:
  - Set USE_PYMUPDF=true to prefer PyMuPDF if installed (also enable the optional `pdf` dependency group).
