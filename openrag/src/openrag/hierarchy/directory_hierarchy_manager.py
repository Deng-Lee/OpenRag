"""Directory hierarchy manager - aggregates children into directory L0/L1."""

from typing import List
from .models import DirectoryHierarchy
from .utils import truncate_to_tokens


class DirectoryHierarchyManager:
    """Manages directory-level hierarchy aggregation."""

    def __init__(self, l0_max_tokens: int = 1000):
        """Initialize manager.

        Args:
            l0_max_tokens: Maximum tokens for directory L0
        """
        self.l0_max_tokens = l0_max_tokens

    def aggregate_directory(
        self,
        directory_path: str,
        children_l0s: List[str],
        children_names: List[str] = None
    ) -> DirectoryHierarchy:
        """Aggregate children into directory L0/L1.

        Args:
            directory_path: Directory path
            children_l0s: L0 summaries of all children
            children_names: Optional names of children

        Returns:
            DirectoryHierarchy with l0 and l1
        """
        # Generate L0: directory summary
        l0 = self._generate_directory_l0(directory_path, children_l0s)

        # Generate L1: list of children with their summaries
        l1 = self._generate_directory_l1(children_l0s, children_names)

        return DirectoryHierarchy(l0=l0, l1=l1)

    def _generate_directory_l0(self, directory_path: str, children_l0s: List[str]) -> str:
        """Generate directory L0 summary.

        Args:
            directory_path: Directory path
            children_l0s: Children L0 summaries

        Returns:
            Directory summary
        """
        dir_name = directory_path.rstrip('/').split('/')[-1]

        # Build summary
        summary_parts = [
            f"目录: {directory_path}",
            f"包含: {len(children_l0s)} 个文档"
        ]

        # Add content overview from children
        if children_l0s:
            combined = ' '.join(children_l0s[:5])  # First 5 children
            overview = truncate_to_tokens(combined, 500)
            summary_parts.append(f"主要内容: {overview}")

        summary = '\n'.join(summary_parts)
        return truncate_to_tokens(summary, self.l0_max_tokens)

    def _generate_directory_l1(
        self,
        children_l0s: List[str],
        children_names: List[str] = None
    ) -> str:
        """Generate directory L1 overview.

        Args:
            children_l0s: Children L0 summaries
            children_names: Optional children names

        Returns:
            Directory overview listing children
        """
        if not children_names:
            children_names = [f"文档 {i+1}" for i in range(len(children_l0s))]

        lines = ["## 目录内容\n"]

        for name, l0 in zip(children_names, children_l0s):
            preview = truncate_to_tokens(l0, 100)
            lines.append(f"### {name}")
            lines.append(preview)
            lines.append("")

        return '\n'.join(lines)
