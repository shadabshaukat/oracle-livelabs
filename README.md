# Oracle LiveLabs: OCI PostgreSQL + Enterprise Search App

This repository contains two related stacks that together deliver an enterprise document search and RAG (Retrieval-Augmented Generation) experience on Oracle Cloud Infrastructure (OCI):

1) `oci_postgres_tf_stack/` — Terraform/Resource Manager stack
   - Provisions VCN + networking (private subnet for PostgreSQL, public subnet for Compute, NAT/Service Gateways, route tables, security lists/NSGs)
   - Provisions an OCI PostgreSQL DB System (with pgvector support created by the app at runtime)
   - Optional Compute instance (for hosting the app) in a public subnet
   - Creates an Object Storage bucket for app uploads (configurable)

2) `search-app/` — Application stack
   - FastAPI backend + minimalist Jinja UI
   - Upload & ingestion of PDF/HTML/TXT/DOCX with robust parsing and chunking, vector embeddings, and full‑text indexing
   - Multi‑mode retrieval: Semantic (pgvector), Full‑Text (tsvector), Hybrid (weighted RRF), and RAG with a local Ollama model by default
   - Deep Research (DR) sessions with notebook, follow-ups, and optional web search
   - SQL Search (NL2SQL), search history auditing, and persistent memory across text/SQL/DR
   - Local home-directory storage by default, with OCI Object Storage or S3 available only when explicitly enabled


## Documentation Index
- Terraform stack: [docs/oci_postgres_tf_stack/README.md](docs/oci_postgres_tf_stack/README.md)
- Application: [docs/search-app/README.md](docs/search-app/README.md)
- Deployment: [DEPLOYMENT.md](DEPLOYMENT.md)
- Architecture: [ARCHITECTURE.md](ARCHITECTURE.md)


## Architecture Overview
- Terraform provisions the network and OCI PostgreSQL. Optional compute can be created.
- The app connects to OCI PostgreSQL and self‑manages schema and indexes on startup (CREATE IF NOT EXISTS).
- Files uploaded via UI/API are saved by default under `$HOME/.oracle-livelabs/search-app/uploads/<user>/YYYY/MM/DD/HHMMSS/filename`. Existing directories and files are preserved. OCI Object Storage or S3 is contacted only when `STORAGE_BACKEND` explicitly enables it.

### End-to-end Workflow (Text + Image)
1) **Upload** (UI or API) → files stored locally and optionally in OCI Object Storage.
2) **Ingest** → text extraction (PDF/DOCX/TXT/HTML/MD/CSV/JSON/XML), normalization, chunking, embeddings.
3) **Index** →
   - Text: chunk content stored in `chunks` with generated `content_tsv` for full‑text.
   - Vectors: embedding stored in `chunks.embedding` (pgvector) for semantic search.
   - Images: OpenCLIP embeddings stored per image; thumbnails saved for fast display.
4) **Search** →
   - Semantic: vector similarity via pgvector.
   - Full‑text: PostgreSQL `tsvector` + GIN index.
   - Hybrid: Reciprocal Rank Fusion of semantic + full‑text results.
   - RAG: uses Hybrid results as context; optional LLM synthesis.
   - Deep Research: persistent conversations, follow-ups, notebook pins, optional web search.
5) **Render** → UI shows LLM response (if used), search matches, references, and image cards.


## Deploying the Infrastructure
You can deploy the infrastructure in two ways: using Terraform CLI or Oracle Resource Manager (ORM).

### Option A: Terraform CLI
Prerequisites: Terraform >= 1.5, OCI credentials configured in your environment.

1) Navigate to the stack directory and initialize:
```bash
cd oci_postgres_tf_stack
terraform init
```

2) Create a `terraform.tfvars` with your values (example):
```hcl
compartment_ocid        = "ocid1.compartment.oc1..aaaa..."
region                  = "ap-sydney-1"
# PostgreSQL admin username (required)
psql_admin              = "pgadmin"
# Optional: predefine the uploads bucket name (else default 'search-app-uploads' is used)
object_storage_bucket_name = "search-app-uploads"
# Optional compute
create_compute          = false
```

3) Plan and apply:
```bash
terraform plan -out plan.out
terraform apply plan.out
```

4) Note the outputs:
- `compute_public_ip` (if compute was created)
- `uploads_bucket_name` (Object Storage bucket for app uploads)
- `psql_admin_pwd` (sensitive)

For production, set additional variables as needed (see [docs/oci_postgres_tf_stack/README.md](docs/oci_postgres_tf_stack/README.md)).

### Option B: Oracle Resource Manager (ORM)
1) Zip the Terraform stack directory or import it directly into ORM:
   - Console → Developer Services → Resource Manager → Stacks → Create Stack
   - Source: Upload zip or link to your Git repo snapshot containing `oci_postgres_tf_stack`
2) Configure variables:
   - Required: `compartment_ocid`, `psql_admin`
   - Optional: `object_storage_bucket_name` (default `search-app-uploads`), compute vars
3) Plan and Apply.
4) Use the Job outputs for bucket name and, if created, the compute instance information.


## Configuring and Running the Application
The app can run anywhere that can reach a PostgreSQL server with pgvector. OCI PostgreSQL is one option, not an
application-runtime requirement.

### Prerequisites
- Supported host: glibc Linux x86_64/ARM64 with systemd, or Apple Silicon macOS 14+
- 2-4 OCPUs/CPU cores, 8 GB RAM minimum, and at least 15 GB free during a clean build
- Linux host tools: `sudo`, `curl`, GNU tar, `sha256sum`, `ss`, and `flock` (util-linux)
- A reachable PostgreSQL server with `vector`, `pgcrypto`, and `citext`; local PostgreSQL commonly needs `DB_SSLMODE=disable`
- No preinstalled Python, uv, Ollama, Homebrew, or model is required. Linux installs the pinned Ollama runtime;
  macOS reuses an existing compatible Ollama and installs a checksum-pinned home-local fallback only when absent.

### Setup
1) Prepare environment
```bash
cd search-app
cp .env.example .env
# Edit .env to point to your OCI PostgreSQL (either DATABASE_URL or DB_HOST/DB_NAME/DB_USER/DB_PASSWORD)
# Leave STORAGE_BACKEND=local unless object storage is explicitly required
```

Key environment variables (see `search-app/.env.example` and `docs/search-app/README.md` for a full list):
- DB: `DATABASE_URL` or `DB_HOST/DB_NAME/DB_USER/DB_PASSWORD`, `DB_SSLMODE`
- Security: `BASIC_AUTH_USER`, `BASIC_AUTH_PASSWORD`
- Embeddings: `EMBEDDING_MODEL`, immutable `EMBEDDING_MODEL_REVISION`, `EMBEDDING_DIM`
- pgvector: `PGVECTOR_METRIC`, `PGVECTOR_LISTS`, `PGVECTOR_PROBES`
- Full‑Text: `FTS_CONFIG`
- Storage backends:
  - `STORAGE_BACKEND=local|oci|both` (default `local`)
  - `DATA_DIR` defaults to `$HOME/.oracle-livelabs/search-app`; uploads, model cache, logs, locks, and portable runtime files live below it
  - `OCI_OS_BUCKET_NAME` (required when using `oci` or `both`)
  - OCI credentials (config file or API key envs) for Object Storage
- RAG LLM provider:
  - Default: `LLM_PROVIDER=ollama`, `OLLAMA_BASE_URL=http://127.0.0.1:11434`
  - Pinned CPU model: `ibm/granite4:1b-q4_K_M` (Q4_K_M, about 1 GB)
  - OpenAI, OCI GenAI, and Bedrock remain optional compatibility providers
- Persistent memory + DR:
  - `TEXT_PERSISTENT_MEMORY_ENABLED`, `SQL_PERSISTENT_MEMORY_ENABLED`, `IMAGE_PERSISTENT_MEMORY_ENABLED`, `DEEP_RESEARCH_PERSISTENT_MEMORY_ENABLED`
  - `PERSISTENT_MEMORY_TOP_K`, `PERSISTENT_MEMORY_MAX_CHARS`, `PERSISTENT_MEMORY_SUMMARY_MAX_CHARS`
- LLM cache: `LLM_CACHE_TTL_SECONDS` (in-process cache)

2) Run the app; the first run bootstraps the pinned runtime, local model, and dependencies
```bash
./run.sh
```
This starts the app at http://0.0.0.0:8000. Authenticate with the Basic Auth credentials in `.env`.

`run.sh` detects Linux versus macOS and dispatches to the platform-specific bootstrap. Linux retains its exact
pinned systemd behavior. macOS first reuses a running or installed Ollama.app, Homebrew/PATH CLI, or explicit
`OLLAMA_CLI_PATH` without replacing it. If no macOS Ollama exists, the bootstrap installs the checksum-verified
official archive under the application home; Homebrew and sudo are not required. On either platform, the exact
model is pulled only when missing/invalid and is loaded only when not currently resident.
Use `./start.sh` after initial setup when you want the same runner in the background.

`search-app/uv.lock` pins the complete transitive package graph and distribution hashes. Commit changes to
`pyproject.toml` and `uv.lock` together; use `uv lock --upgrade` only for a deliberate, tested dependency upgrade.
`search-app/deploy/versions.env` separately pins uv, Python, the Linux/macOS-fallback Ollama installer, the
quantized model digest, binary checksums, and the embedding-model revision. An existing macOS Ollama may have a
different version, but it must pass the required API, exact-model, context, loopback, and smoke-inference checks.

Ollama is intentionally bound to `127.0.0.1:11434`. Do not open port 11434 in an OCI security list, NSG, or host
firewall: the API is unauthenticated and the FastAPI process reaches it over loopback. Only the application port
8000 should be allowed from trusted clients. `run.sh` does not weaken a host or cloud firewall automatically.

### Common Deployment Patterns
- **Linux VM**: run `./run.sh`; it performs first-use setup and starts immediately on subsequent runs.
- **Apple Silicon Mac**: run `./run.sh`; an existing Ollama keeps its normal model store (usually `~/.ollama`).
  Only the no-Ollama fallback runtime/model store is placed under `$HOME/.oracle-livelabs/search-app/runtime/macos/`.
- **Clean rebuild**: stop the app, then run the matching `bootstrap_linux.sh` or `bootstrap_macos.sh` with
  `CLEAN_BUILD=1 FORCE_OLLAMA_REINSTALL=1`. On macOS, the force flag refreshes only the managed fallback and never
  replaces an external Ollama installation.
- **Private DB access**: ensure the VM is in the same VCN/subnet or a peered network; allow 5432 only from trusted sources.

Intel macOS is detected and rejected with an actionable message because the pinned PyTorch 2.12.1 full-platform
build has no Intel macOS wheel. OCR is optional and requires a native Tesseract executable in addition to the
locked Python package.

### Upload Behavior
- Files are saved to `$HOME/.oracle-livelabs/search-app/uploads/<user>/YYYY/MM/DD/HHMMSS/<basename>` by default.
- `run.sh` creates missing home storage directories with private defaults and leaves existing contents and permissions unchanged.
- OCI/S3 credentials or legacy object metadata cannot activate object storage while `STORAGE_BACKEND=local`. To use OCI, explicitly set `STORAGE_BACKEND=oci` (or `both` plus `OBJECT_STORAGE_PROVIDER=oci`) and configure `OCI_OS_BUCKET_NAME` and credentials.
- Image uploads generate thumbnails for faster UI rendering in Library and Image Search.
- Upload limits are enforced per file and per space:
  - `MAX_UPLOAD_SIZE_MB` (per-file size cap)
  - `MAX_FILES_PER_SPACE` (maximum files in a space)
  - `ALLOWED_UPLOAD_EXTENSIONS` (comma-separated allowlist; blank allows all)

### Validating the System
- Health: `GET /api/health` → `{ "status": "ok" }`
- Readiness: `GET /api/ready` → checks pgvector, tsvector tables/indexes
- QA endpoints:
  - `GET /api/doc-summary?doc_id=<id>` → file name, type, chunk count
  - `GET /api/chunks-preview?doc_id=<id>&limit=20` → preview chunk snippets

### Search Modes
- Semantic (pgvector): cosine/L2/IP
- Full‑Text: PostgreSQL FTS (GIN) with `ts_rank_cd`
- Hybrid: Reciprocal Rank Fusion over semantic and full‑text results
- RAG: grounded local synthesis using Ollama by default, with numbered source citations and bounded context
- SQL Search (NL2SQL) for analysts/admins
- Deep Research for multi-step investigations

### Image Search Flow
- Images are embedded with OpenCLIP and stored in PostgreSQL.
- Image search accepts a text query, tags, or a reference image.
- Results include `thumbnail_url` for the UI (served by `/api/image-assets/{id}/thumbnail`) and metadata tags/captions.

### Deep Research Flow
- Start from the AI icon to open the DR modal and ask a question.
- Sessions and notebook pins persist per space in PostgreSQL.
- Follow-up questions open a modal for response entry (Enter-to-send).
- DR activity logs into Search History with `activity_type=deep_research`.


## Typical End‑to‑End Flow
1) Deploy infra with Terraform/ORM (optional compute)
2) Configure app `.env` (DB + storage + RAG)
3) Run app; upload PDFs/DOCX/TXT/HTML
4) Use Search UI (hybrid/semantic/full‑text/RAG)
5) Inspect References panel — click through to Object Storage if enabled


## Troubleshooting
- DB connectivity: verify `.env` values; `DB_SSLMODE` default is `require`
- PDF extraction quality: set `USE_PYMUPDF=true` and ensure pdf extras are installed (`uv sync --locked --extra pdf`)
- Local model: `systemctl status ollama`, `journalctl -u ollama`, and `python scripts/verify_ollama.py --smoke`
- Uploads to OCI: verify `STORAGE_BACKEND` and `OCI_OS_BUCKET_NAME`; ensure OCI credentials are available
- Authentication: Basic Auth protects `/` and `/api`
- Images not rendering: confirm `/api/image-assets/{image_id}/thumbnail` returns 200 and that you are logged in (session cookies).
- Deep Research tables missing: run `psql "$DATABASE_URL" -f schema_v3.sql` or restart the app to let `app/db.py` create DR tables.
- Valkey/Redis: cache removed; delete any leftover Valkey env vars and rely on OCI PostgreSQL.

## New Features in V2 ##

 1. New Landing page !
 2. User account sign-up with an email and password
 3. Added vectorizing images to OCI Postgres and doing image search
 4. Every user gets a space where they can upload files and images. 1 user can have multiple spaces
 5. File browser in-build for each user and it's associated space
 6. OCI Object storage used to store and retrieve images via SDK (no more par urls)
 7. Search and chunking enhancements by used Langchain RCTS 
 8. UI improvements to render it on both mobile and desktop
 9. Search accuracy improvement by enhancing the semantic search & RAG pipeline
10. Metrics added to measure and display LLM response and semantic search response

URL : https://search.shadabmohammad.com/

## Get Started ##

1. Use API username/password to login :   admin/*******
2, Register with an email and password
3. Login with email and Fire up!



## License
Apache-2.0
