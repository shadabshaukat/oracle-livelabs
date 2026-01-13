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

<img width="1449" height="668" alt="search-app-1" src="https://github.com/user-attachments/assets/6c6db981-e7c9-43dd-8729-a5c21bb2867e" />


## Documentation

- Terraform stack: see oci_postgres_tf_stack/README.md and oci_postgres_tf_stack/README-ORM.md
- Application: see search-app/README.md

## How they fit together

- The Terraform stack creates the network and database. The app connects to the OCI PostgreSQL endpoint using credentials you provide in search-app/.env.
- By default, the app listens on PORT=8000. If you deploy the optional Compute instance and set compute_assign_public_ip=true, and your security list allows 8000/tcp (the module includes port 8000 in the public security list), you can reach the UI at http://<public-ip>:8000/ui.
- If you do not assign a public IP, use a bastion/DRG/VPN or a load balancer/proxy.

<img width="2548" height="2141" alt="ai-search-livelab" src="https://github.com/user-attachments/assets/5e9eac98-8958-4e85-9c8a-203d18bafe6f" />




