"""配置管理模块"""

import os
import secrets
from pathlib import Path
from typing import Literal, Optional

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
    hybrid_recall_mode: Literal["legacy", "independent_rrf"] = "legacy"
    chunk_index_mode: Literal["legacy", "v2_alias"] = "legacy"

    @property
    def requires_fulltext_indexing(self) -> bool:
        return self.enabled and self.hybrid_recall_mode in {
            "legacy",
            "independent_rrf",
        }

    @model_validator(mode="after")
    def validate_mode_combination(self) -> "ElasticsearchConfig":
        if self.hybrid_recall_mode == "independent_rrf" and not self.enabled:
            raise ValueError(
                "ELASTICSEARCH__HYBRID_RECALL_MODE=independent_rrf requires "
                "ELASTICSEARCH__ENABLED=true"
            )
        if self.enabled and not self.hosts.strip():
            raise ValueError(
                "ELASTICSEARCH__ENABLED=true requires non-empty "
                "ELASTICSEARCH__HOSTS"
            )
        if self.chunk_index_mode == "v2_alias":
            if self.hybrid_recall_mode != "independent_rrf":
                raise ValueError(
                    "ELASTICSEARCH__CHUNK_INDEX_MODE=v2_alias requires "
                    "ELASTICSEARCH__HYBRID_RECALL_MODE=independent_rrf"
                )
            if not self.enabled:
                raise ValueError(
                    "ELASTICSEARCH__CHUNK_INDEX_MODE=v2_alias requires "
                    "ELASTICSEARCH__ENABLED=true"
                )
            if not self.hosts.strip():
                raise ValueError(
                    "ELASTICSEARCH__CHUNK_INDEX_MODE=v2_alias requires "
                    "non-empty ELASTICSEARCH__HOSTS"
                )
        return self


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


class MultiWorkspaceSearchConfig(BaseSettings):
    """Bounded recall and candidate-pool limits for multi-workspace search."""

    max_workspaces: int = Field(
        default=20,
        ge=1,
        le=20,
        validation_alias="MULTI_WORKSPACE_SEARCH_MAX_WORKSPACES",
    )
    recall_concurrency: int = Field(
        default=10,
        ge=1,
        le=20,
        validation_alias="MULTI_WORKSPACE_SEARCH_RECALL_CONCURRENCY",
    )
    candidate_multiplier: int = Field(
        default=3,
        ge=1,
        le=10,
        validation_alias="MULTI_WORKSPACE_SEARCH_CANDIDATE_MULTIPLIER",
    )
    min_candidates_per_workspace: int = Field(
        default=10,
        ge=1,
        le=100,
        validation_alias="MULTI_WORKSPACE_SEARCH_MIN_CANDIDATES_PER_WORKSPACE",
    )
    max_candidates_per_workspace: int = Field(
        default=100,
        ge=1,
        le=100,
        validation_alias="MULTI_WORKSPACE_SEARCH_MAX_CANDIDATES_PER_WORKSPACE",
    )
    max_global_rerank_candidates: int = Field(
        default=1000,
        ge=1,
        le=2000,
        validation_alias="MULTI_WORKSPACE_SEARCH_MAX_GLOBAL_RERANK_CANDIDATES",
    )

    model_config = SettingsConfigDict(populate_by_name=True, extra="ignore")

    @model_validator(mode="after")
    def validate_candidate_limits(self) -> "MultiWorkspaceSearchConfig":
        if self.min_candidates_per_workspace > self.max_candidates_per_workspace:
            raise ValueError(
                "min_candidates_per_workspace must not exceed max_candidates_per_workspace"
            )
        if self.max_global_rerank_candidates < self.max_workspaces:
            raise ValueError(
                "max_global_rerank_candidates must cover at least one candidate per workspace"
            )
        return self


class SearchGrantConfig(BaseSettings):
    """Short-lived grant configuration for federated service search."""

    enabled: bool = Field(
        default=False,
        validation_alias="SEARCH_GRANT_ENABLED",
    )
    instance_id: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        validation_alias="OPENRAG_INSTANCE_ID",
    )
    signing_key: Optional[SecretStr] = Field(
        default=None,
        validation_alias="SEARCH_GRANT_SIGNING_KEY",
    )
    previous_signing_key: Optional[SecretStr] = Field(
        default=None,
        validation_alias="SEARCH_GRANT_PREVIOUS_SIGNING_KEY",
    )
    algorithm: Literal["HS256"] = Field(
        default="HS256",
        validation_alias="SEARCH_GRANT_ALGORITHM",
    )
    ttl_seconds: int = Field(
        default=60,
        ge=5,
        le=300,
        validation_alias="SEARCH_GRANT_TTL_SECONDS",
    )
    clock_skew_seconds: int = Field(
        default=5,
        ge=0,
        le=30,
        validation_alias="SEARCH_GRANT_CLOCK_SKEW_SECONDS",
    )
    max_scopes_per_issue: int = Field(
        default=20,
        ge=1,
        le=20,
        validation_alias="SEARCH_GRANT_MAX_SCOPES_PER_ISSUE",
    )
    max_grants_per_search: int = Field(
        default=20,
        ge=1,
        le=20,
        validation_alias="SEARCH_GRANT_MAX_GRANTS_PER_SEARCH",
    )

    model_config = SettingsConfigDict(populate_by_name=True, extra="ignore")

    @model_validator(mode="after")
    def validate_enabled_configuration(self) -> "SearchGrantConfig":
        if not self.enabled:
            return self
        if not self.instance_id or not self.instance_id.strip():
            raise ValueError(
                "SEARCH_GRANT_ENABLED=true requires OPENRAG_INSTANCE_ID"
            )
        if self.signing_key is None or len(
            self.signing_key.get_secret_value().strip()
        ) < 32:
            raise ValueError(
                "SEARCH_GRANT_ENABLED=true requires SEARCH_GRANT_SIGNING_KEY with at least 32 characters"
            )
        previous_key = (
            self.previous_signing_key.get_secret_value().strip()
            if self.previous_signing_key is not None
            else ""
        )
        if not previous_key:
            self.previous_signing_key = None
        elif len(previous_key) < 32:
            raise ValueError(
                "SEARCH_GRANT_PREVIOUS_SIGNING_KEY must contain at least 32 characters"
            )
        elif previous_key == self.signing_key.get_secret_value().strip():
            raise ValueError(
                "SEARCH_GRANT_PREVIOUS_SIGNING_KEY must differ from SEARCH_GRANT_SIGNING_KEY"
            )
        self.instance_id = self.instance_id.strip()
        return self


class Config(BaseSettings):
    """全局配置"""

    max_upload_size: int = Field(
        default=100 * 1024 * 1024,
        validation_alias="MAX_UPLOAD_SIZE",
        gt=0,
    )
    storage: StorageConfig = Field(default_factory=StorageConfig)
    vector_db: VectorDBConfig = Field(default_factory=VectorDBConfig)
    elasticsearch: ElasticsearchConfig = Field(default_factory=ElasticsearchConfig)
    postgres: PostgresConfig = Field(default_factory=PostgresConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    preview: PreviewConfig = Field(default_factory=PreviewConfig)
    index_quality: IndexQualityConfig = Field(default_factory=IndexQualityConfig)
    multi_workspace_search: MultiWorkspaceSearchConfig = Field(
        default_factory=MultiWorkspaceSearchConfig
    )
    search_grant: SearchGrantConfig = Field(default_factory=SearchGrantConfig)

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
