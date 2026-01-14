import os
from dataclasses import dataclass
from typing import Optional
from pathlib import Path

# Load environment variables from a .env file if present so `uv run searchapp` works without exporting vars
try:
    from dotenv import load_dotenv, find_dotenv  # type: ignore

    _DOTENV_PATH = Path(__file__).resolve().parent.parent / ".env"
    if _DOTENV_PATH.exists():
        load_dotenv(str(_DOTENV_PATH), override=False)
    else:
        load_dotenv(find_dotenv(), override=False)
except Exception:
    # dotenv is optional; environment can still be provided by the shell or process manager
    pass


def _get_bool(env: str, default: bool = False) -> bool:
    v = os.getenv(env)
    if v is None:
        return default
    return v.lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    # Server
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8000"))
    workers: int = int(os.getenv("WORKERS", "1"))

    # Storage
    data_dir: str = os.getenv("DATA_DIR", "storage")
    upload_dir: str = os.getenv("UPLOAD_DIR", "storage/uploads")
    model_cache_dir: str = os.getenv("MODEL_CACHE_DIR", "storage/models")
    storage_backend: str = os.getenv("STORAGE_BACKEND", "local").lower()  # local | oci
    oci_os_bucket_name: Optional[str] = os.getenv("OCI_OS_BUCKET_NAME")
    # Upload & parsing
    max_upload_size_mb: int = int(os.getenv("MAX_UPLOAD_SIZE_MB", "50"))
    use_pymupdf: bool = _get_bool("USE_PYMUPDF", False)
    # Upload lifecycle
    delete_uploaded_after_ingest: bool = _get_bool("DELETE_UPLOADED_FILES", False)

    # Chunking
    chunk_size: int = int(os.getenv("CHUNK_SIZE", "2500"))
    chunk_overlap: int = int(os.getenv("CHUNK_OVERLAP", "250"))

    # Database (OCI PostgreSQL)
    database_url: Optional[str] = os.getenv("DATABASE_URL")
    db_host: Optional[str] = os.getenv("DB_HOST")
    db_port: int = int(os.getenv("DB_PORT", "5432"))
    db_name: Optional[str] = os.getenv("DB_NAME")
    db_user: Optional[str] = os.getenv("DB_USER")
    db_password: Optional[str] = os.getenv("DB_PASSWORD")
    db_sslmode: str = os.getenv("DB_SSLMODE", "require")
    db_pool_min_size: int = int(os.getenv("DB_POOL_MIN_SIZE", "1"))
    db_pool_max_size: int = int(os.getenv("DB_POOL_MAX_SIZE", "10"))

    # Embeddings
    embedding_model_name: str = os.getenv("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    embedding_dim: int = int(os.getenv("EMBEDDING_DIM", "384"))
    embedding_batch_size: int = int(os.getenv("EMBEDDING_BATCH", "64"))

    # pgvector index
    pgvector_metric: str = os.getenv("PGVECTOR_METRIC", "cosine")  # cosine|l2|ip
    pgvector_lists: int = int(os.getenv("PGVECTOR_LISTS", "1000"))  # tune for 10M (~sqrt(n))
    pgvector_probes: int = int(os.getenv("PGVECTOR_PROBES", "10"))  # runtime probes

    # Full-text search
    fts_config: str = os.getenv("FTS_CONFIG", "english")

    # Security
    allow_cors: bool = _get_bool("ALLOW_CORS", True)
    basic_auth_user: str = os.getenv("BASIC_AUTH_USER", "admin")
    basic_auth_password: str = os.getenv("BASIC_AUTH_PASSWORD", "changeme")

    # RAG/LLM (optional)
    llm_provider: str = os.getenv("LLM_PROVIDER", "none")  # none|openai|oci
    openai_api_key: Optional[str] = os.getenv("OPENAI_API_KEY")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    # OCI configuration
    oci_region: Optional[str] = os.getenv("OCI_REGION")
    oci_compartment_id: Optional[str] = os.getenv("OCI_COMPARTMENT_OCID")
    oci_genai_endpoint: Optional[str] = os.getenv("OCI_GENAI_ENDPOINT")
    oci_genai_model_id: Optional[str] = os.getenv("OCI_GENAI_MODEL_ID")
    # Auth via config file or API key envs
    oci_config_file: Optional[str] = os.getenv("OCI_CONFIG_FILE")
    oci_config_profile: str = os.getenv("OCI_CONFIG_PROFILE", "DEFAULT")
    oci_tenancy_ocid: Optional[str] = os.getenv("OCI_TENANCY_OCID")
    oci_user_ocid: Optional[str] = os.getenv("OCI_USER_OCID")
    oci_fingerprint: Optional[str] = os.getenv("OCI_FINGERPRINT")
    oci_private_key_path: Optional[str] = os.getenv("OCI_PRIVATE_KEY_PATH")
    oci_private_key_passphrase: Optional[str] = os.getenv("OCI_PRIVATE_KEY_PASSPHRASE")


def build_database_url(s: Settings) -> str:
    if s.database_url:
        return s.database_url
    if not (s.db_host and s.db_name and s.db_user and s.db_password):
        raise RuntimeError(
            "Database configuration missing. Provide DATABASE_URL or DB_HOST/DB_NAME/DB_USER/DB_PASSWORD."
        )
    return (
        f"postgresql://{s.db_user}:{s.db_password}@{s.db_host}:{s.db_port}/{s.db_name}"
        f"?sslmode={s.db_sslmode}"
    )


settings = Settings()
