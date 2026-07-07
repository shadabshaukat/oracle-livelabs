# Deployment Guide

This guide covers end‑to‑end deployment of the OCI PostgreSQL infrastructure and the FastAPI search application, including recommended configuration and runtime steps.

## 1) Infrastructure (OCI PostgreSQL + Networking)

The Terraform stack in `oci_postgres_tf_stack/` provisions:
- VCN + subnets + gateways
- OCI PostgreSQL DB System (with pgvector enabled via app or config)
- Optional Compute VM
- Optional Object Storage bucket for uploads; the application does not use it unless explicitly configured

### Option A: Terraform CLI

```bash
cd oci_postgres_tf_stack
terraform init

cat > terraform.tfvars <<'EOF'
compartment_ocid           = "ocid1.compartment.oc1..xxxx"
region                     = "ap-sydney-1"
psql_admin                 = "pgadmin"
object_storage_bucket_name = "search-app-uploads"
create_compute             = false
EOF

terraform plan -out plan.out
terraform apply plan.out
```

### Option B: Oracle Resource Manager (ORM)
- Create Stack → upload `oci_postgres_tf_stack` or Git ref.
- Provide `compartment_ocid`, `psql_admin`, optional bucket name.
- Run **Plan** then **Apply**.

## 2) Application Deployment (search-app)

### Prerequisites
- glibc Linux x86_64/ARM64 with systemd, or Apple Silicon macOS 14+
- 2-4 OCPUs/CPU cores, 8 GB RAM minimum, and at least 15 GB free during a clean build
- Linux only: `sudo`, `curl`, GNU tar, `sha256sum`, `ss`, and `flock` (util-linux)
- Reachable PostgreSQL with pgvector (`vector`, `pgcrypto`, and `citext` extensions)

### Setup

```bash
cd search-app
cp .env.example .env
```

Update `.env` with:
- `DB_HOST`/`DB_NAME`/`DB_USER`/`DB_PASSWORD` (or `DATABASE_URL`)
- `LLM_PROVIDER=ollama` (default); the bootstrap installs the pinned local model
- `STORAGE_BACKEND=local|oci|s3|both` (default `local`)
- `DATA_DIR=${HOME}/.oracle-livelabs/search-app` (default writable root for uploads, model cache, logs, locks, and portable runtime files)
- `OCI_OS_BUCKET_NAME` if using OCI storage
- Upload limits: `MAX_UPLOAD_SIZE_MB` (per-file), `MAX_FILES_PER_SPACE`, `ALLOWED_UPLOAD_EXTENSIONS`
- Deep Research + memory: `DEEP_RESEARCH_PERSISTENT_MEMORY_ENABLED`, `TEXT_PERSISTENT_MEMORY_ENABLED`, `SQL_PERSISTENT_MEMORY_ENABLED`, `IMAGE_PERSISTENT_MEMORY_ENABLED`
- LLM cache: `LLM_CACHE_TTL_SECONDS` (in-process cache)

### Reproducible First Run

```bash
./run.sh
```

The app runs at **http://0.0.0.0:8000**.

`run.sh` detects the operating system and calls `bootstrap_linux.sh` or `bootstrap_macos.sh` when a component is
missing. Linux retains the exact pinned Ollama/systemd installation. macOS reuses an existing running or installed
Ollama without upgrading or replacing it; only a genuinely new Mac receives the checksum-pinned self-contained
Ollama 0.31.1 fallback under the user's application home. No Homebrew or sudo installation is required. Both paths
install managed Python 3.14.6, verify the immutable manifest digest for `ibm/granite4:1b-q4_K_M`, and perform
`uv sync --locked` against the committed lockfile. It supports Linux x86_64 and ARM64 and fails closed on any
Linux binary/version or cross-platform model/digest mismatch. The macOS path currently supports Apple Silicon on macOS 14+;
Intel macOS fails fast because the pinned PyTorch release has no Intel wheel.

On a ready host, `run.sh` does not reinstall Ollama, restart its service, query the model registry, pull the model,
or send a preload. If the model is missing, macOS downloads it through the existing Ollama `/api/pull`; if present
but unloaded, it is preloaded with an indefinite keep-alive. An incompatible existing macOS Ollama is left
untouched and produces upgrade guidance. Use `./start.sh` for background operation after initial setup.

Both platform runners create the home storage tree when it is missing and leave it untouched when it already
exists. OCI credentials alone do not enable OCI Object Storage or OCI Generative AI. Those services are used only
after explicitly selecting `STORAGE_BACKEND=oci|both` or `LLM_PROVIDER=oci` and supplying their required settings.

For a clean repeat of the exact build:

```bash
./stop.sh
case "$(uname -s)" in
  Linux)  CLEAN_BUILD=1 FORCE_OLLAMA_REINSTALL=1 ./bootstrap_linux.sh ;;
  Darwin) CLEAN_BUILD=1 FORCE_OLLAMA_REINSTALL=1 ./bootstrap_macos.sh ;;
esac
./start.sh
```

The canonical pins are in `search-app/deploy/versions.env`; Python packages and artifact hashes are in
`search-app/uv.lock`. Linux image dependencies use CPU-only PyTorch wheels and contain no CUDA runtime packages.

Ollama is bound to loopback, so port 11434 requires no firewall rule. Port 8000 exposure is deliberately not
automated: restrict it using the host/cloud firewall or reverse proxy appropriate to the deployment. OCR requires
the native Tesseract executable and is unavailable when that optional host tool is absent.

## 3) Image Model Requirements (VM Host)

If you want image captioning + embeddings:

```bash
uv sync --locked --python 3.14.6 --extra image
```

These install:
- OpenCLIP (image embeddings)
- Transformers (captioning: BLIP/LLaVA)

## 4) Validation Checklist

1) **Health**: `GET /api/health`
2) **Ready**: `GET /api/ready` (tables/indexes/extensions)
3) **Upload** a PDF or TXT and confirm chunking + search.
4) **Upload** an image and confirm:
   - Library shows thumbnail
   - Image search returns cards
5) **Deep Research**: open the DR modal and ask a question; confirm sessions + history logging.

## 5) Recommended Production Patterns

- Run on OCI Compute with private access to the DB.
- Keep the default local home storage for a single node. Explicitly enable OCI Object Storage for multi-node or durable object-storage deployments.
- Use systemd for the API service.
- Restrict CORS + rotate credentials.

## Systemd Example

```ini
[Unit]
Description=Enterprise Search App
Wants=ollama.service
After=network.target ollama.service

[Service]
WorkingDirectory=/opt/search-app
EnvironmentFile=/opt/search-app/.env
ExecStart=/opt/search-app/run.sh
Restart=always

[Install]
WantedBy=multi-user.target
```

## Cost Sizing (OCI Estimate)

> **Note:** These are directional estimates only. Actual costs depend on region, discounts, and usage patterns.

Assumptions:
- **Compute VM**: E5.Flex, 2 OCPUs, 16 GB RAM, 250 GB boot volume.
- **OCI PostgreSQL**: 2 OCPUs, 16 GB RAM, **300k IOPS** profile.
- **Object Storage**: 100 GB data storage.
- **Local inference**: Ollama + the 1 GB quantized Granite model on the Compute VM; no per-token API charge.

Estimated monthly cost components:
- **Compute VM** (2 OCPU/16 GB + 250 GB boot volume): *~$120–$250/month*
- **OCI PostgreSQL** (2 OCPU/16 GB, 300k IOPS): *~$450–$900/month*
- **Object Storage** (100 GB): *~$3–$6/month*
- **Local inference**: included in the Compute VM estimate (allow roughly 2 GB additional disk/RAM headroom).

**Estimated total:** *~$620–$1,356/month* (approximate)

To refine this:
- Use the **OCI Pricing Calculator** with your exact region.
- Include outbound bandwidth and backup retention if applicable.

## Ops Runbook (Day‑2 Operations)

### Daily/Weekly
- Monitor app logs: `$HOME/.oracle-livelabs/search-app/logs/searchapp.log`.
- Review DB health (`/api/ready`, PostgreSQL alerts).
- Check disk usage for `$HOME/.oracle-livelabs/search-app/uploads/`.

### Monthly
- Rotate credentials (DB, Basic Auth, OCI tokens).
- Re-evaluate IVFFlat lists/probes as corpus grows.
- Archive or purge stale uploads if needed.

### Incident Response
- **Search errors**: check DB connectivity + pgvector extension.
- **Image search errors**: verify OpenCLIP deps and `/api/image-assets/{id}/thumbnail`.
- **RAG errors**: run `systemctl status ollama`, `python scripts/verify_ollama.py --smoke`, then `/api/llm-test`.
- **Deep Research tables missing**: run `psql "$DATABASE_URL" -f schema_v3.sql` or restart to let `app/db.py` create DR tables.

## Security Hardening Checklist

- Restrict CORS (`ALLOW_CORS=false` or set explicit origins).
- Change all default credentials (Basic Auth + app session secrets).
- Store secrets in OCI Vault or environment variables (never commit .env).
- Restrict DB access to private subnet/NSG rules.
- Enable HTTPS in front of FastAPI (NGINX/OCI LB).
- Limit upload size (`MAX_UPLOAD_SIZE_MB` per file), validate file types, and cap per-space uploads (`MAX_FILES_PER_SPACE`).
- Remove any Valkey/Redis configs; the platform is Postgres-only for persistence.
- Enable OS-level firewall; expose port 8000 only to trusted networks.
- Never add ingress for Ollama port 11434. It is bound to loopback because the local API has no authentication.
