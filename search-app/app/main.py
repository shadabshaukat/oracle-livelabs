from __future__ import annotations

import logging
import os
import json
import mimetypes
from time import perf_counter
from pathlib import Path
from typing import Any, Dict, List, Optional
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
from .embeddings import get_model
from .session import get_current_user, sign_session, set_session_cookie_headers, clear_session_cookie_headers
from .users import create_user, authenticate_user, list_spaces, get_default_space_id, create_space, set_default_space
from .vision_embeddings import embed_image_paths, embed_image_texts, VisionModelUnavailable

logger = logging.getLogger("searchapp")
logging.basicConfig(level=os.getenv("LOGLEVEL", "INFO"))


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
        },
    )


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
        answer, hits, used_llm, timings = rag(
            q,
            mode="hybrid",
            top_k=top_k,
            user_id=uid,
            space_id=sid,
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

    return {"results": results, "count": len(results)}


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
    return {"user": {"id": uid, "email": user.get("email")}, "spaces": list_spaces(uid)}


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
        token = sign_session({"user_id": u["id"], "email": email})
        headers = set_session_cookie_headers(token)
        spaces = list_spaces(u["id"]) or []
        return JSONResponse(status_code=200, content={"user": {"id": u["id"], "email": email}, "spaces": spaces}, headers=headers)
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
    token = sign_session({"user_id": u["id"], "email": email})
    headers = set_session_cookie_headers(token)
    spaces = list_spaces(u["id"]) or []
    return JSONResponse(status_code=200, content={"user": {"id": u["id"], "email": email}, "spaces": spaces}, headers=headers)


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
