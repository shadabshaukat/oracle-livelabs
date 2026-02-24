# Phase V3 Build: OCR + NL2SQL

This phase extends the Enterprise Search App with two major features:

1. **OCR-driven metadata for images** (detect text in images, extract, and make it searchable)
2. **NL2SQL** (natural language to SQL) with a dedicated **SQL Search** UI and backend

Phase V3 also absorbed **Deep Research (DR)** upgrades so the platform is unified across text, SQL, and DR. This includes search history capture, persistent memory parity, and complete removal of the Valkey/Redis cache in favor of OCI PostgreSQL.

The goal is to keep the existing UI/UX feel while seamlessly integrating these new capabilities.

---

## 1) OCR for Image Text (Searchable Metadata)

### Objective
When an uploaded image contains text (e.g., invoices, screenshots of spreadsheets, scanned forms), the app should:

- Detect that text is present
- Extract text using OCR
- Store it in metadata
- Make it searchable in **Image Search** (balanced with captions)

### Functional Flow
1. **Image ingest** (`store.py` → `_process_image_asset`) detects text using OCR.
2. OCR output stored in:
   - `image_assets.ocr_text` (new column)
   - `documents.metadata.ocr_text` (for UI metadata + references)
3. **Image Search** ranks results using a mix of:
   - Caption relevance
   - OCR text relevance
   - Image embedding similarity

### Design Notes
- OCR should only run when text is detected (avoid cost on non-text images).
- OCR output stored as plain text; we keep the full output for searchable FTS.
- OCR is applied per image, not per PDF/document.

### Storage / Schema Updates
- Add column to `image_assets`:
  - `ocr_text TEXT` (nullable)
- Add a FTS vector for OCR + caption combination or reuse a generated expression in search queries.

---

## 2) NL2SQL (“SQL Search”)

### Objective
Add a new search mode in the UI that lets users ask natural language questions and generate SQL for PostgreSQL. Users can choose:

- **Generate SQL only**
- **Generate SQL + execute**
- **Generate SQL + execute + show results**

### UI Requirements
- Add a **SQL Search** tab next to Text and Image search.
- Keep layout consistent with existing cards and panels.
- Provide toggles:
  - ✅ Generate SQL
  - ✅ Execute SQL (SELECT-only)
  - ✅ Show results
- Provide a **SQL Context** toggle (User schema vs System catalog) with a badge indicating the active context.

### Backend Flow
1. User submits question + SQL context (user/system).
2. Backend assembles:
   - User question
   - SQL context (public/user schemas vs pg_* catalogs)
   - Candidate table list (schema filtering step)
3. LLM **selects relevant tables** from the candidate list (first pass).
4. Backend builds schema overview + DDL for the selected tables.
5. LLM generates SQL **restricted to those tables** (second pass).
6. If “Execute” selected:
   - Enforce **SELECT-only**
   - Apply row limit (default 200)
   - Return results and execution time (per statement)

### Safety + Performance
- Only SELECT queries allowed.
- Hard row limit enforced in backend (`SQL_MAX_ROWS=200`).
- Query results are truncated if they exceed the limit.
- `information_schema` is **explicitly blocked** in all modes.
- System context is limited to **pg_* schemas only** (pg_catalog, pg_stat, etc.).

### New API Endpoints
- `POST /api/sql-search`
  - Input: `{ question, execute, show_results, space_id, sql_context, max_rows, system_prompt, memory_turns }`
  - Output: `{ sql, executed, queries, rows, columns, elapsed_ms, memory_turns }`

---

## Configuration Additions (.env)
- `OCR_ENABLED=true`
- `OCR_ENGINE=tesseract`
- `SESSION_ACTIVITY_TTL_SECONDS=28800`
- `SQL_MAX_ROWS=200`
- `SQL_SYSTEM_PROMPT="You are an expert PostgreSQL (v14-v18) SQL assistant..."`
- `SQL_DEFAULT_ROWS=200`
- `SQL_MEMORY_TURNS=10`
- `SQL_PERSISTENT_MEMORY_ENABLED=false`
- `TEXT_PERSISTENT_MEMORY_ENABLED=false`
- `IMAGE_PERSISTENT_MEMORY_ENABLED=false`
- `DEEP_RESEARCH_PERSISTENT_MEMORY_ENABLED=true`
- `PERSISTENT_MEMORY_TOP_K=5`
- `PERSISTENT_MEMORY_MAX_CHARS=4000`
- `PERSISTENT_MEMORY_SUMMARY_MAX_CHARS=1200`
- `LLM_CACHE_TTL_SECONDS=3600`

---

## Deliverables Checklist
- [x] OCR extraction for images + metadata storage
- [x] Search integration: OCR text affects ranking
- [x] New DB columns for OCR
- [x] NL2SQL backend route with execution guardrails
- [x] SQL Search UI + UX
- [x] Updated docs + env example
- [x] Search history + session activity capture
- [x] Deep Research persistence + history + memory parity
- [x] Valkey/Redis removal (Postgres-only persistence)

---

## Build Log (V3 Implementation Notes)

### OCR Enhancements
- Added OCR configuration flags: `OCR_ENABLED`, `OCR_ENGINE`, `OCR_MIN_CHARS`, `OCR_MAX_CHARS`.
- Added `image_assets.ocr_text` column and stored OCR text during ingestion.
- OCR extraction now uses Tesseract `image_to_data` for text detection and skips images with no confident words.
- OCR text is included in metadata and search ranking (balanced with captions) for image search.

### NL2SQL Backend
- Added `/api/sql-search` with SELECT-only enforcement and row limits (`SQL_MAX_ROWS=200`).
- Added schema context fetch and SQL extraction helpers.
- Added a two-pass NL2SQL flow (LangChain SQLDatabaseChain-style): the model first selects relevant tables from the schema, then generates SQL using only those tables in the prompt context.
- Tightened OCI prompt handling to pass schema context separately (avoid double-wrapped prompts).
- Added table validation for generated SQL (public tables for analysts; system catalogs for admin in system mode).
- Added `SQL_SYSTEM_PROMPT` setting to control the system prompt used for NL2SQL generation (Postgres v14–v18, no hallucinations).
- SQL generation now includes schema DDL context to reduce hallucinated tables/columns.
- Added query splitting for multi-statement execution and per-query results.
- Added table selection guardrails: SQL prompt includes **Allowed tables** and explicit instructions to use only those tables.
- Added SQL context enforcement (`sql_context=user|system`) to align with role-based schema access.
- Added optional SQL memory (LangChain-style) to pass prior questions + SQL into the prompt, controlled by `SQL_MEMORY_TURNS` or the UI setting.
  - Memory is scoped **per space** (not per user) to keep shared context within a space.

### Role-Based Access Control (SQL Search)
- Added `roles` table and `users.role_id` foreign key; seeded roles: `user`, `analyst`, `admin`.
- Sessions and `/api/me` now include role information.
- SQL Search endpoint restricted to `analyst`/`admin` roles.
- Admin role can query system metadata **only** in **System catalog** mode. System catalog context is limited to **pg_* schemas only** (pg_catalog, pg_stat, etc.), and explicitly excludes `information_schema` and user schemas.
- Analyst role is limited to **User schema** context only (public/user schemas).

### SQL Search UI
- Added SQL Search tab aligned with Text/Image search.
- SQL Search now reuses the main search bar (same styling as Text/Image) with the SQL placeholder when SQL mode is active.
- Added SQL question input (same search bar), execute/show-results toggles, generated SQL display, and results table rendering.
- SQL tab is only visible to analyst/admin roles.
- Added SQL Search settings: Run SQL / Show SQL radio options and a max rows input tied to SQL_MAX_ROWS.
- Added SQL Memory Turns setting to keep a rolling context of prior SQL questions (0 disables).
  - Memory is scoped per space (shared context among users in the same space).
- Added persistent memory toggles (SQL/Text/Image) gated by env flags; default OFF and stored in localStorage.
- Added thumbs up/down rating UI to mark memories for reuse.
  - Only thumbs-up (`rating=1`) memory entries are used for semantic retrieval.
  - Ratings are persisted in `memory_events` and updated via `/api/memory/{id}/rate`.
- Added SQL Context toggle (User schema vs System catalog) with a context badge in the SQL results header; selection is persisted in localStorage. System catalog mode now uses **pg_* system schemas only** (pg_catalog, pg_stat, etc.) and explicitly excludes information_schema and user schemas to mirror a LangChain-style split between user data and system catalogs.
- SQL errors are rendered inline in the SQL panel for easier troubleshooting.
- Added editable SQL system prompt setting (defaults to SQL_SYSTEM_PROMPT) and persisted max rows.
- Removed SQL history + copy button to simplify the UI, and added sortable, scrollable results table with execution timing badges.

### SQL Search Role + Context Quick Guide (UI)
- **Analyst**: User schema context only (public/user schemas).
- **Admin**: Can toggle **User schema** or **System catalog**.
  - System catalog uses **pg_* schemas only** (pg_catalog, pg_stat, etc.).
  - `information_schema` is blocked in all modes.

### NL2SQL Implementation Details (In Depth)
- **Two-pass schema filtering** (SQLDatabaseChain pattern): the model first selects relevant tables from a candidate list, then generates SQL from **only** those tables.
- **Candidate table sourcing**:
  - User context: public/user schemas (role-checked).
  - System context: pg_* schemas only.
  - Relkinds: tables, views, materialized views, foreign tables, and partitioned tables (`r`, `v`, `m`, `f`, `p`).
- **Prompt context** includes:
  - Allowed tables list (explicit guardrail).
  - Schema overview + DDL for selected tables.
  - Role-specific instructions (admin/system catalog vs analyst/user data).
  - System prompt override support from UI settings.
- **Memory buffer**: previous questions + SQL are provided as contextual Q/SQL pairs in the prompt (LangChain-style), scoped per space and limited by the `SQL_MEMORY_TURNS` setting.
- **Persistent memory**: stored in Postgres (`memory_events`) with embeddings + summaries.
  - Semantic retrieval uses pgvector similarity and `rating=1` filter.
  - Prompt context uses summarized memory text to stay within `PERSISTENT_MEMORY_MAX_CHARS`.
  - Memory events partitioned by `created_at` with ivfflat index for large volumes.
- **Execution guardrails**:
  - SELECT-only enforcement.
  - Row limits via `SQL_MAX_ROWS` and UI max rows setting.
  - Multi-statement splitting with per-query results/elapsed time.
  - Table validation to prevent references outside allowed schemas.

### Files Updated
- `app/db.py`: roles table + users.role_id + seed roles + safe FK creation.
- `app/users.py`: role-aware user lookup/auth.
- `app/main.py`: role in sessions, NL2SQL prompt and safety checks, SQL access control.
- `app/vision_embeddings.py`: OCR detection via Tesseract data.
- `app/templates/index.html`: SQL Search UI + role gating.
- `app/static/style.css`: SQL results styling.
- `.env` / `.env.example`: OCR + SQL_MAX_ROWS config.
- `pyproject.toml`: `pytesseract` dependency.
- `schema_v3.sql`: consolidated schema for fresh V3 database seeding.

### Session History + Activity Capture (V3 add-on)
- Added `search_sessions` and `search_activity` tables to capture per-login sessions and detailed request/response payloads.
- Session IDs are UUID7-like hex values stored in the signed session cookie and logged per activity.
- New session timeout config: `SESSION_ACTIVITY_TTL_SECONDS` (default 8 hours).
- Added `/api/search-history` and `/api/search-history/{session_id}` endpoints for UI display.
- Account panel now includes a **Search History** accordion with session list + drill-in details.
- Search history now supports pagination + filters (activity type, space, and time range) and surfaces audit metadata (last IP + user-agent per session; client IP + user-agent per activity).

### Deep Research Integration (V3 continuation)
- **Unified persistence**: DR conversations, steps, notebook entries, and external docs are stored in Postgres (`deep_research_conversations`, `deep_research_steps`, `deep_research_notebook_entries`, `conversation_external_docs`).
- **Search history**: DR runs are logged with `activity_type=deep_research` and a summary of `Deep Research · {message[:120]}` to align with text/sql history.
- **Persistent memory parity**: DR uses the same `memory_events` store and retrieval helpers as text/sql (`memory_store.py`), with `persistent_memory` opt-in controlled by `DEEP_RESEARCH_PERSISTENT_MEMORY_ENABLED`.
- **UI parity**:
  - DR persistent memory toggle appears in the header when enabled.
  - Follow-up questions open a modal for response entry (enter-to-send, send, close).
  - Search history filter includes **Deep Research**.
- **Valkey removed**: All Valkey/Redis caches were removed. LLM responses now use a lightweight in-process cache (`app/llm.py`) with `LLM_CACHE_TTL_SECONDS`.

### Deep Research Migration Notes
- If upgrading an existing database, apply `schema_v3.sql` or ensure `app/db.py` has run to create DR tables.
- Remove any Valkey/Redis variables from .env; only OCI PostgreSQL is required.

### Manual Role Assignment
Use SQL to assign roles until an admin UI is available:

```sql
UPDATE users SET role_id = (SELECT id FROM roles WHERE name = 'analyst') WHERE email = 'you@example.com';
UPDATE users SET role_id = (SELECT id FROM roles WHERE name = 'admin') WHERE email = 'admin@example.com';
```

### Consolidated Schema (Fresh Database)
For new environments, use `schema_v3.sql` to create tables and seed roles in one go. This avoids multiple ALTERs on a fresh database. The script is parameterized via psql `\set` variables; override these values to match your environment:

- `EMBEDDING_DIM`
- `IMAGE_EMBED_DIM`
- `PGVECTOR_METRIC`
- `PGVECTOR_LISTS`
- `FTS_CONFIG`

### Role Assignment Notes
- New users are created with role `user` by default (set in the application layer during registration).
- You can promote users by running explicit SQL updates (see above).

---

## Known Issues / Pending
- SQL Search may return **400** for admin if generated SQL is unsafe or references disallowed tables; backend logging was added to flag unsafe SQL or invalid table usage for troubleshooting.

---

## Notes
- OCR uses **Tesseract** via `pytesseract`.
- We reuse the existing LLM provider settings (OCI/OpenAI) for NL2SQL.
- SQL execution is **SELECT-only** with row limits for safety.

## NL2SQL Improvement Ideas (Future Enhancements)
These align with common patterns in LangChain/LangGraph and MCP-based SQL agents:

1. **Query plan + lint loop**: generate SQL → run `EXPLAIN` → let the model optimize for cost/latency before execution.
2. **Schema-aware validation**: validate columns against `information_schema.columns` in a read-only reflection pass (kept server-side, still blocked in generated SQL).
3. **Guarded tool calls**: tool/agent step that refuses execution if tables/columns are not in allowed list (hard block instead of post-check).
4. **Semantic table retrieval**: embed table/column descriptions and retrieve relevant tables before LLM selection (improves large-schema accuracy).
5. **Join-path hints**: maintain a lightweight FK graph to suggest joins explicitly in the prompt (avoids invalid joins).
6. **Result verification**: post-run check that validates row counts and sanity (e.g., “top N”, negative values) before showing.
7. **Explainable SQL**: optional assistant response with reasoning when “Show SQL” is enabled.
8. **Conversation summary memory**: compress memory into a short summary to avoid prompt bloat while keeping context.
