"""Explicit index generation administration commands."""

import argparse
import json
from dataclasses import asdict

from openrag.config import get_config
from openrag.database import SessionLocal, get_engine
from openrag.indexing.legacy_bootstrap import (
    MilvusLegacyInspector,
    bootstrap_legacy_generation,
    print_legacy_bootstrap_dry_run,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="OpenRag index generation administration"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    bootstrap = subparsers.add_parser(
        "bootstrap-legacy", description="Register existing legacy Collections"
    )
    bootstrap.add_argument("--operator", required=True)
    bootstrap.add_argument("--operator-id", type=int)
    bootstrap.add_argument(
        "--execute",
        action="store_true",
        help="write registry rows; without this flag the command is dry-run only",
    )
    args = parser.parse_args()

    config = get_config()
    SessionLocal.configure(bind=get_engine())
    db = SessionLocal()
    inspector = MilvusLegacyInspector(
        host=config.vector_db.host,
        port=config.vector_db.port,
    )
    try:
        kwargs = {
            "inspector": inspector,
            "embedding_config": config.embedding,
            "operator": args.operator,
            "operator_id": args.operator_id,
        }
        if not args.execute:
            print(print_legacy_bootstrap_dry_run(db, **kwargs))
        else:
            result = bootstrap_legacy_generation(db, **kwargs)
            print(
                json.dumps(asdict(result), ensure_ascii=False, indent=2, sort_keys=True)
            )
    finally:
        inspector.close()
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
