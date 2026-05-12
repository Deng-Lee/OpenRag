"""Smart update engine - determines when to update parent directories."""

import numpy as np
from typing import List, Optional


def cosine_similarity(vec1: np.ndarray, vec2: np.ndarray) -> float:
    """Calculate cosine similarity between two vectors.

    Args:
        vec1: First vector
        vec2: Second vector

    Returns:
        Cosine similarity (0-1)
    """
    dot_product = np.dot(vec1, vec2)
    norm1 = np.linalg.norm(vec1)
    norm2 = np.linalg.norm(vec2)

    if norm1 == 0 or norm2 == 0:
        return 0.0

    return float(dot_product / (norm1 * norm2))


class SmartUpdateEngine:
    """Determines when parent directories need updating based on content similarity."""

    def __init__(
        self,
        similarity_threshold: float = 0.95,
        embedding_engine=None,
        hierarchy_storage=None,
        db_session=None
    ):
        """Initialize engine.

        Args:
            similarity_threshold: Threshold for update decision (< threshold = update)
            embedding_engine: EmbeddingEngine instance
            hierarchy_storage: HierarchyStorage instance
            db_session: Database session
        """
        self.threshold = similarity_threshold
        self.embedding_engine = embedding_engine
        self.hierarchy_storage = hierarchy_storage
        self.db = db_session

    def should_update_parent(
        self,
        parent_path: str,
        new_child_l0: str
    ) -> bool:
        """Determine if parent directory needs updating.

        Compares old and new L0 vectors using cosine similarity.

        Args:
            parent_path: Parent directory path
            new_child_l0: L0 of newly added child

        Returns:
            True if parent should be updated
        """
        # This is a placeholder - full implementation in later tasks
        # For now, always return True (conservative)
        return True

    def propagate_update(
        self,
        file_path: str,
        file_l0: str
    ) -> List[str]:
        """Propagate update from file to ancestors.

        Uses short-circuit: stops when parent doesn't need update.

        Args:
            file_path: File path (URI)
            file_l0: File's L0 content

        Returns:
            List of directory paths that need updating
        """
        paths_to_update = []
        current_path = self._get_parent_path(file_path)
        current_l0 = file_l0

        while current_path:
            if self.should_update_parent(current_path, current_l0):
                paths_to_update.append(current_path)
                # Continue checking ancestors
                if self.hierarchy_storage:
                    current_l0 = self.hierarchy_storage.load_l0(file_uri=current_path) or ""
                current_path = self._get_parent_path(current_path)
            else:
                # Short-circuit: stop propagation
                break

        return paths_to_update

    def _get_parent_path(self, path: str) -> Optional[str]:
        """Get parent directory path.

        Args:
            path: File or directory path

        Returns:
            Parent path or None if at root
        """
        path = path.rstrip('/')
        if '/' not in path:
            return None

        return '/'.join(path.split('/')[:-1])
