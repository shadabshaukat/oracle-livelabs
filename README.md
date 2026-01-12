# Oracle LiveLabs: OCI PostgreSQL + Enterprise Search App

This repository contains two related stacks:

1) oci_postgres_tf_stack/ (Terraform)
   - Provisions OCI networking (VCN, subnets, gateways, route tables, security lists/NSG)
   - Provisions an OCI PostgreSQL DB System with pgvector support (extension created by the app)
   - Optional Compute instance in a public subnet

2) search-app/ (Application)
   - Enterprise search application with Gradio UI + FastAPI API
   - Document ingestion (PDF/HTML/TXT/DOCX) with parsing, recursive character chunking, and embeddings
   - Storage in OCI PostgreSQL with pgvector + GIN FTS
   - Search modes: Semantic, Full-text, Hybrid (RRF), and RAG with OpenAI or OCI Generative AI
   - HTTP Basic Auth protecting both /api and /ui

## Documentation

- Terraform stack: see oci_postgres_tf_stack/README.md and oci_postgres_tf_stack/README-ORM.md
- Application: see search-app/README.md

## How they fit together

- The Terraform stack creates the network and database. The app connects to the OCI PostgreSQL endpoint using credentials you provide in search-app/.env.
- By default, the app listens on PORT=8000. If you deploy the optional Compute instance and set compute_assign_public_ip=true, and your security list allows 8000/tcp (the module includes port 8000 in the public security list), you can reach the UI at http://<public-ip>:8000/ui.
- If you do not assign a public IP, use a bastion/DRG/VPN or a load balancer/proxy.

## Network alignment with Terraform

- Public subnet security list (VCN1-PUBLIC-SL) in oci_postgres_tf_stack/network.tf allows inbound TCP 22, 443, 8443, 8000, and 9000 from the Internet. This permits HTTP access to the app when it runs on port 8000 on a compute instance with a public IP.
- PostgreSQL access is restricted to within the VCN via an NSG rule on 5432.

Note: If you plan a different external port (e.g., 443 with a reverse proxy), add rules accordingly in both the firewall and, if desired, update Terraform security lists.

## Quick start

1) Provision infra via Terraform or ORM using oci_postgres_tf_stack/ (see its README for details).
2) Configure and run the application in search-app/ (see its README for prerequisites, .env config, uv run, and firewall commands).

## Oracle Linux 8 firewall and packages (summary)

```bash
# OS packages
sudo dnf install -y curl git unzip firewalld

# uv installer and PATH
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

# Firewalld rules for the app port (default 8000)
sudo systemctl enable --now firewalld
sudo firewall-cmd --permanent --add-port=8000/tcp
sudo firewall-cmd --reload
```

## Port and URL

- The app binds to HOST:PORT from search-app/.env (default 0.0.0.0:8000)
- UI is served at /ui (e.g., http://<host>:8000/ui)
- API is available at /api/* (Basic Auth required)

## Security

- Basic Auth (BASIC_AUTH_USER/BASIC_AUTH_PASSWORD) protects both UI and API
- Use SSL to PostgreSQL (sslmode=require by default)

## Notes

- Ensure the Terraform module resource names follow Terraform identifier rules (avoid hyphens in resource labels). If needed, rename resources like VCN1-PUBLIC-SL to VCN1_PUBLIC_SL and update references.
- For better PDF extraction of scans/tables, consider adding OCR or specialized parsers later.
