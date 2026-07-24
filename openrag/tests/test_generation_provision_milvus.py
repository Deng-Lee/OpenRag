"""Opt-in real Milvus integration for candidate provisioning."""

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from openrag.config import get_config
from openrag.indexing.milvus_cleaner import (
    MilvusCollectionCleaner,
    PyMilvusCleanupBackend,
)
from openrag.indexing.milvus_provisioner import (
    MilvusCollectionProvisioner,
    PyMilvusAdminBackend,
)
from openrag.indexing.provision_service import ProvisionService
from openrag.indexing.reindex_processor import GenerationReindexProcessor
from openrag.indexing.runtime import IndexRuntime
from openrag.indexing.source_reader import GenerationSourceSnapshot
from openrag.indexing.validator import (
    IndexGenerationValidator,
    PyMilvusValidationBackend,
)
from openrag.chunking.chunk_models import Chunk
from openrag.models import Base
from openrag.models.file import File, ProcessingStatus
from openrag.models.index_generation import IndexGenerationFile, IndexGenerationFileState
from openrag.models.user import User
from openrag.models.workspace import Workspace
from openrag.services.index_generation_service import IndexGenerationService
from openrag.vectorstore.milvus_layer_store import MilvusLayerStore
from openrag.vectorstore.milvus_store import MilvusStore


@pytest.mark.skipif(
    os.getenv("OPENRAG_RUN_MILVUS_INTEGRATION_TESTS") != "1",
    reason="set OPENRAG_RUN_MILVUS_INTEGRATION_TESTS=1 to run local Milvus tests",
)
def test_real_milvus_candidate_provision_is_idempotent_and_cleaned():
    vector = get_config().vector_db
    if vector.host not in {"localhost", "127.0.0.1", "::1"}:
        pytest.skip("destructive integration test is restricted to localhost")
    admin_password = (
        vector.admin_password.get_secret_value()
        if vector.admin_password is not None
        else None
    )
    suffix = uuid4().hex
    generation_id = str(uuid4())
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    user = User(
        username=f"milvus-{suffix}",
        email=f"milvus-{suffix}@example.com",
        password_hash="hash",
        full_name="Milvus Validation",
        is_active=True,
    )
    db.add(user)
    db.flush()
    workspace = Workspace(name="Milvus Validation", slug=f"milvus-{suffix}", owner_id=user.id)
    db.add(workspace)
    db.flush()
    file = File(
        uri="docs/validation.txt",
        name="validation.txt",
        owner_id=user.id,
        workspace_id=workspace.id,
        is_directory=False,
        size=1,
        processing_status=ProcessingStatus.completed,
    )
    db.add(file)
    db.flush()
    service = IndexGenerationService(db)
    generation = service.create_generation(
        id=generation_id,
        scope="global",
        embedding_provider="integration",
        embedding_model="integration-model",
        embedding_revision="integration-revision",
        embedding_dimension=3,
        embedding_fingerprint=suffix.ljust(64, "0")[:64],
        embedding_config_ref="integration/local",
        vector_normalization="none",
        distance_metric="COSINE",
        schema_version=2,
        chunk_policy_revision="integration-chunk-v1",
        hierarchy_policy_revision="integration-hierarchy-v1",
        chunk_collection_name=f"openrag_chunks_g_test_{suffix}",
        layer_collection_name=f"openrag_layers_g_test_{suffix}",
        manifest={
            "generation_id": generation_id,
            "source_generation_id": None,
            "embedding_dimension": 3,
            "embedding_fingerprint": suffix.ljust(64, "0")[:64],
            "schema_version": 2,
            "chunk_collection_name": f"openrag_chunks_g_test_{suffix}",
            "layer_collection_name": f"openrag_layers_g_test_{suffix}",
        },
    )
    backend = PyMilvusAdminBackend(
        host=vector.host,
        port=vector.port,
        user=vector.admin_user,
        password=admin_password,
    )
    provision = ProvisionService(service, MilvusCollectionProvisioner(backend))
    cleanup = MilvusCollectionCleaner(
        PyMilvusCleanupBackend(
            host=vector.host,
            port=vector.port,
            user=vector.admin_user,
            password=admin_password,
        )
    )
    try:
        first = provision.provision(generation.id)
        second = provision.provision(generation.id)
        assert first.changed is True
        assert second.changed is False
        assert first.physical_collections == {
            "chunks": generation.chunk_collection_name,
            "layers": generation.layer_collection_name,
        }
        common = {
            "host": vector.host,
            "port": vector.port,
            "dimension": 3,
            "expected_schema_version": 2,
            "expected_embedding_fingerprint": generation.embedding_fingerprint,
        }
        runtime = IndexRuntime(
            snapshot=SimpleNamespace(
                generation_id=generation.id,
                embedding_fingerprint=generation.embedding_fingerprint,
                chunk_collection_name=generation.chunk_collection_name,
                layer_collection_name=generation.layer_collection_name,
            ),
            embedding_engine=SimpleNamespace(
                assert_fingerprint=lambda value: value
                == generation.embedding_fingerprint,
                embed_chunks=lambda chunks: [
                    (chunk, [1.0, 0.0, 0.0]) for chunk in chunks
                ],
                embed_batch=lambda texts: [[0.0, 1.0, 0.0] for _ in texts],
            ),
            vector_store=MilvusStore(
                collection_name=generation.chunk_collection_name, **common
            ),
            layer_store=MilvusLayerStore(
                collection_name=generation.layer_collection_name, **common
            ),
        )
        source = GenerationSourceSnapshot(
            file_id=file.id,
            workspace_id=workspace.id,
            source_updated_at=datetime.now(timezone.utc),
            source_content_hash="c" * 64,
            chunks=(Chunk(text="candidate chunk", chunk_id=f"chunk-{suffix}"),),
            layers=(("l0", "candidate summary"), ("l1", "candidate overview")),
        )
        reader = SimpleNamespace(get_source_revision=lambda _file_id: source)
        result = GenerationReindexProcessor(reader, runtime).reindex_file(
            source.file_id, expected_source_content_hash=source.source_content_hash
        )
        runtime.vector_store._collection.flush()
        runtime.layer_store._collection.flush()
        chunk_rows = runtime.vector_store._collection.query(
            expr=f"file_id == {source.file_id}",
            output_fields=["chunk_id", "file_id", "workspace_id"],
        )
        layer_rows = runtime.layer_store._collection.query(
            expr=f"file_id == {source.file_id}",
            output_fields=["layer_row_id", "file_id", "workspace_id"],
        )
        assert result.written_chunk_count == len(chunk_rows) == 1
        assert result.written_layer_count == len(layer_rows) == 2
        assert {row["workspace_id"] for row in chunk_rows + layer_rows} == {
            workspace.id
        }
        db.add(
            IndexGenerationFile(
                generation_id=generation.id,
                file_id=file.id,
                workspace_id=workspace.id,
                state=IndexGenerationFileState.SUCCESS.value,
                source_content_hash=source.source_content_hash,
                source_updated_at=source.source_updated_at,
                expected_chunk_count=1,
                written_chunk_count=1,
                expected_layer_count=2,
                written_layer_count=2,
            )
        )
        generation.state = "reconciling"
        generation.build_lag_files = 0
        db.commit()
        runtime_password = (
            vector.runtime_password.get_secret_value()
            if vector.runtime_password is not None
            else None
        )
        validation = IndexGenerationValidator(
            db,
            generation,
            PyMilvusValidationBackend(
                host=vector.host,
                port=vector.port,
                user=vector.runtime_user,
                password=runtime_password,
                secure=vector.secure,
            ),
            embedding_probe=lambda: [1.0, 0.0, 0.0],
        ).build_report()
        assert validation["passed"] is True, validation
    finally:
        db.refresh(generation)
        generation.state = "retired"
        generation.delete_after = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
        cleanup.drop_generation_collections(
            generation,
            active_id=None,
            previous_id=None,
            confirmation=generation.id,
        )
        assert backend.has_collection(generation.chunk_collection_name) is False
        assert backend.has_collection(generation.layer_collection_name) is False
        db.close()
        Base.metadata.drop_all(engine)
