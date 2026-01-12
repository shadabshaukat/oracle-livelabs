from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

import psycopg

from .config import settings
from .db import get_conn
from .embeddings import embed_texts
from .text_utils import ChunkParams, chunk_text, read_text_from_file
from .pgvector_utils import to_vec_literal

logger = logging.getLogger(__name__)


@dataclass
class IngestResult:
    document_id: int
    num_chunks: int


def ensure_dirs() -> None:
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.model_cache_dir).mkdir(parents=True, exist_ok=True)


def save_upload(file_bytes: bytes, filename: str) -> str:
    ensure_dirs()
    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    if len(file_bytes) > max_bytes:
        raise ValueError(f"File too large (> {settings.max_upload_size_mb} MB)")
    safe_name = filename.replace("..", ".").replace("/", "_")
    target = Path(settings.upload_dir) / safe_name
    with open(target, "wb") as f:
        f.write(file_bytes)
    return str(target)


def insert_document(conn: psycopg.Connection, source_path: str, source_type: str, title: Optional[str] = None, metadata: Optional[dict] = None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO documents (source_path, source_type, title, metadata) VALUES (%s, %s, %s, %s) RETURNING id",
            (source_path, source_type, title, json.dumps(metadata or {})),
        )
        doc_id = cur.fetchone()[0]
    return int(doc_id)


def insert_chunks(conn: psycopg.Connection, document_id: int, chunks: Sequence[str], embeddings: Sequence[Sequence[float]]) -> int:
    if len(chunks) != len(embeddings):
        raise ValueError("Chunks and embeddings length mismatch")
    rows = []
    for i, (content, emb) in enumerate(zip(chunks, embeddings)):
        rows.append((document_id, i, content, len(content), settings.embedding_model_name, to_vec_literal(emb)))
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO chunks (document_id, chunk_index, content, content_chars, embedding_model, embedding)
            VALUES (%s, %s, %s, %s, %s, %s::vector)
            """,
            rows,
        )
    return len(rows)


def ingest_file_path(file_path: str, title: Optional[str] = None, metadata: Optional[dict] = None, chunk_params: ChunkParams = ChunkParams()) -> IngestResult:
    text, source_type = read_text_from_file(file_path)
    chunks = chunk_text(text, chunk_params)
    if not chunks:
        raise ValueError("No textual content extracted from file")
    embeddings = embed_texts(chunks)

    with get_conn() as conn:
        doc_id = insert_document(conn, file_path, source_type, title=title, metadata=metadata)
        n = insert_chunks(conn, doc_id, chunks, embeddings)
    logger.info("Ingested file %s as document_id=%s with %s chunks", file_path, doc_id, n)
    return IngestResult(document_id=doc_id, num_chunks=n)
