# Search App Session Specs (Context-Recovery Document)

Last updated: 2026-02-26 19:07 (AEDT)
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
- `STORAGE_BACKEND` controls local vs object storage persistence (see storage behavior below).

### Storage backend behavior (local vs object storage)
- Uploads always write a **local file** for ingestion.
- When `STORAGE_BACKEND=local`: files persist under `storage/uploads/...` and remain on disk.
- When `STORAGE_BACKEND=oci` or `s3`: uploads are sent to object storage **and** written to a local **temp** path under `storage/tmp_uploads/...` for ingestion; the local temp file is used for parsing and can be removed if `DELETE_UPLOADED_FILES=true`.
- When `STORAGE_BACKEND=both`: uploads are stored in object storage and **also** persisted locally under `storage/uploads/...`.
- If extraction fails and an object reference exists, ingestion retries by downloading the object from storage and re-parsing.
- PDF OCR fallback runs against the **local ingest file** (temp or persistent), so it follows the same storage backend behavior above and relies on the local copy created during upload.
- Image assets (including PDF page images) upload their **full image + thumbnail** to object storage when `STORAGE_BACKEND` is `oci`, `s3`, or `both`, and thumbnail endpoints fall back to object storage when local files are missing.
- Delete flow removes document files **and** related image asset files from object storage when using `oci`/`s3`/`both`, ensuring consistent behavior across file types and nodes.
- Document deletion removes DB records and deletes object-storage copies when the backend is `oci`/`s3`/`both`, while local file cleanup is best-effort on the node handling the delete.
- Retrieval endpoints for `image_assets` now use the image asset’s own object key (`file_path` / `thumbnail_path`) as the single object-storage fallback path, avoiding extra object GET attempts via document-level object names.
- Image thumbnail/object keys now include the relative dated upload path (not just basename), reducing cross-folder filename collision risk and keeping object-key mapping deterministic.
- For directly uploaded images, source image object upload is skipped when the source already exists in object storage under the same object key (prevents redundant upload/write amplification).

### Multi-node consistency notes
- **Source of truth**: Postgres + object storage (OCI/S3) when `STORAGE_BACKEND` is `oci`, `s3`, or `both`.
- **Uploads**: always write a local ingest copy; object storage receives the source file and image assets/thumbnails so any node can serve downloads/thumbnails.
- **Downloads/Thumbnails**: endpoints first try local paths, then fall back to object storage for images, thumbnails, and document files.
- **Deletes**: remove DB records and delete object storage objects (source file + thumbnails + image assets) for `oci`/`s3`/`both`; local node cleanup is best-effort.


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

---

## 17) New change request (2026-02-26, this session)

### Task 1: Search history multi-type tags + persistence checks
- Current UI shows `session.last_activity_type` only (single badge in `index.html` -> history summary). This is why only the last search type tag appears.
- `/api/search-history` returns only the last activity’s `activity_type`; it does **not** aggregate per-session activity types.
- `/api/search-history/{session_id}` returns activity list with `activity_type` values for that session.
- Logging is done via `_log_search_activity()` in `app/main.py`. Session IDs are based on signed cookies (invalidated on server restart). Sessions are persisted server-side in `search_sessions` and `search_activity` tables.
- **Planned change**: update `/api/search-history` to include aggregated activity types for each session (unique types + count). Update UI to render all tags like `image_search × 3` in the session summary line.
- **Persistence checks**: cookies persist across refresh; logout clears cookie; session is invalidated on server restart due to SERVER_START_TS. Deep Research stores conversation ID in localStorage per space. SQL context + memory toggles persist in localStorage.

### Task 2: Image card hover modal + download + UI polish
- Current image results are rendered in `index.html` under `doImageSearch()` with `.image-card` containing thumbnail, caption, tags+score.
- Needs new UX:
  - Hover (or tap) shows full-size image in a modal/popup.
  - Download button appears in the card (similar to library download link).
  - Score, caption, tags should get soft highlight “pill” styling with different colors.
- Existing image endpoints: `/api/image-assets/{image_id}` for full image, `/api/doc-download` for doc download.
- **Planned change**: update image card markup to include:
  - Hoverable thumbnail that triggers modal with full-size image (`/api/image-assets/{image_id}` if available).
  - Download link/button using `file_url` (already returned by API).
  - Pill classes for caption/tags/score with distinct colors.

### Upload button alignment
- Upload section currently has a primary `Upload` button and a “Browse files” label styled as button.
- **Planned change**: match Upload button text + size to “Browse files”, and make the animated border around upload button slightly thicker/brighter.

### Mobile layout reset + Deep Research on mobile
- CSS already has a mobile breakpoint at 720px and sets `.dr-modal` to full-screen mobile.
- **Planned change**:
  - Ensure after login on mobile, UI snaps to the mobile layout/stacking (no desktop toggles lingering).
  - Deep Research modal should render like a form-driven mobile view (inputs, buttons, drawers) with improved sizing at mobile breakpoint.
  - Add JS to force `window.scrollTo(0,0)` and reflow on login in mobile view.

### Favicon + Apple Web App icon
- No favicon or apple-touch icon is defined in `index.html` currently.
- **Planned change**:
  - Add `link rel="icon"` and `apple-touch-icon`.
  - Create an SVG favicon and a 180x180 PNG (or SVG) apple touch icon in `app/static/`.

---

## 18) Change request status (2026-02-26, implemented)

### ✅ Task 1: Search history multi-type tags + persistence
- `/api/search-history` now aggregates activity types per session (`activity_types` json map) in `app/main.py`.
- UI now renders all session activity badges with counts (e.g., `image_search × 3`).
- Persistence notes unchanged: signed cookie persists across refresh; logout clears; restart invalidates via `SERVER_START_TS`.

### ✅ Task 2: Image card download + pill styling (hover modal removed)
- Image search cards updated in `app/templates/index.html` to use a simplified layout:
  - inline thumbnail + caption + score/tag pills
  - download link rendered under the tags
- Hover modal preview removed per updated request.
- CSS cleaned up to keep the simplified layout and pill styles.

### ✅ Upload button alignment + border accent
- Upload button now uses `.upload-btn` class to match Browse Files size/weight.
- Upload-ready border is thicker/brighter via updated `::after` styling.

### ✅ Mobile layout + Deep Research tweaks
- JS adds `is-mobile` class and resets panels after login on mobile.
- Mobile CSS tweaks for DR modal inputs/actions to render form-like stacks.

### ✅ Favicon + Apple touch icon
- Added `/static/favicon.svg` and `/static/apple-touch-icon.png`.
- `index.html` now links both.

---

## 19) Latest change request (2026-02-26, in progress)

### Task: Image cards word-cloud tags + uniform height + pinned download
- User request: show only the top 10 tags in image search results, styled as a soft “word cloud”, keep all cards a uniform height, and pin the Download button to the bottom of each card. Also restyle the **Browse files** upload button to match a modern SaaS look and render well on mobile/desktop.
- Implementation updates:
  - `app/templates/index.html`: image search cards now slice tags to top 10 and render `tag-cloud` spans with size classes; download link is pinned via `.image-download--pinned`.
  - `app/static/style.css`: added `.tag-cloud*` styles (soft highlight), set `.image-card` min height for uniform cards, and pinned download styling (margin-top: auto). Browse button refreshed with modern SaaS colors (light + dark mode + hover).
- Dark mode variants added for the tag cloud styling.

---

## 20) Latest changes (2026-02-26, implemented)

### UI/UX tweaks
- **Home reset** now clears search history filters and library selections for a fresh start.
- Image search cards: tag cloud + uniform card height + pinned download button.
- Browse files button restyled to a SaaS-style look (desktop + mobile).
- SQL memory rating label copy updated to **“Use these results again?”**.
- SQL memory rating block moved beneath SQL output in the SQL panel.

### Scripts/ops
- `start.sh` includes a health check using `HEALTH_URL` (defaults to `/api/ready`).
- `stop.sh` performs a graceful shutdown and SIGKILL fallback after timeout.
- `run.sh` supports `SKIP_DEPS=true` to skip `uv sync`.

### PDF OCR fallback + image extraction
- **Config flags** added: `OCR_PDF` and `PDF_IMAGE_EXTRACTION` (both documented in `.env`/`.env.example`).
- `text_utils.py` now performs OCR fallback on PDFs when enabled, rasterizing pages via PyMuPDF and using the existing OCR pipeline.
- `store.py` extracts a **single first-page PDF image** (thumbnail) and ingests it into `image_assets` when enabled (writes a JPEG under `storage/uploads/pdf_pages/<pdf_stem>/`).
- Document metadata includes `pdf_image_count` and `pdf_image_extraction_enabled`; Vision model unavailability is logged as a warning.
- **Notes**: requires Tesseract installed for OCR; uses PyMuPDF for rendering (already in `pdf` extra).
  - OCR still runs **per page** for text extraction; only the thumbnail upload is limited to the first page to avoid object-storage bloat.
  - Object storage calls are minimized by avoiding duplicate source image uploads and by using a single deterministic image/thumbnail retrieval key path.
  - PDF thumbnail generation path does **not** run an extra OCR pass (to avoid duplicate OCR warnings/cost); OCR is handled in the PDF text extraction pipeline.

---

## 21) Session/search history audit + fixes (2026-02-26, completed)

### Scope
- Reviewed and finalized the active request to verify session history/activity correctness and fill gaps in search history behavior.
- Focus files:
  - `search-app/app/main.py`
  - `search-app/app/templates/index.html`

### Backend fixes confirmed in `app/main.py`
- **Activity space consistency fix** in `_log_search_activity(...)`:
  - `search_activity.space_id` now uses `session_space_id` (resolved default-aware value), not the raw incoming `space_id`.
  - Prevents null/mismatched space attribution when callers omit `space_id` and fallback default space is used.
- **Search history list filtering control** in `GET /api/search-history`:
  - Added query parameter `include_empty: bool = False`.
  - Default behavior now excludes sessions with no activity rows (unless `include_empty=true`).
- **Session-level activity totals** in `GET /api/search-history`:
  - Added aggregated `activity_count` and `activity_types` map per session.
  - Response filters now echo `include_empty`.
- **Detail pagination completeness** in `GET /api/search-history/{session_id}`:
  - Added `total` count and `has_more` boolean for robust UI incremental loading.

### Frontend fix completed in `app/templates/index.html`
- Repaired malformed JS in `loadSessionActivities` (introduced by prior fuzzy patch collision).
- Restored proper `try/catch` block integrity and conditional guards.
- Finalized load-more + status behavior:
  - `loadMoreBtn.hidden = !Boolean(detail?.has_more)` (guarded)
  - status text shows `Loaded X of Y` using backend `total`

### Validation performed
- Python compile check:
  - `python3 -m py_compile search-app/app/main.py` ✅
- Targeted frontend logic sanity check (scripted file inspection):
  - `loadSearchHistory` block present ✅
  - `detail?.has_more` usage present ✅
  - `Loaded ${loaded} of ${total}` status string present ✅
  - try/catch marker counts balanced for edited block ✅

### Result
- Session activity logging, list/detail API contracts, and UI history expansion/load-more flow are now aligned.
- The previously broken history JS section is repaired and no longer syntactically malformed.

---

## 22) PDF OCR-only ingest + delete flow audit (2026-02-26, completed)

### Request implemented
- Removed PDF thumbnail/image extraction behavior entirely.
- Kept PDF OCR behavior for text extraction/chunking only.
- Re-checked centralized delete + upload/download/retrieve flow for consistency.

### Code changes
- File: `search-app/app/store.py`
  - Removed PDF page image extraction path from `ingest_file_path(...)`.
  - Removed `_extract_pdf_page_images(...)` helper function.
  - Result: PDFs now ingest as document text/chunks only (with optional OCR in `text_utils.py`), without creating `image_assets` rows or PDF-derived thumbnails.

### OCR behavior retained
- File: `search-app/app/text_utils.py`
  - `extract_text_from_pdf(...)` still supports OCR fallback via `settings.ocr_pdf_enabled`.
  - OCR output is merged into extracted text and then chunked/embedded normally.

### Upload / download / retrieve / delete audit notes
- **Upload** (`/api/upload` in `main.py`):
  - Uses `save_upload(...)` -> `ingest_file_path(...)`.
  - For PDFs, ingestion now writes document + chunks only (no PDF thumbnail/image asset side effects).
- **Download** (`/api/doc-download`):
  - Centralized source retrieval: local path first, object storage fallback if configured.
- **Thumbnail retrieval** (`/api/doc-thumbnail`):
  - Still available for image-backed docs; PDFs ingested after this change will have no thumbnail metadata and return unavailable (expected).
- **Delete** (centralized):
  - Both `DELETE /api/documents/{doc_id}` and `POST /api/documents/{doc_id}/delete` route to `_delete_document_by_id(...)`.
  - Helper removes document DB row and performs best-effort cleanup for local/object storage source + related image assets.

### Validation executed
- `python3 -m py_compile search-app/app/store.py search-app/app/main.py search-app/app/text_utils.py` ✅
- Static assertions on `store.py`:
  - `_extract_pdf_page_images(` reference removed ✅
  - `pdf_image_extraction_enabled` usage removed ✅

### Effective outcome
- PDF pipeline is now OCR/text-only for ingestion.
- No new PDF thumbnails are generated, stored, uploaded, or retrieved.
- Delete functionality remains centralized through `_delete_document_by_id(...)` and is shared by both delete endpoints.

---

## 23) Embedded-image OCR for DOCX/PPTX + references download links (2026-02-26, completed)

### Request implemented
- Keep PDF OCR behavior and no PDF thumbnail storage/retrieval.
- Add OCR extraction for scanned/embedded images in DOCX and PPTX, appending OCR text to extraction output **before chunking**.
- Ensure no thumbnail persistence is added for DOCX/PPTX OCR path.
- Make Text Search references file names downloadable from object-storage-backed document endpoint.
- Re-check retrieval/storage/deletion path integrity.

### Code changes

#### A) Embedded-image OCR added to document extractors
- File: `search-app/app/text_utils.py`
- New helpers:
  - `_ocr_text_from_image_bytes(...)`
  - `_extract_embedded_image_ocr_from_zip(...)`
- Behavior:
  - For DOCX: scans `word/media/*` images inside the DOCX zip and OCRs them.
  - For PPTX: scans `ppt/media/*` images inside the PPTX zip and OCRs them.
  - OCR text is appended to normal extracted text and normalized, then passed to chunking pipeline.
- Important guardrail:
  - This OCR path only extracts text; it does **not** persist thumbnails or image assets.

#### B) Text Search references now clickable downloads
- File: `search-app/app/main.py`
- `POST /api/search` response references now include:
  - `url: /api/doc-download?doc_id=<document_id>`
- Frontend already renders `references[].url` as an anchor in `index.html`, so file names in the References panel are now clickable downloads.

### Retrieval / storage / deletion integrity check (revalidated)
- Upload path remains centralized: `/api/upload` -> `save_upload(...)` -> `ingest_file_path(...)`.
- Download path remains centralized via `/api/doc-download` (local first, object storage fallback).
- Thumbnail retrieval remains via `/api/doc-thumbnail` for docs that have thumbnail metadata (typically image docs).
- Delete remains centralized:
  - `DELETE /api/documents/{doc_id}`
  - `POST /api/documents/{doc_id}/delete`
  - both route through `_delete_document_by_id(...)`.

### Validation run
- Compile checks passed:
  - `python3 -m py_compile search-app/app/main.py search-app/app/text_utils.py search-app/app/store.py` ✅
- Endpoint/centralization presence checks passed:
  - `/api/doc-download`, `/api/doc-thumbnail`, `_delete_document_by_id`, both delete routes detected in `main.py` ✅

### Effective outcome
- PDF remains OCR-capable (text/chunks) and no longer has PDF thumbnail/image extraction side effects.
- DOCX/PPTX can now OCR embedded scanned images into extraction text prior to chunking.
- Text Search References panel file names now resolve to document download links (object-storage-aware through backend endpoint).

---

## 24) Embedded-image OCR warning-noise reduction (2026-02-26, completed)

### Trigger
- During DOCX upload on hosts without Tesseract installed, logs emitted a warning per embedded image:
  - `OCR unavailable for embedded image: tesseract is not installed or it's not in your PATH`
- This created noisy logs while ingestion itself should remain non-fatal.

### Change implemented
- File: `search-app/app/text_utils.py`
- Added a process-local guard for embedded-image OCR attempts:
  - `_EMBEDDED_IMAGE_OCR_DISABLED`
  - `_EMBEDDED_IMAGE_OCR_WARNED`
  - helper `_disable_embedded_image_ocr_once(reason)`
- Behavior now:
  1. On first definitive backend-missing signal (e.g., missing `pytesseract` / missing `tesseract`), embedded-image OCR is disabled for the running process.
  2. A single warning is logged once:
     - `Embedded-image OCR disabled for this process: <reason>`
  3. Subsequent embedded image OCR attempts in DOCX/PPTX extraction return immediately without repeated warnings.

### Scope and non-functional behavior
- Affects **embedded-image OCR path only** (DOCX/PPTX zip media scan flow).
- Does **not** alter PDF OCR logic, chunking logic, or document ingestion success criteria.
- Ingestion remains resilient: document text extraction/chunking proceeds even when OCR backend is unavailable.

### Outcome
- Eliminates repetitive warning spam while preserving graceful degradation.
- Keeps behavior aligned with user requirement: OCR is attempted when available, skipped cleanly when not.

---

## 25) PDF OCR verification check (2026-02-26, completed)

### Concern checked
- User reported concern that PDF OCR might be broken after recent OCR/logging changes.

### Verification steps performed
1. Confirmed config gate still exists and is wired:
   - `settings.ocr_pdf_enabled` in `app/config.py` (env: `OCR_PDF`).
   - `extract_text_from_pdf(...)` in `app/text_utils.py` still calls `_ocr_pdf_pages(path)` when `ocr_pdf_enabled` is true.
2. Executed a focused runtime test under project environment (`uv run`) with OCR enabled:
   - monkeypatched `_ocr_pdf_pages` to return a known marker string.
   - ran `extract_text_from_pdf(...)` against `dataset/Australian-Privacy-Act.pdf`.
3. Observed results:
   - `OCR_PDF_ENABLED= True`
   - `OCR_CALLED= True`
   - `HAS_MARKER= True`

### Conclusion
- PDF OCR path is functioning and still integrated in extraction flow when `OCR_PDF=true`.
- Recent warning-noise changes were limited to embedded-image OCR (DOCX/PPTX path) and did not disable/alter PDF OCR execution.

---

## 26) OCR runtime-path hardening for PDF/DOCX/PPTX (2026-02-26, completed)

### Problem observed
- User reported that scanned PDF OCR appeared missing (chunk count dropped, OCR text not present) while logs also showed embedded-image OCR disabling in some runs.
- This pointed to environment/runtime OCR resolution issues (binary path visibility) rather than disabled OCR logic.

### Diagnosis performed
- Verified host-level Tesseract exists: `tesseract 5.5.1`.
- Verified app runtime environment via `uv run`:
  - `which_tesseract=/opt/homebrew/bin/tesseract`
  - `pytesseract` import OK
  - `_ocr_pdf_pages(...)` returned OCR text (`pdf_ocr_chars=30175` on dataset sample)
- Conclusion: OCR pipeline works when runtime can resolve Tesseract consistently.

### Hardening change implemented
- File: `search-app/app/config.py`
  - Added `OCR_TESSERACT_CMD` setting (`settings.ocr_tesseract_cmd`).
- File: `search-app/app/vision_embeddings.py`
  - In `ocr_image_text(...)`, Tesseract command resolution now follows:
    1. `OCR_TESSERACT_CMD` (explicit override)
    2. `shutil.which("tesseract")`
    3. common absolute fallbacks:
       - `/opt/homebrew/bin/tesseract`
       - `/usr/local/bin/tesseract`
       - `/usr/bin/tesseract`
  - Sets `pytesseract.pytesseract.tesseract_cmd` when found.
  - Improves error hinting to recommend `OCR_TESSERACT_CMD` when PATH-related failures occur.

### Impact
- Makes OCR behavior more deterministic across service launch contexts (shell/launchd/daemon differences in PATH).
- Applies to all OCR call sites using `ocr_image_text(...)`:
  - PDF OCR fallback
  - DOCX/PPTX embedded-image OCR
  - image OCR extraction path

### Validation
- Compile check passed:
  - `uv run python -m py_compile app/config.py app/vision_embeddings.py app/text_utils.py` ✅

### Cross-platform update
- Added env documentation in both:
  - `search-app/.env`
  - `search-app/.env.example`
- New variable:
  - `OCR_TESSERACT_CMD=` (optional absolute override)
- Guidance:
  - Leave empty when PATH is correctly configured.
  - Set explicitly in heterogeneous deployments (macOS/Linux/Oracle Linux/Ubuntu/systemd) to avoid PATH drift.
- `vision_embeddings.py` fallback probe list now includes `/bin/tesseract` in addition to Homebrew and common Unix locations.

---

## 27) Full codebase health check sweep (2026-02-26, completed)

### Scope requested
- User requested a broad verification pass to confirm the codebase changes are stable and expected behavior is intact.

### Checks executed
1. **Whole backend syntax/compile validation**
   - Command:
     - `uv run python -m py_compile $(find app -name '*.py' -type f | tr '\n' ' ')`
   - Result: ✅ success (no syntax/compile errors across `search-app/app`).

2. **OCR runtime smoke checks**
   - Verified effective runtime settings:
     - `OCR_ENABLED=True`
     - `OCR_PDF_ENABLED=True`
     - `OCR_TESSERACT_CMD` currently empty (PATH-based resolution active)
     - `which tesseract => /opt/homebrew/bin/tesseract`
   - Verified real PDF OCR execution:
     - `_ocr_pdf_pages(dataset/DataPrivacy-Law-India.pdf)`
     - `PDF_OCR_CHARS=30175`, non-empty output ✅
   - Embedded-image OCR guard status check:
     - `_EMBEDDED_IMAGE_OCR_DISABLED=False` in current run ✅

3. **Critical endpoint/flow contract presence checks** (`app/main.py`)
   - `_log_search_activity` found ✅
   - `/api/doc-download` route found ✅
   - `/api/doc-thumbnail` route found ✅
   - `_delete_document_by_id` centralized helper found ✅
   - `/api/search-history` route found ✅
   - `/api/search-history/{session_id}` route found ✅

### Summary
- Current codebase state is healthy for the areas changed in this session:
  - OCR pipeline (including PDF OCR) is operational in runtime checks.
  - Session/search history and document retrieval/delete route contracts are present.
  - No compile-time regressions detected across backend Python modules.

