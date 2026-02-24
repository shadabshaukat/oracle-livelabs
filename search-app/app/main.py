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

from fastapi import FastAPI, File, UploadFile, Request, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn

from .auth import SessionOrBasicAuthMiddleware
from .config import settings
from .db import init_db, get_conn
from .store import ensure_dirs, ingest_file_path, save_upload
from .object_storage import default_object_bucket, get_object_store, resolve_object_provider
from .search import semantic_search, fulltext_search, hybrid_search, rag, image_search
from .embeddings import get_model, embed_texts
from .session import get_current_user, sign_session, set_session_cookie_headers, clear_session_cookie_headers
from .users import create_user, authenticate_user, list_spaces, get_default_space_id, create_space, set_default_space
from .vision_embeddings import embed_image_paths, embed_image_texts, VisionModelUnavailable
from .oci_llm import oci_chat_completion
from .pgvector_utils import to_vec_literal

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
    init_db()
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
        "index.html",
        {
            "request": request,
            "upload_max_mb": settings.max_upload_size_mb,
            "upload_max_files": settings.max_files_per_space,
            "upload_allowed_exts": settings.allowed_upload_extensions,
            "sql_max_rows": settings.sql_max_rows,
            "sql_default_rows": settings.sql_default_rows,
            "sql_memory_turns": settings.sql_memory_turns,
            "sql_persistent_memory_enabled": settings.sql_persistent_memory_enabled,
            "text_persistent_memory_enabled": settings.text_persistent_memory_enabled,
            "image_persistent_memory_enabled": settings.image_persistent_memory_enabled,
            "sql_system_prompt": settings.sql_system_prompt,
        },
    )


def _memory_vector_operator() -> str:
    metric = settings.pgvector_metric.lower()
    if metric == "cosine":
        return "<=>"
    if metric == "l2":
        return "<->"
    if metric == "ip":
        return "<#>"
    raise ValueError("Invalid PGVECTOR_METRIC")


def _summarize_memory_text(text: str, max_chars: int) -> str:
    if not text:
        return ""
    trimmed = text.strip()
    if len(trimmed) <= max_chars:
        return trimmed
    if settings.llm_provider == "openai" and settings.openai_api_key:
        try:
            from openai import OpenAI

            client = OpenAI(api_key=settings.openai_api_key)
            prompt = (
                "Summarize the following memory into a concise, factual recap suitable for SQL/Search context. "
                f"Limit to {max_chars} characters.\n\n"
                f"Memory:\n{trimmed[:12000]}"
            )
            resp = client.chat.completions.create(
                model=settings.openai_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=400,
            )
            out = (resp.choices[0].message.content or "").strip()
            if out:
                return out[:max_chars]
        except Exception:
            pass
    if settings.llm_provider == "oci":
        try:
            prompt = (
                "Summarize the following memory into a concise, factual recap suitable for SQL/Search context. "
                f"Limit to {max_chars} characters."
            )
            out = oci_chat_completion(prompt, trimmed[:12000]) or ""
            out = out.strip()
            if out:
                return out[:max_chars]
        except Exception:
            pass
    return trimmed[:max_chars]


def _format_memory_entry(entry: Dict[str, Any]) -> str:
    if entry.get("summary"):
        return str(entry["summary"])
    parts: List[str] = []
    if entry.get("query_text"):
        parts.append(f"Q: {entry['query_text']}")
    if entry.get("generated_sql"):
        parts.append(f"SQL: {entry['generated_sql']}")
    if entry.get("response_text") and not entry.get("generated_sql"):
        parts.append(f"A: {entry['response_text']}")
    return "\n".join(parts).strip()


def _build_persistent_memory_context(entries: List[Dict[str, Any]], max_chars: int) -> str:
    if not entries:
        return ""
    blocks = []
    total = 0
    for entry in entries:
        block = _format_memory_entry(entry)
        if not block:
            continue
        if total + len(block) > max_chars:
            remaining = max_chars - total
            if remaining > 0:
                blocks.append(block[:remaining])
                total += remaining
            break
        blocks.append(block)
        total += len(block) + 2
    return "\n\n".join(blocks).strip()


def _fetch_persistent_memory(space_id: int, memory_type: str, query_text: str) -> List[Dict[str, Any]]:
    top_k = max(1, int(settings.persistent_memory_top_k))
    op = _memory_vector_operator()
    embedding = None
    if query_text:
        try:
            embedding = embed_texts([query_text])[0]
        except Exception:
            embedding = None
    with get_conn() as conn:
        with conn.cursor() as cur:
            if embedding is not None:
                cur.execute(
                    f"""
                    SELECT id, query_text, response_text, generated_sql, summary
                    FROM memory_events
                    WHERE space_id = %s
                      AND memory_type = %s
                      AND rating = 1
                    ORDER BY embedding {op} %s::vector ASC NULLS LAST, created_at DESC
                    LIMIT %s
                    """,
                    (space_id, memory_type, to_vec_literal(embedding), top_k),
                )
            else:
                cur.execute(
                    """
                    SELECT id, query_text, response_text, generated_sql, summary
                    FROM memory_events
                    WHERE space_id = %s
                      AND memory_type = %s
                      AND rating = 1
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (space_id, memory_type, top_k),
                )
            rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "query_text": r[1] or "",
            "response_text": r[2] or "",
            "generated_sql": r[3] or "",
            "summary": r[4] or "",
        }
        for r in rows
    ]


def _persist_memory_event(
    *,
    space_id: int,
    user_id: int,
    memory_type: str,
    query_text: str,
    response_text: str = "",
    generated_sql: str = "",
    columns: Optional[List[str]] = None,
    result_sample: Optional[List[Dict[str, Any]]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    payload_text = "\n".join([query_text or "", generated_sql or "", response_text or ""]).strip()
    summary_text = ""
    if payload_text:
        summary_text = _summarize_memory_text(payload_text, int(settings.persistent_memory_summary_max_chars))
    embedding = None
    try:
        if payload_text:
            embedding = embed_texts([payload_text])[0]
    except Exception:
        embedding = None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO memory_events
                (space_id, user_id, memory_type, query_text, response_text, generated_sql, columns, result_sample, metadata, summary, embedding, embedding_model)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s::vector, %s)
                RETURNING id
                """,
                (
                    space_id,
                    user_id,
                    memory_type,
                    query_text or None,
                    response_text or None,
                    generated_sql or None,
                    json.dumps(columns or []),
                    json.dumps(result_sample or []),
                    json.dumps(metadata or {}),
                    summary_text or None,
                    to_vec_literal(embedding) if embedding is not None else None,
                    settings.embedding_model_name,
                ),
            )
            row = cur.fetchone()
            return int(row[0]) if row else None


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
        # Include top references for UI (file name/type and chunk anchor)
        refs = []
        for e in hits_out[: min(len(hits_out), 5)]:
            refs.append({
                "file_name": e.get("file_name") or e.get("title") or "",
                "file_type": e.get("file_type") or "",
                "chunk_id": e.get("chunk_id"),
                "href": f"#chunk-{e.get('chunk_id')}",
                "url": None,
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
                SELECT ia.thumbnail_path, ia.document_id, d.user_id, d.object_provider, d.object_bucket, d.thumbnail_object_name
                FROM image_assets ia
                JOIN documents d ON d.id = ia.document_id
                WHERE ia.id = %s
                """,
                (int(image_id),),
            )
            row = cur.fetchone()
    if not row:
        return JSONResponse(status_code=404, content={"error": "not found"})
    thumb_rel, _doc_id, owner_id, obj_provider, obj_bucket, thumb_object_name = row
    if int(owner_id) != uid:
        return JSONResponse(status_code=404, content={"error": "not found"})
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
            logger.exception("Failed to stream thumbnail from object storage: %s", thumb_object_name)
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
                SELECT ia.file_path, ia.document_id, d.user_id, d.object_provider, d.object_bucket, d.object_name
                FROM image_assets ia
                JOIN documents d ON d.id = ia.document_id
                WHERE ia.id = %s
                """,
                (int(image_id),),
            )
            row = cur.fetchone()
    if not row:
        return JSONResponse(status_code=404, content={"error": "not found"})
    file_rel, _doc_id, owner_id, obj_provider, obj_bucket, obj_name = row
    if int(owner_id) != uid:
        return JSONResponse(status_code=404, content={"error": "not found"})
    path = _resolve_asset_path(file_rel)
    if path and path.exists():
        media_type = mimetypes.guess_type(str(path))[0] or "image/jpeg"
        return FileResponse(str(path), media_type=media_type)
    if obj_provider and obj_bucket and obj_name:
        try:
            store = get_object_store(obj_provider)
            if store:
                stream, _length, content_type = store.get_object_stream(obj_bucket, obj_name)
                return StreamingResponse(stream, media_type=content_type or "image/jpeg")
        except Exception:
            logger.exception("Failed to stream image asset from object storage: %s", obj_name)
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
            cur.execute("DELETE FROM documents WHERE id = %s AND user_id = %s", (int(doc_id), uid))

    # Best-effort cleanup of local assets (source file + thumbnails)
    try:
        if source_path:
            src_path = Path(source_path)
            if src_path.exists():
                src_path.unlink()
    except Exception:
        logger.warning("Failed to delete source file for doc_id=%s", doc_id)

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
async def api_register(payload: Dict[str, Any]):
    if not settings.allow_registration:
        return JSONResponse(status_code=403, content={"error": "registration disabled"})
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""
    if not email or not password:
        return JSONResponse(status_code=400, content={"error": "email and password required"})
    try:
        u = create_user(email, password)
        token = sign_session({"user_id": u["id"], "email": email, "role": u.get("role") or "user"})
        headers = set_session_cookie_headers(token)
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
async def api_login(payload: Dict[str, Any]):
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""
    if not email or not password:
        return JSONResponse(status_code=400, content={"error": "email and password required"})
    u = authenticate_user(email, password)
    if not u:
        return JSONResponse(status_code=401, content={"error": "invalid credentials"})
    token = sign_session({"user_id": u["id"], "email": email, "role": u.get("role") or "user"})
    headers = set_session_cookie_headers(token)
    spaces = list_spaces(u["id"]) or []
    return JSONResponse(
        status_code=200,
        content={"user": {"id": u["id"], "email": email, "role": u.get("role") or "user"}, "spaces": spaces},
        headers=headers,
    )


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
) -> str:
    include_system = role == "admin" and sql_context == "system"
    include_public = sql_context != "system"
    candidate_tables = _get_candidate_tables(
        include_public=include_public,
        include_system=include_system,
        allowed_tables=allowed_tables,
    )
    selected_tables = _select_relevant_tables(
        question=question,
        candidate_tables=candidate_tables,
        role=role,
        sql_context=sql_context,
        system_prompt_override=system_prompt_override,
    )
    prompt_tables = selected_tables or allowed_tables
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
    persistent_note = ""
    if memory_context:
        persistent_note = f"Persistent memory:\n{memory_context}\n\n"
    prompt = (
        f"{system_prompt}\n\n"
        "Given the schema below and the user question, generate a single SELECT query only. "
        "Do not include explanations.\n\n"
        f"Allowed tables: {allowed_note}.\n"
        f"{role_note}\n"
        f"{monitoring_guidance}\n"
        f"{guardrails}\n"
        f"{persistent_note}"
        f"{('Conversation memory:\n' + memory_note + '\n\n') if memory_note else ''}"
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
        return _extract_sql_from_llm(content)
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
            f"{('Conversation memory:\n' + memory_note + '\n\n') if memory_note else ''}"
            f"Schema:\n{schema}\n\n"
            f"DDL:\n{ddl}\n\n"
            f"User question: {question}"
        )
        content = oci_chat_completion(question_prompt, context) or ""
        return _extract_sql_from_llm(content)
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
        sql = _generate_sql(
            question,
            role=role,
            allowed_tables=None if allow_system else allowed_tables,
            system_prompt_override=system_prompt_override,
            sql_context=sql_context,
            memory=memory_entries,
            memory_context=persistent_memory_context,
        )
    except Exception as exc:
        return JSONResponse(status_code=400, content={"error": "sql_generation_failed", "detail": str(exc)})

    sql = sql.strip().rstrip(";")
    statements = _split_sql_statements(sql)
    if not statements:
        return JSONResponse(status_code=400, content={"error": "sql_generation_failed", "detail": "empty SQL generated"})

    queries: List[Dict[str, Any]] = []
    total_elapsed_ms: Optional[int] = None

    for stmt in statements:
        if not _is_safe_select(stmt):
            logger.warning("SQL search unsafe SQL: role=%s sql=%s", role, stmt)
            queries.append({"sql": stmt, "executed": False, "error": "unsafe_sql_generated"})
            continue

        ok, bad_table = _validate_sql_tables(stmt, allowed_tables, allow_system=allow_system)
        if not ok:
            logger.warning("SQL search disallowed table: role=%s table=%s sql=%s", role, bad_table, stmt)
            queries.append({"sql": stmt, "executed": False, "error": "sql_table_not_allowed", "detail": bad_table})
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
            queries.append({
                "sql": stmt,
                "executed": True,
                "columns": columns,
                "rows": rows,
                "elapsed_ms": elapsed_ms,
                "max_rows": max_rows,
            })
        except Exception as exc:
            queries.append({
                "sql": stmt,
                "executed": False,
                "error": "sql_execution_failed",
                "detail": str(exc),
                "max_rows": max_rows,
            })

    response = {
        "queries": queries,
        "executed": execute,
        "max_rows": max_rows,
        "memory_turns": memory_turns,
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
        for entry in queries:
            if entry.get("executed") and entry.get("columns"):
                sample_columns = entry.get("columns") or []
                rows = entry.get("rows") or []
                if rows and sample_columns:
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
        )
        response["memory_event_id"] = memory_event_id
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
