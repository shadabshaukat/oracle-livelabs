from __future__ import annotations

import hashlib
import logging
import re
from collections import deque
from typing import Dict, Iterable, List, Optional, Set, Tuple

import requests
from bs4 import BeautifulSoup  # type: ignore

from .config import settings
from .db import get_conn
from .embeddings import embed_texts
from .text_utils import ChunkParams, chunk_text

logger = logging.getLogger(__name__)

USER_AGENT = "SearchApp-DeepResearch/1.0 (+https://github.com/shadabshaukat/oracle-livelabs)"
FETCH_TIMEOUT = 15
MAX_HTML_CHARS = 200_000
MIN_CONTENT_LEN = 120


def _normalize_url(url: str) -> Optional[str]:
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    return url.split("#", 1)[0]


def _same_domain(seed: str, candidate: str) -> bool:
    try:
        from urllib.parse import urlparse

        seed_host = urlparse(seed).netloc.lower()
        cand_host = urlparse(candidate).netloc.lower()
        return seed_host == cand_host or cand_host.endswith("." + seed_host)
    except Exception:
        return False


def _fetch(url: str) -> Tuple[str, str]:
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    resp = requests.get(url, headers=headers, timeout=FETCH_TIMEOUT)
    resp.raise_for_status()
    ct = resp.headers.get("content-type", "")
    if "text/html" not in ct and "application/xhtml" not in ct:
        raise ValueError("URL does not appear to be HTML content")
    text = resp.text
    if len(text) > MAX_HTML_CHARS:
        text = text[:MAX_HTML_CHARS]
    return text, resp.url


def _clean_text(html: str) -> Tuple[str, str, List[str]]:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else ""
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ").strip())
    links: List[str] = []
    for a in soup.find_all("a", href=True):
        href = a.get("href")
        if href:
            links.append(href)
    return text, title, links


def _chunk_and_embed(content: str) -> Tuple[List[str], List[List[float]]]:
    if not content or len(content) < MIN_CONTENT_LEN:
        return [], []
    cp = ChunkParams(settings.chunk_size, settings.chunk_overlap)
    chunks = chunk_text(content, cp)
    embeddings = embed_texts(chunks) if chunks else []
    return chunks, embeddings


def _upsert_external_chunk(
    *,
    user_id: int,
    space_id: Optional[int],
    conversation_id: str,
    url: str,
    parent_url: Optional[str],
    depth: int,
    chunk_index: int,
    title: str,
    chunk: str,
    snippet: str,
    metadata: Dict[str, str],
    embedding: List[float],
) -> None:
    digest = hashlib.sha1(chunk.encode("utf-8")).hexdigest()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO conversation_external_docs (
                    user_id, space_id, conversation_id,
                    url, parent_url, depth, chunk_index,
                    title, content, snippet, content_hash, metadata, embedding)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::vector)
                ON CONFLICT (user_id, conversation_id, url, chunk_index)
                DO UPDATE SET
                    content = EXCLUDED.content,
                    snippet = EXCLUDED.snippet,
                    metadata = EXCLUDED.metadata,
                    embedding = EXCLUDED.embedding,
                    updated_at = now()
                """,
                (
                    int(user_id),
                    int(space_id) if space_id is not None else None,
                    conversation_id,
                    url,
                    parent_url,
                    depth,
                    chunk_index,
                    title,
                    chunk,
                    snippet,
                    digest,
                    metadata,
                    embedding,
                ),
            )


def ingest_external_urls(
    *,
    user_id: int,
    space_id: Optional[int],
    conversation_id: str,
    urls: Iterable[str],
    recent_context: Optional[str] = None,
) -> None:
    seeds: List[str] = []
    for raw in urls:
        norm = _normalize_url(raw)
        if norm:
            seeds.append(norm)
    if not seeds:
        return

    max_depth = max(0, settings.deep_research_url_max_depth)
    max_pages = max(1, settings.deep_research_url_max_pages)

    frontier = deque([(seed, None, 0) for seed in seeds])
    seen: Set[str] = set()
    pages_processed = 0

    while frontier and pages_processed < max_pages:
        url, parent, depth = frontier.popleft()
        if url in seen or depth > max_depth:
            continue
        seen.add(url)
        try:
            html, final_url = _fetch(url)
            text, title, links = _clean_text(html)
            chunks, embeddings = _chunk_and_embed(text)
            for idx, (chunk, emb) in enumerate(zip(chunks, embeddings)):
                snippet = chunk[:320]
                metadata = {
                    "title": title,
                    "parent_url": parent or "",
                    "depth": depth,
                }
                _upsert_external_chunk(
                    user_id=user_id,
                    space_id=space_id,
                    conversation_id=conversation_id,
                    url=final_url,
                    parent_url=parent,
                    depth=depth,
                    chunk_index=idx,
                    title=title,
                    chunk=chunk,
                    snippet=snippet,
                    metadata=metadata,
                    embedding=emb,
                )
            pages_processed += 1
            base_url = final_url
            for link in links:
                absolute = _normalize_url(requests.compat.urljoin(base_url, link))
                if absolute and absolute not in seen and _same_domain(base_url, absolute):
                    frontier.append((absolute, final_url, depth + 1))
        except Exception as exc:
            logger.warning("Failed to ingest URL %s: %s", url, exc)
            continue


def retrieve_external_contexts(
    *,
    user_id: int,
    space_id: Optional[int],
    conversation_id: str,
    query: str,
    top_k: int = 6,
) -> List[str]:
    if not query.strip():
        return []
    embeddings = embed_texts([query])
    if not embeddings:
        return []
    emb = embeddings[0]
    from .pgvector_utils import to_vec_literal

    sql = [
        "SELECT url, title, snippet, content",
        "FROM conversation_external_docs",
        "WHERE user_id = %s AND conversation_id = %s",
    ]
    params: List[object] = [int(user_id), conversation_id]
    if space_id is not None:
        sql.append("AND (space_id = %s OR space_id IS NULL)")
        params.append(int(space_id))
    sql.append("ORDER BY embedding <=> %s::vector ASC LIMIT %s")
    params.extend([to_vec_literal(emb), int(top_k)])

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("\n".join(sql), params)
            rows = cur.fetchall()

    contexts: List[str] = []
    for row in rows:
        url, title, snippet, content = row
        title = title or ""
        snippet = snippet or content[:320]
        contexts.append(
            f"External URL: {title}\nURL: {url}\nSnippet: {snippet}\nContent:\n{content[:2000]}"
        )
    return contexts
