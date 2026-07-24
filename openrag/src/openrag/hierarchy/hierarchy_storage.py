"""Storage for hierarchy content (L0/L1/L2), OpenViking-style layout."""

import json
import os
import shutil
from pathlib import Path
from typing import Any, List, Optional, Union

from .models import HierarchyResult, DirectoryHierarchy, Section

_DEFAULT_STORAGE = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "storage", "hierarchies"
)

# OpenViking-style filenames (see OpenViking docs: context layers)
ABSTRACT_NAME = ".abstract.md"
OVERVIEW_NAME = ".overview.md"
CHUNKS_DIR = "chunks"

# Legacy filenames (still loaded if present)
LEGACY_L0 = "l0.txt"
LEGACY_L1 = "l1.json"
LEGACY_L2 = "l2.json"


def _coerce_l1_markdown(l1: Optional[Union[str, List[Any]]]) -> str:
    """Normalize L1 to a single markdown string for .overview.md."""
    if l1 is None:
        return ""
    if isinstance(l1, str):
        return l1
    if isinstance(l1, list):
        if not l1:
            return ""
        first = l1[0]
        if isinstance(first, Section):
            parts = ["# Document overview\n\n"]
            for s in l1:
                parts.append(f"## {s.title}\n\n{s.content}\n\n")
            return "".join(parts)
        if isinstance(first, dict):
            parts = ["# Document overview\n\n"]
            for item in l1:
                title = item.get("title", "")
                content = item.get("content", "")
                parts.append(f"## {title}\n\n{content}\n\n")
            return "".join(parts)
    return str(l1)


def _legacy_l1_json_to_markdown(l1_data: list) -> str:
    """Convert legacy l1.json list into overview markdown."""
    parts = ["# Document overview (imported from legacy l1.json)\n\n"]
    for s in l1_data:
        title = s.get("title", "")
        content = s.get("content", "")
        parts.append(f"## {title}\n\n{content}\n\n")
    return "".join(parts)


class HierarchyStorage:
    """Manages storage of hierarchy content to filesystem."""

    def __init__(self, base_path: str | None = None):
        """Initialize storage.

        Args:
            base_path: Base directory for hierarchy storage.
                       Falls back to HIERARCHY_STORAGE_PATH env var,
                       then to {project_root}/storage/hierarchies.
        """
        resolved = (
            base_path or os.environ.get("HIERARCHY_STORAGE_PATH") or _DEFAULT_STORAGE
        )
        self.base_path = Path(resolved).resolve()
        self.base_path.mkdir(parents=True, exist_ok=True)

    def _uri_parent_and_stem(self, file_uri: str) -> tuple[Path, str]:
        """L0/L1 与 chunks 分层：L0/L1 为同 parent 下平级 *.abstract.md / *.overview.md；chunks 仅放在 parent/<stem>/chunks/。"""
        if "://" in file_uri:
            workspace_slug, _, path = file_uri.partition("://")
            stem = path.replace("/", "_").replace("\\", "_").strip("_") or "root"
            parent = self.base_path / workspace_slug
        else:
            stem = file_uri.replace("/", "_").replace("\\", "_").strip("_") or "root"
            parent = self.base_path / "_legacy"
        return parent, stem

    def _get_uri_dir(self, file_uri: str, create: bool = True) -> Path:
        """仅用于 L2：parent/<stem>/（其下有 chunks/）。不再把 L0/L1 放在此目录内的隐藏文件名里。"""
        parent, stem = self._uri_parent_and_stem(file_uri)
        if create:
            parent.mkdir(parents=True, exist_ok=True)
        bundle = parent / stem
        if create:
            bundle.mkdir(parents=True, exist_ok=True)
        return bundle

    def _get_file_id_bundle_dir(self, file_id: int, create: bool = True) -> Path:
        """L2 根目录：base_path/<id>/chunks/"""
        d = self.base_path / str(file_id)
        if create:
            d.mkdir(parents=True, exist_ok=True)
        return d

    def _save_l2_chunks(self, file_dir: Path, l2: Optional[List]) -> None:
        """Write each chunk as chunks/NNNN.md; clear previous .md in that folder."""
        if not l2:
            chunks_dir = file_dir / CHUNKS_DIR
            if chunks_dir.is_dir():
                for p in chunks_dir.glob("*.md"):
                    try:
                        p.unlink()
                    except OSError:
                        pass
            return

        chunks_dir = file_dir / CHUNKS_DIR
        chunks_dir.mkdir(parents=True, exist_ok=True)
        for p in chunks_dir.glob("*.md"):
            try:
                p.unlink()
            except OSError:
                pass

        for i, chunk in enumerate(l2):
            text = getattr(chunk, "text", str(chunk))
            out = chunks_dir / f"{i:04d}.md"
            out.write_text(text, encoding="utf-8")

    def _load_l2_chunks(self, file_dir: Path) -> List[dict]:
        """Load chunk bodies from chunks/*.md (sorted)."""
        chunks_dir = file_dir / CHUNKS_DIR
        if not chunks_dir.is_dir():
            return []

        items = sorted(chunks_dir.glob("*.md"), key=lambda p: p.name)
        result: List[dict] = []
        for path in items:
            result.append(
                {
                    "text": path.read_text(encoding="utf-8"),
                    "source_path": str(path),
                }
            )
        return result

    def save_document_hierarchy(
        self,
        file_id: Optional[int] = None,
        file_uri: Optional[str] = None,
        l0: Optional[str] = None,
        l1: Optional[Union[str, List]] = None,
        l2: Optional[List] = None,
        hierarchy: Optional[HierarchyResult] = None,
    ) -> None:
        """Save document hierarchy: .abstract.md, .overview.md, chunks/*.md."""
        if hierarchy is not None:
            l0 = hierarchy.l0
            l1 = hierarchy.l1
            l2 = hierarchy.l2

        if file_uri:
            parent, stem = self._uri_parent_and_stem(file_uri)
            parent.mkdir(parents=True, exist_ok=True)
            if l0 is not None and l0 != "":
                (parent / f"{stem}.abstract.md").write_text(l0, encoding="utf-8")

            l1_md = _coerce_l1_markdown(l1)
            if l1_md:
                (parent / f"{stem}.overview.md").write_text(l1_md, encoding="utf-8")

            self._save_l2_chunks(parent / stem, l2)
        elif file_id:
            self.base_path.mkdir(parents=True, exist_ok=True)
            fid = str(file_id)
            if l0 is not None and l0 != "":
                (self.base_path / f"{fid}.abstract.md").write_text(l0, encoding="utf-8")

            l1_md = _coerce_l1_markdown(l1)
            if l1_md:
                (self.base_path / f"{fid}.overview.md").write_text(
                    l1_md, encoding="utf-8"
                )

            self._save_l2_chunks(self._get_file_id_bundle_dir(file_id), l2)
        else:
            raise ValueError("Either file_id or file_uri must be provided")

    def load_document_hierarchy(
        self, file_id: Optional[int] = None, file_uri: Optional[str] = None
    ) -> Optional[HierarchyResult]:
        """Load hierarchy; supports 平级 *.abstract.md、旧版目录内 .abstract.md、legacy l0.txt + l1.json。"""
        if file_uri:
            parent, stem = self._uri_parent_and_stem(file_uri)
            new_abs = parent / f"{stem}.abstract.md"
            new_ov = parent / f"{stem}.overview.md"
            bundle = parent / stem
            old_abs = bundle / ABSTRACT_NAME
            old_ov = bundle / OVERVIEW_NAME
            legacy_l0 = bundle / LEGACY_L0
            legacy_l1 = bundle / LEGACY_L1

            l0: Optional[str] = None
            l1: Optional[str] = None

            if new_abs.is_file():
                l0 = new_abs.read_text(encoding="utf-8")
            elif old_abs.is_file():
                l0 = old_abs.read_text(encoding="utf-8")
            elif legacy_l0.exists():
                l0 = legacy_l0.read_text(encoding="utf-8")

            if new_ov.is_file():
                l1 = new_ov.read_text(encoding="utf-8")
            elif old_ov.is_file():
                l1 = old_ov.read_text(encoding="utf-8")
            elif legacy_l1.exists():
                l1_data = json.loads(legacy_l1.read_text(encoding="utf-8"))
                l1 = _legacy_l1_json_to_markdown(l1_data)

            if l0 is None or l1 is None:
                return None

            l2 = self._load_l2_chunks(bundle)
            if not l2 and bundle.is_dir() and (bundle / LEGACY_L2).exists():
                raw = json.loads((bundle / LEGACY_L2).read_text(encoding="utf-8"))
                l2 = [{"text": item.get("text", ""), "source_path": ""} for item in raw]

            return HierarchyResult(l0=l0, l1=l1, l2=l2)

        if file_id:
            fid = str(file_id)
            bundle = self._get_file_id_bundle_dir(file_id, create=False)
            new_abs = self.base_path / f"{fid}.abstract.md"
            new_ov = self.base_path / f"{fid}.overview.md"
            old_abs = bundle / ABSTRACT_NAME
            old_ov = bundle / OVERVIEW_NAME
            legacy_l0 = bundle / LEGACY_L0
            legacy_l1 = bundle / LEGACY_L1

            l0 = None
            l1 = None

            if new_abs.is_file():
                l0 = new_abs.read_text(encoding="utf-8")
            elif old_abs.is_file():
                l0 = old_abs.read_text(encoding="utf-8")
            elif legacy_l0.exists():
                l0 = legacy_l0.read_text(encoding="utf-8")

            if new_ov.is_file():
                l1 = new_ov.read_text(encoding="utf-8")
            elif old_ov.is_file():
                l1 = old_ov.read_text(encoding="utf-8")
            elif legacy_l1.exists():
                l1_data = json.loads(legacy_l1.read_text(encoding="utf-8"))
                l1 = _legacy_l1_json_to_markdown(l1_data)

            if l0 is None or l1 is None:
                return None

            l2 = self._load_l2_chunks(bundle) if bundle.is_dir() else []
            if not l2 and bundle.is_dir() and (bundle / LEGACY_L2).exists():
                raw = json.loads((bundle / LEGACY_L2).read_text(encoding="utf-8"))
                l2 = [{"text": item.get("text", ""), "source_path": ""} for item in raw]

            return HierarchyResult(l0=l0, l1=l1, l2=l2)

        raise ValueError("Either file_id or file_uri must be provided")

    def save_directory_hierarchy(
        self,
        file_id: Optional[int] = None,
        file_uri: Optional[str] = None,
        hierarchy: Optional[DirectoryHierarchy] = None,
        l0: Optional[str] = None,
        l1: Optional[str] = None,
    ) -> None:
        """Save directory hierarchy as .abstract.md + .overview.md."""
        if file_uri:
            parent, stem = self._uri_parent_and_stem(file_uri)
            parent.mkdir(parents=True, exist_ok=True)
            if hierarchy:
                l0 = hierarchy.l0
                l1 = hierarchy.l1
            if l0:
                (parent / f"{stem}.abstract.md").write_text(l0, encoding="utf-8")
            if l1:
                (parent / f"{stem}.overview.md").write_text(l1, encoding="utf-8")
        elif file_id:
            self.base_path.mkdir(parents=True, exist_ok=True)
            fid = str(file_id)
            if hierarchy:
                l0 = hierarchy.l0
                l1 = hierarchy.l1
            if l0:
                (self.base_path / f"{fid}.abstract.md").write_text(l0, encoding="utf-8")
            if l1:
                (self.base_path / f"{fid}.overview.md").write_text(l1, encoding="utf-8")
        else:
            raise ValueError("Either file_id or file_uri must be provided")

    def load_directory_hierarchy(
        self, file_id: Optional[int] = None, file_uri: Optional[str] = None
    ) -> Optional[DirectoryHierarchy]:
        """Load directory hierarchy; supports new and legacy (l0.txt / l1.txt) names."""
        if file_uri:
            parent, stem = self._uri_parent_and_stem(file_uri)
            bundle = parent / stem
            new_abs = parent / f"{stem}.abstract.md"
            new_ov = parent / f"{stem}.overview.md"
            abstract_path = new_abs if new_abs.is_file() else bundle / ABSTRACT_NAME
            overview_path = new_ov if new_ov.is_file() else bundle / OVERVIEW_NAME
            legacy_l0 = bundle / LEGACY_L0
            legacy_l1 = bundle / "l1.txt"
        elif file_id:
            fid = str(file_id)
            bundle = self._get_file_id_bundle_dir(file_id, create=False)
            new_abs = self.base_path / f"{fid}.abstract.md"
            new_ov = self.base_path / f"{fid}.overview.md"
            abstract_path = new_abs if new_abs.is_file() else bundle / ABSTRACT_NAME
            overview_path = new_ov if new_ov.is_file() else bundle / OVERVIEW_NAME
            legacy_l0 = bundle / LEGACY_L0
            legacy_l1 = bundle / "l1.txt"
        else:
            raise ValueError("Either file_id or file_uri must be provided")

        l0: Optional[str] = None
        l1: Optional[str] = None

        if abstract_path.exists():
            l0 = abstract_path.read_text(encoding="utf-8")
        elif legacy_l0.exists():
            l0 = legacy_l0.read_text(encoding="utf-8")

        if overview_path.exists():
            l1 = overview_path.read_text(encoding="utf-8")
        elif legacy_l1.exists():
            l1 = legacy_l1.read_text(encoding="utf-8")

        if l0 is None or l1 is None:
            return None

        return DirectoryHierarchy(l0=l0, l1=l1)

    def load_l0(
        self, file_id: Optional[int] = None, file_uri: Optional[str] = None
    ) -> Optional[str]:
        """Load only L0 (abstract)."""
        if file_uri:
            parent, stem = self._uri_parent_and_stem(file_uri)
            bundle = parent / stem
            for p in (
                parent / f"{stem}.abstract.md",
                bundle / ABSTRACT_NAME,
                bundle / LEGACY_L0,
            ):
                if p.is_file():
                    return p.read_text(encoding="utf-8")
            return None

        if file_id:
            fid = str(file_id)
            bundle = self._get_file_id_bundle_dir(file_id, create=False)
            for p in (
                self.base_path / f"{fid}.abstract.md",
                bundle / ABSTRACT_NAME,
                bundle / LEGACY_L0,
            ):
                if p.is_file():
                    return p.read_text(encoding="utf-8")
            return None

        raise ValueError("Either file_id or file_uri must be provided")

    def load_l1(
        self, file_id: Optional[int] = None, file_uri: Optional[str] = None
    ) -> Optional[str]:
        """Load only L1 (overview) without creating or modifying hierarchy files."""
        if file_uri:
            parent, stem = self._uri_parent_and_stem(file_uri)
            bundle = parent / stem
            candidates = (
                parent / f"{stem}.overview.md",
                bundle / OVERVIEW_NAME,
                bundle / LEGACY_L1,
            )
        elif file_id:
            fid = str(file_id)
            bundle = self._get_file_id_bundle_dir(file_id, create=False)
            candidates = (
                self.base_path / f"{fid}.overview.md",
                bundle / OVERVIEW_NAME,
                bundle / LEGACY_L1,
            )
        else:
            raise ValueError("Either file_id or file_uri must be provided")
        for path in candidates:
            if not path.is_file():
                continue
            if path.name == LEGACY_L1:
                return _legacy_l1_json_to_markdown(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            return path.read_text(encoding="utf-8")
        return None

    def get_l0_path(self, file_uri: str) -> str:
        """Full path to L0 abstract file (新: 平级 *.abstract.md)。"""
        parent, stem = self._uri_parent_and_stem(file_uri)
        return str(parent / f"{stem}.abstract.md")

    def get_l1_path(self, file_uri: str) -> str:
        """Full path to L1 overview file (新: 平级 *.overview.md)。"""
        parent, stem = self._uri_parent_and_stem(file_uri)
        return str(parent / f"{stem}.overview.md")

    def get_l2_path(self, file_uri: str) -> str:
        """Full path to L2 chunks directory."""
        return str(self._get_uri_dir(file_uri, create=False) / CHUNKS_DIR)

    def get_chunk_file_path(self, file_uri: str, chunk_index: int) -> str:
        """本地 L2 下单个切片文件的绝对路径（与 chunks/NNNN.md 一致）。"""
        return str(
            self._get_uri_dir(file_uri, create=False)
            / CHUNKS_DIR
            / f"{chunk_index:04d}.md"
        )

    def delete_document_hierarchy(
        self, file_id: Optional[int] = None, file_uri: Optional[str] = None
    ) -> bool:
        """删除层级：平级 md、仅含 chunks 的 bundle 目录、旧版目录。"""
        did = False
        if file_uri:
            parent, stem = self._uri_parent_and_stem(file_uri)
            for p in (
                parent / f"{stem}.abstract.md",
                parent / f"{stem}.overview.md",
            ):
                if p.is_file():
                    p.unlink(missing_ok=True)
                    did = True
            bundle = parent / stem
            if bundle.exists() and bundle.is_dir():
                shutil.rmtree(bundle)
                did = True
            return did

        if file_id:
            fid = str(file_id)
            for p in (
                self.base_path / f"{fid}.abstract.md",
                self.base_path / f"{fid}.overview.md",
            ):
                if p.is_file():
                    p.unlink(missing_ok=True)
                    did = True
            bundle = self._get_file_id_bundle_dir(file_id, create=False)
            if bundle.exists() and bundle.is_dir():
                shutil.rmtree(bundle)
                did = True
            return did

        raise ValueError("Either file_id or file_uri must be provided")
