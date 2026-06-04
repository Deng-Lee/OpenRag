import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import openrag.models  # noqa: F401 - register models
from openrag.models import DocumentParseArtifact
from openrag.models.base import Base
from openrag.models.user import User
from openrag.models.workspace import Workspace
from openrag.parsers.base import DocumentBlock
from openrag.services.parse_artifact_service import ParseArtifactService


class FakeMinioStorage:
    def __init__(self):
        self.objects = {}

    def put_file(self, bucket_name, object_name, data, content_type="application/octet-stream"):
        self.objects[(bucket_name, object_name)] = {
            "data": data,
            "content_type": content_type,
        }


def _db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    return engine, Session()


def _seed_workspace(db):
    user = User(
        username="u1",
        email="u1@example.com",
        password_hash="x",
        full_name="User One",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    workspace = Workspace(name="WS", slug="ws", owner_id=user.id)
    db.add(workspace)
    db.commit()
    db.refresh(workspace)
    return user, workspace


def test_parse_artifact_service_writes_canonical_objects_and_upserts_reference():
    engine, db = _db_session()
    try:
        _, workspace = _seed_workspace(db)
        storage = FakeMinioStorage()
        service = ParseArtifactService(db, storage)
        blocks = [
            DocumentBlock(
                text="Title",
                page=1,
                offset=0,
                block_type="heading",
                level=1,
                block_id="b1",
                char_start=0,
                char_end=5,
            ),
            DocumentBlock(
                text="SECRET FULL PARSE BODY",
                page=2,
                offset=6,
                bbox=(1.0, 2.0, 3.0, 4.0),
                block_type="table",
                table_data={"rows": [["a", "b"]]},
                block_id="b2",
                char_start=6,
                char_end=28,
            ),
        ]

        result = service.persist_parse_artifacts(
            workspace_id=workspace.id,
            file_id=10,
            bucket_name=workspace.slug,
            file_uri="/docs/report.pdf",
            source_doc_bytes=b"source-v1",
            blocks=blocks,
            parser_name="FakeParser",
            parser_version="1.0",
        )

        assert result.status == "completed"
        assert result.block_count == 2
        assert result.page_count == 2
        assert result.block_type_counts == {"heading": 1, "table": 1}
        assert result.canonical_json_object_key.endswith("/canonical.json")
        assert result.canonical_md_object_key.endswith("/canonical.md")
        assert result.canonical_text_hash

        json_object = storage.objects[(workspace.slug, result.canonical_json_object_key)]
        md_object = storage.objects[(workspace.slug, result.canonical_md_object_key)]
        assert json_object["content_type"] == "application/json; charset=utf-8"
        assert md_object["content_type"] == "text/markdown; charset=utf-8"
        payload = json.loads(json_object["data"].decode("utf-8"))
        assert payload["blocks"][1]["text"] == "SECRET FULL PARSE BODY"
        assert payload["blocks"][1]["table_data"] == {"rows": [["a", "b"]]}
        assert "Page 2" in md_object["data"].decode("utf-8")

        row = db.query(DocumentParseArtifact).one()
        assert row.artifact_id == result.artifact_id
        assert row.canonical_json_object_key == result.canonical_json_object_key

        updated = service.persist_parse_artifacts(
            workspace_id=workspace.id,
            file_id=10,
            bucket_name=workspace.slug,
            file_uri="/docs/report.pdf",
            source_doc_bytes=b"source-v1",
            blocks=blocks[:1],
            parser_name="FakeParser",
            parser_version="1.0",
        )

        assert updated.id == row.id
        assert db.query(DocumentParseArtifact).count() == 1
        assert db.query(DocumentParseArtifact).one().block_count == 1
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_parse_artifact_service_uses_canonical_text_override():
    engine, db = _db_session()
    try:
        _, workspace = _seed_workspace(db)
        storage = FakeMinioStorage()
        service = ParseArtifactService(db, storage)
        blocks = [
            DocumentBlock(
                text="Alpha",
                page=1,
                offset=0,
                block_id="txt:p:0",
                char_start=0,
                char_end=5,
            ),
            DocumentBlock(
                text="Beta",
                page=1,
                offset=6,
                block_id="txt:p:1",
                char_start=6,
                char_end=10,
            ),
        ]

        result = service.persist_parse_artifacts(
            workspace_id=workspace.id,
            file_id=11,
            bucket_name=workspace.slug,
            file_uri="/docs/notes.txt",
            source_doc_bytes=b"source-v2",
            blocks=blocks,
            parser_name="TxtParserAdapter",
            parser_version="1.0",
            canonical_text_override="Alpha\nBeta",
            canonical_source={"version": "chunk_source_v1"},
        )

        md_object = storage.objects[(workspace.slug, result.canonical_md_object_key)]
        json_object = storage.objects[(workspace.slug, result.canonical_json_object_key)]
        payload = json.loads(json_object["data"].decode("utf-8"))

        assert md_object["data"].decode("utf-8") == "Alpha\nBeta"
        assert payload["canonical_source"] == {"version": "chunk_source_v1"}
        assert payload["canonical_text_hash"] == result.canonical_text_hash
        assert payload["blocks"][1]["char_start"] == 6
        assert payload["blocks"][1]["char_end"] == 10
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
