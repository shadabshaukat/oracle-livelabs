from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import BinaryIO, Iterable, Optional, Tuple

from .config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ObjectRef:
    provider: str
    bucket: str
    name: str


class ObjectStore:
    provider: str

    def upload_bytes(self, bucket: str, object_name: str, data: bytes) -> None:
        raise NotImplementedError

    def upload_stream(self, bucket: str, object_name: str, stream: BinaryIO) -> None:
        raise NotImplementedError

    def upload_file(self, bucket: str, object_name: str, file_path: str) -> None:
        with open(file_path, "rb") as fh:
            self.upload_stream(bucket, object_name, fh)

    def get_object_stream(self, bucket: str, object_name: str) -> Tuple[Iterable[bytes], Optional[int], Optional[str]]:
        raise NotImplementedError

    def delete_object(self, bucket: str, object_name: str) -> None:
        raise NotImplementedError


class OciObjectStore(ObjectStore):
    provider = "oci"

    def __init__(self) -> None:
        self._client = None
        self._namespace = None

    def _build_oci_config(self):
        try:
            import oci  # type: ignore
        except Exception:
            return None, None
        cfg = None
        if settings.oci_config_file:
            try:
                cfg = oci.config.from_file(settings.oci_config_file, settings.oci_config_profile)
                if settings.oci_region:
                    cfg["region"] = settings.oci_region
            except Exception:
                cfg = None
        else:
            required = [
                settings.oci_tenancy_ocid,
                settings.oci_user_ocid,
                settings.oci_fingerprint,
                settings.oci_private_key_path,
            ]
            if all(required):
                cfg = {
                    "tenancy": settings.oci_tenancy_ocid,
                    "user": settings.oci_user_ocid,
                    "fingerprint": settings.oci_fingerprint,
                    "key_file": settings.oci_private_key_path,
                    "pass_phrase": settings.oci_private_key_passphrase,
                    "region": settings.oci_region,
                }
        return cfg, settings.oci_region

    def _get_client(self):
        if self._client is not None and self._namespace is not None:
            return self._client, self._namespace
        import oci  # type: ignore

        cfg, _region = self._build_oci_config()
        if not cfg:
            raise RuntimeError("OCI config not available")
        osc = oci.object_storage.ObjectStorageClient(cfg)
        ns = osc.get_namespace().data
        self._client = osc
        self._namespace = ns
        return osc, ns

    def upload_bytes(self, bucket: str, object_name: str, data: bytes) -> None:
        osc, ns = self._get_client()
        osc.put_object(ns, bucket, object_name, data)
        logger.info("OCI upload complete: bucket=%s object=%s", bucket, object_name)

    def upload_stream(self, bucket: str, object_name: str, stream: BinaryIO) -> None:
        import oci  # type: ignore

        osc, ns = self._get_client()
        upload_manager = oci.object_storage.UploadManager(osc, allow_parallel_uploads=True)
        upload_manager.upload_stream(ns, bucket, object_name, stream)
        logger.info("OCI streaming upload complete: bucket=%s object=%s", bucket, object_name)

    def get_object_stream(self, bucket: str, object_name: str) -> Tuple[Iterable[bytes], Optional[int], Optional[str]]:
        osc, ns = self._get_client()
        resp = osc.get_object(ns, bucket, object_name)
        content_length = None
        content_type = None
        try:
            if resp.headers:
                content_length = int(resp.headers.get("content-length")) if resp.headers.get("content-length") else None
                content_type = resp.headers.get("content-type")
        except Exception:
            content_length = None
            content_type = None
        stream = resp.data.raw if hasattr(resp.data, "raw") else resp.data
        return stream, content_length, content_type

    def delete_object(self, bucket: str, object_name: str) -> None:
        osc, ns = self._get_client()
        osc.delete_object(ns, bucket, object_name)
        logger.info("OCI delete complete: bucket=%s object=%s", bucket, object_name)


class S3ObjectStore(ObjectStore):
    provider = "s3"

    def __init__(self) -> None:
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            import boto3  # type: ignore
        except Exception as exc:
            raise RuntimeError("boto3 is required for S3 storage backend") from exc
        self._client = boto3.client(
            "s3",
            region_name=settings.s3_region or None,
            endpoint_url=settings.s3_endpoint_url or None,
            aws_access_key_id=settings.s3_access_key_id or None,
            aws_secret_access_key=settings.s3_secret_access_key or None,
        )
        return self._client

    def upload_bytes(self, bucket: str, object_name: str, data: bytes) -> None:
        client = self._get_client()
        client.put_object(Bucket=bucket, Key=object_name, Body=data)
        logger.info("S3 upload complete: bucket=%s object=%s", bucket, object_name)

    def upload_stream(self, bucket: str, object_name: str, stream: BinaryIO) -> None:
        client = self._get_client()
        client.upload_fileobj(stream, bucket, object_name)
        logger.info("S3 streaming upload complete: bucket=%s object=%s", bucket, object_name)

    def get_object_stream(self, bucket: str, object_name: str) -> Tuple[Iterable[bytes], Optional[int], Optional[str]]:
        client = self._get_client()
        resp = client.get_object(Bucket=bucket, Key=object_name)
        body = resp.get("Body")
        content_length = resp.get("ContentLength")
        content_type = resp.get("ContentType")
        if body is None:
            raise RuntimeError("S3 get_object returned empty body")
        return body, content_length, content_type

    def delete_object(self, bucket: str, object_name: str) -> None:
        client = self._get_client()
        client.delete_object(Bucket=bucket, Key=object_name)
        logger.info("S3 delete complete: bucket=%s object=%s", bucket, object_name)


_STORE_CACHE: dict[str, ObjectStore] = {}


def resolve_object_provider() -> Optional[str]:
    backend = (settings.storage_backend or "").lower()
    if backend in {"oci", "s3"}:
        return backend
    if backend == "both":
        provider = (settings.object_storage_provider or "").lower()
        return provider if provider in {"oci", "s3"} else None
    return None


def get_object_store(provider: Optional[str] = None) -> Optional[ObjectStore]:
    # Current configuration is authoritative. Persisted object metadata must
    # never reactivate OCI/S3 while the application is in local-only mode.
    enabled_provider = resolve_object_provider()
    requested_provider = (provider or enabled_provider or "").lower()
    if not enabled_provider or requested_provider != enabled_provider:
        return None
    provider = enabled_provider
    if provider in _STORE_CACHE:
        return _STORE_CACHE[provider]
    if provider == "oci":
        store = OciObjectStore()
    elif provider == "s3":
        store = S3ObjectStore()
    else:
        return None
    _STORE_CACHE[provider] = store
    return store


def default_object_bucket(provider: Optional[str] = None) -> Optional[str]:
    enabled_provider = resolve_object_provider()
    requested_provider = (provider or enabled_provider or "").lower()
    if not enabled_provider or requested_provider != enabled_provider:
        return None
    provider = enabled_provider
    if provider == "oci":
        return settings.oci_os_bucket_name
    if provider == "s3":
        return settings.s3_bucket_name
    return None
