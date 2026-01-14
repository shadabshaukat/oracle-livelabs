from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI, File, UploadFile, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn

from .auth import BasicAuthMiddleware
from .config import settings
from .db import init_db, get_conn
from .store import ensure_dirs, ingest_file_path, save_upload
from .search import semantic_search, fulltext_search, hybrid_search, rag
from .embeddings import get_model

logger = logging.getLogger("searchapp")
logging.basicConfig(level=os.getenv("LOGLEVEL", "INFO"))


BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Enterprise Search App", version="0.1.0")
# Protect root UI and API with Basic Auth
app.add_middleware(BasicAuthMiddleware, protect_paths=("/", "/api", "/docs", "/openapi.json", "/redoc"))

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
    return templates.TemplateResponse("index.html", {"request": request})


# API routes
@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/ready")
def ready():
    checks = {"extensions": False, "documents_table": False, "chunks_table": False, "tsv_index": False, "vec_index": False}
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_extension WHERE extname IN ('vector','pgcrypto')")
                checks["extensions"] = len(cur.fetchall()) >= 2
                cur.execute("SELECT to_regclass('public.documents') IS NOT NULL")
                checks["documents_table"] = bool(cur.fetchone()[0])
                cur.execute("SELECT to_regclass('public.chunks') IS NOT NULL")
                checks["chunks_table"] = bool(cur.fetchone()[0])
                cur.execute("SELECT to_regclass('public.idx_chunks_tsv') IS NOT NULL")
                checks["tsv_index"] = bool(cur.fetchone()[0])
                cur.execute("SELECT to_regclass('public.idx_chunks_embedding_ivfflat') IS NOT NULL")
                checks["vec_index"] = bool(cur.fetchone()[0])
        return {"ready": all(checks.values()), **checks}
    except Exception as e:
        return {"ready": False, "error": str(e), **checks}


@app.get("/api/chunks-preview")
def chunks_preview(doc_id: int, limit: int = 20):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, document_id, chunk_index, content_chars, LEFT(content, 600)
                FROM chunks
                WHERE document_id = %s
                ORDER BY chunk_index ASC
                LIMIT %s
                """,
                (doc_id, limit),
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
def doc_summary(doc_id: int):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, source_path, source_type, COALESCE(title, '') FROM documents WHERE id = %s",
                    (doc_id,),
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
async def upload(files: List[UploadFile] = File(...)):
    results: List[Dict[str, Any]] = []
    for f in files:
        data = await f.read()
        local_path, oci_url = save_upload(data, Path(f.filename).name)
        # Use basename as title and include original filename and optional object URL in metadata
        title = Path(f.filename).name
        title_no_ext = Path(title).stem
        logger.info("Upload stored: backend=%s local=%s oci=%s", settings.storage_backend, local_path, "yes" if oci_url else "no")
        try:
            meta = {"filename": title}
            if oci_url:
                meta["object_url"] = oci_url
            ing = ingest_file_path(local_path, title=title_no_ext, metadata=meta)
            results.append({
                "filename": title,
                "title": title_no_ext,
                "document_id": ing.document_id,
                "chunks": ing.num_chunks,
                "object_url": oci_url,
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
async def api_search(payload: Dict[str, Any]):
    q = payload.get("query", "")
    mode = str(payload.get("mode", "hybrid")).lower()
    top_k = int(payload.get("top_k", 25))
    if not q:
        return JSONResponse(status_code=400, content={"error": "query required"})

    answer: str | None = None
    used_llm: bool = False
    if mode == "semantic":
        hits = semantic_search(q, top_k=top_k)
    elif mode == "fulltext":
        hits = fulltext_search(q, top_k=top_k)
    elif mode == "rag":
        answer, hits, used_llm = rag(q, mode="hybrid", top_k=top_k)
    else:
        hits = hybrid_search(q, top_k=top_k)

    # Enrich with document metadata (source_path, title)
    doc_ids = sorted({h.document_id for h in hits})
    doc_info: Dict[int, Dict[str, Any]] = {}
    if doc_ids:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, source_path, source_type, COALESCE(title, ''), metadata FROM documents WHERE id = ANY(%s)", (doc_ids,)
                )
                for row in cur.fetchall():
                    # row: id, source_path, source_type, title, metadata
                    sp = row[1] or ""
                    fn = sp.rsplit("/", 1)[-1] if sp else ""
                    meta = row[4] or {}
                    object_url = None
                    if isinstance(meta, dict):
                        object_url = meta.get("object_url")
                    doc_info[int(row[0])] = {"source_path": sp, "file_name": fn, "file_type": row[2] or "", "title": row[3], "object_url": object_url}

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

    out: Dict[str, Any] = {"mode": mode if mode in {"semantic", "fulltext", "rag"} else "hybrid", "hits": hits_out}
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
                "url": doc_info.get(e.get("document_id", -1), {}).get("object_url") if doc_info else None,
            })
        out["references"] = refs
    return out


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


def main():
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, workers=settings.workers, reload=False)


if __name__ == "__main__":
    main()
