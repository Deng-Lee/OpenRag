"""Preview-token scoped read-only APIs for embedded document previews."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from openrag.api.deps import get_db
from openrag.api import workspace_file_api as workspace_files
from openrag.models.document_chunk import DocumentChunk
from openrag.models.file import File as FileModel
from openrag.services.preview_token_service import (
    PreviewTokenClaims,
    decode_preview_token,
)


router = APIRouter(prefix="/embed/v1", tags=["embed-preview"])


class EmbedDocumentPreviewResponse(BaseModel):
    file: workspace_files.WorkspaceFileSummary
    chunk: workspace_files.WorkspaceDocumentChunkItem
    expires_at: datetime


def get_preview_claims_from_header(
    x_openrag_preview_token: str | None,
) -> PreviewTokenClaims:
    token = (x_openrag_preview_token or "").strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Preview token required",
        )
    try:
        return decode_preview_token(token)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid preview token",
        ) from exc


def _get_preview_claims_dependency(
    x_openrag_preview_token: str | None = Header(
        default=None,
        alias="X-OpenRag-Preview-Token",
    ),
) -> PreviewTokenClaims:
    return get_preview_claims_from_header(x_openrag_preview_token)


def resolve_claims_file_and_chunk(
    db: Session,
    claims: PreviewTokenClaims,
) -> tuple[FileModel, DocumentChunk]:
    file = (
        db.query(FileModel)
        .filter(
            FileModel.id == claims.file_id,
            FileModel.workspace_id == claims.workspace_id,
            FileModel.deleted_at.is_(None),
        )
        .first()
    )
    if file is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File not found",
        )
    if file.is_directory:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File is a directory",
        )

    chunk = (
        db.query(DocumentChunk)
        .filter(
            DocumentChunk.chunk_id == claims.chunk_id,
            DocumentChunk.file_id == file.id,
            DocumentChunk.workspace_id == claims.workspace_id,
        )
        .first()
    )
    if chunk is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chunk not found",
        )
    if claims.chunk_index is not None and chunk.chunk_index != claims.chunk_index:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="chunk_index does not match chunk",
        )
    return file, chunk


@router.get("/document-preview", response_model=EmbedDocumentPreviewResponse)
def get_embed_document_preview(
    claims: PreviewTokenClaims = Depends(_get_preview_claims_dependency),
    db: Session = Depends(get_db),
) -> EmbedDocumentPreviewResponse:
    file, chunk = resolve_claims_file_and_chunk(db, claims)
    return EmbedDocumentPreviewResponse(
        file=workspace_files.file_to_summary(file),
        chunk=workspace_files.document_chunk_to_response(chunk, file),
        expires_at=datetime.fromtimestamp(claims.exp, tz=timezone.utc),
    )


@router.get("/files/content")
def get_embed_file_content(
    claims: PreviewTokenClaims = Depends(_get_preview_claims_dependency),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    file, _chunk = resolve_claims_file_and_chunk(db, claims)
    workspace = workspace_files.get_workspace_or_404(db, claims.workspace_id)
    return workspace_files.build_workspace_file_content_response(file, workspace)


@router.get("/files/preview", response_model=workspace_files.WorkspaceFilePreviewResponse)
def get_embed_file_preview(
    claims: PreviewTokenClaims = Depends(_get_preview_claims_dependency),
    db: Session = Depends(get_db),
) -> workspace_files.WorkspaceFilePreviewResponse:
    file, _chunk = resolve_claims_file_and_chunk(db, claims)
    workspace = workspace_files.get_workspace_or_404(db, claims.workspace_id)
    return workspace_files.build_workspace_file_preview_response(file, workspace)


@router.get("/files/chunk-source", response_model=workspace_files.WorkspaceFilePreviewResponse)
def get_embed_chunk_source(
    claims: PreviewTokenClaims = Depends(_get_preview_claims_dependency),
    db: Session = Depends(get_db),
) -> workspace_files.WorkspaceFilePreviewResponse:
    file, _chunk = resolve_claims_file_and_chunk(db, claims)
    return workspace_files.build_workspace_file_chunk_source_response(
        db,
        claims.workspace_id,
        file,
    )
