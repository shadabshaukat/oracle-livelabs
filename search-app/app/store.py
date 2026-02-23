from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple
import re

import psycopg
from datetime import datetime, timedelta
from PIL import Image  # type: ignore
from urllib.parse import quote as urlquote

from .config import settings
from .db import get_conn
from .embeddings import embed_texts
from .text_utils import ChunkParams, chunk_text, read_text_from_file
from .vision_embeddings import (
    embed_image_paths,
    generate_image_caption,
    CaptioningModelUnavailable,
    VisionModelUnavailable,
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


def _build_oci_config():
    try:
        import oci  # type: ignore
    except Exception:
        return None, None
    cfg = None
    if settings.oci_config_file:
        try:
            cfg = oci.config.from_file(settings.oci_config_file, settings.oci_config_profile)
            if settings.oci_region:
                cfg["region"] = settings.oci_region
        except Exception:
            cfg = None
    else:
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
    return cfg, settings.oci_region


def create_par_for_object(object_name: str, expire_seconds: int = 3600) -> Optional[str]:
    if not object_name or not settings.oci_os_bucket_name:
        return None
    try:
        import oci  # type: ignore

        cfg, region = _build_oci_config()
        if not cfg:
            return None
        osc = oci.object_storage.ObjectStorageClient(cfg)
        ns = osc.get_namespace().data
        details = oci.object_storage.models.CreatePreauthenticatedRequestDetails(
            name=f"kb-par-{int(datetime.utcnow().timestamp())}",
            bucket_listing_action=None,
            object_name=object_name,
            access_type="ObjectRead",
            time_expires=(datetime.utcnow() + timedelta(seconds=int(expire_seconds))),
        )
        resp = osc.create_preauthenticated_request(
            namespace_name=ns,
            bucket_name=settings.oci_os_bucket_name,
            create_preauthenticated_request_details=details,
        )
        region = (cfg.get("region") or region or "").strip()
        base = f"https://objectstorage.{region}.oraclecloud.com" if region else "https://objectstorage.oraclecloud.com"
        return base + getattr(resp.data, "access_uri", "")
    except Exception as e:
        logger.warning("Failed to create PAR for object %s: %s", object_name, e)
        return None


def _upload_to_oci(bucket: str, object_name: str, data: bytes) -> Optional[str]:
    """Upload bytes to OCI Object Storage and return a PAR URL when possible."""
    try:
        import oci  # type: ignore

        cfg, region = _build_oci_config()
        if not cfg:
            return None
        osc = oci.object_storage.ObjectStorageClient(cfg)
        ns = osc.get_namespace().data
        try:
            details = oci.object_storage.models.CreatePreauthenticatedRequestDetails(
                name=f"kb-{int(datetime.utcnow().timestamp())}",
                bucket_listing_action=None,
                object_name=object_name,
                access_type="ObjectRead",
                time_expires=(datetime.utcnow() + timedelta(seconds=3600)),
            )
            resp = osc.create_preauthenticated_request(
                namespace_name=ns,
                bucket_name=bucket,
                create_preauthenticated_request_details=details,
            )
            par = resp.data
        except Exception as exc:
            logger.warning("Failed to create PAR for %s: %s", object_name, exc)
            par = None

        osc.put_object(ns, bucket, object_name, data)
        region = (cfg.get("region") or region or settings.oci_region or "").strip()
        base = f"https://objectstorage.{region}.oraclecloud.com" if region else "https://objectstorage.oraclecloud.com"
        if par is not None:
            url = base + getattr(par, "access_uri", "")
        else:
            url = f"{base}/n/{urlquote(ns)}/b/{urlquote(bucket)}/o/{urlquote(object_name)}"
        logger.info("OCI upload complete: bucket=%s object=%s url=%s", bucket, object_name, url)
        return url
    except Exception as e:
        logger.exception("OCI upload failed: bucket=%s object=%s error=%s", bucket, object_name if 'object_name' in locals() else '?', e)
        return None


def save_upload(file_bytes: bytes, filename: str, user_email: Optional[str] = None) -> Tuple[str, Optional[str]]:
    """Save upload respecting storage backend selection.
    Always writes a local file for ingestion (under storage/uploads when backend includes 'local',
    otherwise under a temporary path). Optionally uploads to OCI when backend includes 'oci'.
    Returns (local_path_for_ingest, oci_object_url_or_None).
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

    oci_url: Optional[str] = None
    if settings.storage_backend in {"oci", "both"} and settings.oci_os_bucket_name:
        obj_name = str(dated_rel).replace("\\", "/")
        oci_url = _upload_to_oci(settings.oci_os_bucket_name, obj_name, file_bytes)

    return str(target), oci_url


def save_upload_stream(fileobj, filename: str, user_email: Optional[str] = None) -> Tuple[str, Optional[str]]:
    """Stream upload without loading whole file in memory.
    - If backend includes 'oci', stream to OCI using UploadManager.upload_stream
    - Always write a local file for ingestion:
        * when backend includes 'local' -> storage/uploads/YYYY/MM/DD/HHMMSS/<basename>
        * when backend is 'oci' only   -> storage/tmp_uploads/YYYY/MM/DD/HHMMSS/<basename>
    Returns (local_path_for_ingest, oci_object_url_or_None).
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

    oci_url: Optional[str] = None

    # If using OCI, stream the file object to Object Storage first (then rewind for local copy)
    if settings.storage_backend in {"oci", "both"} and settings.oci_os_bucket_name:
        try:
            import oci  # type: ignore

            cfg, region = _build_oci_config()
            if cfg:
                osc = oci.object_storage.ObjectStorageClient(cfg)
                ns = osc.get_namespace().data
                upload_manager = oci.object_storage.UploadManager(osc, allow_parallel_uploads=True)
                # Rewind stream to start
                try:
                    fileobj.seek(0)
                except Exception:
                    pass
                object_name = str(dated_rel).replace("\\", "/")
                upload_manager.upload_stream(ns, settings.oci_os_bucket_name, object_name, fileobj)
                region = (cfg.get("region") or region or settings.oci_region or "").strip()
                base = f"https://objectstorage.{region}.oraclecloud.com" if region else "https://objectstorage.oraclecloud.com"
                oci_url = f"{base}/n/{urlquote(ns)}/b/{urlquote(settings.oci_os_bucket_name)}/o/{urlquote(object_name)}"
                logger.info("OCI streaming upload complete: bucket=%s object=%s url=%s", settings.oci_os_bucket_name, object_name, oci_url)
            else:
                logger.warning("OCI streaming upload skipped: missing OCI config")
        except Exception as e:
            logger.exception("OCI streaming upload failed: %s", e)

    # Rewind and copy to local target for ingestion
    try:
        fileobj.seek(0)
    except Exception:
        pass
    with open(target, "wb") as out:
        shutil.copyfileobj(fileobj, out)

    return str(target), oci_url


def insert_document(
    conn: psycopg.Connection,
    user_id: int,
    space_id: Optional[int],
    source_path: str,
    source_type: str,
    title: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO documents (user_id, space_id, source_path, source_type, title, metadata) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (user_id, space_id, source_path, source_type, title, json.dumps(metadata or {})),
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
) -> IngestResult:
    text, source_type = read_text_from_file(file_path)
    if not text and source_type != "image":
        logger.info("Empty extracted text for %s (source_type=%s)", file_path, source_type)
        object_url = (metadata or {}).get("object_url") if isinstance(metadata, dict) else None
        needs_retry = (not os.path.exists(file_path)) or (Path(file_path).suffix.lower() in {".docx", ".pdf"} and object_url)
        if needs_retry and object_url:
            try:
                import urllib.request

                Path(file_path).parent.mkdir(parents=True, exist_ok=True)
                urllib.request.urlretrieve(object_url, file_path)
                logger.info("Downloaded upload from OCI for retry: %s", object_url)
                text, source_type = read_text_from_file(file_path)
            except Exception:
                try:
                    from urllib.parse import urlparse, unquote
                    import oci  # type: ignore

                    cfg = None
                    if settings.oci_config_file:
                        cfg = oci.config.from_file(settings.oci_config_file, settings.oci_config_profile)
                        if settings.oci_region:
                            cfg["region"] = settings.oci_region
                    else:
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
                    if cfg and settings.oci_os_bucket_name:
                        u = urlparse(object_url)
                        parts = u.path.split("/o/")
                        if len(parts) == 2:
                            object_name = unquote(parts[1])
                            osc = oci.object_storage.ObjectStorageClient(cfg)
                            ns = osc.get_namespace().data
                            resp = osc.get_object(ns, settings.oci_os_bucket_name, object_name)
                            Path(file_path).parent.mkdir(parents=True, exist_ok=True)
                            with open(file_path, "wb") as fh:
                                fh.write(resp.data.content)
                            logger.info("Downloaded upload from OCI SDK for retry: %s", object_name)
                            text, source_type = read_text_from_file(file_path)
                    else:
                        logger.exception("Failed to download upload from OCI: %s", object_url)
                except Exception:
                    logger.exception("Failed to download upload from OCI: %s", object_url)
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
    if (
        settings.storage_backend in {"oci", "both"}
        and settings.oci_os_bucket_name
        and not doc_metadata.get("object_url")
    ):
        try:
            object_name = str(_relative_upload_path(file_path)).replace("\\", "/")
            with open(file_path, "rb") as fh:
                data = fh.read()
            uploaded = _upload_to_oci(settings.oci_os_bucket_name, object_name, data)
            if uploaded:
                doc_metadata["object_url"] = uploaded
        except Exception as exc:
            logger.warning("Failed to upload source to OCI for %s: %s", file_path, exc)

    with get_conn() as conn:
        doc_id = insert_document(conn, user_id, space_id, file_path, source_type, title=title, metadata=doc_metadata)
        n = insert_chunks(conn, doc_id, chunks, embeddings) if chunks else 0
        if settings.enable_image_storage and source_type == "image":
            try:
                _process_image_asset(conn, doc_id, user_id, space_id, file_path, doc_metadata)
            except VisionModelUnavailable as exc:
                doc_metadata["image_warning"] = str(exc)
            except Exception:
                logger.exception("Image asset processing failed for doc_id=%s", doc_id)
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

    oci_thumb_url: Optional[str] = None
    oci_object_url = metadata.get("object_url") if isinstance(metadata, dict) else None
    if oci_object_url and settings.storage_backend in {"oci", "both"} and settings.oci_os_bucket_name:
        try:
            from urllib.parse import urlparse, unquote

            u = urlparse(oci_object_url)
            parts = u.path.split("/o/")
            if len(parts) == 2:
                object_name = unquote(parts[1])
                thumb_object = str(Path(object_name).with_name(Path(object_name).stem + "_thumb.jpg"))
                with open(thumb_path, "rb") as tbytes:
                    data = tbytes.read()
                uploaded = _upload_to_oci(settings.oci_os_bucket_name, thumb_object, data)
                if uploaded:
                    oci_thumb_url = create_par_for_object(thumb_object) or uploaded
                    metadata["thumbnail_object_url"] = oci_thumb_url
        except Exception as exc:
            logger.warning("Failed to mirror thumbnail to OCI: %s", exc)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO image_assets (document_id, user_id, space_id, file_path, thumbnail_path, width, height, tags, caption, embedding, embedding_model)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s)
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
                to_vec_literal(vec) if vec else None,
                settings.image_embed_model,
            ),
        )
    metadata.update(
        {
            "thumbnail_path": rel_thumb,
            "thumbnail_object_url": oci_thumb_url,
            "image_tags": tags,
            "image_caption": caption,
            "image_width": width,
            "image_height": height,
        }
    )
