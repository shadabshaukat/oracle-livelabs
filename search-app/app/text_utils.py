from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import List, Tuple
import csv
import json

from bs4 import BeautifulSoup
from pypdf import PdfReader
from docx import Document
from .config import settings


# Prefer keeping paragraph boundaries; avoid collapsing all newlines into spaces
PARA_SPLIT_RE = re.compile(r"(?:\r?\n){2,}")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
UPPER_HEADING_RE = re.compile(r"^[A-Z0-9][A-Z0-9 \-:]{2,}$")
NUMBERED_HEADING_RE = re.compile(r"^(?:[IVXLCDM]+\.|\d+(?:\.\d+)*\.|[A-Z]\.)\s+.+")
PAGE_FOOTER_RE = re.compile(r"^\s*page\s+\d+(?:\s+of\s+\d+)?\s*$", re.I)


@dataclass
class ChunkParams:
    chunk_size: int = 2500
    chunk_overlap: int = 250
    # Optional custom separator order for recursive splitting
    separators: tuple[str, ...] = ("\n\n", "\n", ". ", " ", "")


def _normalize_whitespace_preserve_paragraphs(text: str) -> str:
    """Normalize whitespace but preserve blank lines as paragraph boundaries."""
    # Normalize line endings
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Collapse more than two consecutive newlines to exactly two
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Normalize spaces within lines
    lines = []
    for ln in text.split("\n"):
        ln = re.sub(r"\s+", " ", ln).strip()
        lines.append(ln)
    text = "\n".join(lines)
    # Restore paragraph boundaries
    text = re.sub(r"(\n\s*){3,}", "\n\n", text)
    return text.strip()


def _fix_hyphenation(text: str) -> str:
    """Fix common PDF hyphenation like 'exam-\nple' -> 'example'."""
    # Join words broken by hyphen at line end
    text = re.sub(r"-\n(?=\w)", "", text)
    # Remove lone hyphens surrounded by newlines
    text = re.sub(r"\n-\n", "\n", text)
    # Replace single newlines inside paragraphs with spaces (but keep double newlines)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
    return text


def _insert_heading_boundaries(text: str) -> str:
    """Insert extra blank lines around detected headings to improve chunk boundaries."""
    out_lines: List[str] = []
    for ln in text.split("\n"):
        if UPPER_HEADING_RE.match(ln) or NUMBERED_HEADING_RE.match(ln):
            if out_lines and out_lines[-1] != "":
                out_lines.append("")
            out_lines.append(ln)
            out_lines.append("")
        else:
            out_lines.append(ln)
    return "\n".join(out_lines)


def _remove_common_headers_footers(pages: List[str]) -> List[str]:
    """Heuristic removal of repeating headers/footers across pages."""
    if not pages or len(pages) < 3:
        return pages
    # Collect first and last non-empty lines per page
    first_lines: List[str] = []
    last_lines: List[str] = []
    for p in pages:
        ls = [l.strip() for l in p.split("\n") if l.strip()]
        if not ls:
            first_lines.append("")
            last_lines.append("")
            continue
        first_lines.append(ls[0])
        last_lines.append(ls[-1])
    def most_common(cand: List[str]) -> str:
        from collections import Counter
        c = Counter([x for x in cand if x])
        return c.most_common(1)[0][0] if c else ""
    first_common = most_common(first_lines)
    last_common = most_common(last_lines)
    cleaned_pages: List[str] = []
    for p in pages:
        ls = p.split("\n")
        if first_common:
            ls = ls[1:] if ls and ls[0].strip() == first_common else ls
        if last_common:
            if ls and ls[-1].strip() == last_common:
                ls = ls[:-1]
        # Remove generic page footers like "Page X of Y"
        ls = [l for l in ls if not PAGE_FOOTER_RE.match(l.strip())]
        cleaned_pages.append("\n".join(ls))
    return cleaned_pages


def read_text_from_file(path: str) -> Tuple[str, str]:
    """
    Return (text, source_type) from a supported file.
    source_type: pdf|html|txt|docx|xml|csv|md|json
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return extract_text_from_pdf(path), "pdf"
    if ext in {".html", ".htm"}:
        return extract_text_from_html(path), "html"
    if ext == ".docx":
        return extract_text_from_docx(path), "docx"
    if ext in {".txt", ""}:
        return extract_text_from_txt(path), "txt"
    if ext == ".xml":
        return extract_text_from_xml(path), "xml"
    if ext == ".csv":
        return extract_text_from_csv(path), "csv"
    if ext == ".md":
        return extract_text_from_md(path), "md"
    if ext == ".json":
        return extract_text_from_json(path), "json"
    # Fallback: read as text if possible
    try:
        return extract_text_from_txt(path), ext.lstrip('.') or 'txt'
    except Exception:
        raise ValueError(f"Unsupported file type: {ext}")


def extract_text_from_pdf(path: str) -> str:
    """Robust PDF extraction.
    Order of preference:
      1) PyMuPDF (if enabled): page.get_text("text"); remove common headers/footers; hyphenation fix; preserve paragraphs
      2) pypdf: page.extract_text(); hyphenation fix; preserve paragraphs
      3) pdfplumber fallback for table/figure-heavy PDFs when pypdf output is sparse
    """
    # Optional: use PyMuPDF if enabled and available for better extraction
    if getattr(settings, "use_pymupdf", False):
        try:
            import fitz  # PyMuPDF
            pages_raw: List[str] = []
            with fitz.open(path) as doc:
                for page in doc:
                    # Use textual extraction; "text" preserves reading order better than "blocks" in many docs
                    t = page.get_text("text") or ""
                    pages_raw.append(t)
            # Remove common headers/footers
            pages_clean = _remove_common_headers_footers(pages_raw)
            text = "\n\n".join(pages_clean)
            text = _fix_hyphenation(text)
            text = _normalize_whitespace_preserve_paragraphs(text)
            # Insert heading boundaries to help chunking
            text = _insert_heading_boundaries(text)
            return text
        except Exception:
            # Fall back to other extractors if PyMuPDF is not available or fails
            pass

    # pypdf extraction
    reader = PdfReader(path)
    texts_pypdf: List[str] = []
    try:
        for page in reader.pages:
            txt = page.extract_text() or ""
            texts_pypdf.append(txt)
    except Exception:
        texts_pypdf = []
    text_pypdf = "\n\n".join(texts_pypdf)
    text_pypdf = _fix_hyphenation(text_pypdf)
    text_pypdf = _normalize_whitespace_preserve_paragraphs(text_pypdf)
    text_pypdf = _insert_heading_boundaries(text_pypdf)

    # Decide if we should try pdfplumber fallback (very sparse output or extremely short)
    needs_fallback = len(text_pypdf.strip()) < 200 or text_pypdf.count("\n") < max(2, len(texts_pypdf) // 4)
    if needs_fallback:
        try:
            import pdfplumber  # type: ignore
            with pdfplumber.open(path) as pdf:
                pages_text = []
                for page in pdf.pages:
                    # Tolerances can help capture columns/tables better
                    t = page.extract_text(x_tolerance=1, y_tolerance=1) or ""
                    pages_text.append(t)
            text_plumb = "\n\n".join(pages_text)
            text_plumb = _fix_hyphenation(text_plumb)
            text_plumb = _normalize_whitespace_preserve_paragraphs(text_plumb)
            text_plumb = _insert_heading_boundaries(text_plumb)
            # Prefer the better (longer, more structured) output
            if len(text_plumb.strip()) > len(text_pypdf.strip()):
                return text_plumb
        except Exception:
            # If pdfplumber fails, keep pypdf output
            pass

    return text_pypdf


def extract_text_from_html(path: str) -> str:
    with open(path, "rb") as f:
        data = f.read()
    soup = BeautifulSoup(data, "html.parser")
    # Remove nav-like elements
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    text = _normalize_whitespace_preserve_paragraphs(text)
    return text


def extract_text_from_xml(path: str) -> str:
    with open(path, "rb") as f:
        data = f.read()
    soup = BeautifulSoup(data, "xml")
    text = soup.get_text(separator="\n", strip=True)
    text = _normalize_whitespace_preserve_paragraphs(text)
    return text


def extract_text_from_txt(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()
    text = _normalize_whitespace_preserve_paragraphs(text)
    return text


def extract_text_from_csv(path: str) -> str:
    parts: List[str] = []
    with open(path, newline="", encoding="utf-8", errors="ignore") as f:
        reader = csv.reader(f)
        for row in reader:
            parts.append(" \t ".join(cell.strip() for cell in row if cell))
    text = "\n".join(parts)
    text = _normalize_whitespace_preserve_paragraphs(text)
    return text


def extract_text_from_md(path: str) -> str:
    # Light-weight: treat as plain text (could add markdown parsing if needed)
    return extract_text_from_txt(path)


def extract_text_from_json(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            data = json.load(f)
        # Convert JSON to a flat text string
        def _flatten(obj) -> List[str]:
            out: List[str] = []
            if isinstance(obj, dict):
                for k, v in obj.items():
                    out.append(str(k))
                    out.extend(_flatten(v))
            elif isinstance(obj, list):
                for it in obj:
                    out.extend(_flatten(it))
            else:
                out.append(str(obj))
            return out
        parts = _flatten(data)
        text = "\n".join(s.strip() for s in parts if s and isinstance(s, str))
        text = _normalize_whitespace_preserve_paragraphs(text)
        return text
    except Exception:
        # Fall back to raw text if not valid JSON
        return extract_text_from_txt(path)


def extract_text_from_docx(path: str) -> str:
    doc = Document(path)
    parts: List[str] = []
    for p in doc.paragraphs:
        text = p.text.strip()
        if text:
            parts.append(text)
    text = "\n\n".join(parts)
    text = _normalize_whitespace_preserve_paragraphs(text)
    return text


def _recursive_split(text: str, chunk_size: int, separators: tuple[str, ...]) -> List[str]:
    if not text:
        return []
    if len(text) <= chunk_size or not separators:
        return [text]

    sep = separators[0]
    if sep:
        pieces = text.split(sep)
        rebuilt: List[str] = []
        buf = ""
        joiner = sep
        for piece in pieces:
            candidate = (buf + joiner + piece) if buf else piece
            if len(candidate) <= chunk_size:
                buf = candidate
            else:
                if buf:
                    rebuilt.append(buf)
                if len(piece) <= chunk_size:
                    buf = piece
                else:
                    rebuilt.extend(_recursive_split(piece, chunk_size, separators[1:]))
                    buf = ""
        if buf:
            rebuilt.append(buf)
        return rebuilt
    else:
        return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


def _apply_overlap(chunks: List[str], overlap: int) -> List[str]:
    if overlap <= 0 or not chunks:
        return chunks
    out: List[str] = []
    prev_tail = ""
    for ch in chunks:
        prefix = prev_tail
        combined = (prefix + ch) if prefix else ch
        out.append(combined)
        prev_tail = ch[-overlap:]
    return out


def chunk_text(text: str, params: ChunkParams = ChunkParams()) -> List[str]:
    # Normalize while preserving paragraph boundaries; add extra spacing around likely headings
    text = _normalize_whitespace_preserve_paragraphs(text)
    text = _insert_heading_boundaries(text)
    base_chunks = _recursive_split(text, params.chunk_size, params.separators)
    if not base_chunks:
        return []
    return _apply_overlap(base_chunks, params.chunk_overlap)
