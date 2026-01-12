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


@app.post("/api/upload")
async def upload(files: List[UploadFile] = File(...)):
    results: List[Dict[str, Any]] = []
    for f in files:
        data = await f.read()
        path = save_upload(data, f.filename)
        ing = ingest_file_path(path)
        results.append({"filename": f.filename, "document_id": ing.document_id, "chunks": ing.num_chunks})
    return {"results": results}


@app.post("/api/search")
async def api_search(payload: Dict[str, Any]):
    q = payload.get("query", "")
    mode = str(payload.get("mode", "hybrid")).lower()
    top_k = int(payload.get("top_k", 6))
    if not q:
        return JSONResponse(status_code=400, content={"error": "query required"})

    answer: str | None = None
    if mode == "semantic":
        hits = semantic_search(q, top_k=top_k)
    elif mode == "fulltext":
        hits = fulltext_search(q, top_k=top_k)
    elif mode == "rag":
        answer, hits = rag(q, mode="hybrid", top_k=top_k)
    else:
        hits = hybrid_search(q, top_k=top_k)

    # Enrich with document metadata (source_path, title)
    doc_ids = sorted({h.document_id for h in hits})
    doc_info: Dict[int, Dict[str, Any]] = {}
    if doc_ids:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, source_path, COALESCE(title, '') FROM documents WHERE id = ANY(%s)", (doc_ids,)
                )
                for row in cur.fetchall():
                    doc_info[int(row[0])] = {"source_path": row[1], "title": row[2]}

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
            entry.update(meta)
        hits_out.append(entry)

    out: Dict[str, Any] = {"mode": mode if mode in {"semantic", "fulltext", "rag"} else "hybrid", "hits": hits_out}
    if answer is not None:
        out["answer"] = answer
    return out


def main():
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, workers=settings.workers, reload=False)


if __name__ == "__main__":
    main()
