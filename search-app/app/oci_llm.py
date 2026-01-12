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
    """Attempt to extract text from various possible OCI GenAI response shapes."""
    try:
        # Common fields
        out = getattr(data, "output_text", None)
        if out:
            return out
        out = getattr(data, "generated_text", None)
        if out:
            return out

        # Chat-style choices
        choices = getattr(data, "choices", None)
        if choices:
            try:
                # Chat format: choices[0].message.content[0].text
                msg = choices[0].message
                content = getattr(msg, "content", None)
                if content and len(content) and hasattr(content[0], "text"):
                    return content[0].text
            except Exception:
                pass
            try:
                # Text format: choices[0].text
                txt = getattr(choices[0], "text", None)
                if txt:
                    return txt
            except Exception:
                pass

        # Content array with text
        content = getattr(data, "content", None)
        if content and len(content):
            try:
                if hasattr(content[0], "text"):
                    return content[0].text
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
            logger.warning("OCI GenAI chat() returned no output; data fields=%s", dir(resp.data))
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
            else:
                logger.warning("OCI GenAI generate_text() returned no output; data fields=%s", dir(resp.data))
            return out
        except Exception as e:
            logger.debug("OCI generate_text() path failed: %s", e)
            return None
    except Exception as e:
        logger.exception("OCI GenAI call failed: %s", e)
        return None
