import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from openrag.indexing.milvus_provisioner import MilvusCollectionProvisioner
from openrag.indexing.provision_service import ProvisionService
from openrag.models import Base
from openrag.models.index_generation import IndexGenerationRoute, IndexGenerationState
from openrag.services.index_generation_service import IndexGenerationService


class Backend:
    def __init__(self, fail_layers=False):
        self.collections = {}
        self.create_calls = []
        self.fail_layers = fail_layers

    def has_collection(self, name):
        return name in self.collections

    def create_collection(self, name, schema, metadata):
        if self.fail_layers and "layers" in name:
            raise RuntimeError("secret backend failure")
        self.create_calls.append(name)
        self.collections[name] = {
            "schema": schema.to_dict(),
            "metadata": dict(metadata),
            "indexes": [],
        }

    def create_index(self, name, field_name, index_params):
        self.collections[name]["indexes"].append(
            {"field_name": field_name, **index_params}
        )

    def load_collection(self, _name):
        return None

    def describe_collection(self, name):
        return self.collections[name]


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


def generation_values(suffix):
    generation_id = f"generation-{suffix}"
    return {
        "id": generation_id,
        "scope": "global",
        "embedding_provider": "provider",
        "embedding_model": "model",
        "embedding_revision": "revision",
        "embedding_dimension": 3,
        "embedding_fingerprint": suffix * 64,
        "embedding_config_ref": "embedding/default",
        "vector_normalization": "none",
        "distance_metric": "COSINE",
        "schema_version": 2,
        "chunk_policy_revision": "chunk-v1",
        "hierarchy_policy_revision": "hierarchy-v1",
        "chunk_collection_name": f"chunks_{suffix}",
        "layer_collection_name": f"layers_{suffix}",
        "manifest": {
            "generation_id": generation_id,
            "embedding_dimension": 3,
            "embedding_fingerprint": suffix * 64,
            "schema_version": 2,
            "chunk_collection_name": f"chunks_{suffix}",
            "layer_collection_name": f"layers_{suffix}",
            "source_generation_id": None,
        },
    }


def test_provision_creates_empty_candidate_and_retry_is_idempotent(db):
    generation_service = IndexGenerationService(db)
    candidate = generation_service.create_generation(**generation_values("b"))
    backend = Backend()
    service = ProvisionService(generation_service, MilvusCollectionProvisioner(backend))

    first = service.provision(candidate.id)
    second = service.provision(candidate.id)

    assert first.generation.state == IndexGenerationState.BUILDING.value
    assert first.changed is True
    assert second.changed is False
    assert backend.create_calls == ["chunks_b", "layers_b"]


def test_partial_failure_keeps_active_route_and_does_not_drop_created_chunk(db):
    generation_service = IndexGenerationService(db)
    active = generation_service.create_generation(**generation_values("a"))
    active.state = IndexGenerationState.ACTIVE.value
    candidate = generation_service.create_generation(**generation_values("b"))
    db.add(
        IndexGenerationRoute(
            scope="global", active_generation_id=active.id, route_version=1
        )
    )
    db.commit()
    backend = Backend(fail_layers=True)
    service = ProvisionService(generation_service, MilvusCollectionProvisioner(backend))

    with pytest.raises(RuntimeError, match="INDEX_PROVISION_FAILED") as failure:
        service.provision(candidate.id)

    db.refresh(candidate)
    assert str(failure.value) == "INDEX_PROVISION_FAILED"
    assert candidate.state == IndexGenerationState.FAILED.value
    assert candidate.last_error == "Candidate collection provisioning failed"
    assert generation_service.get_route().active_generation_id == active.id
    assert "chunks_b" in backend.collections
