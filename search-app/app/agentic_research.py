from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence

import requests
from bs4 import BeautifulSoup  # type: ignore

from .config import settings
from .search import ChunkHit

logger = logging.getLogger(__name__)


@dataclass
class WebHit:
    title: str
    url: str
    snippet: str

    def as_dict(self) -> dict:
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


class SmartResearchAgent:
    """Coordinates local context gathering and optional web lookups with simple heuristics."""

    def __init__(self, max_seconds: Optional[int] = None, web_top_k: Optional[int] = None, force_web: bool = False):
        timeout = max_seconds if max_seconds is not None else settings.deep_research_timeout_seconds
        timeout = max(5, min(int(timeout or 120), 180))
        self._deadline = time.monotonic() + timeout
        self.web_hits: List[WebHit] = []
        self.confidence: float = 0.0
        self.web_attempted: bool = False
        self.web_top_k = max(1, int(web_top_k or settings.deep_research_web_top_k or 8))
        self.force_web = bool(force_web)

    def time_remaining(self) -> float:
        return self._deadline - time.monotonic()

    def should_consider_web(self, hits: Sequence[ChunkHit]) -> bool:
        if self.force_web:
            return True
        if not hits:
            return True
        unique_docs = len({h.document_id for h in hits if h.document_id is not None})
        if unique_docs < 2 or len(hits) < 6:
            return True
        coverage = min(len(hits) / 8.0, 1.0)
        diversity = min(unique_docs / 5.0, 1.0)
        distance_scores = []
        for h in hits:
            if h.distance is not None and math.isfinite(h.distance):
                distance_scores.append(h.distance)
        semantic_quality = 0.0
        if distance_scores:
            best = min(distance_scores)
            if best <= 0:
                semantic_quality = 1.0
            else:
                semantic_quality = max(0.0, min(1.0, 1.0 - best))
        if semantic_quality and semantic_quality < 0.25:
            return True
        heuristic = 0.4 * coverage + 0.35 * diversity + 0.25 * semantic_quality
        logger.debug("DR heuristic coverage=%s diversity=%s semantic=%s total=%s", coverage, diversity, semantic_quality, heuristic)
        return heuristic < 0.45

    def _fetch_duckduckgo(self, query: str, limit: Optional[int] = None) -> List[WebHit]:
        url = "https://duckduckgo.com/html/"
        params = {"q": query, "kl": "us-en"}
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; SearchApp/1.0; +https://github.com/shadabshaukat/oracle-livelabs)"
        }
        resp = requests.get(url, params=params, headers=headers, timeout=min(8, max(3, self.time_remaining())))
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        results: List[WebHit] = []
        limit = limit or self.web_top_k

        def _normalize_href(raw: str) -> str:
            if not raw:
                return ""
            if raw.startswith("//"):
                return "https:" + raw
            if raw.startswith("/"):
                return "https://duckduckgo.com" + raw
            return raw

        def _extract_real_url(raw: str) -> str:
            if not raw:
                return ""
            if raw.startswith("/l/"):
                try:
                    from urllib.parse import urlparse, parse_qs
                    parsed = urlparse(raw)
                    params = parse_qs(parsed.query)
                    cand = params.get("uddg") or params.get("u")
                    if cand and cand[0]:
                        return cand[0]
                except Exception:
                    return raw
            if raw.startswith("https://duckduckgo.com/l/"):
                try:
                    from urllib.parse import urlparse, parse_qs
                    parsed = urlparse(raw)
                    params = parse_qs(parsed.query)
                    cand = params.get("uddg") or params.get("u")
                    if cand and cand[0]:
                        return cand[0]
                except Exception:
                    return raw
            return raw

        for a in soup.select("a.result__a"):
            title = (a.get_text(strip=True) or "(untitled)")
            raw_href = a.get("href") or ""
            href = _extract_real_url(_normalize_href(raw_href))
            snippet_el = a.find_next("a", class_="result__snippet")
            snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
            if not href:
                continue
            results.append(WebHit(title=title, url=href, snippet=snippet))
            if len(results) >= limit:
                break
        if results:
            return results

        lite_url = "https://duckduckgo.com/lite/"
        resp = requests.get(lite_url, params=params, headers=headers, timeout=min(8, max(3, self.time_remaining())))
        resp.raise_for_status()
        lite = BeautifulSoup(resp.text, "html.parser")
        for a in lite.select("a.result-link"):
            title = (a.get_text(strip=True) or "(untitled)")
            raw_href = a.get("href") or ""
            href = _extract_real_url(_normalize_href(raw_href))
            snippet_el = a.find_next("div", class_="result-snippet")
            snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
            if not href:
                continue
            results.append(WebHit(title=title, url=href, snippet=snippet))
            if len(results) >= limit:
                break
        return results

    def maybe_fetch_web(self, query: str) -> List[WebHit]:
        self.web_attempted = True
        if self.time_remaining() < 5 and not self.force_web:
            logger.info("DR agent skipping web search due to low remaining time")
            return []
        try:
            hits = self._fetch_duckduckgo(query)
            logger.info("DR agent fetched %d web hits", len(hits))
            self.web_hits = hits
        except Exception as exc:
            logger.warning("Web search failed: %s", exc)
            self.web_hits = []
        return self.web_hits

    def aggregate_contexts(self, local_contexts: List[str]) -> List[str]:
        contexts = list(local_contexts)
        if self.web_hits:
            for hit in self.web_hits:
                contexts.append(f"Web result: {hit.title}\nURL: {hit.url}\nSnippet: {hit.snippet}")
        return contexts

    def compute_confidence(self, local_hits: Sequence[ChunkHit]) -> float:
        unique_docs = len({h.document_id for h in local_hits if h.document_id is not None})
        coverage = min(len(local_hits) / 8.0, 1.0)
        diversity = min(unique_docs / 5.0, 1.0)
        base = 0.25 + 0.35 * coverage + 0.25 * diversity
        if self.web_hits:
            base += 0.15
        self.confidence = float(round(max(0.1, min(base, 0.98)), 2))
        return self.confidence


def decide_web_and_contexts(
    query: str,
    local_hits: Sequence[ChunkHit],
    local_contexts: List[str],
    max_seconds: Optional[float] = None,
    web_top_k: Optional[int] = None,
    force_web: bool = False,
) -> tuple[List[str], List[WebHit], float, bool]:
    budget = None
    if max_seconds is not None and max_seconds > 0:
        budget = int(max_seconds)
    agent = SmartResearchAgent(max_seconds=budget, web_top_k=web_top_k, force_web=force_web)
    if agent.should_consider_web(local_hits):
        agent.maybe_fetch_web(query)
    contexts = agent.aggregate_contexts(local_contexts)
    confidence = agent.compute_confidence(local_hits)
    return contexts, agent.web_hits, confidence, agent.web_attempted
