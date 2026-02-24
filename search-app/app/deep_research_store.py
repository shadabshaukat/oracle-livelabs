from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from .db import get_conn


def ensure_conversation(user_id: int, space_id: Optional[int], conversation_id: str, title: Optional[str] = None) -> Dict[str, Any]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO deep_research_conversations (user_id, space_id, conversation_id, title)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (conversation_id) DO UPDATE
                  SET space_id = COALESCE(EXCLUDED.space_id, deep_research_conversations.space_id),
                      title = COALESCE(deep_research_conversations.title, EXCLUDED.title),
                      updated_at = now()
                RETURNING user_id, space_id, conversation_id, title, created_at, updated_at
                """,
                (user_id, space_id, conversation_id, title),
            )
            row = cur.fetchone()
    return {
        "user_id": int(row[0]),
        "space_id": int(row[1]) if row[1] is not None else None,
        "conversation_id": row[2],
        "title": row[3],
        "created_at": row[4].isoformat() if row[4] else None,
        "updated_at": row[5].isoformat() if row[5] else None,
    }


def update_conversation_title(user_id: int, conversation_id: str, title: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE deep_research_conversations
                SET title = %s, updated_at = now()
                WHERE conversation_id = %s AND user_id = %s
                """,
                (title, conversation_id, user_id),
            )
            if cur.rowcount == 0:
                raise PermissionError("conversation not found")


def append_step(
    *,
    conversation_id: str,
    role: str,
    content: str,
    context_refs: Optional[List[Dict[str, Any]]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    refs_json = json.dumps(context_refs or [])
    meta_json = json.dumps(metadata or {})
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH next_index AS (
                  SELECT COALESCE(MAX(step_index), -1) + 1 AS idx
                  FROM deep_research_steps
                  WHERE conversation_id = %s
                )
                INSERT INTO deep_research_steps (conversation_id, step_index, role, content, context_refs, metadata)
                SELECT %s, next_index.idx, %s, %s, %s::jsonb, %s::jsonb FROM next_index
                """,
                (conversation_id, conversation_id, role, content, refs_json, meta_json),
            )
            cur.execute(
                "UPDATE deep_research_conversations SET updated_at = now() WHERE conversation_id = %s",
                (conversation_id,),
            )


def list_conversations(user_id: int, space_id: Optional[int]) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.conversation_id, c.title, c.created_at, c.updated_at,
                       c.space_id,
                       (SELECT COUNT(*) FROM deep_research_steps s WHERE s.conversation_id = c.conversation_id) AS steps,
                       (SELECT content FROM deep_research_steps s
                        WHERE s.conversation_id = c.conversation_id AND s.role = 'user'
                        ORDER BY s.step_index ASC
                        LIMIT 1) AS first_question
                FROM deep_research_conversations c
                WHERE c.user_id = %s AND (%s IS NULL OR c.space_id = %s)
                ORDER BY c.updated_at DESC
                LIMIT 100
                """,
                (user_id, space_id, space_id),
            )
            rows = cur.fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "conversation_id": row[0],
                "title": row[1],
                "created_at": row[2].isoformat() if row[2] else None,
                "updated_at": row[3].isoformat() if row[3] else None,
                "space_id": int(row[4]) if row[4] is not None else None,
                "step_count": int(row[5] or 0),
                "first_question": row[6] or "",
            }
        )
    return out


def _ensure_owner(conversation_id: str, user_id: int) -> Tuple[int, Optional[int], Dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, space_id, title, created_at, updated_at FROM deep_research_conversations WHERE conversation_id = %s",
                (conversation_id,),
            )
            row = cur.fetchone()
    if not row:
        raise PermissionError("conversation not found")
    if int(row[0]) != int(user_id):
        raise PermissionError("not allowed")
    convo = {
        "conversation_id": conversation_id,
        "title": row[2],
        "created_at": row[3].isoformat() if row[3] else None,
        "updated_at": row[4].isoformat() if row[4] else None,
    }
    return row[0], (int(row[1]) if row[1] is not None else None), convo


def get_conversation_detail(user_id: int, conversation_id: str) -> Dict[str, Any]:
    _, space_id, convo = _ensure_owner(conversation_id, user_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT step_index, role, content, context_refs, metadata, created_at
                FROM deep_research_steps
                WHERE conversation_id = %s
                ORDER BY step_index ASC
                """,
                (conversation_id,),
            )
            steps = cur.fetchall()
            cur.execute(
                """
                SELECT id, title, content, source, created_at, updated_at
                FROM deep_research_notebook_entries
                WHERE conversation_id = %s
                ORDER BY created_at DESC
                """,
                (conversation_id,),
            )
            notebook = cur.fetchall()
    convo["space_id"] = space_id
    step_items = []
    for row in steps:
        step_items.append(
            {
                "step_index": int(row[0]),
                "role": row[1],
                "content": row[2],
                "context_refs": row[3] or [],
                "metadata": row[4] or {},
                "created_at": row[5].isoformat() if row[5] else None,
            }
        )
    notebook_items = []
    for row in notebook:
        notebook_items.append(
            {
                "entry_id": int(row[0]),
                "title": row[1],
                "content": row[2],
                "source": row[3] or {},
                "created_at": row[4].isoformat() if row[4] else None,
                "updated_at": row[5].isoformat() if row[5] else None,
            }
        )
    return {
        "conversation": convo,
        "steps": step_items,
        "notebook": notebook_items,
    }


def add_notebook_entry(
    user_id: int,
    conversation_id: str,
    title: str,
    content: str,
    source: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    _ensure_owner(conversation_id, user_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO deep_research_notebook_entries (conversation_id, title, content, source)
                VALUES (%s, %s, %s, %s::jsonb)
                RETURNING id, title, content, source, created_at, updated_at
                """,
                (conversation_id, title, content, json.dumps(source or {})),
            )
            row = cur.fetchone()
    return {
        "entry_id": int(row[0]),
        "title": row[1],
        "content": row[2],
        "source": row[3] or {},
        "created_at": row[4].isoformat() if row[4] else None,
        "updated_at": row[5].isoformat() if row[5] else None,
    }


def delete_notebook_entry(user_id: int, entry_id: int) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM deep_research_notebook_entries e
                USING deep_research_conversations c
                WHERE e.id = %s AND e.conversation_id = c.conversation_id AND c.user_id = %s
                """,
                (entry_id, user_id),
            )
            deleted = cur.rowcount
    return bool(deleted)
