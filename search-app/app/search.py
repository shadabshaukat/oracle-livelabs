from __future__ import annotations

import json
import logging
from time import perf_counter
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .config import settings
from .db import get_conn, set_search_runtime
from .embeddings import embed_texts
from .pgvector_utils import to_vec_literal

logger = logging.getLogger(__name__)


@dataclass
class ChunkHit:
    chunk_id: int
    document_id: int
    chunk_index: int
    content: str
    distance: Optional[float] = None
    rank: Optional[float] = None


def _vector_operator() -> str:
    metric = settings.pgvector_metric.lower()
    if metric == "cosine":
        return "<=>"
    if metric == "l2":
        return "<->"
    if metric == "ip":
        return "<#>"
    raise ValueError("Invalid PGVECTOR_METRIC")


def semantic_search(query: str, top_k: int = 10, probes: Optional[int] = None, *, user_id: Optional[int] = None, space_id: Optional[int] = None) -> List[ChunkHit]:
    from .pgvector_utils import to_vec_literal
    q_emb = embed_texts([query])[0]
    op = _vector_operator()
    with get_conn() as conn:
        with conn.cursor() as cur:
            set_search_runtime(cur, probes or settings.pgvector_probes)
            if user_id is not None:
                cur.execute(
                    f"""
                    SELECT c.id, c.document_id, c.chunk_index, c.content, (c.embedding {op} %s::vector) AS distance
                    FROM chunks c
                    JOIN documents d ON d.id = c.document_id
                    WHERE c.embedding IS NOT NULL
                      AND d.user_id = %s
                      AND (%s IS NULL OR d.space_id = %s)
                    ORDER BY distance ASC
                    LIMIT %s
                    """,
                    (to_vec_literal(q_emb), int(user_id), space_id, space_id, top_k),
                )
            else:
                cur.execute(
                    f"""
                    SELECT id, document_id, chunk_index, content, (embedding {op} %s::vector) AS distance
                    FROM chunks
                    WHERE embedding IS NOT NULL
                    ORDER BY distance ASC
                    LIMIT %s
                    """,
                    (to_vec_literal(q_emb), top_k),
                )
            rows = cur.fetchall()
    return [ChunkHit(chunk_id=r[0], document_id=r[1], chunk_index=r[2], content=r[3], distance=float(r[4])) for r in rows]


def fulltext_search(query: str, top_k: int = 10, *, user_id: Optional[int] = None, space_id: Optional[int] = None) -> List[ChunkHit]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            if user_id is not None:
                cur.execute(
                    f"""
                    SELECT c.id, c.document_id, c.chunk_index, c.content,
                           ts_rank_cd(c.content_tsv, plainto_tsquery(%s, %s)) AS rank
                    FROM chunks c
                    JOIN documents d ON d.id = c.document_id
                    WHERE c.content_tsv @@ plainto_tsquery(%s, %s)
                      AND d.user_id = %s
                      AND (%s IS NULL OR d.space_id = %s)
                    ORDER BY rank DESC
                    LIMIT %s
                    """,
                    (settings.fts_config, query, settings.fts_config, query, int(user_id), space_id, space_id, top_k),
                )
            else:
                cur.execute(
                    f"""
                    SELECT id, document_id, chunk_index, content,
                           ts_rank_cd(content_tsv, plainto_tsquery(%s, %s)) AS rank
                    FROM chunks
                    WHERE content_tsv @@ plainto_tsquery(%s, %s)
                    ORDER BY rank DESC
                    LIMIT %s
                    """,
                    (settings.fts_config, query, settings.fts_config, query, top_k),
                )
            rows = cur.fetchall()
    return [ChunkHit(chunk_id=r[0], document_id=r[1], chunk_index=r[2], content=r[3], rank=float(r[4])) for r in rows]


def hybrid_search(query: str, top_k: int = 10, alpha: float = 0.5, *, user_id: Optional[int] = None, space_id: Optional[int] = None) -> List[ChunkHit]:
    sem = semantic_search(query, top_k=top_k, user_id=user_id, space_id=space_id)
    fts = fulltext_search(query, top_k=top_k, user_id=user_id, space_id=space_id)

    k = 60.0
    scores: Dict[int, float] = {}
    payload: Dict[int, ChunkHit] = {}

    for rank, hit in enumerate(sem, start=1):
        scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (k + rank)
        payload[hit.chunk_id] = hit
    for rank, hit in enumerate(fts, start=1):
        scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (k + rank)
        payload[hit.chunk_id] = payload.get(hit.chunk_id, hit)

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]
    out: List[ChunkHit] = [payload[cid] for cid, _ in ranked]
    return out


def rag(
    query: str,
    mode: str = "hybrid",
    top_k: int = 6,
    *,
    user_id: Optional[int] = None,
    space_id: Optional[int] = None,
    memory_context: Optional[str] = None,
    return_timings: bool = False,
) -> Tuple[str, List[ChunkHit], bool] | Tuple[str, List[ChunkHit], bool, Dict[str, Optional[int]]]:
    logger.info("rag: query=%r mode=%s top_k=%s provider=%s", query, mode, top_k, settings.llm_provider)
    mode = mode.lower()
    db_start = perf_counter()
    if mode == "semantic":
        hits = semantic_search(query, top_k=top_k, user_id=user_id, space_id=space_id)
    elif mode == "fulltext":
        hits = fulltext_search(query, top_k=top_k, user_id=user_id, space_id=space_id)
    else:
        hits = hybrid_search(query, top_k=top_k, user_id=user_id, space_id=space_id)
    db_ms = int(round((perf_counter() - db_start) * 1000))

    context = "\n\n".join(h.content for h in hits)
    if memory_context:
        context = f"Memory context:\n{memory_context}\n\n{context}"
    logger.info("rag: context_chars=%d hits=%d", len(context), len(hits))

    answer = context
    used_llm = False

    llm_ms: Optional[int] = None
    if settings.llm_provider == "openai" and settings.openai_api_key:
        try:
            from openai import OpenAI

            client = OpenAI(api_key=settings.openai_api_key)
            prompt = (
                "You are a helpful assistant. Using the provided context, answer the question concisely.\n\n"
                f"Question: {query}\n\nContext:\n{context[:12000]}"
            )
            logger.info("rag: calling OpenAI model=%s prompt_chars=%d", settings.openai_model, len(prompt))
            llm_start = perf_counter()
            resp = client.chat.completions.create(
                model=settings.openai_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=settings.rag_max_tokens,
            )
            llm_ms = int(round((perf_counter() - llm_start) * 1000))
            out = resp.choices[0].message.content
            if out:
                answer = out
                used_llm = True
        except Exception as e:
            logger.exception("LLM call failed: %s", e)
    elif settings.llm_provider == "oci":
        try:
            from .oci_llm import oci_chat_completion
            logger.info("rag: calling OCI GenAI")
            llm_start = perf_counter()
            out = oci_chat_completion(query, context, max_tokens=settings.rag_max_tokens)
            llm_ms = int(round((perf_counter() - llm_start) * 1000))
            if out:
                answer = out
                used_llm = True
        except Exception as e:
            logger.exception("OCI LLM call failed: %s", e)

    logger.info("rag: answer_chars=%d", len(answer or ''))
    if return_timings:
        return answer, hits, used_llm, {"db_ms": db_ms, "llm_ms": llm_ms}
    return answer, hits, used_llm


def image_search(query: Optional[str], vector: Optional[List[float]], top_k: int, *, user_id: Optional[int], space_id: Optional[int], tags: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    if not settings.enable_image_storage:
        return []
    if vector is not None:
        vector = [float(v) for v in vector if isinstance(v, (int, float))]
        if not vector:
            vector = None

    where = []
    filter_params: List[Any] = []
    if user_id is not None:
        where.append("ia.user_id = %s")
        filter_params.append(int(user_id))
    if space_id is not None:
        where.append("ia.space_id = %s")
        filter_params.append(int(space_id))
    if tags:
        where.append("ia.tags @> %s::jsonb")
        filter_params.append(json.dumps(tags))
    if query and vector is None:
        where.append(
            "(ia.caption ILIKE %s OR COALESCE(d.metadata->>'image_caption','') ILIKE %s OR COALESCE(ia.ocr_text,'') ILIKE %s)"
        )
        filter_params.extend([f"%{query}%", f"%{query}%", f"%{query}%"])
    if vector is not None:
        where.append("ia.embedding IS NOT NULL")

    order_clause = "ia.created_at DESC"
    distance_expr = "NULL::double precision AS distance"
    vector_param = None
    distance_value_expr = "NULL::double precision"
    if vector is not None:
        distance_expr = f"(ia.embedding {_vector_operator()} %s::vector) AS distance"
        distance_value_expr = f"(ia.embedding {_vector_operator()} %s::vector)"
        vector_param = to_vec_literal(vector)

    rank_expr = "0.0::double precision AS text_rank"
    rank_value_expr = "0.0::double precision"
    rank_params: List[Any] = []
    if query:
        rank_value_expr = (
            "ts_rank_cd("
            "to_tsvector('simple', COALESCE(ia.caption,'') || ' ' || COALESCE(d.metadata->>'image_caption','') || ' ' || COALESCE(ia.ocr_text,'')), "
            "plainto_tsquery('simple', %s)"
            ")"
        )
        rank_expr = rank_value_expr + " AS text_rank"
        rank_params.append(query)

    sql = [
        "SELECT ia.id, ia.document_id, ia.file_path, ia.thumbnail_path, ia.caption, ia.ocr_text, ia.tags, ia.width, ia.height, ia.created_at,",
        distance_expr + ",",
        rank_expr,
        "FROM image_assets ia",
        "JOIN documents d ON d.id = ia.document_id",
    ]
    if where:
        sql.append("WHERE " + " AND ".join(where))
    params: List[Any] = []
    if vector_param is not None:
        params.append(vector_param)
    params.extend(rank_params)
    params.extend(filter_params)

    base_query = "\n".join(sql)
    if vector_param is not None and query:
        query_str = (
            "SELECT * FROM (\n"
            f"{base_query}\n"
            ") AS img\n"
            "ORDER BY (COALESCE(img.text_rank, 0) * %s "
            "+ (1.0 / (1.0 + COALESCE(img.distance, 0))) * %s) DESC\n"
            "LIMIT %s"
        )
        params.extend([settings.image_search_text_weight, settings.image_search_vector_weight, int(top_k)])
    else:
        if vector_param is not None:
            order_clause = "distance ASC"
        elif query:
            order_clause = "text_rank DESC"
        else:
            order_clause = "ia.created_at DESC"
        query_str = base_query + f"\nORDER BY {order_clause} LIMIT %s"
        params.append(int(top_k))
    results: List[Dict[str, Any]] = []
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query_str, params)
            rows = cur.fetchall()
    for row in rows:
        image_id, doc_id, file_path, thumb_path, caption, ocr_text, tags_raw, width, height, created_at, distance, text_rank = row
        parsed_tags: List[str]
        if isinstance(tags_raw, list):
            parsed_tags = tags_raw
        else:
            try:
                parsed_tags = json.loads(tags_raw) if tags_raw else []
            except Exception:
                parsed_tags = []
        src = {
            "doc_id": doc_id,
            "image_id": image_id,
            "file_path": file_path,
            "thumbnail_path": thumb_path,
            "caption": caption,
            "ocr_text": ocr_text,
            "tags": parsed_tags,
            "width": width,
            "height": height,
            "created_at": created_at.isoformat() if created_at else None,
        }
        entry: Dict[str, Any] = {"_source": src}
        vec_score = None
        if distance is not None:
            try:
                dist_val = float(distance)
                vec_score = 1.0 / (1.0 + max(dist_val, 0.0))
            except Exception:
                vec_score = None
        try:
            txt_score = float(text_rank or 0.0)
        except Exception:
            txt_score = 0.0
        if vec_score is None and txt_score == 0.0:
            entry["_score"] = None
        else:
            entry["_score"] = (settings.image_search_vector_weight * (vec_score or 0.0)) + (settings.image_search_text_weight * txt_score)
        results.append(entry)
    return results
