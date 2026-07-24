import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from openrag.database import get_database_url
from openrag.indexing.activation_service import (
    ActivationConflictError,
    IndexActivationService,
)
from openrag.indexing.alias_reconciler import AliasReconciler
from openrag.indexing.runtime import IndexRuntimeResolver
from openrag.models import Base
from openrag.models.index_generation import (
    IndexGeneration,
    IndexGenerationRoute,
    IndexGenerationState,
)
from openrag.models.user import User


class AliasBackend:
    def __init__(self):
        self.targets = {}

    def target(self, alias):
        return self.targets.get(alias)

    def set_alias(self, alias, collection):
        self.targets[alias] = collection


def _generation(generation_id, state, source=None):
    return IndexGeneration(
        id=generation_id,
        scope="global",
        state=state.value,
        source_generation_id=source,
        embedding_provider="provider",
        embedding_model="model",
        embedding_revision="revision",
        embedding_dimension=3,
        embedding_fingerprint=generation_id[0] * 64,
        embedding_config_ref="embedding/default",
        vector_normalization="none",
        distance_metric="COSINE",
        schema_version=2,
        chunk_policy_revision="chunk-v2",
        hierarchy_policy_revision="hierarchy-v2",
        chunk_collection_name=f"chunks_{generation_id[0]}",
        layer_collection_name=f"layers_{generation_id[0]}",
        manifest={"rollback_window_seconds": 3600},
        validation_report={"passed": True},
        quality_report={"passed": True},
        quality_gate_passed=True,
    )


@pytest.mark.skipif(
    os.getenv("OPENRAG_RUN_POSTGRES_MIGRATION_TESTS") != "1",
    reason="set OPENRAG_RUN_POSTGRES_MIGRATION_TESTS=1 to run local PostgreSQL tests",
)
def test_postgres_activation_lock_allows_one_admin_and_route_snapshots_never_mix():
    url = make_url(get_database_url())
    if url.host not in {"localhost", "127.0.0.1", "::1"}:
        pytest.skip("integration test is restricted to localhost")
    admin_engine = sa.create_engine(url)
    schema = f"r05_activation_{uuid4().hex}"
    quoted = admin_engine.dialect.identifier_preparer.quote(schema)
    with admin_engine.begin() as connection:
        connection.execute(sa.text(f"CREATE SCHEMA {quoted}"))
    engine = sa.create_engine(
        url,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    Session = sessionmaker(bind=engine)
    entered = Event()
    release = Event()
    backend = AliasBackend()
    active_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    candidate_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    try:
        Base.metadata.create_all(engine)
        seed = Session()
        user = User(
            username="activation-admin",
            email="activation-admin@example.com",
            password_hash="hash",
            full_name="Activation Admin",
            is_active=True,
            is_admin=True,
        )
        seed.add(user)
        seed.flush()
        seed.add_all(
            [
                _generation(active_id, IndexGenerationState.ACTIVE),
                _generation(candidate_id, IndexGenerationState.READY, active_id),
                IndexGenerationRoute(
                    scope="global", active_generation_id=active_id, route_version=0
                ),
            ]
        )
        seed.commit()
        user_id = user.id
        seed.close()

        def activate(block):
            db = Session()
            try:
                def final(_generation_id):
                    if block:
                        entered.set()
                        release.wait(timeout=5)
                    return 0

                service = IndexActivationService(
                    db,
                    AliasReconciler(backend),
                    final_reconcile=final,
                    quick_validate=lambda _id: True,
                    smoke_test=lambda _id: True,
                )
                service.activate_generation(
                    candidate_id, expected_route_version=0, activated_by=user_id
                )
                return "success"
            except ActivationConflictError:
                return "conflict"
            finally:
                db.close()

        snapshots = []
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(activate, True)
            assert entered.wait(timeout=5)
            second = pool.submit(activate, False)
            reader = Session()
            for _ in range(20):
                snapshot = IndexRuntimeResolver().get_active_snapshot(reader)
                snapshots.append(
                    (snapshot.generation_id, snapshot.chunk_collection_name, snapshot.layer_collection_name)
                )
                reader.expire_all()
            reader.close()
            release.set()
            outcomes = {first.result(timeout=10), second.result(timeout=10)}

        assert outcomes == {"success", "conflict"}
        final_db = Session()
        final_snapshot = IndexRuntimeResolver().get_active_snapshot(final_db)
        snapshots.append(
            (
                final_snapshot.generation_id,
                final_snapshot.chunk_collection_name,
                final_snapshot.layer_collection_name,
            )
        )
        final_db.close()
        assert all(
            (generation_id == active_id and chunk == "chunks_a" and layer == "layers_a")
            or (
                generation_id == candidate_id
                and chunk == "chunks_b"
                and layer == "layers_b"
            )
            for generation_id, chunk, layer in snapshots
        )
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(sa.text(f"DROP SCHEMA IF EXISTS {quoted} CASCADE"))
        admin_engine.dispose()
