from __future__ import annotations

import inspect
import logging
from typing import Optional

from .config import settings

logger = logging.getLogger(__name__)


def _build_oci_clients():
    try:
        import oci
        from oci.generative_ai_inference import GenerativeAiInferenceClient
    except Exception as e:
        logger.error("OCI SDK not available: %s", e)
        return None, None

    config = None
    signer = None

    # Config-file auth
    if settings.oci_config_file:
        try:
            import oci
            config = oci.config.from_file(settings.oci_config_file, settings.oci_config_profile)
            if settings.oci_region:
                config["region"] = settings.oci_region
            client = GenerativeAiInferenceClient(config=config, service_endpoint=settings.oci_genai_endpoint)
            logger.info("OCI client initialized via config file (profile=%s)", settings.oci_config_profile)
            return client, None
        except Exception as e:
            logger.exception("Failed to init OCI client from config file: %s", e)

    # API-key signer auth
    try:
        if not all([
            settings.oci_tenancy_ocid,
            settings.oci_user_ocid,
            settings.oci_fingerprint,
            settings.oci_private_key_path,
            settings.oci_region,
        ]):
            raise ValueError("Missing OCI API key envs (TENANCY, USER, FINGERPRINT, PRIVATE_KEY_PATH, REGION)")
        import oci
        signer = oci.signer.Signer(
            tenancy=settings.oci_tenancy_ocid,
            user=settings.oci_user_ocid,
            fingerprint=settings.oci_fingerprint,
            private_key_file_location=settings.oci_private_key_path,
            pass_phrase=settings.oci_private_key_passphrase,
        )
        client = GenerativeAiInferenceClient(
            config={"region": settings.oci_region}, signer=signer, service_endpoint=settings.oci_genai_endpoint
        )
        logger.info("OCI client initialized via API key signer (region=%s)", settings.oci_region)
        return client, signer
    except Exception as e:
        logger.exception("Failed to init OCI client via API key signer: %s", e)
        return None, None


def _safe_build(model_cls, **kwargs):
    """Construct model by filtering kwargs to supported parameters (SDK compatibility)."""
    try:
        sig = inspect.signature(model_cls.__init__)
        allowed = {k: v for k, v in kwargs.items() if k in sig.parameters}
        return model_cls(**allowed)
    except Exception:
        # Last resort: try empty construction
        return model_cls()


def _extract_text_from_oci_response(data) -> Optional[str]:
    """Attempt to extract text from a wide variety of OCI GenAI response shapes."""
    try:
        if data is None:
            return None

        # Direct strings
        if isinstance(data, str) and data.strip():
            return data

        # Known single-string fields
        for attr in ("output_text", "generated_text", "text", "result", "output"):
            out = getattr(data, attr, None)
            if isinstance(out, str) and out.strip():
                return out

        # Known list-of-strings fields
        for attr in ("output_texts", "generated_texts", "outputs"):
            arr = getattr(data, attr, None)
            if isinstance(arr, (list, tuple)) and arr:
                # first non-empty string
                for v in arr:
                    if isinstance(v, str) and v.strip():
                        return v

        # Chat-style choices
        choices = getattr(data, "choices", None)
        if isinstance(choices, (list, tuple)) and choices:
            # choices[0].message.content[0].text
            try:
                msg = getattr(choices[0], "message", None)
                content = getattr(msg, "content", None)
                if isinstance(content, (list, tuple)) and content:
                    first = content[0]
                    txt = getattr(first, "text", None)
                    if isinstance(txt, str) and txt.strip():
                        return txt
            except Exception:
                pass
            # choices[0].text
            try:
                txt = getattr(choices[0], "text", None)
                if isinstance(txt, str) and txt.strip():
                    return txt
            except Exception:
                pass

        # Content arrays
        content = getattr(data, "content", None)
        if isinstance(content, (list, tuple)) and content:
            try:
                first = content[0]
                txt = getattr(first, "text", None)
                if isinstance(txt, str) and txt.strip():
                    return txt
            except Exception:
                pass

        # Dict-like objects (SDK models often have to_dict)
        try:
            to_dict = getattr(data, "to_dict", None)
            obj = to_dict() if callable(to_dict) else None
            if isinstance(obj, dict) and obj:
                # try common keys
                for key in ("output_text", "generated_text", "text", "result", "output"):
                    v = obj.get(key)
                    if isinstance(v, str) and v.strip():
                        return v
                for key in ("output_texts", "generated_texts", "outputs", "choices", "content"):
                    v = obj.get(key)
                    # list of strings
                    if isinstance(v, (list, tuple)):
                        for it in v:
                            if isinstance(it, str) and it.strip():
                                return it
                            if isinstance(it, dict):
                                t = it.get("text")
                                if isinstance(t, str) and t.strip():
                                    return t
        except Exception:
            pass
    except Exception as e:
        logger.debug("Failed to extract OCI response text: %s", e)
    return None



def oci_chat_completion(question: str, context: str, max_tokens: int = 512, temperature: float = 0.2) -> Optional[str]:
    client, _ = _build_oci_clients()
    if client is None or settings.llm_provider != "oci":
        logger.warning("OCI LLM inactive (provider=%s, client=%s)", settings.llm_provider, bool(client))
        return None

    try:
        comp_id = settings.oci_compartment_id
        model_id = settings.oci_genai_model_id
        if not comp_id or not model_id:
            raise ValueError("Set OCI_COMPARTMENT_OCID and OCI_GENAI_MODEL_ID in environment")

        prompt = (
            "You are a helpful assistant. Using the provided context, answer the question concisely.\n\n"
            f"Question: {question}\n\nContext:\n{context[:12000]}"
        )

        # Try chat() path first
        try:
            from oci.generative_ai_inference.models import ChatDetails, Message, TextContent
            details = _safe_build(
                ChatDetails,
                compartment_id=comp_id,
                model_id=model_id,
                messages=[_safe_build(Message, role="USER", content=[_safe_build(TextContent, text=prompt)])],
                max_tokens=max_tokens,
                temperature=temperature,
            )
            resp = client.chat(details)
            out = _extract_text_from_oci_response(resp.data)
            if out:
                logger.info("OCI GenAI chat() response extracted (chars=%d)", len(out))
                return out
            try:
                logger.warning("OCI GenAI chat(): no text extracted; type=%s fields=%s", type(resp.data), dir(resp.data))
            except Exception:
                logger.warning("OCI GenAI chat(): no text extracted; unable to introspect resp.data")
        except Exception as e:
            logger.debug("OCI chat() path not available or failed: %s", e)

        # Fallback to generate_text()
        try:
            from oci.generative_ai_inference.models import GenerateTextDetails
            details = _safe_build(
                GenerateTextDetails,
                compartment_id=comp_id,
                model_id=model_id,
                prompt=prompt,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            resp = client.generate_text(details)
            out = _extract_text_from_oci_response(resp.data)
            if out:
                logger.info("OCI GenAI generate_text() response extracted (chars=%d)", len(out))
                return out
            try:
                logger.warning("OCI GenAI generate_text(): no text extracted; type=%s fields=%s", type(resp.data), dir(resp.data))
            except Exception:
                logger.warning("OCI GenAI generate_text(): no text extracted; unable to introspect resp.data")
            return None
        except Exception as e:
            logger.debug("OCI generate_text() path failed: %s", e)
            return None
    except Exception as e:
        logger.exception("OCI GenAI call failed: %s", e)
        return None


def oci_chat_completion_chat_only(question: str, context: str, max_tokens: int = 512, temperature: float = 0.2) -> Optional[str]:
    """Force the chat() path and return extracted text or None."""
    client, _ = _build_oci_clients()
    if client is None or settings.llm_provider != "oci":
        return None
    try:
        from oci.generative_ai_inference.models import ChatDetails, Message, TextContent
        comp_id = settings.oci_compartment_id
        model_id = settings.oci_genai_model_id
        if not comp_id or not model_id:
            return None
        prompt = (
            "You are a helpful assistant. Using the provided context, answer the question concisely.\n\n"
            f"Question: {question}\n\nContext:\n{context[:12000]}"
        )
        details = _safe_build(
            ChatDetails,
            compartment_id=comp_id,
            model_id=model_id,
            messages=[_safe_build(Message, role="USER", content=[_safe_build(TextContent, text=prompt)])],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        resp = client.chat(details)
        return _extract_text_from_oci_response(resp.data)
    except Exception:
        return None


def oci_chat_completion_text_only(question: str, context: str, max_tokens: int = 512, temperature: float = 0.2) -> Optional[str]:
    """Force the generate_text() path and return extracted text or None."""
    client, _ = _build_oci_clients()
    if client is None or settings.llm_provider != "oci":
        return None
    try:
        from oci.generative_ai_inference.models import GenerateTextDetails
        comp_id = settings.oci_compartment_id
        model_id = settings.oci_genai_model_id
        if not comp_id or not model_id:
            return None
        prompt = (
            "You are a helpful assistant. Using the provided context, answer the question concisely.\n\n"
            f"Question: {question}\n\nContext:\n{context[:12000]}"
        )
        details = _safe_build(
            GenerateTextDetails,
            compartment_id=comp_id,
            model_id=model_id,
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        resp = client.generate_text(details)
        return _extract_text_from_oci_response(resp.data)
    except Exception:
        return None
