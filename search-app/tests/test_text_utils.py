from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import text_utils


class _Page:
    def __init__(self, text: str):
        self._text = text

    def extract_text(self):
        return self._text


class PdfOcrFallbackTests(unittest.TestCase):
    def _settings(self):
        return SimpleNamespace(
            use_pymupdf=False,
            ocr_pdf_enabled=True,
            pdf_ocr_fallback_min_chars=200,
        )

    def test_native_pdf_text_does_not_run_ocr(self):
        reader = SimpleNamespace(pages=[_Page("Native text. " * 40)])
        with patch.object(text_utils, "settings", self._settings()), patch.object(
            text_utils, "PdfReader", return_value=reader
        ), patch.object(text_utils, "_ocr_pdf_pages") as ocr:
            result = text_utils.extract_text_from_pdf("example.pdf")
        self.assertIn("Native text", result)
        ocr.assert_not_called()

    def test_sparse_pdf_uses_ocr_fallback(self):
        reader = SimpleNamespace(pages=[_Page("")])
        with patch.object(text_utils, "settings", self._settings()), patch.object(
            text_utils, "PdfReader", return_value=reader
        ), patch.object(text_utils, "_ocr_pdf_pages", return_value="Scanned page content") as ocr:
            result = text_utils.extract_text_from_pdf("scan.pdf")
        self.assertIn("Scanned page content", result)
        ocr.assert_called_once_with("scan.pdf")


if __name__ == "__main__":
    unittest.main()
