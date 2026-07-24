"""配置管理模块"""

import os
import secrets
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _load_dotenv_files() -> None:
    """加载环境文件：先仓库 ``docker/.env``（与 compose 共用 ``MINIO_ROOT_*``），再 ``openrag/.env`` 覆盖。

    避免本机只改了 Docker 里 MinIO 密码、却未同步 ``STORAGE_*`` 时出现 S3 AccessDenied。
    """
    here = Path(__file__).resolve()
    inner_openrag_root = here.parents[2]
    repo_root = here.parents[3]
    docker_env = repo_root / "docker" / ".env"
    app_env = inner_openrag_root / ".env"
    try:
        cwd_env = Path(".env").resolve()
    except OSError:
        cwd_env = Path(".env")

    if docker_env.is_file():
        load_dotenv(docker_env)
    if app_env.is_file():
        load_dotenv(app_env, override=True)
    if cwd_env.is_file() and cwd_env != app_env.resolve():
        load_dotenv(cwd_env, override=True)


_load_dotenv_files()


class StorageConfig(BaseSettings):
    """存储配置"""

    type: str = "minio"
    base_path: str = "/tmp/openrag"
    endpoint: Optional[str] = "localhost:9000"
    #: 未设置 ``STORAGE_ACCESS_KEY`` / ``STORAGE_SECRET_KEY`` 时，回退到 ``MINIO_ROOT_USER`` / ``MINIO_ROOT_PASSWORD``
    access_key: Optional[str] = None
    secret_key: Optional[str] = None
    bucket: Optional[str] = "openrag"
    prefix: Optional[str] = None
    # 对外访问对象用的 HTTP 根地址（path-style：{public_url}/{bucket}/{object_key}）
    public_url: Optional[str] = None

    model_config = SettingsConfigDict(env_prefix="STORAGE_", populate_by_name=True)

    @model_validator(mode="after")
    def resolve_minio_credentials(self) -> "StorageConfig":
        def pick(
            value: Optional[str], minio_env: str, default: str = "minioadmin"
        ) -> str:
            if value is not None and str(value).strip() != "":
                return str(value).strip()
            v = os.getenv(minio_env, default)
            return str(v).strip() if str(v).strip() else default

        self.access_key = pick(self.access_key, "MINIO_ROOT_USER")
        self.secret_key = pick(self.secret_key, "MINIO_ROOT_PASSWORD")
        if self.prefix is not None:
            prefix = str(self.prefix).strip().strip("/")
            self.prefix = prefix or None
        return self


class VectorDBConfig(BaseSettings):
    """向量数据库配置"""

    type: str = "milvus"
    host: str = Field(default="localhost", validation_alias="MILVUS_HOST")
    port: int = Field(default=19530, validation_alias="MILVUS_PORT")
    runtime_user: Optional[str] = Field(
        default=None, validation_alias="MILVUS_RUNTIME_USER"
    )
    runtime_password: Optional[SecretStr] = Field(
        default=None, validation_alias="MILVUS_RUNTIME_PASSWORD"
    )
    admin_user: Optional[str] = Field(
        default=None, validation_alias="MILVUS_INDEX_ADMIN_USER"
    )
    admin_password: Optional[SecretStr] = Field(
        default=None, validation_alias="MILVUS_INDEX_ADMIN_PASSWORD"
    )
    cleanup_user: Optional[str] = Field(
        default=None, validation_alias="MILVUS_CLEANUP_USER"
    )
    cleanup_password: Optional[SecretStr] = Field(
        default=None, validation_alias="MILVUS_CLEANUP_PASSWORD"
    )
    secure: bool = Field(default=False, validation_alias="MILVUS_SECURE")
    connection_timeout_seconds: int = Field(
        default=10, validation_alias="MILVUS_CONNECTION_TIMEOUT_SECONDS"
    )

    model_config = SettingsConfigDict(populate_by_name=True, extra="ignore")


class ElasticsearchConfig(BaseSettings):
    """Elasticsearch 全文索引（按 workspace slug 拆索引）。

    环境变量（与 Config 的 env_nested_delimiter 配合）：``ELASTICSEARCH__ENABLED``、
    ``ELASTICSEARCH__HOSTS``、``ELASTICSEARCH__REQUEST_TIMEOUT``、``ELASTICSEARCH__VERIFY_CERTS``。
    """

    enabled: bool = False
    #: 逗号分隔，如 http://localhost:9200,http://es2:9200
    hosts: str = "http://localhost:9200"
    request_timeout: int = 30
    verify_certs: bool = True


class PostgresConfig(BaseSettings):
    """PostgreSQL 配置（OpenRag 元数据唯一支持的关系库）。"""

    host: str = "localhost"
    port: int = 5432
    database: str = "openrag"
    user: str = "openrag"
    password: str = "openrag_dev_password"
    #: 为 true 时 SQLAlchemy 将所有 SQL 打到日志（排障用，生产勿开）
    sqlalchemy_echo: bool = False

    model_config = SettingsConfigDict(env_prefix="POSTGRES_", populate_by_name=True)


class EmbeddingConfig(BaseSettings):
    """Embedding 配置"""

    provider: str = Field(default="openai", validation_alias="EMBEDDING_PROVIDER")
    model: str = Field(default="", validation_alias="EMBEDDING_MODEL")
    api_key: Optional[SecretStr] = Field(default=None, validation_alias="OPENAI_API_KEY")
    base_url: Optional[str] = Field(default=None, validation_alias="OPENAI_BASE_URL")
    dimension: int = Field(default=0, validation_alias="EMBEDDING_DIMENSION")
    revision: str = Field(default="", validation_alias="EMBEDDING_REVISION")
    model_identity: str = Field(default="", validation_alias="EMBEDDING_MODEL_IDENTITY")
    normalization: str = Field(default="none", validation_alias="EMBEDDING_NORMALIZATION")
    input_type: str = Field(default="text", validation_alias="EMBEDDING_INPUT_TYPE")
    encoding_format: str = Field(default="float", validation_alias="EMBEDDING_ENCODING_FORMAT")
    distance_metric: str = Field(default="COSINE", validation_alias="EMBEDDING_DISTANCE_METRIC")
    query_prefix_revision: str = Field(
        default="", validation_alias="EMBEDDING_QUERY_PREFIX_REVISION"
    )
    document_prefix_revision: str = Field(
        default="", validation_alias="EMBEDDING_DOCUMENT_PREFIX_REVISION"
    )
    text_preprocess_revision: str = Field(
        default="", validation_alias="EMBEDDING_TEXT_PREPROCESS_REVISION"
    )
    sdk_contract_revision: str = Field(
        default="openai-v1", validation_alias="EMBEDDING_SDK_CONTRACT_REVISION"
    )
    config_ref: str = Field(default="", validation_alias="EMBEDDING_CONFIG_REF")
    batch_size: int = Field(default=8, validation_alias="EMBEDDING_BATCH_SIZE")
    request_timeout_seconds: int = Field(
        default=60, validation_alias="EMBEDDING_TIMEOUT_SECONDS"
    )
    max_attempts: int = Field(default=3, validation_alias="EMBEDDING_MAX_ATTEMPTS")
    probe_interval_seconds: int = Field(
        default=30, validation_alias="EMBEDDING_PROBE_INTERVAL_SECONDS"
    )

    model_config = SettingsConfigDict(populate_by_name=True, extra="ignore")


class SecurityConfig(BaseSettings):
    """安全配置

    WARNING: The default SECRET_KEY is for development only.
    In production, you MUST set the SECRET_KEY environment variable
    to a secure random value. Use: python -c "import secrets; print(secrets.token_urlsafe(32))"
    """

    secret_key: str = Field(
        default="dev-secret-key-change-in-production-f8a3b2c1d4e5f6g7h8i9j0k1l2m3n4o5",
        alias="SECRET_KEY",
    )
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 720

    model_config = SettingsConfigDict(env_prefix="SECURITY_", populate_by_name=True)


class PreviewConfig(BaseSettings):
    """外部文档预览配置"""

    public_web_base_url: Optional[str] = None
    token_ttl_seconds: int = 900
    token_max_ttl_seconds: int = 1800

    model_config = SettingsConfigDict(env_prefix="PREVIEW_", populate_by_name=True)


class IndexQualityConfig(BaseSettings):
    recall_max_regression: float = Field(
        default=0.02, validation_alias="INDEX_QUALITY_RECALL_MAX_REGRESSION"
    )
    ndcg_max_regression: float = Field(
        default=0.02, validation_alias="INDEX_QUALITY_NDCG_MAX_REGRESSION"
    )
    mrr_max_regression: float = Field(
        default=0.02, validation_alias="INDEX_QUALITY_MRR_MAX_REGRESSION"
    )
    p95_max_regression: float = Field(
        default=0.20, validation_alias="INDEX_QUALITY_P95_MAX_REGRESSION"
    )
    shadow_sample_rate: float = Field(
        default=0.0, ge=0.0, le=1.0, validation_alias="INDEX_SHADOW_SAMPLE_RATE"
    )
    shadow_budget_ms: int = Field(
        default=100, ge=1, validation_alias="INDEX_SHADOW_BUDGET_MS"
    )

    model_config = SettingsConfigDict(populate_by_name=True, extra="ignore")


class Config(BaseSettings):
    """全局配置"""

    storage: StorageConfig = Field(default_factory=StorageConfig)
    vector_db: VectorDBConfig = Field(default_factory=VectorDBConfig)
    elasticsearch: ElasticsearchConfig = Field(default_factory=ElasticsearchConfig)
    postgres: PostgresConfig = Field(default_factory=PostgresConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    preview: PreviewConfig = Field(default_factory=PreviewConfig)
    index_quality: IndexQualityConfig = Field(default_factory=IndexQualityConfig)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",  # Ignore unknown env vars
    )

    @classmethod
    def from_yaml(cls, yaml_path: str) -> "Config":
        """从 YAML 文件加载配置"""
        with open(yaml_path, "r") as f:
            data = yaml.safe_load(f)
        return cls(**data)


# 全局配置实例 - 使用延迟初始化
_config: Optional[Config] = None


def get_config() -> Config:
    """获取全局配置实例（延迟初始化）"""
    global _config
    if _config is None:
        _config = Config()
    return _config


def get_preview_public_web_base_url() -> str:
    """获取对外预览 Web 根地址，并去除尾部斜杠。"""
    value = get_config().preview.public_web_base_url
    if value is None:
        return ""
    return value.strip().rstrip("/")


def get_preview_token_ttl_seconds() -> int:
    """获取 preview token 默认有效期。"""
    return get_config().preview.token_ttl_seconds


def get_preview_token_max_ttl_seconds() -> int:
    """获取 preview token 最大有效期。"""
    return get_config().preview.token_max_ttl_seconds


# Note: Use get_config() to access config instead of direct import
