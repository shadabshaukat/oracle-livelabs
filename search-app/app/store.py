from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Tuple

import psycopg
from datetime import datetime
from urllib.parse import quote as urlquote

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


def _timestamp_path(base_name: str) -> Path:
    now = datetime.utcnow()
    # YYYY/MM/DD/HHMMSS structure
    sub = Path(str(now.year), f"{now.month:02d}", f"{now.day:02d}", now.strftime("%H%M%S"))
    return sub / base_name


def _upload_to_oci(bucket: str, object_name: str, data: bytes) -> Optional[str]:
    """Upload bytes to OCI Object Storage and return object URL if successful."""
    try:
        import oci  # type: ignore
        # Build config via file or env (compatible with app config)
        cfg = None
        if settings.oci_config_file:
            cfg = oci.config.from_file(settings.oci_config_file, settings.oci_config_profile)
            if settings.oci_region:
                cfg["region"] = settings.oci_region
        else:
            # API key envs path
            required = [settings.oci_tenancy_ocid, settings.oci_user_ocid, settings.oci_fingerprint, settings.oci_private_key_path]
            if all(required):
                cfg = {
                    "tenancy": settings.oci_tenancy_ocid,
                    "user": settings.oci_user_ocid,
                    "fingerprint": settings.oci_fingerprint,
                    "key_file": settings.oci_private_key_path,
                    "pass_phrase": settings.oci_private_key_passphrase,
                    "region": settings.oci_region,
                }
        if not cfg:
            return None
        osc = oci.object_storage.ObjectStorageClient(cfg)
        # Discover namespace if not provided
        ns = osc.get_namespace().data
        osc.put_object(ns, bucket, object_name, data)
        region = cfg.get("region") or settings.oci_region or ""
        url = f"https://objectstorage.{region}.oraclecloud.com/n/{urlquote(ns)}/b/{urlquote(bucket)}/o/{urlquote(object_name)}"
        return url
    except Exception:
        return None


def save_upload(file_bytes: bytes, filename: str) -> Tuple[str, Optional[str]]:
    """Save upload to local (dated path) and optionally upload to OCI bucket.
    Returns (local_path, oci_object_url or None).
    """
    ensure_dirs()
    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    if len(file_bytes) > max_bytes:
        raise ValueError(f"File too large (> {settings.max_upload_size_mb} MB)")
    base_name = Path(filename).name.replace("..", ".")
    dated_rel = _timestamp_path(base_name)
    target = Path(settings.upload_dir) / dated_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "wb") as f:
        f.write(file_bytes)

    oci_url: Optional[str] = None
    if settings.storage_backend in {"oci", "both"} and settings.oci_os_bucket_name:
        obj_name = str(dated_rel).replace("\\", "/")
        oci_url = _upload_to_oci(settings.oci_os_bucket_name, obj_name, file_bytes)

    return str(target), oci_url


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


def ingest_file_path(file_path: str, title: Optional[str] = None, metadata: Optional[dict] = None, chunk_params: Optional[ChunkParams] = None) -> IngestResult:
    text, source_type = read_text_from_file(file_path)
    # Use provided chunk params, else build from environment defaults
    cp = chunk_params or ChunkParams(settings.chunk_size, settings.chunk_overlap)
    chunks = chunk_text(text, cp)
    if not chunks:
        raise ValueError("No textual content extracted from file")
    embeddings = embed_texts(chunks)

    with get_conn() as conn:
        doc_id = insert_document(conn, file_path, source_type, title=title, metadata=metadata)
        n = insert_chunks(conn, doc_id, chunks, embeddings)
    logger.info("Ingested file %s as document_id=%s with %s chunks", file_path, doc_id, n)
    return IngestResult(document_id=doc_id, num_chunks=n)
