from __future__ import annotations

import logging
import os
import shutil
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from functools import lru_cache
from pathlib import Path
from typing import Iterable, List, Optional

from .config import settings

logger = logging.getLogger(__name__)


class VisionModelUnavailable(RuntimeError):
    pass


class CaptioningModelUnavailable(RuntimeError):
    pass


class OcrUnavailable(RuntimeError):
    pass


def ocr_image_text(path: str) -> str:
    if not settings.ocr_enabled:
        return ""
    engine = (settings.ocr_engine or "tesseract").lower()
    if engine != "tesseract":
        raise OcrUnavailable(f"Unsupported OCR engine: {engine}")
    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore
    except ModuleNotFoundError as exc:
        raise OcrUnavailable(
            "pytesseract is not installed. Install with `pip install pytesseract` and ensure Tesseract is available."
        ) from exc
    tesseract_cmd = settings.ocr_tesseract_cmd
    if not tesseract_cmd:
        tesseract_cmd = shutil.which("tesseract")
    if not tesseract_cmd:
        for candidate in (
            "/opt/homebrew/bin/tesseract",
            "/usr/local/bin/tesseract",
            "/usr/bin/tesseract",
            "/bin/tesseract",
        ):
            if os.path.exists(candidate):
                tesseract_cmd = candidate
                break
    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
    try:
        image = Image.open(path).convert("RGB")
        data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
        words = []
        confs = data.get("conf", [])
        texts = data.get("text", [])
        for conf, word in zip(confs, texts):
            try:
                conf_val = float(conf)
            except (TypeError, ValueError):
                conf_val = -1.0
            cleaned_word = (word or "").strip()
            if conf_val > 0 and cleaned_word:
                words.append(cleaned_word)
        if not words:
            return ""
        text = " ".join(words)
    except Exception as exc:
        hint = ""
        if "not installed" in str(exc).lower() or "not in your path" in str(exc).lower():
            hint = " Set OCR_TESSERACT_CMD to the absolute binary path (e.g. /opt/homebrew/bin/tesseract)."
        raise OcrUnavailable(f"{exc}{hint}") from exc
    if not text:
        return ""
    cleaned = " ".join(text.split())
    if settings.ocr_min_chars and len(cleaned) < settings.ocr_min_chars:
        return ""
    if settings.ocr_max_chars and len(cleaned) > settings.ocr_max_chars:
        cleaned = cleaned[: settings.ocr_max_chars]
    return cleaned


@lru_cache(maxsize=1)
def _get_clip_model():
    try:
        import open_clip  # type: ignore
    except ModuleNotFoundError as exc:
        raise VisionModelUnavailable(
            "open_clip is not installed. Install extras with `uv sync --extra image` or `pip install .[image]`"
        ) from exc
    model_name = settings.image_embed_model
    variant = model_name.split("/")[-1]
    cache_dir = Path(settings.model_cache_dir) / "vision"
    cache_dir.mkdir(parents=True, exist_ok=True)
    pretrained = "openai" if "openclip" in model_name else "laion2b_s34b_b79k"
    model, _preprocess_train, preprocess = open_clip.create_model_and_transforms(
        variant,
        pretrained=pretrained,
        cache_dir=str(cache_dir),
        device=settings.image_embed_device,
    )
    try:
        tokenizer = open_clip.get_tokenizer(variant)
    except Exception as exc:
        logger.warning("open_clip.get_tokenizer failed for %s: %s", variant, exc)
        tokenizer = getattr(open_clip, "tokenize", None)
    if tokenizer is None:
        raise RuntimeError(f"open_clip tokenizer unavailable for model {variant}")
    logger.info("Loaded image embedding model %s on %s", model_name, settings.image_embed_device)
    return model, preprocess, tokenizer


@lru_cache(maxsize=1)
def _get_clip_text_tokenizer():
    try:
        from open_clip.simple_tokenizer import SimpleTokenizer  # type: ignore

        return SimpleTokenizer()
    except Exception as exc:
        logger.warning("SimpleTokenizer unavailable: %s", exc)
        return None


def vision_dependencies_ready(preload_model: bool = False) -> tuple[bool, str | None]:
    try:
        import PIL  # type: ignore  # noqa: F401
    except ModuleNotFoundError as exc:
        return False, "Pillow is not installed. Install extras with `uv sync --extra image` or `pip install .[image]`."
    try:
        if preload_model:
            _get_clip_model()
    except VisionModelUnavailable as e:
        return False, str(e)
    except Exception as e:
        return False, f"Vision model initialization failed: {e}"
    return True, None


def embed_image_paths(paths: Iterable[str]) -> List[List[float]]:
    paths = list(paths)
    if not paths:
        return []
    model, preprocess, _ = _get_clip_model()
    import torch  # type: ignore
    from PIL import Image  # type: ignore

    device = settings.image_embed_device
    model.eval()
    embeddings: List[List[float]] = []
    with torch.no_grad():
        for path in paths:
            img = Image.open(path).convert("RGB")
            tensor = preprocess(img).unsqueeze(0).to(device)
            vec = model.encode_image(tensor)
            vec /= vec.norm(dim=-1, keepdim=True)
            embeddings.append(vec.squeeze(0).cpu().tolist())
    return embeddings


def embed_image_texts(texts: Iterable[str]) -> List[List[float]]:
    texts = [t.strip() for t in texts if t and t.strip()]
    if not texts:
        return []
    model, _preprocess, tokenizer = _get_clip_model()
    clip_tokenizer = _get_clip_text_tokenizer()
    import torch  # type: ignore

    device = settings.image_embed_device
    model.eval()
    with torch.no_grad():
        try:
            tokens = tokenizer(texts)
        except TypeError:
            if clip_tokenizer is None:
                raise
            encoded = [torch.tensor(clip_tokenizer.encode(text, truncate=True)) for text in texts]
            max_len = max(tok.shape[0] for tok in encoded)
            padded = []
            for tok in encoded:
                if tok.shape[0] < max_len:
                    pad = torch.zeros(max_len - tok.shape[0], dtype=tok.dtype)
                    tok = torch.cat([tok, pad], dim=0)
                padded.append(tok)
            tokens = torch.stack(padded, dim=0)
        tokens = tokens.to(device)
        vecs = model.encode_text(tokens)
        vecs /= vecs.norm(dim=-1, keepdim=True)
    return vecs.cpu().tolist()


def get_image_embedding_dim() -> int:
    """Return the dimensionality of the current image embedding model."""
    try:
        model, _preprocess, _ = _get_clip_model()
        dim = getattr(model, "visual", None)
        if dim is not None and hasattr(dim, "output_dim"):
            return int(dim.output_dim)
        if hasattr(model, "embed_dim"):
            return int(model.embed_dim)
    except Exception as exc:
        logger.warning("Failed to read image embedding dim: %s", exc)
    try:
        sample = embed_image_texts(["dimension probe"])
        return len(sample[0]) if sample else settings.image_embed_dim
    except Exception:
        return settings.image_embed_dim


@lru_cache(maxsize=1)
def _load_caption_model():
    if not settings.enable_image_captioning:
        raise CaptioningModelUnavailable("Image captioning disabled")
    model_name = settings.image_caption_model_small if settings.image_caption_use_small else settings.image_caption_model
    try:
        import torch  # type: ignore
        from transformers import AutoProcessor, LlavaForConditionalGeneration, BlipForConditionalGeneration  # type: ignore
    except ModuleNotFoundError as exc:
        raise CaptioningModelUnavailable(
            "captioning dependencies are not installed. Install extras with `uv sync --extra image` or `pip install .[image]`"
        ) from exc
    device = settings.image_caption_device
    torch_dtype = torch.float16 if device != "cpu" else torch.float32
    processor = AutoProcessor.from_pretrained(model_name)
    if settings.image_caption_use_small:
        model = BlipForConditionalGeneration.from_pretrained(
            model_name,
            torch_dtype=torch_dtype,
            low_cpu_mem_usage=True,
        )
    else:
        model = LlavaForConditionalGeneration.from_pretrained(
            model_name,
            torch_dtype=torch_dtype,
            low_cpu_mem_usage=True,
        )
    model.to(device)
    model.eval()
    logger.info("Loaded image caption model %s on %s", model_name, device)
    return model, processor


def _build_caption_prompt(processor, prompt: str) -> str:
    if hasattr(processor, "apply_chat_template"):
        return processor.apply_chat_template(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image"},
                    ],
                }
            ],
            tokenize=False,
            add_generation_prompt=True,
        )
    return f"USER: <image>\n{prompt}\nASSISTANT:"


def generate_image_caption(path: str) -> str:
    if not settings.enable_image_captioning:
        return ""

    def _run() -> Optional[str]:
        from PIL import Image  # type: ignore

        model, processor = _load_caption_model()
        prompt = settings.image_caption_prompt
        image = Image.open(path).convert("RGB")
        if settings.image_caption_use_small:
            inputs = processor(images=image, return_tensors="pt")
        else:
            prompt = _build_caption_prompt(processor, prompt)
            inputs = processor(images=image, text=prompt, return_tensors="pt")
        device = settings.image_caption_device
        for key in inputs:
            inputs[key] = inputs[key].to(device)
        output = model.generate(**inputs, max_new_tokens=settings.image_caption_max_tokens)
        decoded = processor.decode(output[0], skip_special_tokens=True).strip()
        if "ASSISTANT:" in decoded:
            decoded = decoded.split("ASSISTANT:", 1)[-1].strip()
        return decoded or None

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_run)
            result = future.result(timeout=settings.image_caption_timeout_s)
            return (result or "").strip()
    except TimeoutError:
        logger.warning("Image captioning timed out after %ss", settings.image_caption_timeout_s)
        return ""
    except CaptioningModelUnavailable:
        raise
    except Exception as exc:
        raise CaptioningModelUnavailable(str(exc)) from exc