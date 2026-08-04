"""Local external-store acceptance checks run before packaging a deployment."""

from __future__ import annotations

import inspect
import os
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from elasticsearch import Elasticsearch
from minio import Minio
from minio.error import S3Error
from pymilvus import Collection, connections, utility
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateSchema, DropSchema

from openrag.hierarchy.hierarchy_storage import HierarchyStorage
from openrag.models import (
    Base,
    DocumentChunk,
    DocumentParseArtifact,
    File,
    Task,
    User,
    Workspace,
    WorkspaceMember,
)
from openrag.models.task import TaskStatus
from openrag.search.es_chunk_store import EsChunkStore
from openrag.search.workspace_es_slug import (
    build_workspace_chunks_index_name,
    build_workspace_chunks_physical_index_name,
    build_workspace_chunks_read_alias,
    build_workspace_chunks_write_alias,
)
from openrag.search.workspace_index_lifecycle import WorkspaceIndexLifecycle
from openrag.services import file_deletion
from openrag.services.parse_artifact_service import ParseArtifactService
from openrag.services.workspace_service import (
    WorkspaceNotEmptyError,
    WorkspaceService,
)
from openrag.storage.minio_storage import MinioStorage
from openrag.vectorstore.milvus_layer_store import MilvusLayerStore
from openrag.vectorstore.milvus_store import MilvusStore


@contextmanager
def database_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(engine)


@contextmanager
def postgres_database_session():
    database_url = os.getenv("OPENRAG_TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("set OPENRAG_TEST_POSTGRES_URL to run the PostgreSQL check")

    schema = f"accept_{uuid.uuid4().hex[:16]}"
    admin_engine = create_engine(database_url)
    with admin_engine.begin() as connection:
        connection.execute(CreateSchema(schema))
    engine = create_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin_engine.dispose()


def create_workspace(db, slug: str, *, root: bool = False):
    user = User(
        username=f"user-{slug}",
        email=f"{slug}@example.com",
        password_hash="hash",
        full_name="Deployment Acceptance",
        is_active=True,
    )
    db.add(user)
    db.commit()
    workspace = Workspace(name=f"Workspace {slug}", slug=slug, owner_id=user.id)
    db.add(workspace)
    db.commit()
    if root:
        db.add(
            File(
                uri="/",
                name="root",
                owner_id=user.id,
                workspace_id=workspace.id,
                is_directory=True,
                size=0,
            )
        )
        db.commit()
    return user, workspace


def local_minio_client() -> Minio:
    access_key = os.getenv("MINIO_ROOT_USER")
    secret_key = os.getenv("MINIO_ROOT_PASSWORD")
    if not access_key or not secret_key:
        pytest.skip("set explicit MinIO test credentials to run this check")
    client = Minio(
        "localhost:9000",
        access_key=access_key,
        secret_key=secret_key,
        secure=False,
    )
    try:
        client.list_buckets()
    except Exception as exc:
        pytest.skip(f"local MinIO is unavailable: {exc}")
    return client


def remove_bucket_if_present(client: Minio, bucket_name: str) -> None:
    if not client.bucket_exists(bucket_name):
        return
    for item in client.list_objects(bucket_name, recursive=True):
        client.remove_object(bucket_name, item.object_name)
    client.remove_bucket(bucket_name)


def stub_successful_vector_delete(monkeypatch) -> None:
    if hasattr(file_deletion, "delete_vectors_for_file_across_generations"):
        monkeypatch.setattr(
            file_deletion,
            "delete_vectors_for_file_across_generations",
            lambda _db, file_id: {
                "file_id": file_id,
                "target_generation_ids": ["acceptance-generation"],
                "succeeded_generation_ids": ["acceptance-generation"],
                "failed_generation_ids": [],
                "failed_subsystems": [],
            },
        )
        return
    monkeypatch.setattr(
        file_deletion, "delete_milvus_vectors_for_file", lambda _file_id: []
    )


def test_hierarchy_delete_does_not_swallow_storage_failure(monkeypatch):
    storage = object.__new__(MinioStorage)
    failure = S3Error(
        None,
        "AccessDenied",
        "acceptance failure",
        "hierarchy/Doc1.pdf.abstract.md",
        "request-id",
        "host-id",
    )
    monkeypatch.setattr(storage, "file_exists", lambda *_args: True)

    def fail_remove(*_args):
        raise failure

    monkeypatch.setattr(storage, "remove_file", fail_remove)
    monkeypatch.setattr(storage, "remove_directory", lambda *_args: None)

    with pytest.raises(S3Error):
        storage.remove_document_hierarchy("acceptance", "/Doc1.pdf")


def test_real_minio_file_delete_removes_canonical_objects(monkeypatch):
    client = local_minio_client()
    slug = f"accept-canonical-{uuid.uuid4().hex[:10]}"
    client.make_bucket(slug)
    try:
        with database_session() as db:
            user, workspace = create_workspace(db, slug)
            file = File(
                uri="/Doc1.pdf",
                name="Doc1.pdf",
                owner_id=user.id,
                workspace_id=workspace.id,
                is_directory=False,
                size=4,
                mime_type="application/pdf",
            )
            db.add(file)
            db.commit()
            db.refresh(file)

            storage = MinioStorage()
            storage.put_file(slug, file.uri, b"%PDF", "application/pdf")
            artifact = ParseArtifactService(db, storage).persist_parse_artifacts(
                workspace_id=workspace.id,
                file_id=file.id,
                bucket_name=slug,
                file_uri=file.uri,
                source_doc_bytes=b"%PDF",
                blocks=[{"text": "canonical content", "page": 1}],
                parser_name="PDFParser",
                parser_version="1",
            )
            canonical_keys = {
                artifact.canonical_json_object_key,
                artifact.canonical_md_object_key,
            }
            orphan_key = f"parse_artifacts/{file.id}/old-parser/orphan.json"
            neighbor_key = f"parse_artifacts/{file.id}0/current/canonical.json"
            storage.put_file(slug, orphan_key, b"orphan", "application/json")
            storage.put_file(slug, neighbor_key, b"neighbor", "application/json")

            stub_successful_vector_delete(monkeypatch)
            monkeypatch.setattr(
                file_deletion,
                "HierarchyStorage",
                lambda: type(
                    "HierarchyStub",
                    (),
                    {"delete_document_hierarchy": lambda *_a, **_k: None},
                )(),
            )
            file_deletion.delete_file_with_storage(db, file, workspace)
            remaining = {
                item.object_name
                for item in client.list_objects(slug, recursive=True)
            }
    finally:
        remove_bucket_if_present(client, slug)

    assert canonical_keys.isdisjoint(remaining)
    assert not any(
        key.startswith(f"parse_artifacts/{file.id}/") for key in remaining
    )
    assert neighbor_key in remaining


def test_real_postgres_sync_directory_delete_removes_descendants(monkeypatch):
    client = local_minio_client()
    slug = f"accept-dir-{uuid.uuid4().hex[:10]}"
    client.make_bucket(slug)
    try:
        with postgres_database_session() as db:
            user, workspace = create_workspace(db, slug)
            directory = File(
                uri="/dir",
                name="dir",
                owner_id=user.id,
                workspace_id=workspace.id,
                is_directory=True,
                size=0,
            )
            child = File(
                uri="/dir/child.txt",
                name="child.txt",
                owner_id=user.id,
                workspace_id=workspace.id,
                is_directory=False,
                size=5,
            )
            grandchild = File(
                uri="/dir/sub/grandchild.txt",
                name="grandchild.txt",
                owner_id=user.id,
                workspace_id=workspace.id,
                is_directory=False,
                size=10,
            )
            neighbor = File(
                uri="/dir-other/keep.txt",
                name="keep.txt",
                owner_id=user.id,
                workspace_id=workspace.id,
                is_directory=False,
                size=4,
            )
            db.add_all([directory, child, grandchild, neighbor])
            db.commit()
            deleted_ids = {directory.id, child.id, grandchild.id}
            neighbor_id = neighbor.id

            storage = MinioStorage()
            for row in (child, grandchild, neighbor):
                storage.put_file(slug, row.uri, row.name.encode())
            storage.put_file(
                slug,
                f"parse_artifacts/{child.id}/legacy/orphan.json",
                b"orphan",
                "application/json",
            )
            stub_successful_vector_delete(monkeypatch)
            monkeypatch.setattr(
                file_deletion,
                "HierarchyStorage",
                lambda: type(
                    "HierarchyStub",
                    (),
                    {"delete_document_hierarchy": lambda *_a, **_k: None},
                )(),
            )

            file_deletion.delete_file_with_storage(db, directory, workspace)

            assert db.query(File).filter(File.id.in_(deleted_ids)).count() == 0
            assert db.get(File, neighbor_id) is not None
            remaining = {
                item.object_name for item in client.list_objects(slug, recursive=True)
            }
            assert remaining == {neighbor.uri.lstrip("/")}
    finally:
        remove_bucket_if_present(client, slug)


def test_real_postgres_active_task_blocks_workspace_delete():
    client = local_minio_client()
    slug = f"accept-task-{uuid.uuid4().hex[:10]}"
    client.make_bucket(slug)
    try:
        with postgres_database_session() as db:
            user, workspace = create_workspace(db, slug, root=True)
            workspace_id = workspace.id
            task = Task(
                task_id=f"task-{uuid.uuid4().hex}",
                workspace_id=workspace_id,
                user_id=user.id,
                task_type="process_document",
                status=TaskStatus.PENDING.value,
            )
            db.add(task)
            db.commit()

            with pytest.raises(WorkspaceNotEmptyError, match="active tasks"):
                WorkspaceService(db).delete_workspace(workspace_id)

            assert db.get(Workspace, workspace_id) is not None
            assert client.bucket_exists(slug)

            task.status = TaskStatus.SUCCESS.value
            db.commit()
            assert WorkspaceService(db).delete_workspace(workspace_id) is True
            assert db.get(Workspace, workspace_id) is None
            assert not client.bucket_exists(slug)
    finally:
        remove_bucket_if_present(client, slug)


def test_real_postgres_file_and_workspace_rows_are_cleaned(monkeypatch):
    client = local_minio_client()
    slug = f"accept-pg-{uuid.uuid4().hex[:10]}"
    client.make_bucket(slug)
    try:
        with postgres_database_session() as db:
            user, workspace = create_workspace(db, slug, root=True)
            db.add(
                WorkspaceMember(
                    workspace_id=workspace.id,
                    user_id=user.id,
                    role="write",
                )
            )
            file = File(
                uri="/Doc1.pdf",
                name="Doc1.pdf",
                owner_id=user.id,
                workspace_id=workspace.id,
                is_directory=False,
                size=4,
                mime_type="application/pdf",
            )
            db.add(file)
            db.flush()
            file_id = file.id
            workspace_id = workspace.id
            db.add(
                DocumentChunk(
                    file_id=file_id,
                    workspace_id=workspace_id,
                    chunk_id=f"accept-pg-{uuid.uuid4().hex}",
                    chunk_index=0,
                    object_key="hierarchy/Doc1.pdf/chunks/0000.md",
                    text_preview="chunk",
                )
            )
            db.commit()

            storage = MinioStorage()
            storage.put_file(slug, file.uri, b"%PDF", "application/pdf")
            ParseArtifactService(db, storage).persist_parse_artifacts(
                workspace_id=workspace_id,
                file_id=file_id,
                bucket_name=slug,
                file_uri=file.uri,
                source_doc_bytes=b"%PDF",
                blocks=[{"text": "canonical content", "page": 1}],
                parser_name="PDFParser",
                parser_version="1",
            )

            stub_successful_vector_delete(monkeypatch)
            monkeypatch.setattr(
                file_deletion,
                "HierarchyStorage",
                lambda: type(
                    "HierarchyStub",
                    (),
                    {"delete_document_hierarchy": lambda *_a, **_k: None},
                )(),
            )
            file_deletion.delete_file_with_storage(db, file, workspace)

            assert db.get(File, file_id) is None
            assert (
                db.query(DocumentChunk)
                .filter(DocumentChunk.file_id == file_id)
                .count()
                == 0
            )
            assert (
                db.query(DocumentParseArtifact)
                .filter(DocumentParseArtifact.file_id == file_id)
                .count()
                == 0
            )

            WorkspaceService(db).delete_workspace(workspace_id)
            assert db.get(Workspace, workspace_id) is None
            assert (
                db.query(File).filter(File.workspace_id == workspace_id).count()
                == 0
            )
            assert (
                db.query(WorkspaceMember)
                .filter(WorkspaceMember.workspace_id == workspace_id)
                .count()
                == 0
            )
    finally:
        remove_bucket_if_present(client, slug)


def test_workspace_delete_removes_empty_minio_bucket():
    client = local_minio_client()
    slug = f"accept-bucket-{uuid.uuid4().hex[:10]}"
    client.make_bucket(slug)
    try:
        with database_session() as db:
            _, workspace = create_workspace(db, slug, root=True)
            WorkspaceService(db).delete_workspace(workspace.id)
            bucket_exists_after_delete = client.bucket_exists(slug)
    finally:
        remove_bucket_if_present(client, slug)

    assert bucket_exists_after_delete is False


def local_es_store() -> EsChunkStore:
    store = EsChunkStore(["http://localhost:9200"], verify_certs=False)
    if not store.ping():
        pytest.skip("local Elasticsearch is unavailable")
    return store


def test_legacy_workspace_delete_removes_legacy_index():
    store = local_es_store()
    slug = f"accept-legacy-{uuid.uuid4().hex[:10]}"
    with database_session() as db:
        _, workspace = create_workspace(db, slug, root=True)
        index_name = build_workspace_chunks_index_name(slug, workspace.id)
        store.ensure_index(index_name)
        try:
            WorkspaceService(
                db,
                index_lifecycle=WorkspaceIndexLifecycle(store),
                chunk_index_mode="legacy",
            ).delete_workspace(workspace.id)
            exists_after_delete = bool(
                store.client.indices.exists(index=index_name)
            )
        finally:
            store.delete_index_if_exists(index_name)

    assert exists_after_delete is False


def test_v2_workspace_delete_removes_indices_and_aliases():
    store = local_es_store()
    lifecycle = WorkspaceIndexLifecycle(store)
    slug = f"accept-v2-{uuid.uuid4().hex[:10]}"
    with database_session() as db:
        _, workspace = create_workspace(db, slug, root=True)
        workspace_id = workspace.id
        lifecycle.ensure_ready(workspace_id=workspace_id, workspace_slug=slug)
        physical = build_workspace_chunks_physical_index_name(slug, workspace_id)
        read_alias = build_workspace_chunks_read_alias(slug, workspace_id)
        write_alias = build_workspace_chunks_write_alias(slug, workspace_id)
        try:
            WorkspaceService(
                db,
                index_lifecycle=lifecycle,
                chunk_index_mode="v2_alias",
            ).delete_workspace(workspace_id)
            state = {
                "physical": bool(store.client.indices.exists(index=physical)),
                "read_alias": bool(
                    store.client.indices.exists_alias(name=read_alias)
                ),
                "write_alias": bool(
                    store.client.indices.exists_alias(name=write_alias)
                ),
            }
        finally:
            lifecycle.delete_workspace_indices(
                workspace_id=workspace_id, workspace_slug=slug
            )

    assert state == {
        "physical": False,
        "read_alias": False,
        "write_alias": False,
    }


def test_milvus_chunk_and_layer_deletes_keep_other_file():
    try:
        connections.connect(host="localhost", port="19530")
        utility.list_collections()
    except Exception as exc:
        pytest.skip(f"local Milvus is unavailable: {exc}")

    chunk_collection = f"accept_chunks_{uuid.uuid4().hex[:10]}"
    layer_collection = f"accept_layers_{uuid.uuid4().hex[:10]}"
    try:
        try:
            from openrag.indexing.milvus_provisioner import (
                MilvusCollectionProvisioner,
                PyMilvusAdminBackend,
            )
        except ImportError:
            pass
        else:
            MilvusCollectionProvisioner(
                PyMilvusAdminBackend(host="localhost", port=19530)
            ).provision_generation(
                {
                    "generation_id": str(uuid.uuid4()),
                    "source_generation_id": None,
                    "embedding_dimension": 4,
                    "embedding_fingerprint": "a" * 64,
                    "schema_version": 1,
                    "chunk_collection_name": chunk_collection,
                    "layer_collection_name": layer_collection,
                }
            )
        chunks = MilvusStore(dimension=4, collection_name=chunk_collection)
        layers = MilvusLayerStore(dimension=4, collection_name=layer_collection)
        for file_id in (101, 202):
            chunks.insert_chunks(
                file_id,
                [
                    (
                        SimpleNamespace(
                            chunk_id=f"chunk-{file_id}",
                            text=f"text {file_id}",
                            page=1,
                            level=0,
                            block_type="text",
                        ),
                        [1.0, 0.0, 0.0, 0.0],
                    )
                ],
            )
            if "layer_embeddings" in inspect.signature(
                layers.upsert_file_layers
            ).parameters:
                layers.upsert_file_layers(
                    file_id,
                    [
                        ("l0", f"l0 {file_id}", [1.0, 0.0, 0.0, 0.0]),
                        ("l1", f"l1 {file_id}", [1.0, 0.0, 0.0, 0.0]),
                    ],
                )
            else:
                layers.upsert_file_layers(
                    file_id,
                    f"l0 {file_id}",
                    f"l1 {file_id}",
                    lambda _text: [1.0, 0.0, 0.0, 0.0],
                )
        Collection(chunk_collection).flush()
        Collection(layer_collection).flush()

        chunks.delete_by_file_id(101)
        layers.delete_by_file_id(101)
        Collection(layer_collection).flush()

        chunk_file_ids = {
            int(row["file_id"])
            for row in Collection(chunk_collection).query(
                expr="file_id in [101,202]", output_fields=["file_id"]
            )
        }
        layer_file_ids = {
            int(row["file_id"])
            for row in Collection(layer_collection).query(
                expr="file_id in [101,202]", output_fields=["file_id"]
            )
        }
    finally:
        if utility.has_collection(chunk_collection):
            utility.drop_collection(chunk_collection)
        if utility.has_collection(layer_collection):
            utility.drop_collection(layer_collection)

    assert chunk_file_ids == {202}
    assert layer_file_ids == {202}


def test_real_milvus_delete_reaches_active_and_retired_generations(monkeypatch):
    if not hasattr(file_deletion, "delete_vectors_for_file_across_generations"):
        pytest.skip("generation-aware deletion is not present on accuracy-fix")

    try:
        connections.connect(host="localhost", port="19530")
        utility.list_collections()
    except Exception as exc:
        pytest.skip(f"local Milvus is unavailable: {exc}")

    from openrag.indexing.milvus_provisioner import (
        MilvusCollectionProvisioner,
        PyMilvusAdminBackend,
    )
    from openrag.models.index_generation import (
        IndexGeneration,
        IndexGenerationState,
    )

    backend = PyMilvusAdminBackend(host="localhost", port=19530)
    provisioner = MilvusCollectionProvisioner(backend)
    resources: list[tuple[str, str]] = []
    runtimes: list[tuple[MilvusStore, MilvusLayerStore]] = []
    generation_ids: list[str] = []
    try:
        with database_session() as db:
            for marker, state in (
                ("a", IndexGenerationState.RETIRED.value),
                ("b", IndexGenerationState.ACTIVE.value),
            ):
                suffix = uuid.uuid4().hex[:10]
                generation_id = str(uuid.uuid4())
                chunk_collection = f"accept_gen_chunks_{suffix}"
                layer_collection = f"accept_gen_layers_{suffix}"
                fingerprint = marker * 64
                manifest = {
                    "generation_id": generation_id,
                    "source_generation_id": None,
                    "embedding_dimension": 4,
                    "embedding_fingerprint": fingerprint,
                    "schema_version": 1,
                    "chunk_collection_name": chunk_collection,
                    "layer_collection_name": layer_collection,
                }
                provisioner.provision_generation(manifest)
                resources.append((chunk_collection, layer_collection))
                generation_ids.append(generation_id)
                db.add(
                    IndexGeneration(
                        id=generation_id,
                        scope="global",
                        state=state,
                        embedding_provider="acceptance",
                        embedding_model="acceptance-model",
                        embedding_revision="1",
                        embedding_dimension=4,
                        embedding_fingerprint=fingerprint,
                        embedding_config_ref="acceptance/local",
                        vector_normalization="none",
                        distance_metric="COSINE",
                        schema_version=1,
                        chunk_policy_revision="acceptance-chunk-v1",
                        hierarchy_policy_revision="acceptance-hierarchy-v1",
                        chunk_collection_name=chunk_collection,
                        layer_collection_name=layer_collection,
                        manifest=manifest,
                    )
                )
                runtimes.append(
                    (
                        MilvusStore(
                            collection_name=chunk_collection,
                            dimension=4,
                            expected_schema_version=1,
                            expected_embedding_fingerprint=fingerprint,
                        ),
                        MilvusLayerStore(
                            collection_name=layer_collection,
                            dimension=4,
                            expected_schema_version=1,
                            expected_embedding_fingerprint=fingerprint,
                        ),
                    )
                )
            db.commit()

            for chunks, layers in runtimes:
                for file_id in (101, 202):
                    chunks.insert_chunks(
                        file_id,
                        [
                            (
                                SimpleNamespace(
                                    chunk_id=f"chunk-{file_id}",
                                    text=f"text {file_id}",
                                    page=1,
                                    level=0,
                                    block_type="text",
                                ),
                                [1.0, 0.0, 0.0, 0.0],
                            )
                        ],
                    )
                    layers.upsert_file_layers(
                        file_id,
                        [
                            ("l0", f"l0 {file_id}", [1.0, 0.0, 0.0, 0.0]),
                            ("l1", f"l1 {file_id}", [1.0, 0.0, 0.0, 0.0]),
                        ],
                    )
                chunks._collection.flush()
                layers._collection.flush()

            monkeypatch.setattr(
                file_deletion,
                "_delete_elasticsearch_chunks_for_file_best_effort",
                lambda _file_id: True,
            )
            result = file_deletion.delete_vectors_for_file_across_generations(
                db, 101
            )

            observed: list[tuple[set[int], set[int]]] = []
            for chunks, layers in runtimes:
                layers._collection.flush()
                chunk_ids = {
                    int(row["file_id"])
                    for row in chunks._collection.query(
                        expr="file_id in [101,202]", output_fields=["file_id"]
                    )
                }
                layer_ids = {
                    int(row["file_id"])
                    for row in layers._collection.query(
                        expr="file_id in [101,202]", output_fields=["file_id"]
                    )
                }
                observed.append((chunk_ids, layer_ids))
    finally:
        for chunk_collection, layer_collection in resources:
            if utility.has_collection(chunk_collection):
                utility.drop_collection(chunk_collection)
            if utility.has_collection(layer_collection):
                utility.drop_collection(layer_collection)

    assert result["target_generation_ids"] == generation_ids
    assert result["failed_generation_ids"] == []
    assert observed == [({202}, {202}), ({202}, {202})]
