# Search App Session Specs (Context-Recovery Document)

Last updated: 2026-02-26 15:33 (AEDT)
Scope scanned: `/Users/shadab/Downloads/oracle-livelabs/search-app`

---

## 1) Purpose of this file

This document is a **single-session recovery spec** so if context window is lost, you can restart work quickly without re-scanning the full repo.

It captures:
- architecture and runtime flow
- backend/API contracts and key modules
- UI/UX structure and behavior
- configuration + startup/ops commands
- current known implementation details and gaps
- recommended change workflow for next coding session

---

## 2) High-level system summary

`search-app` is a FastAPI + Jinja app that supports:

1. **Text search** (semantic/fulltext/hybrid/RAG)
2. **Image search** (OpenCLIP vectors + caption/OCR text blending)
3. **SQL search** (NL2SQL with role/context guardrails)
4. **Deep Research** (persistent conversations, notebook, optional web evidence)
5. **Persistent memory** across text/image/sql/deep-research (thumbs-up retrieval)
6. **Session/search history auditing** (session + activity payload logs)

Primary persistence is **PostgreSQL + pgvector** (no Redis/Valkey dependency).

---

## 3) Runtime entrypoints + ops

### Main run flow
- Entry script: `searchapp = app.main:main` (in `pyproject.toml`)
- App startup in `app/main.py`:
  - `ensure_dirs()`
  - `init_db_with_retry()`
  - `_migrate_object_metadata()`
  - preload embeddings model

### Scripts
- `search-app/run.sh`
  - loads `.env`
  - `uv sync --extra pdf --extra image`
  - `uv run searchapp`
- `search-app/start.sh`
  - starts `run.sh` in background
  - writes PID to `storage/searchapp.pid`
  - logs to `storage/logs/searchapp.log`
- `search-app/stop.sh`
  - kills PID from pidfile

### Fresh DB bootstrap
- `psql "$DATABASE_URL" -f schema_v3.sql`

---

## 4) Codebase map (important files)

### Core backend
- `app/main.py` – API endpoints + UI route + SQL/NL2SQL orchestrator + history logging
- `app/config.py` – env-driven settings dataclass
- `app/db.py` – connection pool, schema init, indexes, retry init, partitioned tables
- `app/search.py` – semantic/fulltext/hybrid/rag + image_search
- `app/store.py` – ingestion (file save, extract/chunk/embed/persist, image pipeline)
- `app/text_utils.py` – multi-format text extraction + chunking strategies
- `app/embeddings.py` – text embedding model loading and encode
- `app/vision_embeddings.py` – image embeddings, captioning, OCR
- `app/object_storage.py` – OCI/S3 abstraction layer
- `app/auth.py` + `app/session.py` + `app/users.py` – auth/session/roles/spaces

### Deep Research + memory
- `app/deep_research.py` – DR orchestration loop, confidence, followups, refs, rollups
- `app/deep_research_store.py` – DR persistence helpers
- `app/agentic_research.py` – web/local context decisioning
- `app/external_sources.py` – user URL ingestion/retrieval
- `app/memory_store.py` – persistent memory retrieval/persist/summarize

### Frontend
- `app/templates/index.html` – full app UI + client JS (large single file)
- `app/static/style.css` – all styling (light/dark, responsive, DR modal, SQL tables)

### Schema
- `schema_v3.sql` – full consolidated schema (roles, OCR, SQL search, DR, memory, partitions)

---

## 5) Authentication and access model

### Middleware
- `SessionOrBasicAuthMiddleware` protects `/api`, `/docs`, `/openapi.json`, `/redoc`
- Public exceptions include:
  - `/api/health`, `/api/ready`, `/api/login`, `/api/register`, `/api/llm-config`, `/api/llm-test`, `/api/llm-debug`

### Session
- signed cookie with HMAC SHA256
- includes `user_id`, `email`, `role`, `sid`, `iat`, `sv`
- session invalidates on restart (`SERVER_START_TS` check)

### Roles
- `user` (default)
- `analyst` (SQL search access)
- `admin` (SQL + system catalog mode)

### Spaces
- per-user spaces
- default space used when `space_id` not provided

---

## 6) Database model highlights

Key tables:
- `users`, `roles`, `spaces`
- `documents`, `chunks`
- `image_assets`
- `search_sessions` (hash-partitioned), `search_activity` (range-partitioned)
- `memory_events` (range-partitioned)
- `deep_research_conversations`, `deep_research_steps`, `deep_research_notebook_entries`
- `conversation_external_docs`

Key indexes:
- `chunks`: GIN `content_tsv`, IVFFlat on `embedding`
- `image_assets`: IVFFlat on `embedding`
- `memory_events`: IVFFlat on `embedding`
- session/activity indexes for user+time and space+time filtering

---

## 7) Search and retrieval behavior

### Text search (`/api/search`)
- Modes: `semantic`, `fulltext`, `hybrid`, `rag`
- User + space scoping enforced
- `rag` mode can inject persistent memory context (if enabled/requested)
- returns `hits`, optional `answer`, `references`, `timings`, optional `memory_event_id`

### Image search (`/api/image-search`)
- Accepts text, tags, or reference image upload
- Uses text->image embedding or image->image embedding when available
- Ranking blends text rank and vector score
- includes OCR text in search expression
- returns `thumbnail_url`, caption/tags/score, optional `memory_event_id`

### SQL search (`/api/sql-search`)
- Role-gated (`analyst`/`admin` only)
- Context modes:
  - `user` (public tables)
  - `system` (admin only, pg_* schemas, blocks information_schema)
- Safety:
  - SELECT-only checks
  - table allowlist validation
  - row limits with enforced cap
- Two-pass style generation:
  1. candidate/selected tables
  2. SQL generation with schema + DDL + grounding
- Includes optional agentic retry with execution feedback and sampled rows

### Deep Research (`/api/deep-research/*`)
- conversation start + ask + list/detail + notebook + title edit
- local retrieval, optional URL ingestion, optional web evidence
- confidence and follow-up question generation
- stores step references + metadata
- optional persistent memory + periodic memory rollups

---

## 8) Ingestion behavior

### Upload endpoint
- `/api/upload` currently reads each upload into memory (`await f.read()`)
- validates extension and per-file size
- enforces max files per space

### Text docs
1. save file (local and/or object storage)
2. extract text (`text_utils.py`)
3. chunk (`recursive` or `sentence_pack`)
4. embed text
5. insert `documents` + `chunks`

### Images
1. create thumbnail
2. caption generation (if enabled)
3. OCR extraction (if enabled)
4. image embedding
5. insert `image_assets`
6. update doc metadata with image fields

---

## 9) UI/UX structure and behaviors

Single-page Jinja app in `index.html` with embedded JS.

Main sections:
- landing
- account/auth panel
- search panel (tabs: Text, Image, SQL)
- upload panel (collapsible)
- library panel (collapsible)
- deep research modal + side drawers
- search history accordion with filter + pagination

### Key UX features implemented
- Dark mode toggle + persistence in localStorage
- Space selector in top bar
- Search settings panel (per mode)
- RAG answer area + references area + timings badges
- SQL results table with client-side sortable headers
- Copy buttons for SQL and history payload blocks
- Upload drag/drop with progress rows, retry, concurrency
- Library grid/list toggle + bulk select/delete
- Deep Research modern modal UI with:
  - sessions drawer
  - notebook drawer
  - follow-up modal
  - memory/web toggles

---

## 10) Environment/config (practical set)

Most important env groups from `.env.example`:

1. **Server**: `HOST`, `PORT`, `WORKERS`
2. **DB**: `DATABASE_URL` or discrete DB_* vars, pool/retry tuning
3. **Auth/session**: basic auth + session cookie config
4. **Embeddings/vector**: text model + dims + pgvector metric/lists/probes
5. **Upload controls**: size limit, extensions, file count per space
6. **Image pipeline**: image model + caption + OCR toggles
7. **Storage backend**: `local|oci|s3|both` + provider credentials
8. **LLM provider**: `none|openai|oci`
9. **SQL controls**: row caps, memory turns, agentic retries, persistent memory toggle
10. **Deep Research controls**: time budget, top_k, confidence thresholds, followups, memory rollup

Additional notes from this scan:
- `.env.example` includes OCR, image captioning, and persistent memory flags that materially affect UI feature toggles.
- `LLM_PROVIDER` controls whether RAG answers are synthesized (OCI/OpenAI) or remain context-only.


---

## 11) API surface (quick reference)

### Core
- `GET /api/health`
- `GET /api/ready`
- `POST /api/upload`
- `POST /api/search`
- `POST /api/image-search`
- `GET /api/image-assets/{image_id}/thumbnail`
- `GET /api/doc-download?doc_id=...`
- `GET /api/doc-thumbnail?doc_id=...`
- `GET /api/kb`
- `DELETE /api/documents/{doc_id}`
- `POST /api/documents/{doc_id}/delete`

### Auth/spaces
- `POST /api/register`
- `POST /api/login`
- `POST /api/logout`
- `GET /api/me`
- `GET /api/spaces`
- `POST /api/spaces`
- `POST /api/spaces/default`

### History
- `GET /api/search-history`
- `GET /api/search-history/{session_id}`

### SQL
- `POST /api/sql-search`

### Memory rating
- `POST /api/memory/{memory_id}/rate`

### Deep Research
- `POST /api/deep-research/start`
- `POST /api/deep-research/ask`
- `GET /api/deep-research/conversations`
- `GET /api/deep-research/conversations/{conversation_id}`
- `POST /api/deep-research/conversations/{conversation_id}/title`
- `POST /api/deep-research/notebook/{conversation_id}`
- `DELETE /api/deep-research/notebook/{entry_id}`
- `GET /api/deep-research-config`

### LLM diagnostics
- `GET/POST /api/llm-debug`
- `POST /api/llm-test`
- `GET /api/llm-config`

---

## 12) Known implementation notes / caveats

1. **Upload memory usage**
   - `/api/upload` currently reads full file in memory; streaming helper exists (`save_upload_stream`) but is not wired to endpoint.

2. **Large frontend file**
   - `index.html` contains significant inline JS and app logic; UI changes are centralized but diff-heavy.

3. **CORS default**
   - permissive by default (`ALLOW_CORS=true`), tighten for production.

4. **Session invalidation on restart**
   - expected behavior due to `SERVER_START_TS` check.

5. **SQL guardrails are strict**
   - unsafe SQL/table mismatch will fail fast with clear error payloads.

6. **Long-lived JS in a single template**
   - The client JS is embedded in `index.html`; changes often require careful, large diffs.

---

## 13) Ready-to-edit hotspots (for next session)

### UI/UX likely touch points
- `app/templates/index.html` (layout, behavior, component logic)
- `app/static/style.css` (theme, responsive, interaction polish)

### Backend feature touch points
- search mode behavior: `app/main.py`, `app/search.py`
- ingestion: `app/store.py`, `app/text_utils.py`, `app/vision_embeddings.py`
- SQL agent behavior: `app/main.py` SQL helper methods
- DR behavior: `app/deep_research.py`, `app/agentic_research.py`
- schema changes: `app/db.py` + `schema_v3.sql`

---

## 14) Suggested workflow for your next changes

1. Define whether change is **UI-only**, **backend-only**, or **schema-affecting**.
2. If UI-only, update `index.html` + `style.css` together and test all three search tabs.
3. If backend API contract changes, update:
   - endpoint handler in `main.py`
   - client fetch call in `index.html`
4. If schema changes, update both:
   - idempotent migration logic in `db.py`
   - fresh bootstrap parity in `schema_v3.sql`
5. Validate with:
   - `/api/health`
   - `/api/ready`
   - one text search, one image search, one SQL search, one DR message

---

## 15) Session readiness status

✅ Codebase scanned and mapped
✅ Backend/dataflow reviewed
✅ UI/UX behavior reviewed
✅ Runtime/config/deployment context captured
✅ This recovery spec generated for context loss scenarios

This session is now ready for targeted code and UI/UX change requests.

---

## 16) Latest UX refinements applied (this session)

### A) Search settings icon refresh
- Replaced the plain `⚙️` text button with a cleaner slider-style SVG icon button.
- File: `search-app/app/templates/index.html`
- Styling: `search-app/app/static/style.css` (`.icon-btn-round`, `.icon-settings`)

### B) Deep Research header declutter
- Removed always-visible header toggles (Memory/Web) to reduce visual clutter.
- Moved these toggles into the existing **Actions (⋯)** menu under a dedicated “Research options” section.
- Kept behavior unchanged (same IDs, same localStorage persistence, same backend payload fields), so functionality remains intact while UX is cleaner.
- Files:
  - `search-app/app/templates/index.html`
  - `search-app/app/static/style.css` (`.dr-menu-divider`, `.dr-menu-section`, `.ios-toggle--menu`)

### C) Search settings icon polish
- Tooltip copy now reads “Show search options”.
- Settings icon color is lighter with subtle hover tint; dark mode variant aligned to soft slate tones.
- Files:
  - `search-app/app/templates/index.html`
  - `search-app/app/static/style.css` (`#settingsBtn` overrides)

### D) Dark mode parity sweep
- Deep Research panels and menus now use consistent dark surfaces (menus, source cards, follow-up chips, thinking badge).
- Search history filters and actions now have dark-mode input, button, and hover states.
- Settings panel fields and form controls now match the dark palette with improved focus states and placeholders.
- Files:
  - `search-app/app/static/style.css`

### E) Deep Research composer dark-mode polish
- Composer tray (textarea + actions row) now uses dark surfaces with muted status labels.
- Ingest/web status chips styled for legibility.
- Files:
  - `search-app/app/static/style.css`

### F) Upload + Library UI alignment
- Upload action button now uses the same primary styling as the search button for visual consistency.
- Library “Refresh” button promoted to primary styling to match the search call-to-action.
- Upload layout reordered so the **Browse files** prompt sits above the drag/drop zone, with Upload/Clear actions below it.
- Browse button text size increased slightly (with mobile-friendly scaling) for clearer readability.
- Files:
  - `search-app/app/templates/index.html`
  - `search-app/app/static/style.css`

### G) Upload completion + library controls polish
- Upload flow now fully resets the dropzone, file input, and progress list after processing finishes (preventing lingering files before the library refresh).
- Upload button gains a rotating gradient border (matching the AI icon trace) whenever files are ready; it clears on upload start and after processing. Definition: the border effect is a linear-gradient sweep that moves along the button edge (not a rotating conic “fan”), with a slower cadence and brighter colors for visibility.
- Library bulk-selection controls restyled into a modern toolbar layout with grouped actions and clearer selection count.
- Files:
  - `search-app/app/templates/index.html`
  - `search-app/app/static/style.css`
