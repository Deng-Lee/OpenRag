"""文档切片表：与 Milvus chunk_id 对齐，便于检索后解析存储路径与所属文档。"""

from typing import TYPE_CHECKING, Optional

from sqlalchemy import BigInteger, Float, ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from openrag.models.base import Base, TimestampMixin
from openrag.utils.pg_text import strip_pg_nul_bytes

if TYPE_CHECKING:
    from openrag.models.file import File


class DocumentChunk(Base, TimestampMixin):
    """单条切片元数据（正文完整内容在 MinIO / 本地 L2 文件中）。"""

    __tablename__ = "document_chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    file_id: Mapped[int] = mapped_column(
        ForeignKey("files.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    workspace_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        index=True,
        comment="与 files.workspace_id 一致，便于按工作区筛选",
    )

    chunk_id: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
        comment="与 Milvus 主键一致",
    )
    chunk_index: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="与 L2 chunks/NNNN.md 序号一致，从 0 开始",
    )

    object_key: Mapped[str] = mapped_column(
        String(1024),
        nullable=False,
        comment="MinIO 对象键（逻辑路径），与 bucket 组合唯一定位",
    )
    object_url: Mapped[Optional[str]] = mapped_column(
        String(2048),
        nullable=True,
        comment="path-style HTTP URL；仅 MinIO 模式有值",
    )
    local_chunk_path: Mapped[Optional[str]] = mapped_column(
        String(2048),
        nullable=True,
        comment="本地层级存储时的绝对路径",
    )

    text_preview: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="正文摘要，便于列表与不读对象存储时的上下文",
    )

    page: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    page_num_int: Mapped[Optional[list[int]]] = mapped_column(JSON, nullable=True)
    position_int: Mapped[Optional[list[list[int]]]] = mapped_column(JSON, nullable=True)
    top_int: Mapped[Optional[list[int]]] = mapped_column(JSON, nullable=True)
    level: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    block_type: Mapped[str] = mapped_column(String(32), default="text", nullable=False)
    start_offset: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    end_offset: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    bbox_x0: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    bbox_y0: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    bbox_x1: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    bbox_y1: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    source_block_id: Mapped[Optional[str]] = mapped_column(
        String(128),
        nullable=True,
        comment="解析器逻辑块 id（docx/ppt/pdf 块等）",
    )
    source_char_start: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        comment="整文件字符流起始下标（与 start_offset 同步时有值）",
    )
    source_char_end: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        comment="整文件字符流结束下标（开区间端点之外）",
    )

    file: Mapped["File"] = relationship("File", back_populates="document_chunks")

    @validates(
        "chunk_id",
        "object_key",
        "object_url",
        "local_chunk_path",
        "text_preview",
        "block_type",
        "source_block_id",
    )
    def _no_nul_in_pg_text_columns(self, _key: str, value: object) -> object:
        return strip_pg_nul_bytes(value)

    __table_args__ = (
        Index("ix_document_chunks_file_chunk_index", "file_id", "chunk_index", unique=True),
    )
