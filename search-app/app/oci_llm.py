from __future__ import annotations

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

    if settings.oci_config_file:
        try:
            config = oci.config.from_file(settings.oci_config_file, settings.oci_config_profile)
            if settings.oci_region:
                config["region"] = settings.oci_region
            client = GenerativeAiInferenceClient(config=config, service_endpoint=settings.oci_genai_endpoint)
            return client, None
        except Exception as e:
            logger.exception("Failed to init OCI client from config file: %s", e)

    try:
        if not all([settings.oci_tenancy_ocid, settings.oci_user_ocid, settings.oci_fingerprint, settings.oci_private_key_path, settings.oci_region]):
            raise ValueError("Missing OCI API key envs (TENANCY, USER, FINGERPRINT, PRIVATE_KEY_PATH, REGION)")
        import oci
        signer = oci.signer.Signer(
            tenancy=settings.oci_tenancy_ocid,
            user=settings.oci_user_ocid,
            fingerprint=settings.oci_fingerprint,
            private_key_file_location=settings.oci_private_key_path,
            pass_phrase=settings.oci_private_key_passphrase,
        )
        client = GenerativeAiInferenceClient(config={"region": settings.oci_region}, signer=signer, service_endpoint=settings.oci_genai_endpoint)
        return client, signer
    except Exception as e:
        logger.exception("Failed to init OCI client via API key signer: %s", e)
        return None, None


def oci_chat_completion(question: str, context: str, max_tokens: int = 512, temperature: float = 0.2) -> Optional[str]:
    client, _ = _build_oci_clients()
    if client is None:
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

        try:
            from oci.generative_ai_inference.models import ChatDetails, Message, TextContent
            details = ChatDetails(
                compartment_id=comp_id,
                model_id=model_id,
                messages=[Message(role="USER", content=[TextContent(text=prompt)])],
                max_tokens=max_tokens,
                temperature=temperature,
            )
            resp = client.chat(details)
            out = getattr(resp.data, "output_text", None)
            if not out and getattr(resp.data, "choices", None):
                out = resp.data.choices[0].message.content[0].text
            return out
        except Exception:
            from oci.generative_ai_inference.models import GenerateTextDetails
            details = GenerateTextDetails(
                compartment_id=comp_id,
                model_id=model_id,
                prompt=prompt,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            resp = client.generate_text(details)
            out = getattr(resp.data, "output_text", None)
            return out
    except Exception as e:
        logger.exception("OCI GenAI call failed: %s", e)
        return None
