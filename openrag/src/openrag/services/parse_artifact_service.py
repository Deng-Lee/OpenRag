"""Persist long-lived canonical parse artifacts and DB references."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import Counter
from dataclasses import asdict, is_dataclass
from typing import Any, Iterable, Optional

from sqlalchemy.orm import Session

from openrag.models import DocumentParseArtifact


class ParseArtifactService:
    """Write canonical parse outputs to object storage and upsert their reference."""

    def __init__(self, db: Session, minio_storage: Any):
        self.db = db
        self.minio_storage = minio_storage

    def persist_parse_artifacts(
        self,
        *,
        workspace_id: int,
        file_id: int,
        bucket_name: str,
        file_uri: str,
        source_doc_bytes: bytes,
        blocks: Iterable[Any],
        parser_name: str,
        parser_version: str,
    ) -> DocumentParseArtifact:
        block_list = list(blocks)
        source_doc_hash = _sha256_hex(source_doc_bytes)
        canonical_blocks = [_block_to_dict(block, index) for index, block in enumerate(block_list)]
        canonical_text = _canonical_markdown(canonical_blocks)
        canonical_text_hash = _sha256_hex(canonical_text.encode("utf-8"))
        page_count = len(
            {
                item.get("page")
                for item in canonical_blocks
                if item.get("page") is not None
            }
        )
        block_type_counts = dict(Counter(str(item.get("block_type") or "text") for item in canonical_blocks))

        safe_parser = _safe_key_segment(parser_name)
        safe_version = _safe_key_segment(parser_version)
        base_key = (
            f"parse_artifacts/{file_id}/{source_doc_hash}/"
            f"{safe_parser}/{safe_version}"
        )
        canonical_json_object_key = f"{base_key}/canonical.json"
        canonical_md_object_key = f"{base_key}/canonical.md"

        json_payload = {
            "workspace_id": workspace_id,
            "file_id": file_id,
            "file_uri": file_uri,
            "source_doc_hash": source_doc_hash,
            "parser_name": parser_name,
            "parser_version": parser_version,
            "canonical_text_hash": canonical_text_hash,
            "block_count": len(canonical_blocks),
            "page_count": page_count,
            "block_type_counts": block_type_counts,
            "blocks": canonical_blocks,
        }
        json_bytes = json.dumps(
            json_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        md_bytes = canonical_text.encode("utf-8")

        self.minio_storage.put_file(
            bucket_name,
            canonical_json_object_key,
            json_bytes,
            content_type="application/json; charset=utf-8",
        )
        self.minio_storage.put_file(
            bucket_name,
            canonical_md_object_key,
            md_bytes,
            content_type="text/markdown; charset=utf-8",
        )

        artifact = (
            self.db.query(DocumentParseArtifact)
            .filter(
                DocumentParseArtifact.file_id == file_id,
                DocumentParseArtifact.source_doc_hash == source_doc_hash,
                DocumentParseArtifact.parser_name == parser_name,
                DocumentParseArtifact.parser_version == parser_version,
            )
            .first()
        )
        if artifact is None:
            artifact = DocumentParseArtifact(
                artifact_id=uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"{file_id}:{source_doc_hash}:{parser_name}:{parser_version}",
                ).hex,
                file_id=file_id,
                workspace_id=workspace_id,
                source_doc_hash=source_doc_hash,
                parser_name=parser_name,
                parser_version=parser_version,
            )
            self.db.add(artifact)

        artifact.workspace_id = workspace_id
        artifact.canonical_text_hash = canonical_text_hash
        artifact.canonical_json_bucket = bucket_name
        artifact.canonical_json_object_key = canonical_json_object_key
        artifact.canonical_json_size_bytes = len(json_bytes)
        artifact.canonical_md_bucket = bucket_name
        artifact.canonical_md_object_key = canonical_md_object_key
        artifact.canonical_md_size_bytes = len(md_bytes)
        artifact.block_count = len(canonical_blocks)
        artifact.page_count = page_count
        artifact.block_type_counts = block_type_counts
        artifact.status = "completed"
        artifact.error_message = None
        self.db.commit()
        self.db.refresh(artifact)
        return artifact


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_key_segment(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value or "unknown").strip("._")
    return safe or "unknown"


def _block_to_dict(block: Any, index: int) -> dict[str, Any]:
    if is_dataclass(block):
        raw = asdict(block)
    elif isinstance(block, dict):
        raw = dict(block)
    else:
        raw = {
            key: getattr(block, key, None)
            for key in (
                "text",
                "page",
                "offset",
                "bbox",
                "block_type",
                "level",
                "block_id",
                "char_start",
                "char_end",
                "table_data",
                "layout_type",
                "confidence",
                "language",
                "metadata",
            )
        }
    raw.pop("image", None)
    bbox = raw.get("bbox")
    if bbox is not None:
        raw["bbox"] = list(bbox)
    return {
        "index": index,
        "block_id": raw.get("block_id"),
        "text": raw.get("text") or "",
        "page": raw.get("page"),
        "offset": raw.get("offset"),
        "char_start": raw.get("char_start"),
        "char_end": raw.get("char_end"),
        "bbox": raw.get("bbox"),
        "block_type": raw.get("block_type") or "text",
        "level": raw.get("level") or 0,
        "table_data": raw.get("table_data"),
        "layout_type": raw.get("layout_type"),
        "confidence": raw.get("confidence"),
        "language": raw.get("language"),
        "metadata": raw.get("metadata") or {},
    }


def _canonical_markdown(blocks: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    current_page: Optional[int] = None
    for block in blocks:
        page = block.get("page")
        if page != current_page:
            current_page = page
            if lines:
                lines.append("")
            lines.append(f"<!-- Page {page} -->")
            lines.append("")

        text = str(block.get("text") or "").strip()
        if not text:
            continue
        block_type = str(block.get("block_type") or "text").lower()
        level = int(block.get("level") or 0)
        if block_type in {"heading", "title"} or level > 0:
            heading_level = min(max(level, 1), 6)
            lines.append(f"{'#' * heading_level} {text}")
        else:
            lines.append(text)
        lines.append("")
    return "\n".join(lines).strip() + "\n"
