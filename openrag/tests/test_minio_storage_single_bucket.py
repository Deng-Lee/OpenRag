from types import SimpleNamespace

import pytest
from minio.error import S3Error

from openrag.storage import minio_storage
from openrag.storage.minio_storage import MinioStorage


class FakeObject:
    def __init__(self, object_name: str):
        self.object_name = object_name


class FakeMinioClient:
    def __init__(self):
        self.bucket_exists_value = True
        self.remove_bucket_effective = True
        self.bucket_exists_calls = []
        self.make_bucket_calls = []
        self.remove_bucket_calls = []
        self.put_object_calls = []
        self.list_objects_calls = []
        self.remove_object_calls = []
        self.copy_object_calls = []
        self.fget_object_calls = []
        self.stat_object_calls = []
        self.get_object_calls = []
        self.objects = []

    def bucket_exists(self, bucket_name):
        self.bucket_exists_calls.append(bucket_name)
        return self.bucket_exists_value

    def make_bucket(self, bucket_name):
        self.make_bucket_calls.append(bucket_name)

    def remove_bucket(self, bucket_name):
        self.remove_bucket_calls.append(bucket_name)
        if self.remove_bucket_effective:
            self.bucket_exists_value = False

    def put_object(self, bucket_name, object_name, **kwargs):
        self.put_object_calls.append((bucket_name, object_name, kwargs))

    def fget_object(self, bucket_name, object_name, file_path):
        self.fget_object_calls.append((bucket_name, object_name, file_path))

    def remove_object(self, bucket_name, object_name):
        self.remove_object_calls.append((bucket_name, object_name))
        self.objects = [
            obj for obj in self.objects if obj.object_name != object_name
        ]

    def list_objects(self, bucket_name, prefix, recursive):
        self.list_objects_calls.append((bucket_name, prefix, recursive))
        return [obj for obj in self.objects if obj.object_name.startswith(prefix)]

    def copy_object(self, bucket_name, object_name, source):
        self.copy_object_calls.append((bucket_name, object_name, source))

    def stat_object(self, bucket_name, object_name):
        self.stat_object_calls.append((bucket_name, object_name))

    def get_object(self, bucket_name, object_name):
        self.get_object_calls.append((bucket_name, object_name))
        return SimpleNamespace(
            read=lambda: b"text",
            close=lambda: None,
            release_conn=lambda: None,
        )


@pytest.fixture
def fake_storage(monkeypatch):
    def build(*, bucket="rag-kb", prefix="openrag", public_url="http://172.16.31.63:9000"):
        cfg = SimpleNamespace(
            bucket=bucket,
            prefix=prefix,
            public_url=public_url,
            endpoint="localhost:9000",
        )
        monkeypatch.setattr(
            minio_storage,
            "get_config",
            lambda: SimpleNamespace(storage=cfg),
        )
        storage = object.__new__(MinioStorage)
        storage.client = FakeMinioClient()
        return storage

    return build


def copy_source_bucket(source):
    return getattr(source, "bucket_name", getattr(source, "_bucket_name", None))


def copy_source_object(source):
    return getattr(source, "object_name", getattr(source, "_object_name", None))


def test_put_file_resolves_logical_bucket_to_single_physical_bucket(fake_storage):
    storage = fake_storage()

    storage.put_file("law", "docs/a.pdf", b"pdf")

    assert storage.client.put_object_calls[0][0:2] == (
        "rag-kb",
        "openrag/law/docs/a.pdf",
    )


def test_trailing_slash_prefix_does_not_duplicate_slashes(fake_storage):
    storage = fake_storage(prefix="openrag/")

    storage.put_file("law", "/docs/a.pdf", b"pdf")

    assert storage.client.put_object_calls[0][0:2] == (
        "rag-kb",
        "openrag/law/docs/a.pdf",
    )


def test_unset_prefix_preserves_old_logical_bucket_behavior(fake_storage):
    storage = fake_storage(prefix=None)

    storage.put_file("law", "docs/a.pdf", b"pdf")

    assert storage.client.bucket_exists_calls == ["law"]
    assert storage.client.put_object_calls[0][0:2] == ("law", "docs/a.pdf")


def test_ensure_bucket_checks_only_physical_bucket_in_single_bucket_mode(fake_storage):
    storage = fake_storage()

    storage.ensure_bucket("law")

    assert storage.client.bucket_exists_calls == ["rag-kb"]
    assert storage.client.make_bucket_calls == []


def test_path_style_http_url_uses_physical_bucket_and_prefixed_key(fake_storage):
    storage = fake_storage()

    url = storage.path_style_http_url("law", "docs/a.pdf")

    assert url == "http://172.16.31.63:9000/rag-kb/openrag/law/docs/a.pdf"


def test_remove_directory_lists_and_deletes_physical_prefix(fake_storage):
    storage = fake_storage()
    storage.client.objects = [
        FakeObject("openrag/law/docs/a.pdf"),
        FakeObject("openrag/law/docs/nested/b.pdf"),
        FakeObject("openrag/other/docs/c.pdf"),
    ]

    storage.remove_directory("law", "docs")

    assert storage.client.list_objects_calls == [
        ("rag-kb", "openrag/law/docs/", True)
    ]
    assert storage.client.remove_object_calls == [
        ("rag-kb", "openrag/law/docs/a.pdf"),
        ("rag-kb", "openrag/law/docs/nested/b.pdf"),
    ]


def test_move_file_uses_physical_copy_source(fake_storage):
    storage = fake_storage()

    storage.move_file("law", "old.pdf", "new.pdf")

    bucket, key, source = storage.client.copy_object_calls[0]
    assert (bucket, key) == ("rag-kb", "openrag/law/new.pdf")
    assert copy_source_bucket(source) == "rag-kb"
    assert copy_source_object(source) == "openrag/law/old.pdf"
    assert storage.client.remove_object_calls == [("rag-kb", "openrag/law/old.pdf")]


def test_remove_document_hierarchy_propagates_access_denied(fake_storage):
    storage = fake_storage(prefix=None)
    failure = S3Error(
        None,
        "AccessDenied",
        "denied",
        "hierarchy/doc.txt.abstract.md",
        "request-id",
        "host-id",
    )

    def fail_stat(*_args, **_kwargs):
        raise failure

    storage.client.stat_object = fail_stat

    with pytest.raises(S3Error) as exc_info:
        storage.remove_document_hierarchy("law", "/doc.txt")

    assert exc_info.value.code == "AccessDenied"


def test_remove_workspace_storage_keeps_neighbor_in_shared_bucket(fake_storage):
    storage = fake_storage(prefix="openrag")
    storage.client.objects = [
        FakeObject("openrag/demo/source.pdf"),
        FakeObject("openrag/demo/parse_artifacts/1/canonical.json"),
        FakeObject("openrag/demo-old/source.pdf"),
    ]

    storage.remove_workspace_storage("demo")

    assert storage.client.list_objects_calls == [
        ("rag-kb", "openrag/demo/", True),
        ("rag-kb", "openrag/demo/", True),
    ]
    assert storage.client.remove_object_calls == [
        ("rag-kb", "openrag/demo/source.pdf"),
        ("rag-kb", "openrag/demo/parse_artifacts/1/canonical.json"),
    ]
    assert storage.client.remove_bucket_calls == []


def test_remove_workspace_storage_deletes_dedicated_bucket(fake_storage):
    storage = fake_storage(prefix=None)
    storage.client.objects = [
        FakeObject("source.pdf"),
        FakeObject("parse_artifacts/1/canonical.json"),
    ]

    storage.remove_workspace_storage("demo")

    assert storage.client.remove_object_calls == [
        ("demo", "source.pdf"),
        ("demo", "parse_artifacts/1/canonical.json"),
    ]
    assert storage.client.remove_bucket_calls == ["demo"]


def test_remove_workspace_storage_fails_if_dedicated_bucket_remains(fake_storage):
    storage = fake_storage(prefix=None)
    storage.client.remove_bucket_effective = False

    with pytest.raises(RuntimeError, match="bucket cleanup incomplete"):
        storage.remove_workspace_storage("demo")
