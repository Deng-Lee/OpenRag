from io import BytesIO
from typing import Any, List, Optional
from urllib.parse import quote

from minio import Minio
from minio.error import S3Error
from minio.commonconfig import CopySource

from openrag.config import get_config

# 与 workspace 中源文件路径对应的层级（同 bucket 内）
HIERARCHY_ROOT = "hierarchy"
# 旧版：hierarchy/<uri>/.abstract.md（易被误解成「目录」）；新版见下方平级对象键
CHUNKS_DIR = "chunks"


def _normalize_file_uri(file_uri: str) -> str:
    return file_uri.strip().lstrip("/")


def hierarchy_l0_object_key(file_uri: str) -> str:
    """L0：独立 Markdown 对象，例如 hierarchy/path/to/a.docx.abstract.md（不是目录下的 .abstract.md）。"""
    u = _normalize_file_uri(file_uri)
    return f"{HIERARCHY_ROOT}/{u}.abstract.md"


def hierarchy_l1_object_key(file_uri: str) -> str:
    """L1：独立 Markdown 对象，例如 hierarchy/path/to/a.docx.overview.md"""
    u = _normalize_file_uri(file_uri)
    return f"{HIERARCHY_ROOT}/{u}.overview.md"


def hierarchy_chunks_prefix(file_uri: str) -> str:
    """仅 L2 分片使用该前缀下的对象：hierarchy/<uri>/chunks/NNNN.md"""
    u = _normalize_file_uri(file_uri)
    return f"{HIERARCHY_ROOT}/{u}/{CHUNKS_DIR}/"


def hierarchy_folder_prefix(file_uri: str) -> str:
    """兼容：旧布局 hierarchy/<uri>/ 前缀（读取/删除时仍可能遇到）。"""
    u = _normalize_file_uri(file_uri)
    return f"{HIERARCHY_ROOT}/{u}/"


def _legacy_dotfile_abstract_key(file_uri: str) -> str:
    u = _normalize_file_uri(file_uri)
    return f"{HIERARCHY_ROOT}/{u}/.abstract.md"


def _legacy_dotfile_overview_key(file_uri: str) -> str:
    u = _normalize_file_uri(file_uri)
    return f"{HIERARCHY_ROOT}/{u}/.overview.md"


def chunk_object_key(file_uri: str, chunk_index: int) -> str:
    """L2 分片对象键，与 put_document_hierarchy 一致。"""
    return f"{hierarchy_chunks_prefix(file_uri)}{chunk_index:04d}.md"


class MinioStorage:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(MinioStorage, cls).__new__(cls)
            cls._instance._init_client()
        return cls._instance

    def _init_client(self):
        config = get_config().storage
        endpoint = config.endpoint or "localhost:9000"
        access_key = config.access_key or "minioadmin"
        secret_key = config.secret_key or "minioadmin"

        secure = endpoint.startswith("https://")
        if "://" in endpoint:
            endpoint = endpoint.split("://", 1)[1]

        self.client = Minio(
            endpoint, access_key=access_key, secret_key=secret_key, secure=secure
        )

    def path_style_http_url(self, bucket_name: str, object_key: str) -> str:
        """Path-style 对象 URL：{base}/{bucket}/{key}（与 MinIO path-style 一致）。"""
        object_key = object_key.lstrip("/")
        cfg = get_config().storage
        if cfg.public_url:
            base = cfg.public_url.rstrip("/")
        else:
            ep = cfg.endpoint or "localhost:9000"
            sec = ep.startswith("https://")
            if "://" in ep:
                ep = ep.split("://", 1)[1]
            scheme = "https" if sec else "http"
            base = f"{scheme}://{ep}"

        parts = [p for p in object_key.split("/") if p]
        encoded = "/".join(quote(p, safe="") for p in parts)
        return f"{base}/{bucket_name}/{encoded}"

    def ensure_bucket(self, bucket_name: str):
        """Ensure the bucket exists for the given workspace"""
        try:
            if not self.client.bucket_exists(bucket_name):
                self.client.make_bucket(bucket_name)
        except S3Error as e:
            print(f"Error checking/creating bucket {bucket_name}: {e}")
            raise

    def put_file(
        self,
        bucket_name: str,
        object_name: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ):
        """Upload file to minio bucket"""
        self.ensure_bucket(bucket_name)
        object_name = object_name.lstrip("/")
        self.client.put_object(
            bucket_name,
            object_name,
            data=BytesIO(data),
            length=len(data),
            content_type=content_type,
        )

    def get_file_to_path(self, bucket_name: str, object_name: str, file_path: str):
        """Download file from minio bucket to local file path"""
        object_name = object_name.lstrip("/")
        self.client.fget_object(bucket_name, object_name, file_path)

    def remove_file(self, bucket_name: str, object_name: str):
        """Remove file from minio bucket"""
        object_name = object_name.lstrip("/")
        self.client.remove_object(bucket_name, object_name)

    def remove_directory(self, bucket_name: str, prefix: str):
        """Remove all objects under prefix in minio bucket"""
        prefix = prefix.lstrip("/")
        if not prefix.endswith("/"):
            prefix += "/"
        objects_to_delete = self.client.list_objects(
            bucket_name, prefix=prefix, recursive=True
        )
        for obj in objects_to_delete:
            self.client.remove_object(bucket_name, obj.object_name)

    def move_file(self, bucket_name: str, old_object_name: str, new_object_name: str):
        """Move file in minio bucket"""
        old_object_name = old_object_name.lstrip("/")
        new_object_name = new_object_name.lstrip("/")
        try:
            self.client.copy_object(
                bucket_name, new_object_name, CopySource(bucket_name, old_object_name)
            )
            self.client.remove_object(bucket_name, old_object_name)
        except S3Error as e:
            if e.code != "NoSuchKey":
                raise e

    def file_exists(self, bucket_name: str, object_name: str) -> bool:
        """Check if file exists in minio bucket"""
        object_name = object_name.lstrip("/")
        try:
            self.client.stat_object(bucket_name, object_name)
            return True
        except S3Error as e:
            if e.code == "NoSuchKey":
                return False
            raise

    def open_object_stream(self, bucket_name: str, object_key: str):
        """返回 MinIO get_object 响应流，调用方负责 close/release_conn。"""
        object_key = object_key.lstrip("/")
        return self.client.get_object(bucket_name, object_key)

    def read_object_bytes(self, bucket_name: str, object_key: str) -> bytes:
        """读取完整对象字节。"""
        object_key = object_key.lstrip("/")
        resp = self.client.get_object(bucket_name, object_key)
        try:
            return resp.read()
        finally:
            resp.close()
            resp.release_conn()

    def get_object_text(self, bucket_name: str, object_key: str) -> Optional[str]:
        """读取对象 UTF-8 文本；不存在则返回 None。"""
        object_key = object_key.lstrip("/")
        try:
            resp = self.client.get_object(bucket_name, object_key)
            data = resp.read()
            resp.close()
            resp.release_conn()
            return data.decode("utf-8")
        except S3Error:
            return None

    def read_hierarchy_abstract(self, bucket_name: str, file_uri: str) -> Optional[str]:
        """读取 L0 正文（新布局平级 .abstract.md 对象；兼容旧布局 hierarchy/<uri>/.abstract.md）。"""
        for key in (
            hierarchy_l0_object_key(file_uri),
            _legacy_dotfile_abstract_key(file_uri),
        ):
            t = self.get_object_text(bucket_name, key)
            if t is not None and t.strip():
                return t
        return None

    def read_hierarchy_overview(self, bucket_name: str, file_uri: str) -> Optional[str]:
        """读取 L1 正文（新布局；兼容旧 layout）。"""
        for key in (
            hierarchy_l1_object_key(file_uri),
            _legacy_dotfile_overview_key(file_uri),
        ):
            t = self.get_object_text(bucket_name, key)
            if t is not None and t.strip():
                return t
        return None

    def remove_document_hierarchy(self, bucket_name: str, file_uri: str) -> None:
        """删除某文件在 MinIO 中的 L0/L1/L2 层级对象（新布局 + 旧布局）。"""
        for key in (
            hierarchy_l0_object_key(file_uri),
            hierarchy_l1_object_key(file_uri),
            _legacy_dotfile_abstract_key(file_uri),
            _legacy_dotfile_overview_key(file_uri),
        ):
            try:
                if self.file_exists(bucket_name, key):
                    self.remove_file(bucket_name, key)
            except S3Error:
                pass
        self.remove_directory(bucket_name, hierarchy_chunks_prefix(file_uri))
        self.remove_directory(bucket_name, hierarchy_folder_prefix(file_uri))

    def put_document_hierarchy(
        self,
        bucket_name: str,
        file_uri: str,
        l0: Optional[str],
        l1: Optional[str],
        l2: Optional[List[Any]],
    ) -> dict[str, str]:
        """
        上传 L0(.abstract.md)、L1(.overview.md)、L2(chunks/*.md)。
        返回各层在 MinIO 上的 path-style HTTP URL（l2 为 chunks 目录前缀 URL）。
        """
        self.ensure_bucket(bucket_name)
        self.remove_document_hierarchy(bucket_name, file_uri)

        abstract_key = hierarchy_l0_object_key(file_uri)
        overview_key = hierarchy_l1_object_key(file_uri)

        if l0:
            self.put_file(
                bucket_name,
                abstract_key,
                l0.encode("utf-8"),
                content_type="text/markdown; charset=utf-8",
            )
        if l1:
            self.put_file(
                bucket_name,
                overview_key,
                l1.encode("utf-8"),
                content_type="text/markdown; charset=utf-8",
            )

        if l2:
            for i, chunk in enumerate(l2):
                text = getattr(chunk, "text", str(chunk))
                chunk_key = chunk_object_key(file_uri, i)
                self.put_file(
                    bucket_name,
                    chunk_key,
                    text.encode("utf-8"),
                    content_type="text/markdown; charset=utf-8",
                )

        l0_url = self.path_style_http_url(bucket_name, abstract_key) if l0 else ""
        l1_url = self.path_style_http_url(bucket_name, overview_key) if l1 else ""
        chunks_path = hierarchy_chunks_prefix(file_uri).rstrip("/")
        l2_url = self.path_style_http_url(bucket_name, chunks_path).rstrip("/") + "/"

        return {
            "l0_url": l0_url,
            "l1_url": l1_url,
            "l2_url": l2_url,
        }

    def move_document_hierarchy(
        self, bucket_name: str, old_file_uri: str, new_file_uri: str
    ) -> None:
        """源文件 URI 变更时，同步移动 MinIO 中的层级对象（新布局 + 旧前缀树）。"""
        self.remove_document_hierarchy(bucket_name, new_file_uri)
        moves: dict[str, str] = {}
        new_chunk_pref = hierarchy_chunks_prefix(new_file_uri)

        def _add(src: str, dst: str) -> None:
            if self.file_exists(bucket_name, src):
                moves[src] = dst

        _add(
            hierarchy_l0_object_key(old_file_uri),
            hierarchy_l0_object_key(new_file_uri),
        )
        _add(
            hierarchy_l1_object_key(old_file_uri),
            hierarchy_l1_object_key(new_file_uri),
        )
        _add(
            _legacy_dotfile_abstract_key(old_file_uri),
            hierarchy_l0_object_key(new_file_uri),
        )
        _add(
            _legacy_dotfile_overview_key(old_file_uri),
            hierarchy_l1_object_key(new_file_uri),
        )

        old_chunk_pref = hierarchy_chunks_prefix(old_file_uri)
        for obj in self.client.list_objects(
            bucket_name, prefix=old_chunk_pref, recursive=True
        ):
            ok = obj.object_name
            if ok.startswith(old_chunk_pref):
                moves[ok] = f"{new_chunk_pref}{ok[len(old_chunk_pref):]}"

        old_tree = hierarchy_folder_prefix(old_file_uri)
        for obj in self.client.list_objects(
            bucket_name, prefix=old_tree, recursive=True
        ):
            ok = obj.object_name
            if not ok.startswith(old_tree):
                continue
            rel = ok[len(old_tree) :]
            if rel in (".abstract.md", ".overview.md"):
                continue
            if rel.startswith(f"{CHUNKS_DIR}/"):
                suf = rel[len(f"{CHUNKS_DIR}/") :]
                moves[ok] = f"{new_chunk_pref}{suf}"

        for old_key, new_key in moves.items():
            try:
                self.client.copy_object(
                    bucket_name, new_key, CopySource(bucket_name, old_key)
                )
                self.remove_file(bucket_name, old_key)
            except S3Error as e:
                if e.code != "NoSuchKey":
                    raise

        self.remove_directory(bucket_name, hierarchy_folder_prefix(old_file_uri))
