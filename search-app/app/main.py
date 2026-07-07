from __future__ import annotations

import logging
import os
import json
import mimetypes
from time import perf_counter
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from collections import defaultdict, deque
from tempfile import NamedTemporaryFile
from datetime import datetime, timezone

from fastapi import FastAPI, File, UploadFile, Request, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn

from .auth import SessionOrBasicAuthMiddleware
from .config import settings
from .db import init_db_with_retry, get_conn
from .store import ensure_dirs, ingest_file_path, save_upload
from .object_storage import default_object_bucket, get_object_store, resolve_object_provider
from .search import semantic_search, fulltext_search, hybrid_search, rag, image_search
from .embeddings import get_model, embed_texts
from .session import get_current_user, sign_session, set_session_cookie_headers, clear_session_cookie_headers, generate_session_id
from .users import create_user, authenticate_user, list_spaces, get_default_space_id, create_space, set_default_space, get_user_by_id
from .vision_embeddings import embed_image_paths, embed_image_texts, VisionModelUnavailable
from .oci_llm import oci_chat_completion
from .deep_research import start_conversation as dr_start, ask as dr_ask
from .deep_research_store import (
    list_conversations as dr_list_conversations,
    get_conversation_detail as dr_get_conversation_detail,
    update_conversation_title as dr_update_conversation_title,
    add_notebook_entry as dr_add_notebook_entry,
    delete_notebook_entry as dr_delete_notebook_entry,
)
from .memory_store import (
    _fetch_persistent_memory,
    _build_persistent_memory_context,
    _persist_memory_event,
)

logger = logging.getLogger("searchapp")
logging.basicConfig(level=os.getenv("LOGLEVEL", "INFO"))

SQL_MEMORY_CACHE: Dict[str, deque[Dict[str, str]]] = defaultdict(deque)


BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Enterprise Search App", version="0.2.0")
# Protect API with session or basic auth; UI root is public (renders login when unauthenticated)
app.add_middleware(SessionOrBasicAuthMiddleware, protect_paths=("/api", "/docs", "/openapi.json", "/redoc"))

if settings.allow_cors:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Static and templates
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@app.on_event("startup")
def on_startup():
    ensure_dirs()
    init_db_with_retry()
    _migrate_object_metadata()
    # Preload embeddings model to avoid first-search latency
    try:
        get_model()
        logger.info("Embeddings model preloaded")
    except Exception as e:
        logger.exception("Failed to preload embeddings model: %s", e)
    logger.info("Startup complete: directories ensured and database initialized")


# UI route (minimalist, responsive search app)
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "upload_max_mb": settings.max_upload_size_mb,
            "upload_max_files": settings.max_files_per_space,
            "upload_allowed_exts": settings.allowed_upload_extensions,
            "sql_max_rows": settings.sql_max_rows,
            "sql_default_rows": settings.sql_default_rows,
            "sql_memory_turns": settings.sql_memory_turns,
            "sql_agentic_mode_default": settings.sql_agentic_mode_default,
            "sql_persistent_memory_enabled": settings.sql_persistent_memory_enabled,
            "text_persistent_memory_enabled": settings.text_persistent_memory_enabled,
            "image_persistent_memory_enabled": settings.image_persistent_memory_enabled,
            "deep_research_persistent_memory_enabled": settings.deep_research_persistent_memory_enabled,
            "sql_system_prompt": settings.sql_system_prompt,
        },
    )


def _json_dumps(value: Any) -> str:
    try:
        return json.dumps(value, default=str)
    except Exception:
        return json.dumps({})


def _get_client_ip(request: Request) -> Optional[str]:
    forwarded = request.headers.get("x-forwarded-for") or ""
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return request.client.host if request.client else None


def _get_user_agent(request: Request) -> Optional[str]:
    return request.headers.get("user-agent")


def _get_account_prefix(user_id: int) -> str:
    try:
        user = get_user_by_id(user_id)
    except Exception:
        user = None
    email = (user or {}).get("email") or ""
    account_prefix = (email.split("@", 1)[0] if "@" in email else email).strip().lower()
    return account_prefix or f"user{user_id}"


def _build_session_name(*, account_prefix: str, space_id: int, session_seq: int) -> str:
    return f"{account_prefix}-{space_id}-{session_seq}"


def _upsert_search_session(
    *,
    session_id: str,
    user_id: int,
    space_id: Optional[int],
    name: Optional[str],
    client_ip: Optional[str],
    user_agent: Optional[str],
) -> tuple[int, Optional[str]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO search_sessions
                    (session_id, user_id, space_id, name, last_ip, last_user_agent, first_activity_at, last_activity_at)
                VALUES (%s, %s, %s, %s, %s, %s, now(), now())
                ON CONFLICT (session_id) DO UPDATE
                SET last_activity_at = EXCLUDED.last_activity_at,
                    space_id = COALESCE(EXCLUDED.space_id, search_sessions.space_id),
                    name = COALESCE(search_sessions.name, EXCLUDED.name),
                    last_ip = COALESCE(EXCLUDED.last_ip, search_sessions.last_ip),
                    last_user_agent = COALESCE(EXCLUDED.last_user_agent, search_sessions.last_user_agent)
                RETURNING id, name
                """,
                (session_id, user_id, space_id, name, client_ip, user_agent),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError("search_session_upsert_failed")
            return int(row[0]), row[1]


def _ensure_session_name(
    *,
    session_id: str,
    user_id: int,
    space_id: int,
    session_seq: int,
    current_name: Optional[str],
) -> str:
    if current_name:
        return current_name
    account_prefix = _get_account_prefix(user_id)
    final_name = _build_session_name(account_prefix=account_prefix, space_id=space_id, session_seq=session_seq)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE search_sessions SET name = %s WHERE session_id = %s AND name IS NULL",
                (final_name, session_id),
            )
    return final_name


def _log_search_activity(
    *,
    user: dict,
    space_id: Optional[int],
    activity_type: str,
    request_payload: dict,
    response_payload: dict,
    summary: str,
    session_name: Optional[str] = None,
    request: Optional[Request] = None,
) -> None:
    session_id = user.get("session_id") or user.get("sid")
    if not session_id:
        return
    uid = int(user.get("user_id") or user.get("id"))
    try:
        client_ip = _get_client_ip(request) if request else None
        user_agent = _get_user_agent(request) if request else None
        session_space_id = space_id
        if session_space_id is None:
            session_space_id = get_default_space_id(uid)
        if session_space_id is None:
            raise ValueError("default_space_missing")
        session_seq, current_name = _upsert_search_session(
            session_id=session_id,
            user_id=uid,
            space_id=session_space_id,
            name=session_name,
            client_ip=client_ip,
            user_agent=user_agent,
        )
        _ensure_session_name(
            session_id=session_id,
            user_id=uid,
            space_id=session_space_id,
            session_seq=session_seq,
            current_name=current_name,
        )
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO search_activity
                        (session_id, user_id, space_id, activity_type, request_payload, response_payload, summary, client_ip, user_agent)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)
                    """,
                    (
                        session_id,
                        uid,
                        session_space_id,
                        activity_type,
                        _json_dumps(request_payload or {}),
                        _json_dumps(response_payload or {}),
                        summary or "",
                        client_ip,
                        user_agent,
                    ),
                )
    except Exception:
        logger.exception("Failed to log search activity")


# API routes
@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/db-info")
def db_info():
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database(), current_user, inet_server_addr()::text, inet_server_port()")
                db, user, host, port = cur.fetchone()
        return {"database": db, "user": user, "host": host, "port": port}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/ready")
def ready():
    checks = {"extensions": False, "users": False, "spaces": False, "documents_table": False, "chunks_table": False, "tsv_index": False, "vec_index": False}
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database()")
                checks["database"] = cur.fetchone()[0]
                cur.execute("SELECT 1 FROM pg_extension WHERE extname IN ('vector','pgcrypto','citext')")
                checks["extensions"] = len(cur.fetchall()) >= 3
                for tbl, key in [("users","users"),("spaces","spaces"),("documents","documents_table"),("chunks","chunks_table")]:
                    cur.execute(f"SELECT to_regclass('public.{tbl}') IS NOT NULL")
                    checks[key] = bool(cur.fetchone()[0])
                cur.execute("SELECT to_regclass('public.idx_chunks_tsv') IS NOT NULL")
                checks["tsv_index"] = bool(cur.fetchone()[0])
                cur.execute("SELECT to_regclass('public.idx_chunks_embedding_ivfflat') IS NOT NULL")
                checks["vec_index"] = bool(cur.fetchone()[0])
        ready_status = all(val for key, val in checks.items() if key != "database")
        return {"ready": ready_status, **checks}
    except Exception as e:
        return {"ready": False, "error": str(e), **checks}


@app.get("/api/chunks-preview")
def chunks_preview(request: Request, doc_id: int, limit: int = 20):
    user = get_current_user_sync(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    uid = int(user.get("user_id") or user.get("id"))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, document_id, chunk_index, content_chars, LEFT(content, 600)
                FROM chunks
                WHERE document_id = %s
                  AND document_id IN (SELECT id FROM documents WHERE user_id = %s)
                ORDER BY chunk_index ASC
                LIMIT %s
                """,
                (doc_id, uid, limit),
            )
            rows = cur.fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append({
            "chunk_id": int(r[0]),
            "document_id": int(r[1]),
            "chunk_index": int(r[2]),
            "content_chars": int(r[3]) if r[3] is not None else None,
            "snippet": r[4] or "",
        })
    return out


@app.get("/api/doc-summary")
def doc_summary(request: Request, doc_id: int):
    user = get_current_user_sync(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    uid = int(user.get("user_id") or user.get("id"))
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, source_path, source_type, COALESCE(title, '') FROM documents WHERE id = %s AND user_id = %s",
                    (doc_id, uid),
                )
                doc = cur.fetchone()
                if not doc:
                    return JSONResponse(status_code=404, content={"error": "document not found"})
                cur.execute("SELECT count(*) FROM chunks WHERE document_id = %s", (doc_id,))
                cnt = int(cur.fetchone()[0])
        return {
            "document_id": int(doc[0]),
            "file_name": (doc[1] or "").rsplit("/", 1)[-1] if doc[1] else "",
            "source_path": doc[1] or "",
            "source_type": doc[2] or "",
            "title": doc[3] or "",
            "chunk_count": cnt,
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/upload")
async def upload(request: Request, files: List[UploadFile] = File(...), space_id: int | None = Form(None)):
    user = await get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    uid = int(user.get("user_id") or user.get("id"))
    uemail = user.get("email")
    sid = int(space_id) if space_id is not None else get_default_space_id(uid)
    allowed_exts = _allowed_upload_extensions()
    try:
        _enforce_space_upload_limit(uid, sid, len(files))
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    results: List[Dict[str, Any]] = []
    for f in files:
        if not _is_allowed_filename(f.filename or "", allowed_exts):
            results.append(
                {
                    "filename": f.filename or "",
                    "error": "unsupported file type",
                    "status": "error",
                }
            )
            continue
        data = await f.read()
        if len(data) > settings.max_upload_size_mb * 1024 * 1024:
            results.append(
                {
                    "filename": f.filename or "",
                    "error": f"file too large (> {settings.max_upload_size_mb} MB)",
                    "status": "error",
                }
            )
            continue
        local_path, obj_provider, obj_bucket, obj_name = save_upload(data, Path(f.filename).name, user_email=uemail)
        # Use basename as title and include original filename and optional object URL in metadata
        title = Path(f.filename).name
        title_no_ext = Path(title).stem
        logger.info("Upload stored: backend=%s local=%s object_name=%s", settings.storage_backend, local_path, obj_name or "")
        try:
            meta = {"filename": title}
            ing = ingest_file_path(
                local_path,
                user_id=uid,
                space_id=sid,
                title=title_no_ext,
                metadata=meta,
                object_provider=obj_provider,
                object_bucket=obj_bucket,
                object_name=obj_name,
            )
            logger.info(
                "Upload ingested: file=%s doc_id=%s chunks=%s user_id=%s space_id=%s",
                title,
                ing.document_id,
                ing.num_chunks,
                uid,
                sid,
            )
            results.append({
                "filename": title,
                "title": title_no_ext,
                "document_id": ing.document_id,
                "chunks": ing.num_chunks,
                "object_provider": obj_provider,
                "object_bucket": obj_bucket,
                "object_name": obj_name,
                "status": "ok",
            })
        except Exception as e:
            results.append({
                "filename": title,
                "title": title_no_ext,
                "error": str(e),
                "status": "error",
            })
        finally:
            if settings.delete_uploaded_after_ingest:
                try:
                    os.remove(local_path)
                except Exception:
                    pass
    return {"results": results}


@app.post("/api/search")
async def api_search(request: Request, payload: Dict[str, Any]):
    user = await get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    uid = int(user.get("user_id") or user.get("id"))
    sid = payload.get("space_id")
    sid = int(sid) if sid is not None else get_default_space_id(uid)
    q = payload.get("query", "")
    mode = str(payload.get("mode", "hybrid")).lower()
    top_k = int(payload.get("top_k", 25))
    persistent_memory_requested = _extract_bool(payload.get("persistent_memory"))
    persistent_memory_enabled = bool(settings.text_persistent_memory_enabled and persistent_memory_requested)
    if not q:
        return JSONResponse(status_code=400, content={"error": "query required"})

    answer: str | None = None
    used_llm: bool = False
    timings: Dict[str, Optional[int]] = {"db_ms": None, "llm_ms": None}
    if mode == "semantic":
        db_start = perf_counter()
        hits = semantic_search(q, top_k=top_k, user_id=uid, space_id=sid)
        timings["db_ms"] = int(round((perf_counter() - db_start) * 1000))
    elif mode == "fulltext":
        db_start = perf_counter()
        hits = fulltext_search(q, top_k=top_k, user_id=uid, space_id=sid)
        timings["db_ms"] = int(round((perf_counter() - db_start) * 1000))
    elif mode == "rag":
        memory_context = ""
        if persistent_memory_enabled:
            entries = _fetch_persistent_memory(sid, "text", q)
            memory_context = _build_persistent_memory_context(entries, int(settings.persistent_memory_max_chars))
        answer, hits, used_llm, timings = rag(
            q,
            mode="hybrid",
            top_k=top_k,
            user_id=uid,
            space_id=sid,
            memory_context=memory_context,
            return_timings=True,
        )
    else:
        db_start = perf_counter()
        hits = hybrid_search(q, top_k=top_k, user_id=uid, space_id=sid)
        timings["db_ms"] = int(round((perf_counter() - db_start) * 1000))

    # Enrich with document metadata (source_path, title)
    doc_ids = sorted({h.document_id for h in hits})
    doc_info: Dict[int, Dict[str, Any]] = {}
    if doc_ids:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, source_path, source_type, COALESCE(title, ''), metadata FROM documents WHERE id = ANY(%s) AND user_id = %s",
                    (doc_ids, uid),
                )
                for row in cur.fetchall():
                    # row: id, source_path, source_type, title, metadata
                    sp = row[1] or ""
                    fn = sp.rsplit("/", 1)[-1] if sp else ""
                    doc_info[int(row[0])] = {"source_path": sp, "file_name": fn, "file_type": row[2] or "", "title": row[3]}

    hits_out = []
    for h in hits:
        entry = {
            "chunk_id": h.chunk_id,
            "document_id": h.document_id,
            "chunk_index": h.chunk_index,
            "content": h.content,
            "distance": h.distance,
            "rank": h.rank,
        }
        meta = doc_info.get(h.document_id)
        if meta:
            # Do not expose full source_path to UI; include file_name and file_type
            entry["file_name"] = meta.get("file_name", "")
            entry["file_type"] = meta.get("file_type", "")
            entry["title"] = meta.get("title", "")
        hits_out.append(entry)

    out: Dict[str, Any] = {
        "mode": mode if mode in {"semantic", "fulltext", "rag"} else "hybrid",
        "hits": hits_out,
        "timings": timings,
    }
    if answer is not None:
        out["answer"] = answer
        out["used_llm"] = bool(used_llm)
        out["llm_response"] = answer if used_llm else ""
        # Include top references for UI (file name/type and chunk anchor)
        refs = []
        for e in hits_out[: min(len(hits_out), 5)]:
            doc_id = e.get("document_id")
            refs.append({
                "file_name": e.get("file_name") or e.get("title") or "",
                "file_type": e.get("file_type") or "",
                "chunk_id": e.get("chunk_id"),
                "href": f"#chunk-{e.get('chunk_id')}",
                "url": f"/api/doc-download?doc_id={doc_id}" if doc_id is not None else None,
            })
        out["references"] = refs
    if persistent_memory_enabled:
        response_text = answer or ""
        if not response_text:
            snippets = [h.content[:400] for h in hits[:3] if h.content]
            response_text = "\n\n".join(snippets).strip()
        result_sample = [
            {"chunk_id": h.chunk_id, "document_id": h.document_id, "content": (h.content or "")[:400]}
            for h in hits[:3]
        ]
        memory_event_id = _persist_memory_event(
            space_id=sid,
            user_id=uid,
            memory_type="text",
            query_text=q,
            response_text=response_text,
            result_sample=result_sample,
            metadata={"mode": mode, "top_k": top_k},
        )
        out["memory_event_id"] = memory_event_id
    summary = f"{mode.upper()} · {q[:120]}" if q else f"{mode.upper()}"
    session_name = (q or "").strip()[:80] or None
    request_payload = {
        "query": q,
        "mode": mode,
        "top_k": top_k,
        "space_id": sid,
        "persistent_memory": persistent_memory_requested,
        "user": {"id": uid, "email": user.get("email"), "role": user.get("role")},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _log_search_activity(
        user=user,
        space_id=sid,
        activity_type=f"text_{mode}",
        request_payload=request_payload,
        response_payload=out,
        summary=summary,
        session_name=session_name,
        request=request,
    )
    return out


@app.post("/api/image-search")
async def api_image_search(request: Request):
    user = await get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    uid = int(user.get("user_id") or user.get("id"))
    payload: Dict[str, Any] = {}
    reference_file: UploadFile | None = None
    content_type = request.headers.get("content-type", "")

    if "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        for key, value in form.multi_items():
            if isinstance(value, UploadFile) and key == "reference":
                reference_file = value
            else:
                payload[key] = value
    else:
        try:
            body = await request.json()
            if isinstance(body, dict):
                payload = body
        except Exception:
            payload = {}

    sid = payload.get("space_id")
    sid = int(sid) if sid is not None else get_default_space_id(uid)
    query = _extract_query_text(payload.get("query"))
    tag_filter = _extract_tags(payload.get("tags"))
    top_k = int(payload.get("top_k") or 12)
    vector = _extract_vector(payload.get("vector"))
    persistent_memory_requested = _extract_bool(payload.get("persistent_memory"))
    persistent_memory_enabled = bool(settings.image_persistent_memory_enabled and persistent_memory_requested)

    temp_file_path: str | None = None
    try:
        if reference_file is not None:
            suffix = Path(reference_file.filename or "").suffix.lower() or ".img"
            with NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                data = await reference_file.read()
                tmp.write(data)
                temp_file_path = tmp.name
            try:
                vectors = embed_image_paths([temp_file_path])
                vector = vectors[0] if vectors else None
            except VisionModelUnavailable as e:
                return JSONResponse(status_code=503, content={"error": "vision model unavailable", "detail": str(e)})
            except Exception as e:
                return JSONResponse(status_code=400, content={"error": "failed to process reference image", "detail": str(e)})

        if vector is None and query:
            try:
                vecs = embed_image_texts([query])
                vector = vecs[0] if vecs else None
            except VisionModelUnavailable as e:
                return JSONResponse(status_code=503, content={"error": "vision model unavailable", "detail": str(e)})
            except Exception:
                vector = None

        if vector is None and query:
            logger.warning("Image search: semantic embedding unavailable, falling back to text-only search")
    finally:
        if temp_file_path:
            try:
                os.remove(temp_file_path)
            except OSError:
                pass

    if vector is None and not query and not tag_filter:
        return JSONResponse(status_code=400, content={"error": "provide query, tags, or vector"})

    hits = image_search(query=query, vector=vector, top_k=top_k, user_id=uid, space_id=sid, tags=tag_filter)
    results: List[Dict[str, Any]] = []
    for idx, h in enumerate(hits, start=1):
        src = h.get("_source", h)
        results.append(
            {
                "rank": idx,
                "doc_id": src.get("doc_id"),
                "image_id": src.get("image_id"),
                "thumbnail_path": src.get("thumbnail_path"),
                "file_path": src.get("file_path"),
                "caption": src.get("caption"),
                "ocr_text": src.get("ocr_text"),
                "tags": src.get("tags", []),
                "score": h.get("_score"),
            }
        )
    doc_meta_map: Dict[int, Dict[str, Any]] = {}
    doc_ids = sorted({int(r["doc_id"]) for r in results if r.get("doc_id")})
    if doc_ids:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, COALESCE(metadata,'{}'::jsonb) FROM documents WHERE id = ANY(%s)",
                    (doc_ids,),
                )
                doc_meta_map = {int(row[0]): (row[1] or {}) for row in cur.fetchall()}

    for item in results:
        doc_id = item.get("doc_id")
        image_id = item.get("image_id")
        meta = doc_meta_map.get(int(doc_id)) if doc_id else {}
        item["thumbnail_url"] = f"/api/image-assets/{image_id}/thumbnail" if image_id else None
        if isinstance(meta, dict):
            item["thumbnail_object_url"] = None
            item["object_url"] = None
        else:
            item["thumbnail_object_url"] = None
            item["object_url"] = None
        item["file_url"] = f"/api/doc-download?doc_id={doc_id}" if doc_id else None

    response: Dict[str, Any] = {"results": results, "count": len(results)}
    if persistent_memory_enabled:
        query_parts = [query] if query else []
        if tag_filter:
            query_parts.append("tags: " + ", ".join(tag_filter))
        query_text = " | ".join([p for p in query_parts if p]).strip()
        response_text = "\n".join([r.get("caption") or "" for r in results[:3]]).strip()
        result_sample = [
            {
                "image_id": r.get("image_id"),
                "doc_id": r.get("doc_id"),
                "caption": r.get("caption"),
                "score": r.get("score"),
            }
            for r in results[:3]
        ]
        memory_event_id = _persist_memory_event(
            space_id=sid,
            user_id=uid,
            memory_type="image",
            query_text=query_text,
            response_text=response_text,
            result_sample=result_sample,
            metadata={"top_k": top_k, "tags": tag_filter},
        )
        response["memory_event_id"] = memory_event_id
    summary_parts = []
    if query:
        summary_parts.append(query)
    if tag_filter:
        summary_parts.append("tags: " + ", ".join(tag_filter[:4]))
    if reference_file is not None:
        summary_parts.append("reference image")
    summary = "Image · " + " · ".join(summary_parts) if summary_parts else "Image search"
    session_name = (query or "").strip()[:80] or ("Image search" if reference_file or tag_filter else None)
    request_payload = {
        "query": query,
        "tags": tag_filter,
        "top_k": top_k,
        "space_id": sid,
        "persistent_memory": persistent_memory_requested,
        "reference_image": bool(reference_file is not None),
        "user": {"id": uid, "email": user.get("email"), "role": user.get("role")},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _log_search_activity(
        user=user,
        space_id=sid,
        activity_type="image_search",
        request_payload=request_payload,
        response_payload=response,
        summary=summary,
        session_name=session_name,
        request=request,
    )
    return response


@app.get("/api/image-assets/{image_id}/thumbnail")
async def api_image_thumbnail(request: Request, image_id: int):
    user = await get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    uid = int(user.get("user_id") or user.get("id"))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ia.thumbnail_path, ia.document_id, d.user_id, d.object_provider, d.object_bucket
                FROM image_assets ia
                JOIN documents d ON d.id = ia.document_id
                WHERE ia.id = %s
                """,
                (int(image_id),),
            )
            row = cur.fetchone()
    if not row:
        return JSONResponse(status_code=404, content={"error": "not found"})
    thumb_rel, _doc_id, owner_id, obj_provider, obj_bucket = row
    if int(owner_id) != uid:
        return JSONResponse(status_code=404, content={"error": "not found"})
    path = _resolve_asset_path(thumb_rel)
    if path and path.exists():
        media_type = mimetypes.guess_type(str(path))[0] or "image/jpeg"
        return FileResponse(str(path), media_type=media_type)
    if obj_provider and obj_bucket and thumb_rel:
        try:
            store = get_object_store(obj_provider)
            if store:
                stream, _length, content_type = store.get_object_stream(obj_bucket, str(thumb_rel).replace("\\", "/"))
                return StreamingResponse(stream, media_type=content_type or "image/jpeg")
        except Exception:
            logger.exception("Failed to stream thumbnail from object storage (image path): %s", thumb_rel)
    return JSONResponse(status_code=404, content={"error": "thumbnail unavailable"})


@app.get("/api/image-assets/{image_id}")
async def api_image_asset(request: Request, image_id: int):
    user = await get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    uid = int(user.get("user_id") or user.get("id"))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ia.file_path, ia.document_id, d.user_id, d.object_provider, d.object_bucket
                FROM image_assets ia
                JOIN documents d ON d.id = ia.document_id
                WHERE ia.id = %s
                """,
                (int(image_id),),
            )
            row = cur.fetchone()
    if not row:
        return JSONResponse(status_code=404, content={"error": "not found"})
    file_rel, _doc_id, owner_id, obj_provider, obj_bucket = row
    if int(owner_id) != uid:
        return JSONResponse(status_code=404, content={"error": "not found"})
    path = _resolve_asset_path(file_rel)
    if path and path.exists():
        media_type = mimetypes.guess_type(str(path))[0] or "image/jpeg"
        return FileResponse(str(path), media_type=media_type)
    if obj_provider and obj_bucket and file_rel:
        try:
            store = get_object_store(obj_provider)
            if store:
                stream, _length, content_type = store.get_object_stream(obj_bucket, str(file_rel).replace("\\", "/"))
                return StreamingResponse(stream, media_type=content_type or "image/jpeg")
        except Exception:
            logger.exception("Failed to stream image asset from object storage (image path): %s", file_rel)
    return JSONResponse(status_code=404, content={"error": "image unavailable"})


@app.get("/api/doc-download")
async def api_doc_download(request: Request, doc_id: int):
    user = await get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    uid = int(user.get("user_id") or user.get("id"))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source_path, object_provider, object_bucket, object_name FROM documents WHERE id = %s AND user_id = %s",
                (int(doc_id), uid),
            )
            row = cur.fetchone()
            if not row:
                return JSONResponse(status_code=404, content={"error": "document not found"})
            path = row[0] or ""
            obj_provider = row[1]
            obj_bucket = row[2]
            obj_name = row[3]
    p = Path(path)
    if p.exists():
        return FileResponse(str(p), media_type="application/octet-stream", filename=p.name)
    if obj_provider and obj_bucket and obj_name:
        try:
            store = get_object_store(obj_provider)
            if store:
                stream, _length, content_type = store.get_object_stream(obj_bucket, obj_name)
                headers = {"Content-Disposition": f"attachment; filename=\"{Path(obj_name).name}\""}
                return StreamingResponse(stream, media_type=content_type or "application/octet-stream", headers=headers)
        except Exception:
            logger.exception("Failed to stream document from object storage: %s", obj_name)
    return JSONResponse(status_code=404, content={"error": "file not found"})


@app.get("/api/doc-thumbnail")
async def api_doc_thumbnail(request: Request, doc_id: int):
    user = await get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    uid = int(user.get("user_id") or user.get("id"))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT metadata, object_provider, object_bucket, thumbnail_object_name FROM documents WHERE id = %s AND user_id = %s",
                (int(doc_id), uid),
            )
            row = cur.fetchone()
            if not row:
                return JSONResponse(status_code=404, content={"error": "document not found"})
            metadata = row[0] or {}
            obj_provider = row[1]
            obj_bucket = row[2]
            thumb_object_name = row[3]
    thumb_rel = metadata.get("thumbnail_path") if isinstance(metadata, dict) else None
    if thumb_rel:
        path = _resolve_asset_path(thumb_rel)
        if path and path.exists():
            media_type = mimetypes.guess_type(str(path))[0] or "image/jpeg"
            return FileResponse(str(path), media_type=media_type)
    if obj_provider and obj_bucket and thumb_object_name:
        try:
            store = get_object_store(obj_provider)
            if store:
                stream, _length, content_type = store.get_object_stream(obj_bucket, thumb_object_name)
                return StreamingResponse(stream, media_type=content_type or "image/jpeg")
        except Exception:
            logger.exception("Failed to stream document thumbnail from object storage: %s", thumb_object_name)
    return JSONResponse(status_code=404, content={"error": "thumbnail unavailable"})


@app.get("/api/kb")
async def api_kb(
    request: Request,
    limit: int = 25,
    offset: int = 0,
    space_id: int | None = None,
    sort: str = "newest",
):
    user = await get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    uid = int(user.get("user_id") or user.get("id"))
    items: List[Dict[str, Any]] = []
    total = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            params: List[Any] = [uid]
            space_clause = ""
            if space_id is not None:
                space_clause = "AND d.space_id = %s"
                params.append(int(space_id))
            cur.execute(
                f"SELECT COUNT(*) FROM documents d WHERE d.user_id = %s {space_clause}",
                params,
            )
            total = int(cur.fetchone()[0])
            params.extend([int(limit), int(offset)])
            order_clause = "d.created_at DESC"
            sort_key = (sort or "").strip().lower()
            if sort_key in {"oldest", "asc"}:
                order_clause = "d.created_at ASC"
            elif sort_key in {"az", "title", "alpha"}:
                order_clause = "COALESCE(d.title, d.source_path) ASC"
            elif sort_key in {"za", "title_desc", "alpha_desc"}:
                order_clause = "COALESCE(d.title, d.source_path) DESC"

            cur.execute(
                f"""
                SELECT d.id, d.source_path, d.source_type, COALESCE(d.title,''), d.created_at, COALESCE(d.metadata,'{{}}'::jsonb)
                FROM documents d
                WHERE d.user_id = %s {space_clause}
                ORDER BY {order_clause}
                LIMIT %s OFFSET %s
                """,
                params,
            )
            rows = cur.fetchall()
            doc_ids = [int(r[0]) for r in rows]
            chunk_counts: Dict[int, int] = {}
            image_map: Dict[int, List[Dict[str, Any]]] = {}
            if doc_ids:
                cur.execute(
                    "SELECT document_id, count(*) FROM chunks WHERE document_id = ANY(%s) GROUP BY document_id",
                    (doc_ids,),
                )
                chunk_counts = {int(r[0]): int(r[1]) for r in cur.fetchall()}
                cur.execute(
                    """
                    SELECT document_id, id, thumbnail_path, file_path, width, height, caption, tags
                    FROM image_assets
                    WHERE document_id = ANY(%s)
                    ORDER BY created_at DESC
                    """,
                    (doc_ids,),
                )
                for row in cur.fetchall():
                    doc_key = int(row[0])
                    image_map.setdefault(doc_key, []).append(
                        {
                            "image_id": int(row[1]),
                            "thumbnail_path": row[2],
                            "file_path": row[3],
                            "width": row[4],
                            "height": row[5],
                            "caption": row[6],
                            "tags": row[7] or [],
                        }
                    )
            for r in rows:
                sp = r[1] or ""
                fn = sp.rsplit("/", 1)[-1] if sp else ""
                doc_id = int(r[0])
                metadata = r[5] or {}
                doc_images: List[Dict[str, Any]] = []
                if doc_id in image_map:
                    for img in image_map[doc_id]:
                        image_id = img.get("image_id")
                        doc_images.append(
                            {
                                **img,
                                "thumbnail_url": f"/api/image-assets/{image_id}/thumbnail" if image_id else None,
                                "file_url": f"/api/doc-download?doc_id={doc_id}",
                                "object_url": None,
                                "thumbnail_object_url": None,
                            }
                        )
                preview_url = doc_images[0].get("thumbnail_url") if doc_images else None
                items.append(
                    {
                        "id": doc_id,
                        "file_name": fn,
                        "source_path": sp,
                        "source_type": r[2] or "",
                        "title": r[3] or "",
                        "created_at": (r[4].isoformat() if r[4] else None),
                        "chunk_count": chunk_counts.get(doc_id, 0),
                        "metadata": metadata,
                        "images": doc_images,
                        "thumbnail_preview_url": preview_url,
                    }
                )
    return {"documents": items, "limit": int(limit), "offset": int(offset), "total": int(total)}


def _delete_document_by_id(uid: int, doc_id: int) -> dict:
    image_rows: List[tuple[str | None, str | None]] = []
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source_path, object_provider, object_bucket, object_name, thumbnail_object_name FROM documents WHERE id = %s AND user_id = %s",
                (int(doc_id), uid),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError("not_found")
            source_path = row[0] or ""
            obj_provider = row[1]
            obj_bucket = row[2]
            obj_name = row[3]
            thumb_object_name = row[4]
            cur.execute(
                "SELECT file_path, thumbnail_path FROM image_assets WHERE document_id = %s",
                (int(doc_id),),
            )
            image_rows = cur.fetchall() or []
            cur.execute("DELETE FROM documents WHERE id = %s AND user_id = %s", (int(doc_id), uid))

    # Best-effort cleanup of local assets (source file + thumbnails)
    try:
        if source_path:
            src_path = Path(source_path)
            if src_path.exists():
                src_path.unlink()
    except Exception:
        logger.warning("Failed to delete source file for doc_id=%s", doc_id)

    if settings.storage_backend in {"oci", "s3", "both"}:
        try:
            if thumb_object_name and obj_provider and obj_bucket:
                store = get_object_store(obj_provider)
                if store:
                    store.delete_object(obj_bucket, thumb_object_name)
        except Exception:
            logger.warning("Failed to delete thumbnail object for doc_id=%s", doc_id)

        try:
            if obj_name and obj_provider and obj_bucket:
                store = get_object_store(obj_provider)
                if store:
                    store.delete_object(obj_bucket, obj_name)
        except Exception:
            logger.warning("Failed to delete object storage source for doc_id=%s", doc_id)

        try:
            if obj_provider and obj_bucket:
                store = get_object_store(obj_provider)
                if store:
                    for file_path, thumb_path in image_rows:
                        if file_path:
                            store.delete_object(obj_bucket, str(file_path).lstrip("/"))
                        if thumb_path:
                            store.delete_object(obj_bucket, str(thumb_path).lstrip("/"))
        except Exception:
            logger.warning("Failed to delete image assets from object storage for doc_id=%s", doc_id)

    # Best-effort cleanup of local image assets
    try:
        for file_path, thumb_path in image_rows:
            for rel in (file_path, thumb_path):
                asset_path = _resolve_asset_path(rel)
                if asset_path and asset_path.exists():
                    asset_path.unlink()
    except Exception:
        logger.warning("Failed to delete local image assets for doc_id=%s", doc_id)

    return {"ok": True, "document_id": int(doc_id)}


@app.delete("/api/documents/{doc_id}")
async def api_delete_document(request: Request, doc_id: int):
    user = await get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    uid = int(user.get("user_id") or user.get("id"))
    try:
        return _delete_document_by_id(uid, doc_id)
    except ValueError:
        return JSONResponse(status_code=404, content={"error": "not found"})


@app.post("/api/documents/{doc_id}/delete")
async def api_delete_document_post(request: Request, doc_id: int):
    user = await get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    uid = int(user.get("user_id") or user.get("id"))
    try:
        return _delete_document_by_id(uid, doc_id)
    except ValueError:
        return JSONResponse(status_code=404, content={"error": "not found"})


@app.get("/api/me")
async def api_me(request: Request):
    user = await get_current_user(request)
    if not user:
        return {"user": None}
    uid = int(user.get("user_id") or user.get("id"))
    return {"user": {"id": uid, "email": user.get("email"), "role": user.get("role") or "user"}, "spaces": list_spaces(uid)}


@app.post("/api/register")
async def api_register(request: Request, payload: Dict[str, Any]):
    if not settings.allow_registration:
        return JSONResponse(status_code=403, content={"error": "registration disabled"})
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""
    if not email or not password:
        return JSONResponse(status_code=400, content={"error": "email and password required"})
    try:
        u = create_user(email, password)
        session_id = generate_session_id()
        token = sign_session({"user_id": u["id"], "email": email, "role": u.get("role") or "user", "sid": session_id})
        headers = set_session_cookie_headers(token)
        default_space_id = get_default_space_id(u["id"])
        if default_space_id is None:
            raise ValueError("default_space_missing")
        session_seq, current_name = _upsert_search_session(
            session_id=session_id,
            user_id=u["id"],
            space_id=default_space_id,
            name=None,
            client_ip=_get_client_ip(request),
            user_agent=_get_user_agent(request),
        )
        _ensure_session_name(
            session_id=session_id,
            user_id=u["id"],
            space_id=default_space_id,
            session_seq=session_seq,
            current_name=current_name,
        )
        spaces = list_spaces(u["id"]) or []
        return JSONResponse(
            status_code=200,
            content={"user": {"id": u["id"], "email": email, "role": u.get("role") or "user"}, "spaces": spaces},
            headers=headers,
        )
    except Exception as e:
        msg = str(e) or ""
        low = msg.lower()
        if "duplicate" in low or "unique" in low:
            return JSONResponse(status_code=409, content={"error": "email already registered"})
        return JSONResponse(status_code=400, content={"error": msg})


@app.post("/api/login")
async def api_login(request: Request, payload: Dict[str, Any]):
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""
    if not email or not password:
        return JSONResponse(status_code=400, content={"error": "email and password required"})
    u = authenticate_user(email, password)
    if not u:
        return JSONResponse(status_code=401, content={"error": "invalid credentials"})
    session_id = generate_session_id()
    token = sign_session({"user_id": u["id"], "email": email, "role": u.get("role") or "user", "sid": session_id})
    headers = set_session_cookie_headers(token)
    default_space_id = get_default_space_id(u["id"])
    if default_space_id is None:
        raise ValueError("default_space_missing")
    session_seq, current_name = _upsert_search_session(
        session_id=session_id,
        user_id=u["id"],
        space_id=default_space_id,
        name=None,
        client_ip=_get_client_ip(request),
        user_agent=_get_user_agent(request),
    )
    _ensure_session_name(
        session_id=session_id,
        user_id=u["id"],
        space_id=default_space_id,
        session_seq=session_seq,
        current_name=current_name,
    )
    spaces = list_spaces(u["id"]) or []
    return JSONResponse(
        status_code=200,
        content={"user": {"id": u["id"], "email": email, "role": u.get("role") or "user"}, "spaces": spaces},
        headers=headers,
    )


@app.get("/api/search-history")
async def api_search_history(
    request: Request,
    limit: int = 30,
    offset: int = 0,
    activity_type: Optional[str] = None,
    space_id: Optional[int] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    include_empty: bool = False,
):
    user = await get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    uid = int(user.get("user_id") or user.get("id"))
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    filters: List[str] = ["s.user_id = %s"]
    params: List[Any] = [uid]
    if not include_empty:
        filters.append(
            """
            EXISTS (
                SELECT 1
                FROM search_activity sa_any
                WHERE sa_any.session_id = s.session_id
                  AND sa_any.user_id = %s
            )
            """
        )
        params.append(uid)
    if space_id is not None:
        filters.append("s.space_id = %s")
        params.append(int(space_id))
    if activity_type:
        filters.append(
            """
            EXISTS (
                SELECT 1
                FROM search_activity sa_filter
                WHERE sa_filter.session_id = s.session_id
                  AND sa_filter.user_id = %s
                  AND sa_filter.activity_type = %s
            )
            """
        )
        params.extend([uid, activity_type])
    if since:
        filters.append("s.last_activity_at >= %s")
        params.append(since)
    if until:
        filters.append("s.last_activity_at <= %s")
        params.append(until)
    where_clause = " AND ".join(filters)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM search_sessions s WHERE " + where_clause,
                tuple(params),
            )
            total = int(cur.fetchone()[0])
            cur.execute(
                """
                SELECT s.session_id,
                       s.name,
                       s.first_activity_at,
                       s.last_activity_at,
                       s.space_id,
                       last_act.summary,
                       last_act.activity_type,
                       last_act.created_at,
                       COALESCE(activity_counts.activity_count, 0),
                       COALESCE(activity_counts.activity_types, '{}'::jsonb)
                FROM search_sessions s
                LEFT JOIN LATERAL (
                    SELECT summary, activity_type, created_at
                    FROM search_activity
                    WHERE session_id = s.session_id
                      AND user_id = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                ) last_act ON TRUE
                LEFT JOIN LATERAL (
                    SELECT COALESCE(sum(cnt), 0) AS activity_count,
                           jsonb_object_agg(activity_type, cnt) AS activity_types
                    FROM (
                        SELECT activity_type, count(*) AS cnt
                        FROM search_activity
                        WHERE session_id = s.session_id
                          AND user_id = %s
                        GROUP BY activity_type
                    ) agg
                ) activity_counts ON TRUE
                WHERE """
                + where_clause
                + """
                ORDER BY s.last_activity_at DESC
                LIMIT %s OFFSET %s
                """,
                (uid, uid, *params, limit, offset),
            )
            rows = cur.fetchall()
    sessions = []
    for r in rows:
        activity_count = int(r[8] or 0)
        activity_map = r[9] or {}
        if not isinstance(activity_map, dict):
            activity_map = {}
        sessions.append(
            {
                "session_id": r[0],
                "name": r[1] or "",
                "first_activity_at": r[2].isoformat() if r[2] else None,
                "last_activity_at": r[3].isoformat() if r[3] else None,
                "space_id": r[4],
                "last_summary": r[5] or "",
                "last_activity_type": r[6] or "",
                "last_activity_at": r[7].isoformat() if r[7] else (r[3].isoformat() if r[3] else None),
                "activity_count": activity_count,
                "activity_types": activity_map,
            }
        )
    return {
        "sessions": sessions,
        "total": total,
        "limit": limit,
        "offset": offset,
        "filters": {
            "activity_type": activity_type or "",
            "space_id": space_id,
            "since": since,
            "until": until,
            "include_empty": bool(include_empty),
        },
    }


@app.get("/api/search-history/{session_id}")
async def api_search_history_detail(
    request: Request,
    session_id: str,
    limit: int = 50,
    offset: int = 0,
    activity_type: Optional[str] = None,
):
    user = await get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    uid = int(user.get("user_id") or user.get("id"))
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT session_id, name, first_activity_at, last_activity_at, space_id, last_ip, last_user_agent
                FROM search_sessions
                WHERE session_id = %s AND user_id = %s
                """,
                (session_id, uid),
            )
            session_row = cur.fetchone()
            if not session_row:
                return JSONResponse(status_code=404, content={"error": "session_not_found"})
            activity_filters = ["session_id = %s", "user_id = %s"]
            params: List[Any] = [session_id, uid]
            if activity_type:
                activity_filters.append("activity_type = %s")
                params.append(activity_type)
            where_clause = " AND ".join(activity_filters)
            cur.execute(
                "SELECT COUNT(*) FROM search_activity WHERE " + where_clause,
                tuple(params),
            )
            total = int(cur.fetchone()[0])
            cur.execute(
                """
                SELECT id, activity_type, summary, request_payload, response_payload, created_at, client_ip, user_agent
                FROM search_activity
                WHERE """
                + where_clause
                + """
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                (*params, limit, offset),
            )
            activities = cur.fetchall()
    items = []
    for row in activities:
        items.append(
            {
                "id": int(row[0]),
                "activity_type": row[1] or "",
                "summary": row[2] or "",
                "request": row[3] or {},
                "response": row[4] or {},
                "created_at": row[5].isoformat() if row[5] else None,
                "client_ip": row[6] or "",
                "user_agent": row[7] or "",
            }
        )
    return {
        "session": {
            "session_id": session_row[0],
            "name": session_row[1] or "",
            "first_activity_at": session_row[2].isoformat() if session_row[2] else None,
            "last_activity_at": session_row[3].isoformat() if session_row[3] else None,
            "space_id": session_row[4],
            "last_ip": session_row[5] or "",
            "last_user_agent": session_row[6] or "",
        },
        "activities": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": (offset + len(items)) < total,
        "filters": {"activity_type": activity_type or ""},
    }


@app.post("/api/logout")
async def api_logout():
    headers = clear_session_cookie_headers()
    return JSONResponse(status_code=200, content={"ok": True}, headers=headers)


@app.get("/api/spaces")
async def api_spaces(request: Request):
    user = await get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    uid = int(user.get("user_id") or user.get("id"))
    return {"spaces": list_spaces(uid)}


@app.post("/api/spaces")
async def api_create_space(request: Request, payload: Dict[str, Any]):
    user = await get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    uid = int(user.get("user_id") or user.get("id"))
    name = (payload.get("name") or "").strip()
    if not name:
        return JSONResponse(status_code=400, content={"error": "name required"})
    sid = create_space(uid, name)
    return {"space_id": sid}


@app.post("/api/spaces/default")
async def api_set_default_space(request: Request, payload: Dict[str, Any]):
    user = await get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    uid = int(user.get("user_id") or user.get("id"))
    sid = int(payload.get("space_id"))
    set_default_space(uid, sid)
    return {"ok": True}


@app.post("/api/llm-test")
async def llm_test(payload: Dict[str, Any] | None = None):
    """
    Simple LLM connectivity test. POST a JSON body like:
    { "question": "...", "context": "..." }
    If omitted, a default question/context is used. Returns provider, ok flag, and answer text.
    """
    q = (payload or {}).get("question") if payload else None
    ctx = (payload or {}).get("context") if payload else None
    if not q:
        q = "Test connectivity. Summarize the following context in one sentence."
    if not ctx:
        ctx = "This is a test context from the /api/llm-test endpoint."

    provider = settings.llm_provider
    answer: str | None = None
    error: str | None = None
    chat_ok: bool = False
    text_ok: bool = False
    try:
        if provider == "openai" and settings.openai_api_key:
            try:
                from openai import OpenAI
                client = OpenAI(api_key=settings.openai_api_key)
                prompt = (
                    "You are a helpful assistant. Using the provided context, answer the question concisely.\n\n"
                    f"Question: {q}\n\nContext:\n{ctx[:12000]}"
                )
                resp = client.chat.completions.create(
                    model=settings.openai_model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.2,
                    max_tokens=256,
                )
                answer = resp.choices[0].message.content
            except Exception as e:
                error = str(e)
        elif provider == "oci":
            try:
                from .oci_llm import (
                    oci_chat_completion,
                    oci_chat_completion_chat_only,
                    oci_chat_completion_text_only,
                )
                # Probe both paths for diagnostics
                ans_chat = oci_chat_completion_chat_only(q, ctx)
                ans_text = oci_chat_completion_text_only(q, ctx)
                chat_ok = bool(ans_chat)
                text_ok = bool(ans_text)
                answer = ans_chat or ans_text or oci_chat_completion(q, ctx)
            except Exception as e:
                error = str(e)
        else:
            error = "LLM provider inactive or missing credentials"
    except Exception as e:
        error = str(e)

    return {
        "provider": provider,
        "ok": bool(answer),
        "answer": answer,
        "question": q,
        "context_chars": len(ctx or ""),
        "error": error,
        "chat_ok": chat_ok,
        "text_ok": text_ok,
    }


@app.post("/api/deep-research/start")
async def api_dr_start(request: Request, payload: Dict[str, Any] | None = None):
    user = await get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    uid = int(user.get("user_id") or user.get("id"))
    sid = None
    if payload and payload.get("space_id") is not None:
        try:
            sid = int(payload.get("space_id"))
        except Exception:
            sid = None
    if sid is None:
        sid = get_default_space_id(uid)
    cid = dr_start(uid, sid)
    return {"conversation_id": cid}


@app.post("/api/deep-research/ask")
async def api_dr_ask(request: Request, payload: Dict[str, Any]):
    user = await get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    uid = int(user.get("user_id") or user.get("id"))
    message = (payload or {}).get("message") or ""
    conversation_id = (payload or {}).get("conversation_id") or ""
    provider = (payload or {}).get("llm_provider") or None
    sid = payload.get("space_id")
    sid = int(sid) if sid is not None else get_default_space_id(uid)
    if not conversation_id:
        return JSONResponse(status_code=400, content={"error": "conversation_id required"})
    if not message:
        return JSONResponse(status_code=400, content={"error": "message required"})
    force_web = bool(payload.get("force_web"))
    urls = payload.get("urls")
    if isinstance(urls, str):
        urls = [urls]
    if isinstance(urls, (list, tuple)):
        urls = [str(u) for u in urls if u]
    else:
        urls = []
    persistent_memory_requested = _extract_bool(payload.get("persistent_memory"))
    try:
        out = dr_ask(
            uid,
            sid,
            conversation_id,
            message,
            provider_override=provider,
            force_web=force_web,
            urls=urls,
            persistent_memory=persistent_memory_requested,
        )
        summary = f"Deep Research · {message[:120]}" if message else "Deep Research"
        session_name = (message or "").strip()[:80] or None
        request_payload = {
            "message": message,
            "conversation_id": conversation_id,
            "space_id": sid,
            "force_web": force_web,
            "urls": urls,
            "persistent_memory": persistent_memory_requested,
            "user": {"id": uid, "email": user.get("email"), "role": user.get("role")},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        _log_search_activity(
            user=user,
            space_id=sid,
            activity_type="deep_research",
            request_payload=request_payload,
            response_payload=out,
            summary=summary,
            session_name=session_name,
            request=request,
        )
        return out
    except Exception as e:
        logger.exception("DR ask failed: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/deep-research/conversations")
async def api_dr_conversations(request: Request, space_id: int | None = None):
    user = await get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    uid = int(user.get("user_id") or user.get("id"))
    try:
        items = dr_list_conversations(uid, int(space_id) if space_id is not None else None)
        return {"conversations": items}
    except Exception as e:
        logger.exception("DR conversations list failed: %s", e)
        return JSONResponse(status_code=500, content={"error": "failed to list conversations"})


@app.get("/api/deep-research/conversations/{conversation_id}")
async def api_dr_conversation_detail(request: Request, conversation_id: str):
    user = await get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    uid = int(user.get("user_id") or user.get("id"))
    try:
        detail = dr_get_conversation_detail(uid, conversation_id)
        return detail
    except PermissionError:
        return JSONResponse(status_code=404, content={"error": "conversation not found"})
    except Exception as e:
        logger.exception("DR conversation detail failed: %s", e)
        return JSONResponse(status_code=500, content={"error": "failed to load conversation"})


@app.post("/api/deep-research/conversations/{conversation_id}/title")
async def api_dr_conversation_title(request: Request, conversation_id: str, payload: Dict[str, Any]):
    user = await get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    uid = int(user.get("user_id") or user.get("id"))
    title = (payload or {}).get("title")
    if not title or not str(title).strip():
        return JSONResponse(status_code=400, content={"error": "title required"})
    try:
        dr_update_conversation_title(uid, conversation_id, str(title).strip())
        return {"ok": True}
    except PermissionError:
        return JSONResponse(status_code=404, content={"error": "conversation not found"})
    except Exception as e:
        logger.exception("DR conversation title update failed: %s", e)
        return JSONResponse(status_code=500, content={"error": "failed to update title"})


@app.post("/api/deep-research/notebook/{conversation_id}")
async def api_dr_notebook_add(request: Request, conversation_id: str, payload: Dict[str, Any]):
    user = await get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    uid = int(user.get("user_id") or user.get("id"))
    title = (payload or {}).get("title") or "Notebook entry"
    content = (payload or {}).get("content")
    source = (payload or {}).get("source")
    if not content or not str(content).strip():
        return JSONResponse(status_code=400, content={"error": "content required"})
    try:
        entry = dr_add_notebook_entry(uid, conversation_id, str(title).strip(), str(content).strip(), source if isinstance(source, dict) else None)
        return entry
    except PermissionError:
        return JSONResponse(status_code=404, content={"error": "conversation not found"})
    except Exception as e:
        logger.exception("DR notebook add failed: %s", e)
        return JSONResponse(status_code=500, content={"error": "failed to add entry"})


@app.delete("/api/deep-research/notebook/{entry_id}")
async def api_dr_notebook_delete(request: Request, entry_id: int):
    user = await get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    uid = int(user.get("user_id") or user.get("id"))
    try:
        deleted = dr_delete_notebook_entry(uid, int(entry_id))
        if not deleted:
            return JSONResponse(status_code=404, content={"error": "entry not found"})
        return {"ok": True}
    except PermissionError:
        return JSONResponse(status_code=404, content={"error": "entry not found"})
    except Exception as e:
        logger.exception("DR notebook delete failed: %s", e)
        return JSONResponse(status_code=500, content={"error": "failed to delete entry"})


@app.get("/api/deep-research-config")
async def get_deep_research_config(request: Request):
    user = await get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    return {
        "followup_autosend": bool(settings.deep_research_followup_autosend),
    }


@app.post("/api/llm-debug")
async def llm_debug(payload: Dict[str, Any] | None = None):
    """
    Diagnostic endpoint to introspect OCI GenAI response shapes.
    Returns per-path (chat, text) whether output text was extracted and the response type/fields.
    """
    q = (payload or {}).get("question") if payload else None
    ctx = (payload or {}).get("context") if payload else None
    if not q:
        q = "Test connectivity. Summarize the following context in one sentence."
    if not ctx:
        ctx = "This is a test context from the /api/llm-debug endpoint."

    provider = settings.llm_provider
    if provider != "oci":
        return {
            "provider": provider,
            "error": "llm-debug only supports provider=oci",
        }

    try:
        from .oci_llm import oci_try_chat_debug, oci_try_text_debug
        ans_chat, type_chat, fields_chat = oci_try_chat_debug(q, ctx)
        ans_text, type_text, fields_text = oci_try_text_debug(q, ctx)
        return {
            "provider": provider,
            "chat": {
                "ok": bool(ans_chat),
                "type": type_chat,
                "fields": fields_chat[:50],
            },
            "text": {
                "ok": bool(ans_text),
                "type": type_text,
                "fields": fields_text[:50],
            },
        }
    except Exception as e:
        return {"provider": provider, "error": str(e)}


@app.get("/api/llm-debug")
def llm_debug_get(q: str | None = None, ctx: str | None = None):
    """
    Diagnostic endpoint (GET) to avoid JSON body issues. Provide q and ctx as query params.
    Example: /api/llm-debug?q=Question&ctx=Context
    """
    q = q or "Test connectivity. Summarize the following context in one sentence."
    ctx = ctx or "This is a test context from the /api/llm-debug endpoint."
    provider = settings.llm_provider
    if provider != "oci":
        return {"provider": provider, "error": "llm-debug only supports provider=oci"}
    try:
        from .oci_llm import oci_try_chat_debug, oci_try_text_debug
        ans_chat, type_chat, fields_chat = oci_try_chat_debug(q, ctx)
        ans_text, type_text, fields_text = oci_try_text_debug(q, ctx)
        return {
            "provider": provider,
            "chat": {"ok": bool(ans_chat), "type": type_chat, "fields": fields_chat[:50]},
            "text": {"ok": bool(ans_text), "type": type_text, "fields": fields_text[:50]},
        }
    except Exception as e:
        return {"provider": provider, "error": str(e)}


@app.get("/api/llm-config")
def llm_config():
    def _mask(ocid: str | None, keep_prefix: int = 8, keep_suffix: int = 6) -> str | None:
        if not ocid:
            return None
        if len(ocid) <= keep_prefix + keep_suffix:
            return ocid
        return ocid[:keep_prefix] + "..." + ocid[-keep_suffix:]

    return {
        "provider": settings.llm_provider,
        "oci_region": settings.oci_region,
        "oci_genai_endpoint": settings.oci_genai_endpoint,
        "compartment_id_present": bool(settings.oci_compartment_id),
        "compartment_id": _mask(settings.oci_compartment_id),
        "model_id_present": bool(settings.oci_genai_model_id),
        "model_id": _mask(settings.oci_genai_model_id, 12, 6),
        "config_file": settings.oci_config_file,
        "config_profile": settings.oci_config_profile,
    }


def _get_system_schemas() -> List[str]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT nspname
                FROM pg_catalog.pg_namespace
                WHERE nspname LIKE 'pg_%'
                  AND nspname NOT IN ('pg_temp_1', 'pg_toast_temp_1')
                ORDER BY nspname
                """
            )
            return [row[0] for row in cur.fetchall()]


def _table_allowed(table_schema: str, table_name: str, allowed_tables: Optional[set[str]]) -> bool:
    if not allowed_tables:
        return True
    table_id = f"{table_schema}.{table_name}"
    return table_id in allowed_tables or table_name in allowed_tables


def _get_schema_overview(
    allowed_tables: Optional[set[str]] = None,
    include_system: bool = False,
    include_public: bool = True,
) -> str:
    """Return a concise schema summary for NL2SQL prompts."""
    lines: List[str] = []
    schemas: List[str] = []
    if include_public:
        schemas.append("public")
    if include_system:
        schemas.extend(_get_system_schemas())
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT n.nspname AS table_schema,
                       c.relname AS table_name,
                       a.attname AS column_name,
                       pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type
                FROM pg_catalog.pg_attribute a
                JOIN pg_catalog.pg_class c ON a.attrelid = c.oid
                JOIN pg_catalog.pg_namespace n ON c.relnamespace = n.oid
                WHERE n.nspname = ANY(%s)
                  AND a.attnum > 0
                  AND NOT a.attisdropped
                  AND c.relkind IN ('r', 'v', 'm', 'f', 'p')
                ORDER BY n.nspname, c.relname, a.attnum
                """,
                (schemas,),
            )
            rows = cur.fetchall()
    current_table = None
    for table_schema, table_name, column_name, data_type in rows:
        if not _table_allowed(table_schema, table_name, allowed_tables):
            continue
        table_id = f"{table_schema}.{table_name}"
        if table_id != current_table:
            lines.append(f"\nTable: {table_id}")
            current_table = table_id
        lines.append(f"  - {column_name} ({data_type})")
    return "\n".join(lines).strip()


def _get_schema_ddl(
    allowed_tables: Optional[set[str]] = None,
    include_system: bool = False,
    include_public: bool = True,
) -> str:
    """Return simple DDL (CREATE TABLE ...) for schema context."""
    ddl_lines: List[str] = []
    schemas: List[str] = []
    if include_public:
        schemas.append("public")
    if include_system:
        schemas.extend(_get_system_schemas())
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT n.nspname AS table_schema,
                       c.relname AS table_name,
                       a.attname AS column_name,
                       pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type
                FROM pg_catalog.pg_attribute a
                JOIN pg_catalog.pg_class c ON a.attrelid = c.oid
                JOIN pg_catalog.pg_namespace n ON c.relnamespace = n.oid
                WHERE n.nspname = ANY(%s)
                  AND a.attnum > 0
                  AND NOT a.attisdropped
                  AND c.relkind IN ('r', 'v', 'm', 'f', 'p')
                ORDER BY n.nspname, c.relname, a.attnum
                """,
                (schemas,),
            )
            rows = cur.fetchall()
    current_table: Optional[str] = None
    for table_schema, table_name, column_name, data_type in rows:
        if not _table_allowed(table_schema, table_name, allowed_tables):
            continue
        table_id = f"{table_schema}.{table_name}"
        if table_id != current_table:
            if current_table is not None:
                ddl_lines.append(");")
                ddl_lines.append("")
            ddl_lines.append(f"CREATE TABLE {table_id} (")
            current_table = table_id
        ddl_lines.append(f"  {column_name} {data_type},")
    if current_table is not None:
        ddl_lines.append(");")
        ddl_lines.append("")
    return "\n".join(ddl_lines).strip()


def _get_public_tables() -> set[str]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.relname
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relkind IN ('r', 'p')
                ORDER BY c.relname
                """
            )
            return {row[0] for row in cur.fetchall()}


def _get_candidate_tables(
    include_public: bool,
    include_system: bool,
    allowed_tables: Optional[set[str]] = None,
) -> List[str]:
    schemas: List[str] = []
    if include_public:
        schemas.append("public")
    if include_system:
        schemas.extend(_get_system_schemas())
    if not schemas:
        return []
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT n.nspname AS table_schema,
                       c.relname AS table_name
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON c.relnamespace = n.oid
                WHERE n.nspname = ANY(%s)
                  AND c.relkind IN ('r', 'v', 'm', 'f', 'p')
                ORDER BY n.nspname, c.relname
                """,
                (schemas,),
            )
            rows = cur.fetchall()
    tables: List[str] = []
    for table_schema, table_name in rows:
        if not _table_allowed(table_schema, table_name, allowed_tables):
            continue
        tables.append(f"{table_schema}.{table_name}")
    return tables


def _question_tokens(text: str) -> set[str]:
    raw_tokens = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]+", (text or "").lower())
    tokens: set[str] = set()
    for tok in raw_tokens:
        if len(tok) < 3:
            continue
        tokens.add(tok)
        # very lightweight singularization for plural table names
        if tok.endswith("s") and len(tok) > 4:
            tokens.add(tok[:-1])
    return tokens


def _split_identifier_tokens(name: str) -> set[str]:
    parts = re.split(r"[_\W]+", (name or "").lower())
    out: set[str] = set()
    for part in parts:
        if not part:
            continue
        out.add(part)
        if part.endswith("s") and len(part) > 4:
            out.add(part[:-1])
    return out


def _rank_candidate_tables_for_question(
    question: str,
    candidate_tables: List[str],
    *,
    max_tables: int = 24,
) -> List[str]:
    """Rank candidate tables lexically for better first-turn NL2SQL grounding."""
    if not candidate_tables:
        return []
    max_tables = max(1, min(int(max_tables), 40))
    q = (question or "").lower()
    q_tokens = _question_tokens(question)
    if not q_tokens:
        return candidate_tables[:max_tables]

    table_set = set(candidate_tables)
    score_map: Dict[str, float] = {t: 0.0 for t in candidate_tables}

    # Name-based scoring
    for table_id in candidate_tables:
        parsed = _parse_table_id(table_id)
        if not parsed:
            continue
        _schema, table_name = parsed
        table_name_l = table_name.lower()
        table_name_tokens = _split_identifier_tokens(table_name_l)
        if table_name_l in q:
            score_map[table_id] += 8.0
        overlap = q_tokens.intersection(table_name_tokens)
        if overlap:
            score_map[table_id] += 2.5 * len(overlap)

    # Column-based scoring (single catalog pass across candidate schemas)
    candidate_schemas = sorted({t.split(".", 1)[0] if "." in t else "public" for t in candidate_tables})
    if candidate_schemas:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT n.nspname AS table_schema,
                           c.relname AS table_name,
                           a.attname AS column_name
                    FROM pg_catalog.pg_attribute a
                    JOIN pg_catalog.pg_class c ON a.attrelid = c.oid
                    JOIN pg_catalog.pg_namespace n ON c.relnamespace = n.oid
                    WHERE n.nspname = ANY(%s)
                      AND a.attnum > 0
                      AND NOT a.attisdropped
                      AND c.relkind IN ('r', 'v', 'm', 'f', 'p')
                    """,
                    (candidate_schemas,),
                )
                for table_schema, table_name, column_name in cur.fetchall() or []:
                    table_id = f"{table_schema}.{table_name}"
                    if table_id not in table_set:
                        continue
                    col_tokens = _split_identifier_tokens(column_name)
                    overlap = q_tokens.intersection(col_tokens)
                    if overlap:
                        score_map[table_id] += 1.0 * len(overlap)

    ranked = sorted(candidate_tables, key=lambda t: (score_map.get(t, 0.0), t), reverse=True)
    positives = [t for t in ranked if score_map.get(t, 0.0) > 0]
    if positives:
        return positives[:max_tables]
    return ranked[:max_tables]


def _is_safe_sql_identifier(name: str) -> bool:
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name or ""))


def _parse_table_id(table_id: str) -> Optional[tuple[str, str]]:
    raw = (table_id or "").strip().strip('"')
    if not raw:
        return None
    if "." in raw:
        schema_name, table_name = raw.split(".", 1)
    else:
        schema_name, table_name = "public", raw
    schema_name = schema_name.strip().strip('"')
    table_name = table_name.strip().strip('"')
    if not (_is_safe_sql_identifier(schema_name) and _is_safe_sql_identifier(table_name)):
        return None
    return schema_name, table_name


def _truncate_sample_value(value: Any, max_len: int = 120) -> Any:
    if value is None:
        return None
    text = str(value)
    if len(text) <= max_len:
        return value
    return text[:max_len] + "…"


def _get_user_schema_row_samples(
    table_ids: List[str],
    sample_rows: int = 5,
    max_tables: int = 5,
) -> str:
    """Return random sample rows for public/user-schema tables only."""
    if not table_ids:
        return ""
    sections: List[str] = []
    seen: set[str] = set()
    sample_rows = max(1, min(int(sample_rows), 5))
    max_tables = max(1, min(int(max_tables), 8))
    with get_conn() as conn:
        with conn.cursor() as cur:
            for table_id in table_ids:
                parsed = _parse_table_id(table_id)
                if not parsed:
                    continue
                schema_name, table_name = parsed
                if schema_name != "public":
                    # Only sample user/public schema tables.
                    continue
                normalized_table = f"{schema_name}.{table_name}"
                if normalized_table in seen:
                    continue
                seen.add(normalized_table)
                if len(sections) >= max_tables:
                    break
                try:
                    cur.execute(
                        f'SELECT * FROM "{schema_name}"."{table_name}" ORDER BY random() LIMIT %s',
                        (sample_rows,),
                    )
                    rows = cur.fetchall() or []
                    columns = [desc[0] for desc in (cur.description or [])]
                    if not rows or not columns:
                        continue
                    preview_rows: List[Dict[str, Any]] = []
                    for row in rows:
                        row_dict = {
                            col: _truncate_sample_value(row[idx])
                            for idx, col in enumerate(columns)
                        }
                        preview_rows.append(row_dict)
                    section = (
                        f"Table: {normalized_table}\n"
                        f"Sample rows (random, up to {sample_rows}):\n"
                        f"{json.dumps(preview_rows, default=str)}"
                    )
                    sections.append(section)
                except Exception:
                    # Skip tables that fail sampling (permissions, unsupported types, etc.)
                    continue
    return "\n\n".join(sections).strip()


def _get_table_column_grounding(
    table_ids: List[str],
    *,
    max_tables: int = 12,
    max_columns_per_table: int = 40,
) -> str:
    """Build compact column-level grounding for NL2SQL prompts."""
    if not table_ids:
        return ""
    parsed_tables: List[tuple[str, str]] = []
    seen: set[str] = set()
    for table_id in table_ids:
        parsed = _parse_table_id(table_id)
        if not parsed:
            continue
        schema_name, table_name = parsed
        normalized = f"{schema_name}.{table_name}"
        if normalized in seen:
            continue
        seen.add(normalized)
        parsed_tables.append((schema_name, table_name))
        if len(parsed_tables) >= max_tables:
            break
    if not parsed_tables:
        return ""

    profiles: List[str] = []
    with get_conn() as conn:
        with conn.cursor() as cur:
            for schema_name, table_name in parsed_tables:
                try:
                    cur.execute(
                        """
                        SELECT a.attname,
                               pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
                               EXISTS (
                                   SELECT 1
                                   FROM pg_catalog.pg_constraint c
                                   WHERE c.conrelid = cls.oid
                                     AND c.contype = 'p'
                                     AND a.attnum = ANY(c.conkey)
                               ) AS is_pk,
                               EXISTS (
                                   SELECT 1
                                   FROM pg_catalog.pg_constraint c
                                   WHERE c.conrelid = cls.oid
                                     AND c.contype = 'f'
                                     AND a.attnum = ANY(c.conkey)
                               ) AS is_fk,
                               EXISTS (
                                   SELECT 1
                                   FROM pg_catalog.pg_index i
                                   WHERE i.indrelid = cls.oid
                                     AND a.attnum = ANY(i.indkey)
                               ) AS is_indexed
                        FROM pg_catalog.pg_attribute a
                        JOIN pg_catalog.pg_class cls ON cls.oid = a.attrelid
                        JOIN pg_catalog.pg_namespace ns ON ns.oid = cls.relnamespace
                        WHERE ns.nspname = %s
                          AND cls.relname = %s
                          AND a.attnum > 0
                          AND NOT a.attisdropped
                        ORDER BY a.attnum
                        LIMIT %s
                        """,
                        (schema_name, table_name, max_columns_per_table),
                    )
                    rows = cur.fetchall() or []
                    if not rows:
                        continue
                    column_notes: List[str] = []
                    for col_name, data_type, is_pk, is_fk, is_indexed in rows:
                        tags: List[str] = []
                        if is_pk:
                            tags.append("PK")
                        if is_fk:
                            tags.append("FK")
                        if is_indexed:
                            tags.append("IDX")
                        tag_txt = f" [{'|'.join(tags)}]" if tags else ""
                        column_notes.append(f"- {col_name} ({data_type}){tag_txt}")
                    profiles.append(f"Table: {schema_name}.{table_name}\n" + "\n".join(column_notes))
                except Exception:
                    continue
    return "\n\n".join(profiles).strip()


def _get_fk_join_hints(table_ids: List[str], *, max_hints: int = 20) -> str:
    """Return join hints from FK metadata for the provided table set."""
    if not table_ids:
        return ""
    parsed: List[tuple[str, str]] = []
    selected_ids: set[str] = set()
    for table_id in table_ids:
        item = _parse_table_id(table_id)
        if not item:
            continue
        schema_name, table_name = item
        normalized = f"{schema_name}.{table_name}"
        selected_ids.add(normalized)
        parsed.append((schema_name, table_name))
    if not parsed:
        return ""
    schemas = sorted({s for s, _ in parsed})
    hints: List[str] = []
    seen: set[str] = set()
    with get_conn() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    """
                    SELECT src_ns.nspname AS src_schema,
                           src.relname AS src_table,
                           src_att.attname AS src_col,
                           tgt_ns.nspname AS tgt_schema,
                           tgt.relname AS tgt_table,
                           tgt_att.attname AS tgt_col
                    FROM pg_catalog.pg_constraint c
                    JOIN pg_catalog.pg_class src ON src.oid = c.conrelid
                    JOIN pg_catalog.pg_namespace src_ns ON src_ns.oid = src.relnamespace
                    JOIN pg_catalog.pg_class tgt ON tgt.oid = c.confrelid
                    JOIN pg_catalog.pg_namespace tgt_ns ON tgt_ns.oid = tgt.relnamespace
                    JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS src_key(attnum, ord) ON TRUE
                    JOIN LATERAL unnest(c.confkey) WITH ORDINALITY AS tgt_key(attnum, ord) ON tgt_key.ord = src_key.ord
                    JOIN pg_catalog.pg_attribute src_att ON src_att.attrelid = src.oid AND src_att.attnum = src_key.attnum
                    JOIN pg_catalog.pg_attribute tgt_att ON tgt_att.attrelid = tgt.oid AND tgt_att.attnum = tgt_key.attnum
                    WHERE c.contype = 'f'
                      AND src_ns.nspname = ANY(%s)
                      AND tgt_ns.nspname = ANY(%s)
                    ORDER BY src_ns.nspname, src.relname, tgt_ns.nspname, tgt.relname
                    """,
                    (schemas, schemas),
                )
                rows = cur.fetchall() or []
            except Exception:
                rows = []
    for src_schema, src_table, src_col, tgt_schema, tgt_table, tgt_col in rows:
        src_id = f"{src_schema}.{src_table}"
        tgt_id = f"{tgt_schema}.{tgt_table}"
        if src_id not in selected_ids or tgt_id not in selected_ids:
            continue
        hint = f"{src_id}.{src_col} -> {tgt_id}.{tgt_col}"
        if hint in seen:
            continue
        seen.add(hint)
        hints.append(hint)
        if len(hints) >= max_hints:
            break
    return "\n".join(f"- {h}" for h in hints)


def _select_relevant_tables(
    question: str,
    candidate_tables: List[str],
    role: str,
    sql_context: str,
    system_prompt_override: Optional[str] = None,
) -> set[str]:
    if not candidate_tables:
        return set()
    system_prompt = system_prompt_override or settings.sql_system_prompt
    table_list = "\n".join(candidate_tables)
    selection_prompt = (
        f"{system_prompt}\n\n"
        "You are selecting relevant tables for a SQL query. "
        "Return a comma-separated list of table names from the provided list. "
        "If all tables might be relevant or you are unsure, return ALL.\n\n"
        f"Context: {sql_context} ({role})\n\n"
        f"Question: {question}\n\n"
        "Tables:\n"
        f"{table_list}\n\n"
        "Return ONLY table names or ALL."
    )
    content = ""
    if settings.llm_provider == "openai" and settings.openai_api_key:
        from openai import OpenAI

        client = OpenAI(api_key=settings.openai_api_key)
        resp = client.chat.completions.create(
            model=settings.openai_model,
            messages=[{"role": "user", "content": selection_prompt}],
            temperature=0.0,
            max_tokens=200,
        )
        content = resp.choices[0].message.content or ""
    elif settings.llm_provider == "oci":
        content = oci_chat_completion(
            "Select relevant tables for the question. Return ONLY table names or ALL.",
            selection_prompt,
        ) or ""
    raw = content.strip()
    if not raw:
        return set()
    if re.search(r"\ball\b", raw, flags=re.IGNORECASE):
        return set()
    table_map = {t.lower(): t for t in candidate_tables}
    name_map: Dict[str, List[str]] = {}
    for table_id in candidate_tables:
        name = table_id.split(".")[-1].lower()
        name_map.setdefault(name, []).append(table_id)
    selected: set[str] = set()
    parts = re.split(r"[,\n]+", raw)
    for part in parts:
        cleaned = part.strip().strip('"`')
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in table_map:
            selected.add(table_map[key])
        elif key in name_map and len(name_map[key]) == 1:
            selected.add(name_map[key][0])
    return selected


def _extract_sql_from_llm(raw: str) -> str:
    if not raw:
        return ""
    match = re.search(r"```sql\s*([\s\S]+?)```", raw, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    match = re.search(r"```\s*([\s\S]+?)```", raw)
    if match:
        return match.group(1).strip()
    return raw.strip()


def _split_sql_statements(sql: str) -> List[str]:
    if not sql:
        return []
    cleaned = sql.strip()
    if not cleaned:
        return []
    parts = [s.strip() for s in cleaned.split(";")]
    return [p for p in parts if p]


def _is_safe_select(sql: str) -> bool:
    if not sql:
        return False
    lowered = sql.strip().lower()
    if ";" in lowered.strip(";"):
        return False
    if not re.match(r"^(with\s+|select\s+)", lowered):
        return False
    forbidden = [" insert ", " update ", " delete ", " drop ", " alter ", " create ", " truncate ", " grant ", " revoke "]
    return not any(token in lowered for token in forbidden)


def _apply_row_limit(sql: str, max_rows: int) -> str:
    if max_rows <= 0:
        return sql
    if re.search(r"\blimit\b", sql, flags=re.IGNORECASE):
        return f"SELECT * FROM ({sql.rstrip(';')}) AS limited_query LIMIT {max_rows}"
    return f"{sql.rstrip(';')} LIMIT {max_rows}"


def _generate_sql(
    question: str,
    role: str,
    allowed_tables: Optional[set[str]],
    system_prompt_override: Optional[str] = None,
    sql_context: str = "user",
    memory: Optional[List[Dict[str, str]]] = None,
    memory_context: Optional[str] = None,
    include_row_samples: bool = False,
    sample_table_hints: Optional[List[str]] = None,
    agentic_feedback: Optional[str] = None,
    sample_rows: Optional[int] = None,
    sample_max_tables: Optional[int] = None,
) -> tuple[str, str]:
    include_system = role == "admin" and sql_context == "system"
    include_public = sql_context != "system"
    all_candidate_tables = _get_candidate_tables(
        include_public=include_public,
        include_system=include_system,
        allowed_tables=allowed_tables,
    )
    candidate_tables = _rank_candidate_tables_for_question(
        question,
        all_candidate_tables,
        max_tables=24,
    )
    selected_tables = _select_relevant_tables(
        question=question,
        candidate_tables=candidate_tables,
        role=role,
        sql_context=sql_context,
        system_prompt_override=system_prompt_override,
    )
    prompt_tables = selected_tables or set(candidate_tables)
    grounding_tables = sorted(prompt_tables) if prompt_tables else sorted(candidate_tables)
    schema = _get_schema_overview(
        allowed_tables=prompt_tables,
        include_system=include_system,
        include_public=include_public,
    )
    ddl = _get_schema_ddl(
        allowed_tables=prompt_tables,
        include_system=include_system,
        include_public=include_public,
    )
    if prompt_tables:
        allowed_note = ", ".join(sorted(prompt_tables))
    elif include_public:
        allowed_note = "all public tables"
    else:
        allowed_note = "system catalog tables"
    role_note = ""
    if role == "admin" and sql_context == "system":
        role_note = (
            "Admin role: system catalog context. Use pg_* system schemas only (pg_catalog, pg_stat, etc.). "
            "Do NOT use information_schema or user schemas. Only select columns that exist in the tables you reference; never invent columns."
        )
    elif role == "admin":
        role_note = (
            "Admin role: user schema context. Use only public schema tables and their columns. "
            "Do NOT use information_schema."
        )
    else:
        role_note = "Use only public schema tables and their columns."
    system_prompt = system_prompt_override or settings.sql_system_prompt
    monitoring_guidance = ""
    if sql_context == "system":
        monitoring_guidance = (
            "For performance/monitoring questions (top SQL, slow queries, active sessions, locks, waits, cache, vacuum, replication), "
            "prefer pg_stat_* views or other pg_* system views/tables."
        )
    else:
        monitoring_guidance = (
            "This is user schema mode. Answer with public schema tables only, "
            "unless the user explicitly switches to system catalog mode."
        )
    guardrails = (
        "Use ONLY tables listed in Allowed tables and columns that appear in the provided schema/DDL context. "
        "For pg_catalog/pg_stat, select only well-known columns (e.g., query, state, wait_event, wait_event_type, datname) "
        "and avoid undocumented fields. If you are unsure about a column, omit it."
    )
    memory_note = ""
    if memory:
        memory_lines = []
        for item in memory:
            if item.get("question"):
                memory_lines.append(f"Q: {item['question']}")
            if item.get("sql"):
                memory_lines.append(f"SQL: {item['sql']}")
        memory_note = "\n".join(memory_lines).strip()
    conversation_memory_note = ""
    if memory_note:
        conversation_memory_note = "Conversation memory:\n" + memory_note + "\n\n"
    persistent_note = ""
    if memory_context:
        persistent_note = f"Persistent memory:\n{memory_context}\n\n"
    agentic_feedback_note = ""
    if agentic_feedback:
        agentic_feedback_note = f"Execution feedback from prior attempts:\n{agentic_feedback}\n\n"
    column_grounding = _get_table_column_grounding(grounding_tables)
    column_grounding_note = (
        f"Column-level grounding (types/keys/indexes):\n{column_grounding}\n\n"
        if column_grounding
        else ""
    )
    fk_hints = _get_fk_join_hints(grounding_tables)
    fk_hints_note = f"FK join-path hints:\n{fk_hints}\n\n" if fk_hints else ""
    row_samples_note = ""
    if include_row_samples and include_public and not include_system:
        sample_rows_eff = max(1, min(int(sample_rows or settings.sql_agentic_sample_rows), 5))
        sample_max_tables_eff = max(1, min(int(sample_max_tables or settings.sql_agentic_sample_max_tables), 5))
        hinted_tables = [t for t in (sample_table_hints or []) if t]
        sample_source_tables = hinted_tables or (sorted(selected_tables) if selected_tables else sorted(candidate_tables))
        row_samples = _get_user_schema_row_samples(
            sample_source_tables,
            sample_rows=sample_rows_eff,
            max_tables=sample_max_tables_eff,
        )
        if row_samples:
            row_samples_note = (
                "User schema sample rows (random preview, non-exhaustive):\n"
                f"{row_samples}\n\n"
            )
    prompt = (
        f"{system_prompt}\n\n"
        "Given the schema below and the user question, generate a single SELECT query only. "
        "Do not include explanations.\n\n"
        f"Allowed tables: {allowed_note}.\n"
        f"{role_note}\n"
        f"{monitoring_guidance}\n"
        f"{guardrails}\n"
        f"{persistent_note}"
        f"{agentic_feedback_note}"
        f"{column_grounding_note}"
        f"{fk_hints_note}"
        f"{row_samples_note}"
        f"{conversation_memory_note}"
        f"Schema:\n{schema}\n\n"
        f"DDL:\n{ddl}\n\n"
        f"Question: {question}\n\n"
        "Return ONLY SQL."
    )
    if settings.llm_provider == "openai" and settings.openai_api_key:
        from openai import OpenAI

        client = OpenAI(api_key=settings.openai_api_key)
        resp = client.chat.completions.create(
            model=settings.openai_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=400,
        )
        content = resp.choices[0].message.content or ""
        return _extract_sql_from_llm(content), content
    if settings.llm_provider == "oci":
        question_prompt = (
            "Generate a single PostgreSQL SELECT query for the user question. "
            "Return ONLY SQL without explanations."
        )
        context = (
            f"System: {system_prompt}\n"
            f"Allowed tables: {allowed_note}.\n"
            f"{role_note}\n"
            f"{monitoring_guidance}\n"
            f"{guardrails}\n"
            f"{persistent_note}"
            f"{agentic_feedback_note}"
            f"{column_grounding_note}"
            f"{fk_hints_note}"
            f"{row_samples_note}"
            f"{conversation_memory_note}"
            f"Schema:\n{schema}\n\n"
            f"DDL:\n{ddl}\n\n"
            f"User question: {question}"
        )
        content = oci_chat_completion(question_prompt, context) or ""
        return _extract_sql_from_llm(content), content
    raise ValueError("LLM provider not configured")


def _extract_cte_names(sql: str) -> set[str]:
    if not sql:
        return set()
    names = set()
    for match in re.finditer(r"\b([a-zA-Z_][\w\"]*)\s+as\s*\(", sql, flags=re.IGNORECASE):
        name = match.group(1).strip('"')
        if name:
            names.add(name.lower())
    return names


def _extract_table_names(sql: str) -> set[str]:
    if not sql:
        return set()
    names = set()
    for match in re.finditer(r"\b(from|join)\s+([a-zA-Z0-9_\"\.]+)", sql, flags=re.IGNORECASE):
        raw = match.group(2)
        if not raw or raw.startswith("("):
            continue
        cleaned = raw.strip('"')
        cleaned = cleaned.split(".")[-1]
        cleaned = cleaned.strip('"')
        if cleaned:
            names.add(cleaned.lower())
    return names


def _validate_sql_tables(sql: str, allowed_tables: Optional[set[str]], allow_system: bool) -> tuple[bool, Optional[str]]:
    if re.search(r"\binformation_schema\b", sql, flags=re.IGNORECASE):
        return False, "information_schema"
    if allow_system:
        return True, None
    if not allowed_tables:
        return True, None
    ctes = _extract_cte_names(sql)
    tables = _extract_table_names(sql)
    for table in sorted(tables):
        if table in ctes:
            continue
        if table not in allowed_tables:
            return False, table
    return True, None


def _execute_sql_statements(
    statements: List[str],
    *,
    role: str,
    allowed_tables: Optional[set[str]],
    allow_system: bool,
    execute: bool,
    show_results: bool,
    max_rows: int,
) -> tuple[List[Dict[str, Any]], Optional[int], bool, bool]:
    queries: List[Dict[str, Any]] = []
    total_elapsed_ms: Optional[int] = None
    has_error = False
    has_rows = False

    for stmt in statements:
        if not _is_safe_select(stmt):
            logger.warning("SQL search unsafe SQL: role=%s sql=%s", role, stmt)
            queries.append({"sql": stmt, "executed": False, "error": "unsafe_sql_generated"})
            has_error = True
            continue

        ok, bad_table = _validate_sql_tables(stmt, allowed_tables, allow_system=allow_system)
        if not ok:
            logger.warning("SQL search disallowed table: role=%s table=%s sql=%s", role, bad_table, stmt)
            queries.append({"sql": stmt, "executed": False, "error": "sql_table_not_allowed", "detail": bad_table})
            has_error = True
            continue

        if not execute:
            queries.append({"sql": stmt, "executed": False, "rows": [], "columns": [], "max_rows": max_rows})
            continue

        limited_sql = _apply_row_limit(stmt, max_rows)
        try:
            start = perf_counter()
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(limited_sql)
                    rows = cur.fetchall() if show_results else []
                    columns = [desc[0] for desc in cur.description] if cur.description else []
            elapsed_ms = int(round((perf_counter() - start) * 1000))
            if total_elapsed_ms is None:
                total_elapsed_ms = 0
            total_elapsed_ms += elapsed_ms
            if rows:
                has_rows = True
            queries.append(
                {
                    "sql": stmt,
                    "executed": True,
                    "columns": columns,
                    "rows": rows,
                    "row_count": len(rows) if isinstance(rows, list) else 0,
                    "elapsed_ms": elapsed_ms,
                    "max_rows": max_rows,
                }
            )
        except Exception as exc:
            queries.append(
                {
                    "sql": stmt,
                    "executed": False,
                    "error": "sql_execution_failed",
                    "detail": str(exc),
                    "max_rows": max_rows,
                }
            )
            has_error = True

    return queries, total_elapsed_ms, has_error, has_rows


def _build_sql_agentic_feedback(queries: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for idx, q in enumerate(queries[:5], start=1):
        sql_txt = (q.get("sql") or "").strip()
        if sql_txt:
            lines.append(f"Attempt {idx} SQL: {sql_txt}")
        if q.get("error"):
            lines.append(f"Attempt {idx} error: {q.get('error')} | detail: {q.get('detail') or ''}")
        elif q.get("executed"):
            lines.append(f"Attempt {idx} returned rows: {int(q.get('row_count') or 0)}")
    return "\n".join(lines).strip()


@app.post("/api/sql-search")
async def api_sql_search(request: Request, payload: Dict[str, Any]):
    user = await get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    role = (user.get("role") or "user").lower()
    uid = int(user.get("user_id") or user.get("id"))
    if role not in {"analyst", "admin"}:
        return JSONResponse(status_code=403, content={"error": "sql_search_not_allowed"})
    question = (payload.get("question") or "").strip()
    if not question:
        return JSONResponse(status_code=400, content={"error": "question required"})
    execute = bool(payload.get("execute"))
    show_results = bool(payload.get("show_results", True))
    agentic_mode = (
        _extract_bool(payload.get("agentic_mode"))
        if payload.get("agentic_mode") is not None
        else bool(settings.sql_agentic_mode_default)
    )
    agentic_max_retries = max(0, min(int(settings.sql_agentic_max_retries), 2))
    requested_rows = payload.get("max_rows")
    try:
        max_rows = int(requested_rows) if requested_rows is not None else int(settings.sql_default_rows)
    except (TypeError, ValueError):
        max_rows = int(settings.sql_default_rows)
    max_rows = max(1, min(max_rows, int(settings.sql_max_rows)))
    requested_memory = payload.get("memory_turns")
    try:
        memory_turns = int(requested_memory) if requested_memory is not None else int(settings.sql_memory_turns)
    except (TypeError, ValueError):
        memory_turns = int(settings.sql_memory_turns)
    memory_turns = max(0, min(memory_turns, 100))
    persistent_memory_requested = _extract_bool(payload.get("persistent_memory"))
    persistent_memory_enabled = bool(settings.sql_persistent_memory_enabled and persistent_memory_requested)
    system_prompt_override = (payload.get("system_prompt") or "").strip()
    sql_context = (payload.get("sql_context") or "user").strip().lower()
    if sql_context not in {"user", "system"}:
        sql_context = "user"
    allowed_tables = _get_public_tables() if sql_context == "user" else None
    allow_system = role == "admin" and sql_context == "system"
    if sql_context == "system" and role != "admin":
        return JSONResponse(status_code=403, content={"error": "sql_system_context_not_allowed"})
    space_id = payload.get("space_id")
    try:
        space_id = int(space_id) if space_id is not None else get_default_space_id(uid)
    except (TypeError, ValueError):
        space_id = get_default_space_id(uid)
    memory_key = f"space-{space_id}"
    memory_entries: List[Dict[str, str]] = []
    if memory_turns > 0:
        memory_entries = list(SQL_MEMORY_CACHE.get(memory_key, deque()))[-memory_turns:]
    persistent_memory_context = ""
    if persistent_memory_enabled:
        entries = _fetch_persistent_memory(space_id, "sql", question)
        persistent_memory_context = _build_persistent_memory_context(entries, int(settings.persistent_memory_max_chars))
    try:
        sql, llm_raw = _generate_sql(
            question,
            role=role,
            allowed_tables=None if allow_system else allowed_tables,
            system_prompt_override=system_prompt_override,
            sql_context=sql_context,
            memory=memory_entries,
            memory_context=persistent_memory_context,
            include_row_samples=False,
        )
    except Exception as exc:
        return JSONResponse(status_code=400, content={"error": "sql_generation_failed", "detail": str(exc)})

    sql = sql.strip().rstrip(";")
    statements = _split_sql_statements(sql)
    if not statements:
        return JSONResponse(status_code=400, content={"error": "sql_generation_failed", "detail": "empty SQL generated"})

    queries, total_elapsed_ms, has_error, has_rows = _execute_sql_statements(
        statements,
        role=role,
        allowed_tables=allowed_tables,
        allow_system=allow_system,
        execute=execute,
        show_results=show_results,
        max_rows=max_rows,
    )

    should_retry_with_sampling = bool(
        agentic_mode
        and sql_context == "user"
        and execute
        and (has_error or not has_rows)
    )

    if should_retry_with_sampling and agentic_max_retries > 0:
        current_has_error = has_error
        current_has_rows = has_rows
        for _ in range(agentic_max_retries):
            table_hints = [f"public.{name}" for name in sorted(_extract_table_names(sql))]
            if allowed_tables:
                allowed_full = {f"public.{name}" for name in allowed_tables}
                table_hints = [t for t in table_hints if t in allowed_full]
            try:
                feedback = _build_sql_agentic_feedback(queries)
                retry_sql, retry_llm_raw = _generate_sql(
                    question,
                    role=role,
                    allowed_tables=None if allow_system else allowed_tables,
                    system_prompt_override=system_prompt_override,
                    sql_context=sql_context,
                    memory=memory_entries,
                    memory_context=persistent_memory_context,
                    include_row_samples=True,
                    sample_table_hints=table_hints,
                    agentic_feedback=feedback,
                )
                retry_sql = retry_sql.strip().rstrip(";")
                retry_statements = _split_sql_statements(retry_sql)
                if not retry_statements:
                    break
                retry_queries, retry_elapsed_ms, retry_has_error, retry_has_rows = _execute_sql_statements(
                    retry_statements,
                    role=role,
                    allowed_tables=allowed_tables,
                    allow_system=allow_system,
                    execute=execute,
                    show_results=show_results,
                    max_rows=max_rows,
                )
                if (not retry_has_error and retry_has_rows) or (current_has_error and not retry_has_error):
                    sql = retry_sql
                    llm_raw = retry_llm_raw
                    queries = retry_queries
                    total_elapsed_ms = retry_elapsed_ms
                    current_has_error = retry_has_error
                    current_has_rows = retry_has_rows
                    if not current_has_error and current_has_rows:
                        break
                else:
                    break
            except Exception:
                logger.exception("SQL search retry with row sampling failed")
                break

    response = {
        "queries": queries,
        "executed": execute,
        "max_rows": max_rows,
        "memory_turns": memory_turns,
        "llm_response": llm_raw or "",
    }
    if total_elapsed_ms is not None:
        response["elapsed_ms"] = total_elapsed_ms
    if len(queries) == 1:
        response.update(queries[0])
    if memory_turns > 0 and sql:
        cache = SQL_MEMORY_CACHE[memory_key]
        cache.append({"question": question, "sql": sql})
        while len(cache) > memory_turns:
            cache.popleft()
    if persistent_memory_enabled:
        sample_columns: List[str] | None = None
        sample_rows: List[Dict[str, Any]] | None = None
        has_successful_rows = False
        for entry in queries:
            if entry.get("executed") and entry.get("columns"):
                sample_columns = entry.get("columns") or []
                rows = entry.get("rows") or []
                if rows and sample_columns:
                    has_successful_rows = True
                    sample_rows = [
                        {col: row[idx] for idx, col in enumerate(sample_columns)}
                        for row in rows[:3]
                    ]
                else:
                    sample_rows = []
                break
        memory_event_id = _persist_memory_event(
            space_id=space_id,
            user_id=uid,
            memory_type="sql",
            query_text=question,
            generated_sql=sql,
            columns=sample_columns,
            result_sample=sample_rows,
            metadata={"sql_context": sql_context, "executed": execute, "show_results": show_results},
            rating=1 if has_successful_rows else None,
        )
        response["memory_event_id"] = memory_event_id
    summary = f"SQL · {question[:120]}" if question else "SQL search"
    session_name = (question or "").strip()[:80] or None
    request_payload = {
        "question": question,
        "execute": execute,
        "show_results": show_results,
        "space_id": space_id,
        "sql_context": sql_context,
        "max_rows": max_rows,
        "memory_turns": memory_turns,
        "agentic_mode": agentic_mode,
        "persistent_memory": persistent_memory_requested,
        "user": {"id": uid, "email": user.get("email"), "role": role},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _log_search_activity(
        user=user,
        space_id=space_id,
        activity_type="sql_search",
        request_payload=request_payload,
        response_payload=response,
        summary=summary,
        session_name=session_name,
        request=request,
    )
    return response


@app.post("/api/memory/{memory_id}/rate")
async def api_rate_memory(request: Request, memory_id: int, payload: Dict[str, Any]):
    user = await get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    uid = int(user.get("user_id") or user.get("id"))
    rating_raw = payload.get("rating")
    try:
        rating = int(rating_raw)
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"error": "rating_invalid"})
    if rating not in {-1, 0, 1}:
        return JSONResponse(status_code=400, content={"error": "rating_invalid"})
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE memory_events m
                SET rating = %s
                WHERE m.id = %s
                  AND m.space_id IN (SELECT id FROM spaces WHERE user_id = %s)
                RETURNING m.id
                """,
                (rating, int(memory_id), uid),
            )
            row = cur.fetchone()
    if not row:
        return JSONResponse(status_code=404, content={"error": "memory_not_found"})
    return {"ok": True, "memory_id": int(row[0]), "rating": rating}


def _extract_query_text(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, (list, tuple, set)):
        parts: List[str] = []
        for item in raw:
            txt = _extract_query_text(item)
            if txt:
                parts.append(txt)
        return " ".join(parts).strip()
    return str(raw).strip()


def _extract_tags(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return []
        if stripped.startswith("["):
            try:
                loaded = json.loads(stripped)
                if isinstance(loaded, list):
                    return [str(t).strip() for t in loaded if str(t).strip()]
            except json.JSONDecodeError:
                pass
        return [p.strip() for p in stripped.split(",") if p.strip()]
    if isinstance(raw, (list, tuple, set)):
        return [str(p).strip() for p in raw if str(p).strip()]
    return [str(raw).strip()]


def _extract_vector(raw: Any) -> List[float] | None:
    if raw is None:
        return None
    if isinstance(raw, list):
        floats: List[float] = []
        for v in raw:
            try:
                floats.append(float(v))
            except (TypeError, ValueError):
                return None
        return floats
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return None
        try:
            loaded = json.loads(stripped)
        except json.JSONDecodeError:
            return None
        return _extract_vector(loaded)
    return None


def _extract_bool(raw: Any) -> bool:
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def _asset_candidate_bases() -> List[Path]:
    bases = [Path(settings.upload_dir)]
    tmp_dir = Path(settings.data_dir) / "tmp_uploads"
    if tmp_dir != bases[0]:
        bases.append(tmp_dir)
    return bases


def _resolve_asset_path(rel_path: Optional[str]) -> Optional[Path]:
    if not rel_path:
        return None
    rel = str(rel_path).lstrip("/\\")
    if not rel:
        return None
    for base in _asset_candidate_bases():
        base_resolved = base.resolve()
        candidate = (base_resolved / rel).resolve()
        try:
            candidate.relative_to(base_resolved)
        except ValueError:
            continue
        if candidate.exists():
            return candidate
    return None


def get_current_user_sync(request: Request) -> Optional[dict]:
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        return None
    from .session import verify_session
    return verify_session(token)


def main():
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, workers=settings.workers, reload=False)


def _migrate_object_metadata() -> None:
    """Backfill object_name + bucket/provider from legacy metadata URLs."""
    if settings.storage_backend not in {"oci", "s3", "both"}:
        return
    provider = resolve_object_provider()
    bucket = default_object_bucket(provider)
    if not provider or not bucket:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, metadata
                FROM documents
                WHERE (object_name IS NULL OR object_bucket IS NULL OR object_provider IS NULL)
                  AND metadata IS NOT NULL
                """
            )
            rows = cur.fetchall()
            for doc_id, metadata in rows:
                if not isinstance(metadata, dict):
                    continue
                object_url = metadata.get("object_url")
                thumb_url = metadata.get("thumbnail_object_url")
                object_name = _extract_object_name_from_url(object_url)
                thumb_name = _extract_object_name_from_url(thumb_url)
                if object_name or thumb_name:
                    cur.execute(
                        """
                        UPDATE documents
                        SET object_provider = %s,
                            object_bucket = %s,
                            object_name = COALESCE(object_name, %s),
                            thumbnail_object_name = COALESCE(thumbnail_object_name, %s)
                        WHERE id = %s
                        """,
                        (provider, bucket, object_name, thumb_name, int(doc_id)),
                    )


def _extract_object_name_from_url(url: Any) -> Optional[str]:
    if not url or not isinstance(url, str):
        return None
    try:
        from urllib.parse import urlparse, unquote

        parsed = urlparse(url)
        path = parsed.path or ""
        if "/o/" in path:
            parts = path.split("/o/", 1)
            if len(parts) == 2 and parts[1]:
                return unquote(parts[1])
    except Exception:
        return None
    return None


def _allowed_upload_extensions() -> set[str]:
    raw = settings.allowed_upload_extensions or ""
    exts = {e.strip().lower() for e in raw.split(",") if e.strip()}
    normalized = set()
    for ext in exts:
        if not ext.startswith("."):
            normalized.add(f".{ext}")
        else:
            normalized.add(ext)
    return normalized


def _is_allowed_filename(filename: str, allowed_exts: set[str]) -> bool:
    if not filename:
        return False
    ext = Path(filename).suffix.lower()
    return ext in allowed_exts if allowed_exts else True


def _enforce_space_upload_limit(user_id: int, space_id: int | None, incoming_count: int) -> None:
    if settings.max_files_per_space <= 0:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            if space_id is None:
                cur.execute("SELECT count(*) FROM documents WHERE user_id = %s", (user_id,))
            else:
                cur.execute("SELECT count(*) FROM documents WHERE user_id = %s AND space_id = %s", (user_id, space_id))
            existing = int(cur.fetchone()[0])
    if existing + incoming_count > settings.max_files_per_space:
        remaining = max(settings.max_files_per_space - existing, 0)
        raise ValueError(f"space upload limit reached (max {settings.max_files_per_space}). Remaining: {remaining}")


if __name__ == "__main__":
    main()
