# Architecture: OCI PostgreSQL + Enterprise Search App

This document provides a full, in-depth view of the system architecture, focusing on how data flows through ingestion, storage, and retrieval for both text and image content.

## High-Level Components

1) **OCI PostgreSQL (Primary AI Store)**
   - Persistent system of record for documents, chunks, embeddings, and image assets.
   - Uses **pgvector** for semantic similarity (text + image).
   - Uses **GIN** full‑text indexes for keyword search.

2) **FastAPI Service (search-app)**
   - Handles upload, ingestion, chunking, embedding, search, and UI rendering.
   - Manages schema creation and index setup on startup.
   - Exposes API endpoints for text search, image search, upload, library, and auth.

3) **OCI Generative AI (Inference + Reasoning)**
   - RAG synthesis uses OCI GenAI to answer from retrieved context.
   - The LLM is not a primary store—only an inference engine on top of PostgreSQL results.

4) **VM-hosted Image Models (Captioning + Embeddings)**
   - OpenCLIP generates image embeddings for semantic search.
   - Optional captioning (BLIP/LLaVA) generates tags/captions stored in PostgreSQL.
   - Runs on the app host (CPU by default, CUDA/MPS optional).

5) **Storage Layer**
   - Local filesystem: `storage/uploads/...`
   - Optional OCI Object Storage: objects + thumbnails, **object identifiers stored in metadata**; downloads/thumbnails streamed via OCI SDK (no PAR URLs).

## Text Ingestion Lifecycle (End-to-End)

1) **Upload**
   - UI or `/api/upload` receives files.
   - File saved locally (`storage/uploads/<user>/<date>/<time>/file.ext`).
   - If `STORAGE_BACKEND=oci|both`, file is mirrored to Object Storage and object identifiers (provider/bucket/object name) are saved in metadata; downloads/thumbnails are served by SDK-backed endpoints.

2) **Text Extraction** (`text_utils.py`)
   - PDF: PyMuPDF → pypdf → pdfplumber fallback.
   - DOCX/DOC: python-docx and system tools (textutil/antiword/strings fallback).
   - TXT/MD/HTML/XML/JSON/CSV/PPTX/XLSX: dedicated extractors.
   - Normalization preserves paragraph boundaries and removes common headers/footers.

3) **Chunking**
   - `CHUNK_STRATEGY=recursive|sentence_pack`.
   - `sentence_pack`: paragraph → sentence → pack; fallback to recursive split on long sentences.
   - `SENTENCE_SPLITTER=regex|nltk|spacy` (default NLTK).

4) **Embedding + Indexing**
   - SentenceTransformers produces normalized text embeddings.
   - Each chunk stored in `chunks` table with:
     - `content`
     - `content_tsv` (generated for FTS)
     - `embedding` (pgvector)
   - Indexes: GIN(content_tsv) and IVFFlat(embedding).

5) **Persist**
   - `documents` table stores metadata + original file path + object identifiers (provider/bucket/object name).
   - `chunks` table stores chunk content and embeddings.

## Text Search Lifecycle

1) **Semantic Search**
   - Query embedded → pgvector ANN search (IVFFlat) → top_k chunks.

2) **Full‑Text Search**
   - `plainto_tsquery` against `content_tsv` (GIN) → ranked results.

3) **Hybrid Search**
   - Reciprocal Rank Fusion merges semantic + full‑text rankings.

4) **RAG**
   - Hybrid results assembled into context.
   - OCI GenAI called to synthesize an answer.
   - References include file name/type + object storage link if present.

## Image Ingestion Lifecycle (End-to-End)

1) **Upload**
   - Image upload via `/api/upload` (PNG/JPG/GIF/WebP/BMP).
   - Saved locally; optionally mirrored to Object Storage.

2) **Image Processing** (`store.py`)
   - Generate thumbnail (512px max) stored under `storage/uploads/thumbnails/` (and mirrored to Object Storage when enabled).
   - Optional captioning model generates description + keywords.

3) **Image Embedding** (`vision_embeddings.py`)
   - OpenCLIP embeds image; embeddings normalized and stored in DB.

4) **Persist**
   - `image_assets` table stores:
     - `file_path`, `thumbnail_path`
     - tags, caption
     - embedding vector
   - Document metadata updated with `thumbnail_object_name`, caption, dimensions, tags.

## Image Search Lifecycle

1) **Query Input**
   - `/api/image-search` accepts text, tags, or a reference image.

2) **Embedding**
   - Text: OpenCLIP text encoder.
   - Image: OpenCLIP image encoder.

3) **Vector Search**
   - pgvector similarity against stored image embeddings.
   - Optional text/tag weighting if both query + vector present.

4) **Render**
   - API returns `thumbnail_url` (`/api/image-assets/{id}/thumbnail`).
   - UI displays cards with thumbnail, caption, tags, score.

## Model Caching (Text + Image)

- Text embeddings model and image models are cached locally under `MODEL_CACHE_DIR` (default `storage/models`).
- The app sets `HF_HOME`, `TRANSFORMERS_CACHE`, and `SENTENCE_TRANSFORMERS_HOME` to this directory and uses `cache_dir` for OpenCLIP to speed repeated loads.
- Models are loaded once per process via `lru_cache` to avoid reinitialization overhead.

## Security & Auth

- Session-based auth for UI (login/register).
- Basic Auth fallback for APIs.
- Image asset endpoints require session cookie (user-scoped).

## Key Tables (PostgreSQL)

- `documents`: metadata + file paths.
- `chunks`: text chunks + embeddings + full-text index.
- `image_assets`: images + thumbnails + embeddings + captions/tags.

## Summary

- **OCI PostgreSQL is the primary persistent AI store.**
- **OCI GenAI is used only for inference/reasoning in RAG.**
- **Image models (OpenCLIP + captioning) run on the VM/app host.**
- **Both text and image lifecycles are fully indexable and queryable through PostgreSQL.**