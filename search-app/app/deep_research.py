from __future__ import annotations

import logging
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .db import get_conn
from .config import settings
from .search import hybrid_search, ChunkHit
from .deep_research_store import (
    ensure_conversation as store_ensure_conversation,
    append_step as store_append_step,
    get_conversation_detail as store_get_conversation_detail,
)
from .external_sources import ingest_external_urls, retrieve_external_contexts
from .agentic_research import decide_web_and_contexts
from .memory_store import (
    _fetch_persistent_memory,
    _build_persistent_memory_context,
    _persist_memory_event,
    _summarize_memory_text,
)

logger = logging.getLogger(__name__)

# In-process memory for short-lived DR state
_MEM: Dict[str, Dict[str, object]] = {}


@dataclass
class Message:
    role: str  # 'user' | 'assistant' | 'system'
    content: str


@dataclass
class DRState:
    user_id: int
    space_id: Optional[int]
    conversation_id: str
    messages: List[Message] = field(default_factory=list)

    def key(self) -> str:
        sid = self.space_id if self.space_id is not None else "_"
        return f"dr:{self.user_id}:{sid}:{self.conversation_id}"

    def trim(self, keep: int = 20) -> None:
        max_msgs = max(int(keep) * 2, 40)
        if len(self.messages) > max_msgs:
            self.messages = self.messages[-max_msgs:]


def _load_state(user_id: int, space_id: Optional[int], conversation_id: str) -> DRState:
    key_prefix = f"dr:{user_id}:{space_id if space_id is not None else '_'}:{conversation_id}"
    data = _MEM.get(key_prefix)
    if data and isinstance(data, dict):
        msgs = [Message(**m) for m in data.get("messages", [])]
        return DRState(user_id=user_id, space_id=space_id, conversation_id=conversation_id, messages=msgs)
    return DRState(user_id=user_id, space_id=space_id, conversation_id=conversation_id, messages=[])


def _save_state(state: DRState) -> None:
    payload = {"messages": [m.__dict__ for m in state.messages]}
    _MEM[state.key()] = payload


def start_conversation(user_id: int, space_id: Optional[int]) -> str:
    cid = uuid.uuid4().hex[:12]
    store_ensure_conversation(user_id, space_id, cid, None)
    st = DRState(
        user_id=user_id,
        space_id=space_id,
        conversation_id=cid,
        messages=[
            Message(
                "system",
                "You are Deep Research mode. Work step-by-step: plan, retrieve, analyze, synthesize. "
                "Always ground answers in the user's knowledge base for this space. If something isn't in the KB, say so.",
            )
        ],
    )
    _save_state(st)
    logger.info("DR start: user=%s space=%s cid=%s", user_id, space_id, cid)
    return cid


def _extract_subqueries(question: str) -> List[str]:
    q = question.strip()
    if len(q) < 80:
        return [q]
    parts = re.split(r"\b(?:and|or|,|;|\n)\b", q, flags=re.IGNORECASE)
    subs = [p.strip() for p in parts if p.strip()]
    if 1 < len(subs) <= 6:
        return subs[:4]
    return [q]


def _coverage_metrics(hits: List[ChunkHit]) -> Tuple[int, int, float]:
    if not hits:
        return 0, 0, 0.0
    unique_docs = len({h.document_id for h in hits if h.document_id is not None})
    distances = [h.distance for h in hits if h.distance is not None]
    best_distance = min(distances) if distances else 0.0
    return len(hits), unique_docs, float(best_distance or 0.0)


def _is_local_weak(hits: List[ChunkHit]) -> bool:
    count, unique_docs, _ = _coverage_metrics(hits)
    return count < 4 or unique_docs < 2


def _rewrite_for_search(question: str, recent_context: str) -> Optional[str]:
    try:
        from .llm import chat as llm_chat

        prompt = (
            "Rewrite the user question into a concise web search query. "
            "Use 6-12 words, drop filler, keep proper nouns. "
            "Return only the query text.\n\n"
            f"Question: {question}\n"
            f"Context: {recent_context.strip()}"
        )
        rewritten = (llm_chat(prompt, "", max_tokens=64, temperature=0.2) or "").strip()
        return rewritten.splitlines()[0].strip() if rewritten else None
    except Exception:
        return None


def _identify_missing_concepts(question: str, context_preview: str) -> List[str]:
    try:
        from .llm import chat as llm_chat

        prompt = (
            "Given the question and the available context preview, list missing concepts "
            "or subtopics that should be researched. Return a short comma-separated list.\n\n"
            f"Question: {question}\n"
            f"Context preview: {context_preview.strip()}"
        )
        raw = (llm_chat(prompt, "", max_tokens=80, temperature=0.2) or "").strip()
        if not raw:
            return []
        parts = [p.strip(" -•\t") for p in re.split(r"[\n,]", raw) if p.strip()]
        return parts[:6]
    except Exception:
        return []


def _group_context_blocks(
    *,
    local_contexts: List[str],
    url_contexts: List[str],
    web_contexts: List[str],
    missing_concepts: List[str],
    prioritize_web: bool = False,
) -> Tuple[str, str]:
    blocks: List[str] = []
    preview_parts: List[str] = []
    context_sections = []
    if web_contexts:
        context_sections.append(("=== WEB EVIDENCE ===\n", web_contexts))
    if url_contexts:
        context_sections.append(("=== USER URL EVIDENCE ===\n", url_contexts))
    if local_contexts:
        context_sections.append(("=== LOCAL KB EVIDENCE ===\n", local_contexts))

    if not prioritize_web:
        context_sections = list(reversed(context_sections))

    for label, contexts in context_sections:
        if contexts:
            block = "\n\n".join(contexts)
            blocks.append(label + block)
            preview_parts.append(contexts[0])
    if missing_concepts:
        missing_block = "\n".join(f"- {m}" for m in missing_concepts)
        blocks.append("=== MISSING CONCEPTS ===\n" + missing_block)
    full_ctx = "\n\n".join(blocks) if blocks else "(No relevant context found in your knowledge base.)"
    preview = "\n\n".join(preview_parts)[:1200]
    return full_ctx, preview


def _compute_source_confidence(local_hits: List[ChunkHit], web_hits: List[object], url_contexts: List[str]) -> Dict[str, float]:
    local_count = len(local_hits)
    local_docs = len({h.document_id for h in local_hits if h.document_id is not None})
    local_score = min(1.0, 0.1 + 0.08 * local_count + 0.12 * local_docs)
    web_score = min(1.0, 0.2 + 0.1 * len(web_hits)) if web_hits else 0.0
    url_score = min(1.0, 0.2 + 0.12 * len(url_contexts)) if url_contexts else 0.0
    return {
        "local": round(local_score, 2),
        "web": round(web_score, 2),
        "url": round(url_score, 2),
    }


def _rank_local_refs(local_hits: List[ChunkHit]) -> List[ChunkHit]:
    def score(hit: ChunkHit) -> float:
        if hit.distance is not None:
            return -float(hit.distance)
        if hit.rank is not None:
            return float(hit.rank)
        return 0.0

    return sorted(local_hits, key=score, reverse=True)


def _fetch_doc_recency_scores(hits: List[ChunkHit]) -> Dict[int, float]:
    if not hits:
        return {}
    doc_ids = sorted({int(h.document_id) for h in hits if h.document_id is not None})
    if not doc_ids:
        return {}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, created_at FROM documents WHERE id = ANY(%s)",
                (doc_ids,),
            )
            rows = cur.fetchall()
    now = time.time()
    half_life = max(float(settings.deep_research_recency_half_life_days or 0), 1.0) * 86400.0
    scores: Dict[int, float] = {}
    for doc_id, created_at in rows:
        if not created_at:
            scores[int(doc_id)] = 0.0
            continue
        age_seconds = max(0.0, now - created_at.timestamp())
        decay = math.exp(-math.log(2) * age_seconds / half_life)
        scores[int(doc_id)] = float(decay)
    return scores


def _rank_local_refs_with_recency(local_hits: List[ChunkHit]) -> List[ChunkHit]:
    if not local_hits:
        return []
    recency_scores = _fetch_doc_recency_scores(local_hits)
    boost = max(0.0, float(settings.deep_research_recency_boost or 0.0))

    def score(hit: ChunkHit) -> float:
        base = 0.0
        if hit.distance is not None:
            base = -float(hit.distance)
        elif hit.rank is not None:
            base = float(hit.rank)
        recency = recency_scores.get(int(hit.document_id), 0.0)
        return base + boost * recency

    return sorted(local_hits, key=score, reverse=True)


def _normalize_text(value: str) -> str:
    return re.sub(r"[^a-z0-9\s]", " ", (value or "").lower()).strip()


def _tokenize(value: str) -> List[str]:
    return [tok for tok in _normalize_text(value).split() if len(tok) > 1]


def _jaccard_similarity(left: str, right: str) -> float:
    lset = set(_tokenize(left))
    rset = set(_tokenize(right))
    if not lset or not rset:
        return 0.0
    return len(lset & rset) / len(lset | rset)


def _filter_followup_questions(
    questions: List[str],
    question: str,
    conversation_snippet: str,
    relevance_min: float,
) -> List[str]:
    if not questions:
        return []
    q_norm = _normalize_text(question)
    convo_norm = _normalize_text(conversation_snippet)
    seen = set()
    filtered: List[str] = []
    for item in questions:
        cand = item.strip()
        if not cand:
            continue
        norm = _normalize_text(cand)
        if not norm or norm in seen:
            continue
        if norm == q_norm:
            continue
        similarity = _jaccard_similarity(norm, q_norm)
        convo_similarity = _jaccard_similarity(norm, convo_norm) if convo_norm else 0.0
        if similarity < relevance_min and convo_similarity < relevance_min:
            continue
        seen.add(norm)
        filtered.append(cand)
    return filtered


def _generate_followup_questions(
    question: str,
    context_preview: str,
    max_questions: int,
    conversation_snippet: str = "",
) -> List[str]:
    if max_questions <= 0:
        return []
    try:
        from .llm import chat as llm_chat

        convo_block = f"Conversation so far:\n{conversation_snippet.strip()}\n\n" if conversation_snippet else ""
        prompt = (
            "Based on the conversation so far, ask clarifying follow-up questions that would help answer the user’s current request. "
            "Keep them short, specific, and tied to the user’s intent. Return a numbered list of up to "
            f"{max_questions} questions.\n\n"
            f"{convo_block}"
            f"Current question: {question}\n"
            f"Context preview: {context_preview.strip()}"
        )
        raw = (llm_chat(prompt, "", max_tokens=140, temperature=0.2) or "").strip()
        if not raw:
            return []
        lines = [re.sub(r"^\d+\.\s*", "", ln).strip() for ln in raw.splitlines() if ln.strip()]
        questions = [ln for ln in lines if ln.endswith("?") or len(ln) > 6]
        relevance_min = float(settings.deep_research_followup_relevance_min or 0.0)
        filtered = _filter_followup_questions(questions, question, conversation_snippet, relevance_min)
        return filtered[:max_questions]
    except Exception:
        return []


def _synthesize(question: str, contexts: List[str], provider_override: Optional[str], conv_context: Optional[str] = None) -> Optional[str]:
    try:
        from .llm import chat as llm_chat

        aggregated = "\n\n".join(contexts)[:16000]
        cc = (conv_context or "").strip()
        guardrails = (
            "You must ground every claim in the provided context. "
            "If the context is insufficient, explicitly say what is missing and avoid speculation. "
            "Cite the relevant evidence by referring to the section labels (LOCAL KB, USER URL, WEB)."
        )
        full_ctx = (("Conversation so far:\n" + cc + "\n\n") if cc else "") + guardrails + "\n\n" + aggregated
        return llm_chat(
            question,
            full_ctx,
            provider_override=provider_override,
            max_tokens=800,
            temperature=0.2,
        )
    except Exception as e:
        logger.exception("DR synth failed: %s", e)
        return None


def _refine(question: str, draft: str, contexts: List[str], provider_override: Optional[str], conv_context: Optional[str] = None) -> Optional[str]:
    try:
        from .llm import chat as llm_chat

        cc = (conv_context or "").strip()[:1200]
        ctx_blob = "\n\n".join(contexts)[:15000]
        conversation_block = f"Conversation so far (truncated):\n{cc}\n\n" if cc else ""
        guardrails = (
            "Ground each statement in the provided context. "
            "If evidence is missing or conflicting, say so clearly rather than guessing. "
            "Prefer concise, factual language."
        )
        prompt = (
            "Please refine and improve the following draft answer using the provided context and conversation so far.\n\n"
            f"Question: {question}\n\n"
            f"{conversation_block}"
            f"Draft Answer:\n{draft}\n\n{guardrails}\n\nContext:\n{ctx_blob}\n\n"
            "Return a concise, well-structured answer grounded in the context and consistent with the conversation."
        )
        return llm_chat(prompt, "", provider_override=provider_override, max_tokens=900, temperature=0.2)
    except Exception:
        return None


def ask(
    user_id: int,
    space_id: Optional[int],
    conversation_id: str,
    message: str,
    provider_override: Optional[str] = None,
    force_web: bool = False,
    urls: Optional[List[str]] = None,
    persistent_memory: Optional[bool] = None,
) -> Dict[str, object]:
    start_ts = time.monotonic()
    max_budget = max(float(settings.deep_research_timeout_seconds or 0), 15.0)
    retry_loops = max(0, int(settings.deep_research_retry_loops or 0))
    confidence_floor = float(settings.deep_research_confidence_threshold or 0.0)
    missing_loops = max(0, int(settings.deep_research_missing_concept_loops or 0))
    missing_top_k = max(1, int(settings.deep_research_missing_concept_top_k or 6))

    def _remaining_budget() -> float:
        elapsed = time.monotonic() - start_ts
        remaining = max_budget - elapsed
        return max(0.0, remaining)

    st = _load_state(user_id, space_id, conversation_id)
    st.messages.append(Message("user", message))

    recent = "\n".join(m.content for m in st.messages[-8:] if m.role in ("user", "assistant"))
    recent_snippet = recent[-1000:] if recent else ""

    retrieval_seed = f"{message}\n\nConversation so far:\n{recent_snippet}" if recent_snippet else message
    subqs = _extract_subqueries(retrieval_seed)

    persistent_memory_enabled = bool(getattr(settings, "deep_research_persistent_memory_enabled", False))
    if persistent_memory is not None:
        persistent_memory_enabled = bool(persistent_memory_enabled and persistent_memory)
    persistent_memory_context = ""
    if persistent_memory_enabled:
        try:
            entries = _fetch_persistent_memory(
                space_id or 0,
                "deep_research",
                message,
                top_k=int(settings.deep_research_persistent_memory_top_k),
            )
            persistent_memory_context = _build_persistent_memory_context(
                entries,
                int(settings.deep_research_persistent_memory_max_chars),
            )
        except Exception:
            persistent_memory_context = ""

    if urls:
        try:
            store_ensure_conversation(user_id, space_id, conversation_id, None)
            ingest_external_urls(
                user_id=user_id,
                space_id=space_id,
                conversation_id=conversation_id,
                urls=urls,
                recent_context=recent_snippet,
            )
        except Exception as exc:
            logger.warning("External URL ingestion failed: %s", exc)

    store_ensure_conversation(user_id, space_id, conversation_id, None)
    local_contexts: List[str] = []
    hits_all: List[ChunkHit] = []
    local_top_k = max(15, int(settings.deep_research_local_top_k or 15))
    for sq in subqs:
        try:
            hits = hybrid_search(sq, top_k=local_top_k, user_id=user_id, space_id=space_id)
            hits_all.extend(hits)
            if hits:
                local_contexts.append("\n\n".join(h.content for h in hits))
        except Exception as e:
            logger.warning("DR retrieve failed for %r: %s", sq, e)

    rewritten_query = None
    if _is_local_weak(hits_all):
        rewritten_query = _rewrite_for_search(message, recent_snippet or "")
        if rewritten_query:
            try:
                hits = hybrid_search(rewritten_query, top_k=local_top_k, user_id=user_id, space_id=space_id)
                hits_all.extend(hits)
                if hits:
                    local_contexts.append("\n\n".join(h.content for h in hits))
            except Exception as e:
                logger.warning("DR rewritten retrieve failed for %r: %s", rewritten_query, e)

    if not local_contexts:
        local_contexts.append("(No relevant context found in your knowledge base.)")
    if persistent_memory_context:
        local_contexts.insert(0, f"Persistent memory:\n{persistent_memory_context}")

    url_contexts = retrieve_external_contexts(
        user_id=user_id,
        space_id=space_id,
        conversation_id=conversation_id,
        query=retrieval_seed,
    )
    if not url_contexts:
        url_contexts = []

    search_query = rewritten_query or retrieval_seed
    web_hits: List[object] = []
    web_contexts: List[str] = []
    confidence = 0.0
    web_attempted = False
    for attempt in range(retry_loops + 1):
        combined = list(local_contexts) + list(url_contexts)
        contexts, web_hits, confidence, web_attempted = decide_web_and_contexts(
            search_query,
            hits_all,
            combined,
            max_seconds=_remaining_budget(),
            web_top_k=max(15, int(settings.deep_research_web_top_k or 15)),
            force_web=force_web or attempt > 0 or _is_local_weak(hits_all),
        )
        web_contexts = [c for c in contexts if c.startswith("Web result:")]
        local_weak = _is_local_weak(hits_all)
        if local_weak and web_contexts and local_contexts == ["(No relevant context found in your knowledge base.)"]:
            local_contexts = []

        if local_weak:
            full_context, preview = _group_context_blocks(
                local_contexts=local_contexts,
                url_contexts=url_contexts,
                web_contexts=web_contexts,
                missing_concepts=[],
                prioritize_web=local_weak,
            )
            missing = _identify_missing_concepts(message, preview)
            if missing:
                local_contexts.append("Missing concepts to cover: " + ", ".join(missing))

        if confidence >= confidence_floor and contexts and (web_contexts or not _is_local_weak(hits_all)):
            break
        if attempt < retry_loops:
            search_query = _rewrite_for_search(message, recent_snippet or "") or search_query

    missing_concepts: List[str] = []
    for _ in range(missing_loops):
        local_weak = _is_local_weak(hits_all)
        full_context, preview = _group_context_blocks(
            local_contexts=local_contexts,
            url_contexts=url_contexts,
            web_contexts=web_contexts,
            missing_concepts=missing_concepts,
            prioritize_web=local_weak,
        )
        new_missing = _identify_missing_concepts(message, preview)
        new_missing = [m for m in new_missing if m not in missing_concepts]
        if not new_missing:
            break
        missing_concepts.extend(new_missing)
        for concept in new_missing[:missing_top_k]:
            if _remaining_budget() <= 2:
                break
            try:
                hits = hybrid_search(concept, top_k=max(8, local_top_k // 2), user_id=user_id, space_id=space_id)
                hits_all.extend(hits)
                if hits:
                    local_contexts.append("\n\n".join(h.content for h in hits))
            except Exception as e:
                logger.warning("DR missing concept retrieve failed for %r: %s", concept, e)

    local_weak = _is_local_weak(hits_all)
    full_context, _ = _group_context_blocks(
        local_contexts=local_contexts,
        url_contexts=url_contexts,
        web_contexts=web_contexts,
        missing_concepts=missing_concepts,
        prioritize_web=local_weak,
    )

    draft = _synthesize(message, [full_context], provider_override, conv_context=recent_snippet)
    answer = draft or full_context[:1200]

    try:
        store_append_step(
            conversation_id=conversation_id,
            role="user",
            content=message,
            context_refs=None,
            metadata={"seed": retrieval_seed},
        )
    except Exception:
        logger.exception("Failed to persist DR user step")

    if draft and len(hits_all) > 0:
        refined = _refine(message, draft, [full_context], provider_override, conv_context=recent_snippet)
        if refined:
            answer = refined

    refs: List[Dict[str, object]] = []
    local_hits = _rank_local_refs_with_recency(hits_all)[:min(len(hits_all), max(5, int(settings.deep_research_local_top_k or 15)))]
    doc_meta: Dict[int, Dict[str, object]] = {}
    if local_hits:
        doc_ids = sorted({int(h.document_id) for h in local_hits})
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, COALESCE(title,''), source_path FROM documents WHERE id = ANY(%s)",
                    (doc_ids,),
                )
                for row in cur.fetchall():
                    doc_meta[int(row[0])] = {
                        "title": row[1] or "",
                        "source_path": row[2] or "",
                    }
    try:
        for idx, h in enumerate(local_hits, start=1):
            info = doc_meta.get(int(h.document_id), {})
            refs.append({
                "document_id": h.document_id,
                "chunk_id": h.chunk_id,
                "chunk_index": h.chunk_index,
                "source": "local",
                "title": info.get("title") or "",
                "source_path": info.get("source_path") or "",
                "excerpt": h.content,
                "rank": idx,
            })
        for idx, ctx in enumerate(url_contexts[:max(3, len(url_contexts))], start=1):
            refs.append({
                "source": "url",
                "snippet": ctx[:480],
                "rank": idx,
            })
        web_limit = max(5, int(settings.deep_research_web_top_k or 15))
        for idx, hit in enumerate(web_hits[:web_limit], start=1):
            refs.append({
                "source": "web",
                "title": hit.title,
                "url": hit.url,
                "snippet": hit.snippet,
                "rank": idx,
            })
    except Exception:
        pass

    source_confidence = _compute_source_confidence(local_hits, web_hits, url_contexts)

    followups: List[str] = []
    if settings.deep_research_followup_enable:
        should_prompt = confidence < float(settings.deep_research_followup_threshold or 0.0)
        user_turns = len([m for m in st.messages if m.role == "user"])
        is_first_turn = user_turns <= 1
        has_local_hits = len(hits_all) > 0
        allow_first_turn_prompt = is_first_turn and has_local_hits
        if should_prompt or allow_first_turn_prompt:
            preview = full_context[:1200]
            max_questions = int(settings.deep_research_followup_max_questions or 2)
            if allow_first_turn_prompt and not should_prompt:
                max_questions = min(max_questions, 1)
            followups = _generate_followup_questions(
                message,
                preview,
                max_questions=max_questions,
                conversation_snippet=recent_snippet,
            )

    st.messages.append(Message("assistant", answer))
    st.trim(keep=20)
    _save_state(st)

    memory_event_id = None
    if persistent_memory_enabled:
        try:
            memory_event_id = _persist_memory_event(
                space_id=space_id or 0,
                user_id=user_id,
                memory_type="deep_research",
                query_text=message,
                response_text=answer,
                metadata={
                    "conversation_id": conversation_id,
                    "web_attempted": web_attempted,
                    "confidence": confidence,
                },
            )
        except Exception:
            logger.exception("Failed to persist DR memory event")

    rollup_memory_id = None
    if persistent_memory_enabled and settings.deep_research_memory_rollup_enabled:
        rollup_threshold = int(settings.deep_research_memory_rollup_every_n)
        rollup_min_messages = int(settings.deep_research_memory_rollup_min_messages)
        if rollup_threshold > 0:
            step_count = len(st.messages)
            if step_count >= rollup_min_messages and step_count % rollup_threshold == 0:
                try:
                    detail = store_get_conversation_detail(user_id, conversation_id)
                    steps = detail.get("steps") or []
                    convo_lines: List[str] = []
                    for step in steps:
                        role = step.get("role") or ""
                        content = step.get("content") or ""
                        if not content:
                            continue
                        prefix = "User" if role == "user" else "Assistant" if role == "assistant" else "System"
                        convo_lines.append(f"{prefix}: {content.strip()}")
                    convo_text = "\n".join(convo_lines).strip()
                    if convo_text:
                        truncated = convo_text[: int(settings.deep_research_memory_rollup_max_chars)]
                        rollup_summary = _summarize_memory_text(
                            truncated,
                            int(settings.persistent_memory_summary_max_chars),
                        )
                        rollup_memory_id = _persist_memory_event(
                            space_id=space_id or 0,
                            user_id=user_id,
                            memory_type="deep_research",
                            query_text=f"DR session rollup ({conversation_id})",
                            response_text=rollup_summary,
                            metadata={
                                "conversation_id": conversation_id,
                                "rollup": True,
                                "message_count": step_count,
                            },
                        )
                except Exception:
                    logger.exception("Failed to persist DR rollup memory")

    try:
        store_append_step(
            conversation_id=conversation_id,
            role="assistant",
            content=answer,
            context_refs=refs,
            metadata={
                "confidence": confidence,
                "source_confidence": source_confidence,
                "followup_questions": followups,
                "web_attempted": web_attempted,
                "elapsed_seconds": round(time.monotonic() - start_ts, 2),
            },
        )
    except Exception:
        logger.exception("Failed to persist DR assistant step")

    return {
        "conversation_id": conversation_id,
        "answer": answer,
        "message_count": len(st.messages),
        "references": refs,
        "confidence": confidence,
        "source_confidence": source_confidence,
        "followup_questions": followups,
        "web_attempted": web_attempted,
        "elapsed_seconds": round(time.monotonic() - start_ts, 2),
        "memory_event_id": memory_event_id,
        "memory_rollup_event_id": rollup_memory_id,
        "persistent_memory_enabled": bool(persistent_memory_enabled),
    }
