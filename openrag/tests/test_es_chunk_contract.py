import copy

import pytest

from openrag.search.es_chunk_contract import (
    QUERY_PROFILE_VERSION,
    SCHEMA_VERSION,
    build_chunk_search_document,
    build_chunk_mapping,
    compute_mapping_hash,
    validate_chunk_search_document,
)


def _valid_document():
    return {
        "chunk_id": "chunk-1",
        "file_id": 11,
        "workspace_id": 7,
        "workspace_slug": "workspace-7",
        "content": "标准号 GB/T 35273-2020",
        "doc_type_kwd": "text",
        "content_with_weight": "标准号 GB/T 35273-2020",
        "mom_with_weight": "",
        "exact_terms": [],
        "schema_version": SCHEMA_VERSION,
        "mapping_hash": compute_mapping_hash(),
    }


def test_chunk_mapping_is_strict_complete_and_contains_stable_versions():
    mapping = build_chunk_mapping()

    assert mapping["dynamic"] == "strict"
    assert mapping["_meta"] == {
        "schema_version": "a02-content-exact-v2",
        "mapping_hash": compute_mapping_hash(),
        "query_profile_version": "a02-content-exact-v1",
        "content_analyzer": "standard",
    }
    assert mapping["properties"] == {
        "chunk_id": {"type": "keyword"},
        "file_id": {"type": "long"},
        "workspace_id": {"type": "long"},
        "workspace_slug": {"type": "keyword"},
        "content": {
            "type": "text",
            "analyzer": "standard",
            "search_analyzer": "standard",
        },
        "doc_type_kwd": {"type": "keyword"},
        "content_with_weight": {"type": "text", "index": False},
        "mom_with_weight": {"type": "text", "index": False},
        "exact_terms": {"type": "keyword"},
        "schema_version": {"type": "keyword"},
        "mapping_hash": {"type": "keyword"},
    }
    assert len(compute_mapping_hash()) == 64
    assert compute_mapping_hash() == compute_mapping_hash()
    assert not {
        "content_ltks",
        "content_sm_ltks",
        "mom_ltks",
        "mom_sm_ltks",
        "title_ltks",
        "title_sm_ltks",
        "tokenizer_version",
    } & mapping["properties"].keys()


def test_build_chunk_mapping_returns_independent_objects():
    mapping_a = build_chunk_mapping()
    mapping_b = build_chunk_mapping()

    assert mapping_a is not mapping_b
    assert mapping_a["_meta"] is not mapping_b["_meta"]
    assert mapping_a["properties"] is not mapping_b["properties"]
    assert (
        mapping_a["properties"]["content"]
        is not mapping_b["properties"]["content"]
    )


def test_mapping_hash_remains_stable():
    assert compute_mapping_hash() == (
        "9612b2498fc3e5f4622baba82f7fe9b6c"
        "502a7e1f82cee62a5afdbf4fcbccd25"
    )


def test_document_validation_accepts_complete_document_and_optional_empty_values():
    document = _valid_document()

    validate_chunk_search_document(document)

    without_optional = {
        key: value
        for key, value in document.items()
        if key not in {"content_with_weight", "mom_with_weight", "exact_terms"}
    }
    validate_chunk_search_document(without_optional)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("chunk_id", "", "chunk_id"),
        ("file_id", True, "file_id"),
        ("workspace_id", "7", "workspace_id"),
        ("workspace_slug", "", "workspace_slug"),
        ("content", None, "content"),
        ("doc_type_kwd", [], "doc_type_kwd"),
        ("content_with_weight", [], "content_with_weight"),
        ("mom_with_weight", {}, "mom_with_weight"),
        ("exact_terms", {}, "exact_terms"),
        ("exact_terms", ["ok", 3], "exact_terms"),
        ("schema_version", "wrong", "schema_version"),
        ("mapping_hash", "wrong", "mapping_hash"),
    ],
)
def test_document_validation_rejects_invalid_type_or_version(field, value, message):
    document = _valid_document()
    document[field] = value

    with pytest.raises(ValueError, match=message):
        validate_chunk_search_document(document)


@pytest.mark.parametrize("field", ["contnet", "content_ltks", "tokenizer_version"])
def test_document_validation_rejects_unknown_and_token_fields(field):
    document = copy.deepcopy(_valid_document())
    document[field] = "forbidden"

    with pytest.raises(ValueError, match="unknown fields"):
        validate_chunk_search_document(document)


def test_contract_versions_are_fixed_literals():
    assert SCHEMA_VERSION == "a02-content-exact-v2"
    assert QUERY_PROFILE_VERSION == "a02-content-exact-v1"


def test_builder_uses_one_truncated_content_for_source_and_exact_terms():
    content = "GB/T 35273-2020 " + "正文" * 40000

    document = build_chunk_search_document(
        chunk_id="chunk-1",
        file_id=11,
        workspace_id=7,
        workspace_slug="Workspace 7",
        content=content,
        metadata={
            "doc_type_kwd": "policy",
            "content_with_weight": "different legacy text",
            "content_ltks": "forbidden old token",
            "content_sm_ltks": "forbidden old token",
            "mom_with_weight": "parent compatibility text",
            "unknown": "ignored",
        },
    )

    assert len(document["content"]) == 65000
    assert document["content_with_weight"] == document["content"]
    assert document["exact_terms"] == ["gb/t 35273-2020"]
    assert document["mom_with_weight"] == "parent compatibility text"
    assert document["doc_type_kwd"] == "policy"
    assert document["schema_version"] == SCHEMA_VERSION
    assert document["mapping_hash"] == compute_mapping_hash()
    assert "content_ltks" not in document
    assert "content_sm_ltks" not in document
    assert "unknown" not in document
    validate_chunk_search_document(document)


def test_builder_defaults_invalid_optional_metadata_to_contract_safe_values():
    document = build_chunk_search_document(
        chunk_id="chunk-2",
        file_id=12,
        workspace_id=7,
        workspace_slug="workspace-7",
        content="plain content",
        metadata={"doc_type_kwd": {"bad": "object"}, "mom_with_weight": ["bad"]},
    )

    assert document["doc_type_kwd"] == "text"
    assert document["mom_with_weight"] == ""
    assert document["exact_terms"] == []
