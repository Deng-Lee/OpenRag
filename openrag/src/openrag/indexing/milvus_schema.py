"""Single source of truth for OpenRag Milvus schemas."""

import json
from dataclasses import dataclass
from typing import Any

from pymilvus import CollectionSchema, DataType, FieldSchema


@dataclass(frozen=True)
class MilvusSchemaSpec:
    role: str
    version: int
    dimension: int
    fields: tuple[dict[str, Any], ...]
    index_params: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "version": self.version,
            "dimension": self.dimension,
            "fields": [dict(field) for field in self.fields],
            "index_params": dict(self.index_params),
        }


def _spec(role: str, dimension: int, version: int) -> MilvusSchemaSpec:
    if role == "chunks":
        fields = [
            {"name": "chunk_id", "type": "VARCHAR", "primary": True, "max_length": 64},
            {"name": "file_id", "type": "INT64"},
            {"name": "text", "type": "VARCHAR", "max_length": 65535},
            {"name": "embedding", "type": "FLOAT_VECTOR", "dim": dimension},
            {"name": "page", "type": "INT64"},
            {"name": "level", "type": "INT64"},
            {"name": "block_type", "type": "VARCHAR", "max_length": 32},
        ]
    else:
        fields = [
            {
                "name": "layer_row_id",
                "type": "VARCHAR",
                "primary": True,
                "max_length": 96,
            },
            {"name": "file_id", "type": "INT64"},
            {"name": "layer", "type": "VARCHAR", "max_length": 8},
            {"name": "text", "type": "VARCHAR", "max_length": 65535},
            {"name": "embedding", "type": "FLOAT_VECTOR", "dim": dimension},
        ]
    if version >= 2:
        fields.insert(2, {"name": "workspace_id", "type": "INT64"})
    return MilvusSchemaSpec(
        role,
        version,
        dimension,
        tuple(fields),
        {"metric_type": "COSINE", "index_type": "IVF_FLAT", "params": {"nlist": 128}},
    )


def chunk_schema_spec(dimension: int, schema_version: int = 1) -> MilvusSchemaSpec:
    return _spec("chunks", dimension, schema_version)


def layer_schema_spec(dimension: int, schema_version: int = 1) -> MilvusSchemaSpec:
    return _spec("layers", dimension, schema_version)


def build_collection_schema(
    spec: MilvusSchemaSpec, description: str
) -> CollectionSchema:
    types = {
        "VARCHAR": DataType.VARCHAR,
        "INT64": DataType.INT64,
        "FLOAT_VECTOR": DataType.FLOAT_VECTOR,
    }
    fields = []
    for item in spec.fields:
        kwargs = {
            key: value
            for key, value in item.items()
            if key not in {"name", "type", "primary"}
        }
        fields.append(
            FieldSchema(
                name=item["name"],
                dtype=types[item["type"]],
                is_primary=bool(item.get("primary")),
                **kwargs
            )
        )
    return CollectionSchema(fields=fields, description=description)


def read_collection_metadata(collection) -> dict[str, Any]:
    metadata = {}
    description = str(getattr(collection.schema, "description", "") or "")
    if description.startswith("openrag:"):
        try:
            metadata.update(json.loads(description.removeprefix("openrag:")))
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    metadata.update(dict(getattr(collection, "properties", {}) or {}))
    return metadata


def describe_collection(collection) -> dict[str, Any]:
    fields = []
    for field in collection.schema.fields:
        item = {
            "name": field.name,
            "type": getattr(field.dtype, "name", str(field.dtype)),
        }
        if getattr(field, "is_primary", False):
            item["primary"] = True
        for key in ("dim", "max_length"):
            if key in (field.params or {}):
                item[key] = int(field.params[key])
        fields.append(item)
    indexes = [
        {"field_name": index.field_name, **dict(index.params or {})}
        for index in collection.indexes
    ]
    return {
        "fields": fields,
        "indexes": indexes,
        "metadata": read_collection_metadata(collection),
    }
