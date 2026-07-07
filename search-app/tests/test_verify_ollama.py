from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts import verify_ollama


MODEL = verify_ollama.DEFAULT_MODEL
DIGEST = verify_ollama.DEFAULT_DIGEST
VERSION = verify_ollama.DEFAULT_VERSION


def _tags() -> dict:
    return {
        "models": [
            {
                "name": MODEL,
                "model": MODEL,
                "digest": DIGEST,
                "details": {"quantization_level": "Q4_K_M"},
            }
        ]
    }


def _resident(context_length: int = 8192) -> dict:
    return {
        "models": [
            {
                "name": MODEL,
                "model": MODEL,
                "digest": DIGEST,
                "context_length": context_length,
            }
        ]
    }


class VerifyOllamaTests(unittest.TestCase):
    @patch.object(verify_ollama, "_json_request")
    def test_already_loaded_does_not_send_preload(self, request) -> None:
        def response(url: str, **kwargs):
            self.assertIsNone(kwargs.get("payload"))
            if url.endswith("/api/version"):
                return {"version": VERSION}
            if url.endswith("/api/tags"):
                return _tags()
            if url.endswith("/api/ps"):
                return _resident()
            self.fail(f"Unexpected URL: {url}")

        request.side_effect = response
        verify_ollama.verify("http://127.0.0.1:11434", VERSION, MODEL, DIGEST, ensure_loaded=True)
        self.assertEqual(request.call_count, 3)

    @patch.object(verify_ollama, "_json_request")
    def test_unloaded_model_is_preloaded_and_verified(self, request) -> None:
        ps_responses = iter([{"models": []}, _resident()])
        generate_payloads: list[dict] = []

        def response(url: str, **kwargs):
            if url.endswith("/api/version"):
                return {"version": VERSION}
            if url.endswith("/api/tags"):
                return _tags()
            if url.endswith("/api/ps"):
                return next(ps_responses)
            if url.endswith("/api/generate"):
                generate_payloads.append(kwargs["payload"])
                return {"done": True}
            self.fail(f"Unexpected URL: {url}")

        request.side_effect = response
        with patch.dict("os.environ", {"OLLAMA_KEEP_ALIVE": "-1", "OLLAMA_NUM_CTX": "8192"}):
            verify_ollama.verify("http://127.0.0.1:11434", VERSION, MODEL, DIGEST, ensure_loaded=True)

        self.assertEqual(len(generate_payloads), 1)
        self.assertEqual(generate_payloads[0]["keep_alive"], -1)
        self.assertEqual(generate_payloads[0]["options"], {"num_ctx": 8192})

    @patch.object(verify_ollama, "_json_request")
    def test_wrong_context_is_unloaded_then_reloaded(self, request) -> None:
        ps_responses = iter([_resident(4096), _resident(8192)])
        generate_payloads: list[dict] = []

        def response(url: str, **kwargs):
            if url.endswith("/api/version"):
                return {"version": VERSION}
            if url.endswith("/api/tags"):
                return _tags()
            if url.endswith("/api/ps"):
                return next(ps_responses)
            if url.endswith("/api/generate"):
                generate_payloads.append(kwargs["payload"])
                return {"done": True}
            self.fail(f"Unexpected URL: {url}")

        request.side_effect = response
        with patch.dict("os.environ", {"OLLAMA_KEEP_ALIVE": "-1", "OLLAMA_NUM_CTX": "8192"}):
            verify_ollama.verify("http://127.0.0.1:11434", VERSION, MODEL, DIGEST, ensure_loaded=True)

        self.assertEqual([payload["keep_alive"] for payload in generate_payloads], [0, -1])


if __name__ == "__main__":
    unittest.main()
