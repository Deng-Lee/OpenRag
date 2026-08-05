import pytest
import copy

import openrag.search.es_chunk_store as es_module
from openrag.search.es_chunk_contract import (
    SCHEMA_VERSION,
    build_chunk_mapping,
    compute_mapping_hash,
)
from openrag.search.es_chunk_store import EsChunkStore


class FakeIndices:
    def __init__(self, *, exists=False, mapping=None, aliases=None):
        self._exists = exists
        self._mapping = mapping
        self.aliases = copy.deepcopy(aliases or {})
        self.create_calls = []
        self.alias_update_calls = []

    def exists(self, *, index):
        return self._exists

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        self._exists = True
        self._mapping = copy.deepcopy(kwargs.get("mappings"))

    def delete(self, *, index):
        self._exists = False
        self.aliases = {
            alias: {
                target: config
                for target, config in targets.items()
                if target != index
            }
            for alias, targets in self.aliases.items()
        }
        self.aliases = {
            alias: targets for alias, targets in self.aliases.items() if targets
        }

    def get_mapping(self, *, index):
        return {index: {"mappings": self._mapping}}

    def exists_alias(self, *, name):
        return name in self.aliases

    def get_alias(self, *, name):
        return {
            index: {"aliases": {name: copy.deepcopy(config)}}
            for index, config in self.aliases[name].items()
        }

    def update_aliases(self, *, actions):
        self.alias_update_calls.append(copy.deepcopy(actions))
        for action in actions:
            if "remove" in action:
                item = action["remove"]
                targets = self.aliases.get(item["alias"], {})
                targets.pop(item["index"], None)
                if not targets:
                    self.aliases.pop(item["alias"], None)
            elif "add" in action:
                item = action["add"]
                config = {
                    key: value
                    for key, value in item.items()
                    if key not in {"index", "alias"}
                }
                self.aliases.setdefault(item["alias"], {})[item["index"]] = config


class FakeEsClient:
    def __init__(self, responses=None, error=None, indices=None):
        self.responses = list(responses or [])
        self.error = error
        self.calls = []
        self.delete_by_query_calls = []
        self.indices = indices or FakeIndices()

    def search(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.responses.pop(0)

    def delete_by_query(self, **kwargs):
        self.delete_by_query_calls.append(kwargs)


def _store(client):
    store = EsChunkStore.__new__(EsChunkStore)
    store._client = client
    return store


def _response(*hits):
    return {"hits": {"hits": list(hits)}}


def _hit(chunk_id, file_id, score, text, matched_queries=None):
    hit = {
        "_id": chunk_id,
        "_score": score,
        "_source": {"file_id": file_id, "content": text},
    }
    if matched_queries is not None:
        hit["matched_queries"] = matched_queries
    return hit


def _valid_document(chunk_id="chunk-1"):
    return {
        "chunk_id": chunk_id,
        "file_id": 1,
        "workspace_id": 7,
        "workspace_slug": "workspace-7",
        "content": "正文",
        "doc_type_kwd": "text",
        "content_with_weight": "正文",
        "mom_with_weight": "",
        "exact_terms": [],
        "schema_version": SCHEMA_VERSION,
        "mapping_hash": compute_mapping_hash(),
    }


def test_ensure_versioned_index_creates_complete_strict_contract():
    indices = FakeIndices()

    created = _store(FakeEsClient(indices=indices)).ensure_versioned_index(
        "chunks-v2"
    )

    assert created is True
    assert indices.create_calls == [
        {
            "index": "chunks-v2",
            "settings": {"number_of_shards": 1, "number_of_replicas": 0},
            "mappings": build_chunk_mapping(),
        }
    ]


def test_ensure_versioned_index_accepts_matching_contract_and_rejects_mapping_drift():
    matching = FakeIndices(exists=True, mapping=build_chunk_mapping())
    assert (
        _store(FakeEsClient(indices=matching)).ensure_versioned_index(
            "chunks-v2"
        )
        is False
    )

    normalized_by_es = copy.deepcopy(build_chunk_mapping())
    normalized_by_es["properties"]["content"].pop("search_analyzer")
    _store(
        FakeEsClient(indices=FakeIndices(exists=True, mapping=normalized_by_es))
    ).ensure_versioned_index("chunks-v2")

    drifted_mapping = copy.deepcopy(build_chunk_mapping())
    drifted_mapping["properties"]["content"] = {"type": "keyword"}
    drifted = FakeIndices(exists=True, mapping=drifted_mapping)

    with pytest.raises(RuntimeError, match="contract mismatch"):
        _store(FakeEsClient(indices=drifted)).ensure_versioned_index("chunks-v2")


def test_legacy_ensure_keeps_v1_mapping_and_does_not_require_v2_contract():
    existing_v1 = FakeIndices(
        exists=True,
        mapping={"properties": {"content": {"type": "text"}}},
    )
    _store(FakeEsClient(indices=existing_v1)).ensure_index("chunks-v1")

    new_v1 = FakeIndices()
    _store(FakeEsClient(indices=new_v1)).ensure_index("chunks-v1")
    assert new_v1.create_calls[0]["mappings"] == {
        "properties": {
            "chunk_id": {"type": "keyword"},
            "file_id": {"type": "long"},
            "workspace_id": {"type": "long"},
            "workspace_slug": {"type": "keyword"},
            "content": {"type": "text"},
        }
    }


def test_bulk_validates_whole_batch_before_elasticsearch_request(monkeypatch):
    calls = []

    def fake_bulk(client, actions, **kwargs):
        calls.append((list(actions), kwargs))
        return len(actions), []

    monkeypatch.setattr("elasticsearch.helpers.bulk", fake_bulk)
    store = _store(FakeEsClient())
    invalid = _valid_document("invalid")
    invalid["content_ltks"] = "旧 token"

    with pytest.raises(ValueError, match="unknown fields"):
        store.bulk_upsert_chunks("chunks-v2", [_valid_document(), invalid])
    assert calls == []

    assert store.bulk_upsert_chunks(
        "chunks-v2", [_valid_document("a"), _valid_document("b")]
    ) == 2
    actions, kwargs = calls[0]
    assert [action["_id"] for action in actions] == ["a", "b"]
    assert kwargs["refresh"] == "wait_for"


def test_bulk_delete_chunks_uses_only_exact_chunk_ids(monkeypatch):
    calls = []

    def fake_bulk(client, actions, **kwargs):
        calls.append((list(actions), kwargs))
        return len(actions), []

    monkeypatch.setattr("elasticsearch.helpers.bulk", fake_bulk)

    accepted, errors = _store(FakeEsClient()).bulk_delete_chunks(
        "chunks-v1", ["orphan-b", "orphan-a"]
    )

    assert accepted == 2
    assert errors == []
    actions, kwargs = calls[0]
    assert actions == [
        {
            "_op_type": "delete",
            "_index": "chunks-v1",
            "_id": "orphan-b",
        },
        {
            "_op_type": "delete",
            "_index": "chunks-v1",
            "_id": "orphan-a",
        },
    ]
    assert kwargs["refresh"] == "wait_for"


def test_alias_switch_and_restore_are_each_one_atomic_request():
    indices = FakeIndices(
        exists=True,
        mapping=build_chunk_mapping(),
        aliases={
            "chunks-read": {"chunks-v1": {}},
            "chunks-write": {"chunks-v1": {"is_write_index": True}},
        },
    )
    store = _store(FakeEsClient(indices=indices))

    previous = store.update_workspace_aliases(
        read_alias="chunks-read",
        write_alias="chunks-write",
        target_index="chunks-v2",
    )

    assert previous == {
        "chunks-read": {"chunks-v1": {}},
        "chunks-write": {"chunks-v1": {"is_write_index": True}},
    }
    assert len(indices.alias_update_calls) == 1
    assert indices.aliases == {
        "chunks-read": {"chunks-v2": {}},
        "chunks-write": {"chunks-v2": {"is_write_index": True}},
    }
    assert store.resolve_read_target(
        legacy_index="chunks-v1",
        read_alias="chunks-read",
        chunk_index_mode="v2_alias",
    ) == "chunks-read"
    assert store.resolve_write_target(
        legacy_index="chunks-v1",
        write_alias="chunks-write",
        chunk_index_mode="v2_alias",
    ) == "chunks-write"

    store.restore_workspace_aliases(previous)

    assert len(indices.alias_update_calls) == 2
    assert indices.aliases == {
        "chunks-read": {"chunks-v1": {}},
        "chunks-write": {"chunks-v1": {"is_write_index": True}},
    }


def test_ensure_workspace_aliases_is_idempotent_and_never_redirects():
    indices = FakeIndices(exists=True, mapping=build_chunk_mapping())
    store = _store(FakeEsClient(indices=indices))

    first = store.ensure_workspace_aliases(
        read_alias="chunks-read",
        write_alias="chunks-write",
        target_index="chunks-v2",
    )
    second = store.ensure_workspace_aliases(
        read_alias="chunks-read",
        write_alias="chunks-write",
        target_index="chunks-v2",
    )

    assert first.changed is True
    assert second.changed is False
    assert len(indices.alias_update_calls) == 1
    assert indices.aliases == {
        "chunks-read": {"chunks-v2": {}},
        "chunks-write": {"chunks-v2": {"is_write_index": True}},
    }

    indices.aliases["chunks-read"] = {"other-index": {}}
    with pytest.raises(RuntimeError, match="unexpected targets"):
        store.ensure_workspace_aliases(
            read_alias="chunks-read",
            write_alias="chunks-write",
            target_index="chunks-v2",
        )
    assert len(indices.alias_update_calls) == 1


def test_delete_index_if_exists_is_idempotent():
    indices = FakeIndices(exists=True, mapping=build_chunk_mapping())
    store = _store(FakeEsClient(indices=indices))

    assert store.delete_index_if_exists("chunks-v2") is True
    assert store.delete_index_if_exists("chunks-v2") is False


def test_delete_indices_if_exist_uses_one_request():
    class NamedFakeIndices:
        def __init__(self):
            self.names = {"chunks-v1", "chunks-v2"}
            self.delete_calls = []

        def exists(self, *, index):
            return index in self.names

        def delete(self, *, index):
            self.delete_calls.append(index)
            self.names.difference_update(index.split(","))

    indices = NamedFakeIndices()
    deleted, missing = _store(
        FakeEsClient(indices=indices)
    ).delete_indices_if_exist(("chunks-v1", "chunks-v2", "missing"))

    assert deleted == ("chunks-v1", "chunks-v2")
    assert missing == ("missing",)
    assert indices.delete_calls == ["chunks-v1,chunks-v2"]


def test_required_file_delete_fails_closed_when_index_is_missing():
    client = FakeEsClient(indices=FakeIndices(exists=False))
    store = _store(client)

    store.delete_by_file_id("missing", 7)
    with pytest.raises(RuntimeError, match="not found"):
        store.delete_by_file_id("missing", 7, required=True)

    assert client.delete_by_query_calls == []


def test_v2_read_alias_missing_fails_closed():
    store = _store(
        FakeEsClient(
            indices=FakeIndices(exists=True, mapping=build_chunk_mapping())
        )
    )

    with pytest.raises(RuntimeError, match="one index"):
        store.resolve_read_target(
            legacy_index="chunks-v1",
            read_alias="missing-read",
            chunk_index_mode="v2_alias",
        )


def test_search_chunks_is_independent_and_maps_sparse_hits():
    client = FakeEsClient(
        [_response(_hit("chunk-d", 4, 12.0, "exact"), _hit("chunk-a", 1, 3.0, "alpha"))]
    )

    hits = _store(client).search_chunks(
        index_names=["ws-chunks"],
        query_text="ORAG-A01",
        file_ids=[1, 4],
        top_k=10,
    )

    assert hits == [
        {"chunk_id": "chunk-d", "file_id": 4, "text": "exact", "sparse_score": 12.0},
        {"chunk_id": "chunk-a", "file_id": 1, "text": "alpha", "sparse_score": 3.0},
    ]
    call = client.calls[0]
    assert call["size"] == 10
    assert call["source"] == ["file_id", "content"]
    assert call["query"]["bool"]["filter"] == [{"terms": {"file_id": [1, 4]}}]
    assert call["query"]["bool"]["must"] == [
        {
            "multi_match": {
                "query": "ORAG-A01",
                "type": "best_fields",
                "fields": ["content"],
            }
        }
    ]
    assert "exact_terms" not in str(call["query"])
    assert "ids" not in str(call["query"]).lower()
    assert "chunk_ids" not in str(call["query"]).lower()


def test_search_chunks_batches_file_scope_and_merges_global_top_k(monkeypatch):
    monkeypatch.setattr(es_module, "_FILE_SCOPE_BATCH_SIZE", 2)
    client = FakeEsClient(
        [
            _response(_hit("a", 1, 2.0, "a"), _hit("b", 2, 8.0, "b")),
            _response(_hit("c", 3, 7.0, "c"), _hit("d", 4, 4.0, "d")),
            _response(_hit("e", 5, 9.0, "e")),
        ]
    )

    hits = _store(client).search_chunks(
        index_names=["ws-chunks"],
        query_text="q",
        file_ids=[1, 2, 3, 4, 5],
        top_k=3,
    )

    assert [hit["chunk_id"] for hit in hits] == ["e", "b", "c"]
    assert [call["query"]["bool"]["filter"][0]["terms"]["file_id"] for call in client.calls] == [
        [1, 2],
        [3, 4],
        [5],
    ]


@pytest.mark.parametrize(
    ("index_names", "query_text", "file_ids", "top_k"),
    [([], "q", [1], 5), (["idx"], "", [1], 5), (["idx"], "q", [], 5), (["idx"], "q", [1], 0)],
)
def test_search_chunks_empty_inputs_do_not_query(index_names, query_text, file_ids, top_k):
    client = FakeEsClient()
    assert _store(client).search_chunks(
        index_names=index_names,
        query_text=query_text,
        file_ids=file_ids,
        top_k=top_k,
    ) == []
    assert client.calls == []


def test_search_chunks_propagates_adapter_error_for_service_degradation():
    client = FakeEsClient(error=TimeoutError("es timeout"))
    with pytest.raises(TimeoutError, match="es timeout"):
        _store(client).search_chunks(
            index_names=["idx"], query_text="q", file_ids=[1], top_k=5
        )


def test_v2_search_uses_content_and_exact_named_queries_without_token_fields():
    client = FakeEsClient(
        [
            _response(
                _hit(
                    "exact",
                    1,
                    9.0,
                    "GB/T 35273-2020",
                    ["content_bm25", "exact_identifier"],
                )
            )
        ]
    )

    hits = _store(client).search_chunks(
        index_names=["chunks-read"],
        query_text="请查找 ＧＢ／Ｔ　３５２７３－２０２０",
        file_ids=[1],
        top_k=10,
        chunk_index_mode="v2_alias",
    )

    query = client.calls[0]["query"]
    assert query["bool"]["minimum_should_match"] == 1
    assert query["bool"]["filter"] == [{"terms": {"file_id": [1]}}]
    assert query["bool"]["should"][0] == {
        "match": {
            "content": {
                "query": "请查找 ＧＢ／Ｔ　３５２７３－２０２０",
                "boost": 1.0,
                "_name": "content_bm25",
            }
        }
    }
    assert query["bool"]["should"][1] == {
        "terms": {
            "exact_terms": ["gb/t 35273-2020"],
            "boost": 8.0,
            "_name": "exact_identifier",
        }
    }
    assert "content_ltks" not in str(query)
    assert "mom_" not in str(query)
    assert hits[0]["matched_queries"] == ["content_bm25", "exact_identifier"]
    assert hits[0]["exact_term_match"] is True


def test_v2_natural_language_query_keeps_content_bm25_only():
    client = FakeEsClient([_response()])

    _store(client).search_chunks(
        index_names=["chunks-read"],
        query_text="数据安全管理办法适用于哪些场景",
        file_ids=[1],
        top_k=10,
        chunk_index_mode="v2_alias",
    )

    should = client.calls[0]["query"]["bool"]["should"]
    assert len(should) == 1
    assert should[0]["match"]["content"]["_name"] == "content_bm25"
    assert "exact_terms" not in str(should)
