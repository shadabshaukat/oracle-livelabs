# Deployment Guide

This guide covers end‑to‑end deployment of the OCI PostgreSQL infrastructure and the FastAPI search application, including recommended configuration and runtime steps.

## 1) Infrastructure (OCI PostgreSQL + Networking)

The Terraform stack in `oci_postgres_tf_stack/` provisions:
- VCN + subnets + gateways
- OCI PostgreSQL DB System (with pgvector enabled via app or config)
- Optional Compute VM
- Object Storage bucket for uploads

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
- Python 3.10+
- `uv` package manager
- Access to OCI PostgreSQL endpoint

### Setup

```bash
cd search-app
cp .env.example .env
```

Update `.env` with:
- `DB_HOST`/`DB_NAME`/`DB_USER`/`DB_PASSWORD` (or `DATABASE_URL`)
- `LLM_PROVIDER=oci` + OCI GenAI credentials
- `STORAGE_BACKEND=local|oci|both`
- `OCI_OS_BUCKET_NAME` if using OCI storage

### Install + Run

```bash
uv sync
uv run searchapp
```

The app runs at **http://0.0.0.0:8000**.

## 3) Image Model Requirements (VM Host)

If you want image captioning + embeddings:

```bash
uv sync --extra image
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

## 5) Recommended Production Patterns

- Run on OCI Compute with private access to the DB.
- Store uploads in OCI Object Storage.
- Use systemd for the API service.
- Restrict CORS + rotate credentials.

## Systemd Example

```ini
[Unit]
Description=Enterprise Search App
After=network.target

[Service]
WorkingDirectory=/opt/search-app
EnvironmentFile=/opt/search-app/.env
ExecStart=/usr/bin/env uv run searchapp
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
- **OCI GenAI**: 10,000 API calls/month (mix of input/output tokens).

Estimated monthly cost components:
- **Compute VM** (2 OCPU/16 GB + 250 GB boot volume): *~$120–$250/month*
- **OCI PostgreSQL** (2 OCPU/16 GB, 300k IOPS): *~$450–$900/month*
- **Object Storage** (100 GB): *~$3–$6/month*
- **OCI GenAI** (10k requests): *~$50–$200/month* (token-dependent)

**Estimated total:** *~$620–$1,356/month* (approximate)

To refine this:
- Use the **OCI Pricing Calculator** with your exact region.
- Include outbound bandwidth and backup retention if applicable.

## Ops Runbook (Day‑2 Operations)

### Daily/Weekly
- Monitor app logs: `storage/logs/searchapp.log`.
- Review DB health (`/api/ready`, PostgreSQL alerts).
- Check disk usage for `storage/uploads/`.

### Monthly
- Rotate credentials (DB, Basic Auth, OCI tokens).
- Re-evaluate IVFFlat lists/probes as corpus grows.
- Archive or purge stale uploads if needed.

### Incident Response
- **Search errors**: check DB connectivity + pgvector extension.
- **Image search errors**: verify OpenCLIP deps and `/api/image-assets/{id}/thumbnail`.
- **RAG errors**: verify OCI GenAI credentials and `/api/llm-test`.

## Security Hardening Checklist

- Restrict CORS (`ALLOW_CORS=false` or set explicit origins).
- Change all default credentials (Basic Auth + app session secrets).
- Store secrets in OCI Vault or environment variables (never commit .env).
- Restrict DB access to private subnet/NSG rules.
- Enable HTTPS in front of FastAPI (NGINX/OCI LB).
- Limit upload size (`MAX_UPLOAD_SIZE_MB`) and validate file types.
- Enable OS-level firewall; only expose port 8000 via trusted networks.