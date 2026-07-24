"""Explicit generation cleanup; dry-run unless --execute is supplied."""

import argparse
import json

from openrag.config import get_config
from openrag.database import SessionLocal, get_engine
from openrag.indexing.backup_service import BackupService
from openrag.indexing.milvus_cleaner import MilvusCollectionCleaner, PyMilvusCleanupBackend
from openrag.indexing.retention_service import RetentionService
from openrag.models.index_generation import IndexGeneration, IndexGenerationRoute


def _service(db):
    vector = get_config().vector_db
    if not vector.cleanup_user or vector.cleanup_password is None:
        raise RuntimeError("Dedicated MILVUS_CLEANUP credentials are required")
    cleaner = MilvusCollectionCleaner(
        PyMilvusCleanupBackend(
            host=vector.host,
            port=vector.port,
            user=vector.cleanup_user,
            password=vector.cleanup_password.get_secret_value(),
        )
    )
    return RetentionService(db, cleaner, BackupService(None)), cleaner


def cleanup_generation_dry_run(db, generation_id: str) -> dict:
    service, _ = _service(db)
    return service.build_delete_plan(generation_id)


def cleanup_generation_execute(
    db, generation_id: str, *, confirmation: str, deleted_by: int
) -> dict:
    if confirmation != generation_id:
        raise RuntimeError("Exact generation_id confirmation is required")
    service, cleaner = _service(db)
    plan = service.build_delete_plan(generation_id)
    generation = db.get(IndexGeneration, generation_id)
    service.mark_deleting(generation_id, deleted_by=deleted_by, plan=plan)
    route = db.get(IndexGenerationRoute, generation.scope)
    cleaner.drop_generation_collections(
        generation,
        active_id=route.active_generation_id if route else None,
        previous_id=route.previous_generation_id if route else None,
        confirmation=confirmation,
    )
    service.mark_deleted(generation_id)
    return {**plan, "state": "deleted"}


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenRag generation cleanup")
    parser.add_argument("generation_id")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument("--operator-id", type=int)
    args = parser.parse_args()
    SessionLocal.configure(bind=get_engine())
    db = SessionLocal()
    try:
        if args.execute:
            if args.operator_id is None:
                parser.error("--operator-id is required with --execute")
            result = cleanup_generation_execute(
                db,
                args.generation_id,
                confirmation=args.confirm or "",
                deleted_by=args.operator_id,
            )
        else:
            result = cleanup_generation_dry_run(db, args.generation_id)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
