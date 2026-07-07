# Enterprise Search App (FastAPI + OCI PostgreSQL + pgvector)

An enterprise-grade, self-hosted search and RAG application featuring:
- Minimal FastAPI + Jinja UI for uploads and search
- FastAPI backend
- OCI PostgreSQL with pgvector for embeddings and GIN for full-text
- Multi-mode retrieval: Semantic, Full-text, Hybrid, and RAG
- Designed to scale to ~10M embeddings with IVFFlat and tunable params
- Multi-user accounts with per-user spaces and session cookie auth
- Image search with OpenCLIP embeddings stored in PostgreSQL
- Reproducible Linux bootstrap for pinned uv, Python, Ollama, model digest, and Python dependency lock

## Features

- Upload PDF, HTML, TXT, DOCX. The app extracts, cleans, chunks, embeds, and stores content.
- Upload images (PNG/JPEG/etc.) to enable image similarity search and tagging.
- Search modes:
  - Semantic (pgvector cosine/L2/IP)
  - Full-text (PostgreSQL FTS using GIN index)
  - Hybrid (weighted RRF fusion over semantic + FTS)
  - RAG (local Ollama synthesis by default; optional hosted providers remain supported)
- Image search (OpenCLIP + pgvector) with optional text+tag filtering
- Deep Research mode with persistent conversations, notebook, follow-ups, and optional web search
- Robust schema and indexes:
  - documents(id, source_path, source_type, title, metadata)
  - chunks(id, document_id, chunk_index, content, content_tsv, content_chars, embedding, embedding_model)
  - Indexes: GIN(content_tsv), IVFFlat(embedding) with opclass per metric, unique(doc_id, chunk_index)

## How Search Works (Deep Dive)

### Text Search Pipeline
1) **Query intake**: `/api/search` receives {query, mode, top_k, space_id}.
2) **Embedding** (semantic): query embedded with `EMBEDDING_MODEL`.
3) **Vector search**: pgvector ANN query (IVFFlat + metric) returns top_k chunks.
4) **Full‑text search**: PostgreSQL `tsvector` + `ts_rank_cd` (GIN) returns top_k chunks.
5) **Hybrid**: Weighted Reciprocal Rank Fusion merges semantic + FTS and preserves both vector distance and lexical rank.
6) **RAG**: Weak cosine-only hits are filtered, duplicate text is removed, and up to six numbered source blocks are sent to the pinned local model. The answer cites sources; raw chunks are never returned as an answer when inference fails.

### Image Search Pipeline
1) **Query intake**: `/api/image-search` accepts text, tags, or a reference image.
2) **Embedding**: OpenCLIP embeds text or image to a vector.
3) **Vector search**: pgvector similarity against stored image vectors.
4) **Result shaping**: returns caption/tags + `thumbnail_url` (served by `/api/image-assets/{id}/thumbnail`) for the UI.

### Deep Research Pipeline
1) **Conversation start**: `/api/deep-research/start` creates a Postgres-backed conversation per space.
2) **Ask**: `/api/deep-research/ask` stores a user step, runs research (local + optional web), and persists assistant steps + references.
3) **Notebook**: `/api/deep-research/notebook/{conversation_id}` stores pinned insights.
4) **Follow-ups**: suggested follow-ups return in assistant metadata; users respond via a modal which sends a combined prompt back through `/api/deep-research/ask`.
5) **Persistent memory**: DR can opt into the same `memory_events` pipeline as text/sql when enabled.

### Persistent Memory + Session Activity Flow
```mermaid
flowchart TD
    A[/User request: /api/search | /api/sql-search | /api/image-search | /api/deep-research/ask/] --> B[Session auth + space context]
    B --> C[Endpoint handler (main.py/deep_research.py)]
    C --> D{Persistent memory enabled?}
    D -- Yes --> E[_fetch_persistent_memory(user_id, space_id, memory_type)]
    E --> F[(memory_events
    rating=1 + pgvector)]
    D -- No --> G[Skip memory context]
    C --> H[Search pipeline
    semantic | fulltext | hybrid | rag | sql | image]
    H --> I[Results + optional LLM synthesis]
    C --> J{Persist memory?}
    J -- Yes --> K[_persist_memory_event → memory_events]
    C --> L[_log_search_activity]
    L --> M[(search_sessions)]
    L --> N[(search_activity)]
    I --> O[Response to UI]
```

**Notes:**
- Memory retrieval and search queries are always scoped by `user_id` and optional `space_id`.
- Only `memory_events` with `rating=1` are retrieved for prompt context.

## Text Ingestion Lifecycle (All File Types)

1) **Upload**
   - `/api/upload` accepts PDF, DOCX/DOC, TXT, HTML/HTM, MD, CSV, JSON, XML, PPTX, XLSX/XLS.
   - Files are saved locally and optionally mirrored to OCI Object Storage.

2) **Extraction** (`text_utils.py`)
   - **PDF**: PyMuPDF → pypdf → pdfplumber fallback (tables supported).
   - **DOCX/DOC**: python-docx with system fallbacks (textutil/antiword/strings).
   - **TXT/MD**: plain text normalization.
   - **HTML/XML**: BeautifulSoup extraction with cleanup.
   - **CSV/JSON/XLSX/PPTX**: structured extraction into text blocks.

3) **Normalization + Chunking**
   - Paragraph preservation, header/footer cleanup, heading boundaries.
   - `CHUNK_STRATEGY` + `SENTENCE_SPLITTER` control chunking behavior.

4) **Embedding**
   - SentenceTransformers generates normalized vectors.

5) **Persist**
   - Documents stored in `documents`.
   - Chunks stored in `chunks` with embeddings + full-text index.

## Image Ingestion Lifecycle

1) **Upload**
   - Image files are saved locally and optionally mirrored to OCI Object Storage.

2) **Thumbnail + Caption**
   - Thumbnails created (512px max).
   - Captioning model generates human‑readable description + keywords.

3) **Embedding**
   - OpenCLIP encodes image to a semantic vector.

4) **Persist**
   - Stored in `image_assets` with thumbnail path, caption, tags, embedding.
   - Document metadata updated with thumbnail + caption details.

## Requirements

- Linux x86_64 or ARM64 with systemd (Oracle Linux 9 or a comparable distribution)
- 2-4 OCPUs, at least 8 GB RAM, and at least 8 GB free disk
- `sudo`, `curl`, GNU tar, `sha256sum`, `ss`, and `flock` (util-linux)
- OCI PostgreSQL reachable from the host
- pgvector extension enabled (the app will create it if permitted)

## Quick Start (One Command)

1) Copy environment template and edit values:

```bash
cp .env.example .env
# Edit DB connection and application secrets. Ollama values are already pinned.
```

2) Install anything missing and run the server:

```bash
./run.sh
```

This starts FastAPI at http://0.0.0.0:8000. The UI is available at http://0.0.0.0:8000/

`run.sh` automatically invokes the pinned bootstrap when uv, managed Python, Ollama, or the model is missing. On
consecutive runs it skips bootstrap, service restart, registry access, and model pull. If the exact model is
installed but absent from `/api/ps`, it is preloaded; if already resident, no inference request is sent.

### Rebuild and upgrade policy

```bash
./stop.sh
CLEAN_BUILD=1 FORCE_OLLAMA_REINSTALL=1 ./bootstrap_linux.sh
./start.sh
```

Do not use `latest`, plain `pip install`, or `uv lock --upgrade` in deployment. A deliberate upgrade must update
`deploy/versions.env`, the matching binary/model checksums, `pyproject.toml` plus `uv.lock`, and then pass the clean
end-to-end PDF RAG test. Existing documents must be re-ingested to benefit from changed chunking defaults; the
embedding revision remains pinned to the revision used by existing stored vectors.

## Fresh Database Bootstrap (V3 schema)

Use the consolidated schema script when provisioning a new database:

```bash
psql "$DATABASE_URL" -f schema_v3.sql
```

You can override schema parameters (embedding dims, metric, lists, FTS config) using psql \set variables:

```bash
psql "$DATABASE_URL" \
  -v EMBEDDING_DIM=384 \
  -v IMAGE_EMBED_DIM=768 \
  -v PGVECTOR_METRIC=cosine \
  -v PGVECTOR_LISTS=100 \
  -v FTS_CONFIG=english \
  -f schema_v3.sql
```

## Oracle Linux prerequisites and firewall

```bash
# Install OS packages
sudo dnf install -y curl git unzip firewalld oraclelinux-developer-release-el10 python3-oci-cli postgresql16

# Enable firewall and open port 8000/tcp for the app
sudo systemctl enable --now firewalld
sudo firewall-cmd --permanent --add-port=8000/tcp
sudo firewall-cmd --reload
```

Do **not** open TCP 11434. Ollama is installed as a system service bound only to `127.0.0.1:11434`; its local API
has no authentication. The app reaches it over loopback, so no OCI NSG/security-list or firewalld ingress rule is
required. Outbound HTTPS is required during bootstrap to fetch checksum-verified artifacts and model blobs.

## Configuration

Environment variables (see .env.example):
- DATABASE_URL or DB_HOST/DB_NAME/DB_USER/DB_PASSWORD
- Security: BASIC_AUTH_USER, BASIC_AUTH_PASSWORD (protects /api for non-session use)
- Session auth for UI login/register: SECRET_KEY, SESSION_COOKIE_NAME, SESSION_MAX_AGE_SECONDS, COOKIE_SAMESITE, COOKIE_SECURE, ALLOW_REGISTRATION
- EMBEDDING_MODEL, immutable EMBEDDING_MODEL_REVISION, EMBEDDING_DIM (default MiniLM 384)
- PGVECTOR_METRIC (cosine|l2|ip), PGVECTOR_LISTS (~sqrt(n)), PGVECTOR_PROBES (runtime probes)
- FTS_CONFIG (default english)
- Image search:
  - ENABLE_IMAGE_STORAGE (store images + embeddings)
  - IMAGE_EMBED_MODEL/IMAGE_EMBED_DIM (OpenCLIP settings)
  - IMAGE_EMBED_DEVICE (cpu/cuda)
  - IMAGE_SEARCH_TEXT_WEIGHT, IMAGE_SEARCH_VECTOR_WEIGHT
  - IMAGE_KEYWORD_MAX (max tags stored per image)
- Storage backend:
  - STORAGE_BACKEND=local|oci|both (default local)
  - OCI_OS_BUCKET_NAME (required when STORAGE_BACKEND includes 'oci')
- Files are saved locally under storage/uploads/YYYY/MM/DD/HHMMSS/<filename>; when using 'oci' or 'both', the same object path is used in OCI Object Storage and the **object identifiers** (provider/bucket/object name) are stored in document metadata. Downloads/thumbnails are streamed by the app via the OCI SDK (no PAR URLs).
- OCI-only streaming: When STORAGE_BACKEND=oci, uploads stream directly to OCI without loading the whole file in RAM. A SpooledTemporaryFile is used for ingestion (in-memory up to 2MB, then disk; auto-deleted after use) for memory safety with large files.
- Upload limits:
  - MAX_UPLOAD_SIZE_MB (per-file size cap)
  - MAX_FILES_PER_SPACE (maximum files in a space)
  - ALLOWED_UPLOAD_EXTENSIONS (comma-separated allowlist; blank allows all)
- RAG LLM provider:
  - Default: `LLM_PROVIDER=ollama`, `OLLAMA_BASE_URL=http://127.0.0.1:11434`
  - Pinned model: `ibm/granite4:1b-q4_K_M`, manifest digest in `deploy/versions.env`
  - The app uses `/api/chat`, an 8K runtime context, one local parallel request, and a 300-second CPU timeout.
  - OpenAI: set LLM_PROVIDER=openai and OPENAI_API_KEY (optional compatibility path)
  - OCI GenAI: set LLM_PROVIDER=oci and configure (optional compatibility path):
    - OCI_REGION (e.g., us-chicago-1)
    - OCI_GENAI_ENDPOINT (e.g., https://inference.generativeai.us-chicago-1.oci.oraclecloud.com)
    - OCI_COMPARTMENT_OCID
    - OCI_GENAI_MODEL_ID (chat-capable model in the chosen region)
    - Auth via either:
      - OCI_CONFIG_FILE + OCI_CONFIG_PROFILE (recommended), or
      - API key envs: OCI_TENANCY_OCID, OCI_USER_OCID, OCI_FINGERPRINT, OCI_PRIVATE_KEY_PATH, OCI_REGION

### Chunking Configuration
- `CHUNK_STRATEGY=recursive|sentence_pack` controls the chunker.
- `SENTENCE_SPLITTER=regex|nltk|spacy` selects sentence splitting behavior (default regex; no mutable data download).
- Defaults are `CHUNK_SIZE=1000` and `CHUNK_OVERLAP=150`, sized for MiniLM's effective input window.
- `OCR_PDF=true` enables OCR only as a fallback when native extraction is sparse; it no longer duplicates good PDF text.

## Endpoints

- GET /api/health
- GET /api/ready (DB plus pinned Ollama version/model-digest readiness; returns HTTP 503 when not ready)
- POST /api/upload (multipart) files[] (space_id optional)
- POST /api/search { query, mode: semantic|fulltext|hybrid|rag, top_k, space_id }
- POST /api/image-search (query/tags or reference image)
- POST /api/deep-research/start
- POST /api/deep-research/ask
- GET /api/deep-research/conversations?space_id=
- GET /api/deep-research/conversations/{conversation_id}
- POST /api/deep-research/conversations/{conversation_id}/title
- POST /api/deep-research/notebook/{conversation_id}
- DELETE /api/deep-research/notebook/{entry_id}
- GET /api/image-assets/{image_id}/thumbnail (thumbnail for image results)
- GET /api/doc-thumbnail?doc_id=<id> (document thumbnail for Library)
- GET /api/kb (library listing)
- Auth: /api/register, /api/login, /api/logout, /api/me
- GET /api/search-history (session history with filters + pagination)
- GET /api/search-history/{session_id} (session activity details)
- GET /api/llm-config (provider plus local Ollama version/model/digest settings; optional OCI fields)
- POST /api/llm-test ({question, context}) – verifies LLM connectivity; returns ok + chat_ok/text_ok
- GET/POST /api/llm-debug ({question, context}) – diagnostic shape/fields for OCI responses

UI
- Root at /. Includes: Search, Upload, Library, and Account (login/register) sections.
- Space selector in the top bar filters uploads/searches/library per user space.
- RAG answers include a “References” list (file name, type, and a chunk anchor). Full source paths are not exposed.
- Image search renders cards using `thumbnail_url` and shows caption/tags/score.
- Search History now includes filters (activity type, space, and time range), pagination controls, and audit metadata (session last IP/user-agent plus per-activity client IP/user-agent).
- Deep Research includes a persistent memory toggle (when enabled via env) and follow-up modal with Enter-to-send.

Cache busting tip: Hard refresh (Shift+Reload) or open http://0.0.0.0:8000/?v=2 if you’ve just updated templates.

## RAG and Local Ollama

- `bootstrap_linux.sh` installs Ollama 0.31.1 from a fixed release asset after checking its SHA-256.
- The default `ibm/granite4:1b-q4_K_M` model is about 1 GB, 1.63B parameters, CPU-quantized Q4_K_M,
  Apache-2.0 licensed, and intended for question answering, summarization, and RAG.
- The bootstrap verifies the registry manifest digest before pull and `/api/tags` afterward. If the public tag ever
  changes, the build fails instead of silently drifting.
- `run.sh` uses `/api/ps` to distinguish an installed model from a memory-resident model. The pinned model is kept
  resident with `OLLAMA_KEEP_ALIVE=-1`; `OLLAMA_MAX_LOADED_MODELS=1` bounds local model memory usage.
- Ollama uses `/api/chat` with `num_ctx=8192`, bounded output, low temperature, and thinking disabled.
- The system prompt requires answers from numbered sources, rejects instructions embedded in documents, and asks
  for `[Source N]` citations. An inference failure returns a short availability message while search hits remain
  available in the References panel.
- OCI GenAI, OpenAI, and Bedrock are still available when explicitly selected, but they are not defaults.

Example LLM test (with Basic Auth):

```bash
curl -u admin:letmein -sS -X POST http://0.0.0.0:8000/api/llm-test \
  -H 'Content-Type: application/json' \
  -d '{"question":"Summarize Australia in one sentence","context":"Australia is a country and continent surrounded by the Indian and Pacific oceans."}'
```

Note: Avoid trailing characters after the JSON body (a trailing dot will cause 422 JSON decode error).

## Search Mode Curl Examples

All search endpoints require Basic Auth and a JSON body with "query" and optional "mode" (defaults to hybrid).
RAG defaults to and is capped at `top_k=6`; non-RAG search accepts up to 50.

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
  -d '{"query":"Tell me about MySQL HeatWave","mode":"rag","top_k":6}'
```

## Chunking strategy

- Uses a recursive character splitter inspired by LangChain’s RecursiveCharacterTextSplitter with separators (\n\n, \n, ". ", " ", "").
- Defaults: chunk_size=1000 and chunk_overlap=150 (tune through the environment for future ingests).
- The order of separators ensures we prefer paragraph and sentence boundaries before falling back to word and character splits.
- `sentence_pack` strategy packs paragraph → sentences → chunk windows, with recursive fallback for long sentences.
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
Wants=ollama.service
After=network.target ollama.service

[Service]
Type=simple
WorkingDirectory=/opt/search-app
EnvironmentFile=/opt/search-app/.env
ExecStart=/opt/search-app/run.sh
Restart=always
User=searchapp
Group=searchapp

[Install]
WantedBy=multi-user.target
```

## Troubleshooting

- 422 JSON decode error from curl commands:
  - Ensure there are no trailing characters (e.g., trailing dot) after the JSON body.

- LLM test ok=false or empty answer (Ollama):
  - Confirm `.env` has `LLM_PROVIDER=ollama` and `OLLAMA_BASE_URL=http://127.0.0.1:11434`.
  - Run `systemctl status ollama` and `journalctl -u ollama`.
  - Run `.venv/bin/python scripts/verify_ollama.py --smoke`; it checks runtime version, full model digest,
    Q4_K_M quantization, and inference.
  - Confirm `ss -ltn 'sport = :11434'` shows only `127.0.0.1:11434`.

- Database configuration missing at startup:
  - Ensure search-app/.env contains either DATABASE_URL or DB_HOST/DB_NAME/DB_USER/DB_PASSWORD.
  - The app auto-loads .env on startup via python-dotenv.

- Deep Research tables missing:
  - Run `psql "$DATABASE_URL" -f schema_v3.sql` or restart the app so `app/db.py` can create DR tables.

- Embedding dimension mismatch errors during ingestion (e.g., 384 vs 768):
  - EMBEDDING_DIM must match the chosen EMBEDDING_MODEL (MiniLM-L6-v2 -> 384).
  - If you created the schema with the wrong dimension, recreate/alter the column + index, or drop tables and restart to rebuild schema.

- Valkey/Redis references:
  - Valkey/Redis are no longer used. Remove any leftover env vars and use OCI PostgreSQL for persistence.

- Connectivity/SSL issues to PostgreSQL:
  - Default is DB_SSLMODE=require. Adjust as needed for your environment.

- PDF extraction quality:
  - Set USE_PYMUPDF=true to prefer PyMuPDF if installed (also enable the optional `pdf` dependency group).
- Image cards missing:
  - Confirm `/api/image-assets/{image_id}/thumbnail` returns 200.
  - Ensure you are logged in (session cookie required for image assets).
