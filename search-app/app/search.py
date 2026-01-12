from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .config import settings
from .db import get_conn, set_search_runtime
from .embeddings import embed_texts

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


def semantic_search(query: str, top_k: int = 10, probes: Optional[int] = None) -> List[ChunkHit]:
    from .pgvector_utils import to_vec_literal
    q_emb = embed_texts([query])[0]
    op = _vector_operator()
    with get_conn() as conn:
        with conn.cursor() as cur:
            set_search_runtime(cur, probes or settings.pgvector_probes)
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


def fulltext_search(query: str, top_k: int = 10) -> List[ChunkHit]:
    with get_conn() as conn:
        with conn.cursor() as cur:
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


def hybrid_search(query: str, top_k: int = 10, alpha: float = 0.5) -> List[ChunkHit]:
    sem = semantic_search(query, top_k=top_k)
    fts = fulltext_search(query, top_k=top_k)

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


def rag(query: str, mode: str = "hybrid", top_k: int = 6) -> Tuple[str, List[ChunkHit], bool]:
    logger.info("rag: query=%r mode=%s top_k=%s provider=%s", query, mode, top_k, settings.llm_provider)
    mode = mode.lower()
    if mode == "semantic":
        hits = semantic_search(query, top_k=top_k)
    elif mode == "fulltext":
        hits = fulltext_search(query, top_k=top_k)
    else:
        hits = hybrid_search(query, top_k=top_k)

    context = "\n\n".join(h.content for h in hits)
    logger.info("rag: context_chars=%d hits=%d", len(context), len(hits))

    answer = context
    used_llm = False

    if settings.llm_provider == "openai" and settings.openai_api_key:
        try:
            from openai import OpenAI

            client = OpenAI(api_key=settings.openai_api_key)
            prompt = (
                "You are a helpful assistant. Using the provided context, answer the question concisely.\n\n"
                f"Question: {query}\n\nContext:\n{context[:12000]}"
            )
            logger.info("rag: calling OpenAI model=%s prompt_chars=%d", settings.openai_model, len(prompt))
            resp = client.chat.completions.create(
                model=settings.openai_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=512,
            )
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
            out = oci_chat_completion(query, context)
            if out:
                answer = out
                used_llm = True
        except Exception as e:
            logger.exception("OCI LLM call failed: %s", e)

    logger.info("rag: answer_chars=%d", len(answer or ''))
    return answer, hits, used_llm
