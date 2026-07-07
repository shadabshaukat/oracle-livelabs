from __future__ import annotations

import json
import logging
from time import perf_counter
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .config import settings
from .db import get_conn, set_search_runtime
from .embeddings import embed_texts
from .llm import chat as llm_chat
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
    semantic_weight = min(1.0, max(0.0, float(alpha)))
    lexical_weight = 1.0 - semantic_weight
    scores: Dict[int, float] = {}
    payload: Dict[int, ChunkHit] = {}

    for rank, hit in enumerate(sem, start=1):
        scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + semantic_weight / (k + rank)
        payload[hit.chunk_id] = hit
    for rank, hit in enumerate(fts, start=1):
        scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + lexical_weight / (k + rank)
        existing = payload.get(hit.chunk_id)
        if existing is not None:
            existing.rank = hit.rank
        else:
            payload[hit.chunk_id] = hit

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]
    out: List[ChunkHit] = [payload[cid] for cid, _ in ranked]
    return out


def _filter_rag_hits(hits: List[ChunkHit]) -> List[ChunkHit]:
    """Drop weak cosine-only matches and duplicate text before LLM synthesis."""
    filtered: List[ChunkHit] = []
    seen_content: set[str] = set()
    for hit in hits:
        lexical_match = hit.rank is not None and hit.rank > 0
        semantic_match = hit.distance is not None
        if settings.pgvector_metric.lower() == "cosine" and semantic_match and not lexical_match:
            if float(hit.distance) > float(settings.rag_max_cosine_distance):
                continue
        elif not lexical_match and not semantic_match:
            continue
        normalized = " ".join((hit.content or "").lower().split())
        if not normalized:
            continue
        dedupe_key = normalized[:500]
        if dedupe_key in seen_content:
            continue
        seen_content.add(dedupe_key)
        filtered.append(hit)
    return filtered


def _build_rag_context(hits: List[ChunkHit], memory_context: Optional[str] = None) -> str:
    """Build numbered, fairly sized source blocks within a hard character budget."""
    max_chars = max(500, int(settings.rag_max_context_chars))
    parts: List[str] = []
    used = 0

    memory = (memory_context or "").strip()
    if memory:
        memory_budget = min(len(memory), max_chars // 5)
        memory_block = f"[Conversation memory - not a source]\n{memory[:memory_budget]}"
        parts.append(memory_block)
        used += len(memory_block) + 2

    for index, hit in enumerate(hits, start=1):
        remaining_sources = len(hits) - index + 1
        remaining_budget = max_chars - used
        if remaining_budget <= 0:
            break
        fair_share = max(1, remaining_budget // max(1, remaining_sources))
        header = f"[Source {index}]\n"
        content_limit = max(0, fair_share - len(header) - 2)
        content = (hit.content or "").strip()[:content_limit]
        if not content:
            continue
        block = header + content
        if len(block) > remaining_budget:
            block = block[:remaining_budget]
        parts.append(block)
        used += len(block) + 2

    return "\n\n".join(parts)[:max_chars]


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
    top_k = max(1, min(int(top_k), max(1, int(settings.rag_top_k))))
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

    hits = _filter_rag_hits(hits)[:top_k]
    context = _build_rag_context(hits, memory_context)
    logger.info("rag: context_chars=%d hits=%d", len(context), len(hits))

    answer = ""
    used_llm = False

    llm_ms: Optional[int] = None
    if not hits:
        answer = "I couldn't find sufficiently relevant information in the indexed documents to answer that question."
    else:
        try:
            llm_start = perf_counter()
            out = llm_chat(
                query,
                context,
                max_tokens=settings.rag_max_tokens,
                temperature=0.1,
            )
            llm_ms = int(round((perf_counter() - llm_start) * 1000))
            if out:
                answer = out
                used_llm = True
        except Exception as e:
            llm_ms = int(round((perf_counter() - llm_start) * 1000))
            logger.exception("RAG answer synthesis failed: %s", e)
        if not used_llm:
            answer = (
                "I found relevant sources, but the configured answer model is unavailable. "
                "Verify that Ollama and the pinned local model are running, then try again."
            )

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
