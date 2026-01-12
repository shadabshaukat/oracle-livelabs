from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import List, Tuple

from bs4 import BeautifulSoup
from pypdf import PdfReader
from docx import Document


SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


@dataclass
class ChunkParams:
    chunk_size: int = 1000
    chunk_overlap: int = 200
    # Optional custom separator order for recursive splitting
    separators: tuple[str, ...] = ("\n\n", "\n", ". ", " ", "")


def read_text_from_file(path: str) -> Tuple[str, str]:
    """
    Return (text, source_type) from a supported file.
    source_type: pdf|html|txt|docx
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
    raise ValueError(f"Unsupported file type: {ext}")


def extract_text_from_pdf(path: str) -> str:
    reader = PdfReader(path)
    texts: List[str] = []
    for page in reader.pages:
        txt = page.extract_text() or ""
        texts.append(txt)
    return "\n".join(texts)


def extract_text_from_html(path: str) -> str:
    with open(path, "rb") as f:
        data = f.read()
    soup = BeautifulSoup(data, "html.parser")
    return soup.get_text(separator=" ", strip=True)


def extract_text_from_txt(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def extract_text_from_docx(path: str) -> str:
    doc = Document(path)
    parts: List[str] = []
    for p in doc.paragraphs:
        text = p.text.strip()
        if text:
            parts.append(text)
    return "\n".join(parts)


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
    text = re.sub(r"\s+", " ", text).strip()
    base_chunks = _recursive_split(text, params.chunk_size, params.separators)
    if not base_chunks:
        return []
    return _apply_overlap(base_chunks, params.chunk_overlap)
