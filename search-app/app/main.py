from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

import gradio as gr
from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import uvicorn

from .auth import BasicAuthMiddleware
from .config import settings
from .db import init_db
from .store import ensure_dirs, ingest_file_path, save_upload
from .ui import build_ui
from .search import semantic_search, fulltext_search, hybrid_search, rag

logger = logging.getLogger("searchapp")
logging.basicConfig(level=os.getenv("LOGLEVEL", "INFO"))


app = FastAPI(title="Enterprise Search App", version="0.1.0")
app.add_middleware(BasicAuthMiddleware, protect_paths=("/api", "/ui"))

if settings.allow_cors:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.on_event("startup")
def on_startup():
    ensure_dirs()
    init_db()
    logger.info("Startup complete: directories ensured and database initialized")


@app.get("/api/health")
def health():
    return {"status": "ok"}


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

    if mode == "semantic":
        hits = semantic_search(q, top_k=top_k)
        return {"mode": mode, "hits": [h.__dict__ for h in hits]}
    if mode == "fulltext":
        hits = fulltext_search(q, top_k=top_k)
        return {"mode": mode, "hits": [h.__dict__ for h in hits]}
    if mode == "rag":
        answer, hits = rag(q, mode="hybrid", top_k=top_k)
        return {"mode": mode, "answer": answer, "hits": [h.__dict__ for h in hits]}
    hits = hybrid_search(q, top_k=top_k)
    return {"mode": "hybrid", "hits": [h.__dict__ for h in hits]}


ui = build_ui()
app = gr.mount_gradio_app(app, ui, path="/ui")


def main():
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, workers=settings.workers, reload=False)


if __name__ == "__main__":
    main()
