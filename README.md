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
   - Multi‑mode retrieval: Semantic (pgvector), Full‑Text (tsvector), Hybrid (RRF fusion), and RAG (OpenAI or OCI GenAI)
   - Deep Research (DR) sessions with notebook, follow-ups, and optional web search
   - SQL Search (NL2SQL), search history auditing, and persistent memory across text/SQL/DR
   - Dual storage backends for uploads: local file system and/or OCI Object Storage, with timestamped folder structure


## Documentation Index
- Terraform stack: [docs/oci_postgres_tf_stack/README.md](docs/oci_postgres_tf_stack/README.md)
- Application: [docs/search-app/README.md](docs/search-app/README.md)
- Deployment: [DEPLOYMENT.md](DEPLOYMENT.md)
- Architecture: [ARCHITECTURE.md](ARCHITECTURE.md)


## Architecture Overview
- Terraform provisions the network and OCI PostgreSQL. Optional compute can be created.
- The app connects to OCI PostgreSQL and self‑manages schema and indexes on startup (CREATE IF NOT EXISTS).
- Files uploaded via UI/API are saved locally under `storage/uploads/YYYY/MM/DD/HHMMSS/filename`. When configured, they are also uploaded to OCI Object Storage with the same object path; the **object identifiers** (provider/bucket/object name) are stored in document metadata and downloads/thumbnails are streamed by the app via the OCI SDK (no PAR URLs).

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
The app can run anywhere that can reach the OCI PostgreSQL endpoint. You can run it on your workstation, on the optional Compute VM, or in a container VM.

### Prerequisites
- Python 3.10+
- `uv` package manager (https://docs.astral.sh/uv/)

### Setup
1) Prepare environment
```bash
cd search-app
cp .env.example .env
# Edit .env to point to your OCI PostgreSQL (either DATABASE_URL or DB_HOST/DB_NAME/DB_USER/DB_PASSWORD)
# Optionally set STORAGE_BACKEND and OCI_OS_BUCKET_NAME (see below)
```

Key environment variables (see `search-app/.env.example` and `docs/search-app/README.md` for a full list):
- DB: `DATABASE_URL` or `DB_HOST/DB_NAME/DB_USER/DB_PASSWORD`, `DB_SSLMODE`
- Security: `BASIC_AUTH_USER`, `BASIC_AUTH_PASSWORD`
- Embeddings: `EMBEDDING_MODEL`, `EMBEDDING_DIM`
- pgvector: `PGVECTOR_METRIC`, `PGVECTOR_LISTS`, `PGVECTOR_PROBES`
- Full‑Text: `FTS_CONFIG`
- Storage backends:
  - `STORAGE_BACKEND=local|oci|both` (default `local`)
  - `OCI_OS_BUCKET_NAME` (required when using `oci` or `both`)
  - OCI credentials (config file or API key envs) for Object Storage
- RAG LLM provider:
  - `LLM_PROVIDER=oci` or `openai`, with corresponding credentials
- Persistent memory + DR:
  - `TEXT_PERSISTENT_MEMORY_ENABLED`, `SQL_PERSISTENT_MEMORY_ENABLED`, `IMAGE_PERSISTENT_MEMORY_ENABLED`, `DEEP_RESEARCH_PERSISTENT_MEMORY_ENABLED`
  - `PERSISTENT_MEMORY_TOP_K`, `PERSISTENT_MEMORY_MAX_CHARS`, `PERSISTENT_MEMORY_SUMMARY_MAX_CHARS`
- LLM cache: `LLM_CACHE_TTL_SECONDS` (in-process cache)

2) Install dependencies and run the app
```bash
./run.sh
# or
uv sync --extra pdf
uv run searchapp
```
This starts the app at http://0.0.0.0:8000. Authenticate with the Basic Auth credentials in `.env`.

### Common Deployment Patterns
- **Local dev**: run `uv sync` and `uv run searchapp`.
- **VM (OCI Compute)**: copy repo + `.env`, run `./run.sh`, optionally set up systemd (see docs/search-app/README.md).
- **Private DB access**: ensure the VM is in the same VCN/subnet or a peered network; allow 5432 only from trusted sources.

### Upload Behavior
- Files are saved to `storage/uploads/YYYY/MM/DD/HHMMSS/<basename>`.
- If `STORAGE_BACKEND` includes `oci` and `OCI_OS_BUCKET_NAME` is set, the same date/time path is uploaded to Object Storage and the object identifiers are stored in the DB. The app streams downloads/thumbnails via SDK-backed endpoints (no PAR links).
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
- RAG: Optional LLM synthesis using OpenAI or OCI GenAI
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
- PDF extraction quality: set `USE_PYMUPDF=true` and ensure pdf extras are installed (`uv sync --extra pdf`)
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
