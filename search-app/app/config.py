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
    storage_backend: str = os.getenv("STORAGE_BACKEND", "local").lower()  # local | oci | s3 | both
    object_storage_provider: Optional[str] = os.getenv("OBJECT_STORAGE_PROVIDER")
    oci_os_bucket_name: Optional[str] = os.getenv("OCI_OS_BUCKET_NAME")
    s3_bucket_name: Optional[str] = os.getenv("S3_BUCKET_NAME")
    s3_region: Optional[str] = os.getenv("S3_REGION")
    s3_endpoint_url: Optional[str] = os.getenv("S3_ENDPOINT_URL")
    s3_access_key_id: Optional[str] = os.getenv("S3_ACCESS_KEY_ID")
    s3_secret_access_key: Optional[str] = os.getenv("S3_SECRET_ACCESS_KEY")
    # Upload & parsing
    max_upload_size_mb: int = int(os.getenv("MAX_UPLOAD_SIZE_MB", "50"))
    max_files_per_space: int = int(os.getenv("MAX_FILES_PER_SPACE", "100"))
    allowed_upload_extensions: str = os.getenv(
        "ALLOWED_UPLOAD_EXTENSIONS",
        ".pdf,.docx,.doc,.txt,.html,.htm,.md,.csv,.json,.xml,.pptx,.xlsx,.xls,.png,.jpg,.jpeg,.gif,.webp,.bmp",
    )
    use_pymupdf: bool = _get_bool("USE_PYMUPDF", False)
    # Upload lifecycle
    delete_uploaded_after_ingest: bool = _get_bool("DELETE_UPLOADED_FILES", False)

    # Auth/session
    secret_key: str = os.getenv("SECRET_KEY", "change-me")
    session_cookie_name: str = os.getenv("SESSION_COOKIE_NAME", "searchapp_session")
    session_max_age_seconds: int = int(os.getenv("SESSION_MAX_AGE_SECONDS", "28800"))
    session_activity_ttl_seconds: int = int(os.getenv("SESSION_ACTIVITY_TTL_SECONDS", "28800"))
    cookie_samesite: str = os.getenv("COOKIE_SAMESITE", "Lax")
    cookie_secure: bool = _get_bool("COOKIE_SECURE", False)
    allow_registration: bool = _get_bool("ALLOW_REGISTRATION", True)

    # Chunking
    chunk_size: int = int(os.getenv("CHUNK_SIZE", "2500"))
    chunk_overlap: int = int(os.getenv("CHUNK_OVERLAP", "250"))
    chunk_strategy: str = os.getenv("CHUNK_STRATEGY", "recursive").lower()
    sentence_splitter: str = os.getenv("SENTENCE_SPLITTER", "nltk").lower()

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

    # Image search (OpenCLIP)
    enable_image_storage: bool = _get_bool("ENABLE_IMAGE_STORAGE", True)
    image_embed_model: str = os.getenv("IMAGE_EMBED_MODEL", "openclip/ViT-B-32")
    image_embed_dim: int = int(os.getenv("IMAGE_EMBED_DIM", "512"))
    image_embed_device: str = os.getenv("IMAGE_EMBED_DEVICE", "cpu")
    image_search_text_weight: float = float(os.getenv("IMAGE_SEARCH_TEXT_WEIGHT", "0.45"))
    image_search_vector_weight: float = float(os.getenv("IMAGE_SEARCH_VECTOR_WEIGHT", "0.55"))
    image_keyword_max: int = int(os.getenv("IMAGE_KEYWORD_MAX", "24"))

    # Image captioning (optional)
    enable_image_captioning: bool = _get_bool("ENABLE_IMAGE_CAPTIONING", False)
    image_caption_model: str = os.getenv("IMAGE_CAPTION_MODEL", "llava-hf/llava-1.5-7b-hf")
    image_caption_model_small: str = os.getenv("IMAGE_CAPTION_MODEL_SMALL", "Salesforce/blip-image-captioning-base")
    image_caption_use_small: bool = _get_bool("IMAGE_CAPTION_USE_SMALL", True)
    image_caption_device: str = os.getenv("IMAGE_CAPTION_DEVICE", "cpu")
    image_caption_max_tokens: int = int(os.getenv("IMAGE_CAPTION_MAX_TOKENS", "120"))
    image_caption_prompt: str = os.getenv("IMAGE_CAPTION_PROMPT", "Describe the image in detail.")
    image_caption_timeout_s: int = int(os.getenv("IMAGE_CAPTION_TIMEOUT_S", "60"))

    # OCR (image text extraction)
    ocr_enabled: bool = _get_bool("OCR_ENABLED", True)
    ocr_engine: str = os.getenv("OCR_ENGINE", "tesseract").lower()
    ocr_min_chars: int = int(os.getenv("OCR_MIN_CHARS", "12"))
    ocr_max_chars: int = int(os.getenv("OCR_MAX_CHARS", "8000"))

    # pgvector index
    pgvector_metric: str = os.getenv("PGVECTOR_METRIC", "cosine")  # cosine|l2|ip
    pgvector_lists: int = int(os.getenv("PGVECTOR_LISTS", "1000"))  # tune for 10M (~sqrt(n))
    pgvector_probes: int = int(os.getenv("PGVECTOR_PROBES", "10"))  # runtime probes

    llm_cache_ttl_seconds: int = int(os.getenv("LLM_CACHE_TTL_SECONDS", "900"))

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
    rag_max_tokens: int = int(os.getenv("RAG_MAX_TOKENS", "1024"))

    # AWS Bedrock (optional)
    aws_region: Optional[str] = os.getenv("AWS_REGION")
    aws_bedrock_model_id: Optional[str] = os.getenv("AWS_BEDROCK_MODEL_ID")

    # Ollama (optional)
    ollama_host: Optional[str] = os.getenv("OLLAMA_HOST")
    ollama_model: Optional[str] = os.getenv("OLLAMA_MODEL")

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

    # NL2SQL
    sql_max_rows: int = int(os.getenv("SQL_MAX_ROWS", "200"))
    sql_default_rows: int = int(os.getenv("SQL_DEFAULT_ROWS", "200"))
    sql_memory_turns: int = int(os.getenv("SQL_MEMORY_TURNS", "10"))
    sql_persistent_memory_enabled: bool = _get_bool("SQL_PERSISTENT_MEMORY_ENABLED", False)
    text_persistent_memory_enabled: bool = _get_bool("TEXT_PERSISTENT_MEMORY_ENABLED", False)
    image_persistent_memory_enabled: bool = _get_bool("IMAGE_PERSISTENT_MEMORY_ENABLED", False)
    persistent_memory_top_k: int = int(os.getenv("PERSISTENT_MEMORY_TOP_K", "5"))
    persistent_memory_max_chars: int = int(os.getenv("PERSISTENT_MEMORY_MAX_CHARS", "4000"))
    persistent_memory_summary_max_chars: int = int(os.getenv("PERSISTENT_MEMORY_SUMMARY_MAX_CHARS", "1200"))
    sql_system_prompt: str = os.getenv(
        "SQL_SYSTEM_PROMPT",
        "You are an expert PostgreSQL (v14-v18) SQL assistant. Produce accurate, executable SELECT-only SQL and never hallucinate tables or columns."
        " Use only the provided schema/context. If needed, re-check the schema before answering.",
    )

    # Deep Research feature flags
    dr_rerank_enable: bool = _get_bool("DR_RERANK_ENABLE", True)
    dr_topic_lock_default: bool = _get_bool("DR_TOPIC_LOCK_DEFAULT", False)
    deep_research_timeout_seconds: int = int(os.getenv("DEEP_RESEARCH_TIMEOUT_SECONDS", "120"))
    deep_research_local_top_k: int = int(os.getenv("DEEP_RESEARCH_LOCAL_TOP_K", "15"))
    deep_research_web_top_k: int = int(os.getenv("DEEP_RESEARCH_WEB_TOP_K", "15"))
    deep_research_url_max_depth: int = int(os.getenv("DEEP_RESEARCH_URL_MAX_DEPTH", "2"))
    deep_research_url_max_pages: int = int(os.getenv("DEEP_RESEARCH_URL_MAX_PAGES", "12"))
    deep_research_retry_loops: int = int(os.getenv("DEEP_RESEARCH_RETRY_LOOPS", "1"))
    deep_research_confidence_threshold: float = float(os.getenv("DEEP_RESEARCH_CONFIDENCE_THRESHOLD", "0.45"))
    deep_research_missing_concept_loops: int = int(os.getenv("DEEP_RESEARCH_MISSING_CONCEPT_LOOPS", "1"))
    deep_research_missing_concept_top_k: int = int(os.getenv("DEEP_RESEARCH_MISSING_CONCEPT_TOP_K", "6"))
    deep_research_recency_boost: float = float(os.getenv("DEEP_RESEARCH_RECENCY_BOOST", "0.15"))
    deep_research_recency_half_life_days: float = float(os.getenv("DEEP_RESEARCH_RECENCY_HALF_LIFE_DAYS", "30"))
    deep_research_followup_enable: bool = _get_bool("DEEP_RESEARCH_FOLLOWUP_ENABLE", True)
    deep_research_followup_threshold: float = float(os.getenv("DEEP_RESEARCH_FOLLOWUP_THRESHOLD", "0.4"))
    deep_research_followup_max_questions: int = int(os.getenv("DEEP_RESEARCH_FOLLOWUP_MAX_QUESTIONS", "2"))
    deep_research_followup_autosend: bool = _get_bool("DEEP_RESEARCH_FOLLOWUP_AUTOSEND", True)
    deep_research_followup_relevance_min: float = float(os.getenv("DEEP_RESEARCH_FOLLOWUP_RELEVANCE_MIN", "0.08"))
    deep_research_persistent_memory_enabled: bool = _get_bool("DEEP_RESEARCH_PERSISTENT_MEMORY_ENABLED", False)
    deep_research_persistent_memory_top_k: int = int(os.getenv("DEEP_RESEARCH_PERSISTENT_MEMORY_TOP_K", "8"))
    deep_research_persistent_memory_max_chars: int = int(os.getenv("DEEP_RESEARCH_PERSISTENT_MEMORY_MAX_CHARS", "6000"))
    deep_research_memory_rollup_enabled: bool = _get_bool("DEEP_RESEARCH_MEMORY_ROLLUP_ENABLED", True)
    deep_research_memory_rollup_every_n: int = int(os.getenv("DEEP_RESEARCH_MEMORY_ROLLUP_EVERY_N", "6"))
    deep_research_memory_rollup_min_messages: int = int(os.getenv("DEEP_RESEARCH_MEMORY_ROLLUP_MIN_MESSAGES", "6"))
    deep_research_memory_rollup_max_chars: int = int(os.getenv("DEEP_RESEARCH_MEMORY_ROLLUP_MAX_CHARS", "12000"))


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
