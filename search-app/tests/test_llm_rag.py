from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import llm, search


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _rag_settings(**overrides):
    values = {
        "pgvector_metric": "cosine",
        "rag_max_cosine_distance": 0.65,
        "rag_max_context_chars": 7000,
        "rag_top_k": 6,
        "rag_max_tokens": 512,
        "llm_provider": "ollama",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class OllamaClientTests(unittest.TestCase):
    def test_ollama_chat_uses_chat_api_and_bounded_options(self):
        post = Mock(return_value=_Response({"message": {"content": "  grounded answer  "}}))
        fake_requests = SimpleNamespace(post=post)
        fake_settings = SimpleNamespace(
            llm_provider="ollama",
            llm_cache_ttl_seconds=0,
            ollama_base_url="http://127.0.0.1:11434",
            ollama_model="ibm/granite4:1b-q4_K_M",
            ollama_keep_alive="-1",
            ollama_num_ctx=8192,
            ollama_timeout_seconds=300,
        )
        with patch.object(llm, "settings", fake_settings), patch.dict(sys.modules, {"requests": fake_requests}):
            answer = llm.chat("What is it?", "[Source 1]\nEvidence", max_tokens=123, temperature=0.1)

        self.assertEqual(answer, "grounded answer")
        url = post.call_args.args[0]
        kwargs = post.call_args.kwargs
        self.assertEqual(url, "http://127.0.0.1:11434/api/chat")
        self.assertEqual(kwargs["timeout"], (5, 300.0))
        payload = kwargs["json"]
        self.assertFalse(payload["stream"])
        self.assertFalse(payload["think"])
        self.assertEqual(payload["keep_alive"], -1)
        self.assertEqual(payload["options"]["num_predict"], 123)
        self.assertEqual(payload["options"]["num_ctx"], 8192)
        self.assertIn("[Source 1]", payload["messages"][1]["content"])

    def test_ollama_failure_returns_none(self):
        post = Mock(side_effect=RuntimeError("offline"))
        fake_settings = SimpleNamespace(
            llm_provider="ollama",
            llm_cache_ttl_seconds=0,
            ollama_base_url="http://127.0.0.1:11434",
            ollama_model="ibm/granite4:1b-q4_K_M",
            ollama_keep_alive="-1",
            ollama_num_ctx=8192,
            ollama_timeout_seconds=300,
        )
        with patch.object(llm, "settings", fake_settings), patch.dict(
            sys.modules, {"requests": SimpleNamespace(post=post)}
        ):
            self.assertIsNone(llm.chat("Question", "Context"))


class RagTests(unittest.TestCase):
    def test_hybrid_preserves_lexical_rank_on_duplicate_hit(self):
        semantic = search.ChunkHit(1, 2, 0, "same", distance=0.2)
        lexical = search.ChunkHit(1, 2, 0, "same", rank=0.8)
        with patch.object(search, "semantic_search", return_value=[semantic]), patch.object(
            search, "fulltext_search", return_value=[lexical]
        ):
            result = search.hybrid_search("query", top_k=1)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].distance, 0.2)
        self.assertEqual(result[0].rank, 0.8)

    def test_rag_filters_weak_hits_and_bounds_numbered_context(self):
        hits = [
            search.ChunkHit(1, 1, 0, "A" * 5000, distance=0.2),
            search.ChunkHit(2, 1, 1, "DROP_ME", distance=0.9),
            search.ChunkHit(3, 1, 2, "LEXICAL" * 800, distance=0.9, rank=0.4),
        ]
        captured = {}

        def fake_chat(question, context, **kwargs):
            captured["context"] = context
            return "grounded"

        with patch.object(search, "settings", _rag_settings()), patch.object(
            search, "hybrid_search", return_value=hits
        ) as retrieval, patch.object(search, "llm_chat", side_effect=fake_chat):
            answer, returned, used = search.rag("question", top_k=25)

        self.assertEqual(answer, "grounded")
        self.assertTrue(used)
        self.assertEqual([hit.chunk_id for hit in returned], [1, 3])
        self.assertLessEqual(len(captured["context"]), 7000)
        self.assertIn("[Source 1]", captured["context"])
        self.assertIn("[Source 2]", captured["context"])
        self.assertNotIn("DROP_ME", captured["context"])
        self.assertEqual(retrieval.call_args.kwargs["top_k"], 6)

    def test_rag_model_failure_never_returns_raw_context(self):
        body = "raw chunk body that must not become the answer"
        hits = [search.ChunkHit(1, 1, 0, body, distance=0.2)]
        with patch.object(search, "settings", _rag_settings()), patch.object(
            search, "hybrid_search", return_value=hits
        ), patch.object(search, "llm_chat", return_value=None):
            answer, returned, used = search.rag("question")
        self.assertFalse(used)
        self.assertEqual(len(returned), 1)
        self.assertNotIn(body, answer)
        self.assertIn("answer model is unavailable", answer)

    def test_rag_no_relevant_hits_skips_model(self):
        hits = [search.ChunkHit(1, 1, 0, "weak", distance=0.95)]
        with patch.object(search, "settings", _rag_settings()), patch.object(
            search, "hybrid_search", return_value=hits
        ), patch.object(search, "llm_chat") as chat:
            answer, returned, used = search.rag("question")
        self.assertFalse(used)
        self.assertEqual(returned, [])
        self.assertIn("couldn't find sufficiently relevant information", answer)
        chat.assert_not_called()


if __name__ == "__main__":
    unittest.main()
