from __future__ import annotations

import logging
from typing import Optional

from .config import settings
_LLM_CACHE: dict[str, dict[str, str]] = {}

logger = logging.getLogger(__name__)


def _llm_cache_key(provider: str, question: str, context: str, max_tokens: int, temperature: float) -> str:
    import hashlib

    h = hashlib.sha256()
    h.update(provider.encode("utf-8"))
    h.update(b"|")
    h.update(question.strip().lower().encode("utf-8"))
    h.update(b"|")
    h.update(str(max_tokens).encode("utf-8"))
    h.update(b"|")
    h.update(f"{temperature:.2f}".encode("utf-8"))
    h.update(b"|")
    h.update(context.encode("utf-8"))
    return f"llm:{h.hexdigest()}"


def chat(
    question: str,
    context: str,
    provider_override: Optional[str] = None,
    max_tokens: int = 512,
    temperature: float = 0.2,
    *,
    cache_answer: bool = True,
) -> Optional[str]:
    provider = (provider_override or settings.llm_provider or "none").lower()

    cache_key = None
    if cache_answer and settings.llm_cache_ttl_seconds > 0:
        cache_key = _llm_cache_key(provider, question, context, max_tokens, temperature)
        cached = _LLM_CACHE.get(cache_key)
        if cached and isinstance(cached, dict) and "answer" in cached:
            logger.debug("llm cache hit for provider=%s", provider)
            return cached["answer"]

    if provider == "oci":
        try:
            from .oci_llm import oci_chat_completion

            return oci_chat_completion(question, context, max_tokens=max_tokens, temperature=temperature)
        except Exception as e:
            logger.exception("OCI LLM failed: %s", e)
            return None

    if provider == "openai":
        try:
            from openai import OpenAI  # type: ignore

            if not settings.openai_api_key:
                return None
            client = OpenAI(api_key=settings.openai_api_key)
            prompt = (
                "You are a helpful assistant. Using the provided context, answer the question concisely.\n\n"
                f"Question: {question}\n\nContext:\n{context[:12000]}"
            )
            resp = client.chat.completions.create(
                model=settings.openai_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            answer = resp.choices[0].message.content
            if cache_key:
                _LLM_CACHE[cache_key] = {"answer": answer}
            return answer
        except Exception as e:
            logger.exception("OpenAI LLM failed: %s", e)
            return None

    if provider == "bedrock":
        try:
            import boto3  # type: ignore
            import json

            model_id = (getattr(settings, "aws_bedrock_model_id", None) or "").strip() or "anthropic.claude-3-haiku-20240307-v1:0"
            region = getattr(settings, "aws_region", None) or "us-east-1"
            runtime = boto3.client("bedrock-runtime", region_name=region)

            def _provider(mid: str) -> str:
                mid = (mid or "").lower()
                if mid.startswith("anthropic."):
                    return "anthropic"
                if mid.startswith("meta."):
                    return "meta"
                if mid.startswith("mistral."):
                    return "mistral"
                if mid.startswith("cohere."):
                    return "cohere"
                if mid.startswith("amazon.") or "titan" in mid:
                    return "titan"
                return "unknown"

            sys_prompt = "You are a helpful RAG assistant. Answer from the provided context and cite sources when possible."
            prompt = (
                f"{sys_prompt}\n\nQuestion: {question}\n\nContext:\n{context[:12000]}\n\nAnswer:"
            )
            provider_tag = _provider(model_id)

            if provider_tag == "anthropic":
                body_dict = {
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": int(max_tokens),
                    "temperature": float(temperature),
                    "messages": [
                        {"role": "user", "content": [{"type": "text", "text": prompt}]}
                    ],
                }
            elif provider_tag == "meta":
                inst = f"[INST] <<SYS>>{sys_prompt}<</SYS>>\n{context[:12000]}\n\n{question} [/INST]"
                body_dict = {
                    "prompt": inst,
                    "max_gen_len": int(max_tokens),
                    "temperature": float(temperature),
                    "top_p": 0.95,
                }
            elif provider_tag == "mistral":
                body_dict = {
                    "prompt": prompt,
                    "max_tokens": int(max_tokens),
                    "temperature": float(temperature),
                    "top_p": 0.95,
                }
            elif provider_tag == "cohere":
                body_dict = {
                    "prompt": prompt,
                    "max_tokens": int(max_tokens),
                    "temperature": float(temperature),
                    "p": 0.95,
                    "top_p": 0.95,
                }
            else:
                body_dict = {
                    "inputText": prompt,
                    "textGenerationConfig": {
                        "temperature": float(temperature),
                        "topP": 0.95,
                        "maxTokenCount": int(max_tokens),
                    },
                }

            resp = runtime.invoke_model(
                modelId=model_id,
                body=json.dumps(body_dict),
                contentType="application/json",
                accept="application/json",
            )
            data = json.loads(resp["body"].read().decode("utf-8"))

            answer = None
            if provider_tag == "anthropic":
                try:
                    content = data.get("content") or []
                    if content and isinstance(content, list):
                        first = content[0]
                        if isinstance(first, dict):
                            answer = first.get("text")
                except Exception:
                    answer = None
            if not answer and isinstance(data.get("generation"), str):
                answer = data.get("generation")
            if not answer and isinstance(data.get("outputText"), str):
                answer = data.get("outputText")
            if not answer and isinstance(data.get("outputs"), list) and data["outputs"]:
                out0 = data["outputs"][0]
                if isinstance(out0, dict):
                    answer = out0.get("text") or out0.get("outputText")
            if not answer and isinstance(data.get("generations"), list) and data["generations"]:
                answer = data["generations"][0].get("text")

            if not answer:
                answer = str(data)
            if cache_key:
                _LLM_CACHE[cache_key] = {"answer": answer}
            return answer
        except Exception as e:
            logger.exception("Bedrock LLM failed: %s", e)
            return None

    if provider == "ollama":
        try:
            import requests  # type: ignore

            host = getattr(settings, "ollama_host", None) or "http://localhost:11434"
            model = getattr(settings, "ollama_model", None) or "llama3.2:latest"
            prompt = (
                "You are a helpful assistant. Using only the provided context, answer concisely.\n\n"
                f"Question: {question}\n\nContext:\n{context[:12000]}"
            )
            payload = {"model": model, "prompt": prompt, "stream": False, "options": {"temperature": temperature}}
            logger.info("llm[ollama]: generate (model=%s)", model)
            r = requests.post(f"{host}/api/generate", json=payload, timeout=60)
            r.raise_for_status()
            data = r.json()
            out = data.get("response") or data.get("output")
            logger.info("llm[ollama]: got answer=%s", bool(out))
            if cache_key and out:
                _LLM_CACHE[cache_key] = {"answer": out}
            return out
        except Exception as e:
            logger.exception("Ollama LLM failed: %s", e)
            return None

    return None
