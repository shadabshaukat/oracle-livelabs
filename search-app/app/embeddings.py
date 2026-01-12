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
    logger.info("Loading embeddings model: %s", settings.embedding_model_name)
    # Ensure model cache directories are set for HF/Transformers
    os.makedirs(settings.model_cache_dir, exist_ok=True)
    os.environ.setdefault("HF_HOME", settings.model_cache_dir)
    os.environ.setdefault("TRANSFORMERS_CACHE", settings.model_cache_dir)
    os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", settings.model_cache_dir)
    os.environ.setdefault("HF_HUB_READ_TIMEOUT", "30")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "1")
    try:
        model = SentenceTransformer(settings.embedding_model_name, cache_folder=settings.model_cache_dir)
    except Exception as e:
        logger.warning("Model download failed (%s). Retrying with local_files_only=True", e)
        try:
            model = SentenceTransformer(
                settings.embedding_model_name,
                cache_folder=settings.model_cache_dir,
                local_files_only=True,
            )
        except Exception as e2:
            logger.exception("Failed to load embedding model offline as well: %s", e2)
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
