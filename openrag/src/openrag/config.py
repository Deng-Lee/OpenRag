"""配置管理模块"""

import os
import secrets
from pathlib import Path
from typing import Literal, Optional

import yaml
from dotenv import load_dotenv
from pydantic import Field, model_validator
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
    host: str = "localhost"
    port: int = 19530


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

    @model_validator(mode="after")
    def validate_mode_combination(self) -> "ElasticsearchConfig":
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

    model: str = "Qwen3-Embedding-4B"
    provider: str = "openai"
    api_key: Optional[str] = Field(default=None, alias="OPENAI_API_KEY")
    dimension: int = 2560


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


class Config(BaseSettings):
    """全局配置"""

    storage: StorageConfig = Field(default_factory=StorageConfig)
    vector_db: VectorDBConfig = Field(default_factory=VectorDBConfig)
    elasticsearch: ElasticsearchConfig = Field(default_factory=ElasticsearchConfig)
    postgres: PostgresConfig = Field(default_factory=PostgresConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    preview: PreviewConfig = Field(default_factory=PreviewConfig)

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
