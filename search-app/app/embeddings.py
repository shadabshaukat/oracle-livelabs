from __future__ import annotations

import logging
from functools import lru_cache
from typing import Iterable, List

from sentence_transformers import SentenceTransformer

from .config import settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_model() -> SentenceTransformer:
    logger.info("Loading embeddings model: %s", settings.embedding_model_name)
    model = SentenceTransformer(settings.embedding_model_name)
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
