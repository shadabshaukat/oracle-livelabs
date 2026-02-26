from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple
import re

import psycopg
from datetime import datetime
from PIL import Image  # type: ignore

from .config import settings
from .object_storage import default_object_bucket, get_object_store, resolve_object_provider
from .db import get_conn
from .embeddings import embed_texts
from .text_utils import ChunkParams, chunk_text, read_text_from_file
from .vision_embeddings import (
    embed_image_paths,
    generate_image_caption,
    CaptioningModelUnavailable,
    VisionModelUnavailable,
    ocr_image_text,
    OcrUnavailable,
    vision_dependencies_ready,
)
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


def _sanitize_email_for_path(email: str) -> str:
    e = (email or "public").strip().lower()
    e = e.replace("@", "_at_")
    e = re.sub(r"[^a-z0-9._\-]", "_", e)
    return e or "public"


def _dated_rel(base_name: str, user_email: Optional[str]) -> Path:
    now = datetime.utcnow()
    email_part = _sanitize_email_for_path(user_email or "public")
    sub = Path(email_part, str(now.year), f"{now.month:02d}", f"{now.day:02d}", now.strftime("%H%M%S"))
    return sub / base_name


def upload_object_bytes(bucket: str, object_name: str, data: bytes) -> bool:
    store = get_object_store()
    if not store:
        return False
    store.upload_bytes(bucket, object_name, data)
    return True


def save_upload(file_bytes: bytes, filename: str, user_email: Optional[str] = None) -> Tuple[str, Optional[str], Optional[str], Optional[str]]:
    """Save upload respecting storage backend selection.
    Always writes a local file for ingestion (under storage/uploads when backend includes 'local',
    otherwise under a temporary path). Optionally uploads to OCI when backend includes 'oci'.
    Returns (local_path_for_ingest, object_provider, object_bucket, object_name).
    """
    ensure_dirs()
    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    if len(file_bytes) > max_bytes:
        raise ValueError(f"File too large (> {settings.max_upload_size_mb} MB)")

    persist_local = settings.storage_backend in {"local", "both"}

    base_name = Path(filename).name.replace("..", ".")
    dated_rel = _dated_rel(base_name, user_email)

    # Choose base dir: persistent uploads vs temp area
    if persist_local:
        base_dir = Path(settings.upload_dir)
    else:
        base_dir = Path(settings.data_dir) / "tmp_uploads"
    target = base_dir / dated_rel
    target.parent.mkdir(parents=True, exist_ok=True)

    with open(target, "wb") as f:
        f.write(file_bytes)

    object_provider: Optional[str] = None
    object_bucket: Optional[str] = None
    object_name: Optional[str] = None
    if settings.storage_backend in {"oci", "s3", "both"}:
        provider = resolve_object_provider()
        bucket = default_object_bucket(provider)
        if provider and bucket:
            object_provider = provider
            object_bucket = bucket
            object_name = str(dated_rel).replace("\\", "/")
            upload_object_bytes(bucket, object_name, file_bytes)

    return str(target), object_provider, object_bucket, object_name


def save_upload_stream(fileobj, filename: str, user_email: Optional[str] = None) -> Tuple[str, Optional[str], Optional[str], Optional[str]]:
    """Stream upload without loading whole file in memory.
    - If backend includes 'oci', stream to OCI using UploadManager.upload_stream
    - Always write a local file for ingestion:
        * when backend includes 'local' -> storage/uploads/YYYY/MM/DD/HHMMSS/<basename>
        * when backend is 'oci' only   -> storage/tmp_uploads/YYYY/MM/DD/HHMMSS/<basename>
    Returns (local_path_for_ingest, object_provider, object_bucket, object_name).
    """
    import shutil
    from typing import BinaryIO

    ensure_dirs()
    persist_local = settings.storage_backend in {"local", "both"}

    base_name = Path(filename).name.replace("..", ".")
    dated_rel = _dated_rel(base_name, user_email)

    base_dir = Path(settings.upload_dir) if persist_local else (Path(settings.data_dir) / "tmp_uploads")
    target = base_dir / dated_rel
    target.parent.mkdir(parents=True, exist_ok=True)

    object_provider: Optional[str] = None
    object_bucket: Optional[str] = None
    object_name: Optional[str] = None

    # If using OCI, stream the file object to Object Storage first (then rewind for local copy)
    if settings.storage_backend in {"oci", "s3", "both"}:
        try:
            store = get_object_store()
            provider = resolve_object_provider()
            bucket = default_object_bucket(provider)
            if store and provider and bucket:
                object_provider = provider
                object_bucket = bucket
                object_name = str(dated_rel).replace("\\", "/")
                try:
                    fileobj.seek(0)
                except Exception:
                    pass
                store.upload_stream(bucket, object_name, fileobj)
            else:
                logger.warning("Object storage streaming upload skipped: missing config")
        except Exception as e:
            logger.exception("Object storage streaming upload failed: %s", e)

    # Rewind and copy to local target for ingestion
    try:
        fileobj.seek(0)
    except Exception:
        pass
    with open(target, "wb") as out:
        shutil.copyfileobj(fileobj, out)

    return str(target), object_provider, object_bucket, object_name


def insert_document(
    conn: psycopg.Connection,
    user_id: int,
    space_id: Optional[int],
    source_path: str,
    source_type: str,
    title: Optional[str] = None,
    metadata: Optional[dict] = None,
    object_provider: Optional[str] = None,
    object_bucket: Optional[str] = None,
    object_name: Optional[str] = None,
    thumbnail_object_name: Optional[str] = None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO documents (user_id, space_id, source_path, source_type, title, object_provider, object_bucket, object_name, thumbnail_object_name, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                user_id,
                space_id,
                source_path,
                source_type,
                title,
                object_provider,
                object_bucket,
                object_name,
                thumbnail_object_name,
                json.dumps(metadata or {}),
            ),
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


def ingest_file_path(
    file_path: str,
    user_id: int,
    space_id: Optional[int] = None,
    title: Optional[str] = None,
    metadata: Optional[dict] = None,
    chunk_params: Optional[ChunkParams] = None,
    object_provider: Optional[str] = None,
    object_bucket: Optional[str] = None,
    object_name: Optional[str] = None,
) -> IngestResult:
    text, source_type = read_text_from_file(file_path)
    if not text and source_type != "image":
        logger.info("Empty extracted text for %s (source_type=%s)", file_path, source_type)
        needs_retry = (not os.path.exists(file_path)) or (
            Path(file_path).suffix.lower() in {".docx", ".pdf"} and object_name and object_bucket
        )
        if needs_retry and object_name and object_bucket:
            try:
                store = get_object_store(object_provider)
                if store:
                    stream, _length, _ctype = store.get_object_stream(object_bucket, object_name)
                    Path(file_path).parent.mkdir(parents=True, exist_ok=True)
                    with open(file_path, "wb") as fh:
                        for chunk in stream:
                            fh.write(chunk)
                    logger.info("Downloaded upload from object storage for retry: %s", object_name)
                    text, source_type = read_text_from_file(file_path)
            except Exception:
                logger.exception("Failed to download upload from object storage: %s", object_name)
    # Use provided chunk params, else build from environment defaults
    cp = chunk_params or ChunkParams(
        settings.chunk_size,
        settings.chunk_overlap,
        strategy=settings.chunk_strategy,
    )
    chunks = chunk_text(text, cp)
    if not chunks and source_type != "image":
        logger.warning("No textual content extracted from %s; storing document without chunks", file_path)
    embeddings = embed_texts(chunks) if chunks else []

    created_at = datetime.utcnow().isoformat()
    doc_metadata: Dict[str, Any] = dict(metadata or {})
    if settings.storage_backend in {"oci", "s3", "both"} and not object_name:
        try:
            provider = resolve_object_provider()
            bucket = default_object_bucket(provider)
            if provider and bucket:
                obj_name = str(_relative_upload_path(file_path)).replace("\\", "/")
                with open(file_path, "rb") as fh:
                    data = fh.read()
                upload_object_bytes(bucket, obj_name, data)
                object_provider = provider
                object_bucket = bucket
                object_name = obj_name
        except Exception as exc:
            logger.warning("Failed to upload source to object storage for %s: %s", file_path, exc)

    with get_conn() as conn:
        doc_id = insert_document(
            conn,
            user_id,
            space_id,
            file_path,
            source_type,
            title=title,
            metadata=doc_metadata,
            object_provider=object_provider,
            object_bucket=object_bucket,
            object_name=object_name,
            thumbnail_object_name=None,
        )
        n = insert_chunks(conn, doc_id, chunks, embeddings) if chunks else 0
        if settings.enable_image_storage and source_type == "image":
            try:
                _process_image_asset(conn, doc_id, user_id, space_id, file_path, doc_metadata, object_provider, object_bucket, object_name)
            except VisionModelUnavailable as exc:
                doc_metadata["image_warning"] = str(exc)
            except Exception:
                logger.exception("Image asset processing failed for doc_id=%s", doc_id)
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE documents SET metadata = %s WHERE id = %s",
                    (json.dumps(doc_metadata), doc_id),
                )
        if settings.pdf_image_extraction_enabled and settings.enable_image_storage and source_type == "pdf":
            try:
                extracted = _extract_pdf_page_images(
                    conn,
                    doc_id,
                    user_id,
                    space_id,
                    file_path,
                    doc_metadata,
                )
                doc_metadata["pdf_image_count"] = extracted
                doc_metadata["pdf_image_extraction_enabled"] = True
            except VisionModelUnavailable as exc:
                doc_metadata["pdf_image_warning"] = str(exc)
            except Exception:
                logger.exception("PDF image extraction failed for doc_id=%s", doc_id)
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE documents SET metadata = %s WHERE id = %s",
                    (json.dumps(doc_metadata), doc_id),
                )
        if source_type != "image" and not chunks:
            doc_metadata["ingest_warning"] = "no_text_extracted"
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE documents SET metadata = %s WHERE id = %s",
                    (json.dumps(doc_metadata), doc_id),
                )
    logger.info("Ingested file %s as document_id=%s with %s chunks", file_path, doc_id, n)
    return IngestResult(document_id=doc_id, num_chunks=n)


def _relative_upload_path(abs_path: str) -> Path:
    base = Path(settings.upload_dir).resolve()
    try:
        return Path(abs_path).resolve().relative_to(base)
    except Exception:
        tmp_base = (Path(settings.data_dir) / "tmp_uploads").resolve()
        try:
            return Path(abs_path).resolve().relative_to(tmp_base)
        except Exception:
            return Path(Path(abs_path).name)


def _derive_image_tags(img: "Image.Image", file_path: str) -> Tuple[list[str], str]:
    tags = []
    width, height = img.size
    orientation = "square"
    if width > height * 1.15:
        orientation = "landscape"
    elif height > width * 1.15:
        orientation = "portrait"
    tags.append(orientation)
    ext = Path(file_path).suffix.lower().lstrip(".")
    if ext:
        tags.append(ext)
    return tags, f"{orientation.title()} image, {width}x{height}px"


def _keywords_from_caption(caption: str) -> list[str]:
    if not caption:
        return []
    words = [w.strip(".,;:!?()[]{}\"").lower() for w in caption.split()]
    keywords: list[str] = []
    seen = set()
    for w in words:
        if not w or len(w) < 3:
            continue
        if w in seen:
            continue
        seen.add(w)
        keywords.append(w)
        if len(keywords) >= settings.image_keyword_max:
            break
    return keywords


def _process_image_asset(
    conn: psycopg.Connection,
    doc_id: int,
    user_id: int,
    space_id: Optional[int],
    file_path: str,
    metadata: Dict[str, Any],
    object_provider: Optional[str],
    object_bucket: Optional[str],
    object_name: Optional[str],
) -> None:
    ready, detail = vision_dependencies_ready(preload_model=False)
    if not ready:
        raise VisionModelUnavailable(detail or "vision dependencies unavailable")
    rel_file = str(_relative_upload_path(file_path))
    with Image.open(file_path) as img:
        width, height = img.size
        rgb_img = img.convert("RGB")
    thumb_dir = Path(settings.upload_dir) / "thumbnails"
    thumb_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(file_path).stem
    thumb_path = thumb_dir / f"{stem}_thumb.jpg"
    thumb_img = rgb_img.copy()
    thumb_img.thumbnail((512, 512))
    thumb_img.save(thumb_path, format="JPEG", quality=80)

    rel_thumb = str(_relative_upload_path(str(thumb_path)))
    tags, caption = _derive_image_tags(thumb_img, file_path)
    try:
        generated_caption = generate_image_caption(file_path)
        if generated_caption:
            caption = generated_caption
            tags.extend(_keywords_from_caption(generated_caption))
    except CaptioningModelUnavailable as exc:
        logger.warning("Image captioning unavailable for %s: %s", file_path, exc)
    except Exception:
        logger.exception("Image captioning failed for %s", file_path)

    ocr_text = ""
    if settings.ocr_enabled:
        try:
            ocr_text = ocr_image_text(file_path)
            if ocr_text:
                tags.extend(_keywords_from_caption(ocr_text))
        except OcrUnavailable as exc:
            logger.warning("OCR unavailable for %s: %s", file_path, exc)
        except Exception:
            logger.exception("OCR failed for %s", file_path)
    vec = None
    try:
        emb = embed_image_paths([file_path])
        vec = emb[0] if emb else None
        if vec is not None and len(vec) != settings.image_embed_dim:
            logger.warning(
                "Image embedding dimension mismatch for %s: expected %s, got %s",
                file_path,
                settings.image_embed_dim,
                len(vec),
            )
            metadata["image_embedding_warning"] = f"dimension_mismatch:{len(vec)}"
            vec = None
    except Exception:
        logger.exception("Image embedding failed for %s", file_path)

    if settings.storage_backend in {"oci", "s3", "both"}:
        try:
            provider = object_provider or resolve_object_provider()
            bucket = object_bucket or default_object_bucket(provider)
            if provider and bucket:
                rel_file = str(_relative_upload_path(file_path)).replace("\\", "/")
                rel_thumb_obj = str(_relative_upload_path(str(thumb_path))).replace("\\", "/")
                with open(file_path, "rb") as fbytes:
                    upload_object_bytes(bucket, rel_file, fbytes.read())
                with open(thumb_path, "rb") as tbytes:
                    upload_object_bytes(bucket, rel_thumb_obj, tbytes.read())
                logger.info("Uploaded image asset + thumbnail to object storage: %s, %s", rel_file, rel_thumb_obj)
                if object_name:
                    thumb_object = str(Path(object_name).with_name(Path(object_name).stem + "_thumb.jpg"))
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE documents SET thumbnail_object_name = %s WHERE id = %s",
                            (thumb_object, doc_id),
                        )
        except Exception as exc:
            logger.warning("Failed to mirror thumbnail to object storage: %s", exc)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO image_assets (document_id, user_id, space_id, file_path, thumbnail_path, width, height, tags, caption, ocr_text, embedding, embedding_model)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s)
            """,
            (
                doc_id,
                user_id,
                space_id,
                rel_file,
                rel_thumb,
                width,
                height,
                json.dumps(tags),
                caption,
                ocr_text,
                to_vec_literal(vec) if vec else None,
                settings.image_embed_model,
            ),
        )
    metadata.update(
        {
            "thumbnail_path": rel_thumb,
            "thumbnail_object_url": None,
            "image_tags": tags,
            "image_caption": caption,
            "ocr_text": ocr_text,
            "image_width": width,
            "image_height": height,
        }
    )


def _extract_pdf_page_images(
    conn: psycopg.Connection,
    doc_id: int,
    user_id: int,
    space_id: Optional[int],
    file_path: str,
    metadata: Dict[str, Any],
) -> int:
    try:
        import fitz  # PyMuPDF
    except Exception as exc:
        logger.warning("PyMuPDF not available for PDF image extraction: %s", exc)
        return 0
    out_dir = Path(settings.upload_dir) / "pdf_pages" / Path(file_path).stem
    out_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    with fitz.open(file_path) as doc:
        for page_idx, page in enumerate(doc, start=1):
            try:
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
                img_path = out_dir / f"{Path(file_path).stem}_page_{page_idx}.jpg"
                pix.save(str(img_path))
                _process_image_asset(
                    conn,
                    doc_id,
                    user_id,
                    space_id,
                    str(img_path),
                    metadata,
                    None,
                    None,
                    None,
                )
                count += 1
            except VisionModelUnavailable:
                raise
            except Exception as exc:
                logger.warning("Failed to extract PDF page %s image: %s", page_idx, exc)
                continue
    return count
