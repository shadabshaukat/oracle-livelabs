High-level overview

- The repository provides two complementary stacks:

  - Infrastructure: oci_postgres_tf_stack, a Terraform/Oracle Resource Manager (ORM) deployable module that provisions OCI networking, an OCI PostgreSQL DB System, optionally a Compute VM, and an Object Storage bucket for uploads.
  - Application: search-app, a FastAPI service with a minimalist Jinja UI for document upload, ingestion (text extraction + chunking + embedding), and multi-mode retrieval (semantic with pgvector, full-text with PostgreSQL GIN, hybrid via RRF, and optional RAG using OCI GenAI or OpenAI). Security is via Basic Auth.

Repo structure and roles

- README.md (root): Explains how the Terraform and app pieces fit together, deployment modes (Terraform CLI vs ORM), and application configuration via .env.

- oci_postgres_tf_stack/: Terraform stack

  - Provisions VCN/subnets, gateways, NSGs; an OCI PostgreSQL DB System; and optionally a Compute instance.
  - vars.tf: Configurable inputs (region, compartment, networking toggles, PostgreSQL config, compute toggles, uploads bucket name).
  - psql.tf: OCI PostgreSQL resource including an attached configuration (supports enabling vector extension via config).
  - compute.tf: Optional Compute VM (flex shape) with public IP configurable.
  - Other tf files (availability_domains.tf, network.tf, provider.tf, etc.) wire up the full networking.

- search-app/: Application stack

  - pyproject.toml: Python 3.10+ with FastAPI, uvicorn, psycopg3 + pool, sentence-transformers, OCI SDK, Jinja2, and optional extras (pymupdf, pdfplumber). Entrypoint script: searchapp -> app.main:main.

  - run.sh: Convenience runner (loads .env, uv sync --extra pdf, then uv run searchapp).

  - .env.example: Full set of knobs (DB, security, embeddings, pgvector, FTS, storage backend local/oci/both with bucket name, LLM provider and OCI credentials, chunking sizes). Note: contains placeholder DB credentials – change before real use.

  - app/main.py: FastAPI app with:

    - Startup: ensure directories, initialize DB schema/indexes, and preload the embeddings model.

    - Security: BasicAuthMiddleware protects '/', '/api', docs.

    - Routes:

      - GET /: Serves Jinja template UI.
      - GET /api/health: health OK.
      - GET /api/ready: checks extensions (vector, pgcrypto), tables, and indexes exist.
      - GET /api/chunks-preview?doc_id=: quick chunk view.
      - GET /api/doc-summary?doc_id=: doc/file summary and chunk count.
      - POST /api/upload (multipart files[]): saves file(s) and ingests (extract, chunk, embed, store). Returns per-file doc_id and chunk count. If Storage backend includes OCI, uploads a copy to Object Storage and stores object_url in document metadata.
      - POST /api/search: accepts {query, mode: semantic|fulltext|hybrid|rag, top_k}. For rag, returns answer + hits + references (file name/type, chunk anchor, and object URL when present).
      - GET/POST /api/llm-debug and POST /api/llm-test: connectivity and response-shape diagnostics for OCI GenAI or OpenAI.
      - GET /api/llm-config: masked snapshot of LLM-related configuration.

  - app/config.py: Settings source from env/.env with sane defaults. Key fields:

    - DB via DATABASE_URL or DB_HOST/DB_NAME/DB_USER/DB_PASSWORD, plus pool sizes and sslmode.
    - Embeddings model/dim/batch, pgvector metric/lists/probes, FTS config.
    - Storage: local/oci/both, paths (storage/uploads), max upload size, delete_uploaded_after_ingest, OCI bucket and credentials.
    - LLM: provider openai|oci|none, plus OpenAI key/model or OCI GenAI endpoint/region/compartment/model id and auth (config file or API key envs).

  - app/db.py: psycopg3 pool and idempotent DB init:

    - Extensions: CREATE EXTENSION IF NOT EXISTS vector, pgcrypto.

    - Tables:

      - documents(id, source_path, source_type, title, metadata JSONB, created_at).
      - chunks(id, document_id, chunk_index, content, content_tsv GENERATED ALWAYS AS to_tsvector(config, content), content_chars, embedding vector(dim), embedding_model, created_at).

    - Indexes: unique(doc_id, chunk_index), GIN(content_tsv), IVFFlat(embedding opclass depends on cosine/l2/ip) WITH (lists = PGVECTOR_LISTS). Runtime ivfflat.probes is set per query.

  - app/text_utils.py: Extraction and chunking

    - Robust text extraction for PDF (prefers PyMuPDF when enabled, otherwise pypdf; pdfplumber fallback for sparse outputs), HTML/XML via BeautifulSoup, TXT, DOCX, CSV, JSON, MD.
    - Normalization keeping paragraph boundaries, hyphenation fixes, heading boundary detection, header/footer removal.
    - Recursive chunking with fallback separators and configurable chunk_size/overlap (defaults 2500/250).

  - app/embeddings.py: Sentence-Transformers model loading with cached directory; supports offline fallback; normalized embeddings; batch encoding.

  - app/search.py: Retrieval

    - Semantic: vector ANN search via pgvector distance operators (<=> cosine, <-> l2, <#> ip) with ivfflat.probes set.
    - Full-text: ts_rank_cd over generated content_tsv with plainto_tsquery.
    - Hybrid: simple Reciprocal Rank Fusion across semantic and FTS lists.
    - RAG: builds context from top_k hits and calls either OpenAI or OCI GenAI; returns answer, hits, flag for used_llm.

  - app/store.py: File storage and ingestion

    - ensure_dirs() creates data/uploads/model cache paths.
    - save_upload(): writes local file under storage/uploads/YYYY/MM/DD/HHMMSS/ (or temp when oci-only), optionally uploads to OCI Object Storage (using either config-file or API key env auth), returns (local_path, object_url|None).
    - save_upload_stream(): supports streaming to OCI (UploadManager.upload_stream) and local copy for ingestion; not currently wired into the /api/upload endpoint.
    - insert_document(), insert_chunks() using executemany with vector literal casting (::vector).
    - ingest_file_path(): read/extract text, chunk, embed, and persist in a transaction; returns document_id and number of chunks.

  - Jinja UI: app/templates/index.html and static/style.css

    - Search experience with a clean hero-like landing, search bar, settings (mode/top_k/auto-search debounce), result list with badges for distance/rank, and an Answer panel that shows LLM vs context-only mode.
    - References panel (for RAG) lists top sources with optional link to Object Storage URL if present in metadata.
    - Upload experience supports drag & drop folders/files, directory selection, per-file progress bars with concurrency (4), retries/backoff, and shows per-file processed summary when server responds.

  - app/auth.py: BasicAuthMiddleware protecting root and API/docs. Defaults in .env.example are admin/letmein; change these.

  - app/ui.py: A Gradio-based alternative UI builder (upload, search, status tabs). Not wired into app/main or declared as a dependency in pyproject; treat as optional/demo code.

- third_party/auslegalsearch/: A separate, more sophisticated legal search/reference stack (not imported by search-app)
  - Contains ingestion pipeline (beta) with GPU workers, streamlit/gradio UIs, RAG pipelines for OCI/Ollama, and extensive DB tools. Included here likely as reference materials or for future integration; no direct code linkage found from search-app.

- dataset/: Sample PDFs (privacy law) suitable for quick ingestion tests.

Data model and flow

- Upload: Client uploads file(s) → /api/upload → server saves local copy (and optionally streams/puts to OCI Object Storage) → reads and extracts text → chunks text → embeds chunks → inserts document + chunks with embedding vectors → returns document_id, chunk count, and object URL if any.

- Search:

  - semantic: query embed → vector ANN search via pgvector, ordering by distance.
  - fulltext: tsquery against content_tsv with GIN, ranked.
  - hybrid: RRF fusion of the above.
  - rag: perform hybrid/selected search → assemble context → call LLM (OCI or OpenAI) → return synthesized answer with references to top chunks/files. References include file_name/type and optional Object Storage link.

- Readiness/health: endpoints check DB availability, existence of extensions, tables, and indexes.

Configuration and deployment

- Application:

  - Create and edit search-app/.env (or provide envs). Critical settings:

    - DATABASE_URL or DB_HOST/DB_NAME/DB_USER/DB_PASSWORD (+ DB_SSLMODE, pool sizes).
    - BASIC_AUTH_USER/BASIC_AUTH_PASSWORD – change defaults.
    - EMBEDDING_MODEL and EMBEDDING_DIM must match (MiniLM-L6-v2 -> 384).
    - PGVECTOR_*: metric, lists (for dataset size), probes.
    - STORAGE_BACKEND: local|oci|both; OCI_OS_BUCKET_NAME required for oci/both and OCI credentials available.
    - LLM_PROVIDER: oci or openai + credentials (OCI: region + endpoint + compartment + model id).

  - Run: ./run.sh or uv sync --extra pdf && uv run searchapp (available at [](http://0.0.0.0:8000)<http://0.0.0.0:8000>).

- Infrastructure:

  - Terraform/ORM stack provisions VCN, OCI PostgreSQL, optional Compute, and an Object Storage bucket.
  - Variables control network creation or reuse, compute creation, and DB config (including a configuration that enables extensions).
  - Outputs include compute_public_ip (if created), uploads_bucket_name, and psql_admin_pwd (sensitive).

Security considerations

- Authentication: Basic Auth middleware protects both UI and API. Ensure credentials differ from example defaults.
- Secrets: Never commit real .env. Use OCI Vault/secret manager or environment variables in production; avoid putting sensitive DB or OCI values in Git.
- CORS: allow_cors defaults to true; lock this down for production.
- Network: Terraform config isolates PostgreSQL in private subnets and restricts 5432 via NSG; compute can be public/private per config. For production, front FastAPI with TLS-terminating proxy/WAF.

Scaling and performance

- Vector scale: Designed to scale to ~10M vectors using IVFFlat; tune lists (~sqrt(n)) and probes. Reindex when changing lists.
- Ingestion: executemany reduces round trips; for very large loads, consider COPY. Batch embedding is configurable.
- Post-load optimizations: vacuum/analyze on chunks; ensure adequate CPU/RAM per PostgreSQL instance. GIN index supports FTS.

Notable gaps and suggestions

- Streaming uploads: The README mentions streaming to OCI to avoid large memory usage; the code has save_upload_stream(), but /api/upload currently reads the entire file into memory. Consider swapping in starlette streaming + save_upload_stream to support large files efficiently.
- Gradio UI: app/ui.py depends on gradio but gradio isn’t in pyproject. If you intend to keep it, add an optional dependency group and a switch to run it; otherwise, remove or document it as optional.
- Secrets in .env.example: Contains illustrative DB host/user/password; make it more obviously placeholder and reiterate security guidance.
- CORS: Default allow all; restrict in production.
- Deletion/management endpoints: Consider endpoints to delete documents by id/path or to reindex with new ivfflat lists.
- Metadata search/filters: Future enhancement to filter by file type/title/metadata in queries.
- Tests/CI: No tests present; consider adding DB integration tests with a temporary Postgres container and unit tests for text extraction/chunking.

How to validate quickly

- Health: GET /api/health → { "status": "ok" }
- Readiness: GET /api/ready → ensures extensions/tables/indexes exist.
- Upload a PDF/TXT and search; try RAG mode if OCI/OpenAI creds are configured.
- LLM test: POST /api/llm-test with {question, context}; check chat_ok/text_ok for OCI path diagnostics.

Relationship to third_party/auslegalsearch

- The third_party folder is a separate, feature-rich legal search stack (with Streamlit/Gradio UIs, ingestion orchestration, OCI/Ollama RAG). search-app does not import or depend on it; treat it as examples/reference or a sibling codebase for future integration.

Roadmap

- Wire /api/upload to stream to OCI and local disk for large-file safety.
- Add document deletion/listing endpoints and simple admin views.
- Harden CORS, basic auth config, and secrets handling.
- Add a quick CLI to ingest local folders for bulk tests.
- Prepare a Terraform variable example and app systemd unit for the optional Compute instance.
