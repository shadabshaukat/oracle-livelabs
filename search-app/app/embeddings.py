from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Iterable, List

from sentence_transformers import SentenceTransformer

from .config import settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_model() -> SentenceTransformer:
    logger.info(
        "Loading embeddings model: %s revision=%s",
        settings.embedding_model_name,
        settings.embedding_model_revision,
    )
    # Ensure model cache directories are set for HF/Transformers
    os.makedirs(settings.model_cache_dir, exist_ok=True)
    os.environ.setdefault("HF_HOME", settings.model_cache_dir)
    os.environ.setdefault("TRANSFORMERS_CACHE", settings.model_cache_dir)
    os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", settings.model_cache_dir)
    os.environ.setdefault("HF_HUB_READ_TIMEOUT", "30")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "1")
    try:
        # Prefer the already verified snapshot so normal runtime never checks a
        # mutable remote ref or needs network access.
        model = SentenceTransformer(
            settings.embedding_model_name,
            cache_folder=settings.model_cache_dir,
            revision=settings.embedding_model_revision,
            local_files_only=True,
        )
    except Exception as e:
        logger.info("Pinned embedding snapshot not cached (%s); downloading that exact revision", e)
        try:
            model = SentenceTransformer(
                settings.embedding_model_name,
                cache_folder=settings.model_cache_dir,
                revision=settings.embedding_model_revision,
            )
        except Exception as e2:
            logger.exception("Failed to load pinned embedding model revision: %s", e2)
            raise
    return model


def embed_texts(texts: Iterable[str], batch_size: int | None = None) -> List[list[float]]:
    model = get_model()
    bs = batch_size or settings.embedding_batch_size
    embs = model.encode(
        list(texts),
        batch_size=bs,
        convert_to_numpy=True,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    return [e.tolist() for e in embs]


def get_text_embedding_dim() -> int:
    """Return the dimensionality of the current text embedding model."""
    model = get_model()
    try:
        getter = getattr(model, "get_embedding_dimension", None)
        if callable(getter):
            return int(getter())
        return int(model.get_sentence_embedding_dimension())
    except Exception:
        sample = embed_texts(["dimension probe"], batch_size=1)
        return len(sample[0]) if sample else settings.embedding_dim
