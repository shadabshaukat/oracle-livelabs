from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from .config import settings
from .db import get_conn
from .embeddings import embed_texts
from .llm import chat as llm_chat
from .pgvector_utils import to_vec_literal

logger = logging.getLogger(__name__)


def _memory_vector_operator() -> str:
    metric = settings.pgvector_metric.lower()
    if metric == "cosine":
        return "<=>"
    if metric == "l2":
        return "<->"
    if metric == "ip":
        return "<#>"
    raise ValueError("Invalid PGVECTOR_METRIC")


def _summarize_memory_text(text: str, max_chars: int) -> str:
    if not text:
        return ""
    trimmed = text.strip()
    if len(trimmed) <= max_chars:
        return trimmed
    try:
        prompt = (
            "Summarize this memory into a concise, factual recap suitable for SQL/Search context. "
            f"Limit the result to {max_chars} characters.\n\nMemory:\n{trimmed[:12000]}"
        )
        out = llm_chat(
            prompt,
            "",
            max_tokens=400,
            temperature=0.1,
        ) or ""
        if out.strip():
            return out.strip()[:max_chars]
    except Exception:
        logger.exception("Memory summarization failed")
    return trimmed[:max_chars]


def _format_memory_entry(entry: Dict[str, Any]) -> str:
    if entry.get("summary"):
        return str(entry["summary"])
    parts: List[str] = []
    if entry.get("query_text"):
        parts.append(f"Q: {entry['query_text']}")
    if entry.get("generated_sql"):
        parts.append(f"SQL: {entry['generated_sql']}")
    if entry.get("response_text") and not entry.get("generated_sql"):
        parts.append(f"A: {entry['response_text']}")
    return "\n".join(parts).strip()


def _build_persistent_memory_context(entries: List[Dict[str, Any]], max_chars: int) -> str:
    if not entries:
        return ""
    blocks = []
    total = 0
    for entry in entries:
        block = _format_memory_entry(entry)
        if not block:
            continue
        if total + len(block) > max_chars:
            remaining = max_chars - total
            if remaining > 0:
                blocks.append(block[:remaining])
                total += remaining
            break
        blocks.append(block)
        total += len(block) + 2
    return "\n\n".join(blocks).strip()


def _fetch_persistent_memory(
    space_id: int,
    memory_type: str,
    query_text: str,
    *,
    top_k: Optional[int] = None,
) -> List[Dict[str, Any]]:
    effective_top_k = top_k if top_k is not None else int(settings.persistent_memory_top_k)
    top_k_final = max(1, int(effective_top_k))
    op = _memory_vector_operator()
    embedding = None
    if query_text:
        try:
            embedding = embed_texts([query_text])[0]
        except Exception:
            embedding = None
    with get_conn() as conn:
        with conn.cursor() as cur:
            if embedding is not None:
                cur.execute(
                    f"""
                    SELECT id, query_text, response_text, generated_sql, summary
                    FROM memory_events
                    WHERE space_id = %s
                      AND memory_type = %s
                      AND rating = 1
                    ORDER BY embedding {op} %s::vector ASC NULLS LAST, created_at DESC
                    LIMIT %s
                    """,
                    (space_id, memory_type, to_vec_literal(embedding), top_k_final),
                )
            else:
                cur.execute(
                    """
                    SELECT id, query_text, response_text, generated_sql, summary
                    FROM memory_events
                    WHERE space_id = %s
                      AND memory_type = %s
                      AND rating = 1
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (space_id, memory_type, top_k_final),
                )
            rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "query_text": r[1] or "",
            "response_text": r[2] or "",
            "generated_sql": r[3] or "",
            "summary": r[4] or "",
        }
        for r in rows
    ]


def _persist_memory_event(
    *,
    space_id: int,
    user_id: int,
    memory_type: str,
    query_text: str,
    response_text: str = "",
    generated_sql: str = "",
    columns: Optional[List[str]] = None,
    result_sample: Optional[List[Dict[str, Any]]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    rating: Optional[int] = None,
) -> Optional[int]:
    payload_text = "\n".join([query_text or "", generated_sql or "", response_text or ""]).strip()
    summary_text = ""
    if payload_text:
        summary_text = _summarize_memory_text(payload_text, int(settings.persistent_memory_summary_max_chars))
    embedding = None
    try:
        if payload_text:
            embedding = embed_texts([payload_text])[0]
    except Exception:
        embedding = None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO memory_events
                (space_id, user_id, memory_type, query_text, response_text, generated_sql, columns, result_sample, metadata, summary, embedding, embedding_model, rating)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s::vector, %s, %s)
                RETURNING id
                """,
                (
                    space_id,
                    user_id,
                    memory_type,
                    query_text or None,
                    response_text or None,
                    generated_sql or None,
                    json.dumps(columns or [], default=str),
                    json.dumps(result_sample or [], default=str),
                    json.dumps(metadata or {}, default=str),
                    summary_text or None,
                    to_vec_literal(embedding) if embedding is not None else None,
                    settings.embedding_model_name,
                    rating,
                ),
            )
            row = cur.fetchone()
            return int(row[0]) if row else None
