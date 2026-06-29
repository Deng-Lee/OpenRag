"""Logical path tree / listing for a workspace (service API helpers)."""

from __future__ import annotations

import posixpath
from typing import Any, List, Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from openrag.models.file import File

TREE_MAX_NODES = 5000
TREE_MAX_DEPTH = 50
CHILDREN_LIMIT = 1000


def _escape_ilike(value: str) -> str:
    """Escape % and _ wildcards for PostgreSQL ilike patterns."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _normalize_logical_path(path: str) -> str:
    """Same rules as ``files_api.validate_path`` without importing the API layer."""
    normalized = posixpath.normpath(path or "/")
    if ".." in normalized or normalized.startswith("/.."):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid path: path traversal detected",
        )
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    return normalized


def _parent_uri(uri: str) -> str:
    d = posixpath.dirname(uri)
    if d == "" or d == ".":
        return "/"
    return d


def _depth_from_prefix(prefix: str, uri: str) -> int:
    p = prefix.rstrip("/") or "/"
    u = uri.rstrip("/") or "/"
    if p == "/":
        return len([x for x in u.split("/") if x])
    if u == p:
        return 0
    if not u.startswith(p + "/"):
        return 0
    rest = u[len(p) + 1 :]
    return len([x for x in rest.split("/") if x])


def _under_prefix(uri: str, prefix: str) -> bool:
    p = prefix.rstrip("/") or "/"
    if p == "/":
        return True
    return uri == p or uri.startswith(p + "/")


def _query_subtree(db: Session, workspace_id: int, path_prefix: str) -> List[File]:
    p = path_prefix.rstrip("/") or "/"
    q = db.query(File).filter(File.workspace_id == workspace_id, File.deleted_at.is_(None))
    if p == "/":
        rows = q.all()
    else:
        rows = q.filter((File.uri == p) | (File.uri.startswith(p + "/"))).all()
    return [f for f in rows if _under_prefix(f.uri, path_prefix)]


def list_direct_children(db: Session, workspace_id: int, dir_path: str) -> List[File]:
    """
    Return immediate children (files and directories) of ``dir_path``.

    The directory row must exist in the database.
    """
    d = _normalize_logical_path(dir_path)
    dir_row = (
        db.query(File)
        .filter(
            File.workspace_id == workspace_id,
            File.uri == d,
            File.is_directory.is_(True),
            File.deleted_at.is_(None),
        )
        .first()
    )
    if dir_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Directory not found",
        )

    if d == "/":
        candidates = (
            db.query(File)
            .filter(File.workspace_id == workspace_id, File.uri != "/", File.deleted_at.is_(None))
            .all()
        )
        children = [f for f in candidates if _parent_uri(f.uri) == "/"]
    else:
        candidates = (
            db.query(File)
            .filter(File.workspace_id == workspace_id, File.uri.startswith(d + "/"), File.deleted_at.is_(None))
            .all()
        )
        children = [f for f in candidates if _parent_uri(f.uri) == d]

    if len(children) > CHILDREN_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Too many entries in directory (limit {CHILDREN_LIMIT})",
        )
    children.sort(key=lambda f: (not f.is_directory, f.name.lower()))
    return children


def list_entries_by_prefix(db: Session, workspace_id: int, path_prefix: str) -> List[File]:
    """
    Return all files/directories whose URI is under ``path_prefix`` (inclusive).

    Example:
    - prefix ``/docs`` includes ``/docs`` and ``/docs/...``.
    - prefix ``/`` includes all rows in the workspace.
    """
    prefix = _normalize_logical_path(path_prefix).rstrip("/") or "/"
    q = db.query(File).filter(File.workspace_id == workspace_id, File.deleted_at.is_(None))
    if prefix == "/":
        rows = q.all()
    else:
        rows = q.filter((File.uri == prefix) | (File.uri.startswith(prefix + "/"))).all()
    rows = [f for f in rows if _under_prefix(f.uri, prefix)]
    if len(rows) > TREE_MAX_NODES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Too many nodes under path (limit {TREE_MAX_NODES})",
        )
    rows.sort(key=lambda f: (not f.is_directory, f.uri.lower()))
    return rows


def get_file_document_by_path(db: Session, workspace_id: int, file_path: str) -> File:
    """Return a **file** (not directory) at ``file_path`` or 404 / 400."""
    p = _normalize_logical_path(file_path)
    row = (
        db.query(File)
        .filter(File.workspace_id == workspace_id, File.uri == p, File.deleted_at.is_(None))
        .first()
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File not found",
        )
    if row.is_directory:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path is a directory, not a file",
        )
    return row


def _iso(dt: Optional[Any]) -> Optional[str]:
    if dt is None:
        return None
    return dt.isoformat()


def _file_to_node(f: File) -> dict[str, Any]:
    return {
        "path": f.uri,
        "kind": "dir" if f.is_directory else "file",
        "name": f.name,
        "size": f.size,
        "mime_type": f.mime_type,
        "updated_at": _iso(f.updated_at),
        "children": [] if f.is_directory else None,
    }


def _direct_child_uris(prefix: str, by_uri: dict[str, File]) -> List[str]:
    """URIs one level below ``prefix`` (only rows present in ``by_uri``)."""
    out: List[str] = []
    for uri in by_uri:
        if uri == prefix:
            continue
        if _parent_uri(uri) == prefix:
            out.append(uri)
    out.sort(key=lambda u: (not by_uri[u].is_directory, by_uri[u].name.lower()))
    return out


def _build_subtree(
    prefix: str,
    by_uri: dict[str, File],
    depth: int,
) -> dict[str, Any]:
    if depth > TREE_MAX_DEPTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Tree too deep (limit {TREE_MAX_DEPTH})",
        )
    f = by_uri.get(prefix)
    if f is None:
        # synthetic root /
        node: dict[str, Any] = {
            "path": "/",
            "kind": "dir",
            "name": "",
            "size": 0,
            "mime_type": None,
            "updated_at": None,
            "children": [],
        }
    else:
        node = _file_to_node(f)

    ch = node.setdefault("children", [])
    for uri in _direct_child_uris(prefix, by_uri):
        child_f = by_uri[uri]
        if child_f.is_directory:
            ch.append(_build_subtree(uri, by_uri, depth + 1))
        else:
            ch.append(_file_to_node(child_f))
    return node


def build_nested_tree(
    db: Session,
    workspace_id: int,
    path_prefix: str,
) -> dict[str, Any]:
    """
    Build a nested tree under ``path_prefix``.

    - ``path_prefix`` ``/`` uses a synthetic directory root (no DB row required).
    - Any other prefix must exist as a **directory** row.
    """
    prefix = _normalize_logical_path(path_prefix)

    if prefix != "/":
        root_dir = (
            db.query(File)
            .filter(
                File.workspace_id == workspace_id,
                File.uri == prefix,
                File.is_directory.is_(True),
                File.deleted_at.is_(None),
            )
            .first()
        )
        if root_dir is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Directory not found",
            )

    flat = _query_subtree(db, workspace_id, prefix)
    if len(flat) > TREE_MAX_NODES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Too many nodes under path (limit {TREE_MAX_NODES})",
        )

    by_uri = {f.uri: f for f in flat}
    for f in flat:
        if _depth_from_prefix(prefix, f.uri) > TREE_MAX_DEPTH:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Tree too deep (limit {TREE_MAX_DEPTH})",
            )

    return _build_subtree(prefix, by_uri, 0)


def search_documents_by_name(
    db: Session,
    workspace_id: int,
    filename_pattern: str,
    path_prefix: str = "/",
    skip: int = 0,
    limit: int = 50,
) -> tuple[List[File], int]:
    """
    Search files (not directories) by name substring (case-insensitive).

    Returns (matching_files, total_count). Results sorted by name ascending.
    """
    pattern = filename_pattern.strip()
    q = db.query(File).filter(
        File.workspace_id == workspace_id,
        File.is_directory.is_(False),
        File.deleted_at.is_(None),
    )
    if pattern:
        escaped = _escape_ilike(pattern)
        q = q.filter(File.name.ilike(f"%{escaped}%"))

    # Optional path_prefix filter
    prefix = _normalize_logical_path(path_prefix).rstrip("/") or "/"
    if prefix != "/":
        q = q.filter(
            (File.uri == prefix) | (File.uri.startswith(prefix + "/"))
        )

    total = q.count()
    rows = q.order_by(File.name.asc()).offset(skip).limit(limit).all()
    return rows, total
